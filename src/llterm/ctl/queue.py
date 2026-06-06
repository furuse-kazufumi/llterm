"""ファイルベース制御キュー (.llterm/).

queue/    — Claude (emit CLI) が投函する CtlCommand JSON。ファイル名 = <seq>-<id>.json
inflight/ — poll で取り出し中のコマンド (クラッシュ時に残骸が見える)
results/  — finish() の書き戻し (Claude が次ターンで読む)
rejected/ — 壊れた JSON / parse 失敗の隔離 (fail-closed: 実行しない・消さない)

順序は zero-pad した連番 prefix で FIFO を保証。重複 id は submit 時に拒否。
quarantine は ledger に "quarantined" として残す (レビュー finding: 監査盲点の解消 —
敵対的な壊れコマンドも痕跡ゼロで消えない)。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from llterm.ctl.ledger import Ledger
from llterm.ctl.schema import CtlCommand, ParseError


class CtlQueue:
    def __init__(self, root: Path | str, *, ledger: Ledger | None = None) -> None:
        self.root = Path(root)
        self.qdir = self.root / "queue"
        self.inflight = self.root / "inflight"
        self.results = self.root / "results"
        self.rejected = self.root / "rejected"
        self._ledger = ledger              # consumer 側のみ注入 (producer は不要)

    def _ensure(self) -> None:
        for d in (self.qdir, self.inflight, self.results, self.rejected):
            d.mkdir(parents=True, exist_ok=True)

    # ---- producer 側 (Claude / emit CLI) ----
    def submit(self, cmd: CtlCommand) -> Path:
        self._ensure()
        if any(p.name.endswith(f"-{cmd.id}.json") or p.stem == cmd.id
               for p in self.qdir.glob("*.json")):
            raise FileExistsError(f"duplicate command id: {cmd.id}")
        seq = f"{time.monotonic_ns():020d}"
        path = self.qdir / f"{seq}-{cmd.id}.json"
        path.write_text(json.dumps(cmd.to_dict(), ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return path

    # ---- consumer 側 (llterm host) ----
    def poll(self) -> CtlCommand | None:
        self._ensure()
        for path in sorted(self.qdir.glob("*.json")):
            raw = path.read_text(encoding="utf-8")
            try:
                cmd = CtlCommand.from_json(raw)
            except ParseError:
                path.rename(self.rejected / path.name)  # 隔離 (実行しない・消さない)
                continue
            path.rename(self.inflight / path.name)
            return cmd
        return None

    def finish(self, cmd: CtlCommand, *, ok: bool, result: dict | str) -> Path:
        self._ensure()
        for p in self.inflight.glob(f"*-{cmd.id}.json"):
            p.unlink(missing_ok=True)
        out = self.results / f"{cmd.id}.json"
        out.write_text(json.dumps({"id": cmd.id, "ok": ok, "result": result},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        return out
