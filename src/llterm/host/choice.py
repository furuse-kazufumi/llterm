# SPDX-License-Identifier: Apache-2.0
"""選択ダイアログの検知層 — agent 出力から「ユーザーに選ばせたい」を決定論的に拾う。

llterm は claude を headless (``claude -p … --resume``, stdin=DEVNULL) でターン駆動するため
**ターン中に対話的選択ができない**。そこで agent (claude) がユーザーに選択を求めたいときは、
自由な番号列挙ではなく専用の規約マーカーで出してもらい、それを llterm が決定論的に検知して
GUI の専用ダイアログへ橋渡しする。回答は既存の task injection 機構で次ターンへ注入する。

検知は 2 系統 (どちらも Qt 非依存の純関数):

1. **規約マーカー (主・決定論的)** — :func:`parse_choices_from_text` /
   :func:`parse_choice_blocks`。行頭限定の希少センチネル
   ``⟦LLTERM_CHOICE …⟧ … ⟦/LLTERM_CHOICE⟧`` を解析する。

   - 誤検知防止: **行頭限定** + コードフェンス (```` ``` ````) 内は無視 +
     git diff の追加行 (``+``) 内は無視。
   - 1 ターンに複数あれば **最後の (= 最新の未応答) ブロック**を採用する。
   - ``multi=true/false`` で single (ラジオ) / multi (チェックボックス) を分ける。
     属性が無ければ安全側の single。
   - question は ``question="…"`` 属性が最優先。無ければ**直前の見出し/非空行**を採る。

2. **AskUserQuestion tool_use イベント (bonus)** —
   :func:`choice_from_ask_user_question_event`。stream-json に
   ``tool_use(name="AskUserQuestion", input.questions[].options/multiSelect)`` が出る環境では
   そこからも同じ :class:`Choice` を作る。出ない環境でも 1 の規約マーカーで動く。

fail-safe 契約: 解析は**例外を外へ出さない**。壊れた/不完全な入力は「選択なし」(None /
空 list) として扱い、空ダイアログを出さない (選択肢ゼロのブロックは無効)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Choice:
    """1 つの選択要求。GUI ダイアログ生成と回答整形の唯一のデータ源。"""

    question: str
    options: list[str] = field(default_factory=list)
    multi: bool = False  # True = 複数選択 (チェックボックス) / False = 単一 (ラジオ)


# 行頭アンカーの希少センチネル (U+27E6/27E7 = 数学用山括弧。通常コードや散文に現れない)。
_OPEN = "⟦LLTERM_CHOICE"
_CLOSE = "⟦/LLTERM_CHOICE⟧"

# 開始行: 行頭から ⟦LLTERM_CHOICE …⟧。属性部 (…) を捕捉する。
_OPEN_RE = re.compile(r"^⟦LLTERM_CHOICE\b(?P<attrs>[^⟧]*)⟧\s*$")
_CLOSE_RE = re.compile(r"^⟦/LLTERM_CHOICE⟧\s*$")

# 属性: key="quoted value" / key='quoted' / key=bareword。
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*("([^"]*)"|'([^']*)'|(\S+))""")

# 選択肢行: 行頭 (空白許容) の "1)" / "1." / "1:" / "- " で始まる行のラベル部。
_OPTION_RE = re.compile(r"^\s*(?:\d+[)\.:]|[-*])\s+(?P<label>.+?)\s*$")


def _parse_attrs(raw: str) -> dict[str, str]:
    """``multi=true question="…"`` 形式の属性列を dict へ。失敗しても落ちない (fail-safe)。"""
    attrs: dict[str, str] = {}
    try:
        for m in _ATTR_RE.finditer(raw):
            key = m.group(1).lower()
            val = m.group(3) if m.group(3) is not None else (
                m.group(4) if m.group(4) is not None else (m.group(5) or ""))
            attrs[key] = val
    except (re.error, AttributeError, TypeError):
        pass
    return attrs


def _as_bool(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes", "on", "multi")


def _is_fence_marker(stripped: str) -> bool:
    """コードフェンス開始/終了行か (``` または ~~~ で始まる)。"""
    return stripped.startswith("```") or stripped.startswith("~~~")


def parse_choices_from_text(text: str) -> list[Choice]:
    """agent ターン出力テキスト中の全 ``⟦LLTERM_CHOICE⟧`` ブロックを順に Choice 化して返す。

    - 行頭限定。コードフェンス内・git diff 追加行内のマーカーは無視 (誤検知防止)。
    - 選択肢が 0 個のブロック、終了センチネルが無いブロックは捨てる (fail-safe)。
    - question は 属性 > 直前の非空行 (見出し) の順で決める。

    いかなる入力でも例外を出さない (壊れていれば空 list)。
    """
    if not isinstance(text, str) or _OPEN not in text:
        return []
    try:
        lines = text.splitlines()
    except (AttributeError, TypeError):
        return []

    choices: list[Choice] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if _is_fence_marker(stripped):
            in_fence = not in_fence
            i += 1
            continue
        # diff の追加行はそのままだとマーカー誤検知の温床 → 行頭 + は対象外。
        if in_fence or raw.startswith("+"):
            i += 1
            continue
        m_open = _OPEN_RE.match(raw)
        if not m_open:
            i += 1
            continue
        # ブロック本体を終了センチネルまで収集 (フェンス内で閉じる異常はブロック無効)。
        attrs = _parse_attrs(m_open.group("attrs"))
        body: list[str] = []
        j = i + 1
        closed = False
        while j < n:
            line_j = lines[j]
            sj = line_j.strip()
            if _is_fence_marker(sj):  # ブロック内でフェンスが始まる = 不正 → 中断
                break
            if _CLOSE_RE.match(line_j):
                closed = True
                break
            body.append(line_j)
            j += 1
        if not closed:
            i += 1  # 終了センチネルが無い → このブロックは捨て、次行から探索継続
            continue
        options = [mm.group("label").strip()
                   for ln in body
                   if (mm := _OPTION_RE.match(ln)) and mm.group("label").strip()]
        if options:  # 選択肢ゼロのブロックは無効 (空ダイアログを出さない)
            question = attrs.get("question", "").strip() or _preceding_heading(lines, i)
            choices.append(Choice(question=question, options=options,
                                  multi=_as_bool(attrs.get("multi", ""))))
        i = j + 1  # 終了センチネルの次行から続行
    return choices


def _preceding_heading(lines: list[str], open_idx: int) -> str:
    """開始マーカー直前の意味ある 1 行を question として返す (見出し記号は剥がす)。"""
    k = open_idx - 1
    while k >= 0:
        s = lines[k].strip()
        if not s:
            k -= 1
            continue
        if s.startswith(_OPEN) or _CLOSE_RE.match(lines[k]):
            return ""  # 直前が別マーカー = 見出しではない
        return s.lstrip("#＃ ").rstrip("：:").strip()
    return ""


def parse_choice_blocks(text: str) -> Choice | None:
    """1 ターン分のテキストから採用すべき 1 件 (= 最後の未応答ブロック) を返す。無ければ None。"""
    choices = parse_choices_from_text(text)
    return choices[-1] if choices else None


# ─── AskUserQuestion tool_use イベント (bonus) ────────────────────


def _option_label(opt: object) -> str:
    """AskUserQuestion の option (str | {"label": …}) からラベル文字列を取り出す。"""
    if isinstance(opt, str):
        return opt.strip()
    if isinstance(opt, dict):
        return str(opt.get("label") or opt.get("value") or "").strip()
    return ""


def choice_from_ask_user_question_event(ev: object) -> Choice | None:
    """stream-json の AskUserQuestion tool_use イベントから Choice を作る (無ければ None)。

    ``ev.message.content[*]`` の最初の ``tool_use(name="AskUserQuestion")`` を採り、その
    ``input.questions[0]`` から question / multiSelect / options を読む。options が空なら無効。
    出ない環境でも規約マーカー (parse_choices_from_text) で動くので、これは bonus 経路。
    """
    if not isinstance(ev, dict):
        return None
    try:
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
                continue
            inp = block.get("input")
            questions = inp.get("questions") if isinstance(inp, dict) else None
            if not isinstance(questions, list) or not questions:
                return None
            q = questions[0]
            if not isinstance(q, dict):
                return None
            raw_opts = q.get("options")
            options = [lbl for o in (raw_opts if isinstance(raw_opts, list) else [])
                       if (lbl := _option_label(o))]
            if not options:
                return None
            return Choice(question=str(q.get("question") or "").strip(),
                          options=options, multi=bool(q.get("multiSelect", False)))
    except (AttributeError, TypeError, KeyError):
        return None
    return None


# ─── 回答整形 (番号 + ラベル両方 = --resume 文脈で確実に伝わる) ─────


def format_choice_reply(choice: Choice, selected_indices: list[int]) -> str:
    """選択 index 列を「選択: 2) public」「選択: 1) A, 3) C」形式の注入文へ整形する。

    番号 (1 始まり) とラベルの**両方**を出すことで、新ターンの --resume 文脈でも
    どれが選ばれたか曖昧さなく伝わる。範囲外 index は無視 (堅牢性)。
    """
    parts: list[str] = []
    for idx in selected_indices:
        if isinstance(idx, int) and 0 <= idx < len(choice.options):
            parts.append(f"{idx + 1}) {choice.options[idx]}")
    return "選択: " + (", ".join(parts) if parts else "(なし)")
