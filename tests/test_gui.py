# SPDX-License-Identifier: Apache-2.0
"""GUI (L3) のヘッドレス回帰テスト — offscreen + 仮想 claude で課金ゼロ・実画面なし。

conftest が QT_QPA_PLATFORM=offscreen を立てる。PySide6 未導入環境では skip。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI テストは PySide6 が要る (pip install PySide6)")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from llterm import rad, templates  # noqa: E402
from llterm.gui.app import MainWindow, discover_projects  # noqa: E402
from llterm.gui.virtual import VirtualClaudeRunner  # noqa: E402
from llterm.host.loop import ClaudeRunner  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app  # offscreen なので teardown 不要 (プロセス終了で破棄)


def _make_window(tmp_path: Path, **loop_kw: object) -> MainWindow:
    return MainWindow(
        projects_root=tmp_path,
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


# ─── プロジェクト探索 / コンボボックス ───────────────────────────


def test_discover_projects_filters_by_marker(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / ".git").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "gamma").mkdir()  # マーカー無し → 除外
    (tmp_path / ".hidden").mkdir()  # 隠し → 除外
    names = {name for name, _ in discover_projects(tmp_path)}
    assert names == {"alpha", "beta"}


def test_discover_projects_missing_root_is_empty(tmp_path: Path) -> None:
    assert discover_projects(tmp_path / "nope") == []


def test_combobox_lists_projects_and_selects_workdir(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    (tmp_path / "proj1").mkdir()
    (tmp_path / "proj1" / "CLAUDE.md").write_text("x", encoding="utf-8")
    sub = tmp_path / "proj1"
    win = MainWindow(projects_root=tmp_path, workdir=sub)
    assert win.cmb_project.count() >= 1
    assert win._selected_workdir() == sub


# ─── 実行モード切替 (実 claude=サブスク / 仮想) ────────────────────


def test_build_runner_real_uses_subscription_claude(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path)  # override なし
    win.chk_real.setChecked(True)
    runner = win._build_runner()
    assert isinstance(runner, ClaudeRunner)
    assert runner.use_subscription is True  # サブスク認証 (API キーを外す)
    win.chk_real.setChecked(False)
    assert isinstance(win._build_runner(), VirtualClaudeRunner)


def test_real_default_checks_the_box(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, real_default=True)
    assert win.chk_real.isChecked() is True


def test_rad_default_off_and_on(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    assert MainWindow(projects_root=tmp_path, workdir=tmp_path).chk_rad.isChecked() is False
    assert MainWindow(projects_root=tmp_path, workdir=tmp_path, rad_default=True).chk_rad.isChecked() is True


def test_rad_checkbox_adds_hint_to_loop_kw(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(
        projects_root=tmp_path, workdir=tmp_path,
        runner_factory=lambda: VirtualClaudeRunner(delay=0.0), max_sessions=1,
    )
    win.chk_rad.setChecked(True)
    win.start_loop()
    assert win.worker is not None
    assert win.worker._loop_kw.get("rad_hint")  # RAD ヒントが loop に渡る
    _run_until_finished(qapp, win)


# ─── CLI 引数 ↔ GUI コントロールの同等性 (threshold / window / max-cost) ──


def test_settings_widgets_init_from_loop_kw(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     threshold=0.6, window_tokens=120_000, max_total_cost_usd=5.0)
    assert win.spin_threshold.value() == pytest.approx(0.6)
    assert win.spin_window.value() == 120_000
    assert win.spin_maxcost.value() == pytest.approx(5.0)


def test_settings_widgets_feed_loop_kw(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.0), max_sessions=1)
    win.spin_threshold.setValue(0.55)
    win.spin_window.setValue(150_000)
    win.spin_maxcost.setValue(2.5)
    win.start_loop()
    assert win.worker is not None
    lk = win.worker._loop_kw
    assert lk["threshold"] == pytest.approx(0.55)
    assert lk["window_tokens"] == 150_000
    assert lk["max_total_cost_usd"] == pytest.approx(2.5)
    _run_until_finished(qapp, win)


def test_maxcost_zero_means_unlimited(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.0), max_sessions=1)
    win.spin_maxcost.setValue(0.0)
    win.start_loop()
    assert win.worker is not None
    assert win.worker._loop_kw["max_total_cost_usd"] is None  # 0 → 無制限 (None)
    _run_until_finished(qapp, win)


# ─── テンプレ選択 (機能ごと) + 用途ツールチップ + RAD 公開ゲート ──


def test_template_combobox_lists_all_with_tooltips(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path)
    assert win.cmb_template.count() == len(templates.TEMPLATES)
    for i in range(win.cmb_template.count()):
        tip = win.cmb_template.itemData(i, QtCore.Qt.ItemDataRole.ToolTipRole)
        assert tip  # 各テンプレに用途ツールチップ(description)が入る


def test_template_default_and_param_enable(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, template_default="rad_expand")
    assert win.cmb_template.currentData() == "rad_expand"
    assert win.edit_param.isEnabled() is True             # rad_expand は引数(分野名)が要る
    assert win.cmb_template.toolTip()                      # 選択中テンプレの用途が tooltip に
    win.cmb_template.setCurrentIndex(win.cmb_template.findData("general"))
    assert win.edit_param.isEnabled() is False            # general は引数不要


def test_template_feeds_resume_prompt(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.0), max_sessions=1,
                     template_default="rad_expand")
    win.edit_param.setText("robotics")
    win.start_loop()
    assert win.worker is not None
    rp = win.worker._loop_kw.get("resume_prompt", "")
    assert "robotics" in rp and "staging" in rp            # テンプレ prompt が loop に渡る
    _run_until_finished(qapp, win)


def test_promote_via_gui_moves_staging_to_live(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, rad_docs_root=tmp_path)  # 実 D:/docs に触れない
    stg = rad.staging_dir("robotics", tmp_path)
    stg.mkdir(parents=True)
    (stg / "INDEX.md").write_text("x", encoding="utf-8")
    win.edit_param.setText("robotics")
    win._do_promote("robotics")  # 確認ダイアログを介さず実処理 (ゲートの中身)
    assert (rad.live_dir("robotics", tmp_path) / "INDEX.md").exists()
    assert "公開:" in win.output.toPlainText()


def test_promote_via_gui_reports_error_without_staging(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, rad_docs_root=tmp_path)
    win._do_promote("nope")
    assert "公開失敗" in win.output.toPlainText()  # staging 無し → fail-closed


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
    assert "loop 開始 [仮想claude]" in text
    assert "session #1 開始" in text
    assert "rotate" in text                      # 70% 超で rotate した
    assert "stopped: max_sessions" in text       # 2 セッションで停止
    assert "[virtual claude]" in text            # 仮想 claude の出力が描画された
    assert win.btn_start.isEnabled()             # 終了後 Start が再び有効
    assert not win.btn_stop.isEnabled()
    assert win.cmb_project.isEnabled()           # 終了後コンボボックス再有効


def test_stop_button_halts_loop(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(
        projects_root=tmp_path,
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
        projects_root=tmp_path,
        workdir=tmp_path,
        runner_factory=lambda: VirtualClaudeRunner(delay=0.0, auth_after=1),
        max_sessions=5,
    )
    win.start_loop()
    _run_until_finished(qapp, win)
    assert "done: auth_required" in win.lbl_state.text()
