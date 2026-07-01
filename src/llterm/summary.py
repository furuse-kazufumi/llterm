# SPDX-License-Identifier: Apache-2.0
"""SESSION_SUMMARY.md を人間が一目で判断できるダイジェストへ要約する純関数。

GUI の進捗サマリ パネルは既定でこのダイジェスト (現在地 / 直近の成果 / 次の一手) を出し、
生全文はトグルで切り替える。agent 再取込向けの高密度サマリは人間には判読しづらいため、
見出しキーワードで該当セクションを 3 区分に振り分け、各区分を数行に切り詰めて提示する。

純関数 (Qt 非依存) なので単体テストできる。見出しが無いサマリでも先頭数行 + next 行抽出で
フォールバックする (fail-safe: 空文字でも例外を出さない)。
"""
from __future__ import annotations

from dataclasses import dataclass

# 見出しを 3 区分へ分類するキーワード (小文字照合)。順序は優先度 (next > done > state):
# 「次にやること: 進捗更新」のように next/done 両方を含む見出しは next を優先する。
_NEXT_KEYS = (
    "次の一手", "次にやる", "次に", "次回", "最優先", "top priority", "todo",
    "残作業", "残り", "やるべき", "next step", "next action", "next:", "next ",
)
_DONE_KEYS = (
    "直近の成果", "成果", "完了", "done", "やったこと", "実装済", "完了済",
    "進捗", "achievements", "results", "result", "変更点", "今回",
)
_STATE_KEYS = (
    "現在地", "状況", "概要", "current", "overview", "state", "status",
    "サマリ", "summary", "context",
)

_MAX_LINES_PER_SECTION = 6      # 1 区分あたりの最大表示行数 (glanceability 優先)
_MAX_CHARS_PER_SECTION = 600    # 1 区分あたりの最大文字数 (長大ブロックの切り詰め)
_FALLBACK_HEAD_LINES = 5        # 見出し無しサマリの「現在地」フォールバック行数


@dataclass(frozen=True)
class _Section:
    bucket: str          # "state" | "done" | "next"
    lines: list[str]


def _classify(heading: str) -> str | None:
    """markdown 見出し文字列を 3 区分のいずれか (無ければ None) へ分類する。"""
    h = heading.lower()
    for key in _NEXT_KEYS:
        if key in h:
            return "next"
    for key in _DONE_KEYS:
        if key in h:
            return "done"
    for key in _STATE_KEYS:
        if key in h:
            return "state"
    return None


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _heading_text(line: str) -> str:
    return line.lstrip().lstrip("#").strip()


def _clip(lines: list[str]) -> list[str]:
    """1 区分を最大行数/文字数に切り詰める (末尾に省略記号を付ける)。"""
    out: list[str] = []
    chars = 0
    for ln in lines:
        if len(out) >= _MAX_LINES_PER_SECTION or chars >= _MAX_CHARS_PER_SECTION:
            out.append("  …")
            break
        out.append(ln)
        chars += len(ln)
    return out


def _split_sections(text: str) -> list[_Section]:
    """markdown を (分類済み見出し → 本文行) のセクション列へ分解する。

    見出しの無いサマリは空リストを返す (呼び出し側がフォールバックする)。
    """
    sections: list[_Section] = []
    cur_bucket: str | None = None
    cur_lines: list[str] = []

    def flush() -> None:
        if cur_bucket is not None and cur_lines:
            body = [ln for ln in cur_lines if ln.strip()]
            if body:
                sections.append(_Section(cur_bucket, body))

    for line in text.splitlines():
        if _is_heading(line):
            flush()
            cur_bucket = _classify(_heading_text(line))
            cur_lines = []
        elif cur_bucket is not None:
            cur_lines.append(line.rstrip())
    flush()
    return sections


def _collect(sections: list[_Section], bucket: str) -> list[str]:
    """同一区分の全セクション本文を結合する (複数見出しが同区分でもまとめる)。"""
    lines: list[str] = []
    for sec in sections:
        if sec.bucket == bucket:
            lines.extend(sec.lines)
    return lines


def _fallback_next(text: str) -> list[str]:
    """見出し分類が無い場合、next キーワードを含む行を「次の一手」として拾う。"""
    hits: list[str] = []
    for line in text.splitlines():
        ls = line.strip()
        if not ls:
            continue
        low = ls.lower()
        if any(key in low for key in _NEXT_KEYS):
            hits.append(ls)
    return hits


def summarize_for_human(text: str) -> str:
    """raw SESSION_SUMMARY.md → 人間向けダイジェスト文字列。

    空サマリは "" を返す (GUI は placeholder を出す)。
    """
    if not text or not text.strip():
        return ""

    sections = _split_sections(text)
    state = _collect(sections, "state")
    done = _collect(sections, "done")
    nxt = _collect(sections, "next")

    # 見出しが全く分類できなかったサマリ: 先頭数行を現在地に、next 行抽出を次の一手に。
    # 現在地からは next 行を除外する (同じ行を 2 区分に重複表示して人を迷わせない)。
    if not (state or done or nxt):
        nxt = _fallback_next(text)
        nxt_set = {ln.strip() for ln in nxt}
        meaningful = [ln.rstrip() for ln in text.splitlines()
                      if ln.strip() and ln.strip() not in nxt_set]
        state = meaningful[:_FALLBACK_HEAD_LINES]

    blocks: list[str] = []
    for label, lines in (("現在地", state), ("直近の成果", done), ("次の一手", nxt)):
        body = _clip(lines) if lines else ["  —"]
        blocks.append(f"【{label}】\n" + "\n".join(body))
    return "\n\n".join(blocks)
