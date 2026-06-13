# SPDX-License-Identifier: Apache-2.0
"""choice マーカー parser + AskUserQuestion 抽出の unit test (TDD・Qt 非依存)。

llterm が agent (claude) のターン出力から「ユーザーに選択させたい」ブロックを
決定論的に検知する 2 系統をテストする:

1. 規約マーカー ``⟦LLTERM_CHOICE …⟧ … ⟦/LLTERM_CHOICE⟧`` の parse
   (single/multi 判定・選択肢抽出・question 抽出・コードフェンス/diff 無視・複数 choice)。
2. stream-json の AskUserQuestion tool_use イベントからの抽出 (bonus・出れば拾う)。
"""
from __future__ import annotations

from llterm.host.choice import (
    Choice,
    parse_choice_blocks,
    parse_choices_from_text,
    choice_from_ask_user_question_event,
    format_choice_reply,
)


# ─── マーカー parse: 基本 (single / multi / question / 選択肢) ─────────


def test_parse_single_choice_radio() -> None:
    text = (
        "前置きの応答。\n"
        "⟦LLTERM_CHOICE multi=false question=\"公開範囲を選んでください\"⟧\n"
        "1) public\n"
        "2) private\n"
        "3) limited\n"
        "⟦/LLTERM_CHOICE⟧\n"
        "後置きのテキスト。\n"
    )
    choices = parse_choices_from_text(text)
    assert len(choices) == 1
    c = choices[0]
    assert c.multi is False
    assert c.question == "公開範囲を選んでください"
    assert c.options == ["public", "private", "limited"]


def test_parse_multi_choice_checkbox() -> None:
    text = (
        "⟦LLTERM_CHOICE multi=true question=\"対象を選択\"⟧\n"
        "1) tests\n"
        "2) docs\n"
        "3) lint\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    choices = parse_choices_from_text(text)
    assert len(choices) == 1
    assert choices[0].multi is True
    assert choices[0].options == ["tests", "docs", "lint"]


def test_question_from_preceding_heading_when_attr_missing() -> None:
    """question= 属性が無いとき、直前の見出し行を question に採用する。"""
    text = (
        "## どのブランチに進めますか？\n"
        "⟦LLTERM_CHOICE multi=false⟧\n"
        "1) main\n"
        "2) develop\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    choices = parse_choices_from_text(text)
    assert len(choices) == 1
    assert choices[0].question == "どのブランチに進めますか？"
    assert choices[0].options == ["main", "develop"]


def test_attr_question_wins_over_heading() -> None:
    text = (
        "## 見出し由来\n"
        "⟦LLTERM_CHOICE question=\"属性由来\" multi=false⟧\n"
        "1) a\n"
        "2) b\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    assert parse_choices_from_text(text)[0].question == "属性由来"


# ─── 属性欠落の堅牢性 (fail-safe) ─────────────────────────────────


def test_missing_multi_defaults_to_single() -> None:
    """multi= 属性が無いときは single (ラジオ) 扱い (安全側)。"""
    text = (
        "⟦LLTERM_CHOICE⟧\n"
        "1) one\n"
        "2) two\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    c = parse_choices_from_text(text)[0]
    assert c.multi is False
    assert c.question == ""  # 見出しも属性も無ければ空 (ダイアログ側で既定文言)


def test_block_with_no_options_is_ignored() -> None:
    """選択肢が 1 つも無いブロックは無効として捨てる (空ダイアログを出さない)。"""
    text = (
        "⟦LLTERM_CHOICE multi=false question=\"q\"⟧\n"
        "本文だけで番号付き選択肢が無い\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    assert parse_choices_from_text(text) == []


def test_unterminated_block_is_ignored() -> None:
    """終了センチネルが無いブロックは採用しない (fail-safe)。"""
    text = (
        "⟦LLTERM_CHOICE multi=false⟧\n"
        "1) a\n"
        "2) b\n"
        "（閉じ忘れ）\n"
    )
    assert parse_choices_from_text(text) == []


def test_options_accept_various_bullet_styles() -> None:
    """``1)`` ``1.`` ``1:`` ``- `` を選択肢として受ける (ラベルだけ抽出)。"""
    text = (
        "⟦LLTERM_CHOICE multi=true⟧\n"
        "1) alpha\n"
        "2. beta\n"
        "3: gamma\n"
        "- delta\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    assert parse_choices_from_text(text)[0].options == ["alpha", "beta", "gamma", "delta"]


# ─── 誤検知防止: コードフェンス / diff 内は無視・行頭限定 ──────────


def test_marker_inside_code_fence_is_ignored() -> None:
    """コードフェンス (```) 内のマーカーは「説明用の例示」とみなし検知しない。"""
    text = (
        "使い方の例:\n"
        "```\n"
        "⟦LLTERM_CHOICE multi=false⟧\n"
        "1) example\n"
        "⟦/LLTERM_CHOICE⟧\n"
        "```\n"
        "以上が例です。\n"
    )
    assert parse_choices_from_text(text) == []


def test_real_block_after_code_fence_still_detected() -> None:
    """フェンスの外にある本物のブロックはフェンス例示と独立に検知する。"""
    text = (
        "```\n"
        "⟦LLTERM_CHOICE multi=false⟧\n"
        "1) example\n"
        "⟦/LLTERM_CHOICE⟧\n"
        "```\n"
        "では実際にどうしますか？\n"
        "⟦LLTERM_CHOICE multi=false question=\"本物\"⟧\n"
        "1) yes\n"
        "2) no\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    choices = parse_choices_from_text(text)
    assert len(choices) == 1
    assert choices[0].question == "本物"
    assert choices[0].options == ["yes", "no"]


def test_marker_not_at_line_start_is_ignored() -> None:
    """行頭以外 (インデント以外の本文中) に現れたマーカーは検知しない (行頭限定)。"""
    text = "本文の途中に ⟦LLTERM_CHOICE multi=false⟧ と書いても拾わない\n"
    assert parse_choices_from_text(text) == []


def test_marker_inside_git_diff_is_ignored() -> None:
    """git diff の追加行 (+ で始まる) に現れたマーカーは検知しない。"""
    text = (
        "diff --git a/x b/x\n"
        "@@ -1 +1 @@\n"
        "+⟦LLTERM_CHOICE multi=false⟧\n"
        "+1) a\n"
        "+⟦/LLTERM_CHOICE⟧\n"
    )
    assert parse_choices_from_text(text) == []


# ─── 複数 choice: 最後の未応答を採用 ──────────────────────────────


def test_multiple_blocks_returns_all_in_order() -> None:
    text = (
        "⟦LLTERM_CHOICE multi=false question=\"q1\"⟧\n"
        "1) a\n2) b\n"
        "⟦/LLTERM_CHOICE⟧\n"
        "⟦LLTERM_CHOICE multi=true question=\"q2\"⟧\n"
        "1) c\n2) d\n"
        "⟦/LLTERM_CHOICE⟧\n"
    )
    choices = parse_choices_from_text(text)
    assert [c.question for c in choices] == ["q1", "q2"]


def test_last_choice_helper_picks_final_block() -> None:
    """1 ターンに複数あれば最後のブロックを採用する (= 最新の未応答)。"""
    text = (
        "⟦LLTERM_CHOICE multi=false question=\"old\"⟧\n1) a\n⟦/LLTERM_CHOICE⟧\n"
        "⟦LLTERM_CHOICE multi=false question=\"new\"⟧\n1) z\n2) y\n⟦/LLTERM_CHOICE⟧\n"
    )
    last = parse_choice_blocks(text)
    assert last is not None
    assert last.question == "new"
    assert last.options == ["z", "y"]


def test_parse_choice_blocks_none_when_absent() -> None:
    assert parse_choice_blocks("ただの応答テキスト") is None
    assert parse_choice_blocks("") is None


# ─── AskUserQuestion tool_use イベント (bonus) ────────────────────


def test_choice_from_ask_user_question_single() -> None:
    ev = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": "どれにする？",
                "multiSelect": False,
                "options": [{"label": "A"}, {"label": "B"}],
            }]},
        }]},
    }
    c = choice_from_ask_user_question_event(ev)
    assert c is not None
    assert c.multi is False
    assert c.question == "どれにする？"
    assert c.options == ["A", "B"]


def test_choice_from_ask_user_question_multi_and_string_options() -> None:
    ev = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": "複数可",
                "multiSelect": True,
                "options": ["x", "y", "z"],  # str でも label dict でも受ける
            }]},
        }]},
    }
    c = choice_from_ask_user_question_event(ev)
    assert c is not None and c.multi is True
    assert c.options == ["x", "y", "z"]


def test_choice_from_non_ask_event_is_none() -> None:
    assert choice_from_ask_user_question_event({"type": "result"}) is None
    assert choice_from_ask_user_question_event("not a dict") is None
    assert choice_from_ask_user_question_event(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}}) is None


def test_ask_event_without_options_is_none() -> None:
    """options が無い AskUserQuestion は無効 (空ダイアログを出さない)。"""
    ev = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "q", "options": []}]},
        }]},
    }
    assert choice_from_ask_user_question_event(ev) is None


# ─── 回答整形 (番号 + ラベル両方) ─────────────────────────────────


def test_format_reply_single() -> None:
    c = Choice(question="q", options=["public", "private"], multi=False)
    assert format_choice_reply(c, [1]) == "選択: 2) private"


def test_format_reply_multi() -> None:
    c = Choice(question="q", options=["A", "B", "C"], multi=True)
    assert format_choice_reply(c, [0, 2]) == "選択: 1) A, 3) C"


def test_format_reply_empty_selection() -> None:
    c = Choice(question="q", options=["A", "B"], multi=True)
    assert format_choice_reply(c, []) == "選択: (なし)"


def test_format_reply_ignores_out_of_range_indices() -> None:
    """範囲外 index は無視する (堅牢性)。"""
    c = Choice(question="q", options=["A"], multi=True)
    assert format_choice_reply(c, [0, 5, -1]) == "選択: 1) A"
