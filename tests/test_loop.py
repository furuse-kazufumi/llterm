# SPDX-License-Identifier: Apache-2.0
"""llterm L2 (host/loop.py) の回帰テスト — 仮想 claude (mock) で課金ゼロ検証。

ユーザー指示 (2026-06-11 夜)「仮想でデバッグ繰り返して」に従い、実 claude を一切呼ばず
FakeRunner で loop 駆動の全分岐 (rotate / auth 停止 / circuit breaker / budget /
should_stop / on_event) を検証する。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from llterm.ctl.ledger import Ledger
from llterm.host.loop import (
    DEFAULT_EXIT_PREP_PROMPT,
    DEFAULT_RESUME_PROMPT,
    Ledger as _LedgerReExport,  # noqa: F401  (import 経路の健全性確認)
    SessionLoop,
    TurnResult,
    main,
    parse_stream_json,
)


class FakeRunner:
    """script (各ターンの結果指定 dict のリスト) を順に返す仮想 claude。

    script が尽きたら既定 (閾値未満・非エラー・小 ctx) を返す。全呼び出しを calls に記録。
    """

    def __init__(self, script: list[dict] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[str, str, bool]] = []  # (prompt, session_id, resume)

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        self.calls.append((prompt, session_id, resume))
        spec = self.script.pop(0) if self.script else {}
        ctx = int(spec.get("ctx", 1_000))
        return TurnResult(
            session_id=session_id,
            input_tokens=ctx,
            output_tokens=int(spec.get("out", 100)),
            context_tokens=ctx,
            cost_usd=float(spec.get("cost", 0.0)),
            text=str(spec.get("text", "ok")),
            is_error=bool(spec.get("is_error", False)),
            error_kind=str(spec.get("error_kind", "")),
            num_turns=1,
            raw_exit=int(spec.get("exit", 0)),
        )

    def cancel(self) -> None:
        pass


def _loop(runner: FakeRunner, tmp_path: Path, **kw: object) -> SessionLoop:
    return SessionLoop(
        runner=runner,
        workdir=tmp_path,
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        **kw,  # type: ignore[arg-type]
    )


# ─── parse_stream_json (純関数) ──────────────────────────────────


def test_parse_success_extracts_usage_cost_text() -> None:
    stdout = "\n".join([
        '{"type":"system","subtype":"init","session_id":"abc-123","tools":[]}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
        '{"type":"result","subtype":"success","is_error":false,"session_id":"abc-123",'
        '"total_cost_usd":0.0234,"num_turns":3,"result":"done",'
        '"usage":{"input_tokens":1200,"output_tokens":340,'
        '"cache_read_input_tokens":5000,"cache_creation_input_tokens":100}}',
    ])
    r = parse_stream_json(stdout, exit_code=0)
    assert r.session_id == "abc-123"
    assert r.input_tokens == 1200
    assert r.output_tokens == 340
    assert r.context_tokens == 1200 + 5000 + 100  # input + cache 系
    assert r.cost_usd == pytest.approx(0.0234)
    assert r.text == "done"
    assert r.is_error is False
    assert r.error_kind == ""


def test_parse_context_tokens_uses_last_assistant_not_cumulative() -> None:
    """ctx 占有は最後のメイン assistant の usage を使う (result.usage 累計は過大なので不採用)。"""
    stdout = "\n".join([
        '{"type":"system","subtype":"init","session_id":"s"}',
        '{"type":"assistant","parent_tool_use_id":null,"message":{"usage":'
        '{"input_tokens":12028,"cache_read_input_tokens":20351,"cache_creation_input_tokens":9095}}}',
        '{"type":"assistant","parent_tool_use_id":null,"message":{"usage":'
        '{"input_tokens":2,"cache_read_input_tokens":29446,"cache_creation_input_tokens":12732}}}',
        '{"type":"result","subtype":"success","session_id":"s","total_cost_usd":0.5,'
        '"usage":{"input_tokens":12030,"output_tokens":617,'
        '"cache_read_input_tokens":49797,"cache_creation_input_tokens":21827}}',
    ])
    r = parse_stream_json(stdout, exit_code=0)
    # 最後の assistant: 2 + 29446 + 12732 = 42180 (瞬間占有)。result 累計 83654 ではない。
    assert r.context_tokens == 42180
    assert r.output_tokens == 617  # output/cost は result の値 (累計でよい)
    assert r.cost_usd == pytest.approx(0.5)


def test_parse_context_tokens_ignores_subagent_usage() -> None:
    """サブエージェント (parent_tool_use_id 非 null) の usage はメイン窓占有に含めない。"""
    stdout = "\n".join([
        '{"type":"assistant","parent_tool_use_id":null,"message":{"usage":'
        '{"input_tokens":1000,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}',
        '{"type":"assistant","parent_tool_use_id":"toolu_01","message":{"usage":'
        '{"input_tokens":99999,"cache_read_input_tokens":99999,"cache_creation_input_tokens":99999}}}',
        '{"type":"result","subtype":"success","session_id":"s","usage":{"input_tokens":5}}',
    ])
    r = parse_stream_json(stdout, exit_code=0)
    assert r.context_tokens == 1000  # サブエージェントの巨大 usage は無視 = メインの 1000


def test_parse_context_tokens_falls_back_to_result_when_no_assistant() -> None:
    """assistant usage が無い (error 等) ときだけ result.usage にフォールバックする。"""
    stdout = ('{"type":"result","subtype":"success","session_id":"s",'
              '"usage":{"input_tokens":100,"cache_read_input_tokens":50,'
              '"cache_creation_input_tokens":10}}')
    r = parse_stream_json(stdout, exit_code=0)
    assert r.context_tokens == 160  # フォールバック: 100 + 50 + 10


def test_parse_detects_auth_from_login_signal() -> None:
    r = parse_stream_json("", exit_code=1, stderr="Error: Please run /login to authenticate")
    assert r.is_error is True
    assert r.error_kind == "auth"


def test_parse_missing_result_is_error_other() -> None:
    stdout = '{"type":"system","subtype":"init","session_id":"x"}\n' \
             '{"type":"assistant","message":{}}'
    r = parse_stream_json(stdout, exit_code=0)  # result イベントが無い
    assert r.is_error is True
    assert r.error_kind == "other"


def test_parse_tolerates_broken_lines() -> None:
    stdout = 'not json\n{"type":"result","session_id":"z","usage":{"input_tokens":5}}\n{bad'
    r = parse_stream_json(stdout, exit_code=0)
    assert r.session_id == "z"
    assert r.input_tokens == 5


# ─── summarize_stream_event (リアルタイム表示の要約・純関数) ──────


def test_summarize_init_event() -> None:
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event(
        {"type": "system", "subtype": "init", "model": "claude-fable-5", "session_id": "abc-123"}
    )
    assert items == [{"kind": "init", "model": "claude-fable-5", "session_id": "abc-123"}]


def test_summarize_skips_hook_and_rate_limit_events() -> None:
    from llterm.host.loop import summarize_stream_event

    assert summarize_stream_event({"type": "system", "subtype": "hook_started"}) == []
    assert summarize_stream_event({"type": "system", "subtype": "hook_response"}) == []
    assert summarize_stream_event({"type": "rate_limit_event"}) == []
    assert summarize_stream_event("not a dict") == []
    assert summarize_stream_event({"type": "assistant", "message": "broken"}) == []


def test_summarize_assistant_text_and_tool_use() -> None:
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "応答テキスト"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi", "timeout": 5}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "D:/x.py"}},
        ]},
    })
    assert items[0] == {"kind": "text", "text": "応答テキスト"}
    assert items[1] == {"kind": "tool_use", "name": "Bash", "detail": "echo hi"}
    assert items[2] == {"kind": "tool_use", "name": "Edit", "detail": "D:/x.py"}


def test_summarize_tool_result_string_and_blocks() -> None:
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": "line1\nline2", "is_error": False},
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text", "text": "boom happened"}]},
        ]},
    })
    assert items[0] == {"kind": "tool_result", "is_error": False, "preview": "line1"}
    assert items[1] == {"kind": "tool_result", "is_error": True, "preview": "boom happened"}


def test_summarize_result_event() -> None:
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event({"type": "result", "duration_ms": 4300, "is_error": False})
    assert items == [{"kind": "result", "duration_ms": 4300, "is_error": False}]


def test_summarize_marks_subagent_events() -> None:
    """Task サブエージェント由来 (parent_tool_use_id 非 null) は subagent フラグで区別する。"""
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event({
        "type": "assistant", "parent_tool_use_id": "toolu_01ABC",
        "message": {"content": [{"type": "text", "text": "sub の応答"}]},
    })
    assert items == [{"kind": "text", "text": "sub の応答", "subagent": True}]
    # メイン (parent_tool_use_id が null) には付かない
    items = summarize_stream_event({
        "type": "assistant", "parent_tool_use_id": None,
        "message": {"content": [{"type": "text", "text": "main"}]},
    })
    assert items == [{"kind": "text", "text": "main"}]


def test_summarize_rate_limit_event() -> None:
    """レート制限 = サブスク自走の主制約。黙殺せず status / リセット時刻を伝える。"""
    from llterm.host.loop import summarize_stream_event

    items = summarize_stream_event({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1781251200, "rateLimitType": "five_hour"}})
    assert items == [{"kind": "rate_limit", "status": "rejected",
                      "resets_at": 1781251200, "rate_limit_type": "five_hour"}]


# ─── 実窓サイズ (modelUsage.contextWindow) と auth 判定の限定 ──────


def test_parse_extracts_context_window_from_model_usage() -> None:
    stdout = json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "session_id": "w",
        "result": "ok", "usage": {"input_tokens": 100},
        "modelUsage": {"claude-fable-5": {"contextWindow": 1_000_000}},
    })
    r = parse_stream_json(stdout, exit_code=0)
    assert r.context_window == 1_000_000


def test_used_pct_prefers_reported_context_window(tmp_path: Path) -> None:
    """1M 窓モデルでは設定既定 200K でなく実窓を分母にする (早すぎる rotate の防止)。"""
    loop = _loop(FakeRunner(), tmp_path, window_tokens=200_000)
    res = TurnResult("s", 0, 0, 140_000, 0.0, "", False, "", 1, 0, context_window=1_000_000)
    assert loop.used_pct(res) == pytest.approx(0.14)
    res_unknown = TurnResult("s", 0, 0, 140_000, 0.0, "", False, "", 1, 0)  # 報告なし → 設定値
    assert loop.used_pct(res_unknown) == pytest.approx(0.70)


def test_auth_not_inferred_from_transcript_content() -> None:
    """transcript (tool_result 等) 内の auth 語彙で auth に誤分類しない (自走の不要停止防止)。"""
    stdout = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "please run /login — authentication docs"}]}})
    r = parse_stream_json(stdout, exit_code=1)  # result 無し + exit 1 = エラーだが auth ではない
    assert r.is_error is True
    assert r.error_kind == "other"


def test_auth_detected_from_plain_diagnostic_line() -> None:
    """JSON でない診断行 (claude の生エラー出力) からは従来どおり auth を検出する。"""
    r = parse_stream_json("Error: OAuth token has expired. Please run /login\n", exit_code=1)
    assert r.error_kind == "auth"


# ─── レート制限の検知と自動再開 ───────────────────────────────────


def test_parse_rate_limit_from_blocking_status() -> None:
    """rate_limit_event の status が allowed 以外 + エラーなら rate_limited として resetsAt を拾う。"""
    stdout = "\n".join([
        '{"type":"rate_limit_event","rate_limit_info":'
        '{"status":"rejected","resetsAt":1781251200,"rateLimitType":"five_hour"}}',
    ])
    r = parse_stream_json(stdout, exit_code=1)  # result 無し + exit 1 = エラー
    assert r.error_kind == "rate_limited"
    assert r.rate_limit_resets_at == 1781251200
    assert r.rate_limit_status == "rejected"


def test_parse_rate_limit_from_text_signal() -> None:
    """診断テキストのレート制限シグナルからも rate_limited を検知する。"""
    r = parse_stream_json("Error: usage limit reached. Resets at 14:00\n", exit_code=1)
    assert r.error_kind == "rate_limited"


def test_parse_allowed_rate_limit_is_not_rate_limited() -> None:
    """status=allowed (制限内) の rate_limit_event は成功ターンを rate_limited にしない。"""
    stdout = "\n".join([
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1781251200}}',
        '{"type":"assistant","parent_tool_use_id":null,"message":{"usage":{"input_tokens":100}}}',
        '{"type":"result","subtype":"success","is_error":false,"session_id":"s","result":"ok",'
        '"usage":{"input_tokens":100}}',
    ])
    r = parse_stream_json(stdout, exit_code=0)
    assert r.is_error is False
    assert r.error_kind == ""


def test_auth_takes_priority_over_rate_limit() -> None:
    """auth と rate-limit が同時に見えたら auth を優先 (人間介在が必須なので)。"""
    stdout = '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1}}'
    r = parse_stream_json(stdout, exit_code=1, stderr="Please run /login")
    assert r.error_kind == "auth"


def test_rate_limit_auto_resume_waits_then_retries(tmp_path: Path) -> None:
    """rate_limited → resetsAt まで待機 (fake clock) → 同じターンを再試行して継続。"""
    # 1 ターン目を rate_limited、2 ターン目以降は成功 (rotate して max_sessions 到達)
    runner = FakeRunner([{"is_error": True, "error_kind": "rate_limited", "ctx": 0},
                         {"ctx": 150_000}])
    clock = {"t": 1000.0}
    slept: list[float] = []

    def now() -> float:
        return clock["t"]

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s  # fake clock を進める

    events: list[str] = []
    loop = SessionLoop(
        runner=runner, workdir=tmp_path, ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=1,
        now_fn=now, sleep_fn=sleep,
        on_event=lambda k, d: events.append(k),
    )
    outcome = loop.run()  # resets_at=0 → fallback 待ち → 再試行 (FakeRunner 2 ターン目で成功)
    assert outcome.stop_reason == "max_sessions"
    assert "rate_limited" in events
    assert "rate_limit_resumed" in events
    assert slept  # 実際に待機した


def test_provider_switch_on_rate_limit(tmp_path: Path) -> None:
    """Claude が rate_limited → 利用可能な fallback (Codex 役) に切り替えて継続する。"""
    primary = FakeRunner([{"is_error": True, "error_kind": "rate_limited"}])
    fallback = FakeRunner([{"ctx": 150_000}])  # 切替先は成功 → rotate → max_sessions
    events: list[tuple[str, dict]] = []
    loop = SessionLoop(
        runner=primary, fallback_runners=(fallback,), workdir=tmp_path,
        ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=2,
        now_fn=lambda: 0.0, sleep_fn=lambda s: None,
        on_event=lambda k, d: events.append((k, d)),
    )
    outcome = loop.run()
    assert outcome.stop_reason in ("max_sessions", "stopped")
    # 切替イベントが出て、fallback が実際に走った
    switch = [d for k, d in events if k == "provider_switch"]
    assert switch and switch[0]["provider"] == "FakeRunner"  # 名前は型名 (Codex でなくても切替する)
    assert len(fallback.calls) >= 1  # fallback が 1 ターン以上走った


def test_no_fallback_waits_and_resumes(tmp_path: Path) -> None:
    """fallback が無ければ従来どおり resetsAt まで待って同プロバイダで再開する。"""
    runner = FakeRunner([{"is_error": True, "error_kind": "rate_limited"}, {"ctx": 150_000}])
    slept: list[float] = []
    clock = {"t": 0.0}
    loop = SessionLoop(
        runner=runner, workdir=tmp_path, ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=1,
        now_fn=lambda: clock["t"],
        sleep_fn=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)),
        rate_limit_fallback_wait_s=10.0,
    )
    outcome = loop.run()
    assert outcome.stop_reason == "max_sessions"
    assert slept  # 待機した (切替先が無いので)


def test_all_providers_blocked_waits_for_earliest(tmp_path: Path) -> None:
    """primary も fallback も rate_limited なら、最も早い解除まで待ってから再開する。"""
    primary = FakeRunner([{"is_error": True, "error_kind": "rate_limited"}])
    fallback = FakeRunner([{"is_error": True, "error_kind": "rate_limited"}, {"ctx": 150_000}])
    clock = {"t": 0.0}
    slept: list[float] = []
    loop = SessionLoop(
        runner=primary, fallback_runners=(fallback,), workdir=tmp_path,
        ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=3,
        now_fn=lambda: clock["t"],
        sleep_fn=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)),
        rate_limit_fallback_wait_s=10.0,
    )
    outcome = loop.run()
    # primary rate_limited → fallback へ切替 → fallback も rate_limited → 両方ブロック → 待機 → 再開
    assert outcome.stop_reason in ("max_sessions", "stopped")
    assert slept  # 全ブロックで待機が発生


def test_unavailable_provider_falls_back(tmp_path: Path) -> None:
    """主プロバイダの実行ファイルが使用不能 (error_kind='unavailable') なら、待たずに
    別プロバイダへ即切替して継続する (claude が PATH 不在等で起動できない場合の保険)。"""
    primary = FakeRunner([{"is_error": True, "error_kind": "unavailable"}])
    fallback = FakeRunner([{"ctx": 150_000}])  # 切替先は成功 → rotate → max_sessions
    events: list[tuple[str, dict]] = []
    loop = SessionLoop(
        runner=primary, fallback_runners=(fallback,), workdir=tmp_path,
        ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=2,
        now_fn=lambda: 0.0, sleep_fn=lambda s: None,
        on_event=lambda k, d: events.append((k, d)),
    )
    outcome = loop.run()
    assert outcome.stop_reason in ("max_sessions", "stopped")
    assert any(k == "provider_unavailable" for k, _ in events)
    assert len(fallback.calls) >= 1  # fallback が実際に走った


def test_unavailable_provider_no_fallback_stops_clearly(tmp_path: Path) -> None:
    """使用不能で切替先も無ければ、3 連続エラー (circuit_open) でなく原因を明示して即停止する。"""
    runner = FakeRunner([{"is_error": True, "error_kind": "unavailable"}])
    loop = SessionLoop(
        runner=runner, workdir=tmp_path, ledger=Ledger(tmp_path / "l.jsonl"),
        window_tokens=200_000, threshold=0.70, max_sessions=3,
        now_fn=lambda: 0.0, sleep_fn=lambda s: None,
    )
    outcome = loop.run()
    assert outcome.stop_reason == "provider_unavailable"
    assert len(runner.calls) == 1  # 3 回叩かず 1 回で停止 (silent circuit_open を回避)


def test_rate_limit_wait_interrupted_by_stop(tmp_path: Path) -> None:
    """待機中に Stop されたら自走を停止する (待機は中断可能)。"""
    state = {"turns": 0}
    runner = FakeRunner([{"is_error": True, "error_kind": "rate_limited"}])
    loop = SessionLoop(
        runner=runner, workdir=tmp_path, ledger=Ledger(tmp_path / "l.jsonl"),
        max_sessions=1, now_fn=lambda: 0.0, sleep_fn=lambda s: None,
        # top-of-loop の check では止めず、rate_limited ターンを 1 回走らせてから待機中に停止
        should_stop=lambda: state["turns"] >= 1,
        rate_limit_fallback_wait_s=100.0,
        on_event=lambda k, d: state.__setitem__("turns", state["turns"] + (1 if k == "turn" else 0)),
    )
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert "stop during rate-limit wait" in outcome.detail


# ─── ClaudeRunner ストリーミング (偽の子プロセスで実走・課金ゼロ) ──


_FAKE_CHILD = '''\
import json, sys, time
def p(ev):
    print(json.dumps(ev), flush=True)
p({"type": "system", "subtype": "init", "model": "fake-model", "session_id": "fake-sid"})
p({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "streamed hello"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}})
p({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "tool output", "is_error": False}]}})
p({"type": "result", "subtype": "success", "is_error": False, "session_id": "fake-sid",
   "result": "done", "num_turns": 1, "total_cost_usd": 0.01, "duration_ms": 10,
   "usage": {"input_tokens": 100, "output_tokens": 20,
             "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0}})
'''


_HANGING_CHILD = '''\
import json, sys, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "hang-sid"}), flush=True)
time.sleep(60)
'''


def _scripted_claude_runner(tmp_path: Path, on_stream, *, script_body: str = _FAKE_CHILD,
                            **runner_kw: object) -> object:
    """_build_args を差し替え、claude の代わりに偽 JSONL を吐く python 子を回す。"""
    import sys as _sys

    from llterm.host.loop import ClaudeRunner

    script = tmp_path / "fake_claude.py"
    script.write_text(script_body, encoding="utf-8")

    class ScriptedRunner(ClaudeRunner):
        def _build_args(self, *, prompt: str, session_id: str, resume: bool) -> list[str]:
            return [_sys.executable, str(script)]

        def _exe_error(self) -> str:
            return ""  # 偽の子を直接起動するため実 claude の exe 解決チェックは無効化

    return ScriptedRunner(on_stream=on_stream, **runner_kw)  # type: ignore[arg-type]


def test_claude_runner_streams_events_and_parses_result(tmp_path: Path) -> None:
    """ターン完了を待たずに on_stream へ要約イベントが流れ、最終 TurnResult も正しい。

    これが「llterm に claude の応答が表示されない」バグの回帰テスト —
    旧実装は communicate() 全ブロックで、ターン中に何も通知されなかった。
    """
    seen: list[dict] = []
    runner = _scripted_claude_runner(tmp_path, seen.append)
    res = runner.run_turn(prompt="p", session_id="fake-sid", resume=False, cwd=tmp_path)
    kinds = [it["kind"] for it in seen]
    assert kinds == ["init", "text", "tool_use", "tool_result", "result"]
    assert {"kind": "text", "text": "streamed hello"} in seen
    assert res.text == "done"
    assert res.is_error is False
    assert res.context_tokens == 150  # input 100 + cache_read 50
    assert res.cost_usd == pytest.approx(0.01)


def test_claude_runner_stream_observer_failure_is_safe(tmp_path: Path) -> None:
    def boom(item: dict) -> None:
        raise RuntimeError("observer exploded")

    runner = _scripted_claude_runner(tmp_path, boom)
    res = runner.run_turn(prompt="p", session_id="fake-sid", resume=False, cwd=tmp_path)
    assert res.text == "done"  # 表示側の例外はターンを殺さない (fail-safe)
    assert res.is_error is False


def test_claude_runner_watchdog_kills_hanging_child(tmp_path: Path) -> None:
    """出力を止めてハングする子は watchdog がツリー kill し、タイムアウトエラーで返る。"""
    runner = _scripted_claude_runner(tmp_path, None, script_body=_HANGING_CHILD, timeout=2.0)
    t0 = time.monotonic()
    res = runner.run_turn(prompt="p", session_id="hang-sid", resume=False, cwd=tmp_path)
    assert time.monotonic() - t0 < 30  # 60s sleep の子を待たない
    assert res.is_error is True
    assert res.error_kind == "other"
    assert res.raw_exit == -1


def test_claude_runner_cancel_kills_running_turn(tmp_path: Path) -> None:
    """実行中ターンへの cancel() (Stop ボタン経路) は子をツリー kill し cancelled で返る。"""
    started = threading.Event()
    runner = _scripted_claude_runner(tmp_path, lambda item: started.set(),
                                     script_body=_HANGING_CHILD, timeout=60.0)
    results: list = []

    def _run() -> None:
        results.append(runner.run_turn(prompt="p", session_id="hang-sid", resume=False, cwd=tmp_path))

    th = threading.Thread(target=_run)
    th.start()
    assert started.wait(20)  # 子の最初の stream イベント到着 = 実行中であることの同期点
    runner.cancel()
    th.join(30)
    assert not th.is_alive()
    assert results[0].error_kind == "cancelled"


def test_claude_runner_cancel_before_start_is_sticky(tmp_path: Path) -> None:
    """ターン境界レース: 起動前に届いた cancel は消失せず、新しい子を起動しない。"""
    runner = _scripted_claude_runner(tmp_path, None)
    runner.cancel()
    t0 = time.monotonic()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert time.monotonic() - t0 < 1.0  # 子プロセスを spawn していない


def test_exe_npm_shim_is_rejected_with_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude.cmd (npm shim) しか無い環境では原因不明の exit 127 でなく明示エラーを返す。"""
    from llterm.host import loop as loop_mod

    monkeypatch.setattr(loop_mod.shutil, "which", lambda exe: r"C:\npm\claude.CMD")
    monkeypatch.setattr(loop_mod, "_native_claude_dirs", lambda: (tmp_path / "nope",))
    runner = loop_mod.ClaudeRunner()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.raw_exit == 127
    assert "npm shim" in res.text


def _make_fake_exe(native_dir: Path) -> Path:
    """native install 先に空の claude 実行ファイルを作って返す (プラットフォーム別の名前)。"""
    import sys as _sys

    native_dir.mkdir(parents=True, exist_ok=True)
    exe = native_dir / ("claude.exe" if _sys.platform == "win32" else "claude")
    exe.write_text("", encoding="utf-8")
    return exe


def test_exe_resolves_native_install_when_missing_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH に claude が無くても native install 先 (~/.local/bin) を直接見つけて起動する。

    回帰: 長時間稼働中の GUI が古い PATH を抱えたまま native claude へ移行すると
    shutil.which が None → FileNotFoundError → 空テキストの err=other を 3 連続で
    circuit_open していた (claude レビュー奏者 / claude ターンだけ即死)。
    """
    from llterm.host import loop as loop_mod

    native_dir = tmp_path / ".local" / "bin"
    exe = _make_fake_exe(native_dir)
    monkeypatch.setattr(loop_mod.shutil, "which", lambda _exe: None)  # PATH 上に無い
    monkeypatch.setattr(loop_mod, "_native_claude_dirs", lambda: (native_dir,))

    runner = loop_mod.ClaudeRunner()
    assert runner._exe_error() == ""  # 見つかるのでエラーにしない
    assert runner._resolved_exe() == str(exe)  # 絶対パスで起動する


def test_exe_prefers_native_over_shim_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH 上が shim (.cmd) でも native install 先に exe があればそちらを使い、エラーにしない。"""
    from llterm.host import loop as loop_mod

    native_dir = tmp_path / ".local" / "bin"
    exe = _make_fake_exe(native_dir)
    monkeypatch.setattr(loop_mod.shutil, "which", lambda _exe: r"C:\npm\claude.CMD")
    monkeypatch.setattr(loop_mod, "_native_claude_dirs", lambda: (native_dir,))

    runner = loop_mod.ClaudeRunner()
    assert runner._exe_error() == ""
    assert runner._resolved_exe() == str(exe)


def test_exe_not_found_anywhere_gives_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH にも native install 先にも claude が無ければ、空テキストの err=other でなく
    原因を明示したエラーを返す (silent circuit_open の回帰防止)。"""
    from llterm.host import loop as loop_mod

    monkeypatch.setattr(loop_mod.shutil, "which", lambda _exe: None)
    monkeypatch.setattr(loop_mod, "_native_claude_dirs", lambda: (tmp_path / "nope",))

    runner = loop_mod.ClaudeRunner()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.raw_exit == 127
    # 使用不能 = loop が別プロバイダへ即フォールバック (silent circuit_open を防ぐ)
    assert res.error_kind == "unavailable"
    assert res.text.strip()  # 空でない = 原因が分かる (謎の err=other を解消)


# ─── used_pct ────────────────────────────────────────────────────


def test_used_pct(tmp_path: Path) -> None:
    loop = _loop(FakeRunner(), tmp_path, window_tokens=200_000)
    res = TurnResult("s", 0, 0, 140_000, 0.0, "", False, "", 1, 0)
    assert loop.used_pct(res) == pytest.approx(0.70)


def test_used_pct_zero_window_safe(tmp_path: Path) -> None:
    loop = _loop(FakeRunner(), tmp_path, window_tokens=0)
    assert loop.used_pct(TurnResult("s", 0, 0, 999, 0.0, "", False, "", 1, 0)) == 0.0


# ─── rotate 判定 ─────────────────────────────────────────────────


def test_rotates_when_threshold_exceeded(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])  # 75% >= 70%
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1)
    outcome = loop.run()
    assert outcome.stop_reason == "max_sessions"
    assert outcome.sessions == 1
    assert outcome.turns == 2  # 作業 1 + exit準備 1
    # 最初は新セッション (resume=False)、exit準備は同セッション resume=True で exit_prep_prompt
    assert DEFAULT_RESUME_PROMPT in runner.calls[0][0]
    assert runner.calls[0][2] is False
    assert runner.calls[1][0] == DEFAULT_EXIT_PREP_PROMPT
    assert runner.calls[1][2] is True
    assert runner.calls[0][1] == runner.calls[1][1]  # 同 session_id


def test_continues_under_threshold_same_session(tmp_path: Path) -> None:
    runner = FakeRunner()  # 既定 ctx=1000 (常に閾値未満)
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70,
                 max_sessions=1, max_turns_per_session=3)
    outcome = loop.run()
    assert outcome.sessions == 1
    assert outcome.turns == 4  # 作業 3 (max_turns) + exit準備 1
    work_calls = [c for c in runner.calls if c[0] != DEFAULT_EXIT_PREP_PROMPT]
    assert len(work_calls) == 3
    assert work_calls[0][2] is False               # 1 回目は新セッション
    assert work_calls[1][2] is True                # 以降は同セッション resume
    assert work_calls[2][2] is True
    assert len({c[1] for c in work_calls}) == 1     # 全て同 session_id


def test_rotates_to_new_session_id(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}, {"ctx": 150_000}])  # 2 セッション連続 rotate
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=2)
    outcome = loop.run()
    assert outcome.sessions == 2
    work_sids = [c[1] for c in runner.calls if c[0] != DEFAULT_EXIT_PREP_PROMPT]
    assert len(set(work_sids)) == 2  # rotate で session_id が変わる (fresh context)


# ─── 停止条件 ─────────────────────────────────────────────────────


def test_auth_error_stops_fail_closed(tmp_path: Path) -> None:
    runner = FakeRunner([{"is_error": True, "error_kind": "auth"}])
    loop = _loop(runner, tmp_path, max_sessions=5)
    outcome = loop.run()
    assert outcome.stop_reason == "auth_required"
    assert outcome.sessions == 0
    assert outcome.turns == 1  # 認証切れで即停止 (暴走しない)


def test_circuit_breaker_opens_on_consecutive_errors(tmp_path: Path) -> None:
    runner = FakeRunner([{"is_error": True, "error_kind": "other"},
                         {"is_error": True, "error_kind": "other"}])
    loop = _loop(runner, tmp_path, max_sessions=5, max_consecutive_errors=2)
    outcome = loop.run()
    assert outcome.stop_reason == "circuit_open"
    assert outcome.turns == 2


def test_error_then_success_resets_consec(tmp_path: Path) -> None:
    runner = FakeRunner([{"is_error": True, "error_kind": "other"}, {"ctx": 1_000}])
    loop = _loop(runner, tmp_path, max_sessions=1, max_consecutive_errors=2,
                 max_turns_per_session=2)
    outcome = loop.run()
    # err(1) → success(consec reset, st=2 == max_turns) → rotate → 正常に max_sessions 到達
    assert outcome.stop_reason == "max_sessions"


def test_max_cost_stops(tmp_path: Path) -> None:
    runner = FakeRunner([{"cost": 0.03}, {"cost": 0.03}, {"cost": 0.03}])
    loop = _loop(runner, tmp_path, max_total_cost_usd=0.05, max_turns_per_session=50)
    outcome = loop.run()
    assert outcome.stop_reason == "max_cost"
    assert outcome.total_cost_usd == pytest.approx(0.06)  # 0.05 到達は次ターン頭で検知
    assert outcome.turns == 2


def test_should_stop_halts_immediately(tmp_path: Path) -> None:
    runner = FakeRunner()
    loop = _loop(runner, tmp_path, max_sessions=10, should_stop=lambda: True)
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert outcome.sessions == 0
    assert runner.calls == []  # 1 ターンも回さず停止


def test_rotate_then_stop_records_handoff(tmp_path: Path) -> None:
    """rotate 地点で Stop が届いたら、exit準備 (handoff) を記録してから停止する (graceful)。"""
    stop = {"flag": False}

    def on_event(kind: str, data: dict) -> None:
        if kind == "turn":
            stop["flag"] = True  # ターン完了直後〜rotate 分岐の間に Stop が届いた状況を模擬

    runner = FakeRunner([{"ctx": 150_000}])  # 75% >= 70% → rotate 分岐へ
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=2,
                 should_stop=lambda: stop["flag"], on_event=on_event)
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert len(runner.calls) == 2  # 作業ターン + exit準備 (handoff) を記録してから停止
    assert runner.calls[1][0] == DEFAULT_EXIT_PREP_PROMPT


def test_graceful_stop_runs_handoff_mid_session(tmp_path: Path) -> None:
    """セッション途中の graceful Stop は handoff (作業記録) を 1 回回してから停止する。"""
    calls = {"n": 0}

    def should_stop() -> bool:
        # 1 ターン走った後に停止要求 (top-of-loop の 2 回目の check で True)
        return calls["n"] >= 1

    def on_event(kind: str, data: dict) -> None:
        if kind == "turn":
            calls["n"] += 1

    handoff_seen: list[dict] = []
    runner = FakeRunner()  # 常に閾値未満
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1,
                 max_turns_per_session=10, should_stop=should_stop,
                 on_event=lambda k, d: (on_event(k, d), handoff_seen.append(d) if k == "handoff" else None))
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert len(handoff_seen) == 1  # handoff イベントが 1 回
    assert runner.calls[-1][0] == DEFAULT_EXIT_PREP_PROMPT  # 最後が作業記録ターン


def test_graceful_stop_no_handoff_without_work(tmp_path: Path) -> None:
    """まだ作業していない (session_turns==0) Stop は handoff を回さず即停止。"""
    runner = FakeRunner()
    loop = _loop(runner, tmp_path, max_sessions=5, should_stop=lambda: True)
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert runner.calls == []  # 1 ターンも回さない (handoff も無し)


def test_handoff_on_stop_false_skips_handoff(tmp_path: Path) -> None:
    calls = {"n": 0}
    runner = FakeRunner()
    loop = _loop(runner, tmp_path, max_sessions=1, max_turns_per_session=10,
                 handoff_on_stop=False,
                 should_stop=lambda: calls["n"] >= 1,
                 on_event=lambda k, d: calls.__setitem__("n", calls["n"] + (1 if k == "turn" else 0)))
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert all(c[0] != DEFAULT_EXIT_PREP_PROMPT for c in runner.calls)  # handoff 無し


def test_cancelled_turn_stops_loop_immediately(tmp_path: Path) -> None:
    """cancelled (Stop / 終了由来) のターンはリトライせず即停止する。"""
    runner = FakeRunner([{"is_error": True, "error_kind": "cancelled"}])
    loop = _loop(runner, tmp_path, max_sessions=5)
    outcome = loop.run()
    assert outcome.stop_reason == "stopped"
    assert outcome.turns == 1


# ─── 監査 ledger / on_event ───────────────────────────────────────


def test_ledger_records_events(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])
    led = tmp_path / "ledger.jsonl"
    loop = SessionLoop(runner=runner, workdir=tmp_path, ledger=Ledger(led),
                       window_tokens=200_000, threshold=0.70, max_sessions=1)
    loop.run()
    lines = led.read_text(encoding="utf-8").splitlines()
    events = {__import__("json").loads(ln)["event"] for ln in lines}
    assert {"session_start", "turn", "exit_prep"} <= events


def test_on_event_emits_progress(tmp_path: Path) -> None:
    seen: list[tuple[str, dict]] = []
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1,
                 on_event=lambda kind, data: seen.append((kind, data)))
    loop.run()
    kinds = [k for k, _ in seen]
    assert "session_start" in kinds
    assert "turn" in kinds
    assert "rotate" in kinds
    assert kinds[-1] == "stopped"
    turn_ev = next(d for k, d in seen if k == "turn")
    assert turn_ev["used_pct"] == pytest.approx(0.75)


def test_emits_task_event_with_prompt(tmp_path: Path) -> None:
    """各ターン前に task イベントで prompt を流す (GUI が実行内容を可視化できる)。"""
    seen: list[tuple[str, dict]] = []
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1,
                 on_event=lambda kind, data: seen.append((kind, data)))
    loop.run()
    tasks = [d for k, d in seen if k == "task"]
    assert tasks, "task イベントが流れていない"
    assert DEFAULT_RESUME_PROMPT in tasks[0]["prompt"]
    assert tasks[0]["injected"] is False  # 最初は通常の再開 prompt


def test_injected_task_marked_in_event(tmp_path: Path) -> None:
    """注入タスクが消費されるターンの task イベントは injected=True を持つ。"""
    injected = ["割り込みタスク X"]
    seen: list[tuple[str, dict]] = []
    runner = FakeRunner()  # 常に閾値未満
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1,
                 max_turns_per_session=3,
                 next_prompt=lambda: injected.pop(0) if injected else None,
                 on_event=lambda kind, data: seen.append((kind, data)))
    loop.run()
    tasks = [d for k, d in seen if k == "task"]
    injected_tasks = [d for d in tasks if d["injected"]]
    assert len(injected_tasks) == 1
    # 注入タスクが本体 (末尾に安全弁/監督などの指令が付く)
    assert injected_tasks[0]["prompt"].startswith("割り込みタスク X")


def test_on_event_failure_does_not_kill_loop(tmp_path: Path) -> None:
    def boom(kind: str, data: dict) -> None:
        raise RuntimeError("observer exploded")

    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1,
                 on_event=boom)
    outcome = loop.run()  # observer が例外でも loop は完走 (fail-safe)
    assert outcome.stop_reason == "max_sessions"


# ─── CLI ─────────────────────────────────────────────────────────


def test_cli_refuses_unbounded_run(tmp_path: Path) -> None:
    # --dry-run でも --max-sessions/--max-cost でもない = 課金保護で拒否
    assert main(["--workdir", str(tmp_path)]) == 2


def test_cli_missing_workdir() -> None:
    assert main(["--workdir", "Z:/no/such/dir/llterm-xyz", "--max-sessions", "1"]) == 2


def test_cli_dry_run_wires_end_to_end(tmp_path: Path) -> None:
    rc = main(["--workdir", str(tmp_path), "--dry-run", "--max-sessions", "1"])
    assert rc == 0
    assert (tmp_path / ".llterm" / "loop_ledger.jsonl").exists()


def test_next_prompt_injection_overrides_continue(tmp_path: Path) -> None:
    injected = ["割り込みタスク X"]

    def nxt() -> str | None:
        return injected.pop(0) if injected else None

    runner = FakeRunner()  # 常に閾値未満
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70,
                 max_sessions=1, max_turns_per_session=3, next_prompt=nxt)
    loop.run()
    work = [c for c in runner.calls if c[0] != DEFAULT_EXIT_PREP_PROMPT]
    assert DEFAULT_RESUME_PROMPT in work[0][0]        # 1 回目=新セッションの再開 prompt(継続preamble付)
    assert work[1][0].startswith("割り込みタスク X")  # 2 回目=注入タスクが優先 (末尾に指令が付く)
    assert not work[2][0].startswith("割り込みタスク X")  # 注入は一度だけ (以降は continue)


# ─── サブスク認証 (API キー env を外す) ───────────────────────────


def test_subscription_env_strips_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude.ai サブスク認証を使わせるため API キー系 env を外す (従量課金回避)。"""
    from llterm.host.loop import _subscription_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-be-stripped")
    monkeypatch.setenv("LLTERM_KEEP_ME", "yes")
    env = _subscription_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("LLTERM_KEEP_ME") == "yes"  # 他の env は保持される


def test_claude_runner_defaults_to_subscription() -> None:
    from llterm.host.loop import ClaudeRunner

    assert ClaudeRunner().use_subscription is True


# ─── effort (--effort フラグ) ─────────────────────────────────────


def test_build_args_appends_effort_when_set() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner(effort="max")._build_args(prompt="p", session_id="s", resume=False)
    assert "--effort" in args
    assert args[args.index("--effort") + 1] == "max"


def test_build_args_omits_effort_by_default() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner()._build_args(prompt="p", session_id="s", resume=False)
    assert "--effort" not in args  # 既定 (空) は claude 既定に委ねる


def test_build_args_ignores_invalid_effort() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner(effort="ultracode")._build_args(prompt="p", session_id="s", resume=False)
    assert "--effort" not in args  # vanilla claude に無い値 (ultracode 等) は付けない


# ─── model (--model フラグ) ───────────────────────────────────────


def test_build_args_appends_model_alias_when_set() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner(model="opus")._build_args(prompt="p", session_id="s", resume=False)
    assert "--model" in args
    assert args[args.index("--model") + 1] == "opus"


def test_build_args_appends_full_model_id() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner(model="claude-opus-4-8")._build_args(prompt="p", session_id="s", resume=False)
    assert args[args.index("--model") + 1] == "claude-opus-4-8"


def test_build_args_omits_model_by_default() -> None:
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner()._build_args(prompt="p", session_id="s", resume=False)
    assert "--model" not in args  # 既定 (空) は claude 保存既定に委ねる


def test_build_args_passes_unknown_model_through() -> None:
    """候補表に無いモデルも素通しする (世代更新でコードを直さなくても使える契約)。"""
    from llterm.host.loop import ClaudeRunner

    args = ClaudeRunner(model="claude-future-9")._build_args(prompt="p", session_id="s", resume=False)
    assert args[args.index("--model") + 1] == "claude-future-9"


def test_build_args_coerces_non_uuid_session_id() -> None:
    """claude --session-id は (a) 厳格な UUID を要求し、(b) 既存 id の再利用も
    'Session ID ... is already in use' で拒否する。OrchestraRunner の派生 id
    ('<uuid>-review0' 等) は UUID 不正かつ同一セッション内で毎回同じなので、レビュー系
    (resume=False) は毎回フレッシュな有効 UUID へ写像して衝突を防ぐ。主ループの有効 UUID は
    そのまま (作成 --session-id → 継続 --resume が整合)。
    """
    import uuid as _uuid

    from llterm.host.loop import ClaudeRunner

    runner = ClaudeRunner()
    base = str(_uuid.uuid4())

    # 有効 UUID はそのまま渡す (主ループの sid; 作成→継続が整合)
    args = runner._build_args(prompt="p", session_id=base, resume=False)
    assert args[args.index("--session-id") + 1] == base
    args_r = runner._build_args(prompt="p", session_id=base, resume=True)
    assert args_r[args_r.index("--resume") + 1] == base

    # 派生 id (非 UUID, resume=False) は毎回フレッシュな有効 UUID へ写像し、再利用衝突を回避
    derived = f"{base}-review0"
    a1 = runner._build_args(prompt="p", session_id=derived, resume=False)
    a2 = runner._build_args(prompt="p", session_id=derived, resume=False)
    sid1 = a1[a1.index("--session-id") + 1]
    sid2 = a2[a2.index("--session-id") + 1]
    _uuid.UUID(sid1); _uuid.UUID(sid2)  # いずれも有効 UUID (不正なら ValueError)
    assert sid1 != derived and sid2 != derived
    assert sid1 != sid2  # 毎回ユニーク = 同一セッション内の --session-id 再利用衝突を防ぐ


# ─── 指令 (安全弁 / autonomy / 監督モード) ───────────────────────


def test_apply_directives_autonomy_on(tmp_path: Path) -> None:
    """autonomy ON: 安全弁(常時) + 自律指令が付き、監督指令は付かない。"""
    from llterm.host.loop import AUTONOMY_DIRECTIVE, SAFETY_DIRECTIVE, SUPERVISED_DIRECTIVE

    out = _loop(FakeRunner(), tmp_path, autonomy_fn=lambda: True)._apply_directives("BASE")
    assert "BASE" in out
    assert SAFETY_DIRECTIVE in out          # 不可逆操作の承認 = autonomy 不問で常時
    assert AUTONOMY_DIRECTIVE in out
    assert SUPERVISED_DIRECTIVE not in out


def test_apply_directives_autonomy_off(tmp_path: Path) -> None:
    """autonomy OFF (監督): 安全弁 + 監督指令が付き、自律指令は付かない。"""
    from llterm.host.loop import AUTONOMY_DIRECTIVE, SAFETY_DIRECTIVE, SUPERVISED_DIRECTIVE

    out = _loop(FakeRunner(), tmp_path, autonomy_fn=lambda: False)._apply_directives("BASE")
    assert SAFETY_DIRECTIVE in out
    assert SUPERVISED_DIRECTIVE in out
    assert AUTONOMY_DIRECTIVE not in out


def test_apply_directives_dynamic_toggle(tmp_path: Path) -> None:
    """autonomy_fn は毎回評価される (走行中トグルが次ターンから効く)。"""
    from llterm.host.loop import AUTONOMY_DIRECTIVE, SUPERVISED_DIRECTIVE

    state = {"auto": True}
    loop = _loop(FakeRunner(), tmp_path, autonomy_fn=lambda: state["auto"])
    assert AUTONOMY_DIRECTIVE in loop._apply_directives("X")
    state["auto"] = False  # 走行中にトグル OFF した相当
    assert SUPERVISED_DIRECTIVE in loop._apply_directives("X")


def test_apply_directives_static_autonomy_fallback(tmp_path: Path) -> None:
    """autonomy_fn 未指定なら静的 autonomy にフォールバックする (後方互換)。"""
    from llterm.host.loop import AUTONOMY_DIRECTIVE

    assert AUTONOMY_DIRECTIVE in _loop(FakeRunner(), tmp_path, autonomy=True)._apply_directives("X")
    assert AUTONOMY_DIRECTIVE not in _loop(FakeRunner(), tmp_path, autonomy=False)._apply_directives("X")


def test_opener_includes_safety_directive(tmp_path: Path) -> None:
    """実際の 1 ターン目 opener にも安全弁が乗る (毎ターン適用の回帰)。"""
    from llterm.host.loop import SAFETY_DIRECTIVE

    runner = FakeRunner([{"ctx": 150_000}])  # 1 ターンで rotate → max_sessions=1 到達
    _loop(runner, tmp_path, max_sessions=1, autonomy_fn=lambda: True).run()
    assert SAFETY_DIRECTIVE in runner.calls[0][0]


def test_default_model_is_opus_4_8() -> None:
    from llterm.host.loop import DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-opus-4-8"


# ─── RAD 研究接地ヒント ───────────────────────────────────────────


def test_rad_hint_augments_resume_not_exit_prep(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70,
                 max_sessions=1, rad_hint="RADHINT-XYZ")
    loop.run()
    assert "RADHINT-XYZ" in runner.calls[0][0]                    # 作業(resume)prompt に付く
    assert DEFAULT_RESUME_PROMPT in runner.calls[0][0]
    exit_calls = [c for c in runner.calls if c[0] == DEFAULT_EXIT_PREP_PROMPT]
    assert exit_calls and "RADHINT-XYZ" not in exit_calls[0][0]   # exit準備 には付けない


def test_rad_hint_augments_continue_turns(tmp_path: Path) -> None:
    runner = FakeRunner()  # 常に閾値未満
    loop = _loop(runner, tmp_path, max_sessions=1, max_turns_per_session=2, rad_hint="RADHINT-XYZ")
    loop.run()
    work = [c for c in runner.calls if c[0] != DEFAULT_EXIT_PREP_PROMPT]
    assert all("RADHINT-XYZ" in c[0] for c in work)              # 全作業 prompt に付く


def test_no_rad_hint_by_default(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1)
    loop.run()
    assert DEFAULT_RESUME_PROMPT in runner.calls[0][0]           # 未設定なら rad_hint 無し


# ─── 計算オフロード指令 ───────────────────────────────────────────


def test_offload_hint_augments_work_not_exit_prep(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70,
                 max_sessions=1, offload_hint="OFFLOAD-XYZ")
    loop.run()
    assert "OFFLOAD-XYZ" in runner.calls[0][0]                    # 作業 prompt に付く
    exit_calls = [c for c in runner.calls if c[0] == DEFAULT_EXIT_PREP_PROMPT]
    assert exit_calls and "OFFLOAD-XYZ" not in exit_calls[0][0]   # exit準備 には付けない


def test_rad_and_offload_hints_both_appended(tmp_path: Path) -> None:
    runner = FakeRunner()  # 常に閾値未満
    loop = _loop(runner, tmp_path, max_sessions=1, max_turns_per_session=2,
                 rad_hint="RADHINT-XYZ", offload_hint="OFFLOAD-XYZ")
    loop.run()
    work = [c for c in runner.calls if c[0] != DEFAULT_EXIT_PREP_PROMPT]
    assert all("RADHINT-XYZ" in c[0] and "OFFLOAD-XYZ" in c[0] for c in work)


def test_no_offload_hint_by_default(tmp_path: Path) -> None:
    runner = FakeRunner([{"ctx": 150_000}])
    loop = _loop(runner, tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=1)
    loop.run()
    assert "OFFLOAD" not in runner.calls[0][0]  # 未設定なら offload_hint 無し
