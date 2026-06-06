import json
from pathlib import Path
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand


def _mk(tmp_path: Path) -> CtlQueue:
    return CtlQueue(tmp_path / ".llterm")


def test_submit_creates_queue_file(tmp_path: Path):
    q = _mk(tmp_path)
    cmd = CtlCommand(id="ctl-1", action="rotate", reason="r")
    q.submit(cmd)
    files = list((tmp_path / ".llterm" / "queue").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["id"] == "ctl-1"


def test_poll_consumes_in_order_and_moves_to_inflight(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-a", action="query-state", reason="r"))
    q.submit(CtlCommand(id="ctl-b", action="rotate", reason="r"))
    got = q.poll()
    assert got is not None and got.id == "ctl-a"          # FIFO
    # ファイル名は <seq>-ctl-a.json (連番 prefix) なので glob は *ctl-a* で照合する
    assert not list((tmp_path / ".llterm" / "queue").glob("*ctl-a*"))
    assert list((tmp_path / ".llterm" / "inflight").glob("*ctl-a*"))


def test_poll_empty_returns_none(tmp_path: Path):
    assert _mk(tmp_path).poll() is None


def test_poll_skips_broken_json_and_quarantines(tmp_path: Path):
    q = _mk(tmp_path)
    qdir = tmp_path / ".llterm" / "queue"
    qdir.mkdir(parents=True)
    (qdir / "0000-bad.json").write_text("{broken", encoding="utf-8")
    q.submit(CtlCommand(id="ctl-ok", action="rotate", reason="r"))
    got = q.poll()
    assert got is not None and got.id == "ctl-ok"          # 壊れた方は飛ばす
    assert list((tmp_path / ".llterm" / "rejected").glob("*bad*"))  # 隔離される


def test_write_result_and_finish(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="query-state", reason="r"))
    cmd = q.poll()
    q.finish(cmd, ok=True, result={"state": "alive"})
    res_files = list((tmp_path / ".llterm" / "results").glob("ctl-1*.json"))
    assert len(res_files) == 1
    rec = json.loads(res_files[0].read_text(encoding="utf-8"))
    assert rec["ok"] is True and rec["result"]["state"] == "alive"
    assert not list((tmp_path / ".llterm" / "inflight").glob("ctl-1*"))


def test_duplicate_id_rejected_on_submit(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="rotate", reason="r"))
    import pytest
    with pytest.raises(FileExistsError):
        q.submit(CtlCommand(id="ctl-1", action="rotate", reason="r"))
