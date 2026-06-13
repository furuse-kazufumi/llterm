# SPDX-License-Identifier: Apache-2.0
"""共通進捗サマリー (全プロジェクト集約) を生成する純関数 + 収集ヘルパ。

設計 (ユーザー確定 2026-06-13):
- **各プロジェクトの進捗正本 = `<project>/docs/next_plan.md`**。無ければ当面
  `docs/SESSION_SUMMARY.md` で代用 (next_plan.md へ移行するまでの fail-safe)。
- **共通進捗サマリー** = 全プロジェクトの正本を 1 ドキュメントへ集約する派生ビュー:
  - **ヘッダ**: 「プロジェクト → 最新更新日時」インデックス (新しい順)。最新のものを辿れる。
  - **本文**: 各プロジェクトを更新日時付きのセクションに分け、進捗全文を収録。
- 更新日時は正本ファイルの mtime (自動・確実。agent の書き忘れに影響されない)。
- fullsense も個別プロジェクトとして 1 セクションになる (記事作成 / ブランド化など)。

集約は llterm が決定論的に生成できる (agent 非依存)。本文の収集 (IO) と組み立て (純関数) を
分離し、組み立て側を単体テストできるようにする。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 正本の優先順 (docs/ 配下)。next_plan.md を最優先、無ければ揮発 SESSION_SUMMARY.md で代用。
_PROGRESS_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("next_plan", "docs/next_plan.md"),
    ("session_summary", "docs/SESSION_SUMMARY.md"),
)

# 本文に手書きされた「最終更新」行から記録時刻を拾う正規表現。
# 例: ``> 最終更新: 2026-06-13 15:42 JST`` / ``最終更新：2026-06-13 15:42``。
# **時刻 (HH:MM) を含むときだけ**採用する。日付のみ (`2026-06-13`) は同日内の前後を
# 判定できず mtime より粗いため、あえて拾わず mtime フォールバックに委ねる
# (ユーザー指摘 2026-06-13: 「日付までだと直前のものが判断できない」)。
_UPDATED_RE = re.compile(
    r"最終更新[^\n0-9]*?(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})"
)


@dataclass(frozen=True)
class ProjectProgress:
    """1 プロジェクトの進捗正本のスナップショット。"""

    name: str               # プロジェクト名 (ディレクトリ名)
    path: Path              # 読んだ正本ファイル
    text: str               # 進捗全文
    updated: float          # 並び順の正 = 本文記録の最終更新時刻 (無ければ mtime)。epoch 秒
    source: str             # "next_plan" | "session_summary"
    mtime: float = 0.0      # 正本ファイルの mtime (透明性のため別途保持)。epoch 秒
    updated_source: str = "mtime"  # updated の出所: "header" (本文に記録) | "mtime" (フォールバック)


def parse_updated_at(text: str) -> float | None:
    """進捗本文の「最終更新: YYYY-MM-DD HH:MM」行を epoch 秒 (ローカル) に解す。

    時刻まで含む記録が見つかればその epoch を、無ければ ``None`` を返す
    (呼び出し側が mtime にフォールバックする)。壊れた日付も ``None`` (fail-safe)。
    """
    m = _UPDATED_RE.search(text or "")
    if not m:
        return None
    try:
        y, mo, d, hh, mm = (int(g) for g in m.groups())
        return datetime(y, mo, d, hh, mm).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def progress_source(project_dir: Path) -> tuple[Path | None, str]:
    """プロジェクトの進捗正本パスと種別を返す。無ければ (None, "none")。"""
    for source, rel in _PROGRESS_CANDIDATES:
        cand = project_dir / rel
        if cand.is_file():
            return cand, source
    return None, "none"


def collect_progress(projects_root: Path) -> list[ProjectProgress]:
    """projects_root 直下の各プロジェクトの進捗正本を収集する (mtime 付き)。

    進捗ファイルを持たないディレクトリはスキップ (fail-safe)。読めない物も無視する。
    """
    items: list[ProjectProgress] = []
    try:
        children = sorted(projects_root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return items
    for d in children:
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue  # 隠し / 集約用 (_shared 等) は除外
        path, source = progress_source(d)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            updated = path.stat().st_mtime
        except OSError:
            continue
        items.append(ProjectProgress(d.name, path, text, updated, source))
    return items


def _default_fmt(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "?"


def build_common_summary(
    items: list[ProjectProgress],
    *,
    fmt: Callable[[float], str] = _default_fmt,
) -> str:
    """各プロジェクトの進捗を集約した共通進捗サマリー文字列を組み立てる (純関数)。

    - ヘッダ: 「プロジェクト → 最新更新日時」インデックス (新しい順、先頭に ← 最新)。
    - 本文: 各プロジェクトを更新日時付きセクションで全文収録 (新しい順)。
    空なら見出しのみ返す (GUI 側で placeholder にできる)。
    """
    ordered = sorted(items, key=lambda p: p.updated, reverse=True)

    out: list[str] = ["# 共通進捗サマリー (全プロジェクト集約)", ""]
    if not ordered:
        out.append("(進捗のあるプロジェクトがありません)")
        return "\n".join(out)

    out += ["## 最新更新インデックス (新しい順)", ""]
    for i, p in enumerate(ordered):
        suffix = "  ← 最新" if i == 0 else ""
        note = "" if p.source == "next_plan" else f"  ({p.source})"
        out.append(f"- **{p.name}**: {fmt(p.updated)}{suffix}{note}")
    out += ["", "---", ""]

    for p in ordered:
        out.append(f"## {p.name}  (更新: {fmt(p.updated)})")
        out.append("")
        out.append(p.text.strip() or "(空)")
        out += ["", "---", ""]
    return "\n".join(out).rstrip() + "\n"


def write_common_summary(projects_root: Path, out_path: Path) -> str:
    """projects_root を集約して out_path に共通進捗サマリーを書き出し、その本文を返す。

    out_path の親ディレクトリは必要なら作成する。書込み失敗は呼び出し側に伝播させない
    (戻り値の本文は返す = GUI はファイル書込みに失敗しても表示できる)。
    """
    text = build_common_summary(collect_progress(projects_root))
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


# 既定の集約ルートと出力先 (app.py の DEFAULT_PROJECTS_ROOT と一致させる)。
DEFAULT_PROJECTS_ROOT = Path("D:/projects")
DEFAULT_OUT = DEFAULT_PROJECTS_ROOT / "_shared" / "PROGRESS.md"


def main(argv: list[str] | None = None) -> int:
    """共通進捗サマリーを生成する CLI (スクリプトで自動更新するための入口)。

    例: ``llterm-progress`` → ``D:/projects/_shared/PROGRESS.md`` を再生成。
    ``llterm-progress --stdout`` → 書かずに標準出力へ (プレビュー用)。
    """
    import argparse

    from llterm.host.loop import _ensure_utf8_stdout  # cp932 でも日本語/記号を化けさせない

    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="llterm-progress",
        description="全プロジェクトの docs/next_plan.md を集約した共通進捗サマリーを生成する",
    )
    parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT),
                        help="集約するプロジェクトの親ディレクトリ (既定 %(default)s)")
    parser.add_argument("--out", default=None,
                        help="出力先 (既定 <projects-root>/_shared/PROGRESS.md)")
    parser.add_argument("--stdout", action="store_true",
                        help="ファイルに書かず標準出力へ出す (プレビュー)")
    args = parser.parse_args(argv)

    root = Path(args.projects_root)
    if not root.is_dir():
        print(f"error: projects-root が存在しません: {root}", flush=True)
        return 2
    if args.stdout:
        print(build_common_summary(collect_progress(root)), flush=True)
        return 0
    out = Path(args.out) if args.out else root / "_shared" / "PROGRESS.md"
    write_common_summary(root, out)
    n = len(collect_progress(root))
    print(f"共通進捗サマリー更新: {out} ({n} プロジェクト)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
