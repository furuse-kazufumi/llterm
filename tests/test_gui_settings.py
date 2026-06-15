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


# ── 起動時 one-shot 入力プリフィル ──────────────────────────────


def test_startup_input_path_is_beside_settings(tmp_path: Path) -> None:
    sp = tmp_path / "cfg" / "gui_settings.json"
    assert startup_input_path(sp) == tmp_path / "cfg" / "startup_input.txt"


def test_consume_missing_returns_empty(tmp_path: Path) -> None:
    assert consume_startup_input(tmp_path / "gui_settings.json") == ""  # fail-safe


def test_write_then_consume_is_one_shot(tmp_path: Path) -> None:
    sp = tmp_path / "gui_settings.json"
    brief = "D:/projects/llcore で feat/lm-recurrent を自律実行せよ。\n複数行可。"
    assert write_startup_input(sp, brief) is True
    assert startup_input_path(sp).exists()
    # 1 回目: 内容を返し、ファイルは消費 (clear-on-load)
    assert consume_startup_input(sp) == brief
    assert not startup_input_path(sp).exists()
    # 2 回目: 既に消費済 → 再発火しない
    assert consume_startup_input(sp) == ""


def test_write_empty_cancels_prefill(tmp_path: Path) -> None:
    sp = tmp_path / "gui_settings.json"
    write_startup_input(sp, "something")
    assert write_startup_input(sp, "   \n  ") is True  # 空白のみ → 取消
    assert not startup_input_path(sp).exists()
    assert consume_startup_input(sp) == ""


def test_consume_whitespace_only_returns_empty_and_clears(tmp_path: Path) -> None:
    sp = tmp_path / "gui_settings.json"
    startup_input_path(sp).parent.mkdir(parents=True, exist_ok=True)
    startup_input_path(sp).write_text("   \n\t\n", encoding="utf-8")
    assert consume_startup_input(sp) == ""  # 空白のみは prefill なし扱い
    assert not startup_input_path(sp).exists()  # それでも消費して掃除する


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    sp = tmp_path / "new" / "deeper" / "gui_settings.json"
    assert write_startup_input(sp, "hi") is True
    assert consume_startup_input(sp) == "hi"
