import sys
import pytest
from llterm.host.pty_host import build_paste_payload, PtyHost


def test_build_paste_payload_wraps_bracketed():
    p = build_paste_payload("hello")
    assert p == "\x1b[200~hello\x1b[201~\r"


def test_build_paste_payload_normalizes_newlines():
    # ペースト内は LF→CR (Claude TUI はペースト内 CR を改行挿入として扱う)
    p = build_paste_payload("a\nb\r\nc")
    assert p == "\x1b[200~a\rb\rc\x1b[201~\r"


def test_build_paste_payload_empty_sends_nothing():
    assert build_paste_payload("") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="pywinpty is windows-only")
def test_pty_roundtrip_with_python_child():
    # 実 PTY 統合: echo する Python 子で起動→送信→出力受信
    host = PtyHost(["py", "-3.11", "-c",
                    "s=input();print('GOT:'+s)"], rows=20, cols=80)
    host.spawn()
    host.send_text("ping")
    out = host.read_until("GOT:ping", timeout=10.0)
    assert "GOT:ping" in out
    host.close()
