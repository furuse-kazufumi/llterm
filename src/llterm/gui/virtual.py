# SPDX-License-Identifier: Apache-2.0
"""仮想 claude — GUI / デバッグ用に turn 結果を生成する (実 claude 不要・課金ゼロ).

ユーザー指示「仮想でデバッグ繰り返して」(2026-06-11) の中核。``--resume`` の度に文脈使用率が
増え、閾値超で rotate が起きるよう振る舞う。``delay>0`` で進行が GUI 上で見える。
fail_every / auth_after で circuit breaker・認証切れ経路も模擬できる。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from llterm.host.loop import DEFAULT_WINDOW_TOKENS, TurnResult


@dataclass
class VirtualClaudeRunner:
    """TurnRunner 実装。実 claude を呼ばず擬似 TurnResult を返す。"""

    window_tokens: int = DEFAULT_WINDOW_TOKENS
    step_pct: float = 0.22  # 1 継続ターンごとに増える使用率 (≈4 ターンで 70% 超 → rotate)
    delay: float = 0.25  # 1 ターンの擬似所要秒 (GUI で進行が見える。テストは 0)
    cost_per_turn: float = 0.02
    fail_every: int = 0  # >0 なら N ターンごとに一時エラー (circuit breaker 検証用)
    auth_after: int = 0  # >0 なら N ターン目で認証切れを模擬
    _ctx: dict[str, int] = field(default_factory=dict)
    _n: int = 0
    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        waited = 0.0
        while waited < self.delay and not self._cancelled:  # 中断可能な擬似 sleep
            step = min(0.05, self.delay - waited)
            time.sleep(step)
            waited += step
        if self._cancelled:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, 1)
        self._n += 1
        if self.auth_after and self._n == self.auth_after:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "auth", 0, 1)
        if self.fail_every and self._n % self.fail_every == 0:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "other", 0, 1)
        base = self._ctx.get(session_id, 0) if resume else 0
        ctx = base + int(self.window_tokens * self.step_pct)
        self._ctx[session_id] = ctx
        text = (
            f"[virtual claude] 処理: {prompt[:60].strip()}\n"
            f"  session={session_id[:8]} ctx≈{ctx:,} tok (turn #{self._n})"
        )
        return TurnResult(session_id, ctx, 400, ctx, self.cost_per_turn, text, False, "", 1, 0)
