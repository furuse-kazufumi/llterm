import json
from pathlib import Path
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand
from llterm.host.watcher import CtlWatcher


class FakeHost:
    def __init__(self):
        self.calls = []
    def request_rotate(self, reason):
        self.calls.append(("rotate", reason))
    def state(self):
        return {"alive": True, "session": 1}


def test_rotate_executed_and_ledgered(tmp_path: Path):
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-1", action="rotate", reason="ctx 80%"))
    host = FakeHost()
    w = CtlWatcher(q, host, ledger_path=tmp_path / ".llterm" / "ledger.jsonl")
    n = w.tick()
    assert n == 1
    assert host.calls == [("rotate", "ctx 80%")]
    res = json.loads((tmp_path / ".llterm" / "results" / "ctl-1.json").read_text(encoding="utf-8"))
    assert res["ok"] is True
    led = (tmp_path / ".llterm" / "ledger.jsonl").read_text(encoding="utf-8")
    assert '"executed"' in led


def test_query_state_writes_result(tmp_path: Path):
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-2", action="query-state", reason="r"))
    w = CtlWatcher(q, FakeHost(), ledger_path=tmp_path / ".llterm" / "ledger.jsonl")
    w.tick()
    res = json.loads((tmp_path / ".llterm" / "results" / "ctl-2.json").read_text(encoding="utf-8"))
    assert res["result"]["alive"] is True


def test_hold_for_human_not_executed(tmp_path: Path):
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-3", action="shutdown", reason="r"))
    host = FakeHost()
    w = CtlWatcher(q, host, ledger_path=tmp_path / ".llterm" / "ledger.jsonl")
    w.tick()
    assert host.calls == []                                   # 実行されない
    res = json.loads((tmp_path / ".llterm" / "results" / "ctl-3.json").read_text(encoding="utf-8"))
    assert res["ok"] is False and "human" in res["result"]
    assert w.pending_human and w.pending_human[0].id == "ctl-3"  # 承認待ち一覧に積む


def test_disabled_action_rejected_with_ledger(tmp_path: Path):
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-4", action="set-effort", reason="r"))  # v1 既定で無効
    w = CtlWatcher(q, FakeHost(), ledger_path=tmp_path / ".llterm" / "ledger.jsonl")
    w.tick()
    res = json.loads((tmp_path / ".llterm" / "results" / "ctl-4.json").read_text(encoding="utf-8"))
    assert res["ok"] is False
    led = (tmp_path / ".llterm" / "ledger.jsonl").read_text(encoding="utf-8")
    assert '"rejected"' in led


def test_inject_task_forwards_to_loop_queue(tmp_path: Path):
    # inject-task は claude-loop queue へタスク JSON を書く (constraints 既定 = fail-safe)
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-5", action="inject-task", reason="do X",
                        args={"title": "do X", "priority": "5"}))
    loop_dir = tmp_path / "loop-queue"
    w = CtlWatcher(q, FakeHost(), ledger_path=tmp_path / ".llterm" / "ledger.jsonl",
                   loop_queue_dir=loop_dir)
    w.tick()
    task = json.loads((loop_dir / "ctl-5.json").read_text(encoding="utf-8"))
    assert task["title"] == "do X"
    assert task["priority"] == 5
    assert "no-push" in task["constraints"]          # 既定制約 (危険操作は人間確認)


def test_inject_task_safety_floor_survives_caller_constraints(tmp_path: Path):
    # レビュー finding (high/fail-closed): caller が任意の constraints を渡しても
    # 安全床 (no-push / needs-human-judgment) は剥がせない (union 強制)
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-7", action="inject-task", reason="r",
                        args={"title": "t"}, constraints=("custom-only",)))
    loop_dir = tmp_path / "loop-queue"
    w = CtlWatcher(q, FakeHost(), ledger_path=tmp_path / ".llterm" / "ledger.jsonl",
                   loop_queue_dir=loop_dir)
    w.tick()
    task = json.loads((loop_dir / "ctl-7.json").read_text(encoding="utf-8"))
    assert "no-push" in task["constraints"]
    assert "needs-human-judgment" in task["constraints"]
    assert "custom-only" in task["constraints"]


def test_inject_task_without_loop_dir_errors_gracefully(tmp_path: Path):
    # loop_queue_dir 未設定なら error として results に ok=false (ループは死なない)
    q = CtlQueue(tmp_path / ".llterm")
    q.submit(CtlCommand(id="ctl-6", action="inject-task", reason="r"))
    w = CtlWatcher(q, FakeHost(), ledger_path=tmp_path / ".llterm" / "ledger.jsonl")
    n = w.tick()
    assert n == 1
    res = json.loads((tmp_path / ".llterm" / "results" / "ctl-6.json").read_text(encoding="utf-8"))
    assert res["ok"] is False and "error" in res["result"]
