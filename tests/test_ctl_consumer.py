# SPDX-License-Identifier: Apache-2.0
"""ctl consumer — emit した inject-task が worker.inject へ届くことの回帰テスト。

これまで consumer が無く inject-task が queue/ に滞留していた
(memory: feedback_llterm_injection_gui_not_ctl)。本テストはその欠落配線を固定する:

    CtlQueue.poll() → gate.evaluate() → (inject-task & EXECUTE) → inject(text)
"""
from __future__ import annotations

import json
from pathlib import Path

from llterm.ctl.consumer import CtlConsumer, inject_text
from llterm.ctl.ledger import Ledger
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand


class _Recorder:
    def __init__(self, *, running: bool = True) -> None:
        self.injects: list[tuple[str, bool]] = []
        self.announced: list[tuple[str, str, str]] = []  # (kind, cmd_id, text)
        self.running = running

    def inject(self, text: str, emergency: bool) -> None:
        self.injects.append((text, emergency))

    def is_running(self) -> bool:
        return self.running

    def announce(self, kind: str, cmd: CtlCommand, text: str) -> None:
        self.announced.append((kind, cmd.id, text))


def _setup(tmp_path: Path, *, running: bool = True) -> tuple[CtlQueue, CtlConsumer, _Recorder, Ledger]:
    root = tmp_path / ".llterm"
    ledger = Ledger(root / "loop_ledger.jsonl")
    q = CtlQueue(root, ledger=ledger)
    rec = _Recorder(running=running)
    consumer = CtlConsumer(
        q, inject=rec.inject, running=rec.is_running, announce=rec.announce, ledger=ledger
    )
    return q, consumer, rec, ledger


def _results(tmp_path: Path, cmd_id: str) -> dict:
    p = tmp_path / ".llterm" / "results" / f"{cmd_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _ledger_text(ledger: Ledger) -> str:
    return ledger.path.read_text(encoding="utf-8") if ledger.path.exists() else ""


def test_inject_task_reaches_inject_and_finishes(tmp_path: Path) -> None:
    q, consumer, rec, ledger = _setup(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="inject-task", reason="ccr coord",
                        args={"title": "do X"}))
    assert consumer.tick() == 1
    assert rec.injects == [("do X", False)]
    res = _results(tmp_path, "ctl-1")
    assert res["ok"] is True and res["result"]["injected"] == "do X"
    assert not list((tmp_path / ".llterm" / "queue").glob("*.json"))
    assert not list((tmp_path / ".llterm" / "inflight").glob("*.json"))
    assert '"executed"' in _ledger_text(ledger)
    assert ("executed", "ctl-1", "do X") in rec.announced


def test_inject_text_fallback_order() -> None:
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="r",
                                  args={"title": "T", "text": "X"})) == "T"
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="r",
                                  args={"text": "X"})) == "X"
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="r",
                                  args={"prompt": "P"})) == "P"
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="REASON",
                                  args={})) == "REASON"
    # 空白のみは無視して次の候補へ
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="R",
                                  args={"title": "   "})) == "R"


def test_not_running_does_not_poll(tmp_path: Path) -> None:
    q, consumer, rec, _ = _setup(tmp_path, running=False)
    q.submit(CtlCommand(id="ctl-1", action="inject-task", reason="r", args={"title": "X"}))
    assert consumer.tick() == 0
    assert rec.injects == []
    # 未処理なので queue/ に残る (ループ開始時に拾える)
    assert list((tmp_path / ".llterm" / "queue").glob("*ctl-1*"))


def test_rotate_is_rejected_not_injected(tmp_path: Path) -> None:
    q, consumer, rec, ledger = _setup(tmp_path)
    q.submit(CtlCommand(id="ctl-r", action="rotate", reason="ctx high"))
    assert consumer.tick() == 1
    assert rec.injects == []  # consumer は inject-task のみ。rotate は実行しない
    res = _results(tmp_path, "ctl-r")
    assert res["ok"] is False and res["result"]["decision"] == "rejected"
    assert '"rejected"' in _ledger_text(ledger)


def test_requires_human_is_held(tmp_path: Path) -> None:
    q, consumer, rec, ledger = _setup(tmp_path)
    q.submit(CtlCommand(id="ctl-h", action="inject-task", reason="danger",
                        args={"title": "rm -rf"}, requires_human=True))
    assert consumer.tick() == 1
    assert rec.injects == []  # HOLD: 人間承認待ち (--requires-human)
    res = _results(tmp_path, "ctl-h")
    assert res["ok"] is False and res["result"]["decision"] == "hold_for_human"
    assert '"hold_for_human"' in _ledger_text(ledger)


def test_emergency_arg_uses_emergency_path(tmp_path: Path) -> None:
    q, consumer, rec, _ = _setup(tmp_path)
    q.submit(CtlCommand(id="ctl-e", action="inject-task", reason="urgent",
                        args={"title": "now", "emergency": "true"}))
    consumer.tick()
    assert rec.injects == [("now", True)]


def test_drains_fifo_up_to_max(tmp_path: Path) -> None:
    q, consumer, rec, _ = _setup(tmp_path)
    for i in range(3):
        q.submit(CtlCommand(id=f"ctl-{i}", action="inject-task", reason="r",
                            args={"title": f"t{i}"}))
    assert consumer.tick(max_commands=2) == 2
    assert rec.injects == [("t0", False), ("t1", False)]  # FIFO
    assert len(list((tmp_path / ".llterm" / "queue").glob("*.json"))) == 1  # 3 件目は残る


def test_inject_exception_is_failsafe(tmp_path: Path) -> None:
    root = tmp_path / ".llterm"
    ledger = Ledger(root / "l.jsonl")
    q = CtlQueue(root, ledger=ledger)

    def _boom(text: str, emergency: bool) -> None:
        raise RuntimeError("worker gone")

    consumer = CtlConsumer(q, inject=_boom, running=lambda: True, ledger=ledger)
    q.submit(CtlCommand(id="ctl-x", action="inject-task", reason="r", args={"title": "X"}))
    assert consumer.tick() == 1  # 例外でも tick は完了する
    res = _results(tmp_path, "ctl-x")
    assert res["ok"] is False and res["result"]["decision"] == "error"
    assert not list((tmp_path / ".llterm" / "inflight").glob("*.json"))  # drained
