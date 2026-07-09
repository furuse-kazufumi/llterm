# SPDX-License-Identifier: Apache-2.0
"""llterm.version — 表示用バージョン / 最終更新日ロジックの回帰テスト。

git の有無に依存しない形で、日付解決の縮退 (git → mtime → None) と
fail-safe 契約 (例外を外へ出さない) を検証する。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from llterm import __version__
from llterm import version as ver

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_version_line_contains_version() -> None:
    line = ver.version_line()
    assert line.startswith("llterm v")
    assert __version__ in line


def test_updated_date_shape() -> None:
    """更新日は None か YYYY-MM-DD のいずれか (git でも mtime fallback でも形は同じ)。"""
    date = ver.updated_date()
    assert date is None or _DATE_RE.match(date)


def test_updated_date_falls_back_to_mtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """git が使えない (非 git / git 不在) 環境では __init__.py の mtime へ縮退する。"""
    monkeypatch.setattr(ver, "_git", lambda *a, **k: None)
    init_py = tmp_path / "__init__.py"
    init_py.write_text("x = 1\n", encoding="utf-8")
    date = ver.updated_date(source=tmp_path)
    assert date is not None and _DATE_RE.match(date)


def test_updated_date_dir_without_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """__init__.py が無いディレクトリでもディレクトリ mtime で日付を返す (None にしない)。"""
    monkeypatch.setattr(ver, "_git", lambda *a, **k: None)
    date = ver.updated_date(source=tmp_path)
    assert date is not None and _DATE_RE.match(date)


def test_updated_date_returns_none_when_no_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """git 不可 かつ stat も失敗するなら None (例外を投げない)。"""
    monkeypatch.setattr(ver, "_git", lambda *a, **k: None)
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(ver, "_PKG_ROOT", missing)  # fallback 先も存在しない
    date = ver.updated_date(source=missing)
    assert date is None


def test_git_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 実行が例外を投げても _git は None を返す (fail-safe)。"""
    def _boom(*a: object, **k: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert ver._git(["rev-parse", "HEAD"], cwd=Path(".")) is None


def test_git_nonzero_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """git が非ゼロ終了 (非 git ディレクトリ等) なら None。"""
    class _Proc:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert ver._git(["rev-parse", "HEAD"], cwd=Path(".")) is None


def test_commit_hash_shape() -> None:
    sha = ver.commit_hash()
    assert sha is None or re.fullmatch(r"[0-9a-f]{7,40}", sha)


def test_no_window_flags_is_int() -> None:
    assert isinstance(ver._no_window_flags(), int)
