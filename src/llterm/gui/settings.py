# SPDX-License-Identifier: Apache-2.0
"""GUI 設定の永続化 — 最後の設定 (プロジェクト/トグル/閾値/テンプレ/ウィンドウ位置) を
次回起動時に復元する。

保存先は既定で ``~/.llterm/gui_settings.json`` (人間が読める JSON、レジストリ不使用)。
読み書きとも fail-safe: 壊れた JSON・権限エラー・欠落キーで GUI を殺さず既定値に落ちる。
優先順位は **CLI 明示指定 > 保存値 > 組込み既定** (app.MainWindow 側で適用)。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_SETTINGS_PATH = Path.home() / ".llterm" / "gui_settings.json"


def load_settings(path: Path) -> dict:
    """設定 JSON を読む。無い・壊れている場合は空 dict (既定値で起動)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path: Path, data: dict) -> bool:
    """設定 JSON を原子的に書く (tmp → replace)。失敗しても GUI を殺さない。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
        return True
    except (OSError, TypeError, ValueError):
        return False
