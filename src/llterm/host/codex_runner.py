# SPDX-License-Identifier: Apache-2.0
"""llterm の Codex エンジン — レート制限時のフォールバック用 TurnRunner 実装。

Claude (ClaudeRunner) がレート制限に達したとき、SessionLoop が provider chain を
たどってこの CodexRunner に切り替える。Codex CLI (`codex exec --json`) を非対話で回し、
JSONL イベントを :func:`parse_codex_jsonl` で TurnResult へ変換する。

ChatGPT Pro サブスク認証で動くため **新たな従量課金なし** (cost_usd=0.0)。制約はレート制限。
cross-provider の作業継続性は SESSION_SUMMARY handoff が橋渡しする (Codex は fresh session で
SESSION_SUMMARY / CLAUDE.md を読んで「前回の続き」を継続する)。

実 codex 0.135.0 の `codex exec --json` 出力で確認済のフォーマット (2026-06-12 probe):
- ``thread.started``  : ``thread_id`` (= ``codex exec resume <id>`` で継続)
- ``turn.started``
- ``item.completed``  : ``item`` (``type``=agent_message/command_execution/… と ``text``)
- ``turn.completed``  : ``usage`` (input_tokens / cached_input_tokens / output_tokens / …)
- ``turn.failed`` / ``error`` : エラー
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from llterm.host.loop import (
    _AUTH_SIGNALS,
    _NO_WINDOW,
    _RATE_LIMIT_SIGNALS,
    TurnResult,
    _as_int,
    _short,
    _tool_use_detail,
)
from llterm.i18n import t

# 実装には書込みが必須。Windows では workspace-write でも codex の execpolicy が pwsh 書込みを
# "blocked by policy" で拒否し**書けない** (2026-06-13 実証: workspace-write は全パターン不可、
# danger-full-access のみ書込み可)。codex を実装者にする以上、loop の claude (=skip-permissions
# で全権) と同等の全権を許す方針 (ユーザー決定 2026-06-13: 常に danger-full-access)。
DEFAULT_CODEX_SANDBOX = "danger-full-access"


def summarize_codex_event(ev: object) -> list[dict]:
    """codex exec --json の 1 イベントを GUI 表示用の軽量 dict 列へ要約する。

    Claude の :func:`llterm.host.loop.summarize_stream_event` と同じ ``kind`` 体系へ揃える
    (text / thinking / tool_use / tool_result / init / result) ので GUI 側は共通描画できる。
    """
    if not isinstance(ev, dict):
        return []
    etype = ev.get("type")
    if etype == "thread.started":
        return [{"kind": "init", "model": "codex", "session_id": str(ev.get("thread_id") or "")}]
    if etype == "item.completed":
        item = ev.get("item")
        if not isinstance(item, dict):
            return []
        itype = str(item.get("type") or "")
        if itype == "agent_message":
            text = str(item.get("text") or "")
            return [{"kind": "text", "text": text}] if text.strip() else []
        if itype == "reasoning":
            return [{"kind": "thinking", "preview": _short(str(item.get("text") or ""), 80)}]
        if itype in ("command_execution", "shell", "local_shell_call"):
            detail = item.get("command") or item.get("text") or _tool_use_detail(item)
            return [{"kind": "tool_use", "name": "shell", "detail": _short(str(detail or ""))}]
        if itype in ("file_change", "patch", "apply_patch"):
            return [{"kind": "tool_use", "name": "edit", "detail": _short(str(item.get("text") or ""))}]
        if itype in ("mcp_tool_call", "tool_call", "function_call"):
            return [{"kind": "tool_use", "name": str(item.get("name") or "tool"),
                     "detail": _tool_use_detail(item.get("arguments") or item)}]
        return []  # 未知の item は黙って無視 (将来の type に耐える)
    if etype == "turn.completed":
        return [{"kind": "result", "duration_ms": 0, "is_error": False}]
    return []


def _codex_error_message(ev: dict) -> str:
    """``error`` / ``turn.failed`` イベントから人間可読のエラーメッセージを取り出す。

    実 codex 0.135.0 のフォーマット (2026-06-21 probe):
    - ``{"type":"error","message":"..."}``
    - ``{"type":"turn.failed","error":{"message":"..."}}``  (error は dict)
    旧テスト/将来形式の ``{"...":"error":"<文字列>"}`` (error が文字列) も許容する。
    取り出した message は rate-limit / auth 分類の blob と GUI 表示テキストに使う。
    """
    msg = ev.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    err = ev.get("error")
    if isinstance(err, dict):
        m = err.get("message")
        if isinstance(m, str) and m.strip():
            return m.strip()
    elif isinstance(err, str) and err.strip():
        return err.strip()
    return ""


def parse_codex_jsonl(stdout: str, *, exit_code: int, stderr: str = "") -> TurnResult:
    """``codex exec --json`` (JSONL) を 1 ターン結果へ defensively パースする。

    session_id には codex の ``thread_id`` を入れる (CodexRunner が resume に使う)。
    cost_usd=0.0 (サブスク)。context_window=0 (不明 → SessionLoop が設定窓を使う)。
    """
    thread_id = ""
    texts: list[str] = []
    error_texts: list[str] = []  # error / turn.failed の message (分類 + GUI 表示に使う)
    usage: dict = {}
    turn_completed = False
    failed = False

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
        if etype == "thread.started":
            thread_id = str(ev.get("thread_id") or thread_id)
        elif etype == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                texts.append(str(item.get("text") or ""))
        elif etype == "turn.completed":
            turn_completed = True
            u = ev.get("usage")
            if isinstance(u, dict):
                usage = u
        elif etype in ("turn.failed", "error"):
            failed = True
            msg = _codex_error_message(ev)
            if msg:
                error_texts.append(msg)

    agent_text = texts[-1] if texts else ""
    error_text = "\n".join(error_texts)
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    # context_tokens (= rotate を駆動する「瞬間の窓占有」) は **0 固定**にする。
    # 理由: codex の turn.completed.usage は 1 ターン内の全内部 API 往復の **累積**で、
    # 各往復が文脈を丸ごと再送するため N×文脈に膨れる (実測 ctx 2549% = 物理的に窓を超える
    # = 累積の動かぬ証拠)。これを占有率にすると毎ターン閾値超で rotate し、1 セッション=1 ターンに
    # 縮退する (注入飢餓・レビュー二重の遠因だった)。codex exec resume は自前で文脈を圧縮管理する
    # ため、llterm 側は占有ベースで rotate せず turn 数 (max_turns_per_session=50) で区切れば足りる。
    # input_tokens/output_tokens は情報として保持する (cost/ログ・将来の per-call 取得用)。
    context_tokens = 0

    is_error = exit_code != 0 or failed or not turn_completed
    # 失敗時 (turn.failed) は agent_message が無いのが普通。原因が「中身の見えない err=other」に
    # ならないよう、error/turn.failed の message を表示テキストへ昇格する (GUI で原因が読める)。
    text = agent_text or (error_text if is_error else "")
    error_kind = ""
    if is_error:
        # 分類は error/turn.failed の message・stderr・(あれば) agent_text を走査する。
        # 旧実装は error イベントの message を捨て (failed=True だけ)、stderr が空の実 codex
        # usage-limit 失敗を rate_limited と判定できず other へ誤分類していた。すると loop は
        # フォールバックせず同一プロバイダを再試行し consec_err 累積 → circuit_open で停止していた
        # (本不具合の根本原因。2026-06-21 実機 probe で確定)。
        blob = "\n".join((error_text, stderr, agent_text)).lower()
        if any(s in blob for s in _RATE_LIMIT_SIGNALS):
            error_kind = "rate_limited"
        elif any(s in blob for s in _AUTH_SIGNALS):
            # codex の認証切れは「使用不能」扱いにして loop を保険 (claude) へ graceful fallback
            # させる。claude の auth=fail-closed 全停止と異なり、codex は二次奏者なので全体を
            # 止めず chain から外すのが正しい (人間の再ログイン経路は主奏者 claude が担う)。
            error_kind = "unavailable"
        else:
            error_kind = "other"

    return TurnResult(
        session_id=thread_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_tokens=context_tokens,
        cost_usd=0.0,
        text=text,
        is_error=is_error,
        error_kind=error_kind,
        num_turns=1,
        raw_exit=exit_code,
    )


@dataclass
class CodexRunner:
    """Codex を `codex exec --json` で 1 ターン回す TurnRunner (端末を使わない)。

    - ``resume=False`` → 新 codex thread を作る (`codex exec`)。前 thread は破棄。
    - ``resume=True``  → 直近 thread を継続 (`codex exec resume <thread_id>`)。
    - SessionLoop が渡す session_id (claude 用 uuid) は無視し、codex 自身の thread_id を内部管理。
    - サンドボックス既定 = workspace-write (プロジェクト内の編集可・ネット/外部は不可)。
    """

    exe: str = "codex"
    sandbox: str = DEFAULT_CODEX_SANDBOX
    timeout: float = 7200.0
    model: str = ""  # 空 = codex 設定の既定モデル
    extra_args: Sequence[str] = ()
    on_stream: Callable[[dict], None] | None = None
    _thread_id: str = field(default="", repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    _cancelled: bool = field(default=False, repr=False, compare=False)
    _interrupted: bool = field(default=False, repr=False, compare=False)  # 緊急注入の一発中断

    def _resolved_exe(self) -> str:
        """codex の実体を解決する。codex は npm 配布で Windows では codex.CMD shim が正規なので、
        claude (native .exe 優先) と異なり .cmd/.bat を受け入れる。Python の subprocess は
        フルパスの .cmd を list 形式で安全に起動できる (バッチ用 quoting 適用)。"""
        found = shutil.which(self.exe)
        return found or self.exe

    def _build_args(self, *, resume: bool, cwd: Path) -> list[str]:
        """codex の引数列を組む。**プロンプトは argv に置かず stdin で渡す** ("-" センチネル)。

        理由 (実機バグ): codex は npm 配布で Windows では ``codex.CMD`` shim 経由で起動するため、
        argv に渡した複数行プロンプトは cmd.exe が**最初の改行で途中切断**する (指示文が途切れる)。
        ``codex exec [-]`` / ``codex exec resume <id> [-]`` は "-" 指定で stdin からプロンプトを
        読むので、改行・特殊文字をそのまま安全に渡せ、shell 注入面も同時に消える。

        重要 (実機バグ・circuit_open の主因): ``codex exec resume`` サブコマンドは ``codex exec`` と
        オプション集合が異なり、``-s/--sandbox`` ・ ``-C/--cd`` ・ ``--color`` を**受け付けない**。
        これらを resume に渡すと codex は exit 2 (usage エラー) で即失敗し、**resume ターンが全滅**
        する (新規ターンは成功するのに 2 ターン目以降が err=other → consec_err 累積 → circuit_open)。

        さらに resume は ``-s`` が無いと sandbox を継承せず、danger-full-access でも**書込み不可**に
        なる (2026-06-13 実証)。resume が受け付ける ``-c`` で ``sandbox_mode`` を渡して回避する。
        cwd は ``Popen(cwd=...)`` で担保 (resume は -C 非対応)。
        """
        exe = self._resolved_exe()
        if resume and self._thread_id:
            # resume は -s/-C/--color 非対応。sandbox は -c sandbox_mode で渡す (無いと書けない)。
            base = [exe, "exec", "resume", self._thread_id, "--json", "--skip-git-repo-check",
                    "-c", f'sandbox_mode="{self.sandbox}"']
        else:
            base = [exe, "exec", "--json", "--skip-git-repo-check",
                    "-s", self.sandbox, "-C", str(cwd), "--color", "never"]
        if self.model:
            base += ["-m", self.model]
        base += [*self.extra_args, "-"]  # "-" = プロンプトを stdin から読む (argv truncation 回避)
        return base

    def _notify_stream(self, line: str) -> None:
        if self.on_stream is None:
            return
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        for item in summarize_codex_event(ev):
            try:
                self.on_stream(item)
            except Exception:  # noqa: BLE001
                pass

    def run_turn(self, *, prompt: str, session_id: str, resume: bool, cwd: Path) -> TurnResult:
        args = self._build_args(resume=resume, cwd=cwd)  # プロンプトは stdin で渡す (下記)
        with self._lock:
            if self._cancelled:
                return TurnResult(session_id, 0, 0, 0, 0.0, "", True, "cancelled", 0, -1)
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            # codex 未導入 = 使用不能 → loop が別プロバイダへ即フォールバック (silent circuit 回避)
            return TurnResult(session_id, 0, 0, 0, 0.0, t("runner.codex.not_found"),
                              True, "unavailable", 0, 127)
        with self._lock:
            self._proc = proc
            kill_now = self._cancelled
        if kill_now:
            self._kill(proc)

        # プロンプトを stdin へ書き切って EOF を送る (codex は "-" で stdin から全文を読む)。
        # 別スレッドにすることで、stdout を読む前に大きな prompt を書いてもパイプ
        # デッドロックしない (stderr drain と同型)。子が死んでいれば書込は無害に失敗する。
        def _feed_stdin() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt)
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
        stdin_thread.start()

        timed_out = threading.Event()

        def _on_timeout() -> None:
            if proc.poll() is not None:
                return
            timed_out.set()
            self._kill(proc)

        watchdog = threading.Timer(self.timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        err_buf: list[str] = []

        def _drain_stderr() -> None:
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
            for line in proc.stdout:
                out_lines.append(line)
                self._notify_stream(line)
            try:
                proc.wait(timeout=30)
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
            stdin_thread.join(timeout=5)
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
            # 原因が見えない空テキスト err=other で silent circuit_open しないよう理由を明示する
            # (gem-critic 指摘 2026-06-21)。タイムアウトは一過性のハングもあり得るため other を維持
            # (3 連続のみ circuit_open) だが、GUI で「なぜ落ちたか」が読めるようにする。
            return TurnResult(session_id, 0, 0, 0, 0.0, t("runner.codex.timeout"), True, "other", 0, -1)
        exit_code = proc.returncode if proc.returncode is not None else -1
        res = parse_codex_jsonl("".join(out_lines), exit_code=exit_code, stderr="".join(err_buf))
        if res.session_id:  # codex thread_id を覚えて次ターンの resume に使う
            self._thread_id = res.session_id
        return res

    def cancel(self) -> None:
        """Codex ターンをプロセスツリーごと安全に kill する (恒久・sticky)。"""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def interrupt(self) -> None:
        """現ターンだけを kill する (恒久 cancel と違い、次の run_turn は新規に起動できる)。

        緊急注入用の一発中断。run_turn は error_kind="interrupted" を返し、loop は止めず注入を消費する。
        """
        with self._lock:
            self._interrupted = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
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
