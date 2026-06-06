from llterm.input.buffer import InputBuffer


def test_insert_and_text():
    b = InputBuffer()
    for ch in "abc":
        b.insert(ch)
    assert b.text == "abc"
    assert b.cursor == (0, 3)          # (row, col)


def test_enter_inserts_newline_not_submit():
    b = InputBuffer()
    b.insert("a")
    b.newline()                          # R12: Enter は常に改行挿入
    b.insert("b")
    assert b.text == "a\nb"
    assert b.cursor == (1, 1)


def test_paste_multiline_kept_as_one_input():
    b = InputBuffer()
    b.paste("line1\r\nline2\nline3")     # R11: CRLF/LF 混在も正規化して保持
    assert b.text == "line1\nline2\nline3"
    assert b.cursor == (2, 5)


def test_arrow_moves_cursor_within_lines():
    b = InputBuffer()
    b.paste("ab\ncd")
    b.move("up")                         # R13: 矢印はカーソル移動のみ
    assert b.cursor == (0, 2)
    b.move("left")
    assert b.cursor == (0, 1)
    b.move("down")
    assert b.cursor == (1, 1)
    b.move("right")
    assert b.cursor == (1, 2)


def test_arrow_at_edges_is_noop():
    b = InputBuffer()
    b.move("up"); b.move("left"); b.move("down"); b.move("right")
    assert b.cursor == (0, 0)


def test_backspace_joins_lines():
    # カーソル移動 API で (1,0) に自然に到達してから backspace する
    # (paste 後 cursor=(1,2) → left×2 で (1,0))
    b = InputBuffer()
    b.paste("ab\ncd")
    b.move("left"); b.move("left")        # cursor を (1,0) へ動かす
    assert b.cursor == (1, 0)
    b.backspace()
    assert b.text == "abcd"
    assert b.cursor == (0, 2)


def test_take_returns_and_clears():
    b = InputBuffer()
    b.paste("hello")
    assert b.take() == "hello"           # 送信時に取り出してクリア
    assert b.text == ""
    assert b.cursor == (0, 0)


def test_history_recall_ctrl_arrows():
    b = InputBuffer()
    b.paste("first"); b.push_history(b.take())
    b.paste("second"); b.push_history(b.take())
    b.history_prev()                     # R13: Ctrl+↑ 相当
    assert b.text == "second"
    b.history_prev()
    assert b.text == "first"
    b.history_prev()                     # 先頭で停止 (wrap しない)
    assert b.text == "first"
    b.history_next()
    assert b.text == "second"
    b.history_next()                     # 末尾を超えたら空に戻る
    assert b.text == ""


def test_editing_after_recall_does_not_mutate_history():
    b = InputBuffer()
    b.paste("orig"); b.push_history(b.take())
    b.history_prev()
    b.insert("!")
    assert b.text == "orig!"
    b2_text = b.take()
    b.history_prev()
    assert b.text == "orig"              # 履歴原本は不変
