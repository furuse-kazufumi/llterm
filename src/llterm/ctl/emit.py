"""emit CLI — Claude が Bash ツールから制御コマンドを投函する入口.

    py -3.11 -m llterm.ctl emit rotate --reason "context 80%"
    py -3.11 -m llterm.ctl emit inject-task --reason "..." --arg title="do X"

帯域外 (ツール経由) のみが発火経路 (spec §5-1: 発話 sentinel 不採用)。
id は自動採番 (ctl-<utcstamp>-<rand4>)。stdout に id を出力し Claude が控える。
"""
from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import ALLOWED_ACTIONS, CtlCommand, ParseError
from llterm.i18n import t


def _gen_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ctl-{stamp}-{secrets.token_hex(2)}"


def main(argv: list[str] | None = None) -> int:
    from llterm.host.loop import _ensure_utf8_stdout

    _ensure_utf8_stdout()  # ja メッセージを cp932 console でも化けさせない
    ap = argparse.ArgumentParser(prog="llterm-ctl")
    sub = ap.add_subparsers(dest="subcmd", required=True)
    em = sub.add_parser("emit", help="制御コマンドを投函する")
    em.add_argument("action")
    em.add_argument("--reason", required=False, default="")
    em.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    em.add_argument("--constraint", action="append", default=[])
    em.add_argument("--requires-human", action="store_true")
    em.add_argument("--root", default=".llterm", help="制御ディレクトリ (既定 ./.llterm)")

    try:
        ns = ap.parse_args(argv)
    except SystemExit:
        return 2

    if ns.action not in ALLOWED_ACTIONS:
        print(t("cli.emit.unknown_action", action=ns.action, allowed=", ".join(ALLOWED_ACTIONS)),
              file=sys.stderr)
        return 2
    if not ns.reason:
        print(t("cli.emit.reason_required"), file=sys.stderr)
        return 2

    args: dict[str, str] = {}
    for kv in ns.arg:
        if "=" not in kv:
            print(t("cli.emit.bad_arg", value=kv), file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        args[k] = v

    cmd = CtlCommand(
        id=_gen_id(), action=ns.action, reason=ns.reason, args=args,
        constraints=tuple(ns.constraint), requires_human=ns.requires_human,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        path = CtlQueue(Path(ns.root)).submit(cmd)
    except (FileExistsError, ParseError, OSError) as e:
        print(t("cli.emit.submit_failed", error=e), file=sys.stderr)
        return 2
    print(f"submitted {cmd.id} -> {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
