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
from llterm.gui.app import PALETTE, MainWindow, discover_projects  # noqa: E402
from llterm.gui.virtual import VirtualClaudeRunner  # noqa: E402
from llterm.host.loop import ClaudeRunner  # noqa: E402

PALETTE_ERR = PALETTE["err"]


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


def test_close_event_confirms_then_graceful_stops(
    qapp: QtWidgets.QApplication, tmp_path: Path, monkeypatch
) -> None:
    """× 終了は確認ダイアログ → 「はい」で graceful 停止 (記録) を要求し、完了後に閉じる。"""
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.02), max_sessions=100)
    win.start_loop()
    w = win.worker
    assert w is not None
    win.close()                       # closeEvent → 確認 (Yes) → graceful 停止
    assert w._stop.is_set()           # 停止要求された
    assert win._closing_after_stop    # 記録完了後に閉じる予約
    w.wait(3000)
    qapp.processEvents()              # _on_finished → 砂時計解除 + 予約 close
    assert win._busy_cursor is False  # カーソルは復元済み


def test_close_event_cancel_keeps_window(
    qapp: QtWidgets.QApplication, tmp_path: Path, monkeypatch
) -> None:
    """× 終了で「いいえ」を選ぶと閉じず停止もしない (安全側)。"""
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.02), max_sessions=100)
    win.start_loop()
    w = win.worker
    assert w is not None
    win.close()
    assert not w._stop.is_set()       # 停止していない
    assert not win._closing_after_stop
    win.stop_loop()                   # 後始末
    win.stop_loop()                   # force
    w.wait(3000)


def test_stop_is_graceful_then_force(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """1 回目 Stop = graceful (kill しない・砂時計)、2 回目 = force (即 kill)。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path,
                     runner_factory=lambda: VirtualClaudeRunner(delay=0.05), max_sessions=100)
    win.start_loop()
    win.stop_loop()  # 1 回目
    assert win._stopping is True
    assert win._busy_cursor is True
    assert win.btn_stop.text() == "強制停止"
    assert win.worker._stop.is_set()
    win.stop_loop()  # 2 回目 = force
    win.worker.wait(3000)
    qapp.processEvents()
    assert win._busy_cursor is False  # 終了で砂時計解除


def test_handoff_event_sets_busy_cursor_and_status(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = _make_window(tmp_path)
    win._on_event("handoff", {"session_id": "abcd1234"})
    assert win._busy_cursor is True
    assert "記録中" in win.lbl_state.text()
    assert "記録中" in win.output.toPlainText()
    win._set_busy_cursor(False)  # 後始末


def test_codex_fallback_toggle_persists(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win.chk_codex_fallback.isChecked() is False  # 既定 OFF (安全側)
    win.chk_codex_fallback.setChecked(True)
    win._save_settings()
    win2 = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win2.chk_codex_fallback.isChecked() is True


def test_provider_switch_event_updates_model_label(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = _make_window(tmp_path)
    win._on_event("provider_switch", {"provider": "codex", "index": 1})
    assert "codex" in win.lbl_model.text()
    assert "プロバイダ切替 → codex" in win.output.toPlainText()


def test_window_icon_is_set(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """タイトルバー用アイコンが設定される (assets が見つかる環境)。"""
    from llterm.gui.app import find_app_icon

    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    if find_app_icon() is not None:  # assets がある環境のみ厳密検証
        assert not win.windowIcon().isNull()


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
    # 通常 (非注入) の task はコンパクトな指令送信マーカーのみ — 長い本文は垂れ流さない
    win._on_event("task", {"session_index": 1, "turn": 3, "injected": False,
                           "prompt": "長い再開プロンプト" * 50})
    after = win.output.toPlainText()
    assert "▶ 指令送信 (turn 3)" in after
    assert ("長い再開プロンプト" * 50) not in after


def test_output_lines_include_timestamps(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """指令時 (task) と応答受信時 (turn) と境界に [HH:MM:SS] が出る (要望: 時刻表示)。"""
    import re

    win = _make_window(tmp_path)
    win._on_event("session_start", {"session_id": "abcd1234", "session_index": 1})
    win._on_event("task", {"session_index": 1, "turn": 1, "injected": False, "prompt": "x"})
    win._on_event("turn", {"turn": 1, "session_index": 1, "used_pct": 0.4, "total_cost": 0.0,
                           "text": "resp", "error_kind": ""})
    text = win.output.toPlainText()
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\] ▶ 指令送信", text)        # 指令時刻
    assert re.search(r"\[\d{2}:\d{2}:\d{2}\] \[turn 1\] 応答受信", text)  # 応答受信時刻


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


def test_progress_bar_shows_latest_response_oneline(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """完全自律時の進捗を下部バーに直近応答の 1 行要約で表示する。"""
    win = _make_window(tmp_path)
    win._on_stream({"kind": "text", "text": "認証モジュールのテストを追加中\n(詳細は続く)"})
    assert "認証モジュールのテストを追加中" in win.lbl_progress.text()
    assert win.lbl_progress.text().startswith("進捗:")
    # 長文は 1 行に切り詰め、全文はツールチップへ
    long = "あ" * 300
    win._on_stream({"kind": "text", "text": long})
    assert len(win.lbl_progress.text()) < 200
    assert win.lbl_progress.toolTip().startswith("あ")


def test_summary_panel_shows_full_text_and_is_selectable(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """進捗サマリ パネルは SESSION_SUMMARY.md 全文を表示し、読取専用だが選択コピー可能。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    body = "# 認証リファクタ\n" + "\n".join(f"- 項目{i}" for i in range(30))
    (docs / "SESSION_SUMMARY.md").write_text(body, encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    # 初期表示で既存サマリを全文ロード (2 行だけでなく全文)
    assert "項目0" in win.summary_view.toPlainText()
    assert "項目29" in win.summary_view.toPlainText()
    assert win.summary_view.isReadOnly()
    # 読取専用でもテキスト選択が可能 (単語をタスク注入へコピーできる)
    flags = win.summary_view.textInteractionFlags()
    assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse


def test_summary_panel_refresh_button_rereads(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    summ = docs / "SESSION_SUMMARY.md"
    summ.write_text("初版サマリ", encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    assert "初版サマリ" in win.summary_view.toPlainText()
    summ.write_text("更新後サマリ ABC", encoding="utf-8")  # claude が書き換えた想定
    win._refresh_summary()  # ↻ 更新 ボタン相当
    assert "更新後サマリ ABC" in win.summary_view.toPlainText()


def test_summary_panel_empty_when_no_file(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    assert win.summary_view.toPlainText() == ""  # ファイル無し → 空 (placeholder 表示)


def test_progress_bar_reads_session_summary_on_rotate(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """rotate 時に docs/SESSION_SUMMARY.md の先頭を handoff 進捗として表示する。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "SESSION_SUMMARY.md").write_text("# 認証リファクタ\n残: テスト3件とドキュメント更新\n",
                                             encoding="utf-8")
    win = _make_window(tmp_path)
    win._run_workdir = tmp_path
    win._on_event("rotate", {"session_index": 1, "used_pct": 0.7, "session_turns": 5})
    assert "進捗(handoff)" in win.lbl_progress.text()
    assert "認証リファクタ" in win.lbl_progress.text()


def test_progress_bar_handles_missing_session_summary(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    win = _make_window(tmp_path)
    win._run_workdir = tmp_path  # docs/SESSION_SUMMARY.md なし
    win._on_event("rotate", {"session_index": 1, "used_pct": 0.7, "session_turns": 5})  # 例外を出さない
    assert win._read_session_summary() == ""


def test_model_label_updates_from_init_stream(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """画面に現在のモデルを表示する (init イベントの model)。effort 併記。"""
    win = _make_window(tmp_path)
    win._run_effort = "max"
    win._on_stream({"kind": "init", "model": "claude-fable-5", "session_id": "abcd1234"})
    assert "model: claude-fable-5" in win.lbl_model.text()
    assert "effort=max" in win.lbl_model.text()


def test_subscription_cost_labeled_no_charge(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """サブスク (実 claude) は『課金なし』と明示し、赤字警告は出さない。"""
    from llterm.host.loop import ClaudeRunner

    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    suffix, billed = win._cost_label_mode(ClaudeRunner(use_subscription=True))
    assert suffix == "報告値・課金なし"
    assert billed is False
    win._cost_suffix, win._cost_billed = suffix, billed
    win._set_cost(40.9024)
    assert "課金なし" in win.lbl_cost.text()
    assert "40.9024" in win.lbl_cost.text()
    assert "color" not in win.lbl_cost.styleSheet()  # 警告色なし


def test_virtual_cost_labeled_no_charge(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    win = _make_window(tmp_path)
    suffix, billed = win._cost_label_mode(VirtualClaudeRunner())
    assert suffix == "仮想・課金なし"
    assert billed is False


def test_api_key_cost_labeled_billed_and_red(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """API キー認証 (use_subscription=False) は『実課金』で赤字警告。"""
    from llterm.host.loop import ClaudeRunner

    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    suffix, billed = win._cost_label_mode(ClaudeRunner(use_subscription=False))
    assert suffix == "実課金"
    assert billed is True
    win._cost_suffix, win._cost_billed = suffix, billed
    win._set_cost(40.9024)
    assert "実課金" in win.lbl_cost.text()
    assert PALETTE_ERR in win.lbl_cost.styleSheet()  # 警告色あり


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


def test_model_default_is_opus_and_feeds_real_runner(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """既定 model は Opus 4.8 (ユーザー方針 2026-06-13)、実 claude runner に --model として渡る。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    assert win.cmb_model.currentData() == "claude-opus-4-8"
    win.chk_real.setChecked(True)
    runner = win._build_runner()
    assert runner.model == "claude-opus-4-8"


def test_model_persists_and_restores(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """軽量モデルへ切替 (token 節約) が保存・復元される。"""
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    win.cmb_model.setCurrentIndex(win.cmb_model.findData("sonnet"))
    win._save_settings()
    win2 = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win2.cmb_model.currentData() == "sonnet"


def test_model_cli_overrides_saved(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    from llterm.gui import settings as gs

    sp = tmp_path / "s.json"
    gs.save_settings(sp, {"model": "haiku"})
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp, model_default="sonnet")
    assert win.cmb_model.currentData() == "sonnet"  # CLI 明示指定が保存値に勝つ


def test_model_empty_selects_claude_default(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """'' (claude既定) を選ぶと runner に --model を付けない (claude 保存既定に委ねる)。"""
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp, model_default="")
    assert win.cmb_model.currentData() == ""
    win.chk_real.setChecked(True)
    runner = win._build_runner()
    assert runner.model == ""


# ─── token 節約ルーティング (Codex 優先 / テンプレ別) ─────────────


def _provider_names(win) -> tuple[str, list[str]]:
    primary, fallbacks = win._resolve_providers()
    return type(primary).__name__, [type(f).__name__ for f in fallbacks]


def test_default_chain_is_claude_primary(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """既定 (general テンプレ・トグル OFF) は従来どおり Claude 主・fallback なし。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(True)
    primary, fallbacks = _provider_names(win)
    assert primary == "ClaudeRunner"
    assert fallbacks == []


def test_codex_fallback_toggle_appends_codex(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """Codex フォールバック ON: Claude 主 + Codex を保険に。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(True)
    win.chk_codex_fallback.setChecked(True)
    primary, fallbacks = _provider_names(win)
    assert primary == "ClaudeRunner"
    assert fallbacks == ["CodexRunner"]


def test_codex_first_toggle_makes_codex_primary(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """Codex 優先 ON: Codex 主・Claude を保険に (token をほぼ Codex へ寄せる)。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(True)
    win.chk_codex_first.setChecked(True)
    primary, fallbacks = _provider_names(win)
    assert primary == "CodexRunner"
    assert fallbacks == ["ClaudeRunner"]


def test_mechanical_template_auto_prefers_codex(
    qapp: QtWidgets.QApplication, tmp_path: Path
) -> None:
    """機械的テンプレ (green_keeper) はトグル OFF でも自動で Codex 主になる。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(True)
    win.cmb_template.setCurrentIndex(win.cmb_template.findData("green_keeper"))
    primary, fallbacks = _provider_names(win)
    assert primary == "CodexRunner"
    assert fallbacks == ["ClaudeRunner"]


def test_general_template_stays_claude(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """general テンプレは prefer なし → Claude 主のまま。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(True)
    win.cmb_template.setCurrentIndex(win.cmb_template.findData("general"))
    assert _provider_names(win)[0] == "ClaudeRunner"


def test_virtual_mode_never_uses_codex(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """仮想モードは Codex 優先 ON でも Codex を使わない (課金/サブスク不要のプレビュー)。"""
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=tmp_path / "s.json")
    win.chk_real.setChecked(False)
    win.chk_codex_first.setChecked(True)
    primary, fallbacks = _provider_names(win)
    assert primary == "VirtualClaudeRunner"
    assert fallbacks == []


def test_codex_first_persists(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    win.chk_codex_first.setChecked(True)
    win._save_settings()
    win2 = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win2.chk_codex_first.isChecked() is True


def test_mechanical_template_prefer_codex_metadata() -> None:
    """テンプレ registry の prefer メタデータ (ルーティングの根拠) を固定する。"""
    from llterm import templates as tmpl
    assert tmpl.get("green_keeper").prefer == "codex"
    assert tmpl.get("rad_expand").prefer == "codex"
    assert tmpl.get("doc_update").prefer == "codex"
    assert tmpl.get("security_audit").prefer == "codex"
    assert tmpl.get("general").prefer == ""


# ─── 進捗サマリ 構造化表示トグル ────────────────────────────────


def test_summary_digest_is_default(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """既定はダイジェスト (現在地/直近の成果/次の一手) を表示する。"""
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "docs" / "SESSION_SUMMARY.md").write_text(
        "## 現在地\nXを実装中\n## 次の一手\nYをやる\n", encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=proj, settings_path=tmp_path / "s.json")
    assert win.chk_summary_raw.isChecked() is False
    shown = win.summary_view.toPlainText()
    assert "【現在地】" in shown and "Xを実装中" in shown
    assert "## 現在地" not in shown  # 生の markdown 見出しは出さない


def test_summary_raw_toggle_shows_full(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    """『生』ON で SESSION_SUMMARY.md 全文 (生 markdown) を表示する。"""
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    raw = "## 現在地\nXを実装中\n## 次の一手\nYをやる\n"
    (proj / "docs" / "SESSION_SUMMARY.md").write_text(raw, encoding="utf-8")
    win = MainWindow(projects_root=tmp_path, workdir=proj, settings_path=tmp_path / "s.json")
    win.chk_summary_raw.setChecked(True)
    shown = win.summary_view.toPlainText()
    assert "## 現在地" in shown  # 生 markdown そのまま
    assert "【現在地】" not in shown


def test_summary_raw_pref_persists(qapp: QtWidgets.QApplication, tmp_path: Path) -> None:
    sp = tmp_path / "s.json"
    win = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    win.chk_summary_raw.setChecked(True)
    win._save_settings()
    win2 = MainWindow(projects_root=tmp_path, workdir=tmp_path, settings_path=sp)
    assert win2.chk_summary_raw.isChecked() is True


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
    assert "loop 開始 [仮想claude(課金なし)]" in text
    assert "session 1/2 開始" in text            # session N/max 形式で進捗表示
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
