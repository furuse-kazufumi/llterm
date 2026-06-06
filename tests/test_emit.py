import json
from pathlib import Path
from llterm.ctl.emit import main as emit_main


def test_emit_rotate(tmp_path: Path, capsys):
    rc = emit_main(["emit", "rotate", "--reason", "context 80%", "--root", str(tmp_path / ".llterm")])
    assert rc == 0
    files = list((tmp_path / ".llterm" / "queue").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["action"] == "rotate"
    assert data["reason"] == "context 80%"
    assert data["id"].startswith("ctl-")
    out = capsys.readouterr().out
    assert data["id"] in out                  # 発火 id を stdout に返す (Claude が控える)


def test_emit_inject_task_with_args(tmp_path: Path):
    rc = emit_main(["emit", "inject-task", "--reason", "r",
                    "--arg", "title=do X", "--arg", "priority=5",
                    "--root", str(tmp_path / ".llterm")])
    assert rc == 0
    data = json.loads(next((tmp_path / ".llterm" / "queue").glob("*.json")).read_text(encoding="utf-8"))
    assert data["args"] == {"title": "do X", "priority": "5"}


def test_emit_unknown_action_fails(tmp_path: Path, capsys):
    rc = emit_main(["emit", "self-destruct", "--reason", "r", "--root", str(tmp_path / ".llterm")])
    assert rc == 2
    assert not list((tmp_path / ".llterm" / "queue").glob("*.json"))


def test_emit_requires_reason(tmp_path: Path):
    rc = emit_main(["emit", "rotate", "--root", str(tmp_path / ".llterm")])
    assert rc == 2
