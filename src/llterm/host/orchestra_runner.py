# SPDX-License-Identifier: Apache-2.0
"""OrchestraRunner — 指揮者 × レビュー奏者の分業を 1 ターンに束ねる TurnRunner。

オーケストラ体制の中核。フェイルオーバー (1 人ずつ交代) とは別物で、**同じ 1 ターン内で
複数 AI が分業**する:

    1. **指揮者 (conductor)** が実装する (ファイル編集など)。Claude/Codex/Gemini 等の agent。
    2. ラッパが変更を ``git diff`` で捕捉する。
    3. **レビュー奏者 (reviewer)** が批評する (バグ/抜け/リスク)。無料奏者推奨 = token 節約。
    4. (任意) 指揮者がレビューを受けて修正ターンを回す (apply_review)。

``TurnRunner`` プロトコルに準拠するので、provider chain にそのまま主奏者として差せる
(SessionLoop は無改造)。レビュー奏者を無料枠 AI にすれば、レビュー分の token を Claude から
逃がしつつ品質ゲートを足せる。

fail-safe: レビュー奏者が失敗 (レート制限等) しても指揮者の結果は返す (レビューは best-effort)。
指揮者が失敗したらレビューせず即返す。cancel は両者へ伝播。
"""
from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from llterm.host.loop import TurnResult, TurnRunner

_DIFF_MAX_CHARS = 4000   # レビューに添える git diff の上限 (プロンプト肥大を防ぐ)
_REVIEW_MAX_CHARS = 4000  # 指揮者へ渡すレビュー本文の上限


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
    """指揮者と レビュー奏者の分業を 1 ターンに束ねる。"""

    conductor: TurnRunner
    reviewer: TurnRunner
    apply_review: bool = True   # True: 指揮者がレビューを受けて修正ターンを回す
    include_diff: bool = True   # レビューに git diff を添える
    on_stream: Callable[[dict], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)

    # ─── prompts (指揮者/レビュー奏者への内部指示文) ───────────────
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

    def _fix_prompt(self, review: str) -> str:
        return (
            "レビュー奏者から次の指摘を受けた。妥当な点のみ反映し、不要な指摘はその理由を一言"
            "添えて続行せよ。確認は求めない (自律継続)。\n\n## レビュー指摘\n"
            + review.strip()[:_REVIEW_MAX_CHARS]
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

    # ─── 1 ターン = 実装 → レビュー → (任意) 修正 ──────────────────
    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        with self._lock:
            if self._cancelled:
                return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)

        # 1. 指揮者が実装。stream は指揮者のものをそのまま流す。
        self.conductor.on_stream = self._emit  # type: ignore[attr-defined]
        res = self.conductor.run_turn(prompt=prompt, session_id=session_id, resume=resume, cwd=cwd)
        total_cost = res.cost_usd
        total_turns = res.num_turns
        if res.is_error:
            return res  # 指揮者が失敗 → レビューせず即返す (cancelled/auth/rate を loop に委ねる)
        with self._lock:
            if self._cancelled:
                return res

        # 2. 変更を捕捉。
        diff = self._capture_diff(cwd) if self.include_diff else ""

        # 3. レビュー奏者が批評 (best-effort)。レビューは新規セッション (ステートレス)。
        self._emit({"kind": "review", "phase": "start", "reviewer": runner_label(self.reviewer)})
        self.reviewer.on_stream = self._review_stream  # type: ignore[attr-defined]
        rev = self.reviewer.run_turn(
            prompt=self._review_prompt(res.text, diff),
            session_id=f"{session_id}-review", resume=False, cwd=cwd,
        )
        total_cost += rev.cost_usd
        total_turns += rev.num_turns
        review_text = rev.text if not rev.is_error else ""
        self._emit({"kind": "review", "phase": "end", "reviewer": runner_label(self.reviewer),
                    "text": review_text, "is_error": rev.is_error})

        final = res
        # 4. 指揮者がレビューを反映 (任意・LGTM や空はスキップ)。
        with self._lock:
            cancelled = self._cancelled
        if (self.apply_review and not cancelled and review_text.strip()
                and review_text.strip().upper() != "LGTM"):
            self.conductor.on_stream = self._emit  # type: ignore[attr-defined]
            fix = self.conductor.run_turn(
                prompt=self._fix_prompt(review_text), session_id=session_id, resume=True, cwd=cwd)
            total_cost += fix.cost_usd
            total_turns += fix.num_turns
            final = fix  # 修正後の状態 (context_tokens/text/error) を最終結果に反映

        # 集計した cost / num_turns を最終 TurnResult に載せ替えて返す。
        return TurnResult(
            session_id=final.session_id or session_id,
            input_tokens=final.input_tokens, output_tokens=final.output_tokens,
            context_tokens=final.context_tokens, cost_usd=total_cost, text=final.text,
            is_error=final.is_error, error_kind=final.error_kind, num_turns=max(1, total_turns),
            raw_exit=final.raw_exit, context_window=final.context_window,
            rate_limit_status=final.rate_limit_status, rate_limit_resets_at=final.rate_limit_resets_at,
        )

    def _review_stream(self, item: dict) -> None:
        """レビュー奏者の stream を review タグ付きで流す (GUI が区別表示できる)。"""
        self._emit({**item, "review": True})

    def cancel(self) -> None:
        """指揮者とレビュー奏者の両方を止める (sticky)。"""
        with self._lock:
            self._cancelled = True
        for r in (self.conductor, self.reviewer):
            try:
                r.cancel()
            except Exception:  # noqa: BLE001
                pass
