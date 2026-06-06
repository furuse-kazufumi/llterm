import json
import pytest
from llterm.ctl.schema import CtlCommand, ParseError, ALLOWED_ACTIONS


def test_allowed_actions_frozen():
    assert ALLOWED_ACTIONS == ("rotate", "set-effort", "inject-task",
                               "fork-session", "query-state", "shutdown")


def test_parse_minimal_rotate():
    raw = json.dumps({"id": "ctl-1", "action": "rotate", "reason": "test"})
    cmd = CtlCommand.from_json(raw)
    assert cmd.action == "rotate"
    assert cmd.requires_human is False          # 既定 False
    assert cmd.args == {}
    assert cmd.constraints == ()


def test_unknown_action_rejected():
    raw = json.dumps({"id": "ctl-2", "action": "rm-rf", "reason": "x"})
    with pytest.raises(ParseError, match="action"):
        CtlCommand.from_json(raw)


def test_missing_id_rejected():
    raw = json.dumps({"action": "rotate", "reason": "x"})
    with pytest.raises(ParseError, match="id"):
        CtlCommand.from_json(raw)


def test_missing_reason_rejected():
    # 監査必須: reason 無しは fail-closed で拒否
    raw = json.dumps({"id": "ctl-3", "action": "rotate"})
    with pytest.raises(ParseError, match="reason"):
        CtlCommand.from_json(raw)


def test_broken_json_rejected():
    with pytest.raises(ParseError, match="json"):
        CtlCommand.from_json("{not json")


def test_non_dict_json_rejected():
    with pytest.raises(ParseError, match="object"):
        CtlCommand.from_json("[1,2]")


def test_shutdown_forces_requires_human():
    # 危険 action は requires_human=false と書かれていても True に強制 (fail-closed)
    raw = json.dumps({"id": "ctl-4", "action": "shutdown", "reason": "x",
                      "requires_human": False})
    cmd = CtlCommand.from_json(raw)
    assert cmd.requires_human is True
