"""KeyEvent → 編集 Action のマップ (R12/R13 の規則そのもの).

Enter            -> NEWLINE   (改行挿入のみ)
Ctrl/Shift+Enter -> SUBMIT    (明示送信)
矢印              -> MOVE      (カーソル移動のみ)
Ctrl/Shift+↑/↓   -> HISTORY_PREV / HISTORY_NEXT
印字可能文字       -> INSERT    (IME 確定文字含む)
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

VK_RETURN, VK_BACK = 0x0D, 0x08
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
_ARROWS = {VK_UP: "up", VK_DOWN: "down", VK_LEFT: "left", VK_RIGHT: "right"}


@dataclass(frozen=True)
class KeyEvent:
    vk: int = 0
    char: str = ""
    shift: bool = False
    ctrl: bool = False


class Action(enum.Enum):
    NONE = "none"
    INSERT = "insert"
    NEWLINE = "newline"
    SUBMIT = "submit"
    MOVE = "move"
    HISTORY_PREV = "history_prev"
    HISTORY_NEXT = "history_next"
    BACKSPACE = "backspace"


def decode(ev: KeyEvent) -> tuple[Action, str | None]:
    mod = ev.shift or ev.ctrl
    if ev.vk == VK_RETURN:
        return (Action.SUBMIT, None) if mod else (Action.NEWLINE, None)
    if ev.vk == VK_BACK:
        return (Action.BACKSPACE, None)
    if ev.vk in _ARROWS:
        direction = _ARROWS[ev.vk]
        if mod and direction in ("up", "down"):
            return (Action.HISTORY_PREV if direction == "up" else Action.HISTORY_NEXT, None)
        return (Action.MOVE, direction)
    if ev.char and ev.char.isprintable():
        return (Action.INSERT, ev.char)
    return (Action.NONE, None)
