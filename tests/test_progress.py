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
