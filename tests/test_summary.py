# SPDX-License-Identifier: Apache-2.0
"""summarize_for_human (進捗サマリの人間向けダイジェスト) の回帰テスト。"""
from __future__ import annotations

from llterm.summary import summarize_for_human


def test_empty_returns_empty() -> None:
    assert summarize_for_human("") == ""
    assert summarize_for_human("   \n\n  ") == ""


def test_extracts_three_buckets_from_headings() -> None:
    text = (
        "# セッションサマリ\n"
        "## 現在地\n"
        "llcore Phase 2 を実装中。\n"
        "## 直近の成果\n"
        "- model selector 追加\n"
        "- codex truncation 修正\n"
        "## 次の一手\n"
        "オーケストラ層を実装する。\n"
    )
    out = summarize_for_human(text)
    assert "【現在地】" in out
    assert "【直近の成果】" in out
    assert "【次の一手】" in out
    assert "llcore Phase 2" in out
    assert "model selector" in out
    assert "オーケストラ層" in out


def test_next_priority_over_done_when_heading_ambiguous() -> None:
    """next/done 両方を含む見出しは next を優先 (『次の進捗』等の取りこぼし防止)。"""
    text = "## 次の進捗 (TODO)\nここを次にやる\n"
    out = summarize_for_human(text)
    assert "ここを次にやる" in out
    # next バケットに入る (【次の一手】見出しの後に本文がある)
    nxt_idx = out.index("【次の一手】")
    assert out.index("ここを次にやる") > nxt_idx


def test_fallback_when_no_classifiable_headings() -> None:
    """見出しが無い/分類不能なサマリは先頭数行を現在地に、next 行を次の一手に拾う。"""
    text = (
        "プロジェクト X の作業ログ。\n"
        "ファイル A を編集した。\n"
        "次回最優先: テストを追加する。\n"
    )
    out = summarize_for_human(text)
    assert "【現在地】" in out
    assert "プロジェクト X" in out
    # 'next回最優先' を含む行が次の一手に拾われる
    assert "テストを追加する" in out
    assert out.index("テストを追加する") > out.index("【次の一手】")


def test_missing_bucket_shows_dash() -> None:
    text = "## 現在地\nいまここ。\n"
    out = summarize_for_human(text)
    # done/next が無いので — を出す (空欄で人を迷わせない)
    assert out.count("—") >= 1


def test_long_section_is_clipped() -> None:
    long_body = "\n".join(f"行{i}: " + "あ" * 50 for i in range(40))
    text = "## 現在地\n" + long_body + "\n"
    out = summarize_for_human(text)
    assert "…" in out  # 切り詰め省略記号
    # 現在地ブロックが全 40 行を出さない (glanceability)
    assert out.count("行") < 40


def test_english_headings_classified() -> None:
    text = (
        "## Current State\nbuilding feature\n"
        "## Results\ndid thing\n"
        "## Next Steps\ndo next thing\n"
    )
    out = summarize_for_human(text)
    assert "building feature" in out
    assert "did thing" in out
    assert "do next thing" in out
    assert out.index("do next thing") > out.index("【次の一手】")
