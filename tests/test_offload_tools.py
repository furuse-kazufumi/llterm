# SPDX-License-Identifier: Apache-2.0
"""offload_tools (計算オフロード CLI の検出 + ヒント生成) の回帰テスト。"""
from __future__ import annotations

from llterm.host import offload_tools
from llterm.host.offload_tools import (
    OFFLOAD_TOOLS,
    OffloadTool,
    build_offload_hint,
    detect_available,
)


def _tool(name: str, ok: bool) -> OffloadTool:
    return OffloadTool(name, lambda: ok, f"{name}: use it")


def test_detect_filters_unavailable() -> None:
    tools = (_tool("a", True), _tool("b", False), _tool("c", True))
    got = [t.name for t in detect_available(tools)]
    assert got == ["a", "c"]


def test_detect_swallows_exceptions() -> None:
    def boom() -> bool:
        raise RuntimeError("nope")

    tools = (OffloadTool("x", boom, "x"), _tool("y", True))
    assert [t.name for t in detect_available(tools)] == ["y"]  # 例外は無し扱い


def test_build_hint_empty_when_none() -> None:
    assert build_offload_hint([]) == ""


def test_build_hint_lists_available_tools() -> None:
    hint = build_offload_hint([_tool("kaggle", True), _tool("gh", True)])
    assert "計算オフロード" in hint
    assert "kaggle: use it" in hint
    assert "gh: use it" in hint
    assert "fail-closed" in hint  # push/課金は人間確認の但し書き


def test_registry_has_known_tools() -> None:
    names = {t.name for t in OFFLOAD_TOOLS}
    assert {"kaggle", "gh", "oci"} <= names


def test_kaggle_detect_requires_cli_and_auth(monkeypatch, tmp_path) -> None:
    # CLI 無し → False
    monkeypatch.setattr(offload_tools.shutil, "which", lambda n: None)
    assert offload_tools._kaggle_available() is False
    # CLI 有り + env 認証 → True
    monkeypatch.setattr(offload_tools.shutil, "which", lambda n: "C:/kaggle.exe")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    monkeypatch.setenv("KAGGLE_USERNAME", "u")
    assert offload_tools._kaggle_available() is True


def test_kaggle_detect_via_token_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(offload_tools.shutil, "which", lambda n: "C:/kaggle.exe")
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    home = tmp_path
    (home / ".kaggle").mkdir()
    (home / ".kaggle" / "kaggle.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(offload_tools.Path, "home", staticmethod(lambda: home))
    assert offload_tools._kaggle_available() is True


def test_gh_detect(monkeypatch) -> None:
    monkeypatch.setattr(offload_tools.shutil, "which", lambda n: "C:/gh.exe" if n == "gh" else None)
    assert offload_tools._gh_available() is True
    monkeypatch.setattr(offload_tools.shutil, "which", lambda n: None)
    assert offload_tools._gh_available() is False
