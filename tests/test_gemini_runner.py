# SPDX-License-Identifier: Apache-2.0
"""GeminiRunner (Gemini CLI agentic 奏者) の回帰テスト。

実 gemini を呼ばず、偽の JSON を吐く python 子プロセスを注入して検証する (課金/通信ゼロ)。
parse_gemini_json は gemini --output-format json の単一オブジェクト形に準拠。
"""
from __future__ import annotations

import json
import sys
import time
import subprocess
import threading
from pathlib import Path

from datetime import date
import pytest

from llterm.host.gemini_runner import (
    GEMINI_CLI_FREE_TIER_END,
    GeminiRunner,
    _extract_tokens,
    gemini_cli_free_tier_status,
    parse_gemini_json,
    summarize_gemini_event,
)
from llterm.i18n import t


# ─── Gemini CLI 無料枠 期限通知 ───────────────────────────────────


def test_free_tier_end_date() -> None:
    assert GEMINI_CLI_FREE_TIER_END == date(2026, 6, 18)


def test_free_tier_status_ok_when_far() -> None:
    status, days = gemini_cli_free_tier_status(today=date(2026, 6, 1))
    assert status == "ok"
    assert days == 17


def test_free_tier_status_soon_within_week() -> None:
    status, days = gemini_cli_free_tier_status(today=date(2026, 6, 13))
    assert status == "soon"
    assert days == 5  # 今日(2026-06-13)時点では「間近」


def test_free_tier_status_expired_after() -> None:
    status, days = gemini_cli_free_tier_status(today=date(2026, 6, 20))
    assert status == "expired"
    assert days == -2  # 2 日超過


def test_free_tier_status_on_deadline_is_soon() -> None:
    status, days = gemini_cli_free_tier_status(today=date(2026, 6, 18))
    assert status == "soon"  # 当日 (days=0) はまだ "soon"
    assert days == 0


# ─── parse_gemini_json (純関数) ──────────────────────────────────


def test_parse_success_extracts_response() -> None:
    stdout = json.dumps({"response": "答えは 42", "stats": {"input_tokens": 30, "output_tokens": 5}})
    r = parse_gemini_json(stdout, exit_code=0)
    assert r.text == "答えは 42"
    assert r.is_error is False
    assert r.input_tokens == 30
    assert r.output_tokens == 5
    assert r.context_tokens == 35
    assert r.cost_usd == 0.0


def test_parse_error_on_nonzero_exit() -> None:
    r = parse_gemini_json('{"response": ""}', exit_code=1, stderr="boom")
    assert r.is_error is True
    assert r.error_kind == "other"


def test_parse_error_key_sets_error() -> None:
    stdout = json.dumps({"response": "", "error": {"message": "something failed"}})
    r = parse_gemini_json(stdout, exit_code=0)
    assert r.is_error is True


def test_parse_rate_limit_from_error() -> None:
    stdout = json.dumps({"error": {"message": "quota exceeded, try later"}})
    r = parse_gemini_json(stdout, exit_code=1)
    assert r.error_kind == "rate_limited"


def test_parse_auth_error_maps_to_unavailable() -> None:
    # 二次奏者 Gemini の認証エラーは 'unavailable' (chain から外し fallback)。'auth' は主奏者
    # claude 専用で loop 全体を fail-closed 停止させるため、二次奏者には使わない (J2)。
    stdout = json.dumps({"error": {"message": "request had invalid authentication credentials"}})
    r = parse_gemini_json(stdout, exit_code=1)
    assert r.error_kind == "unavailable"


def test_parse_malformed_json_is_error() -> None:
    r = parse_gemini_json("not json{", exit_code=0)
    assert r.is_error is True
    assert r.text == ""


def test_extract_tokens_real_gemini3_shape() -> None:
    """実 gemini v0.46 形 (2026-06-13 実機): models.<m>.tokens, output = total - input。
    candidates(=候補数 1) を output と誤認しないこと。"""
    stats = {"models": {"gemini-3-flash-preview": {"tokens": {
        "input": 11302, "prompt": 11302, "candidates": 1, "total": 11340,
        "cached": 0, "thoughts": 37, "tool": 0}}}}
    assert _extract_tokens(stats) == (11302, 38)  # 38 = 11340 - 11302


def test_extract_tokens_flat_openai_shape() -> None:
    assert _extract_tokens({"input_tokens": 30, "output_tokens": 5}) == (30, 5)


def test_extract_tokens_missing_is_zero() -> None:
    assert _extract_tokens({}) == (0, 0)
    assert _extract_tokens(None) == (0, 0)


def test_summarize_response_event() -> None:
    assert summarize_gemini_event({"type": "response", "text": "hi"}) == [{"kind": "text", "text": "hi"}]
    # init は GUI にストリーム開始 (model/session_id) を届けるため summarize が分岐する
    # (2026-07-01 修正)。model 欠損時は既定 "gemini"、session_id 欠損時は空文字。
    assert summarize_gemini_event({"type": "init"}) == [{"kind": "init", "model": "gemini", "session_id": ""}]
    assert summarize_gemini_event({"type": "init", "model": "gemini-2.5-flash", "session_id": "abc"}) == [
        {"kind": "init", "model": "gemini-2.5-flash", "session_id": "abc"}
    ]


# ─── _build_args ─────────────────────────────────────────────────


def test_build_args_no_prompt_in_argv_uses_stdin() -> None:
    """プロンプトは argv に載せない (stdin 渡し)。json/yolo/skip-trust フラグが付く。"""
    args = GeminiRunner(model="gemini-2.5-flash")._build_args()
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert "--yolo" in args
    assert "--skip-trust" in args  # v0.46 trusted-folder ゲート回避 (無いと承認待ちで止まる)
    assert args[args.index("-m") + 1] == "gemini-2.5-flash"
    # プロンプト本文や -p は argv に無い (stdin + 非TTY で headless 起動する実機検証済)
    assert "-p" not in args


def test_build_args_omits_model_when_empty() -> None:
    args = GeminiRunner()._build_args()
    assert "-m" not in args  # 空 = gemini 既定モデルに委ねる


def test_build_args_yolo_can_be_disabled() -> None:
    args = GeminiRunner(yolo=False)._build_args()
    assert "--yolo" not in args


def test_build_args_skip_trust_can_be_disabled() -> None:
    args = GeminiRunner(skip_trust=False)._build_args()
    assert "--skip-trust" not in args


# ─── GeminiRunner (偽の子プロセスで実走・課金ゼロ) ───────────────


_ECHO_STDIN_GEMINI = '''\
import json, sys
# gemini は piped stdin をプロンプトとして読む。受け取った全文を response に echo する
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = sys.stdin.read()
print(json.dumps({"response": data, "stats": {"input_tokens": 1, "output_tokens": 1}},
                 ensure_ascii=False))
'''

_SLEEP_GEMINI = '''\
import time
time.sleep(30)
'''


def _scripted_gemini(tmp_path: Path, body: str = _ECHO_STDIN_GEMINI) -> GeminiRunner:
    script = tmp_path / "fake_gemini.py"
    script.write_text(body, encoding="utf-8")

    class Scripted(GeminiRunner):
        def _build_args(self) -> list[str]:
            return [sys.executable, str(script)]

    return Scripted()


def test_multiline_prompt_reaches_child_intact_via_stdin(tmp_path: Path) -> None:
    """複数行プロンプトが改行後も全文 gemini 子へ届く (argv truncation の回帰防止)。"""
    runner = _scripted_gemini(tmp_path)
    prompt = "セッション再開。\n1) これをやる\n2) docs/REPORT.md に書く\n確認は求めない。"
    res = runner.run_turn(prompt=prompt, session_id="sid-1", resume=False, cwd=tmp_path)
    assert res.is_error is False
    assert res.text.rstrip("\n") == prompt   # 改行後も全文届く
    assert "docs/REPORT.md" in res.text
    assert res.session_id == "sid-1"          # loop の uuid を保持して返す


def test_cancel_before_start_is_sticky(tmp_path: Path) -> None:
    runner = _scripted_gemini(tmp_path)
    runner.cancel()
    t0 = time.monotonic()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert time.monotonic() - t0 < 2.0  # 子を spawn していない


def test_gemini_runner_idle_interrupt_does_not_poison_next_turn(tmp_path: Path) -> None:
    runner = _scripted_gemini(tmp_path)
    runner.interrupt()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is False
    assert res.error_kind == ""


def test_gemini_timeout_returns_visible_reason(tmp_path: Path) -> None:
    runner = _scripted_gemini(tmp_path, body=_SLEEP_GEMINI)
    runner.timeout = 0.1
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.error_kind == "other"
    assert res.text == t("runner.gemini.timeout")


def test_gemini_cancel_returns_even_if_pipe_never_closes(tmp_path: Path) -> None:
    """子ツリーを kill しても stdout が EOF に達しない異常でも cancel() 後 run_turn は有界で返る。

    回帰: gemini は proc.stdout.read() で全出力をブロッキング読みするため、taskkill /F /T しても
    孫が継承済みの stdout ハンドルを握って生存すると read() が永久ブロック → 強制停止しても
    止まらなかった (claude/codex と同じ失敗クラス)。_kill を無力化して pipe が閉じないケースを模擬。
    """
    runner = _scripted_gemini(tmp_path, body=_SLEEP_GEMINI)
    runner.timeout = 60.0
    runner._kill = lambda proc: None  # type: ignore[method-assign]  # kill しても pipe が閉じない模擬
    results: list = []

    def _run() -> None:
        results.append(runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path))

    th = threading.Thread(target=_run)
    th.start()
    proc = None
    try:
        for _ in range(400):  # 子が起動し _proc がセットされるまで待つ (最大 ~20s)
            proc = runner._proc
            if proc is not None:
                break
            time.sleep(0.05)
        assert proc is not None
        runner.cancel()
        th.join(20)  # 修正前は子の 30s sleep 完了までブロック → join 失敗
        assert not th.is_alive(), "cancel 後も run_turn が返らない (pipe ハングが解消していない)"
        assert results[0].error_kind == "cancelled"
    finally:
        if proc is not None:
            try:
                proc.kill()  # 無力化した _kill の代わりに孤児の子を実際に落とす
            except Exception:  # noqa: BLE001
                pass


def test_gemini_timeout_waits_again_after_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeIn:
        def write(self, _: str) -> None:
            return None

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeOut:
        def read(self) -> str:
            return ""

        def __iter__(self):  # gemini は _consume_stdout_bounded で行イテレーションする
            return iter(())

    class _FakeErr:
        def __iter__(self):
            return iter(())

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _FakeIn()
            self.stdout = _FakeOut()
            self.stderr = _FakeErr()
            self.returncode = None
            self.pid = 1234
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("gemini", timeout)
            self.returncode = -9
            return self.returncode

    fake = _FakeProc()

    def _fake_popen(*args, **kwargs):
        return fake

    class _ImmediateTimer:
        def __init__(self, timeout, fn):
            self.fn = fn
            self.daemon = False

        def start(self):
            self.fn()

        def cancel(self):
            return None

    runner = GeminiRunner()
    killed: list[_FakeProc] = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(runner, "_kill", lambda proc: killed.append(proc))
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "other"
    assert fake.wait_calls == 2
    assert killed and all(proc is fake for proc in killed)


def test_interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc(
    tmp_path: Path, monkeypatch
) -> None:
    fake = tmp_path / "fake_gemini_interrupt.py"
    fake.write_text(_ECHO_STDIN_GEMINI, encoding="utf-8")
    proc_box: dict[str, subprocess.Popen[str]] = {}
    original_popen = subprocess.Popen

    def _hooked_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        proc_box["proc"] = proc
        runner.interrupt()  # Popen 後にフラグを立て、post-spawn 判定がそれを拾うことを固定する
        return proc

    class SpawnInterruptRunner(GeminiRunner):
        def _build_args(self) -> list[str]:
            return [sys.executable, str(fake)]

    runner = SpawnInterruptRunner()
    monkeypatch.setattr(subprocess, "Popen", _hooked_popen)
    result = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert result.error_kind == "interrupted"
    assert runner._proc is None
    proc = proc_box["proc"]
    proc.wait(timeout=2.0)
