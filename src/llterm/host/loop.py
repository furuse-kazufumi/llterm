# SPDX-License-Identifier: Apache-2.0
"""llterm L2 — セッションホスト / ループ駆動 (spec 馬1.5).

公式ヘッドレス protocol (``claude -p --session-id/--resume --output-format stream-json``) で
Claude Code を自走ループ駆動する。**端末 (PTY/ConPTY/win32-input-mode) を一切通らない**ため、
ccr / lll を苦しめてきた terminal_io 由来の破綻 (Enter 化け / orphan stdin hang) が
構造的に起きない。これが「正しい層」(spec §0 / R7: protocol を再発明せず公式に乗る)。

ユーザーのループ ``ccr → 前回の続き → session 70%以上 → exit準備 → exit → ccr`` をそのまま実装:

1. 新 session-id を採番 (= rotate は常に **fresh context** の新セッション)。
2. resume_prompt を投げる (CLAUDE.md SESSION START / SESSION_SUMMARY / next_plan を読んで自律継続)。
3. context 使用率 < 閾値 のうちは同セッションを ``--resume`` で続行 (= 前回の続き)。
4. 使用率 >= 閾値 (既定 70%) で **exit準備** (handoff の更新を指示) → セッションを畳む。
5. 1 へ戻る (新セッションへ rotate)。

唯一の人間介在点 = **再ログイン (認証切れ)**。spec §5.5 の構造的上限であり、
検知したら fail-closed で停止し人間を待つ。

このモジュールは表示層に依存しない (headless)。GUI (L3) は ``on_event`` コールバックで
進捗を購読し、Qt シグナルへ marshalled して描画する。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from llterm.ctl.ledger import Ledger
from llterm.host.offload_tools import build_offload_hint
from llterm.i18n import t


def _ensure_utf8_stdout() -> None:
    """Windows cp932 コンソールでも日本語/記号を化けさせない (feedback_cli_utf8_stdout_pattern)。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass


DEFAULT_WINDOW_TOKENS = 200_000
DEFAULT_THRESHOLD = 0.70
DEFAULT_MAX_TURNS_PER_SESSION = 50

# レート制限を示す既知シグナル (制御系チャネル = text/stderr/非JSON行のみに適用。
# JSON transcript には "five_hour" 等が常在し誤検知するため走査しない)
_RATE_LIMIT_SIGNALS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "usage limit",
    "5-hour limit",
    "five-hour limit",
    "resets at",
    "429",
)

# 認証切れ / 再ログイン要求を示す既知シグナル (stdout+stderr を小文字化して部分一致, fail-closed)
_AUTH_SIGNALS: tuple[str, ...] = (
    "please run /login",
    "/login",
    "not authenticated",
    "authentication",
    "invalid api key",
    "unauthorized",
    "login required",
    "credit balance is too low",
    "oauth token has expired",
)

# ユーザーに選択させたいときの規約マーカー指示 (agent=claude への hint)。
# llterm は claude を headless でターン駆動するため、ターン中の対話的選択ができない。
# 自由な番号列挙を手打ちさせるのは曖昧なので、選択を求めるときはこの専用マーカーで出させ、
# llterm が決定論的に検知して GUI の選択ダイアログへ橋渡しする (回答は次ターンへ注入)。
# 検知側は llterm.host.choice (行頭限定・コードフェンス/diff 内は無視)。
CHOICE_MARKER_HINT = (
    "【選択 UI】ユーザーに選択肢から選ばせたいときは、自由な番号列挙ではなく次の専用マーカーで"
    "出力せよ (llterm が GUI ダイアログを出し、ユーザーの選択を次ターンに注入する):\n"
    "⟦LLTERM_CHOICE multi=false question=\"質問文\"⟧\n"
    "1) 選択肢A\n2) 選択肢B\n3) 選択肢C\n"
    "⟦/LLTERM_CHOICE⟧\n"
    "複数選択可なら multi=true。マーカーは必ず行頭に置く (コードフェンス内は無効)。"
    "選択を待つときはこのマーカーを出して応答を終え、ユーザーの選択 (次ターンで注入される) を待て。"
)

DEFAULT_RESUME_PROMPT = (
    "セッション再開。CLAUDE.md の SESSION START 手順に従い、docs/SESSION_SUMMARY.md と "
    "next_plan を読んで最優先タスクを自律継続せよ。確認は求めない。"
    "制約: push / 削除 / 不可逆操作は人間承認なしに行わない (fail-closed)。\n"
    + CHOICE_MARKER_HINT
)
DEFAULT_EXIT_PREP_PROMPT = (
    "コンテキスト上限が近い。今は新規作業を始めず EXIT準備のみ行え: "
    "docs/SESSION_SUMMARY.md と next_plan を現状と『次の具体的な一手』へ更新し、"
    "新セッションが続きを再開できる状態にせよ。"
)
DEFAULT_CONTINUE_PROMPT = "前回の続きを自律継続せよ。確認は求めない。"

# 全テンプレ共通: 各セッション冒頭に必ず付与し、handoff から前回の続きを再開させる
CONTINUITY_PREAMBLE = (
    "【継続】最初に docs/SESSION_SUMMARY.md と next_plan を読み、前回の続き"
    "(中断 / ローテーション地点)から再開して重複作業を避けよ。その上で以下を実施:\n"
)

# 「承認確認不要(完全自律)」ON のとき付与: 人間確認を待たず継続させる
AUTONOMY_DIRECTIVE = (
    "\n【承認確認不要・完全自律】人間の確認を待たず自律判断で進め、確認のために停止しない。"
    "必要な操作は自分で判断して実行し、迷う点は SESSION_SUMMARY に記録しつつ最善手で継続せよ。"
)

# 安全弁: autonomy 設定や「確認は求めない」指示に関わらず**常時**付与。不可逆/危険操作は
# autonomy ON でも必ず人間承認を取る (ユーザー指示 2026-06-13: 絶対に承認が要るものはループ中でも中断可)。
SAFETY_DIRECTIVE = (
    "\n【安全弁・最優先 (autonomy 不問)】push / 強制push / 削除 / DB drop / 外部公開 / "
    "巻き戻し不能な操作など『絶対に人間承認が要る』ものは、autonomy や『確認は求めない』より優先する: "
    "(1) まず進捗サマリ docs/next_plan.md を最新化し、(2) 実行前に ⟦LLTERM_CHOICE⟧ マーカー(行頭)で"
    "人間に確認して応答を終え回答を待つ。(3) 回答受領後は決定内容を1段落で要約し、それを "
    "docs/next_plan.md(作業計画)に追記してから実行に移る(誤解防止 + タスク管理)。"
    "回答が来たら速やかに通常のループ作業へ戻ること。"
)

# autonomy OFF (監督モード) のとき付与: タスク注入時など、AI が確認事項を出すのが自然。
SUPERVISED_DIRECTIVE = (
    "\n【監督モード】不明点・意図確認・重要判断は遠慮なく ⟦LLTERM_CHOICE⟧ マーカーで人間に"
    "尋ねてよい(『確認は求めない』より優先)。確認待ちのときはマーカーを出して応答を終え、回答を待て。"
)

DEFAULT_RAD_HINT = (
    "【RAD 研究接地】新規の設計・実装・調査に着手する前に、まず RAD コーパス "
    "(D:/docs/<分野>_corpus_v2/ および D:/docs/hacker_corpus_v2/) を grep して "
    "既存手法・先行研究・差別化軸を確認せよ(車輪の再発明を防ぐ)。該当が無ければ通常どおり進めてよい。"
)


@dataclass(frozen=True)
class TurnResult:
    """1 ターン (1 回の ``claude -p`` 呼び出し) の結果。"""

    session_id: str
    input_tokens: int
    output_tokens: int
    context_tokens: int  # 次ターンに乗る文脈サイズの目安 (input + cache 系) = 使用率の分子
    cost_usd: float
    text: str
    is_error: bool
    error_kind: str  # "" | "auth" | "rate_limited" | "unavailable" | "other" | "cancelled" | "interrupted"
    num_turns: int
    raw_exit: int
    context_window: int = 0  # result の modelUsage.contextWindow (実窓サイズ。0=不明→設定値を使う)
    rate_limit_status: str = ""  # rate_limit_event の status (allowed / 制限種別)
    rate_limit_resets_at: int = 0  # rate_limit_event の resetsAt (epoch秒。自動再開の待機目標)


def parse_stream_json(stdout: str, *, exit_code: int, stderr: str = "") -> TurnResult:
    """``claude --output-format stream-json`` (JSONL) を 1 ターン結果へ defensively パースする。

    末尾の ``type=="result"`` イベントを主情報源にし、無ければ ``system`` から session_id を拾う。
    形式が将来変わっても落ちないよう各フィールドは安全に取得する (fail-safe)。
    認証切れは ``error_kind=="auth"`` として上位へ伝え、ループを fail-closed で止めさせる。
    """
    session_id = ""
    usage: dict = {}
    cost = 0.0
    text = ""
    is_error = False
    num_turns = 0
    result_seen = False
    context_window = 0
    last_ctx_usage: dict = {}  # 最後のメイン assistant の message.usage = 瞬間コンテキスト占有
    rl_status = ""
    rl_resets = 0
    plain_lines: list[str] = []  # JSON でない行 = claude の診断/エラー出力 (auth 判定に使う)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            plain_lines.append(line)
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("session_id"):
            session_id = str(ev["session_id"])
        elif etype == "assistant" and not ev.get("parent_tool_use_id"):
            # メイン (非サブエージェント) の各 assistant の usage = その時点の窓占有量。
            # サブエージェントは別コンテキストなので除外 (メイン窓の占有に含めない)。
            msg = ev.get("message")
            mu = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(mu, dict) and mu:
                last_ctx_usage = mu
        elif etype == "rate_limit_event":
            info = ev.get("rate_limit_info")
            if isinstance(info, dict):
                rl_status = str(info.get("status") or rl_status)
                rl_resets = _as_int(info.get("resetsAt")) or rl_resets
        elif etype == "result":
            result_seen = True
            session_id = str(ev.get("session_id", session_id))
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            cost = _as_float(ev.get("total_cost_usd"))
            num_turns = _as_int(ev.get("num_turns"))
            subtype = str(ev.get("subtype", ""))
            is_error = bool(ev.get("is_error", False)) or subtype.startswith("error")
            if isinstance(ev.get("result"), str):
                text = ev["result"]
            mu = ev.get("modelUsage")
            if isinstance(mu, dict):  # 実窓サイズ (例: fable-5 は 1,000,000) — used_pct の分母に使う
                for v in mu.values():
                    if isinstance(v, dict):
                        context_window = max(context_window, _as_int(v.get("contextWindow")))

    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    # コンテキスト占有 (rotate 判定の分子) は最後のメイン assistant の usage を使う。
    # result.usage はターン内全 API 往復の累計で、キャッシュ再読込が往復ごとに重複加算され
    # 実際の窓占有を大幅に過大評価する (実測: 単純ターンで約 2x、ツール多用で窓サイズ超過 156%)。
    # last_ctx_usage が取れない場合 (error / assistant 無し) のみ result.usage にフォールバック。
    occ = last_ctx_usage or usage
    context_tokens = (
        _as_int(occ.get("input_tokens"))
        + _as_int(occ.get("cache_read_input_tokens"))
        + _as_int(occ.get("cache_creation_input_tokens"))
    )

    error_kind = ""
    if exit_code != 0 or is_error or not result_seen:
        is_error = True
        # auth/rate-limit 判定は制御系チャネル (stderr / 非 JSON 診断行 / result 本文) に限定する。
        # stdout の JSON transcript 全文を検索すると、tool_result 中の語彙や常在する
        # rate_limit_event の "five_hour" 等で誤分類され、不要に停止してしまう (レビュー所見)。
        blob = "\n".join((text, stderr, *plain_lines)).lower()
        rl_blocking = bool(rl_status) and rl_status not in ("allowed", "allowed_warning")
        if any(sig in blob for sig in _AUTH_SIGNALS):
            error_kind = "auth"
        elif rl_blocking or any(sig in blob for sig in _RATE_LIMIT_SIGNALS):
            error_kind = "rate_limited"
        else:
            error_kind = "other"

    return TurnResult(
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_tokens=context_tokens,
        cost_usd=cost,
        text=text,
        is_error=is_error,
        error_kind=error_kind,
        num_turns=num_turns,
        raw_exit=exit_code,
        context_window=context_window,
        rate_limit_status=rl_status,
        rate_limit_resets_at=rl_resets,
    )


def _as_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _short(s: str, n: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_TOOL_DETAIL_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")


def _tool_use_detail(inp: object) -> str:
    """tool_use の input から人間が読んで分かる代表値を 1 行で取り出す。"""
    if not isinstance(inp, dict):
        return ""
    for key in _TOOL_DETAIL_KEYS:
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return _short(v.strip().splitlines()[0])
    try:
        return _short(json.dumps(inp, ensure_ascii=False))
    except (TypeError, ValueError):
        return ""


def _tool_result_preview(block: dict) -> str:
    """tool_result の content (str | block list) から先頭の意味のある 1 行を取り出す。

    数 MB 級の tool_result が 1 イベントで来るため、全文 join/splitlines はせず
    各パーツ先頭 4KB だけ走査する (stdout reader 上で O(サイズ) コピーをしない)。
    """
    content = block.get("content")
    if isinstance(content, str):
        parts: list[str] = [content]
    elif isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    else:
        parts = []
    for part in parts:
        for ln in part[:4096].splitlines():
            if ln.strip():
                return _short(ln)
    return ""


def summarize_stream_event(ev: object) -> list[dict]:
    """stream-json の 1 イベントを GUI 表示用の軽量 dict 列へ要約する (表示不要なら空 list)。

    実 claude 2.1.x の実出力で確認済のフォーマット (2026-06-12 probe):
    - ``system/init``: model / session_id (``hook_started`` 等の system は表示しない)
    - ``assistant``: message.content の ``text`` / ``thinking`` / ``tool_use`` ブロック
      (``parent_tool_use_id`` 非 null = Task サブエージェント由来 → ``subagent: True`` を付与)
    - ``user``: message.content の ``tool_result`` ブロック
    - ``rate_limit_event``: レート制限 status / resetsAt (サブスク自走の主制約 — 黙殺しない)
    - ``result``: ターン完了 (所要時間)。詳細メトリクスは TurnResult 側が正で持つ。

    これが「応答がリアルタイムに表示されない」問題の中核修正 — 従来はターン完了後の
    最終 result テキストしか GUI に渡らず、自律ターン (数分〜数十分) の間 GUI が無表示だった。
    """
    if not isinstance(ev, dict):
        return []
    etype = ev.get("type")
    if etype == "system":
        if ev.get("subtype") == "init":
            return [{
                "kind": "init",
                "model": str(ev.get("model", "")),
                "session_id": str(ev.get("session_id", "")),
            }]
        return []
    if etype in ("assistant", "user"):
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return []
        # Task サブエージェント由来のイベントは parent_tool_use_id 非 null で届く。
        # メイン応答と無区別に表示すると並列サブエージェント運用で監視が成立しないため区別する。
        subagent = bool(ev.get("parent_tool_use_id"))
        items: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            item: dict | None = None
            if etype == "assistant" and btype == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    item = {"kind": "text", "text": text}
            elif etype == "assistant" and btype == "thinking":
                item = {"kind": "thinking",
                        "preview": _short(str(block.get("thinking") or ""), 80)}
            elif etype == "assistant" and btype == "tool_use":
                # AskUserQuestion は選択 UI へ橋渡しする (bonus 経路)。出ない環境でも
                # 規約マーカー (choice.parse_choices_from_text) が主経路なので問題ない。
                if block.get("name") == "AskUserQuestion" and not subagent:
                    from llterm.host.choice import choice_from_ask_user_question_event
                    ch = choice_from_ask_user_question_event(
                        {"message": {"content": [block]}})
                    if ch is not None:
                        items.append({"kind": "choice", "question": ch.question,
                                      "multi": ch.multi, "options": list(ch.options)})
                        continue
                item = {"kind": "tool_use", "name": str(block.get("name") or "?"),
                        "detail": _tool_use_detail(block.get("input"))}
            elif etype == "user" and btype == "tool_result":
                item = {"kind": "tool_result",
                        "is_error": bool(block.get("is_error", False)),
                        "preview": _tool_result_preview(block)}
            if item is not None:
                if subagent:
                    item["subagent"] = True
                items.append(item)
        return items
    if etype == "rate_limit_event":
        # サブスク自走の主制約 = レート制限。status / リセット時刻を GUI に伝える (黙殺しない)。
        info = ev.get("rate_limit_info")
        if isinstance(info, dict):
            return [{"kind": "rate_limit", "status": str(info.get("status") or ""),
                     "resets_at": _as_int(info.get("resetsAt")),
                     "rate_limit_type": str(info.get("rateLimitType") or "")}]
        return []
    if etype == "result":
        return [{"kind": "result", "duration_ms": _as_int(ev.get("duration_ms")),
                 "is_error": bool(ev.get("is_error", False))}]
    return []


_SUBSCRIPTION_STRIP_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# claude --effort が受け付ける値 (claude 2.1.174 で実測確認)。"" = フラグを付けず claude 既定。
# 注: raptor 独自の "ultracode" は vanilla claude には無い (max が最上位)。
EFFORT_LEVELS: tuple[str, ...] = ("", "low", "medium", "high", "xhigh", "max")

# GUI モデル選択コンボに出す候補。"" = --model を付けず claude 側の保存既定に委ねる。
# alias (opus/sonnet/haiku) は起動時に各世代の最新へ解決される。フル ID は世代を固定する。
# 妥当性は claude 本体が検証するため、ここに無い値も --model / extra_args で素通しできる
# (_build_args は非空なら無条件に付与する。世代更新でこの表を直さなくても動く設計)。
MODEL_CHOICES: tuple[str, ...] = (
    "",
    "opus",
    "sonnet",
    "haiku",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)
# llterm 既定モデル (2026-06-13 ユーザー方針: Opus 4.8)。token 消費が重いときは
# GUI のコンボ / --model で sonnet・haiku へ即切替できる (alias は最新世代へ解決)。
DEFAULT_MODEL = "claude-opus-4-8"

# gui-scripts (pythonw) 親が console 子 (claude.exe / taskkill) を spawn すると、stdio を全
# redirect していても console window が毎ターン可視表示される (実機確認済) — これで抑止する。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _subscription_env() -> dict[str, str]:
    """claude.ai サブスク認証 (OAuth) を使わせるため API キー系 env を外して返す。

    ANTHROPIC_API_KEY 等が残っていると claude が従量課金 API を優先しうる。これを外すと
    OAuth (claude.ai サブスク) にフォールバックする = ccr が課金回避に使うのと同じ手法。
    結果、連続自走しても **新たな従量課金は発生しない** (Max 定額の範囲。制約はレート制限)。
    """
    env = dict(os.environ)
    for key in _SUBSCRIPTION_STRIP_VARS:
        env.pop(key, None)
    return env


def _native_claude_dirs() -> tuple[Path, ...]:
    """native claude installer の既定配置先 (claude 2.x は ``~/.local/bin``)。

    GUI が古い PATH を抱えたまま native claude へ移行した直後など、claude が PATH に
    乗っていなくてもここを直接探して見つけられるようにする (PATH 陳腐化に耐える)。
    ``Path.home()`` は PATH に依存しないので、長時間稼働中の GUI でも解決できる。
    テストで差し替え可能 (monkeypatch する)。
    """
    return (Path.home() / ".local" / "bin",)


class TurnRunner(Protocol):
    """1 ターンを実行して結果を返す抽象 (テスト/GUI は mock や仮想 claude を注入する)。"""

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult: ...

    def cancel(self) -> None:
        """実行中ターンを安全に中断する (Stop / ウィンドウ終了用)。"""
        ...


@dataclass
class ClaudeRunner:
    """実 claude を headless stream-json で 1 ターン回す。端末を使わない (PTY なし)。

    - ``resume=False`` → ``--session-id <uuid>`` で新規セッション作成。
    - ``resume=True``  → ``--resume <uuid>`` で同セッション継続 (in-place・同一 ID。2026-06-12 実走確認)。
    - ``stdin=DEVNULL`` — 自走ループの子は stdin を待たない (orphan-reader hang を構造的に排除)。
    - list-based args (shell 不使用)。``--verbose`` は ``-p`` + stream-json に必須。
    - ``on_stream`` — stdout を**行単位でリアルタイム購読**し、要約イベント
      (:func:`summarize_stream_event`) を逐次通知する。ターン完了 (数分〜数十分) を待たずに
      GUI へ応答が流れる。stderr は別スレッドで排出し pipe デッドロックを防ぐ。
    """

    exe: str = "claude"
    timeout: float = 7200.0  # 自律 1 ターンは長い (旧 1800s では正当な作業を途中 kill し得た)
    skip_permissions: bool = True
    use_subscription: bool = True  # True: API キー env を外し claude.ai サブスク認証で回す (課金回避)
    effort: str = ""  # "" = claude 既定 / それ以外は --effort <level> を付与 (EFFORT_LEVELS)
    model: str = ""  # "" = claude 保存既定 / 非空 (alias またはフル ID) は --model <model> を付与
    extra_args: Sequence[str] = ()
    on_stream: Callable[[dict], None] | None = None  # 要約イベントのリアルタイム購読 (GUI 用)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)
    _interrupted: bool = field(default=False, repr=False, compare=False)  # 緊急注入の一発中断

    def _native_claude_path(self) -> str | None:
        """PATH に native exe が無いとき、既知の install 先 (``~/.local/bin``) を直接探す。

        無ければ None。``self.exe`` がパス区切りを含む (= ユーザーが明示パスを指定) 場合は
        尊重して探索しない。GUI が古い PATH を抱えたまま native claude へ移行した直後でも
        この経路で claude を見つけられる (PATH 陳腐化に耐える)。
        """
        if any(sep and sep in self.exe for sep in (os.sep, os.altsep)):
            return None  # 明示パス指定は尊重し探索しない
        names = (self.exe + ".exe", self.exe) if sys.platform == "win32" else (self.exe,)
        for d in _native_claude_dirs():
            for name in names:
                cand = d / name
                if cand.is_file():
                    return str(cand)
        return None

    def _resolved_exe(self) -> str:
        """claude の絶対パスを解決する。Windows の CreateProcess は拡張子なし名に .exe しか
        自動付加しないため明示解決し、PATH に無くても native install 先を直接探す
        (GUI が古い PATH を抱えたまま native claude へ移行した直後でも起動できる)。"""
        found = shutil.which(self.exe)
        if found and found.lower().endswith(".exe"):
            return found
        native = self._native_claude_path()
        if native is not None:  # PATH 陳腐化時の保険
            return native
        return self.exe

    def _exe_error(self) -> str:
        """claude を安全に起動できない環境を fail-closed で検出し、原因を明示する。

        - npm shim (.cmd/.bat/.ps1) しか無い: shim を cmd.exe 経由で実行すると prompt
          (任意文字列) が shell 解釈される注入リスクがあるため非対応 (native exe を求める)。
        - claude が PATH にも native install 先にも無い: 原因不明の exit 127 (空テキストの
          ``err=other``) でなく『見つからない』ことを明示し、GUI 再起動 / 導入を促す。
        native exe を別所に見つけられる場合はエラーにしない (_resolved_exe がそれを使う)。
        """
        found = shutil.which(self.exe)
        if found is not None and found.lower().endswith((".cmd", ".bat", ".ps1")):
            if self._native_claude_path() is None:  # shim のみ (native 不在) → 非対応
                return t("runner.claude.npm_shim", path=found)
        elif found is None and self._native_claude_path() is None:
            return t("runner.claude.not_found")
        return ""

    @staticmethod
    def _cli_session_id(session_id: str, *, resume: bool) -> str:
        """claude --session-id / --resume が要求する UUID へ正規化する。

        claude は (a) 厳格な UUID を要求し、(b) ``--session-id`` で**既存 id の再利用**も
        ``Session ID ... is already in use`` (exit 1) で拒否する。OrchestraRunner が渡す派生 id
        ('<uuid>-review0' / '-aggregate' / '-signoff' / '-factcheck' 等) は UUID 不正なうえ、
        同一 llterm セッション内で毎ターン / exit準備のたびに**同じ派生 id を使い回す**ため、
        決定論的に写像すると 2 回目以降が衝突して全失敗する (= 同一セッションで 2 回目以降の
        レビュー奏者 / 責任者 / sign-off が落ちる原因)。レビュー系は resume=False のステートレス
        一発実行なので、**毎回フレッシュな UUID** を作って衝突を避ける。resume=True の非 UUID は
        決定論的に写像 (実運用では発生しない安全側)。既に有効 UUID (主ループの sid) はそのまま
        返す (作成 ``--session-id`` → 継続 ``--resume`` が整合する)。
        """
        try:
            uuid.UUID(str(session_id))
            return str(session_id)
        except (ValueError, TypeError, AttributeError):
            if resume:
                return str(uuid.uuid5(uuid.NAMESPACE_URL, str(session_id)))
            return str(uuid.uuid4())

    def _build_args(self, *, prompt: str, session_id: str, resume: bool) -> list[str]:
        """claude の引数列を組む (テストはここを差し替えて偽の子プロセスを注入する)。"""
        sid = self._cli_session_id(session_id, resume=resume)
        session_flag = ["--resume", sid] if resume else ["--session-id", sid]
        args = [self._resolved_exe(), "-p", prompt,
                "--output-format", "stream-json", "--verbose", *session_flag]
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        if self.effort and self.effort in EFFORT_LEVELS:  # 不正値は付けない (claude 既定に委ねる)
            args.extend(["--effort", self.effort])
        if self.model:  # 非空ならそのまま渡す (alias/フル ID)。妥当性は claude 側が検証する
            args.extend(["--model", self.model])
        args.extend(self.extra_args)
        return args

    def _notify_stream(self, line: str) -> None:
        """stdout 1 行を要約して購読者へ流す。購読者の例外でターンを殺さない (fail-safe)。"""
        if self.on_stream is None:
            return
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        for item in summarize_stream_event(ev):
            try:
                self.on_stream(item)
            except Exception:  # noqa: BLE001 — 表示側の失敗を loop に波及させない
                pass

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        exe_err = self._exe_error()
        if exe_err:
            # claude を起動できない (shim のみ / 未導入) = このプロバイダは使用不能。
            # err=other だと loop が 3 回叩いて silent circuit_open するため "unavailable" にし、
            # loop に別プロバイダへの即フォールバック (または明示停止) をさせる。
            return TurnResult(session_id, 0, 0, 0, 0.0, exe_err, True, "unavailable", 0, 127)
        args = self._build_args(prompt=prompt, session_id=session_id, resume=resume)
        # cancel は恒久 (リセットしない): Stop 後〜次ターン起動前に届いた cancel を消失させない。
        # GUI は Start ごとに新しい runner を作るので、次の走行に持ち越されることはない。
        with self._lock:
            if self._cancelled:
                return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_NO_WINDOW,
                env=_subscription_env() if self.use_subscription else None,
            )
        except FileNotFoundError:
            # PATH にも native install 先にも claude が無い = 使用不能 → fallback/明示停止に委ねる。
            return TurnResult(session_id, 0, 0, 0, 0.0, t("runner.claude.not_found"),
                              True, "unavailable", 0, 127)
        with self._lock:
            self._proc = proc
            kill_now = self._cancelled  # Popen 中 (=_proc 未設定) に cancel が来た窓を閉じる
        if kill_now:
            self._kill(proc)

        timed_out = threading.Event()

        def _on_timeout() -> None:
            if proc.poll() is not None:
                return  # 正常完了との同時発火 — 完了済みの結果をタイムアウト扱いにしない
            timed_out.set()
            self._kill(proc)

        watchdog = threading.Timer(self.timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        err_buf: list[str] = []

        def _drain_stderr() -> None:  # stderr を排出しないと子が write でブロックし得る
            try:
                assert proc.stderr is not None
                for eline in proc.stderr:
                    err_buf.append(eline)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        out_lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:  # 行単位リアルタイム読み — communicate() の全ブロックを廃止
                out_lines.append(line)
                self._notify_stream(line)
            try:
                proc.wait(timeout=30)  # stdout を閉じても居座る異常な子に timeout まで付き合わない
            except subprocess.TimeoutExpired:
                self._kill(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError):
            self._kill(proc)
        finally:
            watchdog.cancel()
            stderr_thread.join(timeout=5)
            with self._lock:
                self._proc = None

        with self._lock:
            cancelled = self._cancelled
            interrupted = self._interrupted
            self._interrupted = False  # 一発: 次の run_turn は通常起動できる
        if cancelled:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, proc.returncode or -1)
        if interrupted:  # 緊急注入による中断 = 停止ではない。loop が注入を次ターンで消費する
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "interrupted", 0, proc.returncode or -1)
        if timed_out.is_set():
            # 空テキスト err=other で silent circuit_open しないよう理由を明示 (gem-critic 2026-06-21)
            return TurnResult(session_id, 0, 0, 0, 0.0, t("runner.claude.timeout"), True, "other", 0, -1)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return parse_stream_json("".join(out_lines), exit_code=exit_code, stderr="".join(err_buf))

    def cancel(self) -> None:
        """claude ターンをプロセスツリーごと安全に kill する (Stop / 終了用)。

        恒久的: 以後この runner の run_turn は新しい claude を起動せず cancelled を返す
        (Stop 直後のターン境界レースで新プロセスが生まれるのを構造的に防ぐ)。
        """
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def interrupt(self) -> None:
        """現ターンだけを kill する (恒久 cancel と違い、次の run_turn は新規に起動できる)。

        緊急注入「今やっていることを止めて注入タスクを即実行」用の一発中断。run_turn は
        error_kind="interrupted" を返し、loop はループを止めず注入を次ターンで消費する。
        """
        with self._lock:
            self._interrupted = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":  # 子(node 等)も含めツリーで止める
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW, timeout=10)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


@dataclass(frozen=True)
class Outcome:
    stop_reason: str  # "max_sessions"|"max_cost"|"auth_required"|"circuit_open"|"provider_unavailable"|"stopped"
    sessions: int
    turns: int
    total_cost_usd: float
    detail: str = ""


@dataclass
class SessionLoop:
    """L2 ループ駆動本体。閾値到達でセッションを rotate する自走エンジン (表示層に非依存)。"""

    runner: TurnRunner
    workdir: Path
    ledger: Ledger
    fallback_runners: Sequence[TurnRunner] = ()  # レート制限時に切り替えるプロバイダ (優先順)
    resume_prompt: str = DEFAULT_RESUME_PROMPT
    exit_prep_prompt: str = DEFAULT_EXIT_PREP_PROMPT
    continue_prompt: str = DEFAULT_CONTINUE_PROMPT
    rad_hint: str = ""  # 非空なら作業 prompt に RAD 研究接地ヒントを付ける (--rad)
    offload_hint: str = ""  # 非空なら作業 prompt に計算オフロード指令を付ける (offload_tools)
    continuity: bool = True  # 各セッション冒頭で handoff を読み「前回の続き」から再開する
    autonomy: bool = False  # True: 承認確認不要(人間確認を待たず継続)。既定 False=安全側
    autonomy_fn: Callable[[], bool] | None = None  # 非None: 毎ターン autonomy を動的取得 (GUI トグル即反映)
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    threshold: float = DEFAULT_THRESHOLD
    max_sessions: int | None = None
    max_total_cost_usd: float | None = None
    max_consecutive_errors: int = 3
    max_turns_per_session: int = DEFAULT_MAX_TURNS_PER_SESSION
    handoff_on_stop: bool = True  # 停止要求時、作業中なら exit準備 (handoff) を 1 回回してから止める
    auto_resume_on_rate_limit: bool = True  # レート制限時 resetsAt まで待って自動再開する
    max_rate_limit_wait_s: float = 6 * 3600.0  # 1 回の待機上限 (これを超えたら打ち切り再試行)
    rate_limit_fallback_wait_s: float = 300.0  # resetsAt 不明時の固定待ち (5 分)
    now_fn: Callable[[], float] = time.time  # 待機の時計 (テストで差し替え可能)
    sleep_fn: Callable[[float], None] = time.sleep  # 待機の sleep (テストで差し替え可能)
    on_event: Callable[[str, dict], None] | None = None
    should_stop: Callable[[], bool] | None = None  # GUI の Stop ボタン等 (協調停止)
    next_prompt: Callable[[], str | None] | None = None  # GUI のタスク注入 (継続ターンで一度だけ優先)
    _blocked_until: dict[int, float] = field(default_factory=dict, repr=False, compare=False)

    def _emit(self, kind: str, **data: object) -> None:
        """観測者 (GUI 等) へ進捗通知。observer の例外で自走を殺さない (fail-safe)。"""
        if self.on_event is None:
            return
        try:
            self.on_event(kind, dict(data))
        except Exception:  # noqa: BLE001 — 表示側の失敗を loop に波及させない
            pass

    def _stop_requested(self) -> bool:
        if self.should_stop is None:
            return False
        try:
            return bool(self.should_stop())
        except Exception:  # noqa: BLE001
            return False

    def _augment(self, prompt: str) -> str:
        """作業 prompt に各種ヒント (RAD 研究接地 / 計算オフロード) を付ける。

        設定済みのものだけ末尾に連結する (exit準備プロンプトには付けない)。
        """
        hints = [h for h in (self.rad_hint, self.offload_hint) if h]
        if not hints:
            return prompt
        return prompt + "".join(f"\n\n{h}" for h in hints)

    def _autonomy_on(self) -> bool:
        """承認確認不要 (完全自律) の現在値。autonomy_fn があれば毎ターン動的取得する
        (GUI のチェックボックスを走行中にトグルしても次ターンから効く)。"""
        if self.autonomy_fn is not None:
            try:
                return bool(self.autonomy_fn())
            except Exception:  # noqa: BLE001
                return self.autonomy
        return self.autonomy

    def _apply_directives(self, prompt: str) -> str:
        """毎ターン (opener + 継続) に適用する指令を付ける。

        - **安全弁 (常時)**: 不可逆/危険操作は autonomy 不問で人間承認 (進捗更新→⟦LLTERM_CHOICE⟧→要約)。
        - **autonomy ON**: 通常確認は待たず自律継続。
        - **autonomy OFF (監督モード)**: 不明点・確認事項を遠慮なく人間に尋ねてよい。
        タスク注入時に GUI が autonomy を OFF にすると次ターンから監督モードへ切り替わり、
        確認回答後に GUI が autonomy を ON へ戻すと通常ループへ復帰する。
        """
        prompt = prompt + SAFETY_DIRECTIVE
        return prompt + (AUTONOMY_DIRECTIVE if self._autonomy_on() else SUPERVISED_DIRECTIVE)

    def _take_injection(self) -> str | None:
        """注入キューから 1 件取り出す (無ければ None)。next_prompt の例外は握り潰す。

        opener と継続の両方から呼ぶ共通入口。これにより注入は「次のターン境界」で必ず
        消費され、rotate を挟んでも飢餓しない (ユーザー指摘 2026-06-13: 注入は高優先)。
        """
        if self.next_prompt is None:
            return None
        try:
            return self.next_prompt() or None
        except Exception:  # noqa: BLE001
            return None

    def _continue_prompt(self) -> tuple[str, bool]:
        """継続ターンの prompt と「注入タスクか」フラグ。GUI inject があれば一度だけ優先する。"""
        got = self._take_injection()
        base = got if got else self.continue_prompt
        return self._apply_directives(self._augment(base)), got is not None

    def used_pct(self, res: TurnResult) -> float:
        # result イベントの実窓サイズ (modelUsage.contextWindow) があればそちらを分母にする。
        # 設定既定 200K のまま 1M 窓モデル (fable-5 等) で回すと使用率を 5 倍過大評価し
        # 早すぎる rotate を繰り返すため (2026-06-12 レビュー所見)。
        denom = res.context_window or self.window_tokens
        if denom <= 0:
            return 0.0
        # 占有率は物理的に窓 (ハード上限) を超えない。> 100% は過大計上アーティファクトなので
        # [0, 1] にクランプし、累積 usage が紛れても rotate 判定 (used >= threshold) を壊さない
        # (ユーザー指摘 2026-06-13: orchestra で ctx 2549% → 毎ターン rotate していた)。
        return min(1.0, max(0.0, res.context_tokens / denom))

    def _new_session_id(self) -> str:
        # rotation = 新 session-id = fresh context。UUID 衝突は事実上ゼロ。
        return str(uuid.uuid4())

    # ─── provider chain (レート制限時のモデル使い分け) ───────────────

    @property
    def _chain(self) -> list[TurnRunner]:
        """primary + fallback の優先順リスト。index 0 = primary (Claude)。"""
        return [self.runner, *self.fallback_runners]

    @staticmethod
    def provider_name(runner: TurnRunner) -> str:
        """表示用プロバイダ名 (claude / codex / クラス名)。"""
        cls = type(runner).__name__
        return {"ClaudeRunner": "claude", "CodexRunner": "codex"}.get(cls, cls)

    def _select_available(self, now: float, *, exclude: int | None = None) -> int | None:
        """利用可能 (ブロック解除済) な最優先プロバイダの index。無ければ None。"""
        for idx in range(len(self._chain)):
            if idx == exclude:
                continue
            if self._blocked_until.get(idx, 0.0) <= now:
                return idx
        return None

    def _earliest_unblock(self) -> float:
        """全プロバイダがブロック中のとき、最も早い解除時刻。無ければ 0。"""
        times = [t for t in self._blocked_until.values() if t > 0.0]
        return min(times) if times else 0.0

    def _wait_until(self, resets_at: float) -> bool:
        """resetsAt (epoch秒) まで中断可能に待つ。Stop されたら False (= 自走を止める)。

        resetsAt 不明/過去なら固定待ち。max_rate_limit_wait_s で 1 回の待機を上限する。
        sleep は短い刻みで行い should_stop を頻繁に確認する (待機中も Stop が効く)。
        """
        now = self.now_fn()
        if resets_at and resets_at > now:
            target = float(resets_at) + 5.0  # リセット直後の取りこぼし回避に少し余裕
        else:
            target = now + self.rate_limit_fallback_wait_s
        target = min(target, now + self.max_rate_limit_wait_s)  # 1 回の待機上限
        while self.now_fn() < target:
            if self._stop_requested():
                return False
            remaining = target - self.now_fn()
            self.sleep_fn(min(2.0, max(0.0, remaining)))
        return True

    def _handoff_run_turn(self, runner: TurnRunner, *, prompt: str, sid: str,
                          resume: bool) -> TurnResult:
        """handoff / exit準備ターンを実行する。

        orchestra のようなフルレビュー型 runner が ``run_turn_unreviewed`` を持つ場合はそれを使い、
        記録目的のターンに 3-AI のフルレビュー (実装→3レビュー→集約→修正→sign-off) を掛けない
        (ユーザー指摘 2026-06-13: レビューにレビューを重ねている / 時間がかかりすぎ)。
        持たない通常 runner は run_turn にフォールバックする (後方互換)。
        """
        fn = getattr(runner, "run_turn_unreviewed", None)
        if callable(fn):
            return fn(prompt=prompt, session_id=sid, resume=resume, cwd=self.workdir)
        return runner.run_turn(prompt=prompt, session_id=sid, resume=resume, cwd=self.workdir)

    def _handoff(self, runner: TurnRunner, sid: str) -> float:
        """停止要求時の作業記録 (exit準備 = SESSION_SUMMARY 更新) を 1 ターン回す。

        戻り値 = 追加コスト。失敗は握り潰す (fail-safe)。force stop (runner.cancel 済) の場合は
        run_turn が即 cancelled を返すため事実上 no-op (新プロセスは起動しない)。
        """
        self._emit("handoff", session_id=sid)
        try:
            r = self._handoff_run_turn(runner, prompt=self.exit_prep_prompt, sid=sid, resume=True)
            self.ledger.append(event="exit_prep", cmd_id=sid, action="shutdown",
                               detail="handoff on stop")
            return r.cost_usd
        except Exception:  # noqa: BLE001
            return 0.0

    def run(self) -> Outcome:
        sessions = 0
        turns = 0
        total_cost = 0.0
        consec_err = 0
        prev_idx = -1

        while self.max_sessions is None or sessions < self.max_sessions:
            if self._stop_requested():
                return self._finish("stopped", sessions, turns, total_cost, "stop requested")

            # セッション開始時に利用可能な最優先プロバイダを選ぶ (primary 復活を優先)。
            # 全プロバイダがレート制限中なら最も早い解除まで待つ (中断可能)。
            active_idx = self._select_available(self.now_fn())
            if active_idx is None:
                if not self._wait_until(self._earliest_unblock()):
                    return self._finish("stopped", sessions, turns, total_cost,
                                        "stop during rate-limit wait")
                active_idx = self._select_available(self.now_fn()) or 0
            active = self._chain[active_idx]
            if prev_idx >= 0 and active_idx != prev_idx:
                self.ledger.append(event="provider_switch", cmd_id="-", action="rotate",
                                   detail=f"{self.provider_name(self._chain[prev_idx])} "
                                          f"→ {self.provider_name(active)}")
                self._emit("provider_switch", provider=self.provider_name(active), index=active_idx)
            prev_idx = active_idx

            sid = self._new_session_id()
            self.ledger.append(
                event="session_start", cmd_id=sid, action="rotate",
                detail=f"session#{sessions + 1}",
            )
            self._emit("session_start", session_id=sid, session_index=sessions + 1)
            opener = self.resume_prompt
            if self.continuity:
                opener = CONTINUITY_PREAMBLE + opener  # 全テンプレで前回の続きから再開
            # 注入タスクは最優先: 新セッション opener (rotate 直後を含む) でも先に消費する。
            # 従来は継続ターンの _continue_prompt でしか消費せず、ctx 超過で毎ターン rotate する
            # orchestra では _continue_prompt に到達せず注入が永久に飲み込まれた (= 飢餓)。
            # ユーザー指摘 2026-06-13「注入の優先度は高くあるべき」への対処。
            injected = False
            got = self._take_injection()
            if got:
                opener, injected = got, True
            prompt = self._apply_directives(self._augment(opener))  # 安全弁/autonomy は毎ターン動的評価
            resume = False
            session_turns = 0

            while True:
                if self._stop_requested():
                    # graceful stop: 現セッションで作業していれば作業記録 (handoff) を残してから停止。
                    # force stop (runner.cancel 済) のときは _handoff が即 no-op で返る。
                    if self.handoff_on_stop and session_turns > 0:
                        total_cost += self._handoff(active, sid)
                        turns += 1
                    return self._finish("stopped", sessions, turns, total_cost, "stop requested")
                if self.max_total_cost_usd is not None and total_cost >= self.max_total_cost_usd:
                    return self._finish("max_cost", sessions, turns, total_cost, "cost cap reached")

                # これから claude に送る prompt を GUI に見せる (特に注入タスクの実行点を可視化)。
                self._emit("task", session_id=sid, session_index=sessions + 1, turn=turns + 1,
                           injected=injected, prompt=prompt)
                res = active.run_turn(prompt=prompt, session_id=sid, resume=resume, cwd=self.workdir)
                turns += 1
                session_turns += 1
                total_cost += res.cost_usd
                used = self.used_pct(res)
                self.ledger.append(
                    event="turn", cmd_id=sid, action="query-state",
                    detail=f"ctx={res.context_tokens} used={used:.0%} cost={res.cost_usd:.4f} "
                           f"err={res.error_kind or '-'}",
                )
                self._emit(
                    "turn", session_id=sid, session_index=sessions + 1, turn=turns,
                    context_tokens=res.context_tokens, used_pct=used, cost_usd=res.cost_usd,
                    total_cost=total_cost, text=res.text, error_kind=res.error_kind,
                )

                # cancelled = Stop / ウィンドウ終了由来。リトライせず即停止する。
                if res.error_kind == "cancelled":
                    return self._finish("stopped", sessions, turns, total_cost, "turn cancelled")

                # interrupted = 緊急注入による現ターン中断。ループは止めず、注入タスクを
                # 次ターンで必ず消費する (スキップしない)。中断はエラーに数えない。
                if res.error_kind == "interrupted":
                    self.ledger.append(
                        event="interrupted", cmd_id=sid, action="inject",
                        detail="emergency injection — continue with injected task",
                    )
                    self._emit("interrupted", session_id=sid, session_index=sessions + 1, turn=turns)
                    consec_err = 0
                    (prompt, injected), resume = self._continue_prompt(), True
                    continue

                # 認証切れ = 構造的上限。fail-closed で停止 (暴走させない / 人間を待つ)。
                if res.error_kind == "auth":
                    self.ledger.append(
                        event="auth_required", cmd_id=sid, action="shutdown",
                        detail="re-login required — fail-closed stop (human needed)",
                    )
                    return self._finish("auth_required", sessions, turns, total_cost,
                                        "re-login required")

                # レート制限 = このプロバイダをブロック登録し、別プロバイダがあれば切替、
                # 無ければ resetsAt まで待って自動再開する (サブスク自走の主制約)。
                # consec_err は増やさない (失敗ではなく待ち / 切替)。
                if res.error_kind == "rate_limited" and self.auto_resume_on_rate_limit:
                    now = self.now_fn()
                    # resetsAt が未来のときのみそこまで benched。過去/不明 (0) は固定待ちへ落とす
                    # (_wait_until と同じ past-guard)。過去 epoch を block に入れると即解除され、
                    # 直前に rate_limited を返したプロバイダを再選択して provider_switch を逃す
                    # (codex の "try again at <過去日>" / tz スキューで過去化した場合の回帰)。
                    until = float(res.rate_limit_resets_at) \
                        if res.rate_limit_resets_at and res.rate_limit_resets_at > now \
                        else now + self.rate_limit_fallback_wait_s
                    self._blocked_until[active_idx] = until
                    self.ledger.append(
                        event="rate_limited", cmd_id=sid, action="wait",
                        detail=f"provider={self.provider_name(active)} "
                               f"resets_at={res.rate_limit_resets_at} status={res.rate_limit_status}",
                    )
                    self._emit("rate_limited", session_id=sid, provider=self.provider_name(active),
                               resets_at=res.rate_limit_resets_at, status=res.rate_limit_status)
                    # 別プロバイダが今すぐ使えるなら、セッション境界として切替 (fresh session で
                    # SESSION_SUMMARY を読み継続)。無ければ resetsAt まで待って同セッションを再試行。
                    if self._select_available(now, exclude=active_idx) is not None:
                        break  # → 外側ループが次の利用可能プロバイダで新セッション開始
                    if not self._wait_until(res.rate_limit_resets_at):
                        return self._finish("stopped", sessions, turns, total_cost,
                                            "stop during rate-limit wait")
                    self._blocked_until[active_idx] = 0.0  # 解除
                    self.ledger.append(event="rate_limit_resumed", cmd_id=sid, action="resume",
                                       detail=self.provider_name(active))
                    self._emit("rate_limit_resumed", session_id=sid,
                               provider=self.provider_name(active))
                    # opener (resume=False) で rate_limited に当たると、元 sid のセッションが
                    # 既に生成済みの場合 retry の --session-id が "already in use" で弾かれ、
                    # err=other 累積 → circuit_open に至る (J1)。retry 前に fresh sid を採番して
                    # 新規セッションとして開き直す (継続ターン resume=True では元 sid を維持)。
                    if not resume:
                        sid = self._new_session_id()
                    continue  # 同じ prompt を再試行 (consec_err は増やさない)

                # プロバイダの実行ファイルを起動できない (未導入 / PATH 不在 / shim のみ)。
                # 待っても直らないのでこのプロバイダを恒久ブロックし、別プロバイダがあれば即切替、
                # 無ければ原因を明示して停止する (空テキストの err=other で silent circuit_open
                # していた回帰を防ぐ)。consec_err は増やさない (タスク失敗ではなく使用不能)。
                if res.error_kind == "unavailable":
                    self._blocked_until[active_idx] = float("inf")
                    detail = (res.text or "").strip()[:160]
                    self.ledger.append(
                        event="provider_unavailable", cmd_id=sid, action="rotate",
                        detail=f"provider={self.provider_name(active)}: {detail}",
                    )
                    self._emit("provider_unavailable", session_id=sid,
                               provider=self.provider_name(active), detail=detail)
                    if self._select_available(self.now_fn(), exclude=active_idx) is not None:
                        break  # → 外側ループが次の利用可能プロバイダで新セッション開始
                    return self._finish(
                        "provider_unavailable", sessions, turns, total_cost,
                        f"{self.provider_name(active)} unavailable and no fallback available",
                    )

                if res.is_error:
                    consec_err += 1
                    if consec_err >= self.max_consecutive_errors:
                        self.ledger.append(
                            event="circuit_open", cmd_id=sid, action="shutdown",
                            detail=f"{consec_err} consecutive errors",
                        )
                        return self._finish("circuit_open", sessions, turns, total_cost,
                                            f"{consec_err} consecutive errors")
                    (prompt, injected), resume = self._continue_prompt(), True
                    continue
                consec_err = 0

                rotate = used >= self.threshold or session_turns >= self.max_turns_per_session
                if rotate:
                    # exit準備: handoff (SESSION_SUMMARY / next_plan) を書かせてから畳む。
                    # force stop 済みなら run_turn が即 cancelled を返し新プロセスは起動しない
                    # (check-then-act 競合の防止は ClaudeRunner の sticky cancel が担保)。
                    er = self._handoff_run_turn(
                        active, prompt=self.exit_prep_prompt, sid=sid, resume=True,
                    )
                    turns += 1
                    total_cost += er.cost_usd
                    self.ledger.append(
                        event="exit_prep", cmd_id=sid, action="rotate",
                        detail=f"used={used:.0%} turns={session_turns} → rotate",
                    )
                    self._emit("rotate", session_id=sid, session_index=sessions + 1,
                               used_pct=used, session_turns=session_turns)
                    # rotate 地点で停止要求があれば、handoff (exit準備) 済みのまま停止する
                    if self._stop_requested():
                        return self._finish("stopped", sessions, turns, total_cost, "stop requested")
                    break  # → 新セッションへ rotate

                (prompt, injected), resume = self._continue_prompt(), True  # 閾値未満: 同セッション継続

            sessions += 1

        return self._finish("max_sessions", sessions, turns, total_cost,
                            f"reached max_sessions={self.max_sessions}")

    def _finish(self, reason: str, sessions: int, turns: int, cost: float, detail: str) -> Outcome:
        outcome = Outcome(reason, sessions, turns, cost, detail)
        self._emit("stopped", stop_reason=reason, sessions=sessions, turns=turns,
                   total_cost=cost, detail=detail)
        return outcome


@dataclass
class _DryRunner:
    """--dry-run / 仮想 claude: claude を呼ばず使用率が増えていく擬似結果を返す (課金ゼロ)。"""

    window_tokens: int = DEFAULT_WINDOW_TOKENS
    threshold: float = DEFAULT_THRESHOLD
    _calls: int = 0

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        self._calls += 1
        # resume の度に文脈が増える擬似。新セッション (resume=False) でリセット。
        ctx = 10_000 if not resume else int(self.window_tokens * (self.threshold + 0.05))
        return TurnResult(session_id, ctx, 500, ctx, 0.0, "(dry-run turn)", False, "", 1, 0)

    def cancel(self) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="llterm-loop",
        description="llterm L2: 公式 headless protocol で Claude Code を自走ループ駆動 (端末を通らない)",
    )
    parser.add_argument("--workdir", required=True, help="claude を起動する対象プロジェクトのパス")
    parser.add_argument("--resume-prompt", default=DEFAULT_RESUME_PROMPT)
    parser.add_argument("--exit-prep-prompt", default=DEFAULT_EXIT_PREP_PROMPT)
    parser.add_argument("--window-tokens", type=int, default=DEFAULT_WINDOW_TOKENS)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="rotate するコンテキスト使用率 (既定 0.70 = 70%%)")
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--max-cost", type=float, default=None, help="累計コスト上限 (USD)")
    parser.add_argument("--ledger", default=None,
                        help="監査 ledger のパス (既定 <workdir>/.llterm/loop_ledger.jsonl)")
    parser.add_argument("--dry-run", action="store_true",
                        help="claude を呼ばず仮想 claude で配線確認 (課金ゼロ)")
    parser.add_argument("--rad", action="store_true",
                        help="RAD コーパス研究接地を有効化 (新規作業前に D:/docs/*_corpus_v2 を grep)")
    parser.add_argument("--no-offload", action="store_true",
                        help="計算オフロード指令の自動注入を無効化 (既定は有効: 利用可能な "
                             "kaggle/gh/oci 等を検出し、重い計算を自律的に投げる指令を付ける)")
    parser.add_argument("--effort", default="", choices=EFFORT_LEVELS,
                        help="claude の思考努力レベル (low/medium/high/xhigh/max。既定: claude 既定)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="claude モデル (alias: opus/sonnet/haiku、またはフル ID。"
                             "'' で claude 保存既定に委ねる。token 消費を抑えるなら sonnet/haiku。"
                             "既定: %(default)s)")
    parser.add_argument("--template", default="general", help="テンプレ key (templates.py)")
    parser.add_argument("--param", default="", help="テンプレ引数 (例: rad_expand の分野名)")
    # 既定の自走奏者 = Codex (2026-06-15 Anthropic 課金変更対応)。
    # 2026-06-15 以降、claude -p 等のヘッドレス自律利用はサブスク枠から分離され、別建ての
    # 月額クレジットを標準 API 実費で消費する (繰越なし)。一方 Codex は ChatGPT Pro の固定
    # 月額枠で動くため、自走でトークンを大量消費する用途は Codex に寄せるのがコスト最適。
    # Claude も明示選択可 (--runner claude)。codex 未導入なら自動で Claude に倒す (空転防止)。
    parser.add_argument("--runner", default="codex", choices=("codex", "claude"),
                        help="自走の既定奏者 (codex=ChatGPT Pro 固定枠 / claude=API 実費。"
                             "既定: %(default)s。2026-06-15 課金変更で Codex を既定にした)")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        print(t("cli.loop.workdir_missing", workdir=workdir), file=sys.stderr)
        return 2

    if not args.dry_run and args.max_sessions is None and args.max_cost is None:
        print(t("cli.loop.no_budget"), file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger) if args.ledger else workdir / ".llterm" / "loop_ledger.jsonl"

    runner: TurnRunner
    max_sessions = args.max_sessions
    if args.dry_run:
        runner = _DryRunner(window_tokens=args.window_tokens, threshold=args.threshold)
        if max_sessions is None:
            max_sessions = 2
    else:
        # 既定奏者 = Codex (2026-06-15 課金変更: claude -p ヘッドレス自走は API 実費課金になった
        # ため、ChatGPT Pro 固定枠で動く Codex に寄せる)。Claude は --runner claude で明示選択可。
        # codex 未導入時は自動で Claude へ倒し、CLI が空転 (not_found 連発) しないようにする。
        if args.runner == "codex" and shutil.which("codex") is not None:
            from llterm.host.codex_runner import CodexRunner
            runner = CodexRunner()
        else:
            runner = ClaudeRunner(effort=args.effort, model=args.model)

    from llterm import templates as _templates

    try:
        _ov = _templates.get(args.template).build(args.param)
    except KeyError:
        print(t("cli.loop.unknown_template", template=args.template,
                available=", ".join(_templates.keys())), file=sys.stderr)
        return 2
    loop = SessionLoop(
        runner=runner,
        workdir=workdir,
        ledger=Ledger(ledger_path),
        resume_prompt=_ov.get("resume_prompt", args.resume_prompt),
        continue_prompt=_ov.get("continue_prompt", DEFAULT_CONTINUE_PROMPT),
        exit_prep_prompt=args.exit_prep_prompt,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=max_sessions,
        max_total_cost_usd=args.max_cost,
        rad_hint=DEFAULT_RAD_HINT if args.rad else "",
        offload_hint="" if args.no_offload else build_offload_hint(),
    )
    outcome = loop.run()
    print(
        f"\n=== llterm-loop outcome ===\n"
        f"stop: {outcome.stop_reason}\nsessions: {outcome.sessions}\n"
        f"turns: {outcome.turns}\ntotal_cost_usd: {outcome.total_cost_usd:.4f}\n"
        f"detail: {outcome.detail}\nledger: {ledger_path}",
        flush=True,
    )
    return 0 if outcome.stop_reason in ("max_sessions", "max_cost", "stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
