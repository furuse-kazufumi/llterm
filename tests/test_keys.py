from llterm.input.keys import KeyEvent, Action, decode

VK_RETURN, VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT, VK_BACK = 0x0D, 0x26, 0x28, 0x25, 0x27, 0x08


def _k(vk=0, ch="", shift=False, ctrl=False):
    return KeyEvent(vk=vk, char=ch, shift=shift, ctrl=ctrl)


def test_plain_enter_is_newline():
    assert decode(_k(vk=VK_RETURN, ch="\r")) == (Action.NEWLINE, None)


def test_ctrl_enter_is_submit():
    assert decode(_k(vk=VK_RETURN, ctrl=True)) == (Action.SUBMIT, None)


def test_shift_enter_is_submit():
    assert decode(_k(vk=VK_RETURN, shift=True)) == (Action.SUBMIT, None)


def test_plain_arrows_are_cursor_moves():
    assert decode(_k(vk=VK_UP)) == (Action.MOVE, "up")
    assert decode(_k(vk=VK_DOWN)) == (Action.MOVE, "down")
    assert decode(_k(vk=VK_LEFT)) == (Action.MOVE, "left")
    assert decode(_k(vk=VK_RIGHT)) == (Action.MOVE, "right")


def test_ctrl_up_down_is_history():
    assert decode(_k(vk=VK_UP, ctrl=True)) == (Action.HISTORY_PREV, None)
    assert decode(_k(vk=VK_DOWN, ctrl=True)) == (Action.HISTORY_NEXT, None)


def test_shift_up_down_is_history_too():
    assert decode(_k(vk=VK_UP, shift=True)) == (Action.HISTORY_PREV, None)
    assert decode(_k(vk=VK_DOWN, shift=True)) == (Action.HISTORY_NEXT, None)


def test_backspace():
    assert decode(_k(vk=VK_BACK, ch="\x08")) == (Action.BACKSPACE, None)


def test_printable_char_inserts():
    assert decode(_k(ch="あ")) == (Action.INSERT, "あ")
    assert decode(_k(ch="x")) == (Action.INSERT, "x")


def test_control_chars_ignored():
    assert decode(_k(ch="\x00")) == (Action.NONE, None)
    assert decode(_k(vk=0x10)) == (Action.NONE, None)   # 単独 Shift キー
