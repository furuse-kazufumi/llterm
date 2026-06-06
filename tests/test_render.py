from llterm.input.buffer import InputBuffer
from llterm.input.render import render_input_area


def test_render_contains_prompt_and_text():
    b = InputBuffer(); b.paste("hello")
    out = render_input_area(b, term_rows=30, term_cols=80, reserve=4)
    assert "hello" in out
    assert "\x1b[27;1H" in out            # 入力欄先頭行 = rows-reserve+1 = 27 行目
    assert out.endswith("\x1b[27;9H")     # カーソル復帰: prompt "llterm> " 8 桁 + col0 → 9


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
