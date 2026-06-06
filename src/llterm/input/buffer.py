"""入力欄の純ロジック (コンソール非依存・テスト容易).

R11: 複数行ペーストを 1 入力として保持 (CRLF/CR は LF に正規化)
R12: newline() は改行挿入のみ。送信は take() を呼ぶ側 (key decoder) の責務
R13: move() はカーソル移動のみ。履歴は history_prev/next (Ctrl+矢印側が呼ぶ)
"""
from __future__ import annotations


class InputBuffer:
    def __init__(self, max_history: int = 200) -> None:
        self._lines: list[str] = [""]
        self._row = 0
        self._col = 0
        self._history: list[str] = []
        self._hist_idx: int | None = None   # None = 履歴非表示 (live 編集中)
        self._max_history = max_history

    # ---- 状態 ----
    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    @property
    def cursor(self) -> tuple[int, int]:
        return (self._row, self._col)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(self._lines)

    # ---- 編集 ----
    def insert(self, ch: str) -> None:
        line = self._lines[self._row]
        self._lines[self._row] = line[: self._col] + ch + line[self._col:]
        self._col += len(ch)
        self._hist_idx = None

    def newline(self) -> None:
        line = self._lines[self._row]
        head, tail = line[: self._col], line[self._col:]
        self._lines[self._row] = head
        self._lines.insert(self._row + 1, tail)
        self._row += 1
        self._col = 0
        self._hist_idx = None

    def paste(self, blob: str) -> None:
        normalized = blob.replace("\r\n", "\n").replace("\r", "\n")
        parts = normalized.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                self.newline()
            if part:
                self.insert(part)

    def backspace(self) -> None:
        if self._col > 0:
            line = self._lines[self._row]
            self._lines[self._row] = line[: self._col - 1] + line[self._col:]
            self._col -= 1
        elif self._row > 0:
            prev = self._lines[self._row - 1]
            self._col = len(prev)
            self._lines[self._row - 1] = prev + self._lines[self._row]
            del self._lines[self._row]
            self._row -= 1
        self._hist_idx = None

    def move(self, direction: str) -> None:
        if direction == "left" and self._col > 0:
            self._col -= 1
        elif direction == "right" and self._col < len(self._lines[self._row]):
            self._col += 1
        elif direction == "up" and self._row > 0:
            self._row -= 1
            self._col = min(self._col, len(self._lines[self._row]))
        elif direction == "down" and self._row < len(self._lines) - 1:
            self._row += 1
            self._col = min(self._col, len(self._lines[self._row]))

    # ---- 送信・履歴 ----
    def take(self) -> str:
        text = self.text
        self._lines = [""]
        self._row = self._col = 0
        self._hist_idx = None
        return text

    def push_history(self, text: str) -> None:
        if text.strip():
            self._history.append(text)
            del self._history[: -self._max_history]

    def _load(self, text: str) -> None:
        self._lines = text.split("\n") or [""]
        self._row = len(self._lines) - 1
        self._col = len(self._lines[-1])

    def history_prev(self) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self._history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        self._load(self._history[self._hist_idx])
        idx = self._hist_idx
        self._hist_idx = idx  # _load が None 化しないよう保持

    def history_next(self) -> None:
        if self._hist_idx is None:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._load(self._history[self._hist_idx])
            return
        self._hist_idx = None
        self._lines = [""]
        self._row = self._col = 0
