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


def test_path_traversal_or_unsafe_id_rejected():
    """id はパスに補間されるため、区切り/`..`/glob メタ文字/空白/長すぎは fail-closed で拒否。"""
    for bad in ("../evil", "a/b", "..", ".", "x\\y", "a*b", "a b", "", "z" * 129):
        raw = json.dumps({"id": bad, "action": "rotate", "reason": "x"})
        with pytest.raises(ParseError, match="id"):
            CtlCommand.from_json(raw)


def test_safe_id_accepted():
    """emit 生成形式 (ctl-YYYYMMDDTHHMMSS-XXXX) 等の安全な id は通る。"""
    raw = json.dumps({"id": "ctl-20260711T084530-ab12", "action": "rotate", "reason": "x"})
    assert CtlCommand.from_json(raw).id == "ctl-20260711T084530-ab12"


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
