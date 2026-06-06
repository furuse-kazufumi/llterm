"""縮小 PTY ホスト: 子 (claude / pwsh) を rows-N で起動し、出力素通し・一括注入を担う.

設計 (spec §2.5 案(b) / spike Task7 の実証結果を反映):
- PTY を実端末より reserve 行小さく作る → 子 TUI は上部しか知らない (spike Task7 で侵食なしを実証)
- send_text() は bracketed paste で包み LF→CR 正規化 + 末尾 CR 1 回
  (ccr の連結バグ知見: TUI はペースト内 CR を改行挿入、裸 CR を送信として扱う)
- **reader thread 必須 (spike Task7 の教訓)**: winpty の read はブロッキングで、子が exit しても
  read が返らずメインループが終了しない (ConPTY drain 問題, node-pty #375/#1810 と同根)。
  blocking read を daemon スレッドへ隔離し、read() は non-blocking で deque から取り出す。
  EOF/例外で _eof を立て、isalive() が確実に False を返してループが抜けられる。
"""
from __future__ import annotations

import collections
import threading
import time


def build_paste_payload(text: str) -> str:
    if not text:
        return ""
    body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    return f"\x1b[200~{body}\x1b[201~\r"


class PtyHost:
    def __init__(self, argv: list[str], *, rows: int, cols: int) -> None:
        self.argv = argv
        self.rows = rows
        self.cols = cols
        self._pty = None
        self._buf: collections.deque[str] = collections.deque()
        self._eof = False
        self._reader: threading.Thread | None = None

    def spawn(self) -> None:
        import winpty  # 遅延 import (非 Windows でも module import 可能に)
        self._pty = winpty.PtyProcess.spawn(self.argv, dimensions=(self.rows, self.cols))
        self._eof = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """blocking read を専用スレッドに隔離 (spike Task7: 子終了後ハングの根治)."""
        try:
            while True:
                data = self._pty.read(4096)   # blocking
                if not data:
                    break
                self._buf.append(data)        # deque.append は GIL アトミック
        except (EOFError, OSError):
            pass
        finally:
            self._eof = True

    def isalive(self) -> bool:
        return bool(self._pty and self._pty.isalive() and not self._eof)

    def read(self, size: int = 65536) -> str:
        """non-blocking: reader thread が溜めた分を返す (無ければ空文字)."""
        parts = []
        while self._buf:
            parts.append(self._buf.popleft())
        return "".join(parts)

    def send_text(self, text: str) -> None:
        payload = build_paste_payload(text)
        if payload:
            self._pty.write(payload)

    def write_raw(self, data: str) -> None:
        self._pty.write(data)

    def read_until(self, marker: str, *, timeout: float) -> str:
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.read(4096)
            if chunk:
                buf += chunk
                if marker in buf:
                    return buf
            else:
                time.sleep(0.02)
        return buf

    def resize(self, rows: int, cols: int) -> None:
        if self._pty:
            self._pty.setwinsize(rows, cols)

    def close(self) -> None:
        if self._pty and self._pty.isalive():
            self._pty.terminate()
        self._pty = None   # reader は daemon thread なので放置で安全に死ぬ
