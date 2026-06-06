import json
from pathlib import Path
from llterm.ctl.ledger import Ledger


def test_append_and_read(tmp_path: Path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append(event="received", cmd_id="ctl-1", action="rotate", detail="queued")
    led.append(event="executed", cmd_id="ctl-1", action="rotate", detail="ok")
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["event"] == "received"
    assert rec0["cmd_id"] == "ctl-1"
    assert "ts" in rec0  # タイムスタンプ必須


def test_append_creates_parent_dir(tmp_path: Path):
    led = Ledger(tmp_path / "sub" / "ledger.jsonl")
    led.append(event="received", cmd_id="x", action="query-state", detail="")
    assert (tmp_path / "sub" / "ledger.jsonl").exists()


def test_append_never_raises_on_bad_detail(tmp_path: Path):
    # 監査は本処理を殺さない: 直列化できない detail は repr で落とす
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append(event="error", cmd_id="x", action="rotate", detail=object())
    rec = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8"))
    assert "object" in rec["detail"]
