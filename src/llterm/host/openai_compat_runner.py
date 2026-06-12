# SPDX-License-Identifier: Apache-2.0
"""OpenAI 互換 HTTP API を 1 ターン回す TurnRunner (オーケストラの汎用奏者)。

``POST {base_url}/chat/completions`` に従う provider を **単一実装**で奏者化する。
base_url + model + APIキー env 名の差分だけで Groq / Cerebras / OpenRouter / ローカル
Ollama 等を束ねられる (2026-06-13 外部AI調査の設計示唆)。レート制限/コスト削減のため、
機械的タスクを無料枠 provider に振る用途。

設計原則:
- **stdlib (urllib) のみ**: 重い依存を足さない (llterm の最小依存方針)。
- **within-session の会話保持**: ``resume=False`` で履歴リセット、``True`` で append。
  OpenAI 互換 API はステートレスなので messages 配列を runner 側で積む。
- **fail-closed**: APIキー欠落・HTTP エラー・タイムアウトで例外を投げず、is_error な
  ``TurnResult`` を返す (GUI / SessionLoop を殺さない)。401/403→auth, 429→rate_limited。
- **中国系ホスト API は同梱しない**: DeepSeek/Qwen/GLM 等はデータ所在=中国で FullSense
  「外部送信しない」哲学に抵触するため registry に載せない (ユーザー判断 2026-06-13)。
  ローカル Ollama は外部送信が無いので可。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from llterm.host.loop import _RATE_LIMIT_SIGNALS, TurnResult, _as_int
from llterm.i18n import t


@dataclass(frozen=True)
class OpenAICompatProvider:
    """OpenAI 互換 provider の接続情報。"""

    name: str
    base_url: str
    api_key_env: str   # "" = APIキー不要 (ローカル Ollama)
    default_model: str


# 同梱 provider (中国系ホスト API は意図的に除外。ローカル Ollama は外部送信無しなので可)。
# 無料枠は規約変更が激しいため上限値は持たず、実行時の 429 で動的にフォールバックする設計。
PROVIDERS: dict[str, OpenAICompatProvider] = {
    "groq": OpenAICompatProvider(
        "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "cerebras": OpenAICompatProvider(
        "cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b"),
    "openrouter": OpenAICompatProvider(
        "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        "meta-llama/llama-3.3-70b-instruct:free"),  # 既定は非中国系 free モデル
    "ollama": OpenAICompatProvider(
        "ollama", "http://localhost:11434/v1", "", "llama3.1:8b"),  # ローカル=外部送信なし
}


class _HttpError(Exception):
    """非 2xx HTTP 応答 (status + body を保持して error_kind 分類に使う)。"""

    def __init__(self, status: int, body: str, retry_after: int = 0) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


@dataclass
class OpenAICompatRunner:
    """OpenAI 互換 chat/completions を 1 ターン回す TurnRunner。

    - ``provider`` = :data:`PROVIDERS` のキー。``base_url`` / ``model`` / ``api_key_env`` を
      明示指定すると provider 既定を上書きする (未知 provider も直接 base_url 指定で使える)。
    - ``resume=False`` で会話履歴をリセット、``True`` で継続 (within-session の文脈保持)。
    - cost_usd=0.0 (無料枠 provider 前提)。実課金 provider を足す場合は将来拡張。
    """

    provider: str = "groq"
    base_url: str = ""        # "" = PROVIDERS[provider].base_url
    model: str = ""           # "" = PROVIDERS[provider].default_model
    api_key_env: str = ""     # "" = PROVIDERS[provider].api_key_env
    system_prompt: str = ""   # 任意の system メッセージ ("" = 付けない)
    timeout: float = 300.0
    extra_args: Sequence[str] = ()  # 互換のため (未使用)
    on_stream: Callable[[dict], None] | None = None
    _messages: list[dict] = field(default_factory=list, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)

    # ─── provider 解決 ──────────────────────────────────────────
    def _spec(self) -> OpenAICompatProvider | None:
        return PROVIDERS.get(self.provider)

    def _resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        spec = self._spec()
        return spec.base_url.rstrip("/") if spec else ""

    def _resolved_model(self) -> str:
        if self.model:
            return self.model
        spec = self._spec()
        return spec.default_model if spec else ""

    def _resolved_key_env(self) -> str:
        if self.api_key_env:
            return self.api_key_env
        spec = self._spec()
        return spec.api_key_env if spec else ""

    def _api_key(self) -> str:
        env = self._resolved_key_env()
        return os.environ.get(env, "") if env else ""

    def provider_label(self) -> str:
        """表示用 provider 名 (例: 'groq:llama-3.3-70b-versatile')。"""
        return f"{self.provider}:{self._resolved_model()}"

    # ─── HTTP (テストはここを差し替えて network を回避する) ──────
    def _post_json(self, url: str, headers: dict[str, str], payload: dict) -> str:
        """chat/completions に POST して応答 body 文字列を返す。非 2xx は _HttpError。"""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            retry_after = _as_int(exc.headers.get("Retry-After")) if exc.headers else 0
            raise _HttpError(exc.code, body, retry_after) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _HttpError(0, str(exc)) from exc

    def _notify(self, item: dict) -> None:
        if self.on_stream is None:
            return
        try:
            self.on_stream(item)
        except Exception:  # noqa: BLE001
            pass

    # ─── 1 ターン ───────────────────────────────────────────────
    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: object = None) -> TurnResult:
        with self._lock:
            if self._cancelled:
                return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
            if not resume:
                self._messages = []  # 新セッション = 履歴リセット

        base = self._resolved_base_url()
        model = self._resolved_model()
        if not base or not model:
            return TurnResult(session_id, 0, 0, 0, 0.0,
                              t("runner.openai.bad_provider", provider=self.provider),
                              True, "other", 0, -1)
        key_env = self._resolved_key_env()
        api_key = self._api_key()
        if key_env and not api_key:  # キー必須 provider でキー欠落 = fail-closed
            return TurnResult(session_id, 0, 0, 0, 0.0,
                              t("runner.openai.no_key", env=key_env, provider=self.provider),
                              True, "auth", 0, -1)

        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(self._messages)
        messages.append({"role": "user", "content": prompt})

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model, "messages": messages, "stream": False}

        self._notify({"kind": "init", "model": self.provider_label(), "session_id": session_id})
        try:
            raw = self._post_json(f"{base}/chat/completions", headers, payload)
        except _HttpError as exc:
            return self._error_result(session_id, exc)

        return self._parse(session_id, raw, prompt)

    def _parse(self, session_id: str, raw: str, prompt: str) -> TurnResult:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return TurnResult(session_id, 0, 0, 0, 0.0,
                              t("runner.openai.bad_response"), True, "other", 0, -1)
        choices = obj.get("choices") if isinstance(obj, dict) else None
        text = ""
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                text = str(msg.get("content") or "")
        usage = obj.get("usage") if isinstance(obj, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        in_tok = _as_int(usage.get("prompt_tokens"))
        out_tok = _as_int(usage.get("completion_tokens"))
        ctx = _as_int(usage.get("total_tokens")) or (in_tok + out_tok)

        # within-session 文脈保持: 今ターンの user/assistant を履歴へ積む
        with self._lock:
            self._messages.append({"role": "user", "content": prompt})
            self._messages.append({"role": "assistant", "content": text})

        if text.strip():
            self._notify({"kind": "text", "text": text})
        self._notify({"kind": "result", "duration_ms": 0, "is_error": False})
        return TurnResult(
            session_id=session_id, input_tokens=in_tok, output_tokens=out_tok,
            context_tokens=ctx, cost_usd=0.0, text=text, is_error=False,
            error_kind="", num_turns=1, raw_exit=0,
        )

    def _error_result(self, session_id: str, exc: _HttpError) -> TurnResult:
        blob = (exc.body or "").lower()
        if exc.status == 429 or any(s in blob for s in _RATE_LIMIT_SIGNALS):
            kind = "rate_limited"
        elif exc.status in (401, 403):
            kind = "auth"
        else:
            kind = "other"
        return TurnResult(
            session_id=session_id, input_tokens=0, output_tokens=0, context_tokens=0,
            cost_usd=0.0, text=(exc.body or str(exc))[:2000], is_error=True,
            error_kind=kind, num_turns=0, raw_exit=exc.status or -1,
            rate_limit_resets_at=exc.retry_after if kind == "rate_limited" and exc.retry_after else 0,
        )

    def cancel(self) -> None:
        """ターンを中断する (sticky)。urllib はブロッキングのため、新規リクエストの抑止 +
        timeout が実効的な境界。実行中の 1 リクエストは timeout 満了で打ち切られる。"""
        with self._lock:
            self._cancelled = True
