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
        def _build_args(self, *, resume: bool, cwd: Path) -> list[str]:
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
        def _build_args(self, *, resume: bool, cwd: Path) -> list[str]:
            args = CodexRunner._build_args(self, resume=resume, cwd=cwd)
            captured.append(args)
            return [sys.executable, str(tmp_path / "fake_codex.py")]

    spy = Spy(on_stream=None)
    spy._thread_id = "prev-thread"
    spy.run_turn(prompt="p", session_id="s", resume=True, cwd=tmp_path)
    assert "resume" in captured[0]
    assert "prev-thread" in captured[0]


def test_resume_args_omit_exec_only_options(tmp_path: Path) -> None:
    """`codex exec resume` は -s/--sandbox・-C/--cd・--color を受け付けず、渡すと exit 2 で
    resume ターンが全失敗する (新規は成功・2 ターン目以降 err=other → circuit_open の主因)。
    ただし resume は -s が無いと sandbox を継承せず書けないため、resume が受け付ける
    `-c sandbox_mode=...` で sandbox を渡す。新規セッションは従来どおり -s で渡す。
    """
    runner = CodexRunner()
    runner._thread_id = "prev-thread"
    args = runner._build_args(resume=True, cwd=tmp_path)
    assert "resume" in args and "prev-thread" in args
    assert "-s" not in args and "--sandbox" not in args  # resume は -s 非対応
    assert "-C" not in args and "--cd" not in args
    assert "--color" not in args
    # sandbox は -c sandbox_mode で渡す (無いと resume ターンで書込み不可になる)
    assert "-c" in args
    assert any("sandbox_mode" in a for a in args)
    assert args[-1] == "-"  # プロンプトは stdin センチネル

    new_args = runner._build_args(resume=False, cwd=tmp_path)
    assert "-s" in new_args and "-C" in new_args and "--color" in new_args


# ─── 指示文 truncation 回帰 (codex.CMD への argv 改行切断バグ) ──────


def test_build_args_passes_prompt_via_stdin_sentinel(tmp_path: Path) -> None:
    """プロンプトは argv に置かず "-" (stdin センチネル) を末尾に置く。

    codex.CMD shim 経由だと argv の複数行プロンプトが cmd.exe に改行で切断されるため、
    argv にプロンプト本文を絶対に載せない (stdin で渡す) ことを契約として固定する。
    """
    args = CodexRunner()._build_args(resume=False, cwd=tmp_path)
    assert args[-1] == "-"  # PROMPT 位置 = stdin 指定


_ECHO_STDIN_CODEX = '''\
import json, sys
# 実 codex と同じく stdin/stdout を UTF-8 で扱う (本体は Popen(encoding="utf-8") で書込)
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = sys.stdin.read()  # codex は "-" で stdin からプロンプト全文を読む
def p(ev): print(json.dumps(ev, ensure_ascii=False), flush=True)
p({"type": "thread.started", "thread_id": "t"})
p({"type": "item.completed", "item": {"type": "agent_message", "text": data}})
p({"type": "turn.completed", "usage": {"input_tokens": 1}})
'''


def test_multiline_prompt_reaches_child_intact_via_stdin(tmp_path: Path) -> None:
    """複数行プロンプトが改行後も含めて全文 codex 子へ届く (argv truncation の回帰防止)。"""
    runner = _scripted_codex(tmp_path, None, body=_ECHO_STDIN_CODEX)
    prompt = (
        "セキュリティ監査タスク(read-only)。\n"
        "1) スキャンを実行。\n"
        "2) findings を triage する。\n"
        "3) docs/SECURITY_AUDIT.md にレポートを書く。"
    )
    res = runner.run_turn(prompt=prompt, session_id="s", resume=False, cwd=tmp_path)
    # 子が stdin から読み返した全文 = 渡した prompt と完全一致 (末尾の改行差のみ吸収)
    assert res.text.rstrip("\n") == prompt
    assert "triage" in res.text            # 2 行目 (旧バグでは消えていた)
    assert "SECURITY_AUDIT.md" in res.text  # 最終行 (旧バグでは消えていた)
