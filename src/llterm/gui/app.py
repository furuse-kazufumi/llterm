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
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from llterm import rad, templates
from llterm.gui import settings as gui_settings
from llterm.i18n import t
from llterm.gui.settings import DEFAULT_SETTINGS_PATH
from llterm.gui.virtual import VirtualClaudeRunner
from llterm.gui.worker import LoopWorker
from llterm.host import loop as loop_mod
from llterm.host.loop import TurnRunner, _ensure_utf8_stdout

if TYPE_CHECKING:
    from llterm.host.gemini_runner import GeminiRunner
    from llterm.host.openai_compat_runner import OpenAICompatRunner

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


def find_app_icon() -> QtGui.QIcon | None:
    """タイトルバー/タスクバー用アイコンを assets から探す (見つからなければ None)。"""
    here = Path(__file__).resolve()
    bases = [here.parents[3] / "assets", here.parent / "assets"]  # repo/assets と package 同梱
    for base in bases:
        for name in ("llterm-icon.ico", "llterm.ico", "llterm_256.png"):
            p = base / name
            if p.exists():
                return QtGui.QIcon(str(p))
    return None


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
        model_default: str | None = None,
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
        self._model_cli = model_default  # CLI 明示指定 (None = 未指定 → 保存値/既定に委ねる)
        self.loop_kw = dict(loop_kw)
        self.worker: LoopWorker | None = None
        self._streamed_text = 0  # 現ターン中にリアルタイム表示した応答数 (turn 完了時の二重表示防止)
        self._max_sessions = 0  # ステータス表示 (session N/max) 用。Start 時に確定
        self._cost_suffix = t("gui.cost.reported")  # cost ラベルの種別 (Start 時に課金有無で確定)
        self._cost_billed = False  # True = 実課金 (API キー)。サブスク/仮想は False
        self._run_effort = ""  # 実行中の effort (init イベントで model と併記)
        self._run_workdir: Path | None = None  # 実行中の workdir (SESSION_SUMMARY 読取用)
        self._stopping = False  # graceful 停止要求中 (2 回目 Stop で force kill)
        self._busy_cursor = False  # 砂時計カーソル表示中か (set/restore のバランス管理)
        self._closing_after_stop = False  # × 終了確認で graceful 停止 → 完了後に閉じる予約

        # 前回設定の復元: CLI 明示指定 > 保存値 > 組込み既定
        saved = gui_settings.load_settings(self.settings_path)
        if workdir is None and saved.get("workdir"):
            wd = Path(str(saved["workdir"]))
            workdir = wd if wd.is_dir() else None  # 消えたプロジェクトは復元しない (fail-safe)
        real_default = real_default or bool(saved.get("real", False))
        rad_default = rad_default or bool(saved.get("rad", False))
        autonomy_default = bool(saved.get("autonomy", False))
        codex_fallback_default = bool(saved.get("codex_fallback", False))
        codex_first_default = bool(saved.get("codex_first", False))
        gemini_fallback_default = bool(saved.get("gemini_fallback", False))
        reviewer_default = str(saved.get("reviewer") or "")
        template_default = template_default or str(saved.get("template") or "general")
        # effort 既定: CLI 明示 > 保存値 > "max" (ユーザー方針「とりあえず max」2026-06-12)
        effort_default = self._effort_cli if self._effort_cli is not None else str(
            saved.get("effort", "max"))
        # model 既定: CLI 明示 > 保存値 > DEFAULT_MODEL (ユーザー方針「llterm も Opus 4.8」2026-06-13)
        model_default = self._model_cli if self._model_cli is not None else str(
            saved.get("model", loop_mod.DEFAULT_MODEL))
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
                       effort_default=effort_default,
                       model_default=model_default,
                       codex_fallback_default=codex_fallback_default,
                       codex_first_default=codex_first_default,
                       gemini_fallback_default=gemini_fallback_default,
                       reviewer_default=reviewer_default,
                       summary_raw_default=bool(saved.get("summary_raw", False)),
                       free_provider_default=str(saved.get("free_provider") or ""))
        geo = saved.get("geometry")
        if isinstance(geo, str) and geo:  # ウィンドウ位置/サイズの復元 (壊れた値は無視)
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(geo.encode("ascii")))
            except (ValueError, TypeError):
                pass

    # ---- UI 構築 ----
    def _build_ui(self, *, initial_workdir: Path | None, real_default: bool, rad_default: bool,
                  template_default: str, autonomy_default: bool = False,
                  param_default: str = "", effort_default: str = "max",
                  model_default: str = loop_mod.DEFAULT_MODEL,
                  codex_fallback_default: bool = False,
                  codex_first_default: bool = False,
                  gemini_fallback_default: bool = False,
                  reviewer_default: str = "",
                  summary_raw_default: bool = False,
                  free_provider_default: str = "") -> None:
        self.setWindowTitle(t("gui.window.title"))
        icon = find_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)  # タイトルバー/タスクバーのアイコン
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        # プロジェクト選択行
        proj_row = QtWidgets.QHBoxLayout()
        proj_row.addWidget(QtWidgets.QLabel(t("gui.label.project")))
        self.cmb_project = QtWidgets.QComboBox()
        self.cmb_project.setMinimumWidth(360)
        self._populate_projects(initial_workdir)
        proj_row.addWidget(self.cmb_project, 1)
        self.chk_real = QtWidgets.QCheckBox(t("gui.check.real"))
        self.chk_real.setChecked(real_default)
        self.chk_real.setToolTip(t("gui.tip.real"))
        proj_row.addWidget(self.chk_real)
        self.chk_rad = QtWidgets.QCheckBox(t("gui.check.rad"))
        self.chk_rad.setChecked(rad_default)
        self.chk_rad.setToolTip(t("gui.tip.rad"))
        proj_row.addWidget(self.chk_rad)
        self.chk_autonomy = QtWidgets.QCheckBox(t("gui.check.autonomy"))
        self.chk_autonomy.setToolTip(t("gui.tip.autonomy"))
        self.chk_autonomy.setChecked(autonomy_default)
        proj_row.addWidget(self.chk_autonomy)
        self.chk_codex_fallback = QtWidgets.QCheckBox(t("gui.check.codex"))
        self.chk_codex_fallback.setToolTip(t("gui.tip.codex"))
        self.chk_codex_fallback.setChecked(codex_fallback_default)
        proj_row.addWidget(self.chk_codex_fallback)
        self.chk_codex_first = QtWidgets.QCheckBox(t("gui.check.codex_first"))
        self.chk_codex_first.setToolTip(t("gui.tip.codex_first"))
        self.chk_codex_first.setChecked(codex_first_default)
        proj_row.addWidget(self.chk_codex_first)
        self.chk_gemini_fallback = QtWidgets.QCheckBox(t("gui.check.gemini"))
        self.chk_gemini_fallback.setToolTip(t("gui.tip.gemini"))
        self.chk_gemini_fallback.setChecked(gemini_fallback_default)
        proj_row.addWidget(self.chk_gemini_fallback)
        proj_row.addWidget(QtWidgets.QLabel(t("gui.label.max_sessions")))
        self.spin_sessions = QtWidgets.QSpinBox()
        self.spin_sessions.setRange(1, 100000)
        default_sessions = self.loop_kw.get("max_sessions")
        self.spin_sessions.setValue(int(default_sessions) if default_sessions else 8)
        proj_row.addWidget(self.spin_sessions)
        vbox.addLayout(proj_row)

        # 設定行 (CLI 引数と同等のものを GUI からも設定可能に)
        set_row = QtWidgets.QHBoxLayout()
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.threshold")))
        self.spin_threshold = QtWidgets.QDoubleSpinBox()
        self.spin_threshold.setRange(0.10, 0.95)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setDecimals(2)
        self.spin_threshold.setValue(float(self.loop_kw.get("threshold") or 0.70))
        self.spin_threshold.setToolTip(t("gui.tip.threshold"))
        set_row.addWidget(self.spin_threshold)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.window_tokens")))
        self.spin_window = QtWidgets.QSpinBox()
        self.spin_window.setRange(10_000, 2_000_000)
        self.spin_window.setSingleStep(10_000)
        self.spin_window.setGroupSeparatorShown(True)
        self.spin_window.setValue(int(self.loop_kw.get("window_tokens") or 200_000))
        self.spin_window.setToolTip(t("gui.tip.window_tokens"))
        set_row.addWidget(self.spin_window)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.max_cost")))
        self.spin_maxcost = QtWidgets.QDoubleSpinBox()
        self.spin_maxcost.setRange(0.0, 100000.0)
        self.spin_maxcost.setDecimals(2)
        self.spin_maxcost.setSingleStep(1.0)
        _mc = self.loop_kw.get("max_total_cost_usd")
        self.spin_maxcost.setValue(float(_mc) if _mc else 0.0)
        self.spin_maxcost.setToolTip(t("gui.tip.max_cost"))
        set_row.addWidget(self.spin_maxcost)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.effort")))
        self.cmb_effort = QtWidgets.QComboBox()
        for level in loop_mod.EFFORT_LEVELS:
            self.cmb_effort.addItem(level or t("gui.effort.default_item"), level)
        _eidx = self.cmb_effort.findData(effort_default if effort_default in loop_mod.EFFORT_LEVELS
                                         else "max")
        self.cmb_effort.setCurrentIndex(_eidx if _eidx >= 0 else 0)
        self.cmb_effort.setToolTip(t("gui.tip.effort"))
        set_row.addWidget(self.cmb_effort)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.model")))
        self.cmb_model = QtWidgets.QComboBox()
        for m in loop_mod.MODEL_CHOICES:
            self.cmb_model.addItem(m or t("gui.model.default_item"), m)
        # 保存値が候補外 (手編集の独自モデル等) なら DEFAULT_MODEL の位置へ落とす (fail-safe)
        _midx = self.cmb_model.findData(model_default if model_default in loop_mod.MODEL_CHOICES
                                        else loop_mod.DEFAULT_MODEL)
        self.cmb_model.setCurrentIndex(_midx if _midx >= 0 else 0)
        self.cmb_model.setToolTip(t("gui.tip.model_select"))
        set_row.addWidget(self.cmb_model)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.free_player")))
        self.cmb_free_provider = QtWidgets.QComboBox()
        self.cmb_free_provider.addItem(t("gui.free_player.none"), "")
        for _key, _label in (("groq", "Groq"), ("cerebras", "Cerebras"),
                             ("openrouter", "OpenRouter"), ("ollama", "Ollama (local)")):
            self.cmb_free_provider.addItem(_label, _key)
        _fpidx = self.cmb_free_provider.findData(free_provider_default)
        self.cmb_free_provider.setCurrentIndex(_fpidx if _fpidx >= 0 else 0)
        self.cmb_free_provider.setToolTip(t("gui.tip.free_player"))
        set_row.addWidget(self.cmb_free_provider)
        set_row.addWidget(QtWidgets.QLabel(t("gui.label.reviewer")))
        self.cmb_reviewer = QtWidgets.QComboBox()
        self.cmb_reviewer.addItem(t("gui.reviewer.none"), "")
        for _key, _label in (("codex", "Codex"), ("gemini", "Gemini"), ("groq", "Groq"),
                             ("cerebras", "Cerebras"), ("openrouter", "OpenRouter"),
                             ("ollama", "Ollama")):
            self.cmb_reviewer.addItem(_label, _key)
        _rvidx = self.cmb_reviewer.findData(reviewer_default)
        self.cmb_reviewer.setCurrentIndex(_rvidx if _rvidx >= 0 else 0)
        self.cmb_reviewer.setToolTip(t("gui.tip.reviewer"))
        set_row.addWidget(self.cmb_reviewer)
        set_row.addStretch(1)
        vbox.addLayout(set_row)

        # テンプレ行 (機能ごとの自走テンプレ + RAD 公開ゲート)
        tmpl_row = QtWidgets.QHBoxLayout()
        tmpl_row.addWidget(QtWidgets.QLabel(t("gui.label.template")))
        self.cmb_template = QtWidgets.QComboBox()
        for i, tmpl in enumerate(templates.TEMPLATES):
            self.cmb_template.addItem(tmpl.label, tmpl.key)
            self.cmb_template.setItemData(i, tmpl.description, QtCore.Qt.ItemDataRole.ToolTipRole)
        tmpl_row.addWidget(self.cmb_template)
        self.edit_param = QtWidgets.QLineEdit()
        self.edit_param.setPlaceholderText(t("gui.placeholder.param"))
        tmpl_row.addWidget(self.edit_param, 1)
        self.btn_publish = QtWidgets.QPushButton(t("gui.btn.publish"))
        self.btn_publish.setToolTip(t("gui.tip.publish"))
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
        self.lbl_state = QtWidgets.QLabel(t("gui.state.idle"))
        self.lbl_state.setToolTip(t("gui.tip.state"))
        self.lbl_model = QtWidgets.QLabel("model: -")
        self.lbl_model.setToolTip(t("gui.tip.model"))
        self.lbl_session = QtWidgets.QLabel("session -/-  turn -")
        self.lbl_session.setToolTip(t("gui.tip.session"))
        self.ctx_bar = QtWidgets.QProgressBar()
        self.ctx_bar.setRange(0, 100)
        self.ctx_bar.setFormat(f"ctx %p%  (rotate {int(round(float(self.loop_kw.get('threshold') or 0.70) * 100))}%)")
        self.ctx_bar.setToolTip(t("gui.tip.ctx"))
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

        # 進捗サマリ パネル (右側・全文スクロール可・選択コピー可)。claude が書く
        # docs/SESSION_SUMMARY.md の全文を表示する。ここから単語を選んでタスク注入に流用できる。
        # スプリッタでドラッグ収納できるので普段は邪魔にならない。
        summary_panel = QtWidgets.QWidget()
        sp_box = QtWidgets.QVBoxLayout(summary_panel)
        sp_box.setContentsMargins(0, 0, 0, 0)
        sp_head = QtWidgets.QHBoxLayout()
        sp_head.addWidget(QtWidgets.QLabel(t("gui.summary.title")))
        sp_head.addStretch(1)
        # 既定 = 構造化ダイジェスト (現在地/直近の成果/次の一手)。OFF で生 SESSION_SUMMARY 全文。
        self.chk_summary_raw = QtWidgets.QCheckBox(t("gui.check.summary_raw"))
        self.chk_summary_raw.setToolTip(t("gui.tip.summary_raw"))
        self.chk_summary_raw.setChecked(summary_raw_default)
        self.chk_summary_raw.toggled.connect(self._refresh_summary)
        sp_head.addWidget(self.chk_summary_raw)
        self.btn_refresh_summary = QtWidgets.QPushButton(t("gui.btn.refresh"))
        self.btn_refresh_summary.setToolTip(t("gui.tip.refresh"))
        self.btn_refresh_summary.clicked.connect(self._refresh_summary)
        sp_head.addWidget(self.btn_refresh_summary)
        sp_box.addLayout(sp_head)
        self.summary_view = QtWidgets.QPlainTextEdit()
        self.summary_view.setReadOnly(True)  # 読取専用でも選択 + Ctrl+C は可 (単語をタスク注入へ)
        self.summary_view.setPlaceholderText(t("gui.placeholder.summary"))
        self.summary_view.setFont(mono)
        sp_box.addWidget(self.summary_view, 1)

        self.split_main = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.split_main.addWidget(self.output)
        self.split_main.addWidget(summary_panel)
        self.split_main.setStretchFactor(0, 3)  # ログを広めに
        self.split_main.setStretchFactor(1, 2)
        self.split_main.setSizes([620, 380])
        vbox.addWidget(self.split_main, 1)

        self.input = QtWidgets.QPlainTextEdit()
        self.input.setPlaceholderText(t("gui.placeholder.input"))
        self.input.setMaximumHeight(90)
        vbox.addWidget(self.input)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton(t("gui.btn.start"))
        self.btn_stop = QtWidgets.QPushButton(t("gui.btn.stop"))
        self.btn_stop.setEnabled(False)
        self.btn_send = QtWidgets.QPushButton(t("gui.btn.send"))
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_send)
        vbox.addLayout(btn_row)

        # 進捗バー (画面下部・邪魔にならない定位置)。完全自律時に「今どこまで進んだか」を
        # 直近応答の 1 行要約で常時表示する。ホバーで全文 (応答 / handoff サマリ)。
        self.lbl_progress = QtWidgets.QLabel(t("gui.progress.idle"))
        self.lbl_progress.setToolTip(t("gui.tip.progress"))
        self.statusBar().addWidget(self.lbl_progress, 1)

        self.resize(940, 640)
        self.btn_start.clicked.connect(self.start_loop)
        self.btn_stop.clicked.connect(self.stop_loop)
        self.btn_send.clicked.connect(self.send_input)
        # Enter=改行のまま / 送信は Ctrl+Enter (R12: 誤送信の構造的防止)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.input, self.send_input)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Enter"), self.input, self.send_input)
        # プロジェクトを選び直したら、その既存 SESSION_SUMMARY をプレビュー (idle 時)
        self.cmb_project.currentIndexChanged.connect(self._refresh_summary)
        self._refresh_summary()  # 初期表示 (選択プロジェクトの既存サマリ)
        # 走行中に無効化する設定系ウィジェット (途中変更で worker と不整合にしない)
        self._run_widgets: list[QtWidgets.QWidget] = [
            self.cmb_project, self.chk_real, self.chk_rad, self.spin_sessions,
            self.spin_threshold, self.spin_window, self.spin_maxcost,
            self.cmb_template, self.edit_param, self.btn_publish, self.chk_autonomy,
            self.cmb_effort, self.cmb_model, self.chk_codex_fallback, self.chk_codex_first,
            self.chk_gemini_fallback, self.cmb_free_provider,
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
            "codex_fallback": self.chk_codex_fallback.isChecked(),
            "codex_first": self.chk_codex_first.isChecked(),
            "gemini_fallback": self.chk_gemini_fallback.isChecked(),
            "free_provider": self.cmb_free_provider.currentData(),
            "summary_raw": self.chk_summary_raw.isChecked(),
            "max_sessions": self.spin_sessions.value(),
            "threshold": round(self.spin_threshold.value(), 2),
            "window_tokens": self.spin_window.value(),
            "max_cost": self.spin_maxcost.value(),
            "template": self.cmb_template.currentData(),
            "param": self.edit_param.text(),
            "effort": self.cmb_effort.currentData(),
            "model": self.cmb_model.currentData(),
            "geometry": bytes(self.saveGeometry().toHex()).decode("ascii"),
        })

    def _codex_is_primary(self) -> bool:
        """このランで Codex を主役にするか (token 節約ルーティング)。

        「Codex 優先」トグル ON、または選択テンプレが ``prefer=="codex"`` (機械的タスク) の
        とき True。実 claude モードでのみ意味を持つ。
        """
        if self.chk_codex_first.isChecked():
            return True
        return templates.get(self.cmb_template.currentData()).prefer == "codex"

    def _resolve_providers(self) -> tuple[TurnRunner, list[TurnRunner]]:
        """このランの (primary, fallbacks) を決める唯一の真実。

        - テスト override 最優先。
        - 仮想 claude モードは Codex を使わない (課金/サブスク不要のプレビュー)。
        - 実 claude モード: Codex 主なら ``(Codex, [Claude])`` = 作業を無料の Codex に寄せ、
          Claude は保険 (Codex レート制限時に継続) に回す。Claude 主なら従来どおり
          ``(Claude, [Codex] if フォールバック ON else [])``。
        """
        if self.runner_factory_override is not None:
            return self.runner_factory_override(), []
        if not self.chk_real.isChecked():
            return VirtualClaudeRunner(), []
        from llterm.host.codex_runner import CodexRunner
        from llterm.host.loop import ClaudeRunner

        # API キーを外しサブスク認証で実走 + 選択した effort/model (--effort/--model) を付与
        claude = ClaudeRunner(use_subscription=True,
                              effort=str(self.cmb_effort.currentData() or ""),
                              model=str(self.cmb_model.currentData() or ""))
        # Gemini (agentic 奏者 = ファイル編集可) を fallback に。未インストールなら入れない。
        gemini = self._gemini_runner()
        primary: TurnRunner
        fallbacks: list[TurnRunner]
        if self._codex_is_primary():
            # Codex 主。無料 agent (Gemini) を Claude より先に、Claude を最後の保険に置く。
            primary = CodexRunner()
            fallbacks = ([gemini] if gemini else []) + [claude]
        else:
            primary = claude
            fallbacks = [CodexRunner()] if self.chk_codex_fallback.isChecked() else []
            if gemini is not None:
                fallbacks = [*fallbacks, gemini]
        # 無料奏者 (OpenAI 互換 = テキスト専用) は keep-alive 保険として最後尾。
        # APIキー未設定 (Ollama 以外) なら入れない = loop を auth 停止させない。
        free = self._free_runner()
        if free is not None and free.key_available():
            fallbacks = [*fallbacks, free]
        # レビュー奏者を選んでいれば、主奏者を OrchestraRunner で包む = 分業
        # (指揮者=主奏者が実装 → レビュー奏者が批評 → 指揮者が修正)。1 ターンに束ねるので
        # fallbacks (フェイルオーバー) はそのまま (主奏者が枯れたら従来どおり次の奏者へ)。
        reviewer = self._reviewer_runner()
        if reviewer is not None:
            from llterm.host.orchestra_runner import OrchestraRunner
            primary = OrchestraRunner(conductor=primary, reviewer=reviewer)
        return primary, fallbacks

    def _free_runner(self) -> OpenAICompatRunner | None:
        """選択中の無料奏者 (OpenAI 互換) runner、未選択なら None。"""
        key = str(self.cmb_free_provider.currentData() or "")
        if not key:
            return None
        from llterm.host.openai_compat_runner import OpenAICompatRunner
        return OpenAICompatRunner(provider=key)

    def _gemini_runner(self) -> GeminiRunner | None:
        """Gemini fallback が ON かつ gemini が PATH にあれば GeminiRunner、無ければ None。

        未インストールの gemini を chain に入れると毎ターン not_found で空転するため、
        事前に shutil.which で除外する (free 奏者の key_available と同じ fail-safe)。
        """
        if not self.chk_gemini_fallback.isChecked():
            return None
        if shutil.which("gemini") is None:
            return None
        from llterm.host.gemini_runner import GeminiRunner
        return GeminiRunner()

    def _reviewer_runner(self) -> TurnRunner | None:
        """選択中のレビュー奏者 runner、未選択/未導入/キー無は None (分業を組まない fail-safe)。"""
        key = str(self.cmb_reviewer.currentData() or "")
        if not key:
            return None
        if key == "codex":
            if shutil.which("codex") is None:
                return None
            from llterm.host.codex_runner import CodexRunner
            return CodexRunner()
        if key == "gemini":
            if shutil.which("gemini") is None:
                return None
            from llterm.host.gemini_runner import GeminiRunner
            return GeminiRunner()
        from llterm.host.openai_compat_runner import PROVIDERS, OpenAICompatRunner
        if key in PROVIDERS:
            rev = OpenAICompatRunner(provider=key)
            return rev if rev.key_available() else None
        return None

    def _build_runner(self) -> TurnRunner:
        """このランの primary runner (= provider chain の先頭) を返す。"""
        return self._resolve_providers()[0]

    # ---- 操作 ----
    @QtCore.Slot()
    def start_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        workdir = self._selected_workdir()
        if workdir is None or not workdir.is_dir():
            self._append(t("gui.msg.no_project"), PALETTE["err"])
            return
        real = self.runner_factory_override is None and self.chk_real.isChecked()
        runner, fallback_runners = self._resolve_providers()
        # 無料奏者を選んだのに APIキー未設定で chain に入らなかった場合は明示する (honest disclosure)
        free = self._free_runner()
        if real and free is not None and not free.key_available():
            self._append(t("gui.msg.free_player_no_key", provider=str(self.cmb_free_provider.currentData())),
                         PALETTE["err"])
        # Gemini fallback を選んだのに gemini 未インストールで除外された場合も明示する
        if real and self.chk_gemini_fallback.isChecked() and shutil.which("gemini") is None:
            self._append(t("gui.msg.gemini_not_installed"), PALETTE["err"])
        loop_kw = dict(self.loop_kw)
        loop_kw["max_sessions"] = self.spin_sessions.value()  # 常に上限つき (暴走/レート保護)
        loop_kw["threshold"] = self.spin_threshold.value()
        loop_kw["window_tokens"] = self.spin_window.value()
        self._max_sessions = loop_kw["max_sessions"]  # ステータス表示 (session N/max) 用
        self._run_workdir = workdir  # rotate 時に docs/SESSION_SUMMARY.md を読むため保持
        self.lbl_progress.setText(t("gui.progress.starting"))
        self._refresh_summary()  # 開始時に既存の handoff サマリを表示
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
        # provider chain は _resolve_providers() が決定済み (Codex 優先/テンプレ別/フォールバック)。
        # 仮想モードや override では fallback_runners は空。
        ledger_path = workdir / ".llterm" / "loop_ledger.jsonl"
        self.worker = LoopWorker(
            runner=runner, workdir=workdir, ledger_path=ledger_path, loop_kw=loop_kw,
            fallback_runners=fallback_runners,
        )
        self.worker.event.connect(self._on_event)
        self.worker.stream.connect(self._on_stream)  # ターン内リアルタイム表示
        self.worker.finished_outcome.connect(self._on_finished)
        self._streamed_text = 0
        self.worker.start()
        self._stopping = False
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText(t("gui.btn.stop"))
        for widget in self._run_widgets:
            widget.setEnabled(False)
        # cost 種別 (課金なし / 実課金) を確定し、idle 表示を更新
        self._cost_suffix, self._cost_billed = self._cost_label_mode(runner)
        self._set_cost(0.0)
        effort = str(self.cmb_effort.currentData() or "")
        if real:
            mode = t("gui.mode.real_billed") if self._cost_billed else t("gui.mode.real_subscription")
        else:
            mode = t("gui.mode.virtual")
        # 実モデルは init イベントで確定するが、主プロバイダ/選択値を暫定表示する。
        # Codex 主のランは effort/Claude モデルが無関係なので "codex" と出す (混乱回避)。
        codex_primary = real and self._codex_is_primary()
        effort_note = f" effort={effort}" if real and effort and not codex_primary else ""
        if codex_primary:
            model_hint = "codex"
        else:
            model_sel = str(self.cmb_model.currentData() or "")
            model_hint = model_sel if real and model_sel else "…"
        self.lbl_model.setText(
            f"model: {model_hint}{('  effort=' + effort) if real and effort and not codex_primary else ''}")
        self.lbl_state.setText(t("gui.state.running", mode=mode, template=tmpl.key))
        self._run_effort = effort if real else ""  # init で model と併記するため保持
        self._append(t("gui.msg.loop_start", mode=mode, template=tmpl.key, workdir=workdir,
                       max_sessions=loop_kw["max_sessions"], effort_note=effort_note),
                     PALETTE["session"], bold=True, ts=True)
        self._save_settings()  # クラッシュしても Start 時点の設定が次回復元される

    @QtCore.Slot()
    def stop_loop(self) -> None:
        """1 回目: graceful 停止 (作業記録を残してから停止、完了まで砂時計)。2 回目: 強制停止。"""
        if self.worker is None or not self.worker.isRunning():
            return
        if not self._stopping:
            self._stopping = True
            self.worker.request_stop(force=False)  # 現ターン完了後に handoff を残して停止
            self.lbl_state.setText(t("gui.state.stopping"))
            self.btn_stop.setText(t("gui.btn.force_stop"))
            self._set_busy_cursor(True)
            self._append(t("gui.msg.stop_graceful"), PALETTE["rotate"], ts=True)
        else:
            self.worker.request_stop(force=True)  # 実行中ターンを即 kill (記録なし)
            self.lbl_state.setText("force stopping…")
            self._append(t("gui.msg.stop_force"), PALETTE["err"], ts=True)

    @QtCore.Slot()
    def send_input(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._append(t("gui.msg.inject_accepted", text=text), PALETTE["inject"], ts=True)
        if self.worker is not None and self.worker.isRunning():
            self.worker.inject(text)
        else:
            self._append(t("gui.msg.inject_pending"), PALETTE["dim"])

    @QtCore.Slot()
    def _on_template_changed(self) -> None:
        key = self.cmb_template.currentData()
        if key is None:
            return
        tmpl = templates.get(key)
        self.cmb_template.setToolTip(tmpl.description)  # 用途をツールチップで表示
        self.edit_param.setEnabled(tmpl.needs_param)
        self.edit_param.setPlaceholderText(
            tmpl.param_label if tmpl.needs_param else t("gui.placeholder.param_unused"))

    @QtCore.Slot()
    def _promote_clicked(self) -> None:
        domain = self.edit_param.text().strip()
        if not domain:
            self._append(t("gui.msg.promote_need_domain"), PALETTE["err"])
            return
        stg = rad.staging_dir(domain, self.rad_docs_root)
        if not stg.is_dir():
            self._append(t("gui.msg.promote_no_staging", staging=stg), PALETTE["err"])
            return
        reply = QtWidgets.QMessageBox.question(
            self, t("gui.dialog.promote.title"),
            t("gui.dialog.promote.body", domain=domain, staging=stg,
              live=rad.live_dir(domain, self.rad_docs_root)),
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self._do_promote(domain)

    def _do_promote(self, domain: str) -> None:
        try:
            res = rad.promote(domain, docs_root=self.rad_docs_root)
        except rad.RadError as exc:
            self._append(t("gui.msg.promote_failed", error=exc), PALETTE["err"])
            return
        msg = t("gui.msg.promoted", live=res.live)
        if res.backup:
            msg += f" (backup: {res.backup})"
        self._append(msg, PALETTE["inject"])

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override 名)
        """× で閉じる際、ループ実行中なら確認ダイアログを出す (安全設計)。

        「はい」→ graceful 停止 (作業内容を記録) してから閉じる。記録完了は _on_finished が
        検知し、そこで実際に close() する (記録中はウィンドウを開いたまま砂時計表示)。
        「いいえ」→ 閉じない。実行中でなければそのまま閉じる。
        """
        if self.worker is not None and self.worker.isRunning():
            if self._closing_after_stop:
                event.ignore()  # 既に graceful 停止中 — 二重ダイアログを出さない
                return
            reply = QtWidgets.QMessageBox.question(
                self, t("gui.dialog.close.title"), t("gui.dialog.close.body"),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing_after_stop = True  # 記録完了後に _on_finished が close() する
            event.ignore()
            self.stop_loop()  # graceful 停止 (handoff + 砂時計)
            return
        try:
            self._save_settings()  # 最後の設定を次回起動時に復元する
        except Exception:  # noqa: BLE001
            pass
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
                self._set_progress(text)  # 直近応答を進捗バーに (ライブ)
        elif kind == "thinking":
            self._append(f"{prefix}… thinking … {item.get('preview') or ''}", PALETTE["dim"])
        elif kind == "tool_use":
            detail = str(item.get("detail") or "")
            line = f"{prefix}⚙ {item.get('name')}" + (f": {detail}" if detail else "")
            self._append(line, PALETTE["dim"] if sub else PALETTE["tool"])
        elif kind == "tool_result":
            preview = str(item.get("preview") or "")
            if item.get("is_error"):
                self._append(prefix + t("gui.stream.tool_error", preview=preview), PALETTE["err"])
            elif preview:
                self._append(f"{prefix}  ↳ {preview}", PALETTE["dim"])
        elif kind == "rate_limit":
            status = str(item.get("status") or "")
            if status and status != "allowed":  # サブスク自走の主制約 — 制限時は必ず可視化する
                resets_at = int(item.get("resets_at") or 0)
                when = ""
                if resets_at > 0:
                    try:
                        when = t("gui.stream.rate_limit_reset",
                                 time=f"{datetime.fromtimestamp(resets_at):%m-%d %H:%M}")
                    except (OSError, OverflowError, ValueError):
                        pass
                self._append(t("gui.stream.rate_limit", status=status, when=when),
                             PALETTE["err"], bold=True, ts=True)
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
                return t("gui.cost.subscription"), False
            return t("gui.cost.billed"), True
        return t("gui.cost.virtual"), False

    def _set_cost(self, amount: float) -> None:
        """cost ラベルを更新する。実課金時のみ赤字で警告する。"""
        self.lbl_cost.setText(f"cost({self._cost_suffix}): ${amount:.4f}")
        self.lbl_cost.setStyleSheet(f"color:{PALETTE['err']};font-weight:bold"
                                    if self._cost_billed else "")

    def _set_busy_cursor(self, on: bool) -> None:
        """砂時計 (待機) カーソルの ON/OFF。set/restore のバランスを保つ。"""
        if on and not self._busy_cursor:
            QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
            self._busy_cursor = True
        elif not on and self._busy_cursor:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._busy_cursor = False

    def _set_progress(self, text: str, *, prefix: str | None = None) -> None:
        """進捗バーを 1 行要約で更新する (全文はツールチップ)。空テキストは無視。"""
        if prefix is None:
            prefix = t("gui.progress.prefix")
        line = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
        if not line:
            return
        short = line if len(line) <= 140 else line[:139] + "…"
        self.lbl_progress.setText(f"{prefix}: {short}")
        self.lbl_progress.setToolTip(str(text).strip()[:2000])

    def _read_session_summary_full(self, workdir: Path | None) -> str:
        """workdir の docs/SESSION_SUMMARY.md 全文を返す (無ければ空)。"""
        if workdir is None:
            return ""
        try:
            return (Path(workdir) / "docs" / "SESSION_SUMMARY.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _read_session_summary(self) -> str:
        """実行中 workdir の docs/SESSION_SUMMARY.md 先頭の意味ある 2 行 (status bar の handoff 用)。"""
        text = self._read_session_summary_full(self._run_workdir)
        lines = [ln.strip().lstrip("# ").strip() for ln in text.splitlines() if ln.strip()]
        return " / ".join(lines[:2])

    @QtCore.Slot()
    def _refresh_summary(self) -> None:
        """進捗サマリ パネルを更新する (スクロール位置は保持)。

        既定は人間向けダイジェスト (現在地/直近の成果/次の一手)。「生」トグル ON で
        SESSION_SUMMARY.md 全文。要約は純関数 summarize_for_human に委譲 (Qt 非依存)。
        """
        wd = self._run_workdir or self._selected_workdir()
        full = self._read_session_summary_full(wd)
        if self.chk_summary_raw.isChecked():
            text = full
        else:
            from llterm.summary import summarize_for_human
            text = summarize_for_human(full)
        bar = self.summary_view.verticalScrollBar()
        pos = bar.value()
        self.summary_view.setPlainText(text)  # 空なら placeholder が出る
        bar.setValue(min(pos, bar.maximum()))  # 読んでいた位置を維持 (rotate 更新で飛ばさない)

    @QtCore.Slot(str, dict)
    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "session_start":
            idx = data.get("session_index")
            sid = str(data.get("session_id", ""))[:8]
            self.lbl_session.setText(self._session_label(idx))
            self.ctx_bar.setValue(0)  # 新セッションは fresh context = 0%
            self._append("\n" + t("gui.msg.session_start", label=self._session_label(idx), sid=sid),
                         PALETTE["session"], bold=True, ts=True)
            self._streamed_text = 0
        elif kind == "handoff":
            # 停止前の作業記録ターン。砂時計のまま「記録中」を明示する。
            self.lbl_state.setText(t("gui.state.handoff"))
            self._set_busy_cursor(True)
            self._append(t("gui.msg.handoff"), PALETTE["rotate"], ts=True)
        elif kind == "rate_limited":
            # レート制限到達 → resetsAt まで待機して自動再開 (待機中も Stop 可)。
            resets = int(data.get("resets_at") or 0)
            when = ""
            if resets > 0:
                try:
                    when = t("gui.when.until", time=f"{datetime.fromtimestamp(resets):%m-%d %H:%M}")
                except (OSError, OverflowError, ValueError):
                    pass
            self.lbl_state.setText(t("gui.state.rate_limited", when=when))
            self._append(t("gui.msg.rate_limited_wait", when=when),
                         PALETTE["err"], bold=True, ts=True)
        elif kind == "rate_limit_resumed":
            prov = data.get("provider")
            self.lbl_state.setText(t("gui.state.resumed"))
            self._append(t("gui.msg.resumed_with", provider=prov) if prov
                         else t("gui.msg.resumed"), PALETTE["inject"], ts=True)
        elif kind == "provider_switch":
            prov = str(data.get("provider") or "?")
            self.lbl_model.setText(t("gui.model.switched", provider=prov))
            self._append(t("gui.msg.provider_switch", provider=prov),
                         PALETTE["rotate"], bold=True, ts=True)
        elif kind == "task":
            # これから claude に送る指令。時刻を出して「指令時 → 応答受信時」の経過を見せる。
            if data.get("injected"):
                prompt = str(data.get("prompt") or "").strip()
                self._append(t("gui.msg.task_injected", prompt=prompt),
                             PALETTE["inject"], bold=True, ts=True)
            else:
                self._append(t("gui.msg.task_sent", turn=data.get("turn")), PALETTE["dim"], ts=True)
        elif kind == "turn":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self.ctx_bar.setValue(min(pct, 100))
            self._set_cost(float(data.get("total_cost", 0.0)))
            self.lbl_session.setText(self._session_label(data.get("session_index"), data.get("turn")))
            err = data.get("error_kind")
            head = t("gui.msg.turn_head", turn=data.get("turn"), pct=pct,
                     err_note=f"  ERR={err}" if err else "")
            self._append(head, PALETTE["err"] if err else PALETTE["turn"], bold=bool(err), ts=True)
            text = str(data.get("text") or "")
            # ストリーム済みなら再表示しない (二重表示防止)。ただしエラーターンの text は
            # エラー詳細がストリームに乗らないことがあるため常に表示する。
            if text and (self._streamed_text == 0 or err):
                self._append(text, PALETTE["err"] if err else None)
            if text:
                self._set_progress(text)  # ターン最終応答を進捗バーに (確定値)
            self._streamed_text = 0
        elif kind == "rotate":
            pct = int(round(float(data.get("used_pct", 0.0)) * 100))
            self._append(t("gui.msg.rotate", pct=pct), PALETTE["rotate"], ts=True)
            self._streamed_text = 0
            summary = self._read_session_summary()  # exit準備で更新された handoff を進捗に反映
            if summary:
                self._set_progress(summary, prefix=t("gui.progress.handoff_prefix"))
            self._refresh_summary()  # 進捗サマリ パネルも最新 handoff に更新
        elif kind == "stopped":
            self._append(
                f"\n=== stopped: {data.get('stop_reason')} "
                f"(sessions={data.get('sessions')}, turns={data.get('turns')}, "
                f"cost({self._cost_suffix})=${float(data.get('total_cost', 0.0)):.4f}) ===",
                PALETTE["session"], bold=True, ts=True,
            )

    @QtCore.Slot(dict)
    def _on_finished(self, outcome: dict) -> None:
        reason = outcome.get("stop_reason")
        self._set_busy_cursor(False)  # 砂時計を解除 (graceful 停止/記録の完了)
        self._stopping = False
        self.lbl_state.setText(f"done: {reason}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText(t("gui.btn.stop"))
        for widget in self._run_widgets:
            widget.setEnabled(True)
        if reason == "auth_required":
            self._append(t("gui.msg.auth_required"), PALETTE["err"], bold=True)
        if self._closing_after_stop:
            self._closing_after_stop = False
            self.close()  # × 終了確認で予約された閉じる操作を、記録完了後に実行


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
    parser.add_argument("--model", default=None,
                        help="実 claude のモデル (alias: opus/sonnet/haiku、またはフル ID。"
                             "'' で claude 保存既定。token 節約は sonnet/haiku。既定: 前回値→Opus 4.8)")
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
        model_default=args.model,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=args.max_sessions,
        max_total_cost_usd=args.max_cost,
    )
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
