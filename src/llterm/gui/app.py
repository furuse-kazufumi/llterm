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

import html
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from llterm import rad, templates
from llterm.gui import settings as gui_settings
from llterm.gui.settings import DEFAULT_SETTINGS_PATH
from llterm.gui.virtual import VirtualClaudeRunner
from llterm.gui.worker import LoopWorker
from llterm.host import loop as loop_mod
from llterm.host.loop import TurnRunner, _ensure_utf8_stdout

DEFAULT_PROJECTS_ROOT = Path("D:/projects")
_PROJECT_MARKERS = (".git", "pyproject.toml", "CLAUDE.md", "package.json", "Cargo.toml")

# 出力ビューのセマンティックカラー (One Dark 系)。stream-json は ANSI を含まないため、
# 端末色のパススルーではなくイベント種別ごとに llterm 自身が着色する。
PALETTE = {
    "text": "#d8dee9",     # assistant 応答本文
    "session": "#e5c07b",  # セッション境界 (黄)
    "turn": "#61afef",     # ターンヘッダ (青)
    "tool": "#56b6c2",     # ツール実行 (シアン)
    "dim": "#7f848e",      # 補助情報 (灰)
    "err": "#e06c75",      # エラー (赤)
    "rotate": "#c678dd",   # rotate (マゼンタ)
    "inject": "#98c379",   # タスク注入 (緑)
}
_OUTPUT_STYLE = "QPlainTextEdit { background-color: #1e1e2e; color: #d8dee9; border: none; }"


def _coerce_number(value: object, cast: type) -> int | float | None:
    """保存値を int/float へ安全に変換する。変換不能 (型不正・手編集破損) は None。

    bool は int のサブクラスだが数値として扱わない (True/False が 1/0 化するのを防ぐ)。
    "150000.0" のような小数文字列でも int() できるよう float 経由で変換する。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value)) if cast is int else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
        template_default: str | None = None,
        effort_default: str | None = None,
        rad_docs_root: Path = rad.RAD_DOCS_ROOT,
        runner_factory: Callable[[], TurnRunner] | None = None,
        settings_path: Path | None = None,
        **loop_kw: object,
    ) -> None:
        super().__init__()
        self.projects_root = Path(projects_root)
        self.rad_docs_root = Path(rad_docs_root)
        self.runner_factory_override = runner_factory  # tests/仮想を強制注入する穴
        self.settings_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        self._effort_cli = effort_default  # CLI 明示指定 (None = 未指定 → 保存値/既定に委ねる)
        self.loop_kw = dict(loop_kw)
        self.worker: LoopWorker | None = None
        self._streamed_text = 0  # 現ターン中にリアルタイム表示した応答数 (turn 完了時の二重表示防止)
        self._max_sessions = 0  # ステータス表示 (session N/max) 用。Start 時に確定
        self._cost_suffix = "報告値"  # cost ラベルの種別 (Start 時に課金有無で確定)
        self._cost_billed = False  # True = 実課金 (API キー)。サブスク/仮想は False
        self._run_effort = ""  # 実行中の effort (init イベントで model と併記)

        # 前回設定の復元: CLI 明示指定 > 保存値 > 組込み既定
        saved = gui_settings.load_settings(self.settings_path)
        if workdir is None and saved.get("workdir"):
            wd = Path(str(saved["workdir"]))
            workdir = wd if wd.is_dir() else None  # 消えたプロジェクトは復元しない (fail-safe)
        real_default = real_default or bool(saved.get("real", False))
        rad_default = rad_default or bool(saved.get("rad", False))
        autonomy_default = bool(saved.get("autonomy", False))
        template_default = template_default or str(saved.get("template") or "general")
        # effort 既定: CLI 明示 > 保存値 > "max" (ユーザー方針「とりあえず max」2026-06-12)
        effort_default = self._effort_cli if self._effort_cli is not None else str(
            saved.get("effort", "max"))
        # 数値項目は型検証してから取り込む。手編集/外部破損で型不正な値が入っても、
        # _build_ui の int()/float() を直撃させて GUI 起動不能にしない (fail-safe 契約)。
        for kw_key, saved_key, cast in (("max_sessions", "max_sessions", int),
                                        ("threshold", "threshold", float),
                                        ("window_tokens", "window_tokens", int),
                                        ("max_total_cost_usd", "max_cost", float)):
            if self.loop_kw.get(kw_key) is None:
                num = _coerce_number(saved.get(saved_key), cast)
                if num is not None:
                    self.loop_kw[kw_key] = num

        self._build_ui(initial_workdir=Path(workdir) if workdir else None,
                       real_default=real_default, rad_default=rad_default,
                       template_default=template_default,
                       autonomy_default=autonomy_default,
                       param_default=str(saved.get("param") or ""),
                       effort_default=effort_default)
        geo = saved.get("geometry")
        if isinstance(geo, str) and geo:  # ウィンドウ位置/サイズの復元 (壊れた値は無視)
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(geo.encode("ascii")))
            except (ValueError, TypeError):
                pass

    # ---- UI 構築 ----
    def _build_ui(self, *, initial_workdir: Path | None, real_default: bool, rad_default: bool,
                  template_default: str, autonomy_default: bool = False,
                  param_default: str = "", effort_default: str = "max") -> None:
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
        self.chk_autonomy = QtWidgets.QCheckBox("承認確認不要(完全自律)")
        self.chk_autonomy.setToolTip("ON: 人間確認を待たず自律で判断・継続(停止しない)。OFF(既定): 安全側")
        self.chk_autonomy.setChecked(autonomy_default)
        proj_row.addWidget(self.chk_autonomy)
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
        self.spin_window.setToolTip(
            "コンテキスト窓サイズ (使用率の分母)。実 claude が実窓サイズ (modelUsage.contextWindow) "
            "を報告した場合はそちらを優先する")
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
        set_row.addWidget(QtWidgets.QLabel("effort:"))
        self.cmb_effort = QtWidgets.QComboBox()
        for level in loop_mod.EFFORT_LEVELS:
            self.cmb_effort.addItem(level or "(claude既定)", level)
        _eidx = self.cmb_effort.findData(effort_default if effort_default in loop_mod.EFFORT_LEVELS
                                         else "max")
        self.cmb_effort.setCurrentIndex(_eidx if _eidx >= 0 else 0)
        self.cmb_effort.setToolTip(
            "claude の思考努力レベル (--effort)。max が最上位。実 claude のみ有効。"
            "注: raptor の『ultracode』は vanilla claude には無いため max を使う")
        set_row.addWidget(self.cmb_effort)
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
        if param_default:
            self.edit_param.setText(param_default)  # 前回のテンプレ引数を復元

        # ステータス行 — 常時見える状態 (状態 / model / session 進捗 / context 使用率 / cost)
        status_row = QtWidgets.QHBoxLayout()
        self.lbl_state = QtWidgets.QLabel("idle")
        self.lbl_state.setToolTip("ループの状態 (idle / running / stopping / done)")
        self.lbl_model = QtWidgets.QLabel("model: -")
        self.lbl_model.setToolTip("実行中の claude モデル (init イベントから取得) と effort")
        self.lbl_session = QtWidgets.QLabel("session -/-  turn -")
        self.lbl_session.setToolTip("現在のセッション / 最大セッション と、セッション内ターン数")
        self.ctx_bar = QtWidgets.QProgressBar()
        self.ctx_bar.setRange(0, 100)
        self.ctx_bar.setFormat(f"ctx %p%  (rotate {int(round(float(self.loop_kw.get('threshold') or 0.70) * 100))}%)")
        self.ctx_bar.setToolTip("現セッションのコンテキスト使用率。rotate 閾値に達すると新セッションへ畳む")
        self.lbl_cost = QtWidgets.QLabel("cost: $0.0000")
        status_row.addWidget(self.lbl_state)
        status_row.addWidget(self.lbl_model)
        status_row.addWidget(self.lbl_session)
        status_row.addWidget(self.ctx_bar, 1)
        status_row.addWidget(self.lbl_cost)
        vbox.addLayout(status_row)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)  # リングバッファ (上限は表示行数でなく append エントリ数)
        self.output.setStyleSheet(_OUTPUT_STYLE)  # ダーク背景 + セマンティックカラー (PALETTE)
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
            self.cmb_template, self.edit_param, self.btn_publish, self.chk_autonomy,
            self.cmb_effort,
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

    def _append(self, text: str, color: str | None = None, *, bold: bool = False,
                ts: bool = False) -> None:
        """出力ビューへ 1 エントリ追記する (色つき HTML。色未指定は本文色)。

        ts=True で先頭に [HH:MM:SS] を付ける (指令/応答/境界の時刻を見せる)。
        """
        if ts:
            text = f"[{datetime.now():%H:%M:%S}] {text}"
        # CR 正規化: Qt は残留 \r も改行扱いするため CRLF 入りテキストが二重改行になる
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        esc = html.escape(text).replace("\n", "<br/>")
        style = f"color:{color or PALETTE['text']};white-space:pre-wrap"
        if bold:
            style += ";font-weight:bold"
        self.output.appendHtml(f'<span style="{style}">{esc}</span>')

    def _selected_workdir(self) -> Path | None:
        data = self.cmb_project.currentData()
        return Path(str(data)) if data else None

    def _save_settings(self) -> None:
        """現在の GUI 設定を保存する (Start 時と終了時に呼ぶ。失敗しても GUI を殺さない)。"""
        wd = self._selected_workdir()
        gui_settings.save_settings(self.settings_path, {
            "workdir": str(wd) if wd else "",
            "real": self.chk_real.isChecked(),
            "rad": self.chk_rad.isChecked(),
            "autonomy": self.chk_autonomy.isChecked(),
            "max_sessions": self.spin_sessions.value(),
            "threshold": round(self.spin_threshold.value(), 2),
            "window_tokens": self.spin_window.value(),
            "max_cost": self.spin_maxcost.value(),
            "template": self.cmb_template.currentData(),
            "param": self.edit_param.text(),
            "effort": self.cmb_effort.currentData(),
            "geometry": bytes(self.saveGeometry().toHex()).decode("ascii"),
        })

    def _build_runner(self) -> TurnRunner:
        """実行モードに応じた TurnRunner を返す (テスト override 優先)。"""
        if self.runner_factory_override is not None:
            return self.runner_factory_override()
        if self.chk_real.isChecked():
            from llterm.host.loop import ClaudeRunner

            # API キーを外しサブスク認証で実走 + 選択した effort (--effort) を付与
            return ClaudeRunner(use_subscription=True, effort=str(self.cmb_effort.currentData() or ""))
        return VirtualClaudeRunner()

    # ---- 操作 ----
    @QtCore.Slot()
    def start_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        workdir = self._selected_workdir()
        if workdir is None or not workdir.is_dir():
            self._append("error: プロジェクトが選択されていません (コンボボックスから選んでください)",
                         PALETTE["err"])
            return
        real = self.runner_factory_override is None and self.chk_real.isChecked()
        runner = self._build_runner()
        loop_kw = dict(self.loop_kw)
        loop_kw["max_sessions"] = self.spin_sessions.value()  # 常に上限つき (暴走/レート保護)
        loop_kw["threshold"] = self.spin_threshold.value()
        loop_kw["window_tokens"] = self.spin_window.value()
        self._max_sessions = loop_kw["max_sessions"]  # ステータス表示 (session N/max) 用
        # rotate 閾値を ctx バーに反映 (走行中の状態が一目で分かるように)
        self.ctx_bar.setFormat(f"ctx %p%  (rotate {int(round(self.spin_threshold.value() * 100))}%)")
        self.ctx_bar.setValue(0)
        max_cost = self.spin_maxcost.value()
        loop_kw["max_total_cost_usd"] = max_cost if max_cost > 0 else None
        tmpl = templates.get(self.cmb_template.currentData())
        loop_kw.update(tmpl.build(self.edit_param.text()))  # テンプレが resume/continue を上書き
        if self.chk_rad.isChecked():
            from llterm.host.loop import DEFAULT_RAD_HINT

            loop_kw["rad_hint"] = DEFAULT_RAD_HINT
        loop_kw["autonomy"] = self.chk_autonomy.isChecked()  # 承認確認不要トグル
        ledger_path = workdir / ".llterm" / "loop_ledger.jsonl"
        self.worker = LoopWorker(
            runner=runner, workdir=workdir, ledger_path=ledger_path, loop_kw=loop_kw,
        )
        self.worker.event.connect(self._on_event)
        self.worker.stream.connect(self._on_stream)  # ターン内リアルタイム表示
        self.worker.finished_outcome.connect(self._on_finished)
        self._streamed_text = 0
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        for widget in self._run_widgets:
            widget.setEnabled(False)
        # cost 種別 (課金なし / 実課金) を確定し、idle 表示を更新
        self._cost_suffix, self._cost_billed = self._cost_label_mode(runner)
        self._set_cost(0.0)
        effort = str(self.cmb_effort.currentData() or "")
        if real:
            mode = "実claude(API=実課金)" if self._cost_billed else "実claude(サブスク=課金なし)"
        else:
            mode = "仮想claude(課金なし)"
        effort_note = f" effort={effort}" if real and effort else ""
        # model はまだ未確定 (init イベントで判明) — effort だけ先に出す
        self.lbl_model.setText(f"model: …{('  effort=' + effort) if real and effort else ''}")
        self.lbl_state.setText(f"running [{mode}] {tmpl.key}")
        self._run_effort = effort if real else ""  # init で model と併記するため保持
        self._append(f"=== loop 開始 [{mode}] template={tmpl.key} workdir={workdir} "
                     f"max_session={loop_kw['max_sessions']}{effort_note} ===",
                     PALETTE["session"], bold=True, ts=True)
        self._save_settings()  # クラッシュしても Start 時点の設定が次回復元される

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
        self._append(f">> [注入受付] {text}", PALETTE["inject"], ts=True)
        if self.worker is not None and self.worker.isRunning():
            self.worker.inject(text)
        else:
            self._append("  (loop 未起動: Start 後に反映されます)", PALETTE["dim"])

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
            self._append("error: 公開する分野名を引数欄に入れてください", PALETTE["err"])
            return
        stg = rad.staging_dir(domain, self.rad_docs_root)
        if not stg.is_dir():
            self._append(f"error: staging がありません: {stg}", PALETTE["err"])
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
            self._append(f"公開失敗: {exc}", PALETTE["err"])
            return
        msg = f"✓ 公開: {res.live}"
        if res.backup:
            msg += f" (backup: {res.backup})"
        self._append(msg, PALETTE["inject"])

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override 名)
        """ウィンドウを閉じる際、ループ実行中なら停止を要求してから閉じる。

        Stop ボタンと同じく次ターン境界で止まる。現在の claude ターン完了を最大数秒待ち、
        超過時はそのまま閉じる(child の孤児化を最小化)。
        """
        try:
            self._save_settings()  # 最後の設定を次回起動時に復元する
        except Exception:  # noqa: BLE001 — 保存失敗で worker 停止 (子プロセス kill) をスキップさせない
            pass
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()  # 実行中ターンを kill → ループは即終了するので短く待てば足りる
            self.worker.wait(8000)
        event.accept()

    # ---- ワーカーからのイベント (メインスレッドで実行) ----
    @QtCore.Slot(dict)
    def _on_stream(self, item: dict) -> None:
        """ターン内のリアルタイム表示 — 応答・ツール実行をターン完了を待たずに描画する。

        ``subagent: True`` の項目は Task サブエージェント由来 — インデント + 灰色で
        メイン応答と区別し、二重表示防止カウンタ (_streamed_text) には数えない。
        """
        kind = item.get("kind")
        sub = bool(item.get("subagent"))
        prefix = "  ⤷ " if sub else ""
        if kind == "init":
            model = str(item.get("model") or "?")
            sid = str(item.get("session_id", ""))[:8]
            self.lbl_model.setText(f"model: {model}" + (f"  effort={self._run_effort}"
                                                        if self._run_effort else ""))
            self._append(f"⏵ model={model} session={sid}", PALETTE["dim"])
        elif kind == "text":
            text = str(item.get("text") or "")
            if sub:
                self._append(prefix + text, PALETTE["dim"])
            else:
                self._streamed_text += 1
                self._append(text)
        elif kind == "thinking":
            self._append(f"{prefix}… thinking … {item.get('preview') or ''}", PALETTE["dim"])
        elif kind == "tool_use":
            detail = str(item.get("detail") or "")
            line = f"{prefix}⚙ {item.get('name')}" + (f": {detail}" if detail else "")
            self._append(line, PALETTE["dim"] if sub else PALETTE["tool"])
        elif kind == "tool_result":
            preview = str(item.get("preview") or "")
            if item.get("is_error"):
                self._append(f"{prefix}  ↳ エラー: {preview}", PALETTE["err"])
            elif preview:
                self._append(f"{prefix}  ↳ {preview}", PALETTE["dim"])
        elif kind == "rate_limit":
            status = str(item.get("status") or "")
            if status and status != "allowed":  # サブスク自走の主制約 — 制限時は必ず可視化する
                resets_at = int(item.get("resets_at") or 0)
                when = ""
                if resets_at > 0:
                    try:
                        when = f" (リセット: {datetime.fromtimestamp(resets_at):%m-%d %H:%M})"
                    except (OSError, OverflowError, ValueError):
                        pass
                self._append(f"⚠ レート制限: {status}{when}", PALETTE["err"], bold=True, ts=True)
        # kind == "result" はターン完了メトリクス (turn イベント側が正) — ここでは描画しない

    def _session_label(self, index: object, turn: object = None) -> str:
        """session N/max  turn T 形式のステータス文字列を組む。max 未確定時は '-'。"""
        total = f"/{self._max_sessions}" if self._max_sessions else ""
        tail = f"  turn {turn}" if turn is not None else ""
        return f"session {index}{total}{tail}"

    def _cost_label_mode(self, runner: TurnRunner) -> tuple[str, bool]:
        """cost ラベルの種別を runner から決める。(suffix, 実課金か) を返す。

        サブスク認証 (use_subscription=True) と仮想 claude は課金なし。API キー認証のみ実課金。
        """
        from llterm.host.loop import ClaudeRunner

        if isinstance(runner, ClaudeRunner):
            if runner.use_subscription:
                return "報告値・課金なし", False
            return "実課金", True
        return "仮想・課金なし", False

    def _set_cost(self, amount: float) -> None:
        """cost ラベルを更新する。実課金時のみ赤字で警告する。"""
        self.lbl_cost.setText(f"cost({self._cost_suffix}): ${amount:.4f}")
        self.lbl_cost.setStyleSheet(f"color:{PALETTE['err']};font-weight:bold"
                                    if self._cost_billed else "")

    @QtCore.Slot(str, dict)
    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "session_start":
            idx = data.get("session_index")
            sid = str(data.get("session_id", ""))[:8]
            self.lbl_session.setText(self._session_label(idx))
            self.ctx_bar.setValue(0)  # 新セッションは fresh context = 0%
            self._append(f"\n--- {self._session_label(idx)} 開始 ({sid}) ---",
                         PALETTE["session"], bold=True, ts=True)
            self._streamed_text = 0
        elif kind == "task":
            # これから claude に送る指令。時刻を出して「指令時 → 応答受信時」の経過を見せる。
            if data.get("injected"):
                prompt = str(data.get("prompt") or "").strip()
                self._append(f"▶ 注入タスク実行: {prompt}", PALETTE["inject"], bold=True, ts=True)
            else:
                self._append(f"▶ 指令送信 (turn {data.get('turn')})", PALETTE["dim"], ts=True)
        elif kind == "turn":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self.ctx_bar.setValue(min(pct, 100))
            self.lbl_cost.setText(f"cost(報告値): ${float(data.get('total_cost', 0.0)):.4f}")
            self.lbl_session.setText(self._session_label(data.get("session_index"), data.get("turn")))
            err = data.get("error_kind")
            head = f"[turn {data.get('turn')}] 応答受信 ctx {pct}%" + (f"  ERR={err}" if err else "")
            self._append(head, PALETTE["err"] if err else PALETTE["turn"], bold=bool(err), ts=True)
            text = str(data.get("text") or "")
            # ストリーム済みなら再表示しない (二重表示防止)。ただしエラーターンの text は
            # エラー詳細がストリームに乗らないことがあるため常に表示する。
            if text and (self._streamed_text == 0 or err):
                self._append(text, PALETTE["err"] if err else None)
            self._streamed_text = 0
        elif kind == "rotate":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self._append(f"--- rotate (ctx {pct}%) → exit準備 & 新セッションへ ---",
                         PALETTE["rotate"], ts=True)
            self._streamed_text = 0
        elif kind == "stopped":
            self._append(
                f"\n=== stopped: {data.get('stop_reason')} "
                f"(sessions={data.get('sessions')}, turns={data.get('turns')}, "
                f"cost(報告値)=${float(data.get('total_cost', 0.0)):.4f}) ===",
                PALETTE["session"], bold=True, ts=True,
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
                         "— 構造的に唯一の人間介在点。", PALETTE["err"], bold=True)


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
    # 未指定 (None) の項目は前回保存値 → 組込み既定の順で補完される (CLI 明示指定が最優先)
    parser.add_argument("--template", default=None,
                        help="起動時に選ぶテンプレ key (general/rad_expand/green_keeper/doc_update。"
                             "既定: 前回値)")
    parser.add_argument("--effort", default=None, choices=loop_mod.EFFORT_LEVELS,
                        help="claude の思考努力レベル (low/medium/high/xhigh/max。既定: 前回値→max)")
    parser.add_argument("--threshold", type=float, default=None, help="既定: 前回値 (初回 0.70)")
    parser.add_argument("--window-tokens", type=int, default=None, help="既定: 前回値 (初回 200000)")
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
        effort_default=args.effort,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=args.max_sessions,
        max_total_cost_usd=args.max_cost,
    )
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
