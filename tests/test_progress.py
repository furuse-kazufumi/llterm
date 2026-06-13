# SPDX-License-Identifier: Apache-2.0
"""共通進捗サマリー集約 (llterm.progress) の単体テスト。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from llterm.progress import (
    ProjectProgress,
    build_common_summary,
    collect_progress,
    parse_updated_at,
    progress_source,
)


def _mk(project_dir: Path, *, next_plan: str | None = None, session_summary: str | None = None) -> None:
    docs = project_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    if next_plan is not None:
        (docs / "next_plan.md").write_text(next_plan, encoding="utf-8")
    if session_summary is not None:
        (docs / "SESSION_SUMMARY.md").write_text(session_summary, encoding="utf-8")


def test_progress_source_prefers_next_plan(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    _mk(proj, next_plan="NP", session_summary="SS")
    path, source = progress_source(proj)
    assert source == "next_plan" and path.name == "next_plan.md"

    proj2 = tmp_path / "q"
    _mk(proj2, session_summary="SS")  # next_plan 無し
    path2, source2 = progress_source(proj2)
    assert source2 == "session_summary" and path2.name == "SESSION_SUMMARY.md"

    proj3 = tmp_path / "r"
    proj3.mkdir()
    assert progress_source(proj3) == (None, "none")


def test_collect_progress_scans_and_skips(tmp_path: Path) -> None:
    _mk(tmp_path / "alpha", next_plan="# alpha\n## 次の一手\n- do A")
    _mk(tmp_path / "beta", session_summary="# beta summary")
    (tmp_path / "gamma").mkdir()                 # 進捗ファイル無し → スキップ
    _mk(tmp_path / "_shared", next_plan="agg")   # 集約用 → 除外 (アンダースコア始まり)
    (tmp_path / ".hidden").mkdir()               # 隠し → 除外

    items = collect_progress(tmp_path)
    names = {p.name for p in items}
    assert names == {"alpha", "beta"}
    by = {p.name: p for p in items}
    assert by["alpha"].source == "next_plan"
    assert by["beta"].source == "session_summary"


def test_build_common_summary_header_and_sections() -> None:
    items = [
        ProjectProgress("old", Path("o"), "old body", updated=100.0, source="next_plan"),
        ProjectProgress("new", Path("n"), "new body", updated=300.0, source="next_plan"),
        ProjectProgress("mid", Path("m"), "mid body", updated=200.0, source="session_summary"),
    ]
    out = build_common_summary(items, fmt=lambda t: f"T{int(t)}")

    # ヘッダ: 新しい順 (new > mid > old)、先頭に ← 最新
    idx = out.index("## 最新更新インデックス (新しい順)")
    header = out[idx:out.index("---")]
    assert header.index("**new**") < header.index("**mid**") < header.index("**old**")
    assert "← 最新" in out.split("**new**")[1].split("\n")[0]
    # session_summary 代用は種別注記が付く
    assert "(session_summary)" in out

    # 本文: 各プロジェクトのセクションと全文 (新しい順)
    assert "## new  (更新: T300)" in out
    assert "## mid  (更新: T200)" in out
    assert "## old  (更新: T100)" in out
    assert "new body" in out and "mid body" in out and "old body" in out
    assert out.index("## new") < out.index("## mid") < out.index("## old")


def test_build_common_summary_empty() -> None:
    out = build_common_summary([])
    assert "共通進捗サマリー" in out
    assert "ありません" in out


def test_collect_progress_uses_mtime(tmp_path: Path) -> None:
    _mk(tmp_path / "a", next_plan="A")
    p = tmp_path / "a" / "docs" / "next_plan.md"
    os.utime(p, (1_000_000.0, 1_234_567.0))  # mtime を固定
    items = collect_progress(tmp_path)
    assert items[0].updated == 1_234_567.0
    assert items[0].updated_source == "mtime"  # 本文に記録時刻が無い → mtime フォールバック


# ─── 記録された最終更新時刻の解析 (ユーザー指摘: 日付のみでは直前判定不能) ───

def test_parse_updated_at_with_time() -> None:
    txt = "# x\n> 最終更新: 2026-06-13 15:42 JST\n本文\n"
    assert parse_updated_at(txt) == datetime(2026, 6, 13, 15, 42).timestamp()


def test_parse_updated_at_fullwidth_colon() -> None:
    assert parse_updated_at("最終更新：2026-06-13 09:05") == datetime(2026, 6, 13, 9, 5).timestamp()


def test_parse_updated_at_date_only_is_none() -> None:
    # 時刻 (HH:MM) を欠く記録は採用しない → mtime フォールバックに委ねる
    assert parse_updated_at("> 最終更新: 2026-06-13 (EXIT 準備)") is None
    assert parse_updated_at("最終更新：2026-06-13") is None
    assert parse_updated_at("更新は未記録") is None
    assert parse_updated_at("") is None


def test_parse_updated_at_invalid_date_is_none() -> None:
    assert parse_updated_at("最終更新: 2026-13-99 25:61") is None  # 壊れた日時は fail-safe で None


def test_collect_progress_prefers_recorded_timestamp(tmp_path: Path) -> None:
    body = "# a\n> 最終更新: 2026-06-13 15:42 JST\n## 次の一手\n- do\n"
    _mk(tmp_path / "a", next_plan=body)
    p = tmp_path / "a" / "docs" / "next_plan.md"
    os.utime(p, (1.0, 1.0))  # mtime をわざと大昔に → 記録時刻が優先されるはず
    it = collect_progress(tmp_path)[0]
    assert it.updated == datetime(2026, 6, 13, 15, 42).timestamp()
    assert it.updated_source == "header"
    assert it.mtime == 1.0  # mtime は透明性のため別途保持


def test_collect_progress_date_only_falls_back_to_mtime(tmp_path: Path) -> None:
    _mk(tmp_path / "a", next_plan="# a\n> 最終更新: 2026-06-13\n本文")  # 日付のみ
    p = tmp_path / "a" / "docs" / "next_plan.md"
    os.utime(p, (1_000_000.0, 1_234_567.0))
    it = collect_progress(tmp_path)[0]
    assert it.updated == 1_234_567.0 and it.updated_source == "mtime"


def test_build_common_summary_marks_mtime_fallback() -> None:
    items = [
        ProjectProgress("rec", Path("r"), "b", updated=300.0, source="next_plan",
                        mtime=300.0, updated_source="header"),
        ProjectProgress("fb", Path("f"), "b", updated=200.0, source="next_plan",
                        mtime=200.0, updated_source="mtime"),
    ]
    out = build_common_summary(items, fmt=lambda t: f"T{int(t)}")
    rec_line = out.split("**rec**")[1].split("\n")[0]
    fb_line = out.split("**fb**")[1].split("\n")[0]
    assert "(ファイル時刻)" not in rec_line  # 記録時刻つきは信頼でき注記なし
    assert "(ファイル時刻)" in fb_line       # mtime 代用は明示
