# SPDX-License-Identifier: Apache-2.0
"""OrchestraRunner (指揮者×レビュー奏者の分業) の回帰テスト。

偽の TurnRunner を注入して、実装→レビュー→修正の分業フローと cost 集計を検証する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llterm.host.loop import TurnResult
from llterm.host.orchestra_runner import OrchestraRunner, runner_label


def _tr(text: str = "", *, cost: float = 0.0, is_error: bool = False, error_kind: str = "",
        ctx: int = 0, num_turns: int = 1, session_id: str = "s",
        inp: int = 0, out: int = 0, cached: int = 0, reasoning: int = 0,
        token_usage_kind: str = "instant", context_observable: bool = True,
        provider_version: str = "") -> TurnResult:
    return TurnResult(session_id, inp, out, ctx, cost, text, is_error, error_kind, num_turns, 0,
                      context_observable=context_observable,
                      cached_input_tokens=cached, reasoning_output_tokens=reasoning,
                      token_usage_kind=token_usage_kind, provider_version=provider_version)


@dataclass
class FakeRunner:
    """scripted な結果を順に返し、呼び出しを記録する偽 TurnRunner。"""

    results: list[TurnResult]
    calls: list[dict] = field(default_factory=list)
    on_stream: object = None
    cancelled: bool = False

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        self.calls.append({"prompt": prompt, "session_id": session_id, "resume": resume})
        return self.results.pop(0) if self.results else _tr(session_id=session_id)

    def cancel(self) -> None:
        self.cancelled = True


def _orch(conductor_results, reviewer_results, **kw) -> tuple[OrchestraRunner, FakeRunner, FakeRunner]:
    c = FakeRunner(list(conductor_results))
    r = FakeRunner(list(reviewer_results))
    kw.setdefault("include_diff", False)  # 既定で git を呼ばない (テスト純度)
    return OrchestraRunner(conductor=c, reviewer=r, **kw), c, r


# ─── 基本の分業フロー ─────────────────────────────────────────────


def test_full_flow_implement_review_fix(tmp_path: Path) -> None:
    """実装 → レビュー(指摘あり) → 修正 の 3 サブターンが回り cost が合算される。"""
    orch, c, r = _orch(
        conductor_results=[_tr("実装した", cost=1.0), _tr("修正した", cost=0.5, ctx=120)],
        reviewer_results=[_tr("- バグX を直せ", cost=0.0)],
    )
    res = orch.run_turn(prompt="やれ", session_id="sid", resume=False, cwd=tmp_path)
    assert len(c.calls) == 2          # 指揮者: 実装 + 修正
    assert len(r.calls) == 1          # レビュー奏者: 1 回
    # レビュー奏者は指揮者の報告を受け取る
    assert "実装した" in r.calls[0]["prompt"]
    # 修正ターンはレビュー指摘を受け取り、resume=True で継続
    assert "バグX" in c.calls[1]["prompt"]
    assert c.calls[1]["resume"] is True
    assert res.cost_usd == 1.5        # 1.0 + 0.0 + 0.5
    assert res.text == "修正した"      # 最終 = 修正後
    assert res.context_tokens == 120


def test_lgtm_skips_fix(tmp_path: Path) -> None:
    """レビューが LGTM なら指揮者の修正ターンを回さない。"""
    orch, c, r = _orch([_tr("実装", cost=1.0)], [_tr("LGTM", cost=0.0)])
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(c.calls) == 1
    assert res.text == "実装"
    assert res.cost_usd == 1.0


def test_empty_review_skips_fix(tmp_path: Path) -> None:
    orch, c, r = _orch([_tr("実装")], [_tr("   ")])
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(c.calls) == 1  # 空レビュー = 修正なし


def test_apply_review_false_no_fix(tmp_path: Path) -> None:
    orch, c, r = _orch([_tr("実装")], [_tr("- 直せ")], apply_review=False)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(c.calls) == 1   # apply_review=False = レビューは表示のみ
    assert len(r.calls) == 1   # レビュー自体は走る


# ─── エラー / fail-safe ───────────────────────────────────────────


def test_conductor_error_skips_review(tmp_path: Path) -> None:
    """指揮者が失敗したらレビューせず即返す。"""
    orch, c, r = _orch([_tr("", is_error=True, error_kind="rate_limited", cost=0.2)], [_tr("x")])
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.error_kind == "rate_limited"
    assert len(r.calls) == 0  # レビュー奏者は呼ばれない


def test_reviewer_error_returns_conductor_result(tmp_path: Path) -> None:
    """レビュー奏者が失敗してもレビューは best-effort、指揮者の結果を返す (修正なし)。"""
    orch, c, r = _orch([_tr("実装", cost=1.0)],
                       [_tr("", is_error=True, error_kind="rate_limited")])
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is False
    assert res.text == "実装"
    assert len(c.calls) == 1  # レビュー失敗 → 修正ターンなし


def test_rate_limited_reviewer_is_benched_for_next_turn(tmp_path: Path) -> None:
    """limit/auth 系で落ちた補助役は次ターン以降 bench され、毎回叩かない。"""
    orch, c, r = _orch(
        [_tr("実装1"), _tr("実装2")],
        [_tr("", is_error=True, error_kind="rate_limited"), _tr("LGTM")],
    )
    res1 = orch.run_turn(prompt="p1", session_id="s1", resume=False, cwd=tmp_path)
    res2 = orch.run_turn(prompt="p2", session_id="s2", resume=True, cwd=tmp_path)
    assert res1.text == "実装1" and res2.text == "実装2"
    assert len(r.calls) == 1  # 2 ターン目は bench 済みで再度呼ばれない


def test_aux_bench_event_emitted_with_conductor_context(tmp_path: Path) -> None:
    """補助役の bench 開始時は GUI 向け degraded-mode イベントを流す。"""
    seen: list[dict] = []
    orch, _, _ = _orch(
        [_tr("実装1")],
        [_tr("", is_error=True, error_kind="rate_limited")],
    )
    orch.on_stream = seen.append
    orch.run_turn(prompt="p1", session_id="s1", resume=False, cwd=tmp_path)
    bench = next(e for e in seen if e.get("phase") == "aux_benched")
    assert bench["runner"] == "FakeRunner"
    assert bench["conductor"] == "FakeRunner"
    assert bench["error_kind"] == "rate_limited"
    assert bench["cooldown_turns"] == 3


def test_rate_limited_reviewer_retries_after_cooldown(tmp_path: Path) -> None:
    """rate_limited reviewer は cooldown 後に half-open 再試行される。"""
    orch, c, r = _orch(
        [_tr("実装1"), _tr("実装2"), _tr("実装3"), _tr("実装4"), _tr("実装5")],
        [_tr("", is_error=True, error_kind="rate_limited"), _tr("LGTM")],
    )
    for i in range(4):
        orch.run_turn(prompt=f"p{i}", session_id=f"s{i}", resume=bool(i), cwd=tmp_path)
    assert len(r.calls) == 1  # cooldown 中は bench
    orch.run_turn(prompt="p4", session_id="s4", resume=True, cwd=tmp_path)
    assert len(r.calls) == 2  # cooldown 経過で再試行


# ─── git diff / stream / cancel ──────────────────────────────────


def test_diff_included_in_review_prompt(tmp_path: Path) -> None:
    class WithDiff(OrchestraRunner):
        def _capture_diff(self, cwd: Path) -> str:
            return "diff --git a/x b/x\n+added line"

    orch = WithDiff(conductor=FakeRunner([_tr("実装")]), reviewer=FakeRunner([_tr("LGTM")]),
                    include_diff=True)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert "added line" in orch.reviewer.calls[0]["prompt"]


def test_review_events_emitted(tmp_path: Path) -> None:
    seen: list[dict] = []
    orch, c, r = _orch([_tr("実装")], [_tr("- 指摘")])
    orch.on_stream = seen.append
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    review_events = [e for e in seen if e.get("kind") == "review"]
    phases = [e.get("phase") for e in review_events]
    assert "start" in phases and "end" in phases
    end = next(e for e in review_events if e.get("phase") == "end")
    assert end["text"] == "- 指摘"


def test_reviewer_session_id_and_resume(tmp_path: Path) -> None:
    """レビュー奏者は派生 session_id + resume=False (ステートレス批評)。"""
    orch, c, r = _orch([_tr("実装")], [_tr("LGTM")])
    orch.run_turn(prompt="p", session_id="abc", resume=True, cwd=tmp_path)
    assert r.calls[0]["session_id"] == "abc-review"
    assert r.calls[0]["resume"] is False
    assert c.calls[0]["resume"] is True  # 指揮者は呼び出し側の resume を尊重


def test_cancel_propagates(tmp_path: Path) -> None:
    orch, c, r = _orch([_tr("実装")], [_tr("LGTM")])
    orch.cancel()
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert c.cancelled is True and r.cancelled is True


def test_runner_label() -> None:
    class P:
        def provider_label(self):
            return "groq:llama"
    assert runner_label(P()) == "groq:llama"

    class CodexRunner:
        pass
    assert runner_label(CodexRunner()) == "codex"


# ─── v2: パネル (複数レビュー奏者) / 真偽確認 / 責任者集約 / sign-off ──


@dataclass
class LabeledFakeRunner(FakeRunner):
    """provider_label を返す偽 TurnRunner (independent 判定の検証用)。"""

    label: str = "fake"

    def provider_label(self) -> str:
        return self.label


def test_multiple_reviewers_run_independently(tmp_path: Path) -> None:
    """(a) パネル各 reviewer が独立に走り session_id が -review0/-review1 になる。"""
    c = FakeRunner([_tr("実装")])
    r0 = FakeRunner([_tr("LGTM")])
    r1 = FakeRunner([_tr("LGTM")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], lead=None, include_diff=False)
    orch.run_turn(prompt="p", session_id="sid", resume=False, cwd=tmp_path)
    assert r0.calls[0]["session_id"] == "sid-review0"
    assert r1.calls[0]["session_id"] == "sid-review1"
    assert r0.calls[0]["resume"] is False and r1.calls[0]["resume"] is False


def test_lead_aggregates_panel_findings(tmp_path: Path) -> None:
    """(b) 所見 2 件以上で lead 集約が呼ばれ、全パネル text を受け取る。(c) 集約指示で修正。"""
    c = FakeRunner([_tr("実装"), _tr("修正した")])
    r0 = FakeRunner([_tr("- bug A を直せ")])
    r1 = FakeRunner([_tr("- bug B を直せ")])
    lead = FakeRunner([_tr("1. bug A を直す\n2. bug B を直す"), _tr("APPROVED")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], lead=lead, include_diff=False)
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    # lead は集約 (calls[0]) + 修正後 sign-off (calls[1]) の 2 回
    assert lead.calls[0]["session_id"] == "s-aggregate"
    agg_prompt = lead.calls[0]["prompt"]
    assert "bug A" in agg_prompt and "bug B" in agg_prompt  # 全パネル所見が集約に渡る
    # (c) 修正ターンは lead の集約指示を受け取り resume=True
    assert c.calls[1]["resume"] is True
    assert "bug A を直す" in c.calls[1]["prompt"] and "bug B を直す" in c.calls[1]["prompt"]
    assert res.text == "修正した"


def test_rate_limited_lead_falls_back_to_panel_without_retry_storm(tmp_path: Path) -> None:
    """lead が rate_limited なら単一/複数 panel のフォールバックで進み、次ターン以降 bench される。"""
    c = FakeRunner([_tr("実装1"), _tr("修正1"), _tr("実装2"), _tr("修正2")])
    r0 = FakeRunner([_tr("- bug A"), _tr("- bug B")])
    r1 = FakeRunner([_tr("- bug C"), _tr("- bug D")])
    lead = FakeRunner([_tr("", is_error=True, error_kind="rate_limited"), _tr("統合指示")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], lead=lead, include_diff=False)
    res1 = orch.run_turn(prompt="p1", session_id="s1", resume=False, cwd=tmp_path)
    res2 = orch.run_turn(prompt="p2", session_id="s2", resume=True, cwd=tmp_path)
    assert "bug A" in c.calls[1]["prompt"] and "bug C" in c.calls[1]["prompt"]  # panel フォールバック
    assert res1.text == "修正1" and res2.text == "修正2"
    assert len(lead.calls) == 1  # 2 ターン目は bench 済みで再集約しない


def test_auth_lead_retries_after_single_turn_cooldown(tmp_path: Path) -> None:
    """auth/unavailable は永久 bench せず、短い cooldown 後に再試行される。"""
    c = FakeRunner([_tr("実装1"), _tr("修正1"), _tr("実装2"), _tr("修正2"), _tr("実装3"), _tr("修正3")])
    r0 = FakeRunner([_tr("- bug A"), _tr("- bug B"), _tr("- bug C")])
    r1 = FakeRunner([_tr("- bug D"), _tr("- bug E"), _tr("- bug F")])
    lead = FakeRunner([_tr("", is_error=True, error_kind="auth"), _tr("統合指示"), _tr("統合指示")])
    orch = OrchestraRunner(
        conductor=c, reviewers=[r0, r1], lead=lead, include_diff=False, final_signoff=False
    )
    orch.run_turn(prompt="p1", session_id="s1", resume=False, cwd=tmp_path)
    orch.run_turn(prompt="p2", session_id="s2", resume=True, cwd=tmp_path)
    assert len(lead.calls) == 1  # 1 ターン cooldown
    orch.run_turn(prompt="p3", session_id="s3", resume=True, cwd=tmp_path)
    assert len(lead.calls) == 2  # 復帰後は aggregate を再試行
    assert [call["session_id"] for call in lead.calls] == ["s1-aggregate", "s3-aggregate"]


def test_interrupt_from_conductor_propagates_and_skips_review(tmp_path: Path) -> None:
    """指揮者が interrupted を返すと orchestra も interrupted を返し、レビューに進まない。"""
    orch, c, r = _orch(
        conductor_results=[_tr("", is_error=True, error_kind="interrupted")],
        reviewer_results=[_tr("- 指摘")],
    )
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "interrupted"
    assert len(r.calls) == 0  # レビュー奏者は呼ばれない (指揮者中断で即返す)


def test_orchestra_interrupt_delegates_to_members(tmp_path: Path) -> None:
    """orchestra.interrupt() は flag を立て、全メンバの interrupt() を呼ぶ。"""

    @dataclass
    class IRunner(FakeRunner):
        interrupts: int = 0

        def interrupt(self) -> None:
            self.interrupts += 1

    c = IRunner([])
    r = IRunner([])
    orch = OrchestraRunner(conductor=c, reviewers=[r], include_diff=False)
    orch.interrupt()
    assert orch._interrupted is True
    assert c.interrupts == 1 and r.interrupts == 1


def test_run_turn_unreviewed_runs_only_conductor(tmp_path: Path) -> None:
    """run_turn_unreviewed は指揮者だけを回し、レビュー/集約/sign-off を一切しない。"""
    orch, c, r = _orch(conductor_results=[_tr("記録した", cost=1.0, ctx=42)],
                       reviewer_results=[_tr("- 指摘")])
    res = orch.run_turn_unreviewed(prompt="記録せよ", session_id="s", resume=True, cwd=tmp_path)
    assert len(c.calls) == 1          # 指揮者のみ
    assert len(r.calls) == 0          # レビュー奏者は呼ばれない
    assert res.text == "記録した" and res.cost_usd == 1.0 and res.context_tokens == 42


def test_final_result_preserves_cached_tokens_and_usage_kind(tmp_path: Path) -> None:
    c = FakeRunner([_tr("実装", inp=5_000_000, out=1000, cached=98_000, reasoning=200,
                        token_usage_kind="cumulative", context_observable=False,
                        provider_version="codex-cli 0.test")])
    orch = OrchestraRunner(conductor=c, reviewers=[], lead=None, include_diff=False)
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.input_tokens == 5_000_000
    assert res.output_tokens == 1000
    assert res.cached_input_tokens == 98_000
    assert res.reasoning_output_tokens == 200
    assert res.token_usage_kind == "cumulative"
    assert res.context_observable is False
    assert res.provider_version == "codex-cli 0.test"


def test_interrupted_result_preserves_cached_tokens_and_usage_kind(tmp_path: Path) -> None:
    class InterruptingReviewer(FakeRunner):
        orch: OrchestraRunner | None = None

        def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
            assert self.orch is not None
            self.orch.interrupt()
            return super().run_turn(prompt=prompt, session_id=session_id, resume=resume, cwd=cwd)

    c = FakeRunner([_tr("実装", inp=5_000_000, out=1000, cached=98_000, reasoning=200,
                        token_usage_kind="cumulative", context_observable=False,
                        provider_version="codex-cli 0.test")])
    r = InterruptingReviewer([_tr("LGTM")])
    orch = OrchestraRunner(conductor=c, reviewer=r, include_diff=False)
    r.orch = orch
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.is_error is True
    assert res.error_kind == "interrupted"
    assert res.cached_input_tokens == 98_000
    assert res.reasoning_output_tokens == 200
    assert res.token_usage_kind == "cumulative"
    assert res.context_observable is False
    assert res.provider_version == "codex-cli 0.test"


def test_final_signoff_called_after_fix(tmp_path: Path) -> None:
    """(d) 修正後に責任者が新 diff を 1 回だけ再レビューする (sign-off)。"""
    c = FakeRunner([_tr("実装"), _tr("修正した")])
    r0 = FakeRunner([_tr("- A")])
    r1 = FakeRunner([_tr("- B")])
    lead = FakeRunner([_tr("統合指示"), _tr("APPROVED")])  # 1=集約 / 2=sign-off
    seen: list[dict] = []
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], lead=lead,
                           include_diff=False, on_stream=seen.append)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(lead.calls) == 2                       # 集約 + sign-off の 2 回 (再修正ループなし)
    assert lead.calls[1]["session_id"] == "s-signoff"
    assert lead.calls[1]["resume"] is False
    signoff = next(e for e in seen if e.get("phase") == "signoff")
    assert signoff["approved"] is True
    assert signoff["lead"] == "fake" or isinstance(signoff["lead"], str)


def test_signoff_skipped_without_fix(tmp_path: Path) -> None:
    """sign-off は修正が行われたときだけ (LGTM で修正なし → sign-off も呼ばない)。"""
    c = FakeRunner([_tr("実装")])
    r0 = FakeRunner([_tr("LGTM")])
    r1 = FakeRunner([_tr("LGTM")])
    lead = FakeRunner([_tr("LGTM")])  # 集約 → LGTM → 修正なし → sign-off なし
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], lead=lead, include_diff=False)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(c.calls) == 1            # 修正ターンなし
    assert len(lead.calls) == 1         # 集約のみ (sign-off は呼ばれない)


def test_factchecker_called_and_emitted(tmp_path: Path) -> None:
    """(e) 真偽確認奏者が呼ばれ text が emit される。"""
    c = FakeRunner([_tr("実装"), _tr("修正")])
    r0 = FakeRunner([_tr("- 直せ")])
    fc = FakeRunner([_tr("事実Xは誤り")])
    lead = FakeRunner([_tr("統合指示"), _tr("APPROVED")])
    seen: list[dict] = []
    orch = OrchestraRunner(conductor=c, reviewers=[r0], factchecker=fc, lead=lead,
                           include_diff=False, on_stream=seen.append)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(fc.calls) == 1
    assert fc.calls[0]["session_id"] == "s-factcheck"
    fc_event = next(e for e in seen if e.get("phase") == "factcheck")
    assert fc_event["text"] == "事実Xは誤り"
    # factcheck 所見があるので所見 1 件でも lead 集約が走り、その所見を受け取る
    assert "事実Xは誤り" in lead.calls[0]["prompt"]


def test_independent_flag_same_provider_is_double_check(tmp_path: Path) -> None:
    """(f) independent フラグ: 指揮者とレビュアーが同一プロバイダ → False (ダブルチェック)。"""
    c = LabeledFakeRunner([_tr("実装")], label="claude")
    r_same = LabeledFakeRunner([_tr("LGTM")], label="claude")    # 同系
    r_diff = LabeledFakeRunner([_tr("LGTM")], label="codex")     # 別系統
    seen: list[dict] = []
    orch = OrchestraRunner(conductor=c, reviewers=[r_same, r_diff], lead=None,
                           include_diff=False, on_stream=seen.append)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    starts = [e for e in seen if e.get("phase") == "start"]
    by_reviewer = {e["index"]: e["independent"] for e in starts}
    assert by_reviewer[0] is False   # claude conductor × claude reviewer = ダブルチェック
    assert by_reviewer[1] is True    # claude conductor × codex reviewer = 独立


def test_cancel_propagates_to_all_roles(tmp_path: Path) -> None:
    """(g) cancel が指揮者・全レビュー奏者・真偽確認・責任者へ伝播する。"""
    c = FakeRunner([_tr("実装")])
    r0 = FakeRunner([_tr("LGTM")])
    r1 = FakeRunner([_tr("LGTM")])
    fc = FakeRunner([_tr("ok")])
    lead = FakeRunner([_tr("LGTM")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0, r1], factchecker=fc, lead=lead,
                           include_diff=False)
    orch.cancel()
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert all(x.cancelled for x in (c, r0, r1, fc, lead))


def test_single_reviewer_no_lead_skips_aggregate(tmp_path: Path) -> None:
    """効率: 単一所見 + lead 無し は集約呼び出しを省略し、その所見を指示にする。"""
    c = FakeRunner([_tr("実装"), _tr("修正")])
    r0 = FakeRunner([_tr("- 直せ")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0], lead=None, include_diff=False)
    orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert "直せ" in c.calls[1]["prompt"]   # 単一レビューが総合判断 = そのまま修正指示


def test_reviewers_list_uses_indexed_session_ids(tmp_path: Path) -> None:
    """reviewers リスト経由 (新 API) は単一でも -review0 (旧 reviewer= は -review 無印)。"""
    c = FakeRunner([_tr("実装")])
    r0 = FakeRunner([_tr("LGTM")])
    orch = OrchestraRunner(conductor=c, reviewers=[r0], lead=None, include_diff=False)
    orch.run_turn(prompt="p", session_id="abc", resume=False, cwd=tmp_path)
    assert r0.calls[0]["session_id"] == "abc-review0"


# ─── deep-dive 監査で見つけたロバスト性の回帰 ─────────────────────


def test_fix_turn_other_error_keeps_successful_implementation(tmp_path: Path) -> None:
    """任意の review-fix ターンが 'other' 失敗 (timeout 等) でも成功済み実装を error 化しない。

    回帰: final=fix 無条件で、fix の other 失敗が全体を is_error 化 → loop が consec_err を積み
    3 連続で circuit_open。実装は毎回成功しているのに自走が止まっていた。
    """
    orch, c, r = _orch(
        conductor_results=[_tr("実装した", cost=1.0),
                           _tr("", cost=0.5, is_error=True, error_kind="other")],
        reviewer_results=[_tr("- バグを直せ", cost=0.0)],
    )
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert len(c.calls) == 2          # 実装 + fix (fix ターンは走る)
    assert res.is_error is False      # ★ 全体は成功のまま
    assert res.error_kind == ""
    assert res.text == "実装した"      # 実装結果を保持
    assert res.cost_usd == 1.5        # cost は合算 (1.0 + 0.5)


def test_fix_turn_rate_limited_still_propagates(tmp_path: Path) -> None:
    """fix ターンの rate_limited は伝播する (other 以外は loop が正しく扱う)。"""
    orch, c, r = _orch(
        conductor_results=[_tr("実装", cost=1.0),
                           _tr("", is_error=True, error_kind="rate_limited")],
        reviewer_results=[_tr("- 直せ")],
    )
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "rate_limited"


def test_cancel_during_review_returns_cancelled_not_success(tmp_path: Path) -> None:
    """Stop がレビュー中に来たら指揮者の成功でなく cancelled を返す (loop が即停止できる)。"""
    class CancellingReviewer(FakeRunner):
        orch: OrchestraRunner | None = None

        def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
            assert self.orch is not None
            self.orch.cancel()  # レビュー中に Stop
            return super().run_turn(prompt=prompt, session_id=session_id, resume=resume, cwd=cwd)

    c = FakeRunner([_tr("実装", cost=1.0)])
    r = CancellingReviewer([_tr("- 直せ")])
    orch = OrchestraRunner(conductor=c, reviewer=r, include_diff=False)
    r.orch = orch
    res = orch.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"  # ★ 成功でなく cancelled
    assert len(c.calls) == 1              # 集約/修正せず即返す (fix ターン無し)


def test_capture_diff_uses_no_window_creationflag(tmp_path: Path, monkeypatch) -> None:
    """_capture_diff は console 明滅防止の creationflags=_NO_WINDOW で git を呼ぶ (Windows)。"""
    import llterm.host.orchestra_runner as om

    captured: dict = {}

    class _P:
        stdout = ""

    def _fake_run(*_a: object, **k: object) -> object:
        captured.update(k)
        return _P()

    monkeypatch.setattr(om.subprocess, "run", _fake_run)
    orch = OrchestraRunner(conductor=FakeRunner([]), reviewer=FakeRunner([]), include_diff=True)
    orch._capture_diff(tmp_path)
    assert captured.get("creationflags") == om._NO_WINDOW
