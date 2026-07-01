# SPDX-License-Identifier: Apache-2.0
"""選択ダイアログ (ChoiceDialog) — agent の選択要求を GUI で受ける QDialog。

llterm は claude を headless でターン駆動するため、ターン中に対話的選択ができない。
agent (claude) が ``⟦LLTERM_CHOICE …⟧`` または AskUserQuestion で選択を求めたら
(:mod:`llterm.host.choice` が検知)、このダイアログを出してユーザーに選ばせ、回答を
既存の task injection 機構 (worker.inject → next_prompt) で次ターンへ注入する。

**使いやすさ最優先** (ユーザー指示):

- **狭幅スマホ対応**: 大きめタップ領域 (各選択肢 ``minimumHeight >= 44``)、選択肢は縦に
  大きく並べる。レスポンシブ (既存 GUI の narrow-first と整合)。
- **手数最小**: 選んで OK の 2 操作。single は先頭を既定選択しておき、そのままでも 1 択が
  確定する。OK / Cancel は大きめボタン。
- **明快**: question を見出しに、single=ラジオ / multi=チェックボックス。

挙動:
- **OK** → :meth:`reply` が「選択: 2) public」「選択: 1) A, 3) C」形式 (番号+ラベル) を返す。
- **Cancel** → :meth:`reply` は ``None`` (= 注入しない)。呼び出し側は注入欄への自由入力に委ねる。
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from llterm.host.choice import Choice, format_choice_reply
from llterm.i18n import t

# 狭幅スマホでもタップしやすい最小高さ (px)。指タップの推奨ターゲットサイズに合わせる。
_TAP_MIN_HEIGHT = 44


class ChoiceDialog(QtWidgets.QDialog):
    """1 つの :class:`Choice` を提示し、ユーザーの選択を番号+ラベルの注入文へ変換する。"""

    def __init__(self, choice: Choice, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._choice = choice
        self._reply: str | None = None
        self._indices: list[int] = []
        self._buttons: list[QtWidgets.QAbstractButton] = []
        self.setWindowTitle(t("gui.dialog.choice.title"))
        self.setModal(True)  # 選択は即応の対話 — 完了まで前面に保つ
        self._build_ui()

    # ---- UI 構築 (narrow-first・大きめタップ領域) ----
    def _build_ui(self) -> None:
        box = QtWidgets.QVBoxLayout(self)
        box.setSpacing(10)

        # 見出し (question)。空なら既定文言。折り返して狭幅でも全文見えるように。
        self._heading = QtWidgets.QLabel(
            self._choice.question.strip() or t("gui.dialog.choice.default_heading"))
        self._heading.setWordWrap(True)
        font = self._heading.font()
        font.setBold(True)
        font.setPointSize(max(font.pointSize() + 1, 11))
        self._heading.setFont(font)
        box.addWidget(self._heading)

        # 操作ヒント (1 つ選んで OK / 複数可) — 手数を明示して迷わせない。
        hint = QtWidgets.QLabel(
            t("gui.dialog.choice.hint_multi") if self._choice.multi
            else t("gui.dialog.choice.hint_single"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7f848e")
        box.addWidget(hint)

        # 選択肢 — single=ラジオ / multi=チェックボックス。縦に大きく並べる。
        # ラジオは QButtonGroup で排他にし、スクロール可能にして選択肢が多くても破綻しない。
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(not self._choice.multi)
        options_host = QtWidgets.QWidget()
        opt_box = QtWidgets.QVBoxLayout(options_host)
        opt_box.setContentsMargins(0, 0, 0, 0)
        opt_box.setSpacing(6)
        for i, label in enumerate(self._choice.options):
            btn: QtWidgets.QAbstractButton = (
                QtWidgets.QCheckBox(label) if self._choice.multi else QtWidgets.QRadioButton(label))
            btn.setMinimumHeight(_TAP_MIN_HEIGHT)  # 大きめタップ領域 (狭幅スマホ)
            btn.setStyleSheet("QAbstractButton { padding: 6px; font-size: 14px; }")
            if not self._choice.multi and i == 0:
                btn.setChecked(True)  # single は先頭を既定選択 (そのまま OK で 1 択確定)
            self._group.addButton(btn, i)
            self._buttons.append(btn)
            opt_box.addWidget(btn)
        opt_box.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(options_host)
        box.addWidget(scroll, 1)

        # OK (大ボタン・既定) + Cancel。手数最小: 選んで OK の 2 操作。
        btn_row = QtWidgets.QHBoxLayout()
        self._cancel_btn = QtWidgets.QPushButton(t("gui.dialog.choice.cancel"))
        self._ok_btn = QtWidgets.QPushButton(t("gui.dialog.choice.ok"))
        self._ok_btn.setDefault(True)
        self._ok_btn.setAutoDefault(True)
        for b in (self._cancel_btn, self._ok_btn):
            b.setMinimumHeight(_TAP_MIN_HEIGHT)
            b.setStyleSheet("QPushButton { padding: 8px 14px; font-size: 14px; }")
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._ok_btn)
        box.addLayout(btn_row)

        self._ok_btn.clicked.connect(self._accept)
        self._cancel_btn.clicked.connect(self._reject)

        # 狭幅 first (スマホ Remote Desktop を想定): 縦長・横は画面幅に追従。
        self.resize(420, 480)
        self.setMinimumWidth(280)

    # ---- 結果 ----
    def selected_indices(self) -> list[int]:
        """選択された選択肢 index 列 (0 始まり・表示順)。Cancel 時は空。"""
        return list(self._indices)

    def reply(self) -> str | None:
        """注入文 (「選択: 2) private」) を返す。Cancel された場合は None (= 注入しない)。"""
        return self._reply

    def _current_indices(self) -> list[int]:
        return [i for i, b in enumerate(self._buttons) if b.isChecked()]

    @QtCore.Slot()
    def _accept(self) -> None:
        """OK — 現在の選択を番号+ラベルの注入文へ確定し、ダイアログを閉じる。"""
        indices = self._current_indices()
        # multi-select で 1 つも選ばれていない場合は確定させない (空の "選択: (なし)" 注入を防ぐ)。
        # 「どれも選ばない」は Cancel (自由入力へ) が正路 (J9)。
        if self._choice.multi and not indices:
            return
        self._indices = indices
        self._reply = format_choice_reply(self._choice, self._indices)
        self.accept()

    @QtCore.Slot()
    def _reject(self) -> None:
        """Cancel — 注入しない (reply=None)。ユーザーは注入欄へ自由入力できる。"""
        self._indices = []
        self._reply = None
        self.reject()
