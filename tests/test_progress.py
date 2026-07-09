# SPDX-License-Identifier: Apache-2.0
"""共通進捗サマリー集約 (llterm.progress) の単体テスト。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import llterm.progress as progress_mod
from llterm.progress import (
    _commit_summary_text,
    _common_summary_tmp_path,
    _default_fmt,
    _write_text_sync,
    DEFAULT_PROJECTS_ROOT,
    ProjectProgress,
    build_common_summary,
    collect_progress,
    infer_common_summary_paths,
    main as progress_main,
    parse_updated_at,
    parse_session_summary_seed,
    progress_format_gaps,
    progress_source,
    refresh_common_summary_for_project,
    scaffold_missing_next_plans,
    scaffold_next_plan_for_project,
    scaffold_next_plan_text,
    write_common_summary,
    write_common_summary_items,
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
    assert "現在地" in by["alpha"].format_gaps
    assert by["beta"].format_gaps == ()


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


def test_build_common_summary_breaks_same_minute_ties_with_mtime() -> None:
    shared = datetime(2026, 7, 10, 4, 34, tzinfo=timezone(timedelta(hours=9))).timestamp()
    items = [
        ProjectProgress("alpha", Path("a"), "alpha body", updated=shared, mtime=10.0, source="next_plan"),
        ProjectProgress("beta", Path("b"), "beta body", updated=shared, mtime=20.0, source="next_plan"),
    ]
    out = build_common_summary(items, fmt=lambda t: "T")
    header = out[out.index("## 最新更新インデックス"):out.index("---")]
    assert header.index("**beta**") < header.index("**alpha**")
    assert "<!--" not in out


def test_build_common_summary_empty() -> None:
    out = build_common_summary([])
    assert "共通進捗サマリー" in out
    assert "ありません" in out


def test_default_fmt_uses_jst() -> None:
    epoch = datetime(2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))).timestamp()
    assert _default_fmt(epoch) == "2026-06-13 15:42"


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
    assert parse_updated_at(txt) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_fullwidth_colon() -> None:
    assert parse_updated_at("最終更新：2026-06-13 09:05") == datetime(
        2026, 6, 13, 9, 5, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_accepts_session_summary_metadata_line() -> None:
    text = "# Session Summary\n- **最終更新**: 2026-06-13 15:42 JST\n- **プロジェクト**: x\n"
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_date_only_is_none() -> None:
    # 時刻 (HH:MM) を欠く記録は採用しない → mtime フォールバックに委ねる
    assert parse_updated_at("> 最終更新: 2026-06-13 (EXIT 準備)") is None
    assert parse_updated_at("最終更新：2026-06-13") is None
    assert parse_updated_at("更新は未記録") is None
    assert parse_updated_at("") is None


def test_parse_updated_at_invalid_date_is_none() -> None:
    assert parse_updated_at("最終更新: 2026-13-99 25:61") is None  # 壊れた日時は fail-safe で None


def test_parse_updated_at_ignores_body_mentions_and_code_fences() -> None:
    text = (
        "# x\n"
        "本文で 最終更新: 2026-06-13 15:42 JST に触れるだけ\n"
        "```md\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "```\n"
    )
    assert parse_updated_at(text) is None


def test_parse_updated_at_finds_header_beyond_line_twelve() -> None:
    text = (
        "# next_plan\n"
        "\n".join(f"前置き {i}" for i in range(1, 14))
        + "\n> 最終更新: 2026-06-13 15:42 JST\n本文\n"
    )
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_accepts_quoted_preamble_before_header_update() -> None:
    text = (
        "# next_plan\n"
        "> 補足メモ\n"
        "> まだヘッダ部\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "## 現在地\n"
    )
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_accepts_quoted_header_without_space() -> None:
    text = "# next_plan\n>最終更新: 2026-06-13 15:42 JST\n## 現在地\n"
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_accepts_nested_quote_prefix() -> None:
    text = "# next_plan\n>> 最終更新: 2026-06-13 15:42 JST\n## 現在地\n"
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_accepts_nested_quote_prefix_with_spaces() -> None:
    text = "# next_plan\n> > 最終更新: 2026-06-13 15:42 JST\n## 現在地\n"
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_ignores_historical_update_after_first_section() -> None:
    text = (
        "# next_plan\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "## 現在地\n"
        "> 過去ログ\n"
        "> 最終更新: 2026-06-14 10:00 JST\n"
    )
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_parse_updated_at_stops_at_quoted_section_heading() -> None:
    text = (
        "# next_plan\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "> ## 過去ログ\n"
        "> 最終更新: 2026-06-14 10:00 JST\n"
    )
    assert parse_updated_at(text) == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()


def test_collect_progress_prefers_recorded_timestamp(tmp_path: Path) -> None:
    body = "# a\n> 最終更新: 2026-06-13 15:42 JST\n## 次の一手\n- do\n"
    _mk(tmp_path / "a", next_plan=body)
    p = tmp_path / "a" / "docs" / "next_plan.md"
    os.utime(p, (1.0, 1.0))  # mtime をわざと大昔に → 記録時刻が優先されるはず
    it = collect_progress(tmp_path)[0]
    assert it.updated == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()
    assert it.updated_source == "header"
    assert it.mtime == 1.0  # mtime は透明性のため別途保持


def test_collect_progress_uses_session_summary_recorded_timestamp(tmp_path: Path) -> None:
    body = (
        "# Session Summary\n"
        "- **最終更新**: 2026-06-13 15:42 JST\n"
        "- **プロジェクト**: a\n"
    )
    _mk(tmp_path / "a", session_summary=body)
    p = tmp_path / "a" / "docs" / "SESSION_SUMMARY.md"
    os.utime(p, (1.0, 1.0))
    it = collect_progress(tmp_path)[0]
    assert it.source == "session_summary"
    assert it.updated == datetime(
        2026, 6, 13, 15, 42, tzinfo=timezone(timedelta(hours=9))
    ).timestamp()
    assert it.updated_source == "header"


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


def test_progress_format_gaps_reports_missing_canonical_sections() -> None:
    text = "# p\n> 最終更新: 2026-06-13 15:42 JST\n## 次の一手\n- do\n"
    assert progress_format_gaps(text) == ("現在地", "直近の成果", "環境メモ")


def test_progress_format_gaps_accepts_heading_suffixes() -> None:
    text = (
        "# p\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "## 現在地（2026-06-13）\n"
        "## 直近の成果\n"
        "## 次の一手 (優先順)\n"
        "## 環境メモ\n"
    )
    assert progress_format_gaps(text) == ()


def test_progress_format_gaps_reports_out_of_order_sections() -> None:
    text = (
        "# p\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "## 現在地\n"
        "## 次の一手\n"
        "## 直近の成果\n"
        "## 環境メモ\n"
    )
    assert progress_format_gaps(text) == ("見出し順序",)


def test_progress_format_gaps_ignores_quoted_and_fenced_headings() -> None:
    text = (
        "# p\n"
        "> 最終更新: 2026-06-13 15:42 JST\n"
        "> ## 現在地\n"
        "```md\n"
        "## 直近の成果\n"
        "## 環境メモ\n"
        "```\n"
        "## 次の一手\n"
    )
    assert progress_format_gaps(text) == ("現在地", "直近の成果", "環境メモ")


def test_build_common_summary_includes_format_gap_annotations() -> None:
    items = [
        ProjectProgress("fmt", Path("f"), "# f\n## 次の一手\n- do", updated=200.0, source="next_plan",
                        mtime=200.0, updated_source="mtime",
                        format_gaps=("最終更新(YYYY-MM-DD HH:MM JST)", "現在地", "直近の成果", "環境メモ")),
    ]
    out = build_common_summary(items, fmt=lambda t: f"T{int(t)}")
    assert "[format: 最終更新(YYYY-MM-DD HH:MM JST), 現在地, 直近の成果, 環境メモ]" in out
    assert "> format gaps: 最終更新(YYYY-MM-DD HH:MM JST), 現在地, 直近の成果, 環境メモ" in out


def test_infer_common_summary_paths_from_project_child(tmp_path: Path) -> None:
    proj = tmp_path / "alpha"
    sub = proj / "src" / "pkg"
    sub.mkdir(parents=True)
    _mk(proj, next_plan="# alpha\n")
    got = infer_common_summary_paths(sub, projects_root=tmp_path)
    assert got == (tmp_path, tmp_path / "_shared" / "PROGRESS.md")


def test_refresh_common_summary_for_project_writes_shared_progress(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-06-13 15:42 JST\n")
    _mk(beta, next_plan="# beta\n> 最終更新: 2026-06-13 15:41 JST\n")
    text = refresh_common_summary_for_project(alpha / "src", projects_root=tmp_path)
    out = tmp_path / "_shared" / "PROGRESS.md"
    assert text is not None
    assert out.exists()
    shown = out.read_text(encoding="utf-8")
    assert "**alpha**" in shown and "**beta**" in shown


def test_write_common_summary_falls_back_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    alpha = tmp_path / "alpha"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-07-10 04:34 JST\n")
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    out.write_text("stale", encoding="utf-8")

    original_replace = Path.replace

    def _boom(self: Path, target: Path) -> Path:
        raise OSError("replace blocked")

    monkeypatch.setattr(Path, "replace", _boom)
    try:
        text = write_common_summary(tmp_path, out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
    assert "**alpha**: 2026-07-10 04:34" in text
    assert out.read_text(encoding="utf-8") == "stale"


def test_common_summary_tmp_path_is_unique(tmp_path: Path) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    p1 = _common_summary_tmp_path(out)
    p2 = _common_summary_tmp_path(out)
    assert p1 != p2
    assert p1.parent == out.parent == p2.parent
    assert p1.name.startswith("PROGRESS.md.")
    assert p1.name.endswith(".tmp")


def test_write_text_sync_flushes_and_fsyncs(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "x.txt"
    calls: list[int] = []
    original_fsync = os.fsync

    def _capture(fd: int) -> None:
        calls.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", _capture)
    _write_text_sync(out, "hello")
    assert out.read_text(encoding="utf-8") == "hello"
    assert len(calls) == 1


def test_commit_summary_text_fsyncs_parent_dir(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    calls: list[Path] = []
    original = progress_mod._fsync_parent_dir

    def _capture(path: Path) -> None:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(progress_mod, "_fsync_parent_dir", _capture)
    assert _commit_summary_text(out, "hello\n") is True
    assert out.read_text(encoding="utf-8") == "hello\n"
    assert calls == [out.parent]


def test_write_common_summary_cleans_tmp_when_replace_and_fallback_fail(
    tmp_path: Path, monkeypatch
) -> None:
    alpha = tmp_path / "alpha"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-07-10 04:34 JST\n")
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    out.write_text("stale", encoding="utf-8")

    original_sync = progress_mod._write_text_sync
    original_replace = Path.replace

    def _replace_fail(self: Path, target: Path) -> Path:
        raise OSError("replace blocked")

    def _write_fail_for_out(path: Path, data: str) -> None:
        if path == out:
            raise OSError("direct write blocked")
        return original_sync(path, data)

    monkeypatch.setattr(Path, "replace", _replace_fail)
    monkeypatch.setattr(progress_mod, "_write_text_sync", _write_fail_for_out)
    try:
        text = write_common_summary(tmp_path, out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
        monkeypatch.setattr(progress_mod, "_write_text_sync", original_sync)
    assert "**alpha**: 2026-07-10 04:34" in text
    assert out.read_text(encoding="utf-8") == "stale"
    assert list(out.parent.glob("PROGRESS.md.*.tmp")) == []


def test_write_common_summary_does_not_clobber_newer_readback_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    alpha = tmp_path / "alpha"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-07-10 04:34 JST\n")
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    out.write_text("stale", encoding="utf-8")

    original_replace = Path.replace
    newer = "# 共通進捗サマリー (全プロジェクト集約)\n\n## 最新更新インデックス (新しい順)\n\n- **llterm**: 2026-07-10 04:35  ← 最新\n"

    def _newer_writer_wins(self: Path, target: Path) -> Path:
        target.write_text(newer, encoding="utf-8")
        try:
            self.unlink()
        except OSError:
            pass
        return target

    monkeypatch.setattr(Path, "replace", _newer_writer_wins)
    try:
        text = write_common_summary(tmp_path, out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
    assert "**alpha**: 2026-07-10 04:34" in text
    assert out.read_text(encoding="utf-8") == newer


def test_write_common_summary_rewrites_when_readback_is_older(
    tmp_path: Path, monkeypatch
) -> None:
    alpha = tmp_path / "alpha"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-07-10 04:34 JST\n")
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    out.write_text("stale", encoding="utf-8")

    original_replace = Path.replace
    stale = "# 共通進捗サマリー (全プロジェクト集約)\n\n## 最新更新インデックス (新しい順)\n\n- **llterm**: 2026-07-10 04:33  ← 最新\n"
    calls = 0

    def _stale_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text(stale, encoding="utf-8")
            try:
                self.unlink()
            except OSError:
                pass
            return target
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _stale_replace)
    try:
        text = write_common_summary(tmp_path, out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
    assert calls >= 2
    assert out.read_text(encoding="utf-8") == text


def test_write_common_summary_rewrites_when_readback_is_same_minute_but_stale(
    tmp_path: Path, monkeypatch
) -> None:
    alpha = tmp_path / "alpha"
    _mk(alpha, next_plan="# alpha\n> 最終更新: 2026-07-10 04:34 JST\n")
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    out.write_text("stale", encoding="utf-8")

    original_replace = Path.replace
    same_minute_stale = "# 共通進捗サマリー (全プロジェクト集約)\n\n## 最新更新インデックス (新しい順)\n\n- **other**: 2026-07-10 04:34  ← 最新\n"
    calls = 0

    def _same_minute_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text(same_minute_stale, encoding="utf-8")
            try:
                self.unlink()
            except OSError:
                pass
            return target
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _same_minute_replace)
    try:
        text = write_common_summary(tmp_path, out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
    assert calls >= 2
    assert out.read_text(encoding="utf-8") == text


def test_write_common_summary_items_does_not_clobber_same_minute_readback_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    item = ProjectProgress(
        "alpha",
        out,
        "# alpha\n",
        updated=datetime(2026, 7, 10, 4, 34, tzinfo=timezone(timedelta(hours=9))).timestamp(),
        source="next_plan",
        updated_source="header",
    )

    original_replace = Path.replace
    original_sidecar = progress_mod._write_common_summary_meta
    same_minute_other = (
        "# 共通進捗サマリー (全プロジェクト集約)\n\n"
        "## 最新更新インデックス (新しい順)\n\n"
        "- **other**: 2026-07-10 04:34  ← 最新\n"
    )
    other_item = ProjectProgress(
        "other",
        out,
        "# other\n",
        updated=item.updated,
        mtime=item.mtime,
        source="next_plan",
        updated_source="header",
    )
    calls = 0

    def _same_minute_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        if target != out:
            return original_replace(self, target)
        calls += 1
        target.write_text(same_minute_other, encoding="utf-8")
        try:
            self.unlink()
        except OSError:
            pass
        return target

    monkeypatch.setattr(Path, "replace", _same_minute_replace)
    monkeypatch.setattr(
        progress_mod,
        "_write_common_summary_meta",
        lambda path, snapshot: original_sidecar(path, progress_mod._common_summary_snapshot([other_item])),
    )
    try:
        text = write_common_summary_items([item], out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
        monkeypatch.setattr(progress_mod, "_write_common_summary_meta", original_sidecar)
    assert calls == 1
    assert "**alpha**: 2026-07-10 04:34" in text
    assert out.read_text(encoding="utf-8") == same_minute_other


def test_write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak(
    tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    item = ProjectProgress(
        "alpha",
        out,
        "# alpha\n",
        updated=datetime(2026, 7, 10, 4, 34, tzinfo=timezone(timedelta(hours=9))).timestamp(),
        source="next_plan",
        mtime=1720557299.0,
        updated_source="header",
    )

    original_replace = Path.replace
    older_same_minute = (
        "# 共通進捗サマリー (全プロジェクト集約)\n\n"
        "## 最新更新インデックス (新しい順)\n\n"
        "- **alpha**: 2026-07-10 04:34  ← 最新\n"
    )
    calls = 0

    def _older_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        if target != out:
            return original_replace(self, target)
        calls += 1
        if calls == 1:
            target.write_text(older_same_minute, encoding="utf-8")
            try:
                self.unlink()
            except OSError:
                pass
            return target
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _older_replace)
    try:
        text = write_common_summary_items([item], out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
    assert calls >= 2
    shown = out.read_text(encoding="utf-8")
    assert shown == text
    assert "<!--" not in shown


def test_write_common_summary_items_rewrites_when_lower_project_only_changes_same_minute(
    tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    shared = datetime(2026, 7, 10, 4, 34, tzinfo=timezone(timedelta(hours=9))).timestamp()
    alpha = ProjectProgress("alpha", out, "# alpha\n", updated=shared, mtime=100.0, source="next_plan")
    beta_new = ProjectProgress("beta", out, "# beta\nnew body\n", updated=shared, mtime=250.0, source="next_plan")
    beta_old = ProjectProgress("beta", out, "# beta\nold body\n", updated=shared, mtime=200.0, source="next_plan")

    original_replace = Path.replace
    original_sidecar = progress_mod._write_common_summary_meta
    first_text = build_common_summary([alpha, beta_old])
    calls = 0

    def _older_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        if target != out:
            return original_replace(self, target)
        calls += 1
        if calls == 1:
            target.write_text(first_text, encoding="utf-8")
            try:
                self.unlink()
            except OSError:
                pass
            return target
        return original_replace(self, target)

    def _older_meta(path: Path, snapshot: tuple[tuple[str, float, float], ...]) -> None:
        if calls == 1:
            return original_sidecar(path, progress_mod._common_summary_snapshot([alpha, beta_old]))
        return original_sidecar(path, snapshot)

    monkeypatch.setattr(Path, "replace", _older_replace)
    monkeypatch.setattr(progress_mod, "_write_common_summary_meta", _older_meta)
    try:
        text = write_common_summary_items([alpha, beta_new], out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
        monkeypatch.setattr(progress_mod, "_write_common_summary_meta", original_sidecar)
    assert calls >= 2
    assert out.read_text(encoding="utf-8") == text


def test_write_common_summary_items_recency_compare_ignores_project_name_order(
    tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "_shared" / "PROGRESS.md"
    out.parent.mkdir(parents=True)
    base_updated = datetime(2026, 7, 10, 4, 34, tzinfo=timezone(timedelta(hours=9))).timestamp()
    fresh_alpha = ProjectProgress(
        "alpha",
        out,
        "# alpha\n",
        updated=base_updated,
        mtime=200.0,
        source="next_plan",
        updated_source="header",
    )
    stale_zebra = ProjectProgress(
        "zebra",
        out,
        "# zebra\n",
        updated=base_updated - 60.0,
        mtime=100.0,
        source="next_plan",
        updated_source="header",
    )
    fresh_text = build_common_summary([fresh_alpha])
    original_replace = Path.replace
    original_sidecar = progress_mod._write_common_summary_meta
    calls = 0

    def _stale_replace(self: Path, target: Path) -> Path:
        nonlocal calls
        if target != out:
            return original_replace(self, target)
        calls += 1
        target.write_text(fresh_text, encoding="utf-8")
        try:
            self.unlink()
        except OSError:
            pass
        return target

    monkeypatch.setattr(Path, "replace", _stale_replace)
    monkeypatch.setattr(
        progress_mod,
        "_write_common_summary_meta",
        lambda path, snapshot: original_sidecar(path, progress_mod._common_summary_snapshot([fresh_alpha])),
    )
    try:
        write_common_summary_items([stale_zebra], out)
    finally:
        monkeypatch.setattr(Path, "replace", original_replace)
        monkeypatch.setattr(progress_mod, "_write_common_summary_meta", original_sidecar)
    assert calls == 1
    assert out.read_text(encoding="utf-8") == fresh_text


def test_infer_common_summary_paths_ignores_nested_subproject_markers(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    nested = alpha / "subproj"
    _mk(alpha, next_plan="# alpha\n")
    _mk(nested, next_plan="# nested\n")
    got = infer_common_summary_paths(nested / "src", projects_root=tmp_path)
    assert got == (tmp_path, tmp_path / "_shared" / "PROGRESS.md")


def test_refresh_common_summary_for_project_returns_none_outside_projects_root(tmp_path: Path) -> None:
    proj = tmp_path / "alpha"
    _mk(proj, next_plan="# alpha\n")
    other_root = tmp_path / "other"
    other_root.mkdir()
    assert refresh_common_summary_for_project(proj, projects_root=other_root) is None


def test_parse_session_summary_seed_extracts_metadata() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-05-10 10:19:11\n"
        "- **プロジェクト**: `D:/projects/browser-use-project`\n"
        "- **ブランチ**: `master`\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
        "- test を更新\n"
        "## 次にやるべきこと\n"
        "- follow up 1\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.updated_label == "2026-05-10 10:19:11"
    assert seed.project_label == "D:/projects/browser-use-project"
    assert seed.review_target_label is None
    assert seed.branch_label == "master"
    assert seed.latest_heading == "2026-05-10 セッション総括"
    assert seed.latest_bullets == ("feature A を追加", "test を更新")
    assert seed.finding_highlights == ()
    assert seed.exclusion_highlights == ()
    assert seed.next_steps == ("follow up 1",)
    assert seed.environment_notes == ()
    assert seed.environment_notes_omitted == 0


def test_parse_session_summary_seed_does_not_reopen_metadata_after_preamble_text() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-05-10 10:19:11\n"
        "この要約は review continuation 用の前置き。\n"
        "- **プロジェクト**: `D:/projects/stale-should-not-parse`\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.updated_label == "2026-05-10 10:19:11"
    assert seed.project_label is None
    assert seed.latest_heading == "2026-05-10 セッション総括"
    assert seed.latest_bullets == ("feature A を追加",)


def test_parse_session_summary_seed_accepts_quoted_preamble_before_metadata() -> None:
    text = (
        "# Session Summary (auto-generated)\n"
        "\n"
        "> 自動生成: `libexec/raptor-auto-summary` (Stop hook)\n"
        "> 次回 ccr 起動時に CLAUDE.md SESSION START で自動的に読み取られる。\n"
        "\n"
        "- **最終更新**: 2026-05-10 10:19:11\n"
        "- **プロジェクト**: `D:/projects/browser-use-project`\n"
        "- **ブランチ**: `master`\n"
        "\n"
        "## 直近の git log\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.updated_label == "2026-05-10 10:19:11"
    assert seed.project_label == "D:/projects/browser-use-project"
    assert seed.branch_label == "master"


def test_parse_session_summary_seed_accepts_quoted_preamble_before_manual_metadata() -> None:
    text = (
        "# Session Summary (manual EXIT prep override)\n"
        "\n"
        "> 通常は Stop hook の auto-summary が入るが、今回はコンテキスト上限前の EXIT 準備として手動更新。\n"
        "> 次回開始時は stale な自動要約ではなく、まず `docs/next_plan.md` を優先して読むこと。\n"
        "\n"
        "- **最終更新**: 2026-07-09\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "- **実レビュー対象**: `D:/projects/browser-use-project`\n"
        "\n"
        "## 今回の要点\n"
        "- note\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.updated_label == "2026-07-09"
    assert seed.project_label == "D:/projects/onocollo-complete"
    assert seed.review_target_label == "D:/projects/browser-use-project"


def test_parse_session_summary_seed_extracts_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`\n"
        "- GUI: `llterm-gui --real`\n"
        "補足メモ\n"
        "## 次にやるべきこと\n"
        "- follow up 1\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.environment_notes == (
        "CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`",
        "GUI: `llterm-gui --real`",
        "補足メモ",
    )
    assert seed.environment_notes_omitted == 0


def test_parse_session_summary_seed_skips_environment_heading_for_latest_heading() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- GUI: `llterm-gui --real`\n"
        "## 環境メモ\n"
        "- codex は danger-full-access\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
        "## 次にやるべきこと\n"
        "- follow up 1\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.latest_heading == "2026-05-10 セッション総括"
    assert seed.latest_bullets == ("feature A を追加",)


def test_parse_session_summary_seed_accepts_next_steps_heading_suffix() -> None:
    text = (
        "# Session Summary\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
        "## 次にやるべきこと (TRIZ ideation の生存案 / 未着手)\n"
        "- follow up 1\n"
        "- follow up 2\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.latest_heading == "2026-05-10 セッション総括"
    assert seed.next_steps == ("follow up 1", "follow up 2")


def test_parse_session_summary_seed_accepts_concrete_next_step_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "- **実レビュー対象**: `D:/projects/browser-use-project`\n"
        "## 今回の要点\n"
        "- review target を確定\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
        "2. false positive を除外する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.project_label == "D:/projects/onocollo-complete"
    assert seed.review_target_label == "D:/projects/browser-use-project"
    assert seed.latest_heading == "今回の要点"
    assert seed.finding_highlights == ()
    assert seed.exclusion_highlights == ()
    assert seed.next_steps == ("findings を圧縮する", "false positive を除外する")


def test_parse_session_summary_seed_extracts_finding_highlights() -> None:
    text = (
        "# Session Summary\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 確定 Findings\n"
        "- `Critical`: finding 1\n"
        "- `High`: finding 2\n"
        "- `Medium`: finding 3\n"
        "- `Low`: finding 4\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.finding_highlights == (
        "`Critical`: finding 1",
        "`High`: finding 2",
        "`Medium`: finding 3",
    )


def test_parse_session_summary_seed_skips_findings_heading_for_latest_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 確定 Findings\n"
        "- `Critical`: finding 1\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.finding_highlights == ("`Critical`: finding 1",)
    assert seed.latest_heading == "今回の要点"
    assert seed.latest_bullets == ("live review target を確定",)


def test_parse_session_summary_seed_extracts_exclusion_highlights() -> None:
    text = (
        "# Session Summary\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 除外済みメモ\n"
        "- memo 1\n"
        "- memo 2\n"
        "- memo 3\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.exclusion_highlights == ("memo 1", "memo 2")


def test_parse_session_summary_seed_skips_exclusion_heading_for_latest_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 除外済みメモ\n"
        "- memo 1\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.exclusion_highlights == ("memo 1",)
    assert seed.latest_heading == "今回の要点"
    assert seed.latest_bullets == ("live review target を確定",)


def test_scaffold_next_plan_text_does_not_show_findings_as_latest_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 確定 Findings\n"
        "- `Critical`: finding 1\n"
        "- `High`: finding 2\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.latest_heading is None
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "最新セクション見出し: `確定 Findings`" not in out
    assert "- `Critical`: finding 1" in out
    assert "- `High`: finding 2" in out


def test_parse_session_summary_seed_accepts_numbered_next_steps() -> None:
    text = (
        "# Session Summary\n"
        "## 2026-05-10 セッション総括\n"
        "1. feature A を追加\n"
        "2. test を更新\n"
        "## 次にやるべきこと\n"
        "1. follow up 1\n"
        "2. follow up 2\n"
        "3. follow up 3\n"
        "4. follow up 4\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.latest_bullets == ("feature A を追加", "test を更新")
    assert seed.next_steps == ("follow up 1", "follow up 2", "follow up 3")


def test_parse_session_summary_seed_accepts_star_bullets() -> None:
    text = (
        "# Session Summary\n"
        "## 2026-05-10 セッション総括\n"
        "* feature A を追加\n"
        "* test を更新\n"
        "## 次にやるべきこと\n"
        "* follow up 1\n"
        "* follow up 2\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.latest_bullets == ("feature A を追加", "test を更新")
    assert seed.next_steps == ("follow up 1", "follow up 2")


def test_parse_session_summary_seed_keeps_indented_continuation_lines() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと (TRIZ ideation の生存案 / 未着手)\n"
        "- handoff 自己検証ゲート\n"
        "  検証は exit_prep 前スナップショット比較 + 非空 + mtime の複合シグナルに絞る\n"
        "- ctl-gated 自律 rotate\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "handoff 自己検証ゲート 検証は exit_prep 前スナップショット比較 + 非空 + mtime の複合シグナルに絞る",
        "ctl-gated 自律 rotate",
    )


def test_parse_session_summary_seed_keeps_blank_line_continuation_paragraph() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと\n"
        "- handoff 自己検証ゲート\n"
        "\n"
        "  検証は exit_prep 前スナップショット比較 + 非空 + mtime の複合シグナルに絞る\n"
        "- ctl-gated 自律 rotate\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "handoff 自己検証ゲート 検証は exit_prep 前スナップショット比較 + 非空 + mtime の複合シグナルに絞る",
        "ctl-gated 自律 rotate",
    )


def test_parse_session_summary_seed_keeps_nested_bullets_inside_parent_item() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと\n"
        "- handoff 自己検証ゲート\n"
        "  - exit_prep 前スナップショット比較\n"
        "  - 非空 + mtime の複合シグナル\n"
        "- ctl-gated 自律 rotate\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "handoff 自己検証ゲート exit_prep 前スナップショット比較 非空 + mtime の複合シグナル",
        "ctl-gated 自律 rotate",
    )


def test_parse_session_summary_seed_skips_completed_next_steps() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと\n"
        "1. ~~多言語対応 (i18n)~~ — 実装完了\n"
        "2. handoff 自己検証ゲート\n"
        "3. ctl-gated 自律 rotate\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "handoff 自己検証ゲート",
        "ctl-gated 自律 rotate",
    )


def test_parse_session_summary_seed_keeps_remaining_work_from_partial_completion() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと\n"
        "1. ~~多言語対応 (i18n)~~ — 実装完了。残: zh/ko 訳 + GUI 言語切替 UI\n"
        "2. handoff 自己検証ゲート\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "zh/ko 訳 + GUI 言語切替 UI",
        "handoff 自己検証ゲート",
    )


def test_parse_session_summary_seed_keeps_remaining_work_from_fullwidth_or_english_marker() -> None:
    text = (
        "# Session Summary\n"
        "## 次にやるべきこと\n"
        "1. ~~多言語対応 (i18n)~~ — 実装完了。残：zh/ko 訳\n"
        "2. ~~Real-time display~~ resolved. remaining: GUI polish\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.next_steps == (
        "zh/ko 訳",
        "GUI polish",
    )


def test_parse_session_summary_seed_skips_noisy_git_headings_for_latest_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **プロジェクト**: `D:/projects/browser-use-project`\n"
        "## 直近の git log\n"
        "## 現在の git status\n"
        "## 直近 2 時間に変更されたファイル\n"
        "10:18 docs/SESSION_SUMMARY.md\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.project_label == "D:/projects/browser-use-project"
    assert seed.latest_heading is None
    assert seed.latest_bullets == ()


def test_scaffold_next_plan_text_creates_canonical_sections() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-05-10 10:19:11\n"
        "- **プロジェクト**: `D:/projects/browser-use-project`\n"
        "- **ブランチ**: `master`\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
        "- test を更新\n"
        "## 次にやるべきこと\n"
        "- follow up 1\n"
        "- follow up 2\n"
    )
    out = scaffold_next_plan_text("browser-use-project", text)
    assert out.startswith("# next_plan (正本) — browser-use-project")
    assert "> 最終更新: 2026-05-10 10:19 JST" in out
    assert "## 現在地" in out
    assert "## 直近の成果" in out
    assert "## 次の一手" in out
    assert "## 環境メモ" in out
    assert "`master`" in out
    assert "最新セクション見出し: `2026-05-10 セッション総括`" in out
    assert "- feature A を追加" in out
    assert "- follow up 1" in out


def test_scaffold_next_plan_text_carries_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "- **ブランチ**: `main`\n"
        "## 起動方法\n"
        "- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`\n"
        "- GUI: `llterm-gui --real`\n"
        "## 環境メモ\n"
        "- codex は danger-full-access\n"
        "## 次にやるべきこと\n"
        "- follow up 1\n"
    )
    out = scaffold_next_plan_text("alpha", text)
    assert "- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`" in out
    assert "- GUI: `llterm-gui --real`" in out
    assert "- codex は danger-full-access" in out


def test_scaffold_next_plan_text_omits_unknown_branch_lines() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **ブランチ**: `不明`\n## 次にやるべきこと\n- follow up 1\n",
        project_path="D:/custom-root/alpha",
        fallback_epoch=60.0,
    )
    assert "branch は `不明`" not in out
    assert "直近 branch: `不明`" not in out
    assert "- 直近の自動要約では project path は `D:/custom-root/alpha`。" in out


def test_scaffold_next_plan_text_omits_english_unknown_branch_sentinel() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **ブランチ**: `unknown`\n## 次にやるべきこと\n- follow up 1\n",
        project_path="D:/custom-root/alpha",
        fallback_epoch=60.0,
    )
    assert "branch は `unknown`" not in out
    assert "直近 branch: `unknown`" not in out


def test_scaffold_next_plan_text_omits_unknown_review_target_sentinel() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **実レビュー対象**: `不明`\n## 次にやるべきこと\n- follow up 1\n",
        project_path="D:/custom-root/alpha",
        fallback_epoch=60.0,
    )
    assert "実レビュー対象: `不明`" not in out


def test_scaffold_next_plan_text_normalizes_unknown_project_label_to_explicit_path() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **プロジェクト**: `unknown`\n## 次にやるべきこと\n- follow up 1\n",
        project_path="D:/custom-root/alpha",
        fallback_epoch=60.0,
    )
    assert "`D:/custom-root/alpha`" in out
    assert "`unknown`" not in out


def test_scaffold_next_plan_text_uses_project_alias_and_concrete_next_step_heading() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "- **実レビュー対象**: `D:/projects/browser-use-project`\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "`D:/projects/onocollo-complete`" in out
    assert "実レビュー対象: `D:/projects/browser-use-project`" in out
    assert "branch は `不明`" not in out
    assert "- live review target を確定" in out
    assert "- findings を圧縮する" in out


def test_scaffold_next_plan_text_carries_finding_highlights() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 確定 Findings\n"
        "- `Critical`: finding 1\n"
        "- `High`: finding 2\n"
        "- `Medium`: finding 3\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "- live review target を確定" in out
    assert "- `Critical`: finding 1" in out
    assert "- `High`: finding 2" in out
    assert "- `Medium`: finding 3" in out


def test_scaffold_next_plan_text_carries_exclusion_highlights() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 除外済みメモ\n"
        "- memo 1\n"
        "- memo 2\n"
        "- memo 3\n"
    )
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "- 除外済みメモ: memo 1" in out
    assert "- 除外済みメモ: memo 2" in out
    assert "- 除外済みメモ: memo 3" not in out


def test_scaffold_next_plan_text_keeps_findings_when_latest_bullets_has_multiple_items() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**: `D:/projects/onocollo-complete`\n"
        "## 今回の要点\n"
        "- summary 1\n"
        "- summary 2\n"
        "## 確定 Findings\n"
        "- `Critical`: finding 1\n"
        "- `High`: finding 2\n"
        "- `Medium`: finding 3\n"
    )
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "- summary 1" in out
    assert "- summary 2" in out
    assert "- `Critical`: finding 1" in out
    assert "- `High`: finding 2" in out
    assert "- `Medium`: finding 3" in out


def test_scaffold_next_plan_text_handles_real_data_style_alias_heading_and_noise_together() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-09 09:15:00\n"
        "- **作業プロジェクト**： `D:/projects/onocollo-complete`\n"
        "- **実レビュー対象**： `D:/projects/browser-use-project`\n"
        "## 直近の git log\n"
        "## 現在の git status\n"
        "## 直近 2 時間に変更されたファイル\n"
        "10:18 docs/SESSION_SUMMARY.md\n"
        "## 今回の要点\n"
        "- live review target を確定\n"
        "## 除外済みメモ\n"
        "- memo 1\n"
        "## 次の具体的一手\n"
        "1. findings を圧縮する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.project_label == "D:/projects/onocollo-complete"
    assert seed.review_target_label == "D:/projects/browser-use-project"
    assert seed.latest_heading == "今回の要点"
    assert seed.exclusion_highlights == ("memo 1",)
    assert seed.next_steps == ("findings を圧縮する",)
    out = scaffold_next_plan_text("onocollo-complete", text)
    assert "`D:/projects/onocollo-complete`" in out
    assert "実レビュー対象: `D:/projects/browser-use-project`" in out
    assert "- 除外済みメモ: memo 1" in out
    assert "最新セクション見出し: `今回の要点`" in out
    assert "- findings を圧縮する" in out


def test_parse_session_summary_seed_keeps_bold_label_bullets_inside_sections() -> None:
    text = (
        "# Session Summary\n"
        "## 2026-06-12 (夜) 多言語対応 (i18n) 実装完了\n"
        "- **置換**: GUI 全文字列 40+ を差し替え\n"
        "- **README**: README.en.md 完全英訳新設\n"
        "- **テスト**: 207 passed\n"
        "## 起動方法\n"
        "- **テンプレ(機能別)**: `general` / `rad_expand`\n"
        "- **RAD 連携**: `D:/docs/*_corpus_v2` を grep してから着手\n"
        "## 次にやるべきこと\n"
        "- zh/ko 訳を追加する\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.project_label is None
    assert seed.latest_heading == "2026-06-12 (夜) 多言語対応 (i18n) 実装完了"
    assert seed.latest_bullets == (
        "**置換**: GUI 全文字列 40+ を差し替え",
        "**README**: README.en.md 完全英訳新設",
        "**テスト**: 207 passed",
    )
    assert seed.environment_notes == (
        "**テンプレ(機能別)**: `general` / `rad_expand`",
        "**RAD 連携**: `D:/docs/*_corpus_v2` を grep してから着手",
    )


def test_scaffold_next_plan_text_keeps_bold_label_bullets_inside_sections() -> None:
    text = (
        "# Session Summary\n"
        "- **最終更新**: 2026-07-10 01:35:00\n"
        "## 2026-06-12 (夜) 多言語対応 (i18n) 実装完了\n"
        "- **置換**: GUI 全文字列 40+ を差し替え\n"
        "- **README**: README.en.md 完全英訳新設\n"
        "- **テスト**: 207 passed\n"
        "## 起動方法\n"
        "- **テンプレ(機能別)**: `general` / `rad_expand`\n"
        "- **RAD 連携**: `D:/docs/*_corpus_v2` を grep してから着手\n"
        "## 次にやるべきこと\n"
        "- zh/ko 訳を追加する\n"
    )
    out = scaffold_next_plan_text("llterm", text)
    assert "- **置換**: GUI 全文字列 40+ を差し替え" in out
    assert "- **README**: README.en.md 完全英訳新設" in out
    assert "- **テスト**: 207 passed" in out
    assert "- **テンプレ(機能別)**: `general` / `rad_expand`" in out
    assert "- **RAD 連携**: `D:/docs/*_corpus_v2` を grep してから着手" in out
    assert "- zh/ko 訳を追加する" in out


def test_scaffold_next_plan_text_keeps_four_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- note 1\n"
        "- note 2\n"
        "## 環境メモ\n"
        "- note 3\n"
        "- note 4\n"
        "## 2026-05-10 セッション総括\n"
        "- feature A を追加\n"
    )
    out = scaffold_next_plan_text("alpha", text)
    for note in ("- note 1", "- note 2", "- note 3", "- note 4"):
        assert note in out


def test_parse_session_summary_seed_counts_omitted_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- note 1\n"
        "- note 2\n"
        "## 環境メモ\n"
        "- note 3\n"
        "- note 4\n"
        "- note 5\n"
        "note 6\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.environment_notes == ("note 1", "note 2", "note 3", "note 4")
    assert seed.environment_notes_omitted == 2


def test_parse_session_summary_seed_counts_duplicate_omitted_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- note 1\n"
        "- note 2\n"
        "## 環境メモ\n"
        "- note 3\n"
        "- note 4\n"
        "- note 1\n"
        "note 2\n"
    )
    seed = parse_session_summary_seed(text)
    assert seed.environment_notes == ("note 1", "note 2", "note 3", "note 4")
    assert seed.environment_notes_omitted == 2


def test_scaffold_next_plan_text_marks_omitted_environment_notes() -> None:
    text = (
        "# Session Summary\n"
        "## 起動方法\n"
        "- note 1\n"
        "- note 2\n"
        "## 環境メモ\n"
        "- note 3\n"
        "- note 4\n"
        "- note 5\n"
    )
    out = scaffold_next_plan_text("alpha", text)
    assert "環境メモの追加 1 件は未転記" in out


def test_scaffold_next_plan_text_falls_back_to_mtime_not_now() -> None:
    out = scaffold_next_plan_text("alpha", "# Session Summary\n", fallback_epoch=60.0)
    assert "> 最終更新: 1970-01-01 09:01 JST" in out


def test_scaffold_next_plan_text_uses_explicit_project_path_fallback() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **ブランチ**: `main`\n",
        project_path="D:/custom-root/alpha",
        fallback_epoch=60.0,
    )
    assert "`D:/custom-root/alpha`" in out
    assert "初期 scaffold 元: `D:/custom-root/alpha/docs/SESSION_SUMMARY.md`" in out


def test_scaffold_next_plan_text_uses_default_projects_root_fallback() -> None:
    out = scaffold_next_plan_text(
        "alpha",
        "# Session Summary\n- **ブランチ**: `main`\n",
        fallback_epoch=60.0,
    )
    expected = (DEFAULT_PROJECTS_ROOT / "alpha").as_posix()
    assert f"`{expected}`" in out
    assert f"初期 scaffold 元: `{expected}/docs/SESSION_SUMMARY.md`" in out


def test_scaffold_next_plan_for_project_creates_file_without_overwriting(tmp_path: Path) -> None:
    proj = tmp_path / "alpha"
    _mk(
        proj,
        session_summary=(
            "# Session Summary\n"
            "- **最終更新**: 2026-05-10 10:19:11\n"
            "- **プロジェクト**: `D:/projects/alpha`\n"
            "- **ブランチ**: `main`\n"
        ),
    )
    created = scaffold_next_plan_for_project(proj)
    assert created == proj / "docs" / "next_plan.md"
    body = created.read_text(encoding="utf-8")
    assert "## 次の一手" in body

    created.write_text("keep", encoding="utf-8")
    assert scaffold_next_plan_for_project(proj) is None
    assert created.read_text(encoding="utf-8") == "keep"


def test_scaffold_next_plan_for_project_uses_actual_project_dir_when_metadata_missing(tmp_path: Path) -> None:
    proj = tmp_path / "alpha"
    _mk(
        proj,
        session_summary=(
            "# Session Summary\n"
            "- **最終更新**: 2026-05-10 10:19:11\n"
            "- **ブランチ**: `main`\n"
        ),
    )
    created = scaffold_next_plan_for_project(proj)
    assert created == proj / "docs" / "next_plan.md"
    body = created.read_text(encoding="utf-8")
    expected = proj.as_posix()
    assert f"`{expected}`" in body
    assert f"初期 scaffold 元: `{expected}/docs/SESSION_SUMMARY.md`" in body


def test_scaffold_next_plan_for_project_prefers_actual_project_dir_over_stale_metadata(tmp_path: Path) -> None:
    proj = tmp_path / "alpha"
    _mk(
        proj,
        session_summary=(
            "# Session Summary\n"
            "- **最終更新**: 2026-05-10 10:19:11\n"
            "- **プロジェクト**: `D:/projects/stale-alpha`\n"
            "- **ブランチ**: `main`\n"
        ),
    )
    created = scaffold_next_plan_for_project(proj)
    assert created == proj / "docs" / "next_plan.md"
    body = created.read_text(encoding="utf-8")
    expected = proj.as_posix()
    assert f"`{expected}`" in body
    assert "D:/projects/stale-alpha/docs/SESSION_SUMMARY.md" not in body
    assert f"初期 scaffold 元: `{expected}/docs/SESSION_SUMMARY.md`" in body


def test_scaffold_missing_next_plans_only_targets_session_summary_projects(tmp_path: Path) -> None:
    _mk(tmp_path / "alpha", session_summary="- **最終更新**: 2026-05-10 10:19:11")
    _mk(tmp_path / "beta", next_plan="# next_plan\n")
    created = scaffold_missing_next_plans(tmp_path)
    assert created == [tmp_path / "alpha" / "docs" / "next_plan.md"]


def test_progress_main_scaffold_errors_for_missing_projects_root(tmp_path: Path, capsys) -> None:
    rc = progress_main(["--projects-root", str(tmp_path / "missing"), "--scaffold-next-plan", "all"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "projects-root" in out


def test_progress_main_scaffold_requires_explicit_target(tmp_path: Path, capsys) -> None:
    rc = progress_main(["--projects-root", str(tmp_path), "--scaffold-next-plan"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "PROJECT" in out or "all" in out
