# SPDX-License-Identifier: Apache-2.0
"""llterm バージョン情報 — 表示用の『バージョン』と『最終更新日』を組み立てる。

GUI のステータスバーに「今どのビルドが動いているか」を出すための下位モジュール。
表示文字列 (locale 依存) は GUI 側 (:mod:`llterm.i18n`) で組み立て、ここは
**素のデータ (バージョン / 日付 / コミット短縮ハッシュ) を返すだけ**に留める。

『最終更新日』の解決順 (いずれも失敗したら None):

1. ソースが git 作業ツリー内にあれば HEAD のコミット日
   (``git log -1 --format=%cd --date=short`` = ``YYYY-MM-DD``)。開発中に
   commit するたび自動で進むので「自分で改造している版」を確認するのに向く。
2. パッケージ ``__init__.py`` の mtime。git が無い配布形態 (wheel 展開) でも
   インストール日相当を近似できる。

fail-safe 契約: バージョン表示は UI を殺してはならない。git 不在・非 git・
タイムアウト等はすべて握りつぶして None (または日付なし) へ縮退する。
Windows では git 子プロセスで conhost がフラッシュしないよう
``CREATE_NO_WINDOW`` を付す (pythonw 起動の GUI で黒窓が一瞬出るのを防ぐ)。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from llterm import __version__

__all__ = ["__version__", "commit_hash", "updated_date", "version_line"]

# .../src/llterm — このファイルが置かれるパッケージルート。
_PKG_ROOT = Path(__file__).resolve().parent

_GIT_TIMEOUT = 3.0  # git 1 コマンドの上限 (秒)。応答しなければ日付なしへ縮退。


def _no_window_flags() -> int:
    """Windows で子プロセスの conhost フラッシュを抑止する ``creationflags``。

    非 Windows では 0 (無効)。``CREATE_NO_WINDOW`` は Python 3.7+ で常設だが
    getattr で保険をかける。
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _git(args: list[str], *, cwd: Path) -> str | None:
    """``git <args>`` を ``cwd`` で実行し stdout を 1 行で返す (失敗は None)。

    非 git ディレクトリ・git 不在・タイムアウト・非ゼロ終了はすべて None。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def updated_date(source: Path | None = None) -> str | None:
    """『最終更新日』を ``YYYY-MM-DD`` で返す。

    git HEAD のコミット日 → ``__init__.py`` の mtime → None の順で縮退する。

    Args:
        source: 探索起点ディレクトリ (既定 = パッケージルート)。テスト用に差し替え可。
    """
    root = source or _PKG_ROOT

    date = _git(["log", "-1", "--format=%cd", "--date=short"], cwd=root)
    if date:
        return date

    target = root / "__init__.py"
    if not target.exists():
        target = root
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def commit_hash(source: Path | None = None) -> str | None:
    """HEAD の短縮コミットハッシュ (git 作業ツリー外なら None)。"""
    return _git(["rev-parse", "--short", "HEAD"], cwd=(source or _PKG_ROOT))


def version_line() -> str:
    """locale 非依存の 1 行表記: ``llterm v0.2.0a0 (2026-07-10)`` (日付不明なら省略)。

    CLI/ログ用。GUI 表示は i18n 経由で別途組み立てる。
    """
    date = updated_date()
    base = f"llterm v{__version__}"
    return f"{base} ({date})" if date else base
