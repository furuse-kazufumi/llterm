# SPDX-License-Identifier: Apache-2.0
"""テンプレ registry のテスト。"""
from __future__ import annotations

from llterm import templates


def test_registry_has_expected_keys() -> None:
    ks = templates.keys()
    for expected in ("general", "rad_expand", "green_keeper", "doc_update", "security_audit"):
        assert expected in ks


def test_every_template_has_label_and_description() -> None:
    for key in templates.keys():
        t = templates.get(key)
        assert t.label and t.description  # ツールチップ用途が必ずある


def test_general_returns_no_override() -> None:
    assert templates.get("general").build("") == {}


def test_rad_expand_needs_param_and_builds_prompt() -> None:
    t = templates.get("rad_expand")
    assert t.needs_param is True
    assert t.param_label
    out = t.build("robotics")
    assert "resume_prompt" in out and "robotics" in out["resume_prompt"]
    assert "staging" in out["resume_prompt"]
    assert "continue_prompt" in out


def test_green_keeper_and_doc_update_set_resume() -> None:
    assert "resume_prompt" in templates.get("green_keeper").build("")
    assert "resume_prompt" in templates.get("doc_update").build("")


def test_security_audit_template() -> None:
    out = templates.get("security_audit").build("")
    rp = out["resume_prompt"]
    assert "scan" in rp                       # /scan 連携
    assert "raptor.py" in rp
    assert "SECURITY_AUDIT" in rp             # 報告先
    assert "read-only" in rp                  # 監査は read-only (修正しない)
    assert "continue_prompt" in out


def test_get_unknown_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        templates.get("nope")


def test_labels_resolve_per_locale(monkeypatch) -> None:
    """label / description / param_label はアクセス時点の locale で解決される (i18n)。"""
    gen = templates.get("general")
    rad = templates.get("rad_expand")
    monkeypatch.setenv("LLTERM_LANG", "ja")
    assert gen.label == "汎用自走"
    assert rad.param_label == "分野名 (例: robotics)"
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert gen.label == "General self-drive"
    assert "robotics" in rad.param_label
    assert gen.description  # en でも説明が出る


def test_param_label_empty_without_param() -> None:
    assert templates.get("general").param_label == ""  # 引数なしテンプレは空のまま
