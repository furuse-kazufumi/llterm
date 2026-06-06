"""CtlWatcher — 制御キューを tick で消費し、gate を通して host へ橋渡しする.

v1 の実行対象: rotate (host.request_rotate) / inject-task (claude-loop queue へ転送) /
query-state (host.state() を results へ)。HOLD_FOR_HUMAN は pending_human に積み、
results に ok=false を書く (Claude 側は「人間待ち」と分かる)。REJECT も ok=false + ledger。
"""
from __future__ import annotations

import json
from pathlib import Path

from llterm.ctl.gate import DEFAULT_ENABLED, GateDecision, evaluate
from llterm.ctl.ledger import Ledger
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand


class CtlWatcher:
    def __init__(self, queue: CtlQueue, host, *, ledger_path: Path,
                 enabled_actions: tuple[str, ...] = DEFAULT_ENABLED,
                 loop_queue_dir: Path | None = None) -> None:
        self.queue = queue
        self.host = host
        self.ledger = Ledger(ledger_path)
        self.enabled = enabled_actions
        self.loop_queue_dir = loop_queue_dir   # claude-loop の queue/ (inject-task 転送先)
        self.pending_human: list[CtlCommand] = []

    def tick(self) -> int:
        """キューを 1 巡処理して処理件数を返す (app のメインループから呼ぶ)."""
        n = 0
        while (cmd := self.queue.poll()) is not None:
            n += 1
            self.ledger.append(event="received", cmd_id=cmd.id, action=cmd.action,
                               detail=cmd.reason)
            decision = evaluate(cmd, enabled_actions=self.enabled)
            if decision is GateDecision.REJECT:
                self.ledger.append(event="rejected", cmd_id=cmd.id, action=cmd.action,
                                   detail="not in enabled allowlist")
                self.queue.finish(cmd, ok=False, result="rejected: action not enabled")
                continue
            if decision is GateDecision.HOLD_FOR_HUMAN:
                self.pending_human.append(cmd)
                self.ledger.append(event="held", cmd_id=cmd.id, action=cmd.action,
                                   detail="awaiting human approval")
                self.queue.finish(cmd, ok=False, result="held: awaiting human approval")
                continue
            try:
                result = self._execute(cmd)
                self.ledger.append(event="executed", cmd_id=cmd.id, action=cmd.action,
                                   detail="ok")
                self.queue.finish(cmd, ok=True, result=result)
            except Exception as e:  # noqa: BLE001 — 個別失敗はループを殺さない
                self.ledger.append(event="error", cmd_id=cmd.id, action=cmd.action,
                                   detail=str(e))
                self.queue.finish(cmd, ok=False, result=f"error: {e}")
        return n

    def _execute(self, cmd: CtlCommand) -> dict | str:
        if cmd.action == "rotate":
            self.host.request_rotate(cmd.reason)
            return "rotate requested"
        if cmd.action == "query-state":
            return self.host.state()
        if cmd.action == "inject-task":
            if self.loop_queue_dir is None:
                raise RuntimeError("loop_queue_dir not configured")
            self.loop_queue_dir.mkdir(parents=True, exist_ok=True)
            task = {"title": cmd.args.get("title", "(untitled)"),
                    "description": cmd.args.get("description", cmd.reason),
                    "priority": int(cmd.args.get("priority", "50")),
                    "constraints": list(cmd.constraints) or ["no-push", "needs-human-judgment"],
                    "id": cmd.id, "created_at": cmd.created_at}
            (self.loop_queue_dir / f"{cmd.id}.json").write_text(
                json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
            return f"task injected: {cmd.id}"
        raise RuntimeError(f"no executor for action {cmd.action!r}")
