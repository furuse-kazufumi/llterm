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
import os
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
        # FIFO 用の連番 prefix は **プロセス間で比較可能** な wall-clock ns を使う。
        # emit は毎回別プロセスなので monotonic_ns (基準点がプロセス依存・仕様上プロセス間
        # 比較不可) では複数 producer 間の順序が保証されない。time_ns は UTC 基準で比較可能。
        seq = f"{time.time_ns():020d}"
        path = self.qdir / f"{seq}-{cmd.id}.json"
        # アトミック書込み: 一時名 (glob("*.json") が拾わない .json.tmp) に書き切ってから
        # os.replace で最終名へ。これで consumer の poll が「書込み途中」を読んで正当タスクを
        # ParseError で quarantine する race を排除する。
        tmp = self.qdir / f".{seq}-{cmd.id}.json.tmp"
        tmp.write_text(json.dumps(cmd.to_dict(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
        return path

    # ---- consumer 側 (llterm host) ----
    def poll(self) -> CtlCommand | None:
        self._ensure()
        for path in sorted(self.qdir.glob("*.json")):
            try:
                raw = path.read_text(encoding="utf-8")
                cmd = CtlCommand.from_json(raw)
            except (ParseError, UnicodeDecodeError) as e:
                # 解釈できない = 恒久的に壊れている → 隔離 (fail-closed・監査に痕跡を残す)。
                self._quarantine(path, e)
                continue
            except OSError:
                # OSError は一時的の可能性 (Windows 共有違反 / AV ロック / poll と削除の競合)。
                # 隔離すると正当タスクを失うため、隔離せず次 tick で再試行する (tick は殺さない)。
                continue
            try:
                path.rename(self.inflight / path.name)
            except OSError:
                # rename 失敗も transient (別 consumer が先取り等) → 隔離せず次 tick で再試行。
                continue
            return cmd
        return None

    def _quarantine(self, path: Path, err: Exception) -> None:
        """壊れたエントリを rejected/ へ隔離し ledger に痕跡を残す (fail-closed)."""
        detail = f"{type(err).__name__}: {err}"
        target = self.rejected / path.name
        if target.exists():
            # 同名既存でも上書きしない (消さない原則): unique suffix で並置
            target = self.rejected / f"{path.stem}-{time.monotonic_ns()}{path.suffix}"
        try:
            path.rename(target)
            event = "quarantined"
        except OSError:
            event = "quarantine_failed"    # 移動も失敗: 次 poll で再試行される
        if self._ledger is not None:
            self._ledger.append(event=event, cmd_id=path.stem, action="", detail=detail)

    def finish(self, cmd: CtlCommand, *, ok: bool, result: dict | str) -> Path:
        self._ensure()
        for p in self.inflight.glob(f"*-{cmd.id}.json"):
            p.unlink(missing_ok=True)
        out = self.results / f"{cmd.id}.json"
        out.write_text(json.dumps({"id": cmd.id, "ok": ok, "result": result},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        self._prune_results()  # 長時間運用で results/ が無制限に溜まらないよう新しい N 件に保つ
        return out

    def recover_inflight(self) -> int:
        """クラッシュ復旧: inflight/ に残った未完了コマンドを queue/ へ戻す (飢餓の解消)。

        poll で inflight へ移した後 finish 前にプロセスが落ちると、その task は queue にも無く
        二度と実行されない。起動時に本メソッドで queue へ戻し、次 poll で再処理させる。ただし
        ``results/<id>.json`` が既にあれば完了済み → 戻さず残骸を掃除する (二重実行の防止)。
        壊れた inflight 残骸は隔離する (fail-closed)。戻した件数を返す。

        注意: finish 直前 (=副作用は起きたが results 未書込み) でのクラッシュは再実行され得るが、
        『タスクを失わない』を『稀な二重実行』より優先する設計判断。
        """
        self._ensure()
        recovered = 0
        for path in sorted(self.inflight.glob("*.json")):
            try:
                cmd = CtlCommand.from_json(path.read_text(encoding="utf-8"))
            except (ParseError, UnicodeDecodeError) as e:
                self._quarantine(path, e)  # 壊れた残骸 → 隔離
                continue
            except OSError:
                continue  # transient → 次回復旧で再試行
            if (self.results / f"{cmd.id}.json").exists():
                path.unlink(missing_ok=True)  # 完了済み残骸 → 掃除 (二重実行防止)
                continue
            try:
                path.rename(self.qdir / path.name)  # 未完了 → queue へ戻す (seq prefix 維持で FIFO)
                recovered += 1
            except OSError:
                pass
        return recovered

    def _prune_results(self, keep: int = 500) -> None:
        """results/ を新しい keep 件に保つ (古い IPC 応答を掃除)。rejected/ は監査のため掃除しない。"""
        try:
            files = sorted(self.results.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in files[:-keep] if len(files) > keep else []:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
