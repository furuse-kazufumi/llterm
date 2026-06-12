# SPDX-License-Identifier: Apache-2.0
"""機能ごとの自走タスクテンプレ集 (registry)。

GUI のテンプレ選択 / CLI の ``--template`` で選ぶと、その機能向けの resume/continue prompt を
ループに与える。``label`` はコンボボックス表示、``description`` はツールチップ(用途説明)に使う。
新テンプレは TEMPLATES に 1 エントリ足すだけ (表示文字列は llterm.i18n の MESSAGES に追加)。

注: builder が返す resume/continue prompt は **Claude への指示文**であり、ユーザー向け
表示ではないため i18n 対象外 (表示 locale で挙動を変えない)。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from llterm.i18n import t
from llterm.rad import build_expand_prompt, expand_continue_prompt

RAPTOR_PY = Path("D:/tools/raptor/raptor.py")


@dataclass(frozen=True)
class Template:
    key: str
    label_key: str  # コンボボックス表示名の i18n key
    description_key: str  # ツールチップ(用途)の i18n key
    needs_param: bool = False
    param_label_key: str = ""  # 引数欄 placeholder の i18n key ("" = 引数なし)
    builder: Callable[[str], dict] | None = None
    # token 節約ルーティングの既定プロバイダ。"codex" = 機械的/長時間タスクなので Codex
    # (ChatGPT Pro サブスク = Claude トークン非消費) を主に寄せる。"" = 既定 (Claude 主)。
    # GUI の「Codex 優先」トグルが ON ならテンプレ既定に依らず常に Codex 主。
    prefer: str = ""

    @property
    def label(self) -> str:
        """コンボボックス表示名 (現 locale で解決)。"""
        return t(self.label_key)

    @property
    def description(self) -> str:
        """ツールチップ(用途説明) (現 locale で解決)。"""
        return t(self.description_key)

    @property
    def param_label(self) -> str:
        """引数欄の placeholder (現 locale で解決。引数なしテンプレは空文字)。"""
        return t(self.param_label_key) if self.param_label_key else ""

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


def _security_audit(_param: str) -> dict:
    scan_cmd = f"py -3.11 {RAPTOR_PY} scan --repo <このプロジェクトの絶対パス> --policy_groups secrets,owasp"
    help_cmd = f"py -3.11 {RAPTOR_PY} help scan"
    return {
        "resume_prompt": (
            "セキュリティ監査タスク(read-only)。対象 = このプロジェクト(現在の作業ディレクトリ)。\n"
            f"1) raptor の SAST スキャン(/scan 連携)を実行: `{scan_cmd}` "
            f"(オプションは `{help_cmd}` で確認。Semgrep を内部で使い SARIF/findings を出力する)。\n"
            "2) 出力された findings を読み、重大度・真偽(false positive 判定)・到達可能性/悪用可能性で triage する。\n"
            "3) `docs/SECURITY_AUDIT.md` に監査レポート(サマリ + 各 finding + 推奨修正)を書く。\n"
            "制約: **監査は read-only**。修正/パッチ適用・push・削除はしない(remediation は人間が判断)。"
            "レポートを書き終えたら停止。"
        ),
        "continue_prompt": (
            "残りの findings を triage し docs/SECURITY_AUDIT.md を完成させて停止。修正/push はしない。"
        ),
    }


TEMPLATES: tuple[Template, ...] = (
    Template(
        "general", "template.general.label", "template.general.description",
        builder=_general,
    ),
    Template(
        "rad_expand", "template.rad_expand.label", "template.rad_expand.description",
        needs_param=True, param_label_key="template.rad_expand.param_label",
        builder=_rad_expand, prefer="codex",  # 文献取得→corpus 化の機械的長時間タスク
    ),
    Template(
        "green_keeper", "template.green_keeper.label", "template.green_keeper.description",
        builder=_green_keeper, prefer="codex",  # test/lint/型 を緑に保つ機械的反復
    ),
    Template(
        "doc_update", "template.doc_update.label", "template.doc_update.description",
        builder=_doc_update, prefer="codex",  # docs 整合の機械的更新
    ),
    Template(
        "security_audit", "template.security_audit.label",
        "template.security_audit.description",
        builder=_security_audit, prefer="codex",  # read-only スキャン+triage の機械的作業
    ),
)

_BY_KEY = {t.key: t for t in TEMPLATES}


def get(key: str) -> Template:
    return _BY_KEY[key]


def keys() -> list[str]:
    return [t.key for t in TEMPLATES]
