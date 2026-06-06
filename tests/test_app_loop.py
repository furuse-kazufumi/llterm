from pathlib import Path
from llterm.app import App
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand
from llterm.input.keys import KeyEvent


class FakePty:
    def __init__(self, max_ticks: int = 50):
        self.sent = []
        self._alive = True
        self._ticks = max_ticks          # 無限ループ保険: 一定周回で自然死
    def spawn(self): pass
    def isalive(self):
        self._ticks -= 1
        if self._ticks <= 0:
            self._alive = False
        return self._alive
    def read(self, n): return ""
    def send_text(self, t): self.sent.append(t)
    def close(self): self._alive = False


class FakeConsole:
    def __init__(self, events): self._events = list(events)
    def poll_events(self):
        evs, self._events = self._events, []
        return evs


def test_rotate_via_ctl_exits_loop_with_75(tmp_path: Path):
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    app.host = FakePty()
    app.console = FakeConsole([])
    CtlQueue(tmp_path / ".llterm").submit(
        CtlCommand(id="ctl-1", action="rotate", reason="test"))
    rc = app.run()
    assert rc == 75
    assert app.rotate_requested == "test"


def test_submit_sends_buffered_text_to_pty(tmp_path: Path):
    # h, i を打って Ctrl+Enter → host.send_text("hi") が 1 回呼ばれる
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    pty = FakePty(max_ticks=5)
    app.host = pty
    app.console = FakeConsole([
        KeyEvent(char="h"), KeyEvent(char="i"),
        KeyEvent(vk=0x0D, ctrl=True),            # Ctrl+Enter = SUBMIT
    ])
    rc = app.run()
    assert rc == 0                                # 子の自然死で正常終了
    assert pty.sent == ["hi"]
    assert app.buf.text == ""                     # 送信後バッファはクリア


def test_child_death_exits_loop_with_0(tmp_path: Path):
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    app.host = FakePty(max_ticks=2)
    app.console = FakeConsole([])
    rc = app.run()
    assert rc == 0
