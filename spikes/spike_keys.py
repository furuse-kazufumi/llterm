"""spike: ReadConsoleInputW で Ctrl+Enter / Shift+Enter / Ctrl+矢印 を区別できるか.

実行 (Windows Terminal 上の実コンソールで手動実行):
    py -3.11 spikes/spike_keys.py
キーを押すと KEY_EVENT の (VirtualKeyCode, Char, ControlKeyState) を表示する。
q で終了。判定基準:
  - Enter / Ctrl+Enter / Shift+Enter が ControlKeyState で区別できること
  - ↑ / Ctrl+↑ / Shift+↑ が区別できること
  - IME 経由の日本語確定文字が Char に来ること
"""
import ctypes
import ctypes.wintypes as w

KEY_EVENT = 0x0001
STD_INPUT_HANDLE = -10

SHIFT_PRESSED = 0x0010
LEFT_CTRL_PRESSED = 0x0008
RIGHT_CTRL_PRESSED = 0x0004


class CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", w.WCHAR), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD),
        ("wVirtualKeyCode", w.WORD), ("wVirtualScanCode", w.WORD),
        ("uChar", CHAR_UNION), ("dwControlKeyState", w.DWORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]
    _fields_ = [("EventType", w.WORD), ("Event", _EVENT)]


def main() -> None:
    k32 = ctypes.windll.kernel32
    h = k32.GetStdHandle(STD_INPUT_HANDLE)
    print("press keys (q to quit) ...")
    rec = INPUT_RECORD()
    n = w.DWORD()
    while True:
        k32.ReadConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(n))
        if rec.EventType != KEY_EVENT or not rec.Event.KeyEvent.bKeyDown:
            continue
        ke = rec.Event.KeyEvent
        ch = ke.uChar.UnicodeChar
        mods = []
        if ke.dwControlKeyState & SHIFT_PRESSED:
            mods.append("SHIFT")
        if ke.dwControlKeyState & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED):
            mods.append("CTRL")
        print(f"vk={ke.wVirtualKeyCode:>3}  char={ch!r:>8}  mods={'+'.join(mods) or '-'}  "
              f"state=0x{ke.dwControlKeyState:08x}")
        if ch == "q":
            break


if __name__ == "__main__":
    main()
