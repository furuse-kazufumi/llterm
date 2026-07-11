# SPDX-License-Identifier: Apache-2.0
"""CtlConsumer (ctl queue → inject 配線) の回帰テスト。

安全上重要 (gate 尊重・fail-closed・例外隔離) だが従来テストが無かったため新設。
実 CtlQueue (tmp_path) + 偽の inject/running/announce/ledger で検証する。
"""
from __future__ import annotations

from pathlib import Path

from llterm.ctl.consumer import CtlConsumer, inject_text
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand


def _q(tmp_path: Path) -> CtlQueue:
    return CtlQueue(tmp_path / ".llterm")


def _results_text(tmp_path: Path, cid: str) -> str:
    return (tmp_path / ".llterm" / "results" / f"{cid}.json").read_text(encoding="utf-8")


def test_inject_task_executed(tmp_path: Path) -> None:
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="inject-task", reason="r", args={"title": "do X"}))
    injected: list = []
    c = CtlConsumer(q, inject=lambda text, emerg: injected.append((text, emerg)),
                    running=lambda: True)
    assert c.tick() == 1
    assert injected == [("do X", False)]
    assert '"executed"' in _results_text(tmp_path, "ctl-1")


def test_disabled_action_rejected(tmp_path: Path) -> None:
    """consumer は inject-task のみ実装 → rotate 等は fail-closed で REJECT (実行しない)。"""
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-2", action="rotate", reason="r"))
    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append(t), running=lambda: True)
    c.tick()
    assert injected == []
    assert '"rejected"' in _results_text(tmp_path, "ctl-2")


def test_requires_human_held_not_executed(tmp_path: Path) -> None:
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-3", action="inject-task", reason="r",
                        args={"title": "x"}, requires_human=True))
    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append(t), running=lambda: True)
    c.tick()
    assert injected == []  # 保留 = 実行しない (危険タスクの安全弁)
    assert '"hold_for_human"' in _results_text(tmp_path, "ctl-3")


def test_not_running_does_not_poll(tmp_path: Path) -> None:
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-4", action="inject-task", reason="r", args={"title": "x"}))
    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append(t), running=lambda: False)
    assert c.tick() == 0
    assert injected == []
    assert list((tmp_path / ".llterm" / "queue").glob("*.json"))  # queue に滞留 (後で拾う)


def test_inject_exception_finishes_error_and_survives(tmp_path: Path) -> None:
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-5", action="inject-task", reason="r", args={"title": "x"}))

    def _boom(text: str, emerg: bool) -> None:
        raise RuntimeError("inject failed")

    c = CtlConsumer(q, inject=_boom, running=lambda: True)
    assert c.tick() == 1  # 例外を握り tick は死なない
    assert '"error"' in _results_text(tmp_path, "ctl-5")


def test_ledger_failure_does_not_orphan_command(tmp_path: Path) -> None:
    """ledger 書込み失敗でも finish は続行し、poll 済みコマンドを inflight に残さない (回帰)。"""
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-6", action="inject-task", reason="r", args={"title": "x"}))

    class _BoomLedger:
        def append(self, **_kw: object) -> None:
            raise OSError("ledger down")

    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append(t), running=lambda: True,
                    ledger=_BoomLedger())
    c.tick()
    assert injected == ["x"]  # inject は実行済み
    assert (tmp_path / ".llterm" / "results" / "ctl-6.json").exists()  # finish 済み
    assert not list((tmp_path / ".llterm" / "inflight").glob("*.json"))  # orphan していない


def test_emergency_flag_passed_through(tmp_path: Path) -> None:
    q = _q(tmp_path)
    q.submit(CtlCommand(id="ctl-7", action="inject-task", reason="r",
                        args={"title": "x", "emergency": "true"}))
    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append((t, e)), running=lambda: True)
    c.tick()
    assert injected == [("x", True)]  # emergency=true → 緊急注入


def test_tick_bounded_by_max_commands(tmp_path: Path) -> None:
    q = _q(tmp_path)
    for i in range(5):
        q.submit(CtlCommand(id=f"ctl-{i}", action="inject-task", reason="r",
                            args={"title": f"t{i}"}))
    injected: list = []
    c = CtlConsumer(q, inject=lambda t, e: injected.append(t), running=lambda: True)
    assert c.tick(max_commands=2) == 2  # 上限で止まる
    assert len(injected) == 2
    assert c.tick(max_commands=10) == 3  # 残りを次 tick で


def test_inject_text_priority_and_whitespace_fallback() -> None:
    """title > text > prompt > task > reason。空白のみの値は無効として次候補へ。"""
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="R",
                                  args={"title": "T", "text": "X"})) == "T"
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="R",
                                  args={"text": "X"})) == "X"
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="R",
                                  args={"title": "   "})) == "R"  # 空白 → reason へ
    assert inject_text(CtlCommand(id="a", action="inject-task", reason="R")) == "R"
