# SPDX-License-Identifier: Apache-2.0
"""RAD 拡張テンプレ + 公開ゲートのテスト (tmp docs_root で安全に検証、実 D:/docs に触れない)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from llterm.rad import (
    RadError,
    build_expand_prompt,
    live_dir,
    promote,
    staging_dir,
)


def test_dir_naming(tmp_path: Path) -> None:
    assert live_dir("robotics", tmp_path) == tmp_path / "robotics_corpus_v2"
    assert staging_dir("robotics", tmp_path) == tmp_path / "robotics_corpus_v2.staging"


def test_build_expand_prompt_targets_staging_not_live(tmp_path: Path) -> None:
    p = build_expand_prompt("robotics", docs_root=tmp_path)
    assert "robotics" in p
    assert "staging" in p
    assert "live" in p                       # live には書かない旨を含む
    assert "raptor-corpus2skill" in p        # 道具を案内
    assert "raptor-corpus-update" in p


def test_promote_moves_staging_to_live(tmp_path: Path) -> None:
    stg = staging_dir("robotics", tmp_path)
    stg.mkdir(parents=True)
    (stg / "INDEX.md").write_text("x", encoding="utf-8")
    res = promote("robotics", docs_root=tmp_path)
    live = live_dir("robotics", tmp_path)
    assert live.is_dir() and (live / "INDEX.md").exists()
    assert not stg.exists()
    assert res.backup is None


def test_promote_backs_up_existing_live(tmp_path: Path) -> None:
    live = live_dir("x", tmp_path)
    live.mkdir(parents=True)
    (live / "old.txt").write_text("old", encoding="utf-8")
    stg = staging_dir("x", tmp_path)
    stg.mkdir(parents=True)
    (stg / "new.txt").write_text("new", encoding="utf-8")
    res = promote("x", docs_root=tmp_path)
    assert (live_dir("x", tmp_path) / "new.txt").exists()   # 新内容が live に
    assert res.backup is not None and (res.backup / "old.txt").exists()  # 旧 live は退避


def test_promote_errors_without_staging_and_keeps_live(tmp_path: Path) -> None:
    live = live_dir("y", tmp_path)
    live.mkdir(parents=True)
    (live / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RadError):
        promote("y", docs_root=tmp_path)
    assert (live / "keep.txt").exists()  # staging 無しでも live は壊れない (fail-closed)


def test_promote_no_backup_replaces(tmp_path: Path) -> None:
    live = live_dir("z", tmp_path)
    live.mkdir(parents=True)
    (live / "old.txt").write_text("old", encoding="utf-8")
    stg = staging_dir("z", tmp_path)
    stg.mkdir(parents=True)
    (stg / "new.txt").write_text("new", encoding="utf-8")
    res = promote("z", docs_root=tmp_path, make_backup=False)
    assert (live_dir("z", tmp_path) / "new.txt").exists()
    assert not (live_dir("z", tmp_path) / "old.txt").exists()
    assert res.backup is None


def test_promote_invalid_domain_rejected(tmp_path: Path) -> None:
    """パス区切り/`..`/空白/空 を含む分野名は昇格せず RadError (traversal 防止・fail-closed)。"""
    for bad in ("../evil", "a/b", "..", "a b", "", "x\y"):
        with pytest.raises(RadError):
            promote(bad, docs_root=tmp_path)


def test_promote_oserror_becomes_raderror(tmp_path: Path, monkeypatch) -> None:
    """破壊的 FS 操作の生 OSError は RadError に正規化される (GUI は RadError のみ catch → 死なない)。"""
    stg = staging_dir("robotics", tmp_path)
    stg.mkdir(parents=True)
    (stg / "INDEX.md").write_text("x", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("locked")

    monkeypatch.setattr("llterm.rad.shutil.move", _boom)
    with pytest.raises(RadError):
        promote("robotics", docs_root=tmp_path)
