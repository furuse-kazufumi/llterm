# SPDX-License-Identifier: Apache-2.0
"""GUI (L3) のヘッドレス回帰テスト — offscreen + 仮想 claude で課金ゼロ・実画面なし。

conftest が QT_QPA_PLATFORM=offscreen を立てる。PySide6 未導入環境では skip。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI テストは PySide6 が要る (pip install PySide6)")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from llterm.gui.app import MainWindow  # noqa: E402
from llterm.gui.virtual import VirtualClaudeRunner  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app  # offscreen なので teardown 不要 (プロセス終了で破棄)


def _make_window(tmp_path: Path, **loop_kw: object) -> MainWindow:
    return MainWindow(
        workdir=tmp_path,
        runner_factory=lambda: VirtualClaudeRunner(delay=0.0),
        **loop_kw,
    )


def _run_until_finished(qapp: QtWidgets.QApplication, win: MainWindow, timeout_ms: int = 15000) -> None:
    """worker スレッドが終わるまでイベントループを回す (offscreen, 競合に耐性)。"""
    assert win.worker is not None
    loop = QtCore.QEventLoop()
    win.worker.finished.connect(loop.quit)  # QThread 標準シグナル (必ず発火)
    guard = QtCore.QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)
    if not win.worker.isFinished():
        loop.exec()
    win.worker.wait(2000)
    qapp.processEvents()  # 残った queued slot (on_event / on_finished) を流し切る


# ─── 描画スロット (スレッドなし・直接呼び出し) ─────────────────────


def test_render_slots_update_widgets(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._on_event("session_start", {"session_id": "abcdef123456", "session_index": 1})
    win._on_event("turn", {"turn": 1, "used_pct": 0.42, "total_cost": 0.06,
                           "text": "hello from virtual", "error_kind": ""})
    assert "session: #1" in win.lbl_session.text()
    assert win.ctx_bar.value() == 42
    assert "0.0600" in win.lbl_cost.text()
    assert "hello from virtual" in win.output.toPlainText()


def test_render_auth_required_message(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._on_finished({"stop_reason": "auth_required", "sessions": 0, "turns": 1, "total_cost": 0.0})
    assert "done: auth_required" in win.lbl_state.text()
    assert "再ログイン" in win.output.toPlainText()


def test_ctx_bar_clamped_over_100(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._on_event("turn", {"turn": 1, "used_pct": 1.5, "total_cost": 0.0, "text": "", "error_kind": ""})
    assert win.ctx_bar.value() == 100


# ─── end-to-end: 仮想 claude でループを実駆動 (QThread + offscreen) ──


def test_loop_runs_end_to_end_with_virtual_claude(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = _make_window(tmp_path, window_tokens=200_000, threshold=0.70, max_sessions=2)
    win.start_loop()
    _run_until_finished(qapp, win)
    text = win.output.toPlainText()
    assert "loop 開始" in text
    assert "session #1 開始" in text
    assert "rotate" in text                      # 70% 超で rotate した
    assert "stopped: max_sessions" in text       # 2 セッションで停止
    assert "[virtual claude]" in text            # 仮想 claude の出力が描画された
    assert win.btn_start.isEnabled()             # 終了後 Start が再び有効
    assert not win.btn_stop.isEnabled()


def test_stop_button_halts_loop(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    # delay を入れて、走行中に Stop を要求 → 早期 stopped で終わる
    win = MainWindow(
        workdir=tmp_path,
        runner_factory=lambda: VirtualClaudeRunner(delay=0.05),
        max_sessions=100,  # 放置すれば長時間 → Stop が効くことの検証
    )
    win.start_loop()
    QtCore.QTimer.singleShot(120, win.stop_loop)
    _run_until_finished(qapp, win, timeout_ms=8000)
    assert "stopped: stopped" in win.output.toPlainText()
    assert win.btn_start.isEnabled()


def test_auth_stops_loop_end_to_end(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(
        workdir=tmp_path,
        runner_factory=lambda: VirtualClaudeRunner(delay=0.0, auth_after=1),
        max_sessions=5,
    )
    win.start_loop()
    _run_until_finished(qapp, win)
    assert "done: auth_required" in win.lbl_state.text()
