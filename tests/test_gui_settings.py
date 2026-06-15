# SPDX-License-Identifier: Apache-2.0
"""GUI 設定永続化 (gui/settings.py) の回帰テスト — 純関数部 (PySide6 不要)。"""
from __future__ import annotations

from pathlib import Path

from llterm.gui.settings import (
    consume_startup_input,
    load_settings,
    save_settings,
    startup_input_path,
    write_startup_input,
)


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "nope.json") == {}


def test_load_broken_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{broken", encoding="utf-8")
    assert load_settings(p) == {}  # fail-safe: 壊れた設定で GUI を殺さない


def test_load_non_dict_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[1, 2]", encoding="utf-8")
    assert load_settings(p) == {}


def test_save_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "gui_settings.json"  # 親ディレクトリも自動作成される
    data = {"workdir": "D:/projects/llterm", "real": True, "threshold": 0.55,
            "template": "rad_expand", "param": "ロボティクス"}
    assert save_settings(p, data) is True
    assert load_settings(p) == data
    assert not p.with_name(p.name + ".tmp").exists()  # 一時ファイルを残さない


def test_save_failure_is_swallowed(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert save_settings(blocker / "sub.json", {"a": 1}) is False  # 親がファイル → 書けない
