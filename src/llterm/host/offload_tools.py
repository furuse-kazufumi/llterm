# SPDX-License-Identifier: Apache-2.0
"""計算オフロード用 CLI ツールの自動検出 + エージェントへのヒント生成。

llterm が駆動する Claude Code が、**重い計算/ビルド/実験を低スペックなローカルでなく
無料の外部サービスへ自律的に投げられる**よう、利用可能なツール (インストール+認証済み) を
検出してヒントを作る。ヒントは SessionLoop._augment 経由で作業プロンプトに付き、エージェントが
細かい指示なしにオフロード先を自覚して使う (ユーザー要望 2026-06-13)。

LLM 推論奏者 (TurnRunner) とは別レイヤ。ここは「作業そのものを外部計算資源に投げる道具」。
検出は cheap (which + 認証ファイル/env の有無) に留め、毎 start で走らせても軽い。
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OffloadTool:
    """オフロード用 CLI 1 種 (検出関数 + エージェント向け使い方)。"""

    name: str
    detect: Callable[[], bool]
    hint: str  # エージェントへの使い方 (1 項目)


def _kaggle_available() -> bool:
    """kaggle CLI が入っていて認証済み (kaggle.json or KAGGLE_KEY+USERNAME)。"""
    if shutil.which("kaggle") is None:
        return False
    if os.environ.get("KAGGLE_KEY") and os.environ.get("KAGGLE_USERNAME"):
        return True
    return (Path.home() / ".kaggle" / "kaggle.json").is_file()


def _gh_available() -> bool:
    """gh (GitHub CLI) がインストール済み (認証は実行時にエージェントが扱う)。"""
    return shutil.which("gh") is not None


def _oci_available() -> bool:
    """OCI CLI が入っていて設定済み (~/.oci/config or OCI_CLI_* env)。Oracle Cloud Always Free。"""
    if shutil.which("oci") is None:
        return False
    if os.environ.get("OCI_CLI_USER") or os.environ.get("OCI_CLI_KEY_FILE"):
        return True
    return (Path.home() / ".oci" / "config").is_file()


# 既知のオフロード先 (検出順 = ヒント記載順)。research(2026-06-13) の Tier1 を反映。
OFFLOAD_TOOLS: tuple[OffloadTool, ...] = (
    OffloadTool(
        "kaggle", _kaggle_available,
        "kaggle: 無料GPU(P100/T4×2, 約30h/週)/TPU ノートブック。`kaggle kernels push -p <dir>` で "
        "ジョブ投入 → `kaggle kernels status <owner/slug>` でポーリング → "
        "`kaggle kernels output <owner/slug> -p <dir>` で結果取得。kernel-metadata.json に "
        "enable_gpu/enable_internet=true を忘れない。GPU 学習・ベンチ・重い数値計算向き。",
    ),
    OffloadTool(
        "gh", _gh_available,
        "gh: GitHub Actions(public repo は分数無制限・無料) を計算資源に。`gh workflow run <wf> "
        "-f k=v` で投入 → `gh run watch <id>` で監視 → `gh run download <id>` で成果物取得。"
        "長時間バッチ/テスト/スクレイピング/cron 向き(private は 2,000 分/月で課金注意)。"
        "中時間の対話環境が要れば `gh codespace create`(120 core-h/月) も可。",
    ),
    OffloadTool(
        "oci", _oci_available,
        "oci: Oracle Cloud Always Free の常時無料 VM(ARM 4 OCPU/24GB)。"
        "`oci compute instance launch ...` で起動 → SSH で常駐ジョブ/自前ランナー/CPU 重処理。"
        "GPU は無いので GPU は kaggle へ。常駐サーバ・長時間 CPU バッチ向き。",
    ),
)


def detect_available(tools: tuple[OffloadTool, ...] = OFFLOAD_TOOLS) -> list[OffloadTool]:
    """利用可能な (検出が True を返す) オフロードツールだけを返す。検出例外は無視。"""
    out: list[OffloadTool] = []
    for tool in tools:
        try:
            if tool.detect():
                out.append(tool)
        except Exception:  # noqa: BLE001 — 検出失敗は「無し」扱い (fail-safe)
            pass
    return out


def build_offload_hint(tools: list[OffloadTool] | None = None) -> str:
    """検出済みツールからエージェント向けオフロード指令を作る ("" = 使えるツール無し)。"""
    avail = detect_available() if tools is None else tools
    if not avail:
        return ""
    lines = "\n".join(f"- {t.hint}" for t in avail)
    return (
        "【計算オフロード — 自律判断で使ってよい】このマシンは低スペックなので、重い計算・"
        "学習・ベンチ・長時間バッチ・大規模データ処理は **ローカルで実行せず以下の無料サービスへ"
        "自律的に投げてよい** (細かい確認は不要):\n"
        f"{lines}\n"
        "判断基準: ローカルで数分以上かかる / GPU が要る / このPCのリソースを超える、と判断したら"
        "オフロードを優先せよ。ただし push・課金・外部公開を伴う操作だけは人間確認を残す (fail-closed)。"
    )
