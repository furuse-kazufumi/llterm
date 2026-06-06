from llterm.host.vtbridge import VtResponseFilter
from llterm.input.keys import KeyEvent


def _chars(s: str) -> list[KeyEvent]:
    return [KeyEvent(char=c) for c in s]


def test_da1_response_separated_from_keys():
    # 実機 2026-06-07: claude の DA1 クエリ素通し → 実端末が応答を入力に書き戻す
    f = VtResponseFilter()
    keys, resp = f.feed(_chars("\x1b[?61;4;6c"))
    assert keys == []
    assert resp == ["\x1b[?61;4;6c"]


def test_osc_response_with_st_terminator():
    # OSC 11 応答 (背景色)。終端 = ESC \ (ST)
    f = VtResponseFilter()
    keys, resp = f.feed(_chars("\x1b]11;rgb:0c0c/0c0c/0c0c\x1b\\"))
    assert keys == []
    assert resp == ["\x1b]11;rgb:0c0c/0c0c/0c0c\x1b\\"]


def test_osc_response_with_bel_terminator():
    f = VtResponseFilter()
    keys, resp = f.feed(_chars("\x1b]11;rgb:1111/2222/3333\x07"))
    assert resp == ["\x1b]11;rgb:1111/2222/3333\x07"]


def test_split_across_batches():
    # 応答が 2 バッチに分割到着しても完結する
    f = VtResponseFilter()
    keys1, resp1 = f.feed(_chars("\x1b[?61;4"))
    assert keys1 == [] and resp1 == []
    keys2, resp2 = f.feed(_chars(";6c"))
    assert keys2 == []
    assert resp2 == ["\x1b[?61;4;6c"]


def test_normal_keys_pass_through():
    f = VtResponseFilter()
    evs = _chars("ab") + [KeyEvent(vk=0x26)]      # a, b, ↑ (vk のみ)
    keys, resp = f.feed(evs)
    assert keys == evs
    assert resp == []


def test_vk_only_events_pass_during_sequence():
    # シーケンス収集中でも純 vk イベント (char='\x00' / '') はキー側へ
    f = VtResponseFilter()
    evs = _chars("\x1b[?6") + [KeyEvent(vk=0x26)] + _chars("1c")
    keys, resp = f.feed(evs)
    assert keys == [KeyEvent(vk=0x26)]
    assert resp == ["\x1b[?61c"]


def test_esc_then_normal_char_flushes_esc_as_key():
    # CSI/OSC でない ESC + 文字: ESC は捨てず key として流し、文字も通す
    f = VtResponseFilter()
    esc = KeyEvent(vk=0x1B, char="\x1b")
    a = KeyEvent(char="a")
    keys, resp = f.feed([esc, a])
    assert keys == [esc, a]
    assert resp == []


def test_keys_mixed_around_response():
    # 応答の前後の通常キーは順序を保って通る
    f = VtResponseFilter()
    a, b = KeyEvent(char="a"), KeyEvent(char="b")
    keys, resp = f.feed([a] + _chars("\x1b[0c") + [b])
    assert keys == [a, b]
    assert resp == ["\x1b[0c"]
