# SPDX-License-Identifier: Apache-2.0
"""ターミナル表示内容のローテーション付き永続ログ (トレーサビリティ用)。

ユーザー要望 2026-06-13:
- ターミナルに表示された内容を **1 時間単位**のファイルに **行単位で append** し残す。
- 保持は最大 **1 週間 (168 時間)**。古いものから自動削除する。
- 失敗 (ディスク/権限) でも GUI を絶対に殺さない (fail-safe = 握り潰す)。

Qt 非依存の純ロジックなので単体テストできる (clock を注入する)。GUI は ``_append`` の
唯一のファネルからここへ 1 行ずつ流す。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

# ファイル名 = "YYYY-MM-DD_HH.log" (時間単位)。この regex で時刻を逆算し保持判定に使う。
_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})\.log$")


class TerminalLog:
    """時間単位のローテーション + 週次保持つき append ロガー。

    ``write()`` は呼ばれるたびに「現在時刻の時間ファイル」へ行を追記する。時間が変わった
    最初の書込みでディレクトリ作成と古いファイルの剪定 (retain_hours 超過分) を行う。
    """

    def __init__(self, log_dir: Path | str, *, retain_hours: int = 168,
                 now_fn: Callable[[], datetime] = datetime.now) -> None:
        self.log_dir = Path(log_dir)
        self.retain_hours = int(retain_hours)
        self._now_fn = now_fn
        self._current_stem: str | None = None  # 直近に書いた時間ファイルの stem (剪定の発火判定)

    def path_for(self, now: datetime) -> Path:
        return self.log_dir / f"{now:%Y-%m-%d_%H}.log"

    def write(self, text: str) -> None:
        """``text`` (複数行可) を現在の時間ファイルへ 1 行ずつ append する (fail-safe)。"""
        try:
            now = self._now_fn()
            path = self.path_for(now)
            if path.stem != self._current_stem:  # 初回 or 時間が変わった → 作成 + 剪定
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._prune(now)
                self._current_stem = path.stem
            with path.open("a", encoding="utf-8") as f:
                lines = text.split("\n")
                if lines and lines[-1] == "":
                    lines.pop()  # 末尾改行による分割余りを除去 (本物の空行は保持)
                for line in lines:  # 常に行単位 append (ユーザー要望)
                    f.write(line + "\n")
        except OSError:
            pass  # ログ失敗で GUI を殺さない

    def _prune(self, now: datetime) -> None:
        """retain_hours より古い時間ファイルを削除する (古いものから / fail-safe)。"""
        cutoff = now - timedelta(hours=self.retain_hours)
        try:
            entries = list(self.log_dir.glob("*.log"))
        except OSError:
            return
        for p in entries:
            dt = _parse_stem(p.name)
            if dt is not None and dt < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass


def _parse_stem(name: str) -> datetime | None:
    """"YYYY-MM-DD_HH.log" を datetime に変換する (不正名は None = 剪定対象外)。"""
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        y, mo, d, hh = (int(g) for g in m.groups())
        return datetime(y, mo, d, hh)
    except (ValueError, OverflowError):
        return None
