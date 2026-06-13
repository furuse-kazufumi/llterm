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

from llterm.host.loop import TurnResult, TurnRunner

_DIFF_MAX_CHARS = 4000   # レビューに添える git diff の上限 (プロンプト肥大を防ぐ)
_REVIEW_MAX_CHARS = 4000  # 指揮者へ渡すレビュー/集約本文の上限
_PANEL_MAX_CHARS = 2000  # 集約プロンプトに載せる 1 レビューあたりの上限


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

    # ─── 1 ターン = 実装 → パネル → 真偽確認 → 集約 → 修正 → sign-off ─────
    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        if self._is_cancelled():
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)

        # 1. 指揮者が実装。stream は指揮者のものをそのまま流す。
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
            label = runner_label(rev_runner)
            independent = label != conductor_label
            self._emit({"kind": "review", "phase": "start", "reviewer": label,
                        "conductor": conductor_label, "independent": independent, "index": i})
            # 後方互換: 旧 API の単一 reviewer は `-review` (無印)。パネルは `-review{i}`。
            review_sid = f"{session_id}-review" if self._legacy_single else f"{session_id}-review{i}"
            cost, turns, text, is_error = self._sub_review(
                rev_runner, self._review_prompt(res.text, diff), review_sid, cwd)
            total_cost += cost
            total_turns += turns
            self._emit({"kind": "review", "phase": "end", "reviewer": label,
                        "conductor": conductor_label, "independent": independent, "index": i,
                        "text": text, "is_error": is_error})
            if not is_error and text.strip():
                panel.append((label, text))

        # 4. 真偽確認奏者 (あれば): 実装報告 + diff の事実主張を裏取り (best-effort / stateless)。
        factcheck_text = ""
        if self.factchecker is not None and not self._is_cancelled():
            fc_label = runner_label(self.factchecker)
            cost, turns, text, is_error = self._sub_review(
                self.factchecker, self._factcheck_prompt(res.text, diff),
                f"{session_id}-factcheck", cwd)
            total_cost += cost
            total_turns += turns
            self._emit({"kind": "review", "phase": "factcheck", "checker": fc_label,
                        "text": text, "is_error": is_error})
            if not is_error and text.strip():
                factcheck_text = text

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
            final = fix  # 修正後の状態 (context_tokens/text/error) を最終結果に反映
            fixed = True

        # 7. 最終 sign-off (責任者がループを閉じる)。有界: 再修正はしない (最大 1 回)。
        if (self.final_signoff and self.lead is not None and fixed
                and not final.is_error and not self._is_cancelled()):
            lead_label = runner_label(self.lead)
            new_diff = self._capture_diff(cwd) if self.include_diff else ""
            cost, turns, text, is_error = self._sub_review(
                self.lead, self._signoff_prompt(new_diff), f"{session_id}-signoff", cwd)
            total_cost += cost
            total_turns += turns
            approved = (not is_error) and "APPROVED" in text.strip().upper()[:40]
            self._emit({"kind": "review", "phase": "signoff", "lead": lead_label,
                        "text": text, "is_error": is_error, "approved": approved})

        # 集計した cost / num_turns を最終 TurnResult に載せ替えて返す。
        return TurnResult(
            session_id=final.session_id or session_id,
            input_tokens=final.input_tokens, output_tokens=final.output_tokens,
            context_tokens=final.context_tokens, cost_usd=total_cost, text=final.text,
            is_error=final.is_error, error_kind=final.error_kind, num_turns=max(1, total_turns),
            raw_exit=final.raw_exit, context_window=final.context_window,
            rate_limit_status=final.rate_limit_status, rate_limit_resets_at=final.rate_limit_resets_at,
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
                    cwd: Path) -> tuple[float, int, str, bool]:
        """レビュー系の 1 サブターンを stateless (resume=False) で回す (best-effort)。

        戻り値 = (cost, num_turns, text, is_error)。失敗 (例外含む) は is_error=True で握り潰す。
        """
        runner.on_stream = self._review_stream  # type: ignore[attr-defined]
        try:
            r = runner.run_turn(prompt=prompt, session_id=session_id, resume=False, cwd=cwd)
        except Exception:  # noqa: BLE001 — レビュー系の失敗は指揮者の結果を殺さない
            return 0.0, 0, "", True
        text = r.text if not r.is_error else ""
        return r.cost_usd, r.num_turns, text, r.is_error

    def _aggregate(self, work: str, diff: str, panel: list[tuple[str, str]], factcheck: str,
                   session_id: str, cwd: Path) -> tuple[str, float, int]:
        """責任者にパネル + factcheck を集約させ統合修正指示を得る。

        効率ルール:
        - 責任者がいて (パネル所見 2 件以上 **または** factcheck 所見あり) のときだけ集約呼び出し。
        - 責任者がいない or 単一所見のみなら集約呼び出しを省略し、その単一所見を指示とする
          (パネルが lead と同一の単一 reviewer の場合 = そのレビューが総合判断)。

        戻り値 = (統合指示文, 追加コスト, 追加ターン)。
        """
        need_aggregate = (self.lead is not None
                          and (len(panel) >= 2 or bool(factcheck.strip()))
                          and not self._is_cancelled())
        if need_aggregate:
            assert self.lead is not None
            lead_label = runner_label(self.lead)
            cost, turns, text, is_error = self._sub_review(
                self.lead, self._aggregate_prompt(work, diff, panel, factcheck),
                f"{session_id}-aggregate", cwd)
            self._emit({"kind": "review", "phase": "aggregate", "lead": lead_label,
                        "text": text, "is_error": is_error})
            if not is_error and text.strip():
                return text, cost, turns
            # 集約失敗時は fail-safe でパネル所見にフォールバック
            return self._fallback_instructions(panel), cost, turns
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

    def cancel(self) -> None:
        """指揮者 + 全レビュー奏者 + 真偽確認 + 責任者を止める (sticky)。"""
        with self._lock:
            self._cancelled = True
        targets: list[object] = [self.conductor, *self.reviewers]
        if self.factchecker is not None:
            targets.append(self.factchecker)
        if self.lead is not None:
            targets.append(self.lead)
        for r in targets:
            try:
                r.cancel()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
