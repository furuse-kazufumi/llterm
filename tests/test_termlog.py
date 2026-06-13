# SPDX-License-Identifier: Apache-2.0
"""ターミナル ローテーションログ (llterm.gui.termlog) の単体テスト (Qt 非依存)。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from llterm.gui.termlog import TerminalLog, _parse_stem


def test_write_appends_lines_to_hourly_file(tmp_path: Path) -> None:
    clock = {"t": datetime(2026, 6, 13, 16, 30)}
    log = TerminalLog(tmp_path, now_fn=lambda: clock["t"])
    log.write("[16:30:00] line A")
    log.write("[16:30:01] line B\n[16:30:01] line C")  # 複数行は行単位で append
    f = tmp_path / "2026-06-13_16.log"
    assert f.exists()
    assert f.read_text(encoding="utf-8").splitlines() == [
        "[16:30:00] line A", "[16:30:01] line B", "[16:30:01] line C"]


def test_hour_rollover_creates_new_file(tmp_path: Path) -> None:
    clock = {"t": datetime(2026, 6, 13, 16, 30)}
    log = TerminalLog(tmp_path, now_fn=lambda: clock["t"])
    log.write("a")
    clock["t"] = datetime(2026, 6, 13, 17, 0)  # 時間が変わる
    log.write("b")
    assert (tmp_path / "2026-06-13_16.log").read_text(encoding="utf-8").strip() == "a"
    assert (tmp_path / "2026-06-13_17.log").read_text(encoding="utf-8").strip() == "b"


def test_prune_removes_files_older_than_one_week(tmp_path: Path) -> None:
    (tmp_path / "2026-06-01_00.log").write_text("old", encoding="utf-8")     # 12 日前
    (tmp_path / "2026-06-13_10.log").write_text("recent", encoding="utf-8")  # 6 時間前
    log = TerminalLog(tmp_path, retain_hours=168,  # 1 週間
                      now_fn=lambda: datetime(2026, 6, 13, 16, 0))
    log.write("trigger prune")  # 新ファイル作成時に剪定が走る
    assert not (tmp_path / "2026-06-01_00.log").exists()  # 7 日超 → 削除
    assert (tmp_path / "2026-06-13_10.log").exists()       # 1 週間内 → 残る
    assert (tmp_path / "2026-06-13_16.log").exists()


def test_parse_stem() -> None:
    assert _parse_stem("2026-06-13_16.log") == datetime(2026, 6, 13, 16)
    assert _parse_stem("not-a-log.txt") is None
    assert _parse_stem("2026-13-99_25.log") is None  # 不正な日時 → None


def test_write_is_failsafe_when_dir_unusable(tmp_path: Path) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")  # 同名ファイルがあり mkdir できない
    log = TerminalLog(blocker / "sub", now_fn=lambda: datetime(2026, 6, 13, 16, 0))
    log.write("must not raise")  # 例外を投げなければ OK (fail-safe)
