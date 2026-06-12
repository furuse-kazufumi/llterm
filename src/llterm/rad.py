# SPDX-License-Identifier: Apache-2.0
"""RAD コーパス拡張のテンプレ + 公開ゲート (HITL)。

拡張は **staging** (`D:/docs/<domain>_corpus_v2.staging/`) に書き、**live (共有 D:/docs) への
昇格は人間の明示操作のみ** (``promote()`` は GUI の確認ダイアログ / ``llterm-rad publish`` からだけ
呼ぶ)。自走ループは promote を絶対に呼ばない = 共有 RAD の上書き事故を構造的に防ぐ。

拡張ツール (raptor の invokable CLI、絶対パスで実行可):
  - ``raptor-corpus-update --sources <csv>``      既知ソース (ghsa/phrack 等) を取得
  - ``raptor-corpus2skill --source <dir> --name <domain> --out <report>``  文献→階層スキル化
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

RAD_DOCS_ROOT = Path("D:/docs")
RAPTOR_LIBEXEC = Path("D:/tools/raptor/libexec")


class RadError(RuntimeError):
    """RAD 操作の失敗 (fail-closed: 呼び出し側は live を壊さず中止)。"""


def live_dir(domain: str, docs_root: Path = RAD_DOCS_ROOT) -> Path:
    return Path(docs_root) / f"{domain}_corpus_v2"


def staging_dir(domain: str, docs_root: Path = RAD_DOCS_ROOT) -> Path:
    return Path(docs_root) / f"{domain}_corpus_v2.staging"


def build_expand_prompt(
    domain: str, *, docs_root: Path = RAD_DOCS_ROOT, libexec: Path = RAPTOR_LIBEXEC,
) -> str:
    """RAD 拡張タスクの resume_prompt。staging のみへ書き、live には触れないことを明示する。"""
    stg = staging_dir(domain, docs_root)
    live = live_dir(domain, docs_root)
    return (
        f"RAD コーパス拡張タスク。対象分野 = 「{domain}」。\n"
        f"目的: この分野の先行研究/文献を集めて階層スキル化し、**staging ディレクトリ** "
        f"`{stg}` にのみ書き出す。**live (`{live}`) には絶対に書かない/上書きしない** "
        f"(公開は人間が別途ゲートで行う)。\n"
        f"道具 (絶対パスで実行可。--help で引数確認):\n"
        f"  - 既知ソース(ghsa/phrack 等)の取得: `{libexec / 'raptor-corpus-update'}` "
        f"(--sources <csv>)\n"
        f"  - 文献→階層スキル化: `{libexec / 'raptor-corpus2skill'}` "
        f"(--source <文献dir> --name {domain} --out <report>; 出力を `{stg}` 配下へ)\n"
        f"手順: (1) 分野のクエリ/ソースを定義 (2) 文献を取得 (3) raptor-corpus2skill で "
        f"`{stg}` 配下に階層化 (4) 生成物の件数/構成を報告して停止。\n"
        f"制約: push / 削除 / live への書込は禁止。staging への生成のみ。"
    )


def expand_continue_prompt(domain: str) -> str:
    return (f"分野「{domain}」の RAD 拡張を staging に対して続行せよ。"
            f"live には書かない。完了したら生成物を報告して停止。")


@dataclass(frozen=True)
class PromoteResult:
    domain: str
    live: Path
    backup: Path | None


def _free_backup(live: Path) -> Path:
    base = live.with_name(live.name + ".bak")
    if not base.exists():
        return base
    n = 1
    while True:
        cand = live.with_name(f"{live.name}.bak{n}")
        if not cand.exists():
            return cand
        n += 1


def promote(
    domain: str, *, docs_root: Path = RAD_DOCS_ROOT, make_backup: bool = True,
) -> PromoteResult:
    """公開ゲート: staging を live へ昇格する。**人間の明示操作からのみ呼ぶこと**。

    既存 live は破棄前に必ずバックアップする (make_backup) = 上書き事故を防ぐ fail-safe。
    staging が無ければ RadError (live を壊さない)。
    """
    stg = staging_dir(domain, docs_root)
    live = live_dir(domain, docs_root)
    if not stg.is_dir():
        raise RadError(t("rad.staging_missing", staging=stg))
    backup: Path | None = None
    if live.exists():
        if make_backup:
            backup = _free_backup(live)
            live.rename(backup)
        else:
            shutil.rmtree(live)
    shutil.move(str(stg), str(live))
    return PromoteResult(domain=domain, live=live, backup=backup)


def main(argv: list[str] | None = None) -> int:
    from llterm.host.loop import _ensure_utf8_stdout

    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(prog="llterm-rad", description="RAD コーパス拡張の公開ゲート")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish", help="staging を live へ昇格 (この実行が人間の承認ゲート)")
    pub.add_argument("domain")
    pub.add_argument("--docs-root", default=str(RAD_DOCS_ROOT))
    pub.add_argument("--no-backup", action="store_true")
    pr = sub.add_parser("prompt", help="拡張テンプレ prompt を表示 (注入用)")
    pr.add_argument("domain")
    args = parser.parse_args(argv)

    if args.cmd == "publish":
        try:
            res = promote(args.domain, docs_root=Path(args.docs_root), make_backup=not args.no_backup)
        except RadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        msg = f"published: {res.live}"
        if res.backup:
            msg += f" (backup: {res.backup})"
        print(msg)
        return 0
    if args.cmd == "prompt":
        print(build_expand_prompt(args.domain))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
