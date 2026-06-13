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
        ctx: int = 0, num_turns: int = 1, session_id: str = "s") -> TurnResult:
    return TurnResult(session_id, 0, 0, ctx, cost, text, is_error, error_kind, num_turns, 0)


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
