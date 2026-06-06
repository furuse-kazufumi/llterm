from llterm.input.buffer import InputBuffer
from llterm.input.render import render_input_area


def test_render_contains_prompt_and_text():
    b = InputBuffer(); b.paste("hello")
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    assert "hello" in out
    assert "\x1b[27;1H" in out            # 入力欄先頭行 = rows-reserve+1 = 27 行目
    # paste 後 cursor=(0,5)。prompt "llterm> " 8 桁 + width("hello")=5 + 1 → 14
    # (計画 doc の期待値 27;9H は col0 前提の誤り。実バッファは末尾 col5 — 最小修正)
    assert out.endswith("\x1b[27;14H")


def test_render_clears_reserved_lines():
    b = InputBuffer()
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    assert out.count("\x1b[2K") == 4      # 予約 4 行を毎回クリア


def test_render_multiline_cursor_position():
    b = InputBuffer(); b.paste("ab\ncd")  # cursor=(1,2)
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    # 2 行目 (28 行目)、継続 prompt "      | " 8 桁 + col2 → 11 桁目
    assert out.endswith("\x1b[28;11H")


def test_render_cjk_width_counted_as_two():
    b = InputBuffer(); b.paste("あい")     # 全角 2 文字 = 表示幅 4
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    assert out.endswith("\x1b[27;13H")    # 8 + 4 + 1 = 13 (R8: EAW 幅計算)


def test_render_scrolls_window_when_buffer_exceeds_reserve():
    # レビュー finding (high): 5 行以上でカーソルが scroll region 内に飛んでいた。
    # 表示窓はカーソル行を必ず含む末尾 reserve 行へスクロールする。
    b = InputBuffer(); b.paste("l0\nl1\nl2\nl3\nl4\nl5")   # cursor=(5,2)
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    assert "l2" in out and "l5" in out     # 窓 = 行 2..5
    assert "l0" not in out and "l1" not in out
    # カーソルは入力欄最下行 (30 行目)、CONT 8 桁 + col2 → 11
    assert out.endswith("\x1b[30;11H")


def test_render_cursor_row_never_exceeds_term_rows():
    # どれだけ行が増えてもカーソル Y は term_rows を超えない (上部領域へ侵入しない)
    b = InputBuffer(); b.paste("\n".join(f"x{i}" for i in range(20)))
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    pos = out.rsplit("\x1b[", 1)[1]        # 最後のカーソル復帰 "ROW;COLH"
    row = int(pos.split(";")[0])
    assert 27 <= row <= 30
