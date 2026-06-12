# SPDX-License-Identifier: Apache-2.0
"""pytest 共通設定。GUI テストはヘッドレス (offscreen) で動かす。

Qt の ``offscreen`` プラットフォームにより、実ディスプレイ無し (CI / 夜間自走) でも
ウィジェット生成・シグナル配送・イベントループが動く。実 claude も実画面も不要で
「仮想でデバッグ繰り返し」できる。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_gui_settings(tmp_path, monkeypatch):
    """GUI テストが実ユーザーの ~/.llterm/gui_settings.json を読み書きしないよう隔離する。"""
    try:
        from llterm.gui import app as gui_app
    except ImportError:  # PySide6 なし環境では GUI テスト自体が skip される
        yield
        return
    monkeypatch.setattr(gui_app, "DEFAULT_SETTINGS_PATH", tmp_path / "gui_settings.json")
    yield
