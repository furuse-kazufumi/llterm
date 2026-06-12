# SPDX-License-Identifier: Apache-2.0
"""llterm GUI (L3) — Claude Code 自走ループの窓口。

端末を捨てた GUI。プロジェクト選択(コンボボックス)→ Start で自走開始。
出力ビュー(リングバッファ)/ コンテキスト使用率バー / コスト(報告値)/ セッション番号 /
Start・Stop / タスク注入欄(Ctrl+Enter 送信)/ セッション上限。

実行モード:
- **仮想 claude**(既定, チェックを外す): 実 claude を呼ばない。課金ゼロのデバッグ/プレビュー。
- **実 claude(claude.ai サブスク認証)**: ``ClaudeRunner(use_subscription=True)`` が API キー env を外して
  OAuth サブスクで回す → **新たな従量課金なし**(Max 定額の範囲。制約はレート制限)。
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from llterm import rad, templates
from llterm.gui.virtual import VirtualClaudeRunner
from llterm.gui.worker import LoopWorker
from llterm.host.loop import TurnRunner, _ensure_utf8_stdout

DEFAULT_PROJECTS_ROOT = Path("D:/projects")
_PROJECT_MARKERS = (".git", "pyproject.toml", "CLAUDE.md", "package.json", "Cargo.toml")


def discover_projects(root: Path) -> list[tuple[str, Path]]:
    """projects root 直下の「プロジェクトらしい」ディレクトリを (名前, パス) で列挙する。"""
    found: list[tuple[str, Path]] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if any((child / marker).exists() for marker in _PROJECT_MARKERS):
                found.append((child.name, child))
    except OSError:
        pass
    return found


class MainWindow(QtWidgets.QMainWindow):
    """ループ駆動の主ウィンドウ。L2 (SessionLoop) を QThread で回し進捗を描画する。"""

    def __init__(
        self,
        *,
        projects_root: Path = DEFAULT_PROJECTS_ROOT,
        workdir: Path | None = None,
        real_default: bool = False,
        rad_default: bool = False,
        template_default: str = "general",
        rad_docs_root: Path = rad.RAD_DOCS_ROOT,
        runner_factory: Callable[[], TurnRunner] | None = None,
        **loop_kw: object,
    ) -> None:
        super().__init__()
        self.projects_root = Path(projects_root)
        self.rad_docs_root = Path(rad_docs_root)
        self.runner_factory_override = runner_factory  # tests/仮想を強制注入する穴
        self.loop_kw = dict(loop_kw)
        self.worker: LoopWorker | None = None
        self._build_ui(initial_workdir=Path(workdir) if workdir else None,
                       real_default=real_default, rad_default=rad_default,
                       template_default=template_default)

    # ---- UI 構築 ----
    def _build_ui(self, *, initial_workdir: Path | None, real_default: bool, rad_default: bool,
                  template_default: str) -> None:
        self.setWindowTitle("llterm — Claude Code 自走ループ (GUI)")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        # プロジェクト選択行
        proj_row = QtWidgets.QHBoxLayout()
        proj_row.addWidget(QtWidgets.QLabel("プロジェクト:"))
        self.cmb_project = QtWidgets.QComboBox()
        self.cmb_project.setMinimumWidth(360)
        self._populate_projects(initial_workdir)
        proj_row.addWidget(self.cmb_project, 1)
        self.chk_real = QtWidgets.QCheckBox("実 claude (claude.ai サブスク認証)")
        self.chk_real.setChecked(real_default)
        self.chk_real.setToolTip("off = 仮想 claude (課金ゼロ)。on = サブスク認証で実走 (従量課金なし・レート制限内)")
        proj_row.addWidget(self.chk_real)
        self.chk_rad = QtWidgets.QCheckBox("RAD 参照")
        self.chk_rad.setChecked(rad_default)
        self.chk_rad.setToolTip("新規作業前に RAD コーパス (D:/docs/*_corpus_v2) を grep して研究接地する")
        proj_row.addWidget(self.chk_rad)
        proj_row.addWidget(QtWidgets.QLabel("最大session:"))
        self.spin_sessions = QtWidgets.QSpinBox()
        self.spin_sessions.setRange(1, 100000)
        default_sessions = self.loop_kw.get("max_sessions")
        self.spin_sessions.setValue(int(default_sessions) if default_sessions else 8)
        proj_row.addWidget(self.spin_sessions)
        vbox.addLayout(proj_row)

        # 設定行 (CLI 引数と同等のものを GUI からも設定可能に)
        set_row = QtWidgets.QHBoxLayout()
        set_row.addWidget(QtWidgets.QLabel("rotate閾値:"))
        self.spin_threshold = QtWidgets.QDoubleSpinBox()
        self.spin_threshold.setRange(0.10, 0.95)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setDecimals(2)
        self.spin_threshold.setValue(float(self.loop_kw.get("threshold") or 0.70))
        self.spin_threshold.setToolTip("この使用率で rotate (exit準備 → 新セッション)")
        set_row.addWidget(self.spin_threshold)
        set_row.addWidget(QtWidgets.QLabel("窓tokens:"))
        self.spin_window = QtWidgets.QSpinBox()
        self.spin_window.setRange(10_000, 2_000_000)
        self.spin_window.setSingleStep(10_000)
        self.spin_window.setGroupSeparatorShown(True)
        self.spin_window.setValue(int(self.loop_kw.get("window_tokens") or 200_000))
        self.spin_window.setToolTip("コンテキスト窓サイズ (使用率の分母)")
        set_row.addWidget(self.spin_window)
        set_row.addWidget(QtWidgets.QLabel("コスト上限$(0=無制限):"))
        self.spin_maxcost = QtWidgets.QDoubleSpinBox()
        self.spin_maxcost.setRange(0.0, 100000.0)
        self.spin_maxcost.setDecimals(2)
        self.spin_maxcost.setSingleStep(1.0)
        _mc = self.loop_kw.get("max_total_cost_usd")
        self.spin_maxcost.setValue(float(_mc) if _mc else 0.0)
        self.spin_maxcost.setToolTip("報告コストの累計上限 (サブスクでは governor。0 で無制限)")
        set_row.addWidget(self.spin_maxcost)
        set_row.addStretch(1)
        vbox.addLayout(set_row)

        # テンプレ行 (機能ごとの自走テンプレ + RAD 公開ゲート)
        tmpl_row = QtWidgets.QHBoxLayout()
        tmpl_row.addWidget(QtWidgets.QLabel("テンプレ:"))
        self.cmb_template = QtWidgets.QComboBox()
        for i, t in enumerate(templates.TEMPLATES):
            self.cmb_template.addItem(t.label, t.key)
            self.cmb_template.setItemData(i, t.description, QtCore.Qt.ItemDataRole.ToolTipRole)
        tmpl_row.addWidget(self.cmb_template)
        self.edit_param = QtWidgets.QLineEdit()
        self.edit_param.setPlaceholderText("(テンプレ引数)")
        tmpl_row.addWidget(self.edit_param, 1)
        self.btn_publish = QtWidgets.QPushButton("公開(staging→live)")
        self.btn_publish.setToolTip("RAD 拡張の staging を共有 live へ昇格する公開ゲート(人間の明示操作)。")
        self.btn_publish.clicked.connect(self._promote_clicked)
        tmpl_row.addWidget(self.btn_publish)
        vbox.addLayout(tmpl_row)
        self.cmb_template.currentIndexChanged.connect(self._on_template_changed)
        _idx = self.cmb_template.findData(template_default)
        self.cmb_template.setCurrentIndex(_idx if _idx >= 0 else 0)
        self._on_template_changed()  # 初期 tooltip / param 有効化

        # ステータス行
        status_row = QtWidgets.QHBoxLayout()
        self.lbl_state = QtWidgets.QLabel("idle")
        self.lbl_session = QtWidgets.QLabel("session: -")
        self.ctx_bar = QtWidgets.QProgressBar()
        self.ctx_bar.setRange(0, 100)
        self.ctx_bar.setFormat("ctx %p%")
        self.lbl_cost = QtWidgets.QLabel("cost(報告値): $0.0000")
        status_row.addWidget(self.lbl_state)
        status_row.addWidget(self.lbl_session)
        status_row.addWidget(self.ctx_bar, 1)
        status_row.addWidget(self.lbl_cost)
        vbox.addLayout(status_row)

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

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_send = QtWidgets.QPushButton("Send (Ctrl+Enter)")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_send)
        vbox.addLayout(btn_row)

        self.resize(940, 640)
        self.btn_start.clicked.connect(self.start_loop)
        self.btn_stop.clicked.connect(self.stop_loop)
        self.btn_send.clicked.connect(self.send_input)
        # Enter=改行のまま / 送信は Ctrl+Enter (R12: 誤送信の構造的防止)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.input, self.send_input)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Enter"), self.input, self.send_input)
        # 走行中に無効化する設定系ウィジェット (途中変更で worker と不整合にしない)
        self._run_widgets: list[QtWidgets.QWidget] = [
            self.cmb_project, self.chk_real, self.chk_rad, self.spin_sessions,
            self.spin_threshold, self.spin_window, self.spin_maxcost,
            self.cmb_template, self.edit_param, self.btn_publish,
        ]

    def _populate_projects(self, initial_workdir: Path | None) -> None:
        self.cmb_project.clear()
        for name, path in discover_projects(self.projects_root):
            self.cmb_project.addItem(name, str(path))
        # 明示指定の workdir は (root 配下でなくても) 必ず候補に入れて選択する
        if initial_workdir is not None:
            target = str(initial_workdir)
            idx = self.cmb_project.findData(target)
            if idx < 0:
                self.cmb_project.insertItem(0, initial_workdir.name, target)
                idx = 0
            self.cmb_project.setCurrentIndex(idx)

    def _append(self, text: str) -> None:
        self.output.appendPlainText(text)

    def _selected_workdir(self) -> Path | None:
        data = self.cmb_project.currentData()
        return Path(str(data)) if data else None

    def _build_runner(self) -> TurnRunner:
        """実行モードに応じた TurnRunner を返す (テスト override 優先)。"""
        if self.runner_factory_override is not None:
            return self.runner_factory_override()
        if self.chk_real.isChecked():
            from llterm.host.loop import ClaudeRunner

            return ClaudeRunner(use_subscription=True)  # API キーを外しサブスク認証で実走
        return VirtualClaudeRunner()

    # ---- 操作 ----
    @QtCore.Slot()
    def start_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        workdir = self._selected_workdir()
        if workdir is None or not workdir.is_dir():
            self._append("error: プロジェクトが選択されていません (コンボボックスから選んでください)")
            return
        real = self.runner_factory_override is None and self.chk_real.isChecked()
        runner = self._build_runner()
        loop_kw = dict(self.loop_kw)
        loop_kw["max_sessions"] = self.spin_sessions.value()  # 常に上限つき (暴走/レート保護)
        loop_kw["threshold"] = self.spin_threshold.value()
        loop_kw["window_tokens"] = self.spin_window.value()
        max_cost = self.spin_maxcost.value()
        loop_kw["max_total_cost_usd"] = max_cost if max_cost > 0 else None
        tmpl = templates.get(self.cmb_template.currentData())
        loop_kw.update(tmpl.build(self.edit_param.text()))  # テンプレが resume/continue を上書き
        if self.chk_rad.isChecked():
            from llterm.host.loop import DEFAULT_RAD_HINT

            loop_kw["rad_hint"] = DEFAULT_RAD_HINT
        ledger_path = workdir / ".llterm" / "loop_ledger.jsonl"
        self.worker = LoopWorker(
            runner=runner, workdir=workdir, ledger_path=ledger_path, loop_kw=loop_kw,
        )
        self.worker.event.connect(self._on_event)
        self.worker.finished_outcome.connect(self._on_finished)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        for widget in self._run_widgets:
            widget.setEnabled(False)
        mode = "実claude(サブスク)" if real else "仮想claude"
        self.lbl_state.setText(f"running [{mode}] {tmpl.key}")
        self._append(f"=== loop 開始 [{mode}] template={tmpl.key} workdir={workdir} "
                     f"max_session={loop_kw['max_sessions']} ===")

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

    @QtCore.Slot()
    def _on_template_changed(self) -> None:
        key = self.cmb_template.currentData()
        if key is None:
            return
        tmpl = templates.get(key)
        self.cmb_template.setToolTip(tmpl.description)  # 用途をツールチップで表示
        self.edit_param.setEnabled(tmpl.needs_param)
        self.edit_param.setPlaceholderText(tmpl.param_label if tmpl.needs_param else "(引数不要)")

    @QtCore.Slot()
    def _promote_clicked(self) -> None:
        domain = self.edit_param.text().strip()
        if not domain:
            self._append("error: 公開する分野名を引数欄に入れてください")
            return
        stg = rad.staging_dir(domain, self.rad_docs_root)
        if not stg.is_dir():
            self._append(f"error: staging がありません: {stg}")
            return
        reply = QtWidgets.QMessageBox.question(
            self, "RAD 公開ゲート",
            f"分野「{domain}」の staging を共有 live へ公開しますか?\n"
            f"  staging: {stg}\n  live: {rad.live_dir(domain, self.rad_docs_root)}\n"
            f"既存 live はバックアップされます。",
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self._do_promote(domain)

    def _do_promote(self, domain: str) -> None:
        try:
            res = rad.promote(domain, docs_root=self.rad_docs_root)
        except rad.RadError as exc:
            self._append(f"公開失敗: {exc}")
            return
        msg = f"✓ 公開: {res.live}"
        if res.backup:
            msg += f" (backup: {res.backup})"
        self._append(msg)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override 名)
        """ウィンドウを閉じる際、ループ実行中なら停止を要求してから閉じる。

        Stop ボタンと同じく次ターン境界で止まる。現在の claude ターン完了を最大数秒待ち、
        超過時はそのまま閉じる(child の孤児化を最小化)。
        """
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(3000)
        event.accept()

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
            self.lbl_cost.setText(f"cost(報告値): ${float(data.get('total_cost', 0.0)):.4f}")
            self.lbl_session.setText(f"session #{data.get('session_index')} · turn {data.get('turn')}")
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
                f"cost(報告値)=${float(data.get('total_cost', 0.0)):.4f}) ==="
            )

    @QtCore.Slot(dict)
    def _on_finished(self, outcome: dict) -> None:
        reason = outcome.get("stop_reason")
        self.lbl_state.setText(f"done: {reason}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        for widget in self._run_widgets:
            widget.setEnabled(True)
        if reason == "auth_required":
            self._append("⚠ 再ログインが必要です (claude /login)。認証後に Start で再開してください "
                         "— 構造的に唯一の人間介在点。")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    import argparse

    parser = argparse.ArgumentParser(
        prog="llterm-gui",
        description="llterm GUI: Claude Code 自走ループの窓口 (端末非依存)。既定は仮想 claude (課金ゼロ)。",
    )
    parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT),
                        help="コンボボックスに出すプロジェクトの親ディレクトリ (既定 D:/projects)")
    parser.add_argument("--workdir", default=None, help="初期選択するプロジェクト (任意)")
    parser.add_argument("--real", action="store_true",
                        help="起動時に『実 claude(サブスク認証)』を選択状態にする")
    parser.add_argument("--rad", action="store_true",
                        help="起動時に『RAD 参照(研究接地)』を選択状態にする")
    parser.add_argument("--template", default="general",
                        help="起動時に選ぶテンプレ key (general/rad_expand/green_keeper/doc_update)")
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--window-tokens", type=int, default=200_000)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--max-cost", type=float, default=None)
    args = parser.parse_args(argv)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow(
        projects_root=Path(args.projects_root),
        workdir=Path(args.workdir).resolve() if args.workdir else None,
        real_default=args.real,
        rad_default=args.rad,
        template_default=args.template,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=args.max_sessions,
        max_total_cost_usd=args.max_cost,
    )
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
