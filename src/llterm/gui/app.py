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
from llterm.ctl.consumer import CtlConsumer
from llterm.ctl.ledger import Ledger
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand
from llterm.gui import settings as gui_settings
from llterm.i18n import t
from llterm.gui.settings import DEFAULT_SETTINGS_PATH
from llterm.gui.termlog import TerminalLog
from llterm.gui.virtual import VirtualClaudeRunner
from llterm.gui.worker import LoopWorker
from llterm.host import loop as loop_mod
from llterm.host.loop import TurnRunner, _ensure_utf8_stdout

if TYPE_CHECKING:
    from llterm.host.gemini_runner import GeminiRunner

DEFAULT_PROJECTS_ROOT = Path("D:/projects")
_PROJECT_MARKERS = (".git", "pyproject.toml", "CLAUDE.md", "package.json", "Cargo.toml")
_CTL_POLL_MS = 1500  # ctl queue を走行中にポーリングする間隔 (ms)

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


# レビュー奏者パネルの候補 (key, 表示ラベル)。"claude" = 責任者と同系のダブルチェックも許容。
REVIEWER_CHOICES: tuple[tuple[str, str], ...] = (
    ("claude", "Claude"), ("codex", "Codex"), ("gemini", "Gemini CLI"),
    ("gemini-api", "Gemini API"), ("groq", "Groq"), ("cerebras", "Cerebras"),
    ("openrouter", "OpenRouter"), ("ollama", "Ollama"),
)
_REVIEWER_KEYS = {key for key, _ in REVIEWER_CHOICES}


def _coerce_reviewers(reviewers: object, legacy_reviewer: object) -> list[str]:
    """保存値からレビュー奏者パネルの選択 key リストを復元する (fail-safe + migrate)。

    優先: 新形式 "reviewers" (list) → 旧単一 "reviewer" (str) を [値] へ migrate → 既定 ["claude"]。
    未知の key は黙って捨てる (手編集/将来削除に耐える)。空に畳まれたら既定 ["claude"]。
    """
    keys: list[str] = []
    if isinstance(reviewers, (list, tuple)):
        for k in reviewers:
            if isinstance(k, str) and k in _REVIEWER_KEYS and k not in keys:
                keys.append(k)
        return keys or ["claude"]
    if isinstance(legacy_reviewer, str) and legacy_reviewer:
        return [legacy_reviewer] if legacy_reviewer in _REVIEWER_KEYS else ["claude"]
    return ["claude"]


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
        # ターミナル表示のローテーションログ (1 時間単位・1 週間保持・行単位 append・fail-safe)。
        # 全 _append より前に生成する (settings と同じ ~/.llterm/ 配下 logs/)。
        self._termlog = TerminalLog(self.settings_path.parent / "logs")
        self._effort_cli = effort_default  # CLI 明示指定 (None = 未指定 → 保存値/既定に委ねる)
        self._model_cli = model_default  # CLI 明示指定 (None = 未指定 → 保存値/既定に委ねる)
        self.loop_kw = dict(loop_kw)
        self.worker: LoopWorker | None = None
        # ctl queue consumer: Claude (emit CLI) が投函した inject-task を GUI 手動注入と同じ
        # 経路 (worker.inject) へ流す。走行中だけ QTimer で poll する (ccr→llterm 注入の欠落配線解消)。
        self._ctl_consumer: CtlConsumer | None = None
        self._ctl_timer = QtCore.QTimer(self)
        self._ctl_timer.setInterval(_CTL_POLL_MS)
        self._ctl_timer.timeout.connect(self._ctl_tick)
        self._streamed_text = 0  # 現ターン中にリアルタイム表示した応答数 (turn 完了時の二重表示防止)
        self._max_sessions = 0  # ステータス表示 (session N/max) 用。Start 時に確定
        self._cost_suffix = t("gui.cost.reported")  # cost ラベルの種別 (Start 時に課金有無で確定)
        self._cost_billed = False  # True = 実課金 (API キー)。サブスク/仮想は False
        self._run_effort = ""  # 実行中の effort (init イベントで model と併記)
        self._run_workdir: Path | None = None  # 実行中の workdir (SESSION_SUMMARY 読取用)
        self._stopping = False  # graceful 停止要求中 (2 回目 Stop で force kill)
        self._busy_cursor = False  # 砂時計カーソル表示中か (set/restore のバランス管理)
        self._closing_after_stop = False  # × 終了確認で graceful 停止 → 完了後に閉じる予約
        # 選択ダイアログのファクトリ (テストはスタブを差し込む。既定 = 実 ChoiceDialog)。
        self._choice_dialog_factory: Callable[[object], object] | None = None
        self._choice_active = False  # ダイアログ表示中の再入防止 (連続検知で多重に出さない)

        # 前回設定の復元: CLI 明示指定 > 保存値 > 組込み既定
        saved = gui_settings.load_settings(self.settings_path)
        if workdir is None and saved.get("workdir"):
            wd = Path(str(saved["workdir"]))
            workdir = wd if wd.is_dir() else None  # 消えたプロジェクトは復元しない (fail-safe)
        real_default = real_default or bool(saved.get("real", False))
        rad_default = rad_default or bool(saved.get("rad", False))
        offload_default = bool(saved.get("offload", True))  # 既定 ON = 必要なら自動オフロード
        autonomy_default = bool(saved.get("autonomy", False))
        # codex_first 既定: 保存値があればそれ。無ければ codex が導入済みなら True
        # (= 既定で Codex を主奏者にする)。理由 (2026-06-15 Anthropic 課金変更): claude -p 等の
        # ヘッドレス自律利用はサブスク枠から分離され API 実費課金 (繰越なし) になったため、
        # 自走でトークンを大量消費する用途は ChatGPT Pro 固定枠で動く Codex に寄せるのがコスト最適。
        # Claude は引き続き選択可 (トグル OFF / レビュー奏者 / 責任者) で chain backbone に常駐。
        # codex 未導入環境では False に倒し Codex 主の空転を防ぐ (可用性ガード)。
        if "codex_first" in saved:
            codex_first_default = bool(saved.get("codex_first"))
        else:
            codex_first_default = shutil.which("codex") is not None
        # レビュー奏者パネル (multi-select)。旧単一 "reviewer" 文字列があり "reviewers" 無なら
        # [旧値] (空なら既定 ["claude"]) へ migrate する。既定 = ["claude"] (Codex 実装 + Claude レビュー)。
        reviewers_default = _coerce_reviewers(saved.get("reviewers"), saved.get("reviewer"))
        factcheck_default = str(saved.get("factchecker") or "")
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
                       offload_default=offload_default,
                       template_default=template_default,
                       autonomy_default=autonomy_default,
                       param_default=str(saved.get("param") or ""),
                       effort_default=effort_default,
                       model_default=model_default,
                       codex_first_default=codex_first_default,
                       reviewers_default=reviewers_default,
                       factcheck_default=factcheck_default,
                       summary_raw_default=bool(saved.get("summary_raw", False)))
        geo = saved.get("geometry")
        if isinstance(geo, str) and geo:  # ウィンドウ位置/サイズの復元 (壊れた値は無視)
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(geo.encode("ascii")))
            except (ValueError, TypeError):
                pass
        # 起動時にも Gemini CLI 無料枠期限を通知 (Gemini CLI を選択中のときだけ)
        startup_note = self._gemini_cli_deadline_note()
        if startup_note:
            self._append(startup_note, PALETTE["err"], bold=True)

    # ---- UI 構築 ----
    def _build_ui(self, *, initial_workdir: Path | None, real_default: bool, rad_default: bool,
                  offload_default: bool = True,
                  template_default: str, autonomy_default: bool = False,
                  param_default: str = "", effort_default: str = "max",
                  model_default: str = loop_mod.DEFAULT_MODEL,
                  codex_first_default: bool = False,
                  reviewers_default: list[str] | None = None,
                  factcheck_default: str = "",
                  summary_raw_default: bool = False) -> None:
        self.setWindowTitle(t("gui.window.title"))
        icon = find_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)  # タイトルバー/タスクバーのアイコン

        # 狭幅 (スマホ Remote Desktop) 再構成:
        #   メイン窓 = 操作バー (Start/Stop + 状態 + ⚙Settings + project + max-sessions) +
        #              タスク注入欄 (上寄り) + 出力ログ/進捗サマリ の縦 QSplitter。
        #   Settings ダイアログ = 滅多に変えない設定一式 (実行モード/閾値/モデル/テンプレ/
        #              レビュー奏者 等) を別画面 (QScrollArea + 縦積み) へ分離。
        # ★ ウィジェットは全て self.X のまま唯一の状態源。配置だけ Settings ダイアログへ移す
        #   (Apply/reject の概念は導入しない。Start は常に self.X.* を live に読む)。
        # ★ 復元値を流す setValue/setChecked/setCurrentIndex は QSignalBlocker で囲み、
        #   構築中の signal 副作用 (provider 解決 / 再保存) を防ぐ。
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.StyleHint.Monospace)

        # まず全ウィジェットを生成し、復元値は signal を遮断して流し込む。
        self._create_widgets(initial_workdir=initial_workdir, real_default=real_default,
                             rad_default=rad_default, offload_default=offload_default,
                             autonomy_default=autonomy_default, codex_first_default=codex_first_default,
                             reviewers_default=reviewers_default or ["claude"],
                             factcheck_default=factcheck_default, effort_default=effort_default,
                             model_default=model_default, summary_raw_default=summary_raw_default,
                             mono=mono)

        # ── メイン窓 (狭幅 first・最小) ─────────────────────────────
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)

        # 操作バー: Start / Stop / Send + ⚙Settings + project + max-sessions。
        # (project と max-sessions は頻繁に使うのでメイン窓に残す = ユーザー指定)。
        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(self.btn_start)
        op_row.addWidget(self.btn_stop)
        op_row.addWidget(self.btn_send)
        op_row.addWidget(self.btn_emergency)
        op_row.addWidget(self.btn_settings)
        op_row.addWidget(QtWidgets.QLabel(t("gui.label.project")))
        op_row.addWidget(self.cmb_project, 1)
        op_row.addWidget(QtWidgets.QLabel(t("gui.label.max_sessions")))
        op_row.addWidget(self.spin_sessions)
        vbox.addLayout(op_row)

        # 承認確認不要 (完全自律) — 走行中も自由に ON/OFF できる生きたトグル (メイン窓に常設)。
        # タスク注入で自動 OFF (監督モード = AI が確認事項を出せる)、確認回答後に自動 ON (ループ復帰)。
        auto_row = QtWidgets.QHBoxLayout()
        auto_row.addWidget(self.chk_autonomy)
        auto_row.addStretch(1)
        vbox.addLayout(auto_row)

        # ステータス行 — 常時見える状態 (状態 / model / session 進捗 / context 使用率 / cost)
        status_row = QtWidgets.QHBoxLayout()
        status_row.addWidget(self.lbl_state)
        status_row.addWidget(self.lbl_model)
        status_row.addWidget(self.lbl_session)
        status_row.addWidget(self.ctx_bar, 1)
        status_row.addWidget(self.lbl_cost)
        vbox.addLayout(status_row)

        # タスク注入欄 — 操作バー直下 (上寄り)。狭幅 + 仮想キーボードで画面下が隠れても
        # 注入欄が見えるように最下部固定をやめる。フォーカス時に見える化する。
        vbox.addWidget(self.input)

        # 出力ログ (主) と 進捗サマリ タブ (下部) を縦 QSplitter で積む (狭幅では横並びを避ける)。
        self.split_main = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.split_main.addWidget(self.output)
        self.split_main.addWidget(self.summary_tabs)
        self.split_main.setChildrenCollapsible(False)  # どちらの pane も潰さない
        self.output.setMinimumHeight(120)
        self.summary_tabs.setMinimumHeight(80)
        self.split_main.setStretchFactor(0, 3)  # ログを広めに
        self.split_main.setStretchFactor(1, 2)
        self.split_main.setSizes([380, 200])
        vbox.addWidget(self.split_main, 1)

        # 進捗バー (画面下部・邪魔にならない定位置)。完全自律時に「今どこまで進んだか」を
        # 直近応答の 1 行要約で常時表示する。ホバーで全文 (応答 / handoff サマリ)。
        self.lbl_progress = QtWidgets.QLabel(t("gui.progress.idle"))
        self.lbl_progress.setToolTip(t("gui.tip.progress"))
        self.statusBar().addWidget(self.lbl_progress, 1)

        # ── Settings ダイアログ (別画面・非モーダル・強参照で保持) ─────
        self._build_settings_dialog()

        self.resize(560, 720)  # 狭幅 first (縦長): スマホ Remote Desktop を想定
        self.setMinimumWidth(320)

        # 結線 (ウィジェットは生成済み)。
        self.btn_start.clicked.connect(self.start_loop)
        self.btn_stop.clicked.connect(self.stop_loop)
        self.btn_send.clicked.connect(self.send_input)
        self.btn_emergency.clicked.connect(self.emergency_inject)
        self.btn_settings.clicked.connect(self._open_settings)
        self.btn_publish.clicked.connect(self._promote_clicked)
        self.chk_summary_raw.toggled.connect(self._refresh_summary)
        self.chk_autonomy.toggled.connect(self._on_autonomy_toggled)  # 走行中も即反映
        self.btn_refresh_summary.clicked.connect(self._refresh_summary)
        self.cmb_template.currentIndexChanged.connect(self._on_template_changed)
        # Enter=改行のまま / 送信は Ctrl+Enter (R12: 誤送信の構造的防止)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Return"), self.input, self.send_input)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Enter"), self.input, self.send_input)
        # プロジェクトを選び直したら、その既存 SESSION_SUMMARY をプレビュー (idle 時)
        self.cmb_project.currentIndexChanged.connect(self._refresh_summary)

        # 注入欄フォーカス時に見える化 (狭幅 + 仮想キーボードで隠れる対策)。
        self.input.installEventFilter(self)

        # テンプレ既定を復元 (signal を遮断 → 構築中の副作用なし)。値を流してから
        # _on_template_changed() を明示呼びして tooltip / param 有効化を初期化する。
        _tidx = self.cmb_template.findData(template_default)
        with QtCore.QSignalBlocker(self.cmb_template):
            self.cmb_template.setCurrentIndex(_tidx if _tidx >= 0 else 0)
        self._on_template_changed()  # 初期 tooltip / param 有効化
        if param_default:
            with QtCore.QSignalBlocker(self.edit_param):
                self.edit_param.setText(param_default)  # 前回のテンプレ引数を復元
        self._refresh_summary()  # 初期表示 (選択プロジェクトの既存サマリ)

        # 起動時 one-shot 入力プリフィル: ~/.llterm/startup_input.txt があれば EditBox へ
        # 流し込み、ファイルは即消費する (clear-on-load)。外部 (ccr) がこのファイルに指示文を
        # 書いておくだけで、次回起動時に入力欄へ入った状態になり、消費後は再発火しない。
        _prefill = gui_settings.consume_startup_input(self.settings_path)
        if _prefill:
            self.input.setPlainText(_prefill)

        # 走行中に無効化する設定系ウィジェット (途中変更で worker と不整合にしない)
        self._run_widgets: list[QtWidgets.QWidget] = [
            self.cmb_project, self.chk_real, self.chk_rad, self.chk_offload, self.spin_sessions,
            self.spin_threshold, self.spin_window, self.spin_maxcost,
            self.cmb_template, self.edit_param, self.btn_publish,
            self.cmb_effort, self.cmb_model, self.chk_codex_first, self.cmb_factcheck,
            *self.chk_reviewers.values(),
        ]

    def _create_widgets(self, *, initial_workdir: Path | None, real_default: bool,
                        rad_default: bool, offload_default: bool, autonomy_default: bool,
                        codex_first_default: bool, reviewers_default: list[str],
                        factcheck_default: str, effort_default: str, model_default: str,
                        summary_raw_default: bool, mono: QtGui.QFont) -> None:
        """全ウィジェットを生成し復元値を流す。配置 (メイン窓 / Settings) は呼び出し側が行う。

        ★ 復元値の流し込み (setValue/setChecked/setCurrentIndex/setText) は QSignalBlocker で
        囲み、構築中に provider 解決や再保存が走る副作用を防ぐ (Codex レビュー指摘)。
        """
        # 操作系 (メイン窓に残る): project / max-sessions / Start・Stop・Send・Settings。
        self.cmb_project = QtWidgets.QComboBox()
        self.cmb_project.setMinimumWidth(220)
        with QtCore.QSignalBlocker(self.cmb_project):
            self._populate_projects(initial_workdir)
        self.spin_sessions = QtWidgets.QSpinBox()
        self.spin_sessions.setRange(1, 100000)
        default_sessions = self.loop_kw.get("max_sessions")
        with QtCore.QSignalBlocker(self.spin_sessions):
            self.spin_sessions.setValue(int(default_sessions) if default_sessions else 8)
        self.btn_start = QtWidgets.QPushButton(t("gui.btn.start"))
        self.btn_stop = QtWidgets.QPushButton(t("gui.btn.stop"))
        self.btn_stop.setEnabled(False)
        self.btn_send = QtWidgets.QPushButton(t("gui.btn.send"))
        self.btn_emergency = QtWidgets.QPushButton(t("gui.btn.emergency"))
        self.btn_emergency.setToolTip(t("gui.tip.emergency"))
        self.btn_settings = QtWidgets.QPushButton(t("gui.btn.settings"))
        self.btn_settings.setToolTip(t("gui.tip.settings"))

        # 設定系 (Settings ダイアログへ移す): 実行モード/閾値/モデル/effort/オフロード/自律。
        self.chk_real = QtWidgets.QCheckBox(t("gui.check.real"))
        self.chk_real.setToolTip(t("gui.tip.real"))
        self.chk_rad = QtWidgets.QCheckBox(t("gui.check.rad"))
        self.chk_rad.setToolTip(t("gui.tip.rad"))
        self.chk_offload = QtWidgets.QCheckBox(t("gui.check.offload"))
        self.chk_offload.setToolTip(t("gui.tip.offload"))
        self.chk_autonomy = QtWidgets.QCheckBox(t("gui.check.autonomy"))
        self.chk_autonomy.setToolTip(t("gui.tip.autonomy"))
        self.chk_codex_first = QtWidgets.QCheckBox(t("gui.check.codex_first"))
        self.chk_codex_first.setToolTip(t("gui.tip.codex_first"))
        for cb, val in ((self.chk_real, real_default), (self.chk_rad, rad_default),
                        (self.chk_offload, offload_default), (self.chk_autonomy, autonomy_default),
                        (self.chk_codex_first, codex_first_default)):
            with QtCore.QSignalBlocker(cb):
                cb.setChecked(val)

        self.spin_threshold = QtWidgets.QDoubleSpinBox()
        self.spin_threshold.setRange(0.10, 0.95)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setDecimals(2)
        self.spin_threshold.setToolTip(t("gui.tip.threshold"))
        with QtCore.QSignalBlocker(self.spin_threshold):
            self.spin_threshold.setValue(float(self.loop_kw.get("threshold") or 0.70))
        self.spin_window = QtWidgets.QSpinBox()
        self.spin_window.setRange(10_000, 2_000_000)
        self.spin_window.setSingleStep(10_000)
        self.spin_window.setGroupSeparatorShown(True)
        self.spin_window.setToolTip(t("gui.tip.window_tokens"))
        with QtCore.QSignalBlocker(self.spin_window):
            self.spin_window.setValue(int(self.loop_kw.get("window_tokens") or 200_000))
        self.spin_maxcost = QtWidgets.QDoubleSpinBox()
        self.spin_maxcost.setRange(0.0, 100000.0)
        self.spin_maxcost.setDecimals(2)
        self.spin_maxcost.setSingleStep(1.0)
        self.spin_maxcost.setToolTip(t("gui.tip.max_cost"))
        _mc = self.loop_kw.get("max_total_cost_usd")
        with QtCore.QSignalBlocker(self.spin_maxcost):
            self.spin_maxcost.setValue(float(_mc) if _mc else 0.0)

        self.cmb_effort = QtWidgets.QComboBox()
        for level in loop_mod.EFFORT_LEVELS:
            self.cmb_effort.addItem(level or t("gui.effort.default_item"), level)
        self.cmb_effort.setToolTip(t("gui.tip.effort"))
        _eidx = self.cmb_effort.findData(effort_default if effort_default in loop_mod.EFFORT_LEVELS
                                         else "max")
        with QtCore.QSignalBlocker(self.cmb_effort):
            self.cmb_effort.setCurrentIndex(_eidx if _eidx >= 0 else 0)
        self.cmb_model = QtWidgets.QComboBox()
        for m in loop_mod.MODEL_CHOICES:
            self.cmb_model.addItem(m or t("gui.model.default_item"), m)
        # 保存値が候補外 (手編集の独自モデル等) なら DEFAULT_MODEL の位置へ落とす (fail-safe)
        _midx = self.cmb_model.findData(model_default if model_default in loop_mod.MODEL_CHOICES
                                        else loop_mod.DEFAULT_MODEL)
        self.cmb_model.setToolTip(t("gui.tip.model_select"))
        with QtCore.QSignalBlocker(self.cmb_model):
            self.cmb_model.setCurrentIndex(_midx if _midx >= 0 else 0)

        # レビュー奏者パネル (複数) + 真偽確認奏者 (任意・単一)。
        self.chk_reviewers: dict[str, QtWidgets.QCheckBox] = {}
        for _key, _label in REVIEWER_CHOICES:
            cb = QtWidgets.QCheckBox(_label)
            cb.setToolTip(t("gui.tip.review_panel"))
            with QtCore.QSignalBlocker(cb):
                cb.setChecked(_key in reviewers_default)
            self.chk_reviewers[_key] = cb
        self.cmb_factcheck = QtWidgets.QComboBox()
        self.cmb_factcheck.addItem(t("gui.factcheck.none"), "")
        for _key, _label in (("perplexity", "Perplexity"),):
            self.cmb_factcheck.addItem(_label, _key)
        self.cmb_factcheck.setToolTip(t("gui.tip.factcheck"))
        _fcidx = self.cmb_factcheck.findData(factcheck_default)
        with QtCore.QSignalBlocker(self.cmb_factcheck):
            self.cmb_factcheck.setCurrentIndex(_fcidx if _fcidx >= 0 else 0)

        # テンプレ + RAD 公開ゲート。
        self.cmb_template = QtWidgets.QComboBox()
        for i, tmpl in enumerate(templates.TEMPLATES):
            self.cmb_template.addItem(tmpl.label, tmpl.key)
            self.cmb_template.setItemData(i, tmpl.description, QtCore.Qt.ItemDataRole.ToolTipRole)
        self.edit_param = QtWidgets.QLineEdit()
        self.edit_param.setPlaceholderText(t("gui.placeholder.param"))
        self.btn_publish = QtWidgets.QPushButton(t("gui.btn.publish"))
        self.btn_publish.setToolTip(t("gui.tip.publish"))

        # ステータス系ラベル (メイン窓・常時表示)。
        self.lbl_state = QtWidgets.QLabel(t("gui.state.idle"))
        self.lbl_state.setToolTip(t("gui.tip.state"))
        self.lbl_model = QtWidgets.QLabel("model: -")
        self.lbl_model.setToolTip(t("gui.tip.model"))
        self.lbl_session = QtWidgets.QLabel("session -/-  turn -")
        self.lbl_session.setToolTip(t("gui.tip.session"))
        self.ctx_bar = QtWidgets.QProgressBar()
        self.ctx_bar.setRange(0, 100)
        self.ctx_bar.setFormat(
            f"ctx %p%  (rotate {int(round(float(self.loop_kw.get('threshold') or 0.70) * 100))}%)")
        self.ctx_bar.setToolTip(t("gui.tip.ctx"))
        self.lbl_cost = QtWidgets.QLabel("cost: $0.0000")

        # 出力ログ (主・メイン窓)。
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(5000)  # リングバッファ (上限は表示行数でなく append エントリ数)
        self.output.setStyleSheet(_OUTPUT_STYLE)  # ダーク背景 + セマンティックカラー (PALETTE)
        self.output.setFont(mono)

        # タスク注入欄 (メイン窓・上寄り)。
        self.input = QtWidgets.QPlainTextEdit()
        self.input.setPlaceholderText(t("gui.placeholder.input"))
        self.input.setMaximumHeight(90)

        # 進捗サマリ パネル (下部・全文スクロール可・選択コピー可)。claude が書く
        # docs/SESSION_SUMMARY.md の全文を表示する。ここから単語を選んでタスク注入に流用できる。
        self._summary_panel = QtWidgets.QWidget()
        sp_box = QtWidgets.QVBoxLayout(self._summary_panel)
        sp_box.setContentsMargins(0, 0, 0, 0)
        sp_head = QtWidgets.QHBoxLayout()
        sp_head.addWidget(QtWidgets.QLabel(t("gui.summary.title")))
        sp_head.addStretch(1)
        # 既定 = 構造化ダイジェスト (現在地/直近の成果/次の一手)。OFF で生 SESSION_SUMMARY 全文。
        self.chk_summary_raw = QtWidgets.QCheckBox(t("gui.check.summary_raw"))
        self.chk_summary_raw.setToolTip(t("gui.tip.summary_raw"))
        with QtCore.QSignalBlocker(self.chk_summary_raw):
            self.chk_summary_raw.setChecked(summary_raw_default)
        sp_head.addWidget(self.chk_summary_raw)
        self.btn_refresh_summary = QtWidgets.QPushButton(t("gui.btn.refresh"))
        self.btn_refresh_summary.setToolTip(t("gui.tip.refresh"))
        sp_head.addWidget(self.btn_refresh_summary)
        sp_box.addLayout(sp_head)
        self.summary_view = QtWidgets.QPlainTextEdit()
        self.summary_view.setReadOnly(True)  # 読取専用でも選択 + Ctrl+C は可 (単語をタスク注入へ)
        self.summary_view.setPlaceholderText(t("gui.placeholder.summary"))
        self.summary_view.setFont(mono)
        sp_box.addWidget(self.summary_view, 1)

        # 進捗サマリは 2 タブに分割 (ユーザー指摘 2026-06-13「タブで分ける」):
        #   「実行中」= 上で組んだ _summary_panel。選択/実行中 project の SESSION_SUMMARY。
        #   「共通」  = 全 project の docs/next_plan.md を集約し、記録された最終更新時刻の
        #              新しい順に並べた横断ビュー。どれが直近かを時刻つきで判断できる。
        self.common_view = QtWidgets.QPlainTextEdit()
        self.common_view.setReadOnly(True)  # 読取専用 + 選択コピー可 (next_plan の文言を流用)
        self.common_view.setPlaceholderText(t("gui.placeholder.common"))
        self.common_view.setToolTip(t("gui.tip.common"))
        self.common_view.setFont(mono)
        self.summary_tabs = QtWidgets.QTabWidget()
        self.summary_tabs.addTab(self._summary_panel, t("gui.tab.live"))
        self.summary_tabs.addTab(self.common_view, t("gui.tab.common"))

    def _build_settings_dialog(self) -> None:
        """設定系ウィジェットを別画面 (非モーダル QDialog + QScrollArea) に配置する。

        ★ ダイアログは ``self.settings_dialog`` に強参照で保持し、WA_DeleteOnClose は付けない
        (閉じても破棄せず再利用 → 設定ウィジェットがダングリングしない: Codex レビュー指摘)。
        ★ 中身は 1 個の container を QScrollArea (setWidgetResizable=True) に載せ、各設定は
        1 列縦積み (QFormLayout、narrow-first = 横一列の再発を避ける)。
        ★ Apply/reject の概念は導入しない (ウィジェットが唯一の状態源・Start が live に読む)。
        ★ ダイアログ Close では保存しない (closeEvent との二重保存を避ける)。
        """
        self.settings_dialog = QtWidgets.QDialog(self)
        self.settings_dialog.setWindowTitle(t("gui.dialog.settings.title"))
        self.settings_dialog.setModal(False)  # 非モーダル: ループ操作 (Start/Stop/Send) を妨げない
        dlg_box = QtWidgets.QVBoxLayout(self.settings_dialog)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)  # container を横幅に追従させる (narrow-first)
        container = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(container)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapAllRows)  # 狭幅: ラベルを上に

        # 実行モード / フラグ (1 列縦積み)。
        form.addRow(self.chk_real)
        form.addRow(self.chk_rad)
        form.addRow(self.chk_offload)
        form.addRow(self.chk_codex_first)
        # 数値設定。
        form.addRow(t("gui.label.threshold"), self.spin_threshold)
        form.addRow(t("gui.label.window_tokens"), self.spin_window)
        form.addRow(t("gui.label.max_cost"), self.spin_maxcost)
        # モデル / effort。
        form.addRow(t("gui.label.effort"), self.cmb_effort)
        form.addRow(t("gui.label.model"), self.cmb_model)
        # レビュー奏者パネル (複数 checkbox を縦に束ねる)。
        panel = QtWidgets.QWidget()
        panel_box = QtWidgets.QVBoxLayout(panel)
        panel_box.setContentsMargins(0, 0, 0, 0)
        for _key, _ in REVIEWER_CHOICES:
            panel_box.addWidget(self.chk_reviewers[_key])
        form.addRow(t("gui.label.review_panel"), panel)
        # 真偽確認奏者 + 責任者 (固定 Claude の明示ラベル)。
        form.addRow(t("gui.label.factcheck"), self.cmb_factcheck)
        lead_label = QtWidgets.QLabel(t("gui.lead.value"))
        lead_label.setToolTip(t("gui.tip.lead"))
        form.addRow(t("gui.label.lead"), lead_label)
        # テンプレ + 引数 + 公開ボタン。
        form.addRow(t("gui.label.template"), self.cmb_template)
        form.addRow(t("gui.placeholder.param"), self.edit_param)
        form.addRow(self.btn_publish)

        scroll.setWidget(container)
        dlg_box.addWidget(scroll, 1)
        btn_close = QtWidgets.QPushButton(t("gui.dialog.settings.close"))
        btn_close.clicked.connect(self.settings_dialog.hide)  # 純粋に閉じるだけ (保存しない)
        dlg_box.addWidget(btn_close)
        self.settings_dialog.resize(420, 640)  # 狭幅 first

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        """注入欄フォーカス時に見える化する (狭幅 + 仮想キーボードで隠れる対策)。

        スクロール文脈 (QSplitter) では ensureWidgetVisible が無い窓もあるため、ここでは
        入力欄を最前面寄りに保つよう活性化のみ行い、失敗しても握り潰す (fail-safe)。
        """
        if obj is self.input and event.type() == QtCore.QEvent.Type.FocusIn:
            try:
                self.input.raise_()
            except Exception:  # noqa: BLE001
                pass
        return super().eventFilter(obj, event)

    @QtCore.Slot()
    def _open_settings(self) -> None:
        """⚙Settings — 既に生成済みのダイアログを表示 (再利用・再生成しない)。"""
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

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

        ★ 各行の先頭に必ず [HH:MM:SS] を付ける (ユーザー要望 2026-06-13: トレーサビリティ)。
        空行は素通し。``ts`` 引数は後方互換のため残すが常時付与なので無視する。
        同じ時刻つき本文をローテーションログ (termlog) にも 1 行ずつ append する。
        """
        # CR 正規化: Qt は残留 \r も改行扱いするため CRLF 入りテキストが二重改行になる
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        stamp = f"[{datetime.now():%H:%M:%S}] "
        stamped = "\n".join((stamp + ln) if ln.strip() else ln for ln in text.split("\n"))
        self._termlog.write(stamped)  # トレーサビリティ: 時刻つき plaintext を行単位で永続化
        esc = html.escape(stamped).replace("\n", "<br/>")
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
            "offload": self.chk_offload.isChecked(),
            "autonomy": self.chk_autonomy.isChecked(),
            "codex_first": self.chk_codex_first.isChecked(),
            "reviewers": self._selected_reviewer_keys(),
            "factchecker": self.cmb_factcheck.currentData(),
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
        とき True。ただし **codex が未導入なら常に False** (可用性ガード): Codex 主にしても
        毎ターン not_found で空転するため、未導入時は Claude 主に倒す (chain backbone 維持)。
        実 claude モードでのみ意味を持つ。
        """
        if shutil.which("codex") is None:
            return False  # codex 不可用 → Codex 主を選べない (codex_first ON でも Claude 主)
        if self.chk_codex_first.isChecked():
            return True
        return templates.get(self.cmb_template.currentData()).prefer == "codex"

    def _resolve_providers(self) -> tuple[TurnRunner, list[TurnRunner]]:
        """このランの (primary, fallbacks) を決める唯一の真実。

        - テスト override 最優先。
        - 仮想 claude モードは Codex を使わない (課金/サブスク不要のプレビュー)。
        - 実 claude モード (可用性で自動 include/exclude — トグルではない):
          - **Codex** は ``shutil.which("codex")`` で導入済みなら使える奏者。
          - **Gemini** は導入済み (PATH) かつ無料枠未失効 (2026-06-18) なら使える奏者。
          - Codex 主 (codex_first or 機械的テンプレ かつ codex 可用): ``(Codex, [Gemini?, Claude])``
            = 作業を無料の Codex に寄せ、Gemini を次の無料 agent、Claude を最後の保険に置く。
          - それ以外 (Claude 主): ``(Claude, [Codex?, Gemini?])`` = 可用な無料 agent を保険に。
        - **Claude は常に primary か fallback に居る** (backbone) — chain は決して空にならない。
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
        # 可用性判定 (トグルでなく自動 include/exclude)。codex/gemini が使えなければ chain に入れない。
        codex_available = shutil.which("codex") is not None
        gemini = self._gemini_runner()  # 導入済み かつ 無料枠未失効なら GeminiRunner、else None
        primary: TurnRunner
        fallbacks: list[TurnRunner]
        if self._codex_is_primary():  # _codex_is_primary は codex 不可用なら False を返す
            # Codex 主。無料 agent (Gemini) を Claude より先に、Claude を最後の保険に置く。
            primary = CodexRunner()
            fallbacks = ([gemini] if gemini else []) + [claude]
        else:
            # Claude 主。可用な無料 agent (Codex → Gemini) を保険に並べる。
            primary = claude
            fallbacks = ([CodexRunner()] if codex_available else []) + ([gemini] if gemini else [])
        # オーケストラを組む: レビュー奏者パネル (複数・独立) or 真偽確認奏者が居れば、
        # 主奏者を指揮者として OrchestraRunner で包む。責任者 (lead=Claude) がレビュー + 真偽確認を
        # 取りまとめ → 統合指示 → 指揮者が修正。指揮者==lead==Claude でもブロックしない。
        # ★ final_signoff=False (2026-06-13 ユーザー指摘「レビューにレビューを重ねている」):
        #   lead の総合判断 (集約) が既に審判なので、修正後の再レビュー (sign-off) は冗長。
        #   1 ターンの AI 呼び出しを減らし、orchestra のレビュー所要時間を短縮する。
        reviewers = self._reviewer_runners()
        factchecker = self._factcheck_runner()
        if reviewers or factchecker is not None:
            from llterm.host.orchestra_runner import OrchestraRunner
            primary = OrchestraRunner(
                conductor=primary, reviewers=reviewers, factchecker=factchecker,
                lead=self._lead_runner(), apply_review=True, final_signoff=False)
        return primary, fallbacks

    def _gemini_runner(self) -> GeminiRunner | None:
        """gemini が PATH にあり、かつ無料枠が未失効なら GeminiRunner、無ければ None。

        トグルは廃止し可用性で自動 include/exclude する: 未インストールの gemini を chain に
        入れると毎ターン not_found で空転し、無料枠失効後 (2026-06-18) は無料では動かないため、
        どちらでも自動除外する (fail-safe)。
        """
        if shutil.which("gemini") is None:
            return None
        from llterm.host.gemini_runner import GeminiRunner, gemini_cli_free_tier_status
        if gemini_cli_free_tier_status()[0] == "expired":
            return None  # 無料枠失効後は agentic Gemini CLI を無料では使えない → 自動除外
        return GeminiRunner()

    def _runner_label(self, runner: TurnRunner) -> str:
        """1 つの runner を奏者ラベル (i18n) にする。OrchestraRunner は指揮者で代表する。"""
        if type(runner).__name__ == "OrchestraRunner":
            runner = runner.conductor  # type: ignore[attr-defined]
        cls = type(runner).__name__
        labels = {
            "CodexRunner": t("gui.player.codex"),
            "ClaudeRunner": t("gui.player.claude"),
            "GeminiRunner": t("gui.player.gemini"),
        }
        return labels.get(cls, cls)

    def _excluded_players(self) -> list[str]:
        """可用性判定で chain から外れた奏者の除外理由リスト (i18n) を組む。"""
        reasons: list[str] = []
        if shutil.which("codex") is None:
            reasons.append(t("gui.exclude.codex_missing"))
        if shutil.which("gemini") is None:
            reasons.append(t("gui.exclude.gemini_missing"))
        else:
            from llterm.host.gemini_runner import gemini_cli_free_tier_status
            if gemini_cli_free_tier_status()[0] == "expired":
                reasons.append(t("gui.exclude.gemini_expired"))
        return reasons

    def _provider_status_line(self, primary: TurnRunner, fallbacks: list[TurnRunner]) -> str:
        """奏者 chain 構成 (主→保険) と除外理由を 1 行にまとめた dim 表示文を返す。

        例: 「奏者: Codex(実装) → Claude(保険) / 除外: Gemini=無料枠失効」。可用性判定で
        自動 include/exclude された結果を honest disclosure する (なぜその奏者かを見せる)。
        """
        chain = " → ".join([self._runner_label(primary), *(self._runner_label(f) for f in fallbacks)])
        excluded = self._excluded_players()
        line = t("gui.status.players", chain=chain)
        if excluded:
            line += t("gui.status.excluded", reasons=" / ".join(excluded))
        return line

    def _gemini_cli_deadline_note(self) -> str:
        """Gemini CLI 奏者 (agentic) を使う設定のとき、無料枠期限の通知文を返す ("" = 通知なし)。

        Gemini CLI の個人無料枠は GEMINI_CLI_FREE_TIER_END (2026-06-18) で停止する。期限が
        間近/超過していて、かつ Gemini CLI を使う見込み (gemini が PATH にある自動可用 /
        レビュー奏者=Gemini CLI) のときだけ通知する。移行先 = Gemini API (provider 'gemini-api')。
        """
        reviewer_is_gemini_cli = self.chk_reviewers["gemini"].isChecked()
        uses_cli = (shutil.which("gemini") is not None) or reviewer_is_gemini_cli
        if not uses_cli:
            return ""
        from llterm.host.gemini_runner import gemini_cli_free_tier_status
        status, days = gemini_cli_free_tier_status()
        if status == "expired":
            return t("gui.msg.gemini_cli_expired", days=-days)
        if status == "soon":
            return t("gui.msg.gemini_cli_expiring", days=days)
        return ""

    def _selected_reviewer_keys(self) -> list[str]:
        """レビュー奏者パネルでチェック済みの key を REVIEWER_CHOICES の表示順で返す。"""
        return [key for key, _ in REVIEWER_CHOICES if self.chk_reviewers[key].isChecked()]

    def _make_reviewer_runner(self, key: str) -> TurnRunner | None:
        """1 つのレビュー奏者 key を runner 化する。未導入/キー無は None (fail-safe)。"""
        if not key:
            return None
        if key == "claude":  # 責任者と同系のダブルチェックも許容 (ブロックしない)
            from llterm.host.loop import ClaudeRunner
            return ClaudeRunner(use_subscription=True, model=str(self.cmb_model.currentData() or ""))
        if key == "codex":
            if shutil.which("codex") is None:
                return None
            from llterm.host.codex_runner import CodexRunner
            return CodexRunner()
        if key == "gemini":
            # レビュー奏者 (text 専用) は Gemini API が durable: CLI 無料枠は 6/18 停止
            # かつクォータ枯渇 (429) で毎ターン失敗する。API キーがあれば gemini-api へ
            # 自動ルートし、無いときだけ従来の CLI (GeminiRunner) にフォールバックする。
            from llterm.host.openai_compat_runner import OpenAICompatRunner
            api = OpenAICompatRunner(provider="gemini-api")
            if api.key_available():
                return api
            if shutil.which("gemini") is None:
                return None
            from llterm.host.gemini_runner import GeminiRunner
            return GeminiRunner()
        from llterm.host.openai_compat_runner import PROVIDERS, OpenAICompatRunner
        if key in PROVIDERS:
            rev = OpenAICompatRunner(provider=key)
            return rev if rev.key_available() else None
        return None

    def _reviewer_runners(self) -> list[TurnRunner]:
        """レビュー奏者パネルの選択 key 群を runner 化する (パネル)。

        未導入/キー無の奏者はスキップする (fail-safe = 分業を壊さず可用なものだけ参加)。
        """
        runners: list[TurnRunner] = []
        for key in self._selected_reviewer_keys():
            runner = self._make_reviewer_runner(key)
            if runner is not None:
                runners.append(runner)
        return runners

    def _factcheck_runner(self) -> TurnRunner | None:
        """調査・真偽確認奏者 (Perplexity 等) runner、未選択/キー無は None (fail-safe)。"""
        key = str(self.cmb_factcheck.currentData() or "")
        if not key:
            return None
        from llterm.host.openai_compat_runner import PROVIDERS, OpenAICompatRunner
        if key in PROVIDERS:
            fc = OpenAICompatRunner(provider=key)
            return fc if fc.key_available() else None
        return None

    def _lead_runner(self) -> TurnRunner:
        """責任者/総合判断 = Claude Code (固定)。レビュー取りまとめ + 最終 sign-off を担う。"""
        from llterm.host.loop import ClaudeRunner
        return ClaudeRunner(use_subscription=True, model=str(self.cmb_model.currentData() or ""))

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
        # 奏者構成と除外理由を 1 行 dim 表示 (可用性判定で自動 include/exclude したことを可視化)。
        if real:
            self._append(self._provider_status_line(runner, fallback_runners), PALETTE["dim"], ts=True)
        # Gemini CLI 無料枠の期限 (2026-06-18) が間近/超過なら通知 (移行先=Gemini API)
        deadline_note = self._gemini_cli_deadline_note()
        if real and deadline_note:
            self._append(deadline_note, PALETTE["err"], bold=True, ts=True)
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
        if self.chk_offload.isChecked():
            # 利用可能な計算オフロード先 (kaggle/gh/oci 等) を検出し、自律利用の指令を注入。
            from llterm.host.offload_tools import build_offload_hint

            loop_kw["offload_hint"] = build_offload_hint()
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
        self._start_ctl_consumer(workdir)  # Claude/emit の inject-task を走行中に拾う
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
            # タスク注入 = 人間の介入 → 監督モードへ (承認確認不要を OFF)。AI が確認事項を出せ、
            # 安全な Stop/引継ぎの意味を保つ。確認回答後に自動で ON へ戻る (ループ復帰)。
            if self.chk_autonomy.isChecked():
                self.chk_autonomy.setChecked(False)  # toggled → worker.set_autonomy(False)
        else:
            self._append(t("gui.msg.inject_pending"), PALETTE["dim"])

    def emergency_inject(self) -> None:
        """入力中タスクを緊急注入する: 現ターンを即中断し、最優先で実行する。

        通常注入と違い現ターンの完了を待たない。中断はループ停止ではなく、注入タスクは
        キュー先頭に積まれてスキップされず必ず次ターンで実行される (ユーザー要望 2026-06-13)。
        loop 未起動時は緊急の意味が無いので通常注入として queue に積む。
        """
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        if self.worker is not None and self.worker.isRunning():
            self._append(t("gui.msg.emergency_accepted", text=text), PALETTE["inject"],
                         bold=True, ts=True)
            self.worker.inject(text, emergency=True)  # 先頭挿入 + 現ターン interrupt
            if self.chk_autonomy.isChecked():
                self.chk_autonomy.setChecked(False)  # 介入 → 監督モードへ (送信と同方針)
        else:
            self._append(t("gui.msg.inject_accepted", text=text), PALETTE["inject"], ts=True)
            self._append(t("gui.msg.emergency_pending"), PALETTE["dim"])

    @QtCore.Slot(bool)
    def _on_autonomy_toggled(self, on: bool) -> None:
        """承認確認不要トグルを走行中の worker に即反映する (次ターンから効く)。

        走行中でなければ何もしない (start_loop が開始時に現在値を loop_kw へ渡す)。
        """
        if self.worker is not None and self.worker.isRunning():
            self.worker.set_autonomy(on)

    # ---- ctl queue consumer (Claude/emit → worker.inject 配線) ----
    def _start_ctl_consumer(self, workdir: Path) -> None:
        """走行開始時に ctl queue consumer を立て、QTimer ポーリングを始める。

        Claude (emit CLI) が ``<workdir>/.llterm/queue/`` に投函した inject-task を、GUI 手動
        注入と同一経路 (``worker.inject`` → SessionLoop.next_prompt) へ流す。これまで consumer が
        無く inject-task は queue/ に滞留していた (欠落配線の解消)。
        """
        root = workdir / ".llterm"
        ledger = Ledger(root / "loop_ledger.jsonl")  # ループと同じ監査 ledger に相乗り
        self._ctl_consumer = CtlConsumer(
            CtlQueue(root, ledger=ledger),
            inject=self._ctl_inject,
            running=lambda: self.worker is not None and self.worker.isRunning(),
            announce=self._ctl_announce,
            ledger=ledger,
        )
        self._ctl_timer.start()

    def _stop_ctl_consumer(self) -> None:
        self._ctl_timer.stop()
        self._ctl_consumer = None

    @QtCore.Slot()
    def _ctl_tick(self) -> None:
        """QTimer から定期的に呼ばれ、走行中なら ctl queue を消費する (fail-safe)。"""
        if self._ctl_consumer is None:
            return
        try:
            self._ctl_consumer.tick()
        except Exception:  # noqa: BLE001 — ポーリング失敗で GUI を殺さない
            pass

    def _ctl_inject(self, text: str, emergency: bool) -> None:
        """consumer から受けた inject-task を走行中 worker へ注入する (手動注入と同経路)。"""
        if self.worker is not None and self.worker.isRunning():
            self.worker.inject(text, emergency=emergency)

    def _ctl_announce(self, kind: str, cmd: CtlCommand, text: str) -> None:
        """ctl 注入の結果を出力ビューに可視化する (監査性: 何が来て何をしたかが人間に見える)。"""
        if kind == "executed":
            self._append(t("gui.msg.ctl_injected", text=text, cid=cmd.id),
                         PALETTE["inject"], bold=True, ts=True)
        elif kind == "hold_for_human":
            self._append(t("gui.msg.ctl_held", reason=cmd.reason, cid=cmd.id),
                         PALETTE["rotate"], bold=True, ts=True)
        elif kind == "error":
            self._append(t("gui.msg.ctl_error", cid=cmd.id), PALETTE["err"], ts=True)
        else:  # rejected / ignored
            self._append(t("gui.msg.ctl_rejected", action=cmd.action, cid=cmd.id),
                         PALETTE["dim"], ts=True)

    # ---- 選択ダイアログ ↔ ループ協調 (choice → inject) ----
    def _maybe_prompt_choice(self, text: str) -> bool:
        """agent 応答テキストに ⟦LLTERM_CHOICE⟧ があれば選択ダイアログを出す。

        検知 (行頭限定・コードフェンス/diff 内は無視) は llterm.host.choice の純関数に委譲する。
        1 ターンに複数あれば最後の (= 最新の未応答) ブロックを採用する。戻り値 = 検知して
        ダイアログを出したか。検知ゼロ / 既に表示中なら False (誤検知ゼロ・多重表示防止)。
        """
        from llterm.host.choice import parse_choice_blocks

        choice = parse_choice_blocks(text)
        if choice is None:
            return False
        return self._prompt_choice(choice)

    def _prompt_choice(self, choice: object) -> bool:
        """与えられた Choice を選択ダイアログで提示し、OK なら回答を次ターンへ注入する。

        - **OK**: 番号+ラベルの注入文 (例「選択: 2) public」) を既存 inject 機構
          (worker.inject → SessionLoop.next_prompt) に積む。これは継続プロンプトより優先される
          (next_prompt が非 None を返すと auto-continue を上書きする設計)。
        - **Cancel**: 注入しない (ユーザーは注入欄に自由入力できる)。
        - ループ未走行 / 多重検知中は出さない (fail-safe)。
        """
        if self._choice_active:
            return False  # 既に 1 件提示中 — 多重ダイアログを避ける
        if self.worker is None or not self.worker.isRunning():
            return False  # 走行中でなければ注入先が無い (idle では出さない)
        self._choice_active = True
        try:
            self._append(t("gui.msg.choice_detected"), PALETTE["inject"], bold=True, ts=True)
            factory = self._choice_dialog_factory or self._default_choice_dialog
            dialog = factory(choice)
            dialog.exec()  # モーダル (テストはスタブが即値を返す)
            reply = dialog.reply()
            if reply:
                self.worker.inject(reply)  # 次ターンの prompt に (auto-continue を上書き)
                # 確認に回答したら通常ループへ復帰 (autonomy ON)。「回答後すぐループに戻る」。
                if not self.chk_autonomy.isChecked():
                    self.chk_autonomy.setChecked(True)
                self._append(t("gui.msg.choice_replied", reply=reply), PALETTE["inject"], ts=True)
            else:
                self._append(t("gui.msg.choice_cancelled"), PALETTE["dim"], ts=True)
            return True
        finally:
            self._choice_active = False

    def _default_choice_dialog(self, choice: object) -> object:
        """実 ChoiceDialog を生成する (テスト以外の既定ファクトリ)。"""
        from llterm.gui.choice_dialog import ChoiceDialog

        return ChoiceDialog(choice, self)  # type: ignore[arg-type]

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
        rev = bool(item.get("review"))  # レビュー奏者由来 (分業オーケストラ)
        prefix = "  ⤷ " if sub else ("  📝 " if rev else "")
        if kind == "review":
            self._render_review_event(item)
            return
        if kind == "choice":
            # AskUserQuestion 由来の選択要求 (bonus 経路)。規約マーカーと同じダイアログへ。
            from llterm.host.choice import Choice

            self._prompt_choice(Choice(
                question=str(item.get("question") or ""),
                options=[str(o) for o in item.get("options") or []],
                multi=bool(item.get("multi", False))))
            return
        if kind == "init":
            model = str(item.get("model") or "?")
            sid = str(item.get("session_id", ""))[:8]
            self.lbl_model.setText(f"model: {model}" + (f"  effort={self._run_effort}"
                                                        if self._run_effort else ""))
            self._append(f"⏵ model={model} session={sid}", PALETTE["dim"])
        elif kind == "text":
            text = str(item.get("text") or "")
            if sub or rev:  # サブエージェント/レビュー奏者は区別表示し本応答カウントに数えない
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

    def _render_review_event(self, item: dict) -> None:
        """分業オーケストラの review イベント (パネル/真偽確認/集約/sign-off) を描画する。

        独立 (別系統) / ダブルチェック (同系) を見出しに付記し、責任者の取りまとめ・最終確認も
        専用見出しで可視化する (『レビューやりっぱなし』を画面でも追えるように)。
        """
        phase = item.get("phase")
        if phase == "factcheck":
            checker = str(item.get("checker") or "factcheck")
            if item.get("is_error"):
                self._append(t("gui.stream.review_factcheck_failed", checker=checker), PALETTE["err"])
            else:
                self._append(t("gui.stream.review_factcheck", checker=checker), PALETTE["rotate"])
            return
        if phase == "aggregate":
            self._append(t("gui.stream.review_aggregate"), PALETTE["rotate"], bold=True)
            return
        if phase == "signoff":
            self._append(t("gui.stream.review_signoff"), PALETTE["rotate"], bold=True)
            outcome = (t("gui.stream.review_signoff_approved") if item.get("approved")
                       else t("gui.stream.review_signoff_changes"))
            self._append(outcome, PALETTE["inject"] if item.get("approved") else PALETTE["err"])
            return
        # 既定: パネルのレビュー奏者 (start / end / failed)
        who = str(item.get("reviewer") or "reviewer")
        if phase == "start":
            tag = (t("gui.stream.review_independent") if item.get("independent")
                   else t("gui.stream.review_doublecheck")) if "independent" in item else ""
            self._append(t("gui.stream.review_start", reviewer=who, kind=tag),
                         PALETTE["rotate"], bold=True)
        elif item.get("is_error"):
            self._append(t("gui.stream.review_failed", reviewer=who), PALETTE["err"])
        else:
            self._append(t("gui.stream.review_end", reviewer=who), PALETTE["rotate"])

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
        self._refresh_common_summary()  # 共通タブ (全 project 集約) も同時に更新

    def _refresh_common_summary(self) -> None:
        """共通タブを全 project の docs/next_plan.md 集約で再生成する。

        記録された最終更新時刻 (無ければ mtime) の新しい順に並ぶので、どの project が
        直近に動いたかを時刻つきで判断できる。IO 失敗でも GUI を殺さない (fail-safe)。
        """
        from llterm.progress import build_common_summary, collect_progress
        try:
            text = build_common_summary(collect_progress(self.projects_root))
        except OSError:
            text = ""
        bar = self.common_view.verticalScrollBar()
        pos = bar.value()
        self.common_view.setPlainText(text)
        bar.setValue(min(pos, bar.maximum()))  # 読んでいた位置を維持

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
            # ターン最終テキストに規約マーカーがあれば選択ダイアログを出す (主経路)。
            # マルチライン block はターン完了テキストで確実に拾える (stream 途中では分割され得る)。
            if text and not err:
                self._maybe_prompt_choice(text)
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
    # ローカル鍵束 (D:/api-keys.json 等) を env へ補完する。env 未設定でも JSON 由来の鍵で
    # 奏者を可用化する (例: GEMINI_API_KEY 不在で gemini-api 奏者が除外される問題の解消)。
    # fail-safe (ファイル非存在/JSON 不正でも例外を投げない) なので無条件に呼んでよい。
    # runner 構築 (MainWindow → _resolve_providers) より前に呼ぶのが要点。
    from llterm.host.api_keys import load_api_keys_into_env
    load_api_keys_into_env()
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
