# SPDX-License-Identifier: Apache-2.0
"""ChoiceDialog (選択 QDialog) のヘッドレス回帰テスト — offscreen・使いやすさ配慮の検証。

狭幅スマホでもタップしやすい・手数最小 (選んで OK の 2 操作) を満たすことと、OK で
番号+ラベルの注入文を返し、Cancel で未注入 (None) になることを検証する。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI テストは PySide6 が要る")

from PySide6 import QtWidgets  # noqa: E402

from llterm.host.choice import Choice  # noqa: E402
from llterm.gui.choice_dialog import ChoiceDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_single_choice_builds_radio_buttons(qapp: QtWidgets.QApplication) -> None:
    dlg = ChoiceDialog(Choice(question="公開範囲", options=["public", "private"], multi=False))
    assert len(dlg._buttons) == 2
    assert all(isinstance(b, QtWidgets.QRadioButton) for b in dlg._buttons)
    # ラベルはそのまま表示される
    assert [b.text() for b in dlg._buttons] == ["public", "private"]
    dlg.deleteLater()


def test_multi_choice_builds_checkboxes(qapp: QtWidgets.QApplication) -> None:
    dlg = ChoiceDialog(Choice(question="対象", options=["tests", "docs", "lint"], multi=True))
    assert len(dlg._buttons) == 3
    assert all(isinstance(b, QtWidgets.QCheckBox) for b in dlg._buttons)
    dlg.deleteLater()


def test_question_shown_as_heading(qapp: QtWidgets.QApplication) -> None:
    dlg = ChoiceDialog(Choice(question="どれにする？", options=["a", "b"], multi=False))
    assert "どれにする？" in dlg._heading.text()
    dlg.deleteLater()


def test_empty_question_uses_default_heading(qapp: QtWidgets.QApplication) -> None:
    """question が空でも見出しは空にしない (既定文言を出す)。"""
    dlg = ChoiceDialog(Choice(question="", options=["a", "b"], multi=False))
    assert dlg._heading.text().strip() != ""
    dlg.deleteLater()


def test_single_default_selects_first(qapp: QtWidgets.QApplication) -> None:
    """single は先頭を既定選択 (手数最小: そのまま OK で 1 択が決まる)。"""
    dlg = ChoiceDialog(Choice(question="q", options=["a", "b"], multi=False))
    assert dlg._buttons[0].isChecked()
    dlg.deleteLater()


def test_ok_returns_numbered_reply_single(qapp: QtWidgets.QApplication) -> None:
    c = Choice(question="q", options=["public", "private"], multi=False)
    dlg = ChoiceDialog(c)
    dlg._buttons[1].setChecked(True)  # private を選ぶ
    dlg._accept()  # OK 相当
    assert dlg.reply() == "選択: 2) private"
    assert dlg.selected_indices() == [1]
    dlg.deleteLater()


def test_ok_returns_numbered_reply_multi(qapp: QtWidgets.QApplication) -> None:
    c = Choice(question="q", options=["A", "B", "C"], multi=True)
    dlg = ChoiceDialog(c)
    dlg._buttons[0].setChecked(True)
    dlg._buttons[2].setChecked(True)
    dlg._accept()
    assert dlg.reply() == "選択: 1) A, 3) C"
    dlg.deleteLater()


def test_cancel_yields_no_reply(qapp: QtWidgets.QApplication) -> None:
    """Cancel は注入しない = reply() が None。"""
    dlg = ChoiceDialog(Choice(question="q", options=["a", "b"], multi=False))
    dlg._reject()  # Cancel 相当
    assert dlg.reply() is None
    dlg.deleteLater()


def test_large_tap_targets_for_narrow_screen(qapp: QtWidgets.QApplication) -> None:
    """狭幅スマホ向け: 各選択肢の最小高さが十分に大きい (タップしやすさ)。"""
    dlg = ChoiceDialog(Choice(question="q", options=["a", "b"], multi=False))
    assert all(b.minimumHeight() >= 40 for b in dlg._buttons)
    dlg.deleteLater()
