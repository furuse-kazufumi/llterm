# SPDX-License-Identifier: Apache-2.0
"""GUI とループエンジン (L2) をつなぐ QThread ワーカー。

SessionLoop.run() は blocking なので別スレッドで回す。進捗は ``on_event`` →
Qt シグナルへ marshalled され、メインスレッドのスロットが安全にウィジェットを更新する
(Qt のシグナルはスレッド境界を越えて queued connection で配送される)。
"""
from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path

from PySide6 import QtCore

from llterm.ctl.ledger import Ledger
from llterm.host.loop import SessionLoop, TurnRunner


class LoopWorker(QtCore.QThread):
    """SessionLoop を別スレッドで駆動し、イベントをシグナルで流す。"""

    event = QtCore.Signal(str, dict)  # (kind, data)
    finished_outcome = QtCore.Signal(dict)

    def __init__(
        self,
        *,
        runner: TurnRunner,
        workdir: Path,
        ledger_path: Path,
        loop_kw: dict,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._workdir = Path(workdir)
        self._ledger_path = Path(ledger_path)
        self._loop_kw = dict(loop_kw)
        self._stop = threading.Event()
        self._inject_lock = threading.Lock()
        self._injected: list[str] = []

    def request_stop(self) -> None:
        self._stop.set()

    def inject(self, text: str) -> None:
        with self._inject_lock:
            self._injected.append(text)

    def _next_prompt(self) -> str | None:
        with self._inject_lock:
            return self._injected.pop(0) if self._injected else None

    def run(self) -> None:  # QThread のエントリ (別スレッド)
        loop = SessionLoop(
            runner=self._runner,
            workdir=self._workdir,
            ledger=Ledger(self._ledger_path),
            on_event=lambda kind, data: self.event.emit(kind, data),
            should_stop=self._stop.is_set,
            next_prompt=self._next_prompt,
            **self._loop_kw,
        )
        outcome = loop.run()
        self.finished_outcome.emit(asdict(outcome))
