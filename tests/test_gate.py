from llterm.ctl.gate import GateDecision, evaluate
from llterm.ctl.schema import CtlCommand


def test_normal_action_admitted():
    d = evaluate(CtlCommand(id="1", action="rotate", reason="r"))
    assert d is GateDecision.EXECUTE


def test_requires_human_held():
    d = evaluate(CtlCommand(id="2", action="rotate", reason="r", requires_human=True))
    assert d is GateDecision.HOLD_FOR_HUMAN


def test_shutdown_always_held():
    # schema 側で強制済みだが、gate 単体でも二重に守る (defense in depth)
    d = evaluate(CtlCommand(id="3", action="shutdown", reason="r", requires_human=False))
    assert d is GateDecision.HOLD_FOR_HUMAN


def test_disabled_action_rejected():
    d = evaluate(CtlCommand(id="4", action="fork-session", reason="r"),
                 enabled_actions=("rotate", "query-state"))
    assert d is GateDecision.REJECT
