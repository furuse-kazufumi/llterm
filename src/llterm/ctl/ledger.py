"""append-only 監査 ledger (jsonl).

全制御イベント (received / gated / executed / rejected / error) を 1 行 JSON で残す。
監査が本処理を殺さないよう、書き込み失敗・直列化不能は内部で吸収する (fail-safe)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, *, event: str, cmd_id: str, action: str, detail: object) -> None:
        try:
            detail_s = detail if isinstance(detail, (str, int, float, bool, type(None))) else repr(detail)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event, "cmd_id": cmd_id, "action": action, "detail": detail_s,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 監査失敗で本処理を止めない (fail-safe)
