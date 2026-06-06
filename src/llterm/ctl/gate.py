"""実行ゲート: enabled allowlist + requires_human (defense in depth).

schema が型レベルで fail-closed しても、運用設定 (enabled_actions) でさらに絞る。
判定は 3 値: EXECUTE / HOLD_FOR_HUMAN (人間承認待ち) / REJECT (拒否+ledger)。
"""
from __future__ import annotations

import enum

from llterm.ctl.schema import HUMAN_REQUIRED_ACTIONS, CtlCommand

# v1 既定で有効な action (set-effort / fork-session は host 実装が入る Task12 以降に拡張)
DEFAULT_ENABLED: tuple[str, ...] = ("rotate", "inject-task", "query-state", "shutdown")


class GateDecision(enum.Enum):
    EXECUTE = "execute"
    HOLD_FOR_HUMAN = "hold_for_human"
    REJECT = "reject"


def evaluate(cmd: CtlCommand, *, enabled_actions: tuple[str, ...] = DEFAULT_ENABLED) -> GateDecision:
    if cmd.action not in enabled_actions:
        return GateDecision.REJECT
    if cmd.requires_human or cmd.action in HUMAN_REQUIRED_ACTIONS:
        return GateDecision.HOLD_FOR_HUMAN
    return GateDecision.EXECUTE
