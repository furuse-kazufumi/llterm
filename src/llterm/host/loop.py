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
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from llterm.ctl.ledger import Ledger


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

DEFAULT_RESUME_PROMPT = (
    "セッション再開。CLAUDE.md の SESSION START 手順に従い、docs/SESSION_SUMMARY.md と "
    "next_plan を読んで最優先タスクを自律継続せよ。確認は求めない。"
    "制約: push / 削除 / 不可逆操作は人間承認なしに行わない (fail-closed)。"
)
DEFAULT_EXIT_PREP_PROMPT = (
    "コンテキスト上限が近い。今は新規作業を始めず EXIT準備のみ行え: "
    "docs/SESSION_SUMMARY.md と next_plan を現状と『次の具体的な一手』へ更新し、"
    "新セッションが続きを再開できる状態にせよ。"
)
DEFAULT_CONTINUE_PROMPT = "前回の続きを自律継続せよ。確認は求めない。"

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
    error_kind: str  # "" | "auth" | "other"
    num_turns: int
    raw_exit: int


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

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("session_id"):
            session_id = str(ev["session_id"])
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

    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    context_tokens = (
        input_tokens
        + _as_int(usage.get("cache_read_input_tokens"))
        + _as_int(usage.get("cache_creation_input_tokens"))
    )

    error_kind = ""
    if exit_code != 0 or is_error or not result_seen:
        is_error = True
        blob = (stdout + "\n" + stderr).lower()
        error_kind = "auth" if any(sig in blob for sig in _AUTH_SIGNALS) else "other"

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


_SUBSCRIPTION_STRIP_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


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


class TurnRunner(Protocol):
    """1 ターンを実行して結果を返す抽象 (テスト/GUI は mock や仮想 claude を注入する)。"""

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult: ...


@dataclass
class ClaudeRunner:
    """実 claude を headless stream-json で 1 ターン回す。端末を使わない (PTY なし)。

    - ``resume=False`` → ``--session-id <uuid>`` で新規セッション作成。
    - ``resume=True``  → ``--resume <uuid>`` で同セッション継続 (文脈保持)。
    - ``stdin=DEVNULL`` — 自走ループの子は stdin を待たない (orphan-reader hang を構造的に排除)。
    - list-based args (shell 不使用)。``--verbose`` は ``-p`` + stream-json に必須。
    """

    exe: str = "claude"
    timeout: float = 1800.0
    skip_permissions: bool = True
    use_subscription: bool = True  # True: API キー env を外し claude.ai サブスク認証で回す (課金回避)
    extra_args: Sequence[str] = ()

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        session_flag = ["--resume", session_id] if resume else ["--session-id", session_id]
        args = [
            self.exe,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            *session_flag,
        ]
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        args.extend(self.extra_args)
        try:
            proc = subprocess.run(
                args,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=_subscription_env() if self.use_subscription else None,
            )
        except FileNotFoundError:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "other", 0, 127)
        except subprocess.TimeoutExpired:
            return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "other", 0, -1)
        return parse_stream_json(proc.stdout, exit_code=proc.returncode, stderr=proc.stderr)


@dataclass(frozen=True)
class Outcome:
    stop_reason: str  # "max_sessions" | "max_cost" | "auth_required" | "circuit_open" | "stopped"
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
    resume_prompt: str = DEFAULT_RESUME_PROMPT
    exit_prep_prompt: str = DEFAULT_EXIT_PREP_PROMPT
    continue_prompt: str = DEFAULT_CONTINUE_PROMPT
    rad_hint: str = ""  # 非空なら作業 prompt に RAD 研究接地ヒントを付ける (--rad)
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    threshold: float = DEFAULT_THRESHOLD
    max_sessions: int | None = None
    max_total_cost_usd: float | None = None
    max_consecutive_errors: int = 3
    max_turns_per_session: int = DEFAULT_MAX_TURNS_PER_SESSION
    on_event: Callable[[str, dict], None] | None = None
    should_stop: Callable[[], bool] | None = None  # GUI の Stop ボタン等 (協調停止)
    next_prompt: Callable[[], str | None] | None = None  # GUI のタスク注入 (継続ターンで一度だけ優先)

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
        """作業 prompt に RAD 研究接地ヒントを付ける (rad_hint 設定時のみ。exit準備には付けない)。"""
        return f"{prompt}\n\n{self.rad_hint}" if self.rad_hint else prompt

    def _continue_prompt(self) -> str:
        """継続ターンの prompt。GUI が inject したタスクがあれば優先する (一度だけ)。"""
        base = self.continue_prompt
        if self.next_prompt is not None:
            try:
                injected = self.next_prompt()
            except Exception:  # noqa: BLE001
                injected = None
            if injected:
                base = injected
        return self._augment(base)

    def used_pct(self, res: TurnResult) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return res.context_tokens / self.window_tokens

    def _new_session_id(self) -> str:
        # rotation = 新 session-id = fresh context。UUID 衝突は事実上ゼロ。
        return str(uuid.uuid4())

    def run(self) -> Outcome:
        sessions = 0
        turns = 0
        total_cost = 0.0
        consec_err = 0

        while self.max_sessions is None or sessions < self.max_sessions:
            if self._stop_requested():
                return self._finish("stopped", sessions, turns, total_cost, "stop requested")
            sid = self._new_session_id()
            self.ledger.append(
                event="session_start", cmd_id=sid, action="rotate",
                detail=f"session#{sessions + 1}",
            )
            self._emit("session_start", session_id=sid, session_index=sessions + 1)
            prompt = self._augment(self.resume_prompt)
            resume = False
            session_turns = 0

            while True:
                if self._stop_requested():
                    return self._finish("stopped", sessions, turns, total_cost, "stop requested")
                if self.max_total_cost_usd is not None and total_cost >= self.max_total_cost_usd:
                    return self._finish("max_cost", sessions, turns, total_cost, "cost cap reached")

                res = self.runner.run_turn(prompt=prompt, session_id=sid, resume=resume, cwd=self.workdir)
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

                # 認証切れ = 構造的上限。fail-closed で停止 (暴走させない / 人間を待つ)。
                if res.error_kind == "auth":
                    self.ledger.append(
                        event="auth_required", cmd_id=sid, action="shutdown",
                        detail="re-login required — fail-closed stop (human needed)",
                    )
                    return self._finish("auth_required", sessions, turns, total_cost,
                                        "re-login required")

                if res.is_error:
                    consec_err += 1
                    if consec_err >= self.max_consecutive_errors:
                        self.ledger.append(
                            event="circuit_open", cmd_id=sid, action="shutdown",
                            detail=f"{consec_err} consecutive errors",
                        )
                        return self._finish("circuit_open", sessions, turns, total_cost,
                                            f"{consec_err} consecutive errors")
                    prompt, resume = self._continue_prompt(), True
                    continue
                consec_err = 0

                rotate = used >= self.threshold or session_turns >= self.max_turns_per_session
                if rotate:
                    # exit準備: handoff (SESSION_SUMMARY / next_plan) を書かせてから畳む。
                    self.runner.run_turn(
                        prompt=self.exit_prep_prompt, session_id=sid, resume=True, cwd=self.workdir,
                    )
                    turns += 1
                    self.ledger.append(
                        event="exit_prep", cmd_id=sid, action="rotate",
                        detail=f"used={used:.0%} turns={session_turns} → rotate",
                    )
                    self._emit("rotate", session_id=sid, session_index=sessions + 1,
                               used_pct=used, session_turns=session_turns)
                    break  # → 新セッションへ rotate

                prompt, resume = self._continue_prompt(), True  # 閾値未満: 同セッション継続

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
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        print(f"error: --workdir が存在しません: {workdir}", file=sys.stderr)
        return 2

    if not args.dry_run and args.max_sessions is None and args.max_cost is None:
        print("error: --max-sessions か --max-cost のどちらかを指定してください "
              "(無制限自走は課金保護のため拒否)", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger) if args.ledger else workdir / ".llterm" / "loop_ledger.jsonl"

    runner: TurnRunner
    max_sessions = args.max_sessions
    if args.dry_run:
        runner = _DryRunner(window_tokens=args.window_tokens, threshold=args.threshold)
        if max_sessions is None:
            max_sessions = 2
    else:
        runner = ClaudeRunner()

    loop = SessionLoop(
        runner=runner,
        workdir=workdir,
        ledger=Ledger(ledger_path),
        resume_prompt=args.resume_prompt,
        exit_prep_prompt=args.exit_prep_prompt,
        window_tokens=args.window_tokens,
        threshold=args.threshold,
        max_sessions=max_sessions,
        max_total_cost_usd=args.max_cost,
        rad_hint=DEFAULT_RAD_HINT if args.rad else "",
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
