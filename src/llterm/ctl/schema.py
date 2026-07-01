"""llterm-ctl コマンド schema (fail-closed parser).

spec (llterm_spec_2026_06_06.md §4) の制御コマンド。設計規律:
- 未知 action / 必須フィールド欠落 / 壊れた JSON は ParseError (fail-closed)
- 危険 action (HUMAN_REQUIRED_ACTIONS) は requires_human を True に強制
- reason は監査必須 (なぜ Claude がこれを発火したか)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

ALLOWED_ACTIONS: tuple[str, ...] = (
    "rotate", "set-effort", "inject-task", "fork-session", "query-state", "shutdown",
)
# 人間承認を常に要求する action (書かれた値に関わらず強制)
HUMAN_REQUIRED_ACTIONS: frozenset[str] = frozenset({"shutdown"})


class ParseError(ValueError):
    """制御コマンドの解釈失敗 (fail-closed: 呼び出し側は実行せず ledger へ記録)."""


@dataclass(frozen=True)
class CtlCommand:
    id: str
    action: str
    reason: str
    args: dict = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    requires_human: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        # 危険 action は構築時に requires_human=True へ正規化する。from_json だけでなく emit の
        # 直接構築 (to_dict → on-disk JSON) でも invariant が成立し、永続化された監査記録が
        # 実行時 (poll / gate 判定) と一致する (J7)。
        if self.action in HUMAN_REQUIRED_ACTIONS and not self.requires_human:
            object.__setattr__(self, "requires_human", True)

    @classmethod
    def from_json(cls, raw: str) -> "CtlCommand":
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ParseError(f"broken json: {e}") from e
        if not isinstance(data, dict):
            raise ParseError("top-level must be a json object")

        cmd_id = data.get("id")
        if not isinstance(cmd_id, str) or not cmd_id:
            raise ParseError("missing/invalid 'id'")
        action = data.get("action")
        if action not in ALLOWED_ACTIONS:
            raise ParseError(f"unknown/missing 'action': {action!r}")
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ParseError("missing 'reason' (audit-mandatory)")

        args = data.get("args", {})
        if not isinstance(args, dict):
            raise ParseError("'args' must be an object")
        constraints = data.get("constraints", [])
        if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
            raise ParseError("'constraints' must be a list of strings")

        requires_human = bool(data.get("requires_human", False))
        if action in HUMAN_REQUIRED_ACTIONS:
            requires_human = True  # fail-closed 強制

        return cls(
            id=cmd_id, action=action, reason=reason, args=args,
            constraints=tuple(constraints), requires_human=requires_human,
            created_at=str(data.get("created_at", "")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "action": self.action, "reason": self.reason,
            "args": self.args, "constraints": list(self.constraints),
            "requires_human": self.requires_human, "created_at": self.created_at,
        }
