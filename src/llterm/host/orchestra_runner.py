# SPDX-License-Identifier: Apache-2.0
"""OrchestraRunner — 指揮者 × レビュー奏者パネル × 真偽確認 × 責任者の分業を 1 ターンに束ねる。

オーケストラ体制の中核。フェイルオーバー (1 人ずつ交代) とは別物で、**同じ 1 ターン内で
複数 AI が分業**する (ユーザー確定の 4 役モデル):

    1. **指揮者 (conductor / 実装者)** が実装する (ファイル編集など)。1 AI。Codex/Claude/Gemini 等。
       Perplexity は実装者にしない (プログラミング苦手) — これは GUI 側で担保する。
    2. ラッパが変更を ``git diff`` で捕捉する。
    3. **レビュー奏者パネル (reviewers)** が **複数 AI** でそれぞれ独立に批評する (バグ/抜け/リスク)。
    4. (任意) **調査・真偽確認奏者 (factchecker)** = Perplexity 等が実装報告/diff の事実主張を裏取りする。
    5. **責任者/総合判断 (lead)** = Claude Code が、パネル各レビュー + factcheck を取りまとめ
       (集約) し、重複排除した優先度付き修正指示 + 総合判断を出す。
    6. (任意) 指揮者が統合指示を受けて修正ターンを回す (apply_review)。
    7. **最終 sign-off (final_signoff)**: 責任者が新 diff を 1 回だけ再レビューし、ループを閉じる。

``TurnRunner`` プロトコルに準拠するので、provider chain にそのまま主奏者として差せる
(SessionLoop は無改造)。レビュー/真偽確認/集約を無料枠 or web 系 AI に振れば、品質ゲートを
足しつつ Claude の token を節約できる。

設計方針 (ユーザー確定):
- **実装者とレビュアー/責任者が同一プロバイダでも禁止しない** ("ダブルチェック" として許容)。
  ハードブロック/自動置換はせず、**独立 (別系統) / ダブルチェック (同系) をラベル表示するだけ**。
- **後方互換**: 旧 `reviewer: TurnRunner` (単一) 引数も受ける (``__post_init__`` で
  ``reviewers=[reviewer]`` に正規化)。既存テストの ``orch.reviewer`` 参照も互換 property で残す。

fail-safe: レビュー/真偽確認/集約/sign-off が失敗 (レート制限等) しても指揮者の結果は返す
(レビュー系は best-effort)。指揮者が失敗したらレビューせず即返す。cancel は全役へ伝播。
"""
from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from llterm.host.loop import _NO_WINDOW, TurnResult, TurnRunner

_DIFF_MAX_CHARS = 4000   # レビューに添える git diff の上限 (プロンプト肥大を防ぐ)
_REVIEW_MAX_CHARS = 4000  # 指揮者へ渡すレビュー/集約本文の上限
_PANEL_MAX_CHARS = 2000  # 集約プロンプトに載せる 1 レビューあたりの上限
_AUX_COOLDOWN_TURNS = {
    "rate_limited": 3,
    "auth": 1,
    "unavailable": 1,
}


def runner_label(runner: object) -> str:
    """奏者の表示名 (claude / codex / gemini / <provider:model> / クラス名)。"""
    label = getattr(runner, "provider_label", None)
    if callable(label):
        try:
            return str(label())
        except Exception:  # noqa: BLE001
            pass
    return {
        "ClaudeRunner": "claude", "CodexRunner": "codex", "GeminiRunner": "gemini",
        "OpenAICompatRunner": "openai-compat", "VirtualClaudeRunner": "virtual",
    }.get(type(runner).__name__, type(runner).__name__)


@dataclass
class OrchestraRunner:
    """指揮者 × レビュー奏者パネル × 真偽確認 × 責任者の分業を 1 ターンに束ねる。"""

    conductor: TurnRunner
    # パネル (0+)。旧 `reviewer` (単一) も後方互換で受ける → __post_init__ で reviewers に正規化。
    reviewers: list[TurnRunner] = field(default_factory=list)
    reviewer: TurnRunner | None = None  # 後方互換用 (旧 API)。__post_init__ で reviewers へ畳む
    factchecker: TurnRunner | None = None  # 調査・真偽確認奏者 (Perplexity 等)。任意・単一
    lead: TurnRunner | None = None  # 責任者 (Claude)。パネル + factcheck を集約・総合判断
    apply_review: bool = True   # True: 指揮者が統合指示を受けて修正ターンを回す
    final_signoff: bool = True  # True: 修正後に責任者が新 diff を 1 回だけ再レビューして閉じる
    include_diff: bool = True   # レビューに git diff を添える
    on_stream: Callable[[dict], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)
    _interrupted: bool = field(default=False, repr=False, compare=False)  # 緊急注入の一発中断
    # key は runner object の寿命内で安定な `id(runner)`。補助奏者は OrchestraRunner が強参照し
    # 実行中に GC されない前提なので、現設計では role 名より安価に bench 状態を持てる。
    _disabled_aux_until: dict[int, int] = field(default_factory=dict, repr=False, compare=False)
    _turn_seq: int = field(default=0, repr=False, compare=False)
    # 旧 API (`reviewer=` 単一・`reviewers` 未指定) で構築されたか。True のとき派生 session_id を
    # `-review` (無印) にして既存テスト/呼び出しと後方互換を保つ (複数パネルは `-review{i}`)。
    _legacy_single: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # 後方互換: 旧 `reviewer=` (単一) を受けたら reviewers パネルへ畳む。`reviewers` を明示
        # 指定していない単一 reviewer 構築は legacy single 扱いにし、session_id を無印にする。
        if self.reviewer is not None:
            self._legacy_single = not self.reviewers  # reviewers 未指定 = 旧 API 経路
            if self.reviewer not in self.reviewers:
                self.reviewers = [self.reviewer, *self.reviewers]

    # ─── prompts (各役への内部指示文) ───────────────────────────────
    def _review_prompt(self, work: str, diff: str) -> str:
        parts = [
            "あなたはコードレビュー奏者。別の AI (指揮者) が下記の作業を行った。",
            "バグ・抜け漏れ・リスク・規約違反を簡潔に箇条書きで指摘せよ。",
            "問題が無ければ 'LGTM' とだけ書け。実装はせず指摘のみ行う。",
            "",
            "## 指揮者の報告",
            work.strip() or "(報告テキストなし)",
        ]
        if diff.strip():
            parts += ["", "## 変更 (git diff)", "```diff", diff.strip(), "```"]
        return "\n".join(parts)

    def _factcheck_prompt(self, work: str, diff: str) -> str:
        parts = [
            "あなたは調査・真偽確認奏者 (web で裏取りできる)。別の AI (指揮者) が下記の作業を行った。",
            "実装報告や変更に含まれる**事実主張・根拠・API/ライブラリの挙動・前提**を、可能なら一次"
            "情報で真偽確認せよ。誤り・古い情報・要出典の箇所を簡潔に箇条書きで指摘し、確かな点は"
            "その旨を述べよ。実装・コード修正はせず、事実の裏取りのみ行う。",
            "",
            "## 指揮者の報告",
            work.strip() or "(報告テキストなし)",
        ]
        if diff.strip():
            parts += ["", "## 変更 (git diff)", "```diff", diff.strip(), "```"]
        return "\n".join(parts)

    def _aggregate_prompt(self, work: str, diff: str,
                          panel: list[tuple[str, str]], factcheck: str) -> str:
        parts = [
            "あなたは責任者 (総合判断)。指揮者の実装に対し、複数のレビュー奏者と真偽確認奏者から"
            "所見が集まった。これらを**取りまとめ**よ:",
            "1. 重複する指摘は 1 つに統合する。",
            "2. 重要度の高い順に並べた『修正指示リスト』を作る (指揮者がそのまま反映できる粒度で)。",
            "3. 反映すべき actionable な指摘が無ければ 'LGTM' とだけ書く。",
            "実装はせず、統合した指示と総合判断のみを出力する。",
            "",
            "## 指揮者の報告",
            work.strip() or "(報告テキストなし)",
        ]
        if diff.strip():
            parts += ["", "## 変更 (git diff)", "```diff", diff.strip()[:_DIFF_MAX_CHARS], "```"]
        if panel:
            parts += ["", "## レビュー奏者パネルの所見"]
            for label, text in panel:
                parts += [f"### {label}", text.strip()[:_PANEL_MAX_CHARS] or "(空)"]
        if factcheck.strip():
            parts += ["", "## 真偽確認奏者の所見", factcheck.strip()[:_PANEL_MAX_CHARS]]
        return "\n".join(parts)

    def _signoff_prompt(self, diff: str) -> str:
        parts = [
            "あなたは責任者。指揮者が修正を反映した。修正後の変更を確認し、問題が解消されたか"
            "判断せよ。問題が無ければ最初の行に 'APPROVED' と書き、残課題があれば 'CHANGES' と"
            "書いて簡潔に列挙せよ。実装はしない (これは最終確認であり、再修正ループはしない)。",
        ]
        if diff.strip():
            parts += ["", "## 修正後の変更 (git diff)", "```diff", diff.strip(), "```"]
        else:
            parts += ["", "(差分は捕捉できなかった。報告ベースで判断せよ。)"]
        return "\n".join(parts)

    def _fix_prompt(self, instructions: str) -> str:
        return (
            "責任者が取りまとめた次の統合指示を受けた。妥当な点を反映し、不要な指摘はその理由を"
            "一言添えて続行せよ。確認は求めない (自律継続)。\n\n## 統合修正指示\n"
            + instructions.strip()[:_REVIEW_MAX_CHARS]
        )

    # ─── git diff 捕捉 (テストは override 可) ────────────────────
    def _capture_diff(self, cwd: Path) -> str:
        """指揮者の編集を git diff で捕捉する。git でない/失敗時は空 (fail-safe)。"""
        try:
            proc = subprocess.run(
                ["git", "-C", str(cwd), "--no-pager", "diff"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=False,
                creationflags=_NO_WINDOW,  # pythonw GUI 親が console 子を出すと毎ターン窓が明滅する
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        out = proc.stdout or ""
        return out[:_DIFF_MAX_CHARS] + ("\n…(truncated)" if len(out) > _DIFF_MAX_CHARS else "")

    def _emit(self, item: dict) -> None:
        if self.on_stream is None:
            return
        try:
            self.on_stream(item)
        except Exception:  # noqa: BLE001
            pass

    def _review_stream(self, item: dict) -> None:
        """レビュー系の stream を review タグ付きで流す (GUI が区別表示できる)。"""
        self._emit({**item, "review": True})

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _aux_enabled(self, runner: TurnRunner | None) -> bool:
        if runner is None:
            return False
        until = self._disabled_aux_until.get(id(runner), 0)
        if until > self._turn_seq:
            return False
        self._disabled_aux_until.pop(id(runner), None)  # cooldown 経過で half-open 再試行
        return True

    def _maybe_disable_aux(self, runner: TurnRunner, result: TurnResult, *,
                           label: str, conductor_label: str) -> None:
        """rate_limit/auth/unavailable な補助役を cooldown 付きで一時 bench する。

        レビュー/真偽確認/責任者が Claude limit 等で毎ターン失敗すると、Codex 主運用でも
        無駄な補助ターンを延々叩き続けて代替運用の足を引っ張る。aux role は best-effort なので、
        構造的な失敗を返したら一旦 bench して conductor の進行を優先しつつ、cooldown 後に
        再試行して一時障害からの回復も拾う。
        """
        cooldown = _AUX_COOLDOWN_TURNS.get(result.error_kind, 0)
        if cooldown > 0:
            self._disabled_aux_until[id(runner)] = self._turn_seq + cooldown + 1
            self._emit({
                "kind": "review",
                "phase": "aux_benched",
                "runner": label,
                "conductor": conductor_label,
                "error_kind": result.error_kind,
                "cooldown_turns": cooldown,
            })

    # ─── 1 ターン = 実装 → パネル → 真偽確認 → 集約 → 修正 → sign-off ─────
    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        if self._is_cancelled():
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
        with self._lock:
            self._interrupted = False  # ターン開始時にリセット (走行中の interrupt() だけを拾う)
            self._turn_seq += 1

        # 1. 指揮者が実装。stream は指揮者のものをそのまま流す。
        # 緊急注入で指揮者が interrupt されると res.error_kind="interrupted" が返り、
        # 下の `if res.is_error: return res` でそのまま loop に伝播する (停止ではなく注入消費へ)。
        self.conductor.on_stream = self._emit  # type: ignore[attr-defined]
        res = self.conductor.run_turn(prompt=prompt, session_id=session_id, resume=resume, cwd=cwd)
        total_cost = res.cost_usd
        total_turns = res.num_turns
        if res.is_error:
            return res  # 指揮者が失敗 → レビューせず即返す (cancelled/auth/rate を loop に委ねる)
        if self._is_cancelled():
            return res

        conductor_label = runner_label(self.conductor)

        # 2. 変更を捕捉。
        diff = self._capture_diff(cwd) if self.include_diff else ""

        # 3. パネル各 reviewer が独立に批評 (best-effort / stateless)。
        panel: list[tuple[str, str]] = []  # (label, text) of 非空・非エラー
        for i, rev_runner in enumerate(self.reviewers):
            if self._is_cancelled():
                break
            if not self._aux_enabled(rev_runner):
                continue
            label = runner_label(rev_runner)
            independent = label != conductor_label
            self._emit({"kind": "review", "phase": "start", "reviewer": label,
                        "conductor": conductor_label, "independent": independent, "index": i})
            # 後方互換: 旧 API の単一 reviewer は `-review` (無印)。パネルは `-review{i}`。
            review_sid = f"{session_id}-review" if self._legacy_single else f"{session_id}-review{i}"
            rr = self._sub_review(
                rev_runner, self._review_prompt(res.text, diff), review_sid, cwd,
                label=label, conductor_label=conductor_label)
            total_cost += rr.cost_usd
            total_turns += rr.num_turns
            self._emit({"kind": "review", "phase": "end", "reviewer": label,
                        "conductor": conductor_label, "independent": independent, "index": i,
                        "text": rr.text, "is_error": rr.is_error})
            if not rr.is_error and rr.text.strip():
                panel.append((label, rr.text))

        # 緊急注入がレビュー中に来た場合: 残りの集約/修正/sign-off を行わず即 interrupted を返す
        # (loop はループを止めず注入を次ターンで消費する)。指揮者実装/修正フェーズ中の中断は
        # 各 run_turn が "interrupted" を返して伝播するため、ここはレビュー中の取りこぼし防止。
        with self._lock:
            interrupted = self._interrupted
        if interrupted:
            return TurnResult(res.session_id or session_id, res.input_tokens, res.output_tokens,
                              res.context_tokens, total_cost, "", True, "interrupted",
                              max(1, total_turns), -1, context_window=res.context_window,
                              context_observable=res.context_observable,
                              context_observable_reason=res.context_observable_reason,
                              rate_limit_status=res.rate_limit_status,
                              rate_limit_resets_at=res.rate_limit_resets_at,
                              cached_input_tokens=res.cached_input_tokens,
                              reasoning_output_tokens=res.reasoning_output_tokens,
                              token_usage_kind=res.token_usage_kind,
                              provider_version=res.provider_version)
        # Stop がレビュー中に来た場合: 以降の集約/修正/sign-off をせず即 cancelled を返す。
        # 返さないと指揮者の success 結果が返り、loop が『成功ターン』として扱い停止が次ターンまで
        # 遅れる (+ GUI/ledger に誤って成功と出る)。interrupted のミラー。
        if self._is_cancelled():
            return TurnResult(res.session_id or session_id, res.input_tokens, res.output_tokens,
                              res.context_tokens, total_cost, "", True, "cancelled",
                              max(1, total_turns), -1, context_window=res.context_window,
                              context_observable=res.context_observable,
                              context_observable_reason=res.context_observable_reason,
                              rate_limit_status=res.rate_limit_status,
                              rate_limit_resets_at=res.rate_limit_resets_at,
                              cached_input_tokens=res.cached_input_tokens,
                              reasoning_output_tokens=res.reasoning_output_tokens,
                              token_usage_kind=res.token_usage_kind,
                              provider_version=res.provider_version)

        # 4. 真偽確認奏者 (あれば): 実装報告 + diff の事実主張を裏取り (best-effort / stateless)。
        factcheck_text = ""
        if self._aux_enabled(self.factchecker) and not self._is_cancelled():
            fc_label = runner_label(self.factchecker)
            fr = self._sub_review(
                self.factchecker, self._factcheck_prompt(res.text, diff),
                f"{session_id}-factcheck", cwd,
                label=fc_label, conductor_label=conductor_label)
            total_cost += fr.cost_usd
            total_turns += fr.num_turns
            self._emit({"kind": "review", "phase": "factcheck", "checker": fc_label,
                        "text": fr.text, "is_error": fr.is_error})
            if not fr.is_error and fr.text.strip():
                factcheck_text = fr.text

        # 5. 責任者が集約 (取りまとめ + 総合判断)。actionable な統合指示を得る。
        # _aggregate は (指示文, 追加コスト, 追加ターン) を返す
        instr_text, agg_cost, agg_turns = self._aggregate(
            res.text, diff, panel, factcheck_text, session_id, cwd)
        total_cost += agg_cost
        total_turns += agg_turns

        final = res
        fixed = False
        # 6. 指揮者が統合指示を反映 (任意・LGTM や空はスキップ)。
        if (self.apply_review and not self._is_cancelled() and instr_text.strip()
                and instr_text.strip().upper() != "LGTM"):
            self.conductor.on_stream = self._emit  # type: ignore[attr-defined]
            fix = self.conductor.run_turn(
                prompt=self._fix_prompt(instr_text), session_id=session_id, resume=True, cwd=cwd)
            total_cost += fix.cost_usd
            total_turns += fix.num_turns
            if fix.is_error and fix.error_kind == "other":
                # review-fix は任意の品質ゲート。その "other" 失敗 (timeout 等) で成功済みの
                # 実装結果 (res) を捨てて全体を error にすると、loop が consec_err を積み 3 連続で
                # circuit_open → 実装は毎回成功しているのに自走が止まる。実装は保持し、fix 失敗は
                # イベントで可視化するに留める (ユーザー方針: ループを止めない)。rate_limited/auth/
                # interrupted/cancelled/unavailable は loop が正しく扱うのでそのまま伝播させる。
                self._emit({"kind": "review", "phase": "fix_failed",
                            "detail": (fix.text or fix.error_kind or "")[:200]})
                # final は res のまま / fixed も False のまま (fix は適用されなかった → sign-off しない)
            else:
                final = fix  # 修正後の状態 (context_tokens/text/error) を最終結果に反映
                fixed = True

        # 7. 最終 sign-off (責任者がループを閉じる)。有界: 再修正はしない (最大 1 回)。
        if (self.final_signoff and self._aux_enabled(self.lead) and fixed
                and not final.is_error and not self._is_cancelled()):
            lead_label = runner_label(self.lead)
            new_diff = self._capture_diff(cwd) if self.include_diff else ""
            sr = self._sub_review(
                self.lead, self._signoff_prompt(new_diff), f"{session_id}-signoff", cwd,
                label=lead_label, conductor_label=conductor_label)
            total_cost += sr.cost_usd
            total_turns += sr.num_turns
            approved = (not sr.is_error) and "APPROVED" in sr.text.strip().upper()[:40]
            self._emit({"kind": "review", "phase": "signoff", "lead": lead_label,
                        "text": sr.text, "is_error": sr.is_error, "approved": approved})

        # 集計した cost / num_turns を最終 TurnResult に載せ替えて返す。
        return TurnResult(
            session_id=final.session_id or session_id,
            input_tokens=final.input_tokens, output_tokens=final.output_tokens,
            context_tokens=final.context_tokens, cost_usd=total_cost, text=final.text,
            is_error=final.is_error, error_kind=final.error_kind, num_turns=max(1, total_turns),
            raw_exit=final.raw_exit, context_window=final.context_window,
            context_observable=final.context_observable,
            context_observable_reason=final.context_observable_reason,
            rate_limit_status=final.rate_limit_status, rate_limit_resets_at=final.rate_limit_resets_at,
            cached_input_tokens=final.cached_input_tokens,
            reasoning_output_tokens=final.reasoning_output_tokens,
            token_usage_kind=final.token_usage_kind,
            provider_version=final.provider_version,
        )

    def run_turn_unreviewed(self, *, prompt: str, session_id: str, resume: bool,
                            cwd: Path) -> TurnResult:
        """指揮者のみで 1 ターン回す (パネル/集約/修正/sign-off を一切掛けない)。

        handoff / exit準備のような「記録目的」ターン向け。これらに 3-AI フルレビューを
        掛けるのは過剰 (ユーザー指摘 2026-06-13: レビューにレビューを重ねている)。loop は
        ``getattr(runner, "run_turn_unreviewed", ...)`` でこの経路を優先する。
        """
        if self._is_cancelled():
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
        self.conductor.on_stream = self._emit  # type: ignore[attr-defined]
        return self.conductor.run_turn(
            prompt=prompt, session_id=session_id, resume=resume, cwd=cwd)

    def _sub_review(self, runner: TurnRunner, prompt: str, session_id: str,
                    cwd: Path, *, label: str, conductor_label: str) -> TurnResult:
        """レビュー系の 1 サブターンを stateless (resume=False) で回す (best-effort)。

        失敗 (例外含む) は is_error=True の TurnResult で握り潰す。構造的失敗
        (rate_limited/auth/unavailable) は bench 対象にする。
        """
        runner.on_stream = self._review_stream  # type: ignore[attr-defined]
        try:
            r = runner.run_turn(prompt=prompt, session_id=session_id, resume=False, cwd=cwd)
        except Exception:  # noqa: BLE001 — レビュー系の失敗は指揮者の結果を殺さない
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "other", 0, -1)
        self._maybe_disable_aux(runner, r, label=label, conductor_label=conductor_label)
        if r.is_error:
            return TurnResult(session_id, r.input_tokens, r.output_tokens, r.context_tokens,
                              r.cost_usd, "", True, r.error_kind, r.num_turns, r.raw_exit,
                              context_window=r.context_window,
                              context_observable=r.context_observable,
                              context_observable_reason=r.context_observable_reason,
                              rate_limit_status=r.rate_limit_status,
                              rate_limit_resets_at=r.rate_limit_resets_at,
                              cached_input_tokens=r.cached_input_tokens,
                              reasoning_output_tokens=r.reasoning_output_tokens,
                              token_usage_kind=r.token_usage_kind,
                              provider_version=r.provider_version)
        return r

    def _aggregate(self, work: str, diff: str, panel: list[tuple[str, str]], factcheck: str,
                   session_id: str, cwd: Path) -> tuple[str, float, int]:
        """責任者にパネル + factcheck を集約させ統合修正指示を得る。

        効率ルール:
        - 責任者がいて (パネル所見 2 件以上 **または** factcheck 所見あり) のときだけ集約呼び出し。
        - 責任者がいない or 単一所見のみなら集約呼び出しを省略し、その単一所見を指示とする
          (パネルが lead と同一の単一 reviewer の場合 = そのレビューが総合判断)。

        戻り値 = (統合指示文, 追加コスト, 追加ターン)。
        """
        need_aggregate = (self._aux_enabled(self.lead)
                          and (len(panel) >= 2 or bool(factcheck.strip()))
                          and not self._is_cancelled())
        if need_aggregate:
            assert self.lead is not None
            lead_label = runner_label(self.lead)
            ar = self._sub_review(
                self.lead, self._aggregate_prompt(work, diff, panel, factcheck),
                f"{session_id}-aggregate", cwd,
                label=lead_label, conductor_label=runner_label(self.conductor))
            self._emit({"kind": "review", "phase": "aggregate", "lead": lead_label,
                        "text": ar.text, "is_error": ar.is_error})
            if not ar.is_error and ar.text.strip():
                return ar.text, ar.cost_usd, ar.num_turns
            # 集約失敗時は fail-safe でパネル所見にフォールバック
            return self._fallback_instructions(panel), ar.cost_usd, ar.num_turns
        return self._fallback_instructions(panel), 0.0, 0

    @staticmethod
    def _fallback_instructions(panel: list[tuple[str, str]]) -> str:
        """集約しない/集約失敗時の指示 (空なら空文字)。

        単一所見はその本文をそのまま指示にする (= その単一レビューが総合判断。'LGTM'/空の
        スキップ判定を従来どおり効かせるためラベルで包まない)。複数はラベル付きで連結する。
        """
        if not panel:
            return ""
        if len(panel) == 1:
            return panel[0][1]
        return "\n\n".join(f"[{label}]\n{text.strip()}" for label, text in panel)

    def _members(self) -> list[object]:
        """指揮者 + 全レビュー奏者 + 真偽確認 + 責任者 (重複は除く)。"""
        targets: list[object] = [self.conductor, *self.reviewers]
        if self.factchecker is not None:
            targets.append(self.factchecker)
        if self.lead is not None:
            targets.append(self.lead)
        seen: set[int] = set()
        uniq: list[object] = []
        for r in targets:
            if id(r) not in seen:
                seen.add(id(r))
                uniq.append(r)
        return uniq

    def cancel(self) -> None:
        """指揮者 + 全レビュー奏者 + 真偽確認 + 責任者を止める (sticky)。"""
        with self._lock:
            self._cancelled = True
        for r in self._members():
            try:
                r.cancel()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def interrupt(self) -> None:
        """現在実行中のサブターン (指揮者/レビュー/真偽確認/責任者) を一発中断する (恒久 cancel ではない)。

        緊急注入用。実行中でないメンバへの interrupt は no-op (proc 無し)。run_turn は
        指揮者フェーズなら "interrupted" を伝播し、レビュー中なら集約前に "interrupted" を返す。
        """
        with self._lock:
            self._interrupted = True
        for r in self._members():
            fn = getattr(r, "interrupt", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
