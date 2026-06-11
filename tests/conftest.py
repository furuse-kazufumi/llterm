# SPDX-License-Identifier: Apache-2.0
"""pytest 共通設定。GUI テストはヘッドレス (offscreen) で動かす。

Qt の ``offscreen`` プラットフォームにより、実ディスプレイ無し (CI / 夜間自走) でも
ウィジェット生成・シグナル配送・イベントループが動く。実 claude も実画面も不要で
「仮想でデバッグ繰り返し」できる。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
