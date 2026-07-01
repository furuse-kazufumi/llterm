# SPDX-License-Identifier: Apache-2.0
"""llterm の Gemini エンジン — Gemini CLI を非対話で回す TurnRunner (agentic 奏者)。

Codex と同じく **ファイル編集/シェル実行ができる agent** なので、テキスト専用の
:mod:`openai_compat_runner` と違い編集タスクの第一級奏者になれる。レート制限/コスト削減
のため作業を Codex/Gemini/無料枠へ振り分けるオーケストラの一員。

Gemini CLI (`@google/gemini-cli`, 2026-06 時点 v0.46.0) の headless 仕様で確認した点:
- ``gemini --output-format json`` で **単一 JSON** ``{"response": "...", "stats": {...},
  "error": {...?}}`` を返す。``--output-format stream-json`` は NDJSON イベント (将来対応)。
- **プロンプトは piped stdin で渡せる** (stdin はプロンプトとして読まれる)。npm 配布で
  Windows は ``gemini.CMD`` shim = argv の複数行プロンプトは cmd.exe が改行で切断する
  ため (memory: feedback_npm_cli_shim_stdin_prompt)、**argv に載せず stdin** で渡す。
- 自走では tool 承認を自動化する ``--yolo`` が必要 (非対話で確認を待たない)。
- v0.46.0 で **trusted-folder ゲート**追加: ``--skip-trust`` (または env
  ``GEMINI_CLI_TRUST_WORKSPACE=true``) が無いと yolo が ``default`` に降格され承認待ちで
  止まる (2026-06-13 実機検証で確認)。自走には ``--skip-trust`` を付ける。
- ``-p`` は付けなくてよい: piped stdin + ``--output-format json`` (非 TTY) で headless 起動する
  (実機検証済)。プロンプトは stdin の入力としてそのまま渡る。
- モデルは ``-m`` (例 gemini-2.5-flash / gemini-3 系)。空なら CLI 既定。
- 認証は GEMINI_API_KEY env または OAuth キャッシュ (CLI が管理)。
- 終了コード: 0=成功 / 1=一般エラー・API失敗 / 42=入力エラー / 53=ターン上限。

ChatGPT Pro 同様、Google 個人アカウント無料枠 or API キーで動くため新たな従量課金を
避けやすい。詳細な認証/枠は環境依存なので runner は CLI に委ねる。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from llterm.host.loop import _NO_WINDOW, _RATE_LIMIT_SIGNALS, TurnResult, _as_int
from llterm.i18n import t

# Gemini CLI 個人無料枠 (Google One/unpaid, OAuth) の提供停止日 (2026-06-18, Google 公式・延期なし)。
# これ以降は agentic な Gemini CLI 奏者は無料では動かない → Gemini API (provider "gemini-api")
# へ移行する。GUI はこの日付を見て期限間近/超過を通知する (project_llterm_orchestra_free_players)。
GEMINI_CLI_FREE_TIER_END = date(2026, 6, 18)
_FREE_TIER_SOON_DAYS = 7  # 何日前から「間近」通知を出すか


def gemini_cli_free_tier_status(today: date | None = None) -> tuple[str, int]:
    """Gemini CLI 無料枠の期限ステータスを返す。

    Returns ``(status, days)``: status = "expired" (超過) / "soon" (間近) / "ok"。
    days = 期限までの残日数 (負 = 超過日数)。``today`` を渡せばテストで固定できる。
    """
    today = today or date.today()
    days = (GEMINI_CLI_FREE_TIER_END - today).days
    if days < 0:
        return ("expired", days)
    if days <= _FREE_TIER_SOON_DAYS:
        return ("soon", days)
    return ("ok", days)

# 認証切れを示す既知シグナル (制御系チャネル=stderr/error 本文のみに適用)。
_AUTH_SIGNALS: tuple[str, ...] = (
    "unauthenticated", "unauthorized", "permission denied", "api key", "401", "403",
    "authentication", "credential", "could not load the default credentials",
)
# gemini 終了コード → エラー種別の補助 (42=入力 / 53=ターン上限 は other 扱い)。


# gemini 実 stats (v0.46, 2026-06-13 実機確認):
# stats.models.<model>.tokens = {input, prompt, candidates, total, cached, thoughts, tool}
# ★candidates は「候補数」(=1) でトークン数ではない → output は total - input で導く。
_TOKEN_INPUT_KEYS = ("input", "prompt", "input_tokens", "prompt_tokens", "promptTokenCount")
_TOKEN_OUTPUT_KEYS = ("output", "output_tokens", "completion_tokens", "candidatesTokenCount")
_TOKEN_TOTAL_KEYS = ("total", "total_tokens", "totalTokenCount")


def _first_int(d: dict, keys: tuple[str, ...]) -> int:
    for k in keys:
        if k in d:
            return _as_int(d.get(k))
    return 0


def _find_token_dict(obj: object) -> dict | None:
    """input 系 + (total 系 or output 系) を併せ持つ token dict を再帰探索する。"""
    if not isinstance(obj, dict):
        return None
    has_in = any(k in obj for k in _TOKEN_INPUT_KEYS)
    has_amt = any(k in obj for k in _TOKEN_TOTAL_KEYS) or any(k in obj for k in _TOKEN_OUTPUT_KEYS)
    if has_in and has_amt:
        return obj
    for val in obj.values():
        found = _find_token_dict(val)
        if found is not None:
            return found
    return None


def _extract_tokens(stats: object) -> tuple[int, int]:
    """gemini stats から (input, output) トークンを best-effort で取り出す (不明は 0)。

    フラット形 (input_tokens/output_tokens) とネスト形 (models.<m>.tokens) の両対応。
    total があれば output = total - input で導く (candidates=候補数を誤って使わない)。
    取れなくても loop は context_tokens=0 のとき設定窓を使うので安全 (fail-safe)。
    """
    tok = _find_token_dict(stats)
    if tok is None:
        return 0, 0
    in_tok = _first_int(tok, _TOKEN_INPUT_KEYS)
    total = _first_int(tok, _TOKEN_TOTAL_KEYS)
    out_tok = max(0, total - in_tok) if total else _first_int(tok, _TOKEN_OUTPUT_KEYS)
    return in_tok, out_tok


def parse_gemini_json(stdout: str, *, exit_code: int, stderr: str = "") -> TurnResult:
    """``gemini --output-format json`` の単一 JSON を 1 ターン結果へ defensively パースする。"""
    text = ""
    err_obj: object = None
    obj: object = None
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        text = str(obj.get("response") or "")
        err_obj = obj.get("error")
    in_tok, out_tok = _extract_tokens(obj.get("stats") if isinstance(obj, dict) else None)

    is_error = exit_code != 0 or bool(err_obj) or obj is None
    error_kind = ""
    if is_error:
        blob = (json.dumps(err_obj, ensure_ascii=False) if err_obj else "")
        blob = (blob + "\n" + stderr).lower()  # モデル応答文 (text) は分類に含めない (誤分類防止)
        if any(s in blob for s in _RATE_LIMIT_SIGNALS) or "quota" in blob or "429" in blob:
            error_kind = "rate_limited"
        elif any(s in blob for s in _AUTH_SIGNALS):
            # Gemini は二次奏者。認証切れ・無料枠失効は codex と同じく "unavailable" にして
            # ループを止めず chain から外す (fallback)。error_kind="auth" は主奏者 claude 専用で
            # loop がループ全体を fail-closed 停止させる値のため使わない (J2, loop.py:1027)。
            error_kind = "unavailable"
        else:
            error_kind = "other"

    return TurnResult(
        session_id="",  # gemini headless は一発実行 (server セッション維持なし)
        input_tokens=in_tok, output_tokens=out_tok,
        context_tokens=in_tok + out_tok, cost_usd=0.0, text=text,
        is_error=is_error, error_kind=error_kind, num_turns=1, raw_exit=exit_code,
    )


def summarize_gemini_event(ev: object) -> list[dict]:
    """stream-json イベント (将来対応) を GUI 表示用 dict 列へ要約する。

    現状は ``--output-format json`` (単一オブジェクト) 主体だが、将来 stream-json に
    切り替えても GUI を変えずに済むよう、Codex と同じ kind 体系へ揃える土台を用意する。
    """
    if not isinstance(ev, dict):
        return []
    etype = str(ev.get("type") or "")
    if etype in ("assistant", "content", "response"):
        text = str(ev.get("text") or ev.get("content") or ev.get("response") or "")
        return [{"kind": "text", "text": text}] if text.strip() else []
    if etype in ("tool", "tool_call", "tool_use"):
        return [{"kind": "tool_use", "name": str(ev.get("name") or "tool"),
                 "detail": str(ev.get("detail") or "")}]
    if etype == "init":
        return [{"kind": "init", "model": str(ev.get("model") or "gemini"),
                 "session_id": str(ev.get("session_id") or "")}]
    return []


@dataclass
class GeminiRunner:
    """Gemini CLI を ``gemini --output-format json`` で 1 ターン回す TurnRunner (端末なし)。

    - プロンプトは **stdin** で渡す (argv truncation 回避)。
    - ``--yolo`` で tool 承認を自動化 (自走で確認を待たない)。
    - 各ターンは独立した一発実行 (server セッション維持なし)。within-session の継続は
      ワークスペースのファイル + 注入プロンプトに依存する (codex の thread resume とは別)。
    """

    exe: str = "gemini"
    model: str = ""           # "" = gemini CLI 既定 (現行 gemini-2.5-flash / gemini-3 系)
    yolo: bool = True         # tool 承認を自動化 (自走で確認待ちにしない)
    skip_trust: bool = True   # trusted-folder ゲートを skip (無いと yolo が降格し承認待ちで止まる)
    output_format: str = "json"
    timeout: float = 7200.0
    extra_args: Sequence[str] = ()
    on_stream: Callable[[dict], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)
    _interrupted: bool = field(default=False, repr=False, compare=False)  # 緊急注入の一発中断

    def _resolved_exe(self) -> str:
        """gemini の実体を解決する。npm 配布で Windows は gemini.CMD shim が正規なので
        codex 同様 .cmd/.bat を受け入れる (プロンプトは stdin 渡しで注入面は無い)。"""
        return shutil.which(self.exe) or self.exe

    def _build_args(self) -> list[str]:
        """gemini の引数列を組む。**プロンプトは argv に置かず stdin で渡す**。"""
        args = [self._resolved_exe(), "--output-format", self.output_format]
        if self.yolo:
            args.append("--yolo")
        if self.skip_trust:
            args.append("--skip-trust")  # 無いと headless で yolo が降格し承認待ちで止まる
        if self.model:
            args += ["-m", self.model]
        args += [*self.extra_args]
        return args

    def _notify_stream(self, line: str) -> None:
        if self.on_stream is None:
            return
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        for item in summarize_gemini_event(ev):
            try:
                self.on_stream(item)
            except Exception:  # noqa: BLE001
                pass

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        args = self._build_args()
        with self._lock:
            if self._cancelled:
                return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
        self._notify_stream(json.dumps({"type": "init", "model": self.model or "gemini"}))
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            # gemini 未導入 = 使用不能 → loop が別プロバイダへ即フォールバック (silent circuit 回避)
            return TurnResult(session_id, 0, 0, 0, 0.0, t("runner.gemini.not_found"),
                              True, "unavailable", 0, 127)
        with self._lock:
            self._proc = proc
            kill_now = self._cancelled
        if kill_now:
            self._kill(proc)

        # プロンプトを stdin へ書き切って EOF (gemini は piped stdin をプロンプトとして読む)。
        # 別スレッドで書くことで stdout を読む前に大きな prompt を書いてもデッドロックしない。
        def _feed_stdin() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
        stdin_thread.start()

        timed_out = threading.Event()

        def _on_timeout() -> None:
            if proc.poll() is not None:
                return
            timed_out.set()
            self._kill(proc)

        watchdog = threading.Timer(self.timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        err_buf: list[str] = []

        def _drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                for eline in proc.stderr:
                    err_buf.append(eline)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        out = ""
        try:
            assert proc.stdout is not None
            out = proc.stdout.read()  # --output-format json は単一オブジェクト = 全読み
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._kill(proc)
        except (OSError, ValueError):
            self._kill(proc)
        finally:
            watchdog.cancel()
            stderr_thread.join(timeout=5)
            stdin_thread.join(timeout=5)
            with self._lock:
                self._proc = None

        with self._lock:
            cancelled = self._cancelled
            interrupted = self._interrupted
            self._interrupted = False  # 一発: 次の run_turn は通常起動できる
        if cancelled:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0,
                              proc.returncode or -1)
        if interrupted:  # 緊急注入による中断 = 停止ではない
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "interrupted", 0,
                              proc.returncode or -1)
        if timed_out.is_set():
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "other", 0, -1)
        exit_code = proc.returncode if proc.returncode is not None else -1
        res = parse_gemini_json(out, exit_code=exit_code, stderr="".join(err_buf))
        # session_id は呼び出し側 (loop) の uuid を保持して返す (gemini 側 session は無いため)
        res_with_sid = TurnResult(
            session_id=session_id, input_tokens=res.input_tokens, output_tokens=res.output_tokens,
            context_tokens=res.context_tokens, cost_usd=res.cost_usd, text=res.text,
            is_error=res.is_error, error_kind=res.error_kind, num_turns=res.num_turns,
            raw_exit=res.raw_exit,
        )
        if res_with_sid.text.strip():
            self._notify_stream(json.dumps({"type": "response", "text": res_with_sid.text}))
        return res_with_sid

    def cancel(self) -> None:
        """Gemini ターンをプロセスツリーごと安全に kill する (恒久・sticky)。"""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def interrupt(self) -> None:
        """現ターンだけを kill する (恒久 cancel と違い、次の run_turn は新規に起動できる)。緊急注入用。"""
        with self._lock:
            self._interrupted = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW, timeout=10)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
