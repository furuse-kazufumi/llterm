# SPDX-License-Identifier: Apache-2.0
"""共通進捗サマリー (全プロジェクト集約) を生成する純関数 + 収集ヘルパ。

設計 (ユーザー確定 2026-06-13):
- **各プロジェクトの進捗正本 = `<project>/docs/next_plan.md`**。無ければ当面
  `docs/SESSION_SUMMARY.md` で代用 (next_plan.md へ移行するまでの fail-safe)。
- **共通進捗サマリー** = 全プロジェクトの正本を 1 ドキュメントへ集約する派生ビュー:
  - **ヘッダ**: 「プロジェクト → 最新更新日時」インデックス (新しい順)。最新のものを辿れる。
  - **本文**: 各プロジェクトを更新日時付きのセクションに分け、進捗全文を収録。
- 更新日時は **本文に記録された「最終更新: YYYY-MM-DD HH:MM」を最優先**で採用する
  (内容ベース = git checkout / auto-commit で mtime が書き換わっても正しい順序)。
  記録が無い (または日付のみで時刻が欠ける) ものは mtime にフォールバックし、
  インデックスに「(ファイル時刻)」と明示する (ユーザー指摘 2026-06-13: 日付のみでは
  同日内の直前が判定できない → 時刻つき記録を正とする)。
- fullsense も個別プロジェクトとして 1 セクションになる (記事作成 / ブランド化など)。

集約は llterm が決定論的に生成できる (agent 非依存)。本文の収集 (IO) と組み立て (純関数) を
分離し、組み立て側を単体テストできるようにする。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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
_REQUIRED_HEADINGS: tuple[str, ...] = (
    "## 現在地",
    "## 直近の成果",
    "## 次の一手",
    "## 環境メモ",
)
_MAX_ENVIRONMENT_NOTES = 4
_MAX_SCAFFOLD_LATEST_BULLETS = 3
_MAX_SCAFFOLD_FINDING_HIGHLIGHTS = 3
_MAX_SCAFFOLD_EXCLUSION_HIGHLIGHTS = 2
_MAX_SCAFFOLD_RECENT_RESULTS = (
    _MAX_SCAFFOLD_LATEST_BULLETS + _MAX_SCAFFOLD_FINDING_HIGHLIGHTS
)
_ENVIRONMENT_NOTES_OMITTED_NOTE = (
    "環境メモの追加 {count} 件は未転記 "
    f"(stale/過剰転記防止のため上限 {_MAX_ENVIRONMENT_NOTES} 件)"
)
_JST = timezone(timedelta(hours=9), name="JST")
_SESSION_FIELD_RE = re.compile(r"^-\s+\*\*(.+?)\*\*[：:]\s*(.+?)\s*$")
_SESSION_PROJECT_KEYS = ("プロジェクト", "作業プロジェクト")
_NOISY_LATEST_HEADINGS = (
    "直近の git log",
    "現在の git status",
    "直近 2 時間に変更されたファイル",
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
    format_gaps: tuple[str, ...] = ()  # 正本フォーマットで欠けている必須要素


@dataclass(frozen=True)
class SessionSummarySeed:
    """SESSION_SUMMARY から next_plan scaffold を起こすための最小メタデータ。"""

    updated_label: str | None
    project_label: str | None
    review_target_label: str | None
    branch_label: str | None
    latest_heading: str | None
    latest_bullets: tuple[str, ...]
    finding_highlights: tuple[str, ...]
    exclusion_highlights: tuple[str, ...]
    next_steps: tuple[str, ...]
    environment_notes: tuple[str, ...]
    environment_notes_omitted: int


def _non_fenced_lines(text: str) -> list[str]:
    """Markdown のコードフェンス外の行だけを返す。"""
    lines: list[str] = []
    in_fence = False
    for raw in (text or "").splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(raw)
    return lines


def _is_next_steps_heading(heading: str) -> bool:
    """`次にやるべきこと` 見出しか。suffix 注釈つきも受理する。"""
    return (
        heading == "次にやるべきこと"
        or heading.startswith("次にやるべきこと ")
        or heading.startswith("次にやるべきこと(")
        or heading.startswith("次にやるべきこと（")
        or heading == "次の具体的一手"
        or heading.startswith("次の具体的一手 ")
        or heading.startswith("次の具体的一手(")
        or heading.startswith("次の具体的一手（")
    )


def _is_environment_heading(heading: str) -> bool:
    """scaffold の環境メモへ継いでよい heading か。"""
    return (
        heading == "起動方法"
        or heading.startswith("起動方法 ")
        or heading.startswith("起動方法(")
        or heading.startswith("起動方法（")
        or heading == "環境メモ"
        or heading.startswith("環境メモ ")
        or heading.startswith("環境メモ(")
        or heading.startswith("環境メモ（")
    )


def _is_noisy_latest_heading(heading: str) -> bool:
    return any(
        heading == candidate or heading.startswith(f"{candidate} ")
        or heading.startswith(f"{candidate}(") or heading.startswith(f"{candidate}（")
        for candidate in _NOISY_LATEST_HEADINGS
    )


def _is_findings_heading(heading: str) -> bool:
    return (
        heading == "確定 Findings"
        or heading.startswith("確定 Findings ")
        or heading.startswith("確定 Findings(")
        or heading.startswith("確定 Findings（")
    )


def _is_exclusion_heading(heading: str) -> bool:
    return (
        heading == "除外済みメモ"
        or heading.startswith("除外済みメモ ")
        or heading.startswith("除外済みメモ(")
        or heading.startswith("除外済みメモ（")
    )


def _append_unique(parts: list[str], text: str, *, limit: int) -> bool:
    text = text.strip()
    if text and text not in parts and len(parts) < limit:
        parts.append(text)
        return True
    return False


def _append_environment_note(parts: list[str], text: str) -> bool:
    """環境メモを保持上限つきで扱い、上限超過ぶんを omitted として数える。"""
    text = text.strip()
    if not text:
        return False
    if _append_unique(parts, text, limit=_MAX_ENVIRONMENT_NOTES):
        return False
    return len(parts) >= _MAX_ENVIRONMENT_NOTES


def _summary_list_item(line: str) -> str | None:
    """SESSION_SUMMARY の箇条書き/番号付き列挙から本文だけを返す。"""
    match = _summary_list_item_match(line)
    return match[1] if match is not None else None


def _summary_list_item_match(line: str) -> tuple[int, str] | None:
    """SESSION_SUMMARY の list item を `(indent, text)` で返す。"""
    raw = line.rstrip()
    indent = len(raw) - len(raw.lstrip(" "))
    stripped = raw[indent:]
    if stripped.startswith(("- ", "* ")):
        item = stripped[2:].strip()
        return (indent, item) if item else None
    m = re.match(r"^\d+\.\s+(.+?)\s*$", stripped)
    if m:
        item = m.group(1).strip()
        return (indent, item) if item else None
    return None


def _line_indent(line: str) -> int:
    raw = line.rstrip()
    return len(raw) - len(raw.lstrip(" "))


def _append_summary_part(parts: list[str], text: str) -> None:
    text = text.strip()
    if text:
        parts.append(text)


def _strip_quote_prefix(text: str) -> str:
    """Markdown 引用 (`>`, `>>`, `> >`) の接頭辞を剥がす。"""
    out = text.lstrip()
    while out.startswith(">"):
        out = out[1:].lstrip()
    return out


def _normalize_next_step_item(text: str) -> str | None:
    """次アクション候補として残す本文へ正規化する。

    - `~~完了~~` だけの項目は落とす
    - `~~完了~~ ... 残: foo` のような一部完了は `foo` 側だけ救う
    - それ以外はそのまま返す
    """
    stripped = text.strip()
    if not (stripped.startswith("~~") and "~~" in stripped[2:]):
        return stripped or None
    m = re.search(r"(?:残|remaining)\s*[:：]\s*(.+)$", stripped, flags=re.IGNORECASE)
    if m:
        remain = m.group(1).strip(" -")
        return remain or None
    return None


def _collect_summary_list_item(lines: list[str], start: int) -> tuple[str | None, int]:
    """list item 1 件を継続行込みで集め、次に読む index を返す。"""
    head = _summary_list_item_match(lines[start])
    if head is None:
        return None, start + 1
    base_indent, item = head
    parts = [item]
    idx = start + 1
    pending_blank = False
    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped:
            pending_blank = True
            idx += 1
            continue
        if stripped.startswith("## "):
            break
        nested = _summary_list_item_match(raw)
        indent = _line_indent(raw)
        if nested is not None and nested[0] <= base_indent:
            break
        if indent > base_indent:
            text = nested[1] if nested is not None else stripped
            _append_summary_part(parts, text)
            idx += 1
            pending_blank = False
            continue
        if pending_blank:
            break
        break
    return " ".join(parts), idx


def parse_updated_at(text: str) -> float | None:
    """進捗本文の「最終更新: YYYY-MM-DD HH:MM JST」行を epoch 秒に解す。

    時刻まで含む記録が見つかればその epoch を、無ければ ``None`` を返す
    (呼び出し側が mtime にフォールバックする)。壊れた日付も ``None`` (fail-safe)。
    """
    # 正規の next_plan ヘッダで使う「最終更新」は文頭近くの dedicated 行だけを採用する。
    # 本文の過去ログ/引用/コードブロック中の同文字列では更新時刻と見なさない。
    for line in _non_fenced_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        if _strip_quote_prefix(stripped).startswith("## "):
            break
        candidate = _strip_quote_prefix(stripped)
        update_text = candidate
        if not update_text.startswith("最終更新"):
            field = _SESSION_FIELD_RE.match(candidate)
            if field is None or field.group(1).strip() != "最終更新":
                continue
            update_text = f"最終更新: {field.group(2).strip()}"
        m = _UPDATED_RE.match(update_text)
        if not m:
            continue
        try:
            y, mo, d, hh, mm = (int(g) for g in m.groups())
            return datetime(y, mo, d, hh, mm, tzinfo=_JST).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def progress_format_gaps(text: str) -> tuple[str, ...]:
    """共通進捗で期待する next_plan 正規フォーマットの欠落を返す。"""
    gaps: list[str] = []
    if parse_updated_at(text) is None:
        gaps.append("最終更新(YYYY-MM-DD HH:MM JST)")
    visible_headings = [line.strip() for line in _non_fenced_lines(text)]
    seen_indexes: list[int] = []
    for heading in _REQUIRED_HEADINGS:
        idx = next(
            (
                i for i, line in enumerate(visible_headings)
                if line == heading or line.startswith(f"{heading} ")
                or line.startswith(f"{heading}（") or line.startswith(f"{heading}(")
            ),
            -1,
        )
        if idx < 0:
            gaps.append(heading.removeprefix("## ").strip())
            continue
        seen_indexes.append(idx)
    if len(seen_indexes) == len(_REQUIRED_HEADINGS) and seen_indexes != sorted(seen_indexes):
        gaps.append("見出し順序")
    return tuple(gaps)


def progress_source(project_dir: Path) -> tuple[Path | None, str]:
    """プロジェクトの進捗正本パスと種別を返す。無ければ (None, "none")。"""
    for source, rel in _PROGRESS_CANDIDATES:
        cand = project_dir / rel
        if cand.is_file():
            return cand, source
    return None, "none"


def parse_session_summary_seed(text: str) -> SessionSummarySeed:
    """自動生成 SESSION_SUMMARY から scaffold 用の最小メタデータを抜く。"""
    updated_label = project_label = review_target_label = branch_label = None
    latest_heading = None
    latest_bullets: list[str] = []
    finding_highlights: list[str] = []
    exclusion_highlights: list[str] = []
    next_steps: list[str] = []
    environment_notes: list[str] = []
    environment_notes_omitted = 0
    current_section: str | None = None
    header_block_open = True
    lines = _non_fenced_lines(text)
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped.removeprefix("## ").strip()
            header_block_open = False
            if (current_section and not _is_next_steps_heading(current_section)
                    and not _is_environment_heading(current_section)
                    and not _is_findings_heading(current_section)
                    and not _is_exclusion_heading(current_section)
                    and not _is_noisy_latest_heading(current_section)
                    and latest_heading is None):
                latest_heading = current_section
            idx += 1
            continue
        if current_section is None and header_block_open and stripped.startswith("# "):
            idx += 1
            continue
        if current_section is None and header_block_open and stripped.startswith(">"):
            idx += 1
            continue
        m = (
            _SESSION_FIELD_RE.match(stripped)
            if current_section is None and header_block_open
            else None
        )
        if m is not None:
            key, value = m.groups()
            if key == "最終更新":
                updated_label = value
            elif key in _SESSION_PROJECT_KEYS:
                project_label = value.strip("`")
            elif key == "実レビュー対象":
                review_target_label = value.strip("`")
            elif key == "ブランチ":
                branch_label = value.strip("`")
            idx += 1
            continue
        if current_section is None and header_block_open and stripped:
            header_block_open = False
        bullet, next_idx = _collect_summary_list_item(lines, idx)
        if bullet is None:
            if current_section and _is_environment_heading(current_section):
                if _append_environment_note(environment_notes, stripped):
                    environment_notes_omitted += 1
            idx += 1
            continue
        if current_section and _is_next_steps_heading(current_section):
            normalized = _normalize_next_step_item(bullet)
            if len(next_steps) < 3 and normalized is not None:
                next_steps.append(normalized)
        elif current_section and _is_environment_heading(current_section):
            if _append_environment_note(environment_notes, bullet):
                environment_notes_omitted += 1
        elif (current_section and _is_findings_heading(current_section)
              and len(finding_highlights) < _MAX_SCAFFOLD_FINDING_HIGHLIGHTS):
            finding_highlights.append(bullet)
        elif (current_section and _is_exclusion_heading(current_section)
              and len(exclusion_highlights) < _MAX_SCAFFOLD_EXCLUSION_HIGHLIGHTS):
            exclusion_highlights.append(bullet)
        elif latest_heading is not None and current_section == latest_heading and len(latest_bullets) < 4:
            latest_bullets.append(bullet)
        idx = next_idx
    return SessionSummarySeed(
        updated_label, project_label, review_target_label, branch_label,
        latest_heading, tuple(latest_bullets), tuple(finding_highlights), tuple(exclusion_highlights),
        tuple(next_steps), tuple(environment_notes),
        environment_notes_omitted,
    )


def _format_jst_epoch(epoch: float) -> str:
    """epoch 秒を next_plan 用 `YYYY-MM-DD HH:MM JST` へ整形する。"""
    try:
        return datetime.fromtimestamp(epoch, tz=_JST).strftime("%Y-%m-%d %H:%M JST")
    except (OSError, OverflowError, ValueError):
        return "1970-01-01 00:00 JST"


def _normalize_summary_updated(label: str | None, *, fallback_epoch: float | None = None) -> str:
    """SESSION_SUMMARY の `YYYY-MM-DD HH:MM:SS` を next_plan 用 `... HH:MM JST` へ寄せる。"""
    if not label:
        return _format_jst_epoch(fallback_epoch or 0.0)
    m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?\s*$", label)
    if not m:
        return _format_jst_epoch(fallback_epoch or 0.0)
    date, hh, mm = m.groups()
    return f"{date} {hh}:{mm} JST"


def _normalize_optional_label(label: str | None) -> str | None:
    """unknown sentinel を落として、実値だけを返す。"""
    if label is None:
        return None
    stripped = label.strip().strip("`").strip()
    if not stripped:
        return None
    if stripped.lower() in {"unknown", "n/a", "na"}:
        return None
    if stripped in {"不明", "なし", "未設定"}:
        return None
    return stripped


def scaffold_next_plan_text(
    project_name: str,
    session_summary_text: str,
    *,
    project_path: str | None = None,
    fallback_epoch: float | None = None,
) -> str:
    """SESSION_SUMMARY をもとに next_plan 正規フォーマットの初期雛形を返す。"""
    seed = parse_session_summary_seed(session_summary_text)
    updated = _normalize_summary_updated(seed.updated_label, fallback_epoch=fallback_epoch)
    project_path = (
        project_path
        or _normalize_optional_label(seed.project_label)
        or (DEFAULT_PROJECTS_ROOT / project_name).as_posix()
    )
    review_target = _normalize_optional_label(seed.review_target_label)
    branch = _normalize_optional_label(seed.branch_label)
    summary_path = f"{project_path}/docs/SESSION_SUMMARY.md"
    latest_heading = seed.latest_heading or "直近セッション"
    latest_bullets = list(seed.latest_bullets[:_MAX_SCAFFOLD_LATEST_BULLETS])
    next_steps = list(seed.next_steps[:3])
    current_location = [
        "- `docs/SESSION_SUMMARY.md` 代用から `next_plan.md` へ移行した初期状態。",
        f"- 直近の自動要約では project path は `{project_path}`。",
    ]
    if branch:
        current_location.append(f"- 直近の自動要約では branch は `{branch}`。")
    if review_target:
        current_location.append(f"- 実レビュー対象: `{review_target}`")
    for exclusion in seed.exclusion_highlights:
        current_location.append(f"- 除外済みメモ: {exclusion}")
    if seed.latest_heading:
        current_location.append(f"- 最新セクション見出し: `{latest_heading}`")
    recent_results = list(latest_bullets)
    for finding in seed.finding_highlights:
        if finding not in recent_results and len(recent_results) < _MAX_SCAFFOLD_RECENT_RESULTS:
            recent_results.append(finding)
    if not recent_results:
        recent_results = [
            "自動生成 `SESSION_SUMMARY` の最新スナップショットを引き継いだ。",
            f"元ファイル: `{summary_path}`",
        ]
    next_step_lines = next_steps or [
        "最新の git status / 作業意図 / 未完了タスクを確認し、このファイルへ人間可読な要点を移す。",
        "`SESSION_SUMMARY.md` の自動更新に依存していた再開手順を、この `next_plan.md` 正本ベースへ切り替える。",
    ]
    environment_lines = list(seed.environment_notes[:_MAX_ENVIRONMENT_NOTES])
    if seed.environment_notes_omitted:
        environment_lines.append(
            _ENVIRONMENT_NOTES_OMITTED_NOTE.format(count=seed.environment_notes_omitted)
        )
    environment_lines += [
        f"初期 scaffold 元: `{summary_path}`",
        "このファイルは機械監査対象。`最終更新` と 4 見出しを維持する。",
    ]
    if branch:
        environment_lines.insert(-1, f"直近 branch: `{branch}`")
    return (
        f"# next_plan (正本) — {project_name}\n\n"
        f"> 最終更新: {updated}\n"
        "> SESSION_SUMMARY.md 代用から移行した初期 scaffold。以後の再開・更新はこのファイルを正本にする。\n\n"
        "## 現在地\n\n"
        + "\n".join(current_location) + "\n\n"
        + "## 直近の成果\n\n"
        + "\n".join(f"- {line}" for line in recent_results) + "\n\n"
        + "## 次の一手\n\n"
        + "\n".join(f"- {line}" for line in next_step_lines) + "\n\n"
        +
        "## 環境メモ\n\n"
        + "\n".join(f"- {line}" for line in environment_lines) + "\n"
    )


def scaffold_next_plan_for_project(project_dir: Path, *, overwrite: bool = False) -> Path | None:
    """SESSION_SUMMARY 代用 project に next_plan scaffold を作る。既存 next_plan は既定で保護する。"""
    next_plan = project_dir / "docs" / "next_plan.md"
    session_summary = project_dir / "docs" / "SESSION_SUMMARY.md"
    if next_plan.exists() and not overwrite:
        return None
    if not session_summary.is_file():
        return None
    try:
        text = session_summary.read_text(encoding="utf-8", errors="replace")
        fallback_epoch = session_summary.stat().st_mtime
        next_plan.parent.mkdir(parents=True, exist_ok=True)
        next_plan.write_text(
            scaffold_next_plan_text(
                project_dir.name,
                text,
                project_path=project_dir.as_posix(),
                fallback_epoch=fallback_epoch,
            ),
            encoding="utf-8",
        )
    except OSError:
        return None
    return next_plan


def scaffold_missing_next_plans(
    projects_root: Path,
    *,
    names: set[str] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """projects_root 配下で SESSION_SUMMARY 代用 project の next_plan scaffold を作る。"""
    created: list[Path] = []
    for item in collect_progress(projects_root):
        if item.source != "session_summary":
            continue
        if names and item.name not in names:
            continue
        out = scaffold_next_plan_for_project(item.path.parent.parent, overwrite=overwrite)
        if out is not None:
            created.append(out)
    return created


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
            mtime = path.stat().st_mtime
        except OSError:
            continue
        # 並び順の正 = 本文に記録された最終更新時刻 (内容ベース = git checkout や
        # auto-commit で mtime が書き換わっても正しい)。無ければ mtime にフォールバック。
        header = parse_updated_at(text)
        if header is not None:
            updated, updated_source = header, "header"
        else:
            updated, updated_source = mtime, "mtime"
        items.append(ProjectProgress(
            d.name, path, text, updated, source, mtime, updated_source,
            progress_format_gaps(text) if source == "next_plan" else (),
        ))
    return items


def _default_fmt(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch, tz=_JST).strftime("%Y-%m-%d %H:%M")
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
    ordered = sorted(items, key=lambda p: (p.updated, p.mtime), reverse=True)

    out: list[str] = ["# 共通進捗サマリー (全プロジェクト集約)", ""]
    if not ordered:
        out.append("(進捗のあるプロジェクトがありません)")
        return "\n".join(out)

    out += ["## 最新更新インデックス (新しい順)", ""]
    for i, p in enumerate(ordered):
        suffix = "  ← 最新" if i == 0 else ""
        note = "" if p.source == "next_plan" else f"  ({p.source})"
        # 時刻が本文に記録されておらず mtime で代用したものは明示する
        # (記録時刻つきのものは信頼でき、これは「直近判定が粗い」サインになる)。
        time_note = "" if p.updated_source == "header" else "  (ファイル時刻)"
        gap_note = "" if not p.format_gaps else f"  [format: {', '.join(p.format_gaps)}]"
        out.append(f"- **{p.name}**: {fmt(p.updated)}{suffix}{note}{time_note}{gap_note}")
    out += ["", "---", ""]

    for p in ordered:
        out.append(f"## {p.name}  (更新: {fmt(p.updated)})")
        out.append("")
        if p.format_gaps:
            out.append(f"> format gaps: {', '.join(p.format_gaps)}")
            out.append("")
        out.append(p.text.strip() or "(空)")
        out += ["", "---", ""]
    return "\n".join(out).rstrip() + "\n"


def _common_summary_tmp_path(out_path: Path) -> Path:
    """共通進捗書込み用の一時パスを返す。

    固定名 ``PROGRESS.md.tmp`` は複数 writer で衝突するため、同一ディレクトリ内に
    writer ごと一意な名前を切る。
    """
    return out_path.with_name(f"{out_path.name}.{uuid4().hex}.tmp")


def _summary_file_matches(out_path: Path, text: str) -> bool:
    try:
        return out_path.read_text(encoding="utf-8") == text
    except (OSError, ValueError):
        # ValueError = UnicodeDecodeError (非 UTF-8 で外部保存された PROGRESS.md 等)。
        # fail-safe 契約 (IO 失敗を投げない) を守るため握って「不一致」扱いにする。
        return False


def _write_text_sync(path: Path, text: str) -> None:
    """UTF-8 で書き出し、flush+fsync まで行う。"""
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _fsync_parent_dir(path: Path) -> None:
    """親ディレクトリエントリの fsync を試みる (未対応 OS では fail-safe)。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_COMMON_INDEX_RE = re.compile(r"^- \*\*.+?\*\*: (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})")


def _common_summary_snapshot(items: list[ProjectProgress]) -> tuple[tuple[str, float, float], ...]:
    """共通進捗 snapshot の機械比較用 fingerprint。"""
    ordered = sorted(items, key=lambda p: (p.updated, p.mtime), reverse=True)
    return tuple((p.name, p.updated, p.mtime) for p in ordered)


def _snapshot_recency_key(
    snapshot: tuple[tuple[str, float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """snapshot の freshness 比較専用キー。

    同一性判定には project 名も必要だが、stale/fresh の前後比較は recency
    `(updated, mtime)` のみで見る。name を先頭に残すと辞書順比較になり、
    project 名の大小で stale overwrite 判定が歪む。
    """
    return tuple((updated, mtime) for _, updated, mtime in snapshot)


def _common_summary_meta_path(out_path: Path) -> Path:
    """共通進捗の sidecar metadata パス。"""
    return out_path.with_name(f".{out_path.name}.meta.json")


def _write_common_summary_meta(out_path: Path, snapshot: tuple[tuple[str, float, float], ...]) -> None:
    meta_path = _common_summary_meta_path(out_path)
    tmp_path = _common_summary_tmp_path(meta_path)
    try:
        _write_text_sync(tmp_path, json.dumps({"snapshot": snapshot}, ensure_ascii=False) + "\n")
        tmp_path.replace(meta_path)
        _fsync_parent_dir(meta_path.parent)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_common_summary_meta(out_path: Path) -> tuple[tuple[str, float, float], ...] | None:
    """sidecar metadata から snapshot fingerprint を読む。"""
    try:
        obj = json.loads(_common_summary_meta_path(out_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    raw = obj.get("snapshot")
    if not isinstance(raw, list):
        return None
    snapshot: list[tuple[str, float, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return None
        try:
            snapshot.append((str(item[0]), float(item[1]), float(item[2])))
        except (TypeError, ValueError):
            return None
    return tuple(snapshot)


def _common_summary_latest_epoch(text: str) -> float | None:
    """共通進捗本文の先頭インデックスから最新時刻を JST epoch として抜く。"""
    for line in text.splitlines():
        m = _COMMON_INDEX_RE.match(line.strip())
        if not m:
            continue
        try:
            date, hh, mm = m.groups()
            y, mo, d = (int(part) for part in date.split("-"))
            return datetime(y, mo, d, int(hh), int(mm), tzinfo=_JST).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _commit_summary_text(out_path: Path, text: str) -> bool:
    """一意 tmp を経由して summary 本文を commit する。成功時だけ True。"""
    tmp_path: Path | None = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _common_summary_tmp_path(out_path)
        _write_text_sync(tmp_path, text)
        tmp_path.replace(out_path)
        _fsync_parent_dir(out_path.parent)
        return True
    except OSError:
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_common_summary_text(
    out_path: Path,
    text: str,
    *,
    expected_snapshot: tuple[tuple[str, float, float], ...] | None = None,
    rewrite_same_epoch_stale: Callable[[float, float | None], bool] | None = None,
) -> str:
    """既に組み立て済みの共通サマリーを out_path へ best-effort で commit する。"""
    expected_epoch = _common_summary_latest_epoch(text)
    try:
        committed = _commit_summary_text(out_path, text)
        if committed and expected_snapshot is not None:
            _write_common_summary_meta(out_path, expected_snapshot)
        if not _summary_file_matches(out_path, text):
            try:
                current_text = out_path.read_text(encoding="utf-8")
            except OSError:
                current_text = ""
            current_snapshot = _read_common_summary_meta(out_path)
            current_epoch = _common_summary_latest_epoch(current_text)
            should_retry = (
                expected_snapshot is not None
                and (
                    current_snapshot is None
                    or _snapshot_recency_key(current_snapshot)
                    < _snapshot_recency_key(expected_snapshot)
                )
            )
            if (
                not should_retry
                and expected_snapshot is not None
                and current_snapshot == expected_snapshot
                and current_text != text
            ):
                should_retry = True
            if (
                not should_retry
                and expected_epoch is not None
                and current_epoch == expected_epoch
                and rewrite_same_epoch_stale is not None
            ):
                should_retry = rewrite_same_epoch_stale(expected_epoch, current_epoch)
            if should_retry:
                committed = _commit_summary_text(out_path, text)
                if committed and expected_snapshot is not None:
                    _write_common_summary_meta(out_path, expected_snapshot)
    except OSError:
        pass
    return text


def write_common_summary_items(items: list[ProjectProgress], out_path: Path) -> str:
    """事前収集済み items を 1 つの snapshot として共通サマリーへ commit する。"""
    return _write_common_summary_text(
        out_path,
        build_common_summary(items),
        expected_snapshot=_common_summary_snapshot(items),
    )


def write_common_summary(projects_root: Path, out_path: Path) -> str:
    """projects_root を集約して out_path に共通進捗サマリーを書き出し、その本文を返す。

    out_path の親ディレクトリは必要なら作成する。書込み失敗は呼び出し側に伝播させない
    (戻り値の本文は返す = GUI はファイル書込みに失敗しても表示できる)。
    """
    items = collect_progress(projects_root)
    text = build_common_summary(items)
    # CLI / hook 側は source 群を再読してもよいので、「同じ分でも stale」に限り
    # 1 回だけ再集約して単独 writer の readback ずれを補正する。
    return _write_common_summary_text(
        out_path,
        text,
        expected_snapshot=_common_summary_snapshot(items),
        rewrite_same_epoch_stale=lambda expected_epoch, current_epoch: (
            expected_epoch == current_epoch
            and build_common_summary(collect_progress(projects_root)) == text
        ),
    )


def infer_common_summary_paths(
    path: Path,
    *,
    projects_root: Path | None = None,
) -> tuple[Path, Path] | None:
    """既知の ``projects_root`` 配下で共通進捗の ``(projects_root, out_path)`` を解決する。

    想定レイアウト:
    - ``<projects_root>/<project>/docs/next_plan.md`` (または SESSION_SUMMARY.md)
    - 出力先 = ``<projects_root>/_shared/PROGRESS.md``

    ``path`` 自体は project root でもその配下でもよいが、解決は**必ず既知の
    projects_root 直下の project**に固定する。近い親を曖昧推定すると、ネストした
    subproject や途中階層の ``docs/next_plan.md`` を誤って project root とみなし、
    意図しない ``_shared/PROGRESS.md`` を静かに更新し得るため。
    """
    try:
        cur = path.resolve()
    except OSError:
        cur = path
    try:
        root = (projects_root or DEFAULT_PROJECTS_ROOT).resolve()
    except OSError:
        root = projects_root or DEFAULT_PROJECTS_ROOT
    base = cur if cur.is_dir() else cur.parent
    try:
        rel = base.relative_to(root)
    except ValueError:
        return None  # 既知の projects_root 外は更新しない (fail-closed)
    if not rel.parts:
        return None  # projects_root 自体では project を一意に定められない
    project_dir = root / rel.parts[0]  # 直下 project に固定 (途中階層は project と見なさない)
    src, _ = progress_source(project_dir)
    if src is None:
        return None
    return root, root / "_shared" / "PROGRESS.md"


def refresh_common_summary_for_project(
    path: Path,
    *,
    projects_root: Path | None = None,
) -> str | None:
    """``path`` が属する projects_root の共通進捗サマリーを再生成する。

    ``projects_root`` が与えられた場合はその配下の直下 project に限定して更新する。
    該当 project を一意に決められない場合は ``None`` を返す。
    IO 失敗は ``write_common_summary`` 側で握り潰されるため、呼び出し側は fail-safe に使える。
    """
    guessed = infer_common_summary_paths(path, projects_root=projects_root)
    if guessed is None:
        return None
    projects_root, out_path = guessed
    return write_common_summary(projects_root, out_path)


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
    parser.add_argument(
        "--scaffold-next-plan",
        nargs="*",
        metavar="PROJECT",
        help="SESSION_SUMMARY 代用 project に next_plan scaffold を作る (`all` で全件)。既存 next_plan は上書きしない",
    )
    args = parser.parse_args(argv)

    root = Path(args.projects_root)
    if not root.is_dir():
        print(f"error: projects-root が存在しません: {root}", flush=True)
        return 2
    if args.scaffold_next_plan is not None:
        if not args.scaffold_next_plan:
            print("error: --scaffold-next-plan は PROJECT... または all を要求します", flush=True)
            return 2
        names = None
        if args.scaffold_next_plan and "all" not in args.scaffold_next_plan:
            names = set(args.scaffold_next_plan)
        created = scaffold_missing_next_plans(root, names=names)
        if args.stdout:
            for path in created:
                print(path, flush=True)
        else:
            print(f"scaffolded {len(created)} next_plan.md", flush=True)
        return 0
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
