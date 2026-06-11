# SPDX-License-Identifier: Apache-2.0
"""llterm GUI (L3) — Claude Code 自走ループの窓口。

端末を捨てた GUI。出力ビュー (リングバッファ) / コンテキスト使用率バー / コスト /
セッション番号 / Start・Stop / タスク注入欄 (Ctrl+Enter 送信) を持つ。
既定は **仮想 claude** で駆動 (課金ゼロ・反復デバッグ用)。``--real`` で実 claude。
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from llterm.gui.virtual import VirtualClaudeRunner
from llterm.gui.worker import LoopWorker
from llterm.host.loop import TurnRunner, _ensure_utf8_stdout


class MainWindow(QtWidgets.QMainWindow):
    """ループ駆動の主ウィンドウ。L2 (SessionLoop) を QThread で回し進捗を描画する。"""

    def __init__(
        self,
        *,
        workdir: Path,
        runner_factory: Callable[[], TurnRunner],
        **loop_kw: object,
    ) -> None:
        super().__init__()
        self.workdir = Path(workdir)
        self.runner_factory = runner_factory
        self.loop_kw = dict(loop_kw)
        self.worker: LoopWorker | None = None
        self._build_ui()

    # ---- UI 構築 ----
    def _build_ui(self) -> None:
        self.setWindowTitle("llterm — Claude Code 自走ループ (GUI)")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        row = QtWidgets.QHBoxLayout()
        self.lbl_state = QtWidgets.QLabel("idle")
        self.lbl_session = QtWidgets.QLabel("session: -")
        self.ctx_bar = QtWidgets.QProgressBar()
        self.ctx_bar.setRange(0, 100)
        self.ctx_bar.setFormat("ctx %p%")
        self.lbl_cost = QtWidgets.QLabel("cost: $0.0000")
        row.addWidget(self.lbl_state)
        row.addWidget(self.lbl_session)
        row.addWidget(self.ctx_bar, 1)
        row.addWidget(self.lbl_cost)
        vbox.addLayout(row)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)  # リングバッファ: 長時間でもメモリが膨張しない (R3)
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        self.output.setFont(mono)
        vbox.addWidget(self.output, 1)

        self.input = QtWidgets.QPlainTextEdit()
        self.input.setPlaceholderText("タスク注入 / 指示 (Ctrl+Enter で送信)")
        self.input.setMaximumHeight(90)
        vbox.addWidget(self.input)

        btnrow = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_send = QtWidgets.QPushButton("Send (Ctrl+Enter)")
        btnrow.addWidget(self.btn_start)
        btnrow.addWidget(self.btn_stop)
        btnrow.addStretch(1)
        btnrow.addWidget(self.btn_send)
        vbox.addLayout(btnrow)

        self.resize(900, 620)
        self.btn_start.clicked.connect(self.start_loop)
        self.btn_stop.clicked.connect(self.stop_loop)
        self.btn_send.clicked.connect(self.send_input)
        # Enter=改行のまま / 送信は Ctrl+Enter (R12: 誤送信の構造的防止)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.input, self.send_input)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Enter"), self.input, self.send_input)

    def _append(self, text: str) -> None:
        self.output.appendPlainText(text)

    # ---- 操作 ----
    @QtCore.Slot()
    def start_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        runner = self.runner_factory()
        ledger_path = self.workdir / ".llterm" / "loop_ledger.jsonl"
        self.worker = LoopWorker(
            runner=runner, workdir=self.workdir, ledger_path=ledger_path, loop_kw=self.loop_kw,
        )
        self.worker.event.connect(self._on_event)
        self.worker.finished_outcome.connect(self._on_finished)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_state.setText("running")
        self._append("=== loop 開始 ===")

    @QtCore.Slot()
    def stop_loop(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self.lbl_state.setText("stopping…")

    @QtCore.Slot()
    def send_input(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._append(f">> [注入] {text}")
        if self.worker is not None and self.worker.isRunning():
            self.worker.inject(text)
        else:
            self._append("  (loop 未起動: Start 後に反映されます)")

    # ---- ワーカーからのイベント (メインスレッドで実行) ----
    @QtCore.Slot(str, dict)
    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "session_start":
            sid = str(data.get("session_id", ""))[:8]
            self.lbl_session.setText(f"session: #{data.get('session_index')} ({sid})")
            self._append(f"\n--- session #{data.get('session_index')} 開始 ---")
        elif kind == "turn":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self.ctx_bar.setValue(min(pct, 100))
            self.lbl_cost.setText(f"cost: ${float(data.get('total_cost', 0.0)):.4f}")
            err = data.get("error_kind")
            head = f"[turn {data.get('turn')}] ctx {pct}%" + (f"  ERR={err}" if err else "")
            self._append(f"{head}\n{data.get('text') or ''}")
        elif kind == "rotate":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self._append(f"--- rotate (ctx {pct}%) → exit準備 & 新セッションへ ---")
        elif kind == "stopped":
            self._append(
                f"\n=== stopped: {data.get('stop_reason')} "
                f"(sessions={data.get('sessions')}, turns={data.get('turns')}, "
                f"cost=${float(data.get('total_cost', 0.0)):.4f}) ==="
            )

    @QtCore.Slot(dict)
    def _on_finished(self, outcome: dict) -> None:
        reason = outcome.get("stop_reason")
        self.lbl_state.setText(f"done: {reason}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if reason == "auth_required":
            self._append("⚠ 再ログインが必要です。認証後に Start で再開してください "
                         "(構造的に唯一の人間介在点)。")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="llterm-gui",
        description="llterm GUI: Claude Code 自走ループの窓口 (端末非依存)。既定は仮想 claude (課金ゼロ)。",
    )
    parser.add_argument("--workdir", default=".", help="claude を起動する対象プロジェクト")
    parser.add_argument("--real", action="store_true",
                        help="実 claude を使う (既定は仮想 claude = 課金ゼロのデバッグ)")
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--window-tokens", type=int, default=200_000)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--max-cost", type=float, default=None)
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    runner_factory: Callable[[], TurnRunner]
    if args.real:
        from llterm.host.loop import ClaudeRunner

        if args.max_sessions is None and args.max_cost is None:
            print("error: --real は --max-sessions か --max-cost が必要 (課金保護)", file=sys.stderr)
            return 2
        runner_factory = ClaudeRunner
    else:
        runner_factory = VirtualClaudeRunner

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(
        workdir=workdir,
        runner_factory=runner_factory,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=args.max_sessions,
        max_total_cost_usd=args.max_cost,
    )
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
