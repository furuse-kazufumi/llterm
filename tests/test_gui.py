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


def test_close_event_requests_worker_stop(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.02), max_sessions=100)
    win.start_loop()
    w = win.worker
    assert w is not None
    win.close()                  # closeEvent → ループに停止要求
    assert w._stop.is_set()      # 閉じる操作が loop の停止を要求した
    w.wait(3000)


# ─── 描画スロット (スレッドなし・直接呼び出し) ─────────────────────


def test_stream_slot_renders_realtime_events(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """応答・ツール実行がターン完了を待たず逐次描画される (本バグ修正の中核)。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "init", "model": "claude-fable-5", "session_id": "abcdef99"})
    win._on_stream({"kind": "text", "text": "リアルタイム応答です"})
    win._on_stream({"kind": "tool_use", "name": "Bash", "detail": "echo hi"})
    win._on_stream({"kind": "tool_result", "is_error": False, "preview": "hi"})
    win._on_stream({"kind": "tool_result", "is_error": True, "preview": "boom"})
    text = win.output.toPlainText()
    assert "model=claude-fable-5" in text
    assert "リアルタイム応答です" in text
    assert "⚙ Bash: echo hi" in text
    assert "↳ hi" in text
    assert "エラー: boom" in text


def test_stream_then_turn_does_not_duplicate_text(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """ストリーム表示済みの応答は turn 完了時に再表示しない (二重表示防止)。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "text", "text": "ユニークな応答XYZ"})
    win._on_event("turn", {"turn": 1, "used_pct": 0.1, "total_cost": 0.0,
                           "text": "ユニークな応答XYZ", "error_kind": ""})
    assert win.output.toPlainText().count("ユニークな応答XYZ") == 1
    # 次の turn はストリームが無ければ通常どおり text を表示する (カウンタがリセットされる)
    win._on_event("turn", {"turn": 2, "used_pct": 0.1, "total_cost": 0.0,
                           "text": "二回目の応答ABC", "error_kind": ""})
    assert "二回目の応答ABC" in win.output.toPlainText()


def test_output_view_uses_colored_dark_style(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """カラー表示: ダーク背景スタイル + イベント種別ごとの色つき描画。"""
    win = _make_window(tmp_path)
    assert "background-color" in win.output.styleSheet()
    win._on_event("session_start", {"session_id": "abcdef123456", "session_index": 1})
    # toHtml() のシリアライズ表現 (hex/rgb) は Qt 内部仕様のため、文字フォーマットを直接検証する
    cursor = win.output.document().find("session 1")
    assert not cursor.isNull()
    assert cursor.charFormat().foreground().color().name() == "#e5c07b"  # PALETTE["session"]


def test_append_normalizes_carriage_returns(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """CRLF/CR 入りテキストが二重改行にならない (Qt は残留 \\r も改行扱いするため)。"""
    win = _make_window(tmp_path)
    win._append("a\r\nb\rc")
    assert "a\nb\nc" in win.output.toPlainText()


def test_error_turn_text_shown_even_after_stream(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """エラーターンの詳細テキストは、前置きがストリーム済みでも握り潰さず表示する。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "text", "text": "前置きテキスト"})
    win._on_event("turn", {"turn": 1, "used_pct": 0.1, "total_cost": 0.0,
                           "text": "API error: rate exceeded", "error_kind": "other"})
    assert "API error: rate exceeded" in win.output.toPlainText()


def test_subagent_stream_is_labeled_and_not_counted(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """サブエージェント出力は ⤷ 付き区別表示で、メイン応答の二重表示判定に数えない。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "text", "text": "サブ応答", "subagent": True})
    assert "⤷ サブ応答" in win.output.toPlainText()
    win._on_event("turn", {"turn": 1, "used_pct": 0.1, "total_cost": 0.0,
                           "text": "メイン最終応答", "error_kind": ""})
    assert "メイン最終応答" in win.output.toPlainText()  # サブのみストリーム → メイン text は出す


def test_rate_limit_warning_rendered(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """レート制限 (サブスク自走の主制約) は GUI に必ず可視化、allowed はノイズにしない。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "rate_limit", "status": "rejected", "resets_at": 0})
    assert "レート制限: rejected" in win.output.toPlainText()
    win._on_stream({"kind": "rate_limit", "status": "allowed", "resets_at": 0})
    assert win.output.toPlainText().count("レート制限") == 1


def test_worker_preserves_existing_on_stream(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    from llterm.gui.worker import LoopWorker

    def sentinel(item: dict) -> None:
        pass

    runner = VirtualClaudeRunner(delay=0.0, on_stream=sentinel)
    LoopWorker(runner=runner, workdir=tmp_path, ledger_path=tmp_path / "l.jsonl",
               loop_kw={"max_sessions": 1})
    assert runner.on_stream is sentinel  # 呼び出し側のコールバックを黙って上書きしない


def test_worker_accepts_runner_without_on_stream(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """on_stream を持たない素の TurnRunner でも worker は購読なしで正常動作する (後方互換)。"""
    from llterm.gui.worker import LoopWorker
    from llterm.host.loop import TurnResult

    class BareRunner:
        def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
            return TurnResult(session_id, 150_000, 1, 150_000, 0.0, "bare", False, "", 1, 0)

        def cancel(self) -> None:
            pass

    w = LoopWorker(runner=BareRunner(), workdir=tmp_path, ledger_path=tmp_path / "l.jsonl",
                   loop_kw={"max_sessions": 1})
    w.start()
    assert w.wait(10000)
    assert w.isFinished()


def test_injected_task_shown_at_consumption(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """注入タスクが実際に実行される瞬間 (task イベント injected=True) に画面表示する。"""
    win = _make_window(tmp_path)
    win._on_event("task", {"session_index": 1, "turn": 2, "injected": True,
                           "prompt": "テスト用の割り込みタスク"})
    assert "▶ 注入タスク実行: テスト用の割り込みタスク" in win.output.toPlainText()
    # 通常 (非注入) の task は本文を垂れ流さない (ノイズ防止)
    before = win.output.toPlainText()
    win._on_event("task", {"session_index": 1, "turn": 3, "injected": False,
                           "prompt": "長い再開プロンプト" * 50})
    assert win.output.toPlainText() == before


def test_status_shows_session_progress_over_max(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """セッション進捗が session N/max 形式で見える。"""
    win = _make_window(tmp_path)
    win._max_sessions = 8
    win._on_event("session_start", {"session_id": "abcdef123456", "session_index": 3})
    assert "session 3/8" in win.lbl_session.text()
    win._on_event("turn", {"turn": 5, "session_index": 3, "used_pct": 0.4, "total_cost": 0.0,
                           "text": "", "error_kind": ""})
    assert "session 3/8" in win.lbl_session.text()
    assert "turn 5" in win.lbl_session.text()


def test_ctx_bar_shows_rotate_threshold(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path, threshold=0.65)
    assert "rotate 65%" in win.ctx_bar.format()


def test_render_slots_update_widgets(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    win._on_event("session_start", {"session_id": "abcdef123456", "session_index": 1})
    win._on_event("turn", {"turn": 1, "session_index": 1, "used_pct": 0.42, "total_cost": 0.06,
                           "text": "hello from virtual", "error_kind": ""})
    assert "session 1" in win.lbl_session.text()
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


# ─── 設定永続化 (最後の設定を次回起動時に復元) ─────────────────────


def test_settings_persist_across_windows(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """前回の設定 (プロジェクト/トグル/閾値/テンプレ/引数) が次回起動時に復元される。"""
    sp = tmp_path / "s.json"
    proj = tmp_path / "projx"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x", encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=proj, settings_path=sp)
    win.chk_real.setChecked(True)
    win.chk_autonomy.setChecked(True)
    win.spin_threshold.setValue(0.55)
    win.spin_window.setValue(150_000)
    win.spin_sessions.setValue(3)
    win.cmb_template.setCurrentIndex(win.cmb_template.findData("rad_expand"))
    win.edit_param.setText("robotics")
    win._save_settings()

    win2 = MainWindow(projects_root=tmp_path, settings_path=sp)  # 全て未指定 → 保存値で復元
    assert win2._selected_workdir() == proj
    assert win2.chk_real.isChecked() is True
    assert win2.chk_autonomy.isChecked() is True
    assert win2.spin_threshold.value() == pytest.approx(0.55)
    assert win2.spin_window.value() == 150_000
    assert win2.spin_sessions.value() == 3
    assert win2.cmb_template.currentData() == "rad_expand"
    assert win2.edit_param.text() == "robotics"


def test_effort_default_is_max_and_feeds_real_runner(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """既定 effort は max (ユーザー方針)、実 claude runner に --effort として渡る。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    assert win.cmb_effort.currentData() == "max"
    win.chk_real.setChecked(True)
    runner = win._build_runner()
    assert runner.effort == "max"


def test_effort_persists_and_restores(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    win.cmb_effort.setCurrentIndex(win.cmb_effort.findData("high"))
    win._save_settings()
    win2 = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win2.cmb_effort.currentData() == "high"


def test_effort_cli_overrides_saved(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, {"effort": "low"})
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp, effort_default="xhigh")
    assert win.cmb_effort.currentData() == "xhigh"  # CLI 明示指定が保存値に勝つ


def test_explicit_args_override_saved_settings(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """優先順位: CLI 明示指定 > 保存値 > 組込み既定。"""
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, {"template": "rad_expand", "threshold": 0.50, "real": True})
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp,
                     template_default="general", threshold=0.90)
    assert win.cmb_template.currentData() == "general"          # 明示指定が勝つ
    assert win.spin_threshold.value() == pytest.approx(0.90)    # 明示指定が勝つ
    assert win.chk_real.isChecked() is True                     # 未指定のフラグは保存値


def test_broken_settings_file_falls_back_to_defaults(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    sp = tmp_path / "s.json"
    sp.write_text("{broken", encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win.spin_threshold.value() == pytest.approx(0.70)  # fail-safe で既定値起動
    assert win.cmb_template.currentData() == "general"


@pytest.mark.parametrize("bad", [
    {"threshold": "0,70"},          # 欧州小数点の手編集
    {"threshold": "high"},          # 数値でない文字列
    {"threshold": [0.7]},           # 型不正 (list)
    {"window_tokens": "200,000"},   # カンマ区切り
    {"max_sessions": {"x": 1}},     # 型不正 (dict)
    {"max_cost": "free"},           # 数値でない文字列
    {"max_sessions": True},         # bool は数値扱いしない
])
def test_type_unsafe_settings_do_not_crash_gui(
    qapp: QtWidgets.QApplication, tmp_path: Path, bad: dict
) -> None:
    """手編集/外部破損で型不正な数値が入っても GUI 起動はクラッシュせず既定値に落ちる (major 修正)。"""
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, bad)
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)  # 例外を出さない
    assert win.spin_threshold.value() == pytest.approx(0.70)  # 既定にフォールバック
    assert win.spin_window.value() == 200_000


def test_valid_numeric_settings_still_restore(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, {"threshold": 0.55, "window_tokens": "150000", "max_sessions": 4})
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win.spin_threshold.value() == pytest.approx(0.55)
    assert win.spin_window.value() == 150_000       # 文字列 "150000" も復元される
    assert win.spin_sessions.value() == 4


def test_vanished_workdir_is_not_restored(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, {"workdir": str(tmp_path / "deleted-project")})
    win = MainWindow(projects_root=tmp_path, settings_path=sp)
    assert win._selected_workdir() != tmp_path / "deleted-project"  # 消えたパスは選択しない


def test_close_event_saves_settings(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    win.close()  # 終了時に保存される
    assert sp.exists()


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
    assert "⚙ VirtualTool" in text               # stream イベントが worker 経由で逐次描画された
    assert text.count("turn #1)") == 1           # ストリーム済み応答は turn 完了時に二重表示しない
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
