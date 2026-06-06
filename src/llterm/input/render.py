"""入力欄の ANSI 描画 (純関数 — 文字列を返すだけ、書き込みは呼び出し側).

最下部 reserve 行をクリアして InputBuffer の内容を描き、
最後に「実カーソル」を編集位置へ動かす (IME composition がここに出る = R10)。
幅計算は East Asian Width で F/W/A を 2 桁とする (R8, 日本語ロケール準拠)。
"""
from __future__ import annotations

import unicodedata

from llterm.input.buffer import InputBuffer

PROMPT = "llterm> "
CONT = "      | "


def _disp_width(s: str) -> int:
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
    return w


def render_input_area(buf: InputBuffer, *, term_rows: int, term_cols: int, reserve: int) -> str:
    top = term_rows - reserve + 1          # 入力欄の先頭スクリーン行 (1-origin)
    out: list[str] = []
    out.append("\x1b[?25l")                # 描画中はカーソル非表示 (ちらつき抑制)
    for i in range(reserve):
        out.append(f"\x1b[{top + i};1H\x1b[2K")
        if i < len(buf.lines):
            prefix = PROMPT if i == 0 else CONT
            line = buf.lines[i]
            # 桁あふれは末尾切り (v1: 横スクロールは v2)
            avail = term_cols - len(prefix)
            shown, w = "", 0
            for ch in line:
                cw = _disp_width(ch)
                if w + cw > avail:
                    break
                shown += ch
                w += cw
            out.append(f"\x1b[7m{prefix}\x1b[0m{shown}" if i == 0 else f"{prefix}{shown}")
    row, col = buf.cursor
    prefix = PROMPT if row == 0 else CONT
    cur_x = len(prefix) + _disp_width(buf.lines[row][:col]) + 1
    out.append("\x1b[?25h")                # カーソル再表示
    out.append(f"\x1b[{top + row};{cur_x}H")
    return "".join(out)
