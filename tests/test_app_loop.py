from pathlib import Path
from llterm.app import App
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand
from llterm.input.keys import KeyEvent


class FakePty:
    def __init__(self, max_ticks: int = 50, final_chunk: str = "", chunks: list | None = None):
        self.sent = []
        self.raw = []
        self._alive = True
        self._ticks = max_ticks          # 無限ループ保険: 一定周回で自然死
        self._final_chunk = final_chunk  # 死亡後に 1 回だけ返す残データ (EOF drain 検証)
        self._chunks = list(chunks or [])  # alive 中に順に返す出力 (素通し検証)
    def spawn(self): pass
    def isalive(self):
        self._ticks -= 1
        if self._ticks <= 0:
            self._alive = False
        return self._alive
    def read(self, n):
        if self._alive and self._chunks:
            return self._chunks.pop(0)
        if not self._alive and self._final_chunk:
            chunk, self._final_chunk = self._final_chunk, ""
            return chunk
        return ""
    def send_text(self, t): self.sent.append(t)
    def write_raw(self, d): self.raw.append(d)
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


def test_final_chunk_drained_after_child_death(tmp_path: Path, capsys):
    # レビュー finding (high): 子の最終出力チャンクが isalive() 先行チェックで失われる
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    app.host = FakePty(max_ticks=1, final_chunk="LAST-WORDS")
    app.console = FakeConsole([])
    rc = app.run()
    assert rc == 0
    assert "LAST-WORDS" in capsys.readouterr().out  # 死後の残データも画面に出る


def test_terminal_response_forwarded_not_inserted(tmp_path: Path):
    # 実機バグ 2026-06-07: DA1 応答が入力欄を汚染し子に届かない
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    pty = FakePty(max_ticks=5)
    app.host = pty
    app.console = FakeConsole([KeyEvent(char=c) for c in "\x1b[?61;4;6c"])
    app.run()
    assert "".join(pty.raw) == "\x1b[?61;4;6c"    # 子へそのまま転送
    assert app.buf.text == ""                      # 入力欄は汚染されない


def test_empty_buffer_plain_arrow_and_enter_passthrough(tmp_path: Path):
    # 実機知見 2026-06-07: claude の選択 UI (信頼確認等) は矢印/Enter の直接入力が必要
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    pty = FakePty(max_ticks=5)
    app.host = pty
    app.console = FakeConsole([
        KeyEvent(vk=0x28),                        # ↓ (plain)
        KeyEvent(vk=0x0D, char="\r"),             # Enter (plain)
    ])
    app.run()
    assert pty.raw == ["\x1b[B", "\r"]            # 空欄時は子へ直接転送
    assert app.buf.text == ""                     # 入力欄は動かない


def test_nonempty_buffer_keeps_local_editing(tmp_path: Path):
    # 入力欄に内容があるときは R12/R13 どおりローカル編集 (パススルーしない)
    app = App(["dummy"], ctl_root=tmp_path / ".llterm")
    pty = FakePty(max_ticks=5)
    app.host = pty
    app.console = FakeConsole([
        KeyEvent(char="a"),
        KeyEvent(vk=0x0D, char="\r"),             # Enter = 改行挿入
        KeyEvent(char="b"),
        KeyEvent(vk=0x26),                        # ↑ = カーソル移動
    ])
    app.run()
    assert pty.raw == []
    assert app.buf.text == "a\nb"
    assert app.buf.cursor == (0, 1)
