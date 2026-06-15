# SPDX-License-Identifier: Apache-2.0
"""GUI 設定の永続化 — 最後の設定 (プロジェクト/トグル/閾値/テンプレ/ウィンドウ位置) を
次回起動時に復元する。

保存先は既定で ``~/.llterm/gui_settings.json`` (人間が読める JSON、レジストリ不使用)。
読み書きとも fail-safe: 壊れた JSON・権限エラー・欠落キーで GUI を殺さず既定値に落ちる。
優先順位は **CLI 明示指定 > 保存値 > 組込み既定** (app.MainWindow 側で適用)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_SETTINGS_PATH = Path.home() / ".llterm" / "gui_settings.json"

# 起動時 one-shot 入力プリフィルのファイル名 (settings と同じ ~/.llterm/ 配下)。
STARTUP_INPUT_FILENAME = "startup_input.txt"


def startup_input_path(settings_path: Path) -> Path:
    """One-shot 起動時入力プリフィルのファイルパス (settings と同じディレクトリ配下)。"""
    return Path(settings_path).parent / STARTUP_INPUT_FILENAME


def write_startup_input(settings_path: Path, text: str) -> bool:
    """次回 GUI 起動時に EditBox へ流し込む one-shot 入力を書く。

    外部 (ccr 等) からの供給路: このファイルに指示文を書いておくだけで、次回 llterm 起動時に
    EditBox へ入った状態になる。原子的書き込み (tmp → replace)・fail-safe。``text`` が
    空/空白なら既存プリフィルを削除する (= 取り消し)。
    """
    path = startup_input_path(settings_path)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not text.strip():
            path.unlink(missing_ok=True)
            return True
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return True
    except (OSError, ValueError):
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def consume_startup_input(settings_path: Path) -> str:
    """起動時 one-shot 入力プリフィルを読み、読めたら即クリアする (clear-on-load)。

    外部 (ccr 等) が ``~/.llterm/startup_input.txt`` に書いておくだけで、次回 GUI 起動時に
    EditBox へ入った状態になり、**消費後は消えて再発火しない** (1 回だけ)。読めない/空白のみ
    なら ``""`` を返す (fail-safe: 壊れていても GUI を殺さない)。
    """
    path = startup_input_path(settings_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        path.unlink(missing_ok=True)  # 消費 (clear-on-load): 同じプリフィルを次回再発火させない
    except OSError:
        pass
    return text if text.strip() else ""


def load_settings(path: Path) -> dict:
    """設定 JSON を読む。無い・壊れている場合は空 dict (既定値で起動)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path: Path, data: dict) -> bool:
    """設定 JSON を原子的に書く (tmp → replace)。失敗しても GUI を殺さない。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # tmp 名は PID でユニーク化: 複数インスタンス同時保存での replace 衝突 / .tmp 残留を避ける
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
        return True
    except (OSError, TypeError, ValueError):
        try:
            if tmp.exists():  # replace 失敗時に中間ファイルを残さない
                tmp.unlink()
        except (OSError, NameError):
            pass
        return False
