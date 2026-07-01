# SPDX-License-Identifier: Apache-2.0
"""OpenAICompatRunner (Groq/Cerebras/OpenRouter/Ollama 汎用奏者) の回帰テスト。

実 network を叩かず、_post_json を差し替えて OpenAI 互換応答を注入する (課金/通信ゼロ)。
"""
from __future__ import annotations

import json
import time

from llterm.host.openai_compat_runner import (
    PROVIDERS,
    OpenAICompatRunner,
    _HttpError,
)


def _ok_response(content: str = "答えは 42", *, prompt_tokens: int = 100,
                 completion_tokens: int = 10, total: int | None = None) -> str:
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if total is not None:
        usage["total_tokens"] = total
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage,
    })


class _Scripted(OpenAICompatRunner):
    """_post_json を差し替えて固定応答/例外を返すテスト用 runner。"""

    def __init__(self, *, response: str = "", error: _HttpError | None = None, **kw) -> None:
        super().__init__(**kw)
        self._scripted_response = response
        self._scripted_error = error
        self.captured: list[dict] = []  # 送信 payload を記録

    def _post_json(self, url: str, headers: dict, payload: dict) -> str:
        self.captured.append({"url": url, "headers": headers, "payload": payload})
        if self._scripted_error is not None:
            raise self._scripted_error
        return self._scripted_response


# ─── provider 解決 ────────────────────────────────────────────────


def test_provider_registry_excludes_chinese_hosts() -> None:
    """中国系ホスト API (deepseek/qwen/glm/kimi/zhipu/moonshot) は同梱しない。"""
    keys = set(PROVIDERS)
    assert {"groq", "cerebras", "openrouter", "ollama", "gemini-api"} <= keys
    for banned in ("deepseek", "qwen", "glm", "kimi", "zhipu", "moonshot", "dashscope"):
        assert banned not in keys


def test_gemini_api_provider_defaults() -> None:
    """Gemini API (OpenAI 互換) provider = AI Studio エンドポイント + GEMINI_API_KEY。"""
    r = OpenAICompatRunner(provider="gemini-api")
    assert r._resolved_base_url() == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert r._resolved_model() == "gemini-2.5-flash"
    assert r._resolved_key_env() == "GEMINI_API_KEY"


def test_resolves_provider_defaults() -> None:
    r = OpenAICompatRunner(provider="groq")
    assert r._resolved_base_url() == "https://api.groq.com/openai/v1"
    assert r._resolved_model() == "llama-3.3-70b-versatile"
    assert r._resolved_key_env() == "GROQ_API_KEY"


def test_ollama_needs_no_key() -> None:
    r = OpenAICompatRunner(provider="ollama")
    assert r._resolved_key_env() == ""  # ローカル = キー不要


def test_explicit_overrides_win() -> None:
    r = OpenAICompatRunner(provider="groq", base_url="http://x/v1", model="m", api_key_env="K")
    assert r._resolved_base_url() == "http://x/v1"
    assert r._resolved_model() == "m"
    assert r._resolved_key_env() == "K"


# ─── 成功パス ─────────────────────────────────────────────────────


def test_run_turn_parses_content_and_usage(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "sk-test")
    r = _Scripted(provider="groq", response=_ok_response("hi", prompt_tokens=120,
                                                          completion_tokens=8, total=128))
    res = r.run_turn(prompt="q", session_id="s1", resume=False)
    assert res.is_error is False
    assert res.text == "hi"
    assert res.input_tokens == 120
    assert res.output_tokens == 8
    assert res.context_tokens == 128
    assert res.cost_usd == 0.0
    # payload に model と user メッセージが乗る + Bearer ヘッダ
    sent = r.captured[0]
    assert sent["payload"]["model"] == "llama-3.3-70b-versatile"
    assert sent["payload"]["messages"][-1] == {"role": "user", "content": "q"}
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["url"].endswith("/chat/completions")


def test_context_tokens_fallback_without_total(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response=_ok_response(prompt_tokens=30, completion_tokens=7))
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.context_tokens == 37  # total_tokens 無→ prompt+completion


def test_system_prompt_prepended(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response=_ok_response(), system_prompt="あなたは奏者")
    r.run_turn(prompt="q", session_id="s", resume=False)
    msgs = r.captured[0]["payload"]["messages"]
    assert msgs[0] == {"role": "system", "content": "あなたは奏者"}


# ─── within-session 会話保持 ──────────────────────────────────────


def test_resume_accumulates_history(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response=_ok_response("a1"))
    r.run_turn(prompt="q1", session_id="s", resume=False)
    r._scripted_response = _ok_response("a2")
    r.run_turn(prompt="q2", session_id="s", resume=True)  # 継続
    # 2 ターン目の messages に 1 ターン目の user/assistant が積まれている
    msgs = r.captured[1]["payload"]["messages"]
    contents = [(m["role"], m["content"]) for m in msgs]
    assert ("user", "q1") in contents
    assert ("assistant", "a1") in contents
    assert contents[-1] == ("user", "q2")


def test_resume_false_resets_history(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response=_ok_response("a1"))
    r.run_turn(prompt="q1", session_id="s", resume=False)
    r._scripted_response = _ok_response("a2")
    r.run_turn(prompt="q2", session_id="s2", resume=False)  # 新セッション = リセット
    msgs = r.captured[1]["payload"]["messages"]
    assert all(m["content"] != "q1" for m in msgs)  # 前履歴は消えている
    assert msgs[-1] == {"role": "user", "content": "q2"}


# ─── fail-closed / エラー分類 ─────────────────────────────────────


def test_missing_api_key_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    r = _Scripted(provider="groq", response=_ok_response())
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.is_error is True
    assert res.error_kind == "auth"
    assert not r.captured  # キー無 = HTTP を投げない (fail-closed)


def test_ollama_runs_without_key(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    r = _Scripted(provider="ollama", response=_ok_response("local"))
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.is_error is False
    assert res.text == "local"
    assert "Authorization" not in r.captured[0]["headers"]  # キー不要


def test_429_maps_to_rate_limited(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", error=_HttpError(429, "rate limit exceeded", retry_after=42))
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.is_error is True
    assert res.error_kind == "rate_limited"
    # Retry-After=42 (delta秒) は絶対 epoch (now+42) として格納される (11-fix:
    # delta を epoch フィールドに入れると rate-limit が効かなかった回帰の是正)。
    # 呼び出しと assert の間の clock tick 分の許容を持たせて epoch セマンティクスを検証。
    now = int(time.time())
    assert now + 40 <= res.rate_limit_resets_at <= now + 44


def test_401_maps_to_auth(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", error=_HttpError(401, "invalid api key"))
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.error_kind == "auth"


def test_500_maps_to_other(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", error=_HttpError(500, "server error"))
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.error_kind == "other"


def test_malformed_response_is_error(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response="not json{")
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.is_error is True
    assert res.error_kind == "other"


def test_cancel_is_sticky(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    r = _Scripted(provider="groq", response=_ok_response())
    r.cancel()
    res = r.run_turn(prompt="q", session_id="s", resume=False)
    assert res.error_kind == "cancelled"
    assert not r.captured  # cancel 後は HTTP を投げない


def test_on_stream_emits_init_text_result(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "k")
    seen: list[dict] = []
    r = _Scripted(provider="groq", response=_ok_response("yo"), on_stream=seen.append)
    r.run_turn(prompt="q", session_id="s", resume=False)
    kinds = [e["kind"] for e in seen]
    assert kinds == ["init", "text", "result"]
    assert seen[0]["model"] == "groq:llama-3.3-70b-versatile"
