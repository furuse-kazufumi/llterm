# SPDX-License-Identifier: Apache-2.0
"""機能ごとの自走タスクテンプレ集 (registry)。

GUI のテンプレ選択 / CLI の ``--template`` で選ぶと、その機能向けの resume/continue prompt を
ループに与える。``label`` はコンボボックス表示、``description`` はツールチップ(用途説明)に使う。
新テンプレは TEMPLATES に 1 エントリ足すだけ。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from llterm.rad import build_expand_prompt, expand_continue_prompt

RAPTOR_PY = Path("D:/tools/raptor/raptor.py")


@dataclass(frozen=True)
class Template:
    key: str
    label: str  # コンボボックス表示名
    description: str  # ツールチップ(用途)
    needs_param: bool = False
    param_label: str = ""
    builder: Callable[[str], dict] | None = None

    def build(self, param: str = "") -> dict:
        """ループ上書き (resume_prompt / continue_prompt 等) を返す。"""
        return self.builder(param.strip()) if self.builder is not None else {}


def _general(_param: str) -> dict:
    return {}


def _rad_expand(param: str) -> dict:
    domain = param or "<分野名>"
    return {
        "resume_prompt": build_expand_prompt(domain),
        "continue_prompt": expand_continue_prompt(domain),
    }


def _green_keeper(_param: str) -> dict:
    return {
        "resume_prompt": (
            "このプロジェクトの test / lint / 型チェックを緑に保て。壊れていれば安全な範囲"
            "(ruff --fix 等の非破壊修復)で直す。push / 削除 / 不可逆操作は人間承認なしに行わない。"
            "緑になったら停止してよい。"
        ),
        "continue_prompt": "残りのチェック失敗を安全に修復し、緑を確認したら停止せよ。",
    }


def _doc_update(_param: str) -> dict:
    return {
        "resume_prompt": (
            "README / docs を現状のコードと整合するよう更新せよ。必ず実コードを確認してから書く"
            "(憶測で書かない)。push はしない。完了したら停止。"
        ),
    }


TEMPLATES: tuple[Template, ...] = (
    Template(
        "general", "汎用自走",
        "前回の続きを自律継続する既定モード。SESSION_SUMMARY / next_plan を読んで最優先タスクを進める。",
        builder=_general,
    ),
    Template(
        "rad_expand", "RAD 拡張 (staging)",
        "指定分野の RAD コーパスを取得→階層スキル化し staging に生成する。"
        "共有 live への公開は『公開』ボタン(人間ゲート)でのみ行う。",
        needs_param=True, param_label="分野名 (例: robotics)", builder=_rad_expand,
    ),
    Template(
        "green_keeper", "テスト緑維持",
        "test / lint / 型チェックを安全な範囲(非破壊修復)で緑に保つ。破壊操作はしない。",
        builder=_green_keeper,
    ),
    Template(
        "doc_update", "ドキュメント整備",
        "README / docs を実コードと整合させる。憶測で書かず必ずコードを確認する。",
        builder=_doc_update,
    ),
)

_BY_KEY = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template:
    return _BY_KEY[key]


def keys() -> list[str]:
    return [t.key for t in TEMPLATES]
