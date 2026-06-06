"""Windows Console 入力 (ReadConsoleInputW) → KeyEvent 変換.

spike_keys.py の確定結果を本実装化。ポーリング型 (GetNumberOfConsoleInputEvents で
非ブロック確認 → あれば読む) にして、app のメインループ (PTY read / ctl tick と同居) に乗せる。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w

from llterm.input.keys import KeyEvent

KEY_EVENT = 0x0001
STD_INPUT_HANDLE = -10
SHIFT_PRESSED = 0x0010
CTRL_PRESSED = 0x0008 | 0x0004


class _CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", w.WCHAR), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD),
                ("wVirtualKeyCode", w.WORD), ("wVirtualScanCode", w.WORD),
                ("uChar", _CHAR_UNION), ("dwControlKeyState", w.DWORD)]


class _INPUT_RECORD(ctypes.Structure):
    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]
    _fields_ = [("EventType", w.WORD), ("Event", _EVENT)]


class ConsoleInput:
    def __init__(self) -> None:
        self._k32 = ctypes.windll.kernel32
        self._h = self._k32.GetStdHandle(STD_INPUT_HANDLE)

    def poll_events(self, max_events: int = 32) -> list[KeyEvent]:
        n_avail = w.DWORD()
        self._k32.GetNumberOfConsoleInputEvents(self._h, ctypes.byref(n_avail))
        events: list[KeyEvent] = []
        for _ in range(min(n_avail.value, max_events)):
            rec = _INPUT_RECORD()
            n = w.DWORD()
            self._k32.ReadConsoleInputW(self._h, ctypes.byref(rec), 1, ctypes.byref(n))
            if rec.EventType != KEY_EVENT or not rec.Event.KeyEvent.bKeyDown:
                continue
            ke = rec.Event.KeyEvent
            for _ in range(max(1, ke.wRepeatCount)):
                events.append(KeyEvent(
                    vk=ke.wVirtualKeyCode,
                    char=ke.uChar.UnicodeChar or "",
                    shift=bool(ke.dwControlKeyState & SHIFT_PRESSED),
                    ctrl=bool(ke.dwControlKeyState & CTRL_PRESSED),
                ))
        return events
