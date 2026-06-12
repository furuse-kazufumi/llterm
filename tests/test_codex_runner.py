# SPDX-License-Identifier: Apache-2.0
"""CodexRunner (レート制限フォールバック用エンジン) の回帰テスト。

実 codex を呼ばず、偽の JSONL を吐く python 子プロセスを注入して検証する (課金ゼロ)。
parse_codex_jsonl は実 codex 0.135.0 の probe 出力フォーマットに準拠。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from llterm.host.codex_runner import CodexRunner, parse_codex_jsonl, summarize_codex_event


# ─── parse_codex_jsonl (純関数) ──────────────────────────────────


def test_parse_codex_success() -> None:
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"th-123"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"答えは 42"}}',
        '{"type":"turn.completed","usage":{"input_tokens":9926,"cached_input_tokens":8064,'
        '"output_tokens":20}}',
    ])
    r = parse_codex_jsonl(stdout, exit_code=0)
    assert r.session_id == "th-123"          # codex thread_id
    assert r.text == "答えは 42"
    assert r.is_error is False
    assert r.context_tokens == 9926 + 8064   # input + cached = 占有近似
    assert r.cost_usd == 0.0                 # サブスク = 課金なし


def test_parse_codex_uses_last_agent_message() -> None:
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"途中"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"最終"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1}}',
    ])
    assert parse_codex_jsonl(stdout, exit_code=0).text == "最終"


def test_parse_codex_error_when_no_turn_completed() -> None:
    stdout = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}'
    r = parse_codex_jsonl(stdout, exit_code=0)  # turn.completed 無し = 異常
    assert r.is_error is True
    assert r.error_kind == "other"


def test_parse_codex_rate_limit_from_text() -> None:
    stdout = '{"type":"turn.failed","error":"rate limit exceeded"}'
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="rate limit exceeded, retry later")
    assert r.is_error is True
    assert r.error_kind == "rate_limited"


def test_summarize_codex_events() -> None:
    assert summarize_codex_event({"type": "thread.started", "thread_id": "t"}) == [
        {"kind": "init", "model": "codex", "session_id": "t"}]
    assert summarize_codex_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}) == [
        {"kind": "text", "text": "hi"}]
    cmd = summarize_codex_event(
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls -la"}})
    assert cmd == [{"kind": "tool_use", "name": "shell", "detail": "ls -la"}]
    assert summarize_codex_event({"type": "turn.started"}) == []


# ─── CodexRunner (偽の子プロセスで実走・課金ゼロ) ─────────────────


_FAKE_CODEX = '''\
import json, sys
def p(ev): print(json.dumps(ev), flush=True)
p({"type": "thread.started", "thread_id": "fake-thread"})
p({"type": "turn.started"})
p({"type": "item.completed", "item": {"type": "command_execution", "command": "echo hi"}})
p({"type": "item.completed", "item": {"type": "agent_message", "text": "codex done"}})
p({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}})
'''


def _scripted_codex(tmp_path: Path, on_stream, *, body: str = _FAKE_CODEX) -> CodexRunner:
    script = tmp_path / "fake_codex.py"
    script.write_text(body, encoding="utf-8")

    class Scripted(CodexRunner):
        def _build_args(self, *, prompt: str, resume: bool, cwd: Path) -> list[str]:
            return [sys.executable, str(script)]

    return Scripted(on_stream=on_stream)


def test_codex_runner_streams_and_parses(tmp_path: Path) -> None:
    seen: list[dict] = []
    runner = _scripted_codex(tmp_path, seen.append)
    res = runner.run_turn(prompt="p", session_id="ignored-uuid", resume=False, cwd=tmp_path)
    kinds = [it["kind"] for it in seen]
    assert kinds == ["init", "tool_use", "text", "result"]
    assert res.text == "codex done"
    assert res.is_error is False
    assert res.context_tokens == 150
    assert runner._thread_id == "fake-thread"  # 次ターンの resume 用に thread_id を保持


def test_codex_runner_cancel_before_start_is_sticky(tmp_path: Path) -> None:
    runner = _scripted_codex(tmp_path, None)
    runner.cancel()
    t0 = time.monotonic()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert time.monotonic() - t0 < 1.0  # 子を spawn していない


def test_codex_runner_resume_uses_thread_id(tmp_path: Path) -> None:
    captured: list[list[str]] = []
    runner = _scripted_codex(tmp_path, None)
    runner._thread_id = "prev-thread"

    class Spy(type(runner)):
        def _build_args(self, *, prompt: str, resume: bool, cwd: Path) -> list[str]:
            args = CodexRunner._build_args(self, prompt=prompt, resume=resume, cwd=cwd)
            captured.append(args)
            return [sys.executable, str(tmp_path / "fake_codex.py")]

    spy = Spy(on_stream=None)
    spy._thread_id = "prev-thread"
    spy.run_turn(prompt="p", session_id="s", resume=True, cwd=tmp_path)
    assert "resume" in captured[0]
    assert "prev-thread" in captured[0]
