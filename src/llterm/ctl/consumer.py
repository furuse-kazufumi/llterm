# SPDX-License-Identifier: Apache-2.0
"""ctl queue の consumer — Claude (emit CLI) が投函した inject-task を実注入へ流す。

producer (``llterm.ctl.emit`` / :meth:`CtlQueue.submit`) は ``queue/`` に JSON を積むだけで、
これまで **consumer が存在せず** inject-task が ``worker.inject`` に届かなかった
(memory: ``feedback_llterm_injection_gui_not_ctl``)。``gate.py`` のコメント
「host 実装が入る Task12 以降に拡張」が示すとおり、消費側が未実装だった。本モジュールが
その欠落を埋め、Claude Code (ccr) → llterm の帯域外タスク注入を成立させる::

    CtlQueue.poll() → gate.evaluate() → (inject-task & EXECUTE) → inject(text)

設計規律 (fail-closed / 監査可能):

- **inject-task のみ** consumer 対応 (:data:`CONSUMER_ENABLED`)。``rotate`` / ``query-state`` /
  ``shutdown`` 等は gate で **REJECT** し ledger に残す (勝手に実行しない = 半端な実装で
  暴発させない)。各 action の host 実装が入ったらここを広げる。
- ``requires_human`` (emit ``--requires-human``) は **HOLD_FOR_HUMAN**: 実行せず保留を記録
  (人間が GUI で後追い承認する想定)。危険タスクの安全弁。
- 全イベント (``executed`` / ``rejected`` / ``hold_for_human`` / ``ignored`` / ``error``) を
  ledger と ``results/`` に残す (producer 側 = ccr が次に結果を読める)。
- ``inject`` が例外を投げても握り潰し ``ok=False`` で finish する (1 件の不良が tick を殺さない)。
- ``running()`` が False の間は **poll しない** → コマンドは ``queue/`` に滞留し、ループ開始時に
  拾われる (Start 前に積んでおける)。
"""
from __future__ import annotations

from collections.abc import Callable

from llterm.ctl.gate import GateDecision, evaluate
from llterm.ctl.ledger import Ledger
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand

# consumer が実際に実行する action。gate.DEFAULT_ENABLED より狭める (inject-task のみ実装済)。
CONSUMER_ENABLED: tuple[str, ...] = ("inject-task",)

# 注入本文を探す args キー (優先順)。いずれも無ければ reason を使う (reason は schema で必須・非空)。
_TEXT_KEYS: tuple[str, ...] = ("title", "text", "prompt", "task")

InjectFn = Callable[[str, bool], None]  # (text, emergency)
RunningFn = Callable[[], bool]
AnnounceFn = Callable[[str, CtlCommand, str], None]  # (kind, cmd, text)


def _truthy(value: object) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def inject_text(cmd: CtlCommand) -> str:
    """inject-task の注入本文を決める。``args.title/text/prompt/task`` → ``reason`` の順。

    空白のみの値は無効として次の候補へ送る (空注入を防ぐ)。
    """
    for key in _TEXT_KEYS:
        value = cmd.args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return cmd.reason.strip()


class CtlConsumer:
    """ctl queue を消費し inject-task を ``inject`` コールバックへ流す (gate 尊重・fail-closed)。

    GUI からは走行中に QTimer で :meth:`tick` を呼ぶ。``inject`` は ``worker.inject`` を束ねた
    もので、GUI の手動注入と**同一経路** (``next_prompt`` → SessionLoop) に乗る。
    """

    def __init__(
        self,
        queue: CtlQueue,
        *,
        inject: InjectFn,
        running: RunningFn,
        announce: AnnounceFn | None = None,
        ledger: Ledger | None = None,
        enabled_actions: tuple[str, ...] = CONSUMER_ENABLED,
    ) -> None:
        self._q = queue
        self._inject = inject
        self._running = running
        self._announce = announce
        self._ledger = ledger
        self._enabled = tuple(enabled_actions)

    def tick(self, *, max_commands: int = 8) -> int:
        """走行中なら queue を最大 ``max_commands`` 件まで処理し、処理件数を返す。

        非走行中は 0 を返し **poll しない** (queue/ に滞留させる)。
        """
        if not self._running():
            return 0
        handled = 0
        for _ in range(max(0, max_commands)):
            cmd = self._q.poll()
            if cmd is None:
                break
            self._handle(cmd)
            handled += 1
        return handled

    def _handle(self, cmd: CtlCommand) -> None:
        decision = evaluate(cmd, enabled_actions=self._enabled)
        if decision is GateDecision.REJECT:
            self._finish(cmd, ok=False, kind="rejected",
                         result={"decision": "rejected", "action": cmd.action})
            return
        if decision is GateDecision.HOLD_FOR_HUMAN:
            self._finish(cmd, ok=False, kind="hold_for_human",
                         result={"decision": "hold_for_human", "action": cmd.action})
            return
        # EXECUTE — consumer は inject-task のみ実装 (他は CONSUMER_ENABLED で弾かれるが防御的に確認)。
        if cmd.action != "inject-task":
            self._finish(cmd, ok=False, kind="ignored",
                         result={"decision": "ignored", "action": cmd.action})
            return
        text = inject_text(cmd)
        if not text:
            self._finish(cmd, ok=False, kind="rejected",
                         result={"decision": "rejected", "why": "empty inject text"})
            return
        try:
            self._inject(text, _truthy(cmd.args.get("emergency")))
        except Exception as exc:  # noqa: BLE001 — 1 件の注入失敗が tick を殺さない (fail-safe)
            self._finish(cmd, ok=False, kind="error",
                         result={"decision": "error", "error": repr(exc)}, text=text)
            return
        self._finish(cmd, ok=True, kind="executed",
                     result={"decision": "executed", "injected": text}, text=text)

    def _finish(
        self, cmd: CtlCommand, *, ok: bool, kind: str, result: dict[str, object], text: str = ""
    ) -> None:
        if self._ledger is not None:
            self._ledger.append(event=kind, cmd_id=cmd.id, action=cmd.action,
                                detail=(text or cmd.reason)[:200])
        try:
            self._q.finish(cmd, ok=ok, result=result)
        except OSError:
            pass  # 書き戻し失敗で tick を殺さない (監査は ledger 側に残る)
        if self._announce is not None:
            try:
                self._announce(kind, cmd, text)
            except Exception:  # noqa: BLE001 — GUI 表示失敗で本処理を止めない
                pass
