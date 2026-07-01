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
    assert r.input_tokens == 9926            # usage は情報として保持 (cost/ログ用)
    # context_tokens は 0 固定: codex の usage は 1 ターンの全 API 往復の累積で瞬間占有にならない。
    # 占有率にすると毎ターン rotate するため 0 とし turn 数で rotate する (codex は自前で文脈圧縮)。
    assert r.context_tokens == 0
    assert r.cost_usd == 0.0                 # サブスク = 課金なし


def test_parse_codex_huge_cumulative_usage_does_not_overcount() -> None:
    """累積 usage が窓を遥かに超えても context_tokens は 0 = 毎ターン rotate を防ぐ (実測 2549% の是正)。"""
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        # 1 ターン内の多数ツール往復で累積 5.1M (= 窓 200k の 2549% 相当)
        '{"type":"turn.completed","usage":{"input_tokens":5000000,'
        '"cached_input_tokens":98000,"output_tokens":1000}}',
    ])
    r = parse_codex_jsonl(stdout, exit_code=0)
    assert r.context_tokens == 0        # 占有率には使わない (rotate を駆動させない)
    assert r.input_tokens == 5_000_000  # 情報としては保持


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


def test_parse_codex_usage_limit_event_is_rate_limited() -> None:
    """実 codex 0.135.0 の usage-limit 失敗 (2026-06-21 probe を逐語再現) を rate_limited に分類する。

    実機は **stderr が空**で、原因メッセージは ``error`` / ``turn.failed`` の JSON イベントに**だけ**
    乗る。旧実装はこの message を捨て failed=True にするだけで、blob (text+stderr) が空 →
    other 誤分類 → consec_err 累積 → circuit_open していた (本不具合の根本原因)。
    """
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"019ee9fb-9523-7131-985f-7f9badb63a25"}',
        '{"type":"turn.started"}',
        '{"type":"error","message":"You\'ve hit your usage limit. Visit '
        'https://chatgpt.com/codex/settings/usage to purchase more credits or try again at '
        'Jun 25th, 2026 6:26 AM."}',
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Visit '
        'https://chatgpt.com/codex/settings/usage to purchase more credits or try again at '
        'Jun 25th, 2026 6:26 AM."}}',
    ])
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")  # stderr 空 = 実機どおり
    assert r.is_error is True
    assert r.error_kind == "rate_limited"          # other ではなく rate_limited
    assert "usage limit" in r.text.lower()         # 原因が GUI で読める (旧実装は空テキスト)


def test_parse_codex_turn_failed_dict_message_captured() -> None:
    """``turn.failed`` の error が dict ({"message": ...}) でも message を分類に使える。"""
    stdout = '{"type":"turn.failed","error":{"message":"Rate limit reached for requests"}}'
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")
    assert r.error_kind == "rate_limited"
    assert "rate limit" in r.text.lower()


def test_parse_codex_auth_error_is_unavailable() -> None:
    """codex の認証切れは unavailable に分類する (loop が保険 claude へ graceful fallback)。

    claude の auth=fail-closed 全停止と違い、codex は二次奏者なので全体を止めず chain から外す。
    """
    stdout = ('{"type":"error","message":"Not authenticated. Please run codex login."}\n'
              '{"type":"turn.failed","error":{"message":"Not authenticated."}}')
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")
    assert r.is_error is True
    assert r.error_kind == "unavailable"


def test_parse_codex_unknown_error_still_other() -> None:
    """既知シグナルに当たらない失敗は従来どおり other (リトライ対象)。message は表示に昇格。"""
    stdout = '{"type":"turn.failed","error":{"message":"unexpected internal failure xyz"}}'
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")
    assert r.error_kind == "other"
    assert "unexpected internal failure" in r.text


def test_parse_codex_auth_words_in_agent_text_not_misclassified() -> None:
    """失敗ターンの agent_message に auth 語 ("/login" 等) が紛れても unavailable にしない。

    分類は制御チャネル (error/turn.failed + stderr) 限定。モデルの散文で codex を誤って恒久
    ブロックしないための gem-critic 指摘対応。ここでは制御チャネルは generic error なので other。
    """
    stdout = "\n".join([
        '{"type":"item.completed","item":{"type":"agent_message","text":'
        '"I edited the /login handler and the unauthorized path. authentication wired."}}',
        '{"type":"turn.failed","error":{"message":"internal error e123"}}',
    ])
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")
    assert r.error_kind == "other"  # auth 語は agent_text のみ → unavailable にしない


def test_parse_codex_retry_epoch() -> None:
    """codex の "try again at <date>" を epoch へ best-effort 変換 (解釈不能なら 0)。"""
    from datetime import datetime

    from llterm.host.codex_runner import _parse_codex_retry_epoch
    assert _parse_codex_retry_epoch("... try again at Jun 25th, 2026 6:26 AM.") == int(
        datetime(2026, 6, 25, 6, 26).timestamp())
    assert _parse_codex_retry_epoch("try again at Dec 1, 2026 11:00 PM") == int(
        datetime(2026, 12, 1, 23, 0).timestamp())  # 序数なし・PM 12h 換算
    assert _parse_codex_retry_epoch("no date here") == 0
    assert _parse_codex_retry_epoch("") == 0


def test_parse_codex_usage_limit_sets_resets_at() -> None:
    """usage-limit メッセージに "try again at" があれば rate_limit_resets_at を立てる
    (codex を実リセットまで benched にして 5 分ごとの再プローブ churn を防ぐ)。"""
    stdout = ('{"type":"turn.failed","error":{"message":'
              '"You\'ve hit your usage limit. try again at Jun 25th, 2026 6:26 AM."}}')
    r = parse_codex_jsonl(stdout, exit_code=1, stderr="")
    assert r.error_kind == "rate_limited"
    assert r.rate_limit_resets_at > 0


_SLEEP_CODEX = '''\
import time
time.sleep(30)
'''


def test_codex_timeout_returns_visible_reason(tmp_path: Path) -> None:
    """タイムアウトは空テキスト err=other で silent circuit_open せず、理由テキストを返す。"""
    runner = _scripted_codex(tmp_path, None, body=_SLEEP_CODEX)
    runner.timeout = 0.1  # 子は 30s sleep → watchdog が 0.1s で kill
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.error_kind == "other"
    assert res.text.strip()  # 空でない (GUI で「なぜ落ちたか」が読める)


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
    assert res.context_tokens == 0  # 累積 usage は占有率にしない (毎ターン rotate 防止)
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


_USAGE_LIMIT_CODEX = '''\
import json, sys
def p(ev): print(json.dumps(ev), flush=True)
p({"type": "thread.started", "thread_id": "th-rl"})
p({"type": "turn.started"})
p({"type": "error", "message": "You've hit your usage limit. try again at Jun 25th, 2026 6:26 AM."})
p({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
sys.exit(1)
'''


def test_codex_usage_limit_falls_back_to_claude_not_circuit_open(tmp_path: Path) -> None:
    """E2E: 実 subprocess の codex usage-limit 失敗で loop が circuit_open せず保険 claude へ切替える。

    本不具合 (2026-06-21) の end-to-end 回帰防止。codex を実子プロセスで回し、usage-limit を
    JSON イベントだけで返す (stderr 空 = 実機どおり)。修正前はここで err=other → 3 連続 →
    circuit_open でループが死んでいた。修正後は rate_limited 分類 → provider_switch → claude 継続。
    """
    from llterm.ctl.ledger import Ledger
    from llterm.host.loop import SessionLoop, TurnResult

    codex = _scripted_codex(tmp_path, None, body=_USAGE_LIMIT_CODEX)

    class _Ok:  # claude 役の保険 (成功 → rotate)
        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
            self.calls += 1
            return TurnResult(session_id, 150_000, 100, 150_000, 0.0, "ok", False, "", 1, 0)

        def cancel(self) -> None:
            pass

    claude = _Ok()
    events: list[tuple[str, dict]] = []
    loop = SessionLoop(
        runner=codex, fallback_runners=(claude,), workdir=tmp_path,
        ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=2,
        on_event=lambda k, d: events.append((k, d)),
    )
    outcome = loop.run()
    assert outcome.stop_reason != "circuit_open"          # ← 修正前はこれだった
    assert any(k == "rate_limited" for k, _ in events)    # codex が正しく rate_limited 分類
    assert any(k == "provider_switch" for k, _ in events)  # 保険へ切替
    assert claude.calls >= 1                               # claude が実際に走った


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
