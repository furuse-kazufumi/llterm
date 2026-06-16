# SPDX-License-Identifier: Apache-2.0
"""ローカルの API キー JSON を ``os.environ`` へ読み込む fail-safe ローダ。

llterm のオーケストラ奏者 (Groq / Cerebras / OpenRouter / Gemini API / Perplexity)
は API キーを **環境変数**から取る (:class:`~llterm.host.openai_compat_runner.OpenAICompatRunner`
の ``key_available()`` 参照)。一方ユーザーの鍵束は ``D:/api-keys.json`` の 1 ファイルに
集約されている。GUI 起動時にこのローダを 1 回呼ぶことで、env に未設定でも JSON 由来の
鍵で奏者を可用化する (例: ``GEMINI_API_KEY`` 不在で gemini-api 奏者が除外される問題の解消)。

設計原則:
- **fail-safe**: ファイル非存在 / JSON パース失敗 / 読み取り例外でも **例外を投げず** 空
  リストを返す。GUI / SessionLoop を起動失敗で殺さない (llterm の fail-closed/fail-safe 方針)。
- **既存 env を上書きしない**: :func:`os.environ.setdefault` を使い、シェルや CI で
  明示設定された値が常に勝つ (ローカル鍵束はあくまで補完)。
- **str 値のみ**: JSON 最上位の文字列値だけを env へ載せる。dict / list / 数値 / None は
  env に入れられない (または構造化値) のでスキップする。
"""
from __future__ import annotations

import json
import os
import pathlib

#: 既定の API キー JSON パス (``LLTERM_API_KEYS_FILE`` env で上書き可)。
DEFAULT_API_KEYS_FILE = pathlib.Path("D:/api-keys.json")

#: パス解決に使う環境変数名。
API_KEYS_FILE_ENV = "LLTERM_API_KEYS_FILE"


def _resolve_path(path: pathlib.Path | None) -> pathlib.Path:
    """API キー JSON のパスを優先順位で解決する。

    Parameters
    ----------
    path
        明示指定パス。``None`` のとき env / 既定にフォールバックする。

    Returns
    -------
    pathlib.Path
        解決したパス。優先順: 引数 ``path`` > 環境変数 ``LLTERM_API_KEYS_FILE`` >
        既定 :data:`DEFAULT_API_KEYS_FILE`。
    """
    if path is not None:
        return path
    env_value = os.environ.get(API_KEYS_FILE_ENV, "")
    if env_value:
        return pathlib.Path(env_value)
    return DEFAULT_API_KEYS_FILE


def load_api_keys_into_env(path: pathlib.Path | None = None) -> list[str]:
    """ローカルの API キー JSON を ``os.environ`` へ ``setdefault`` で読み込む。

    JSON 最上位 (オブジェクト) の **文字列値のみ** を環境変数へ載せる。既存の環境変数は
    上書きしない (``setdefault``) ため、明示設定された値が常に勝つ。dict / list / 非 str
    値はスキップする。

    Parameters
    ----------
    path
        API キー JSON のパス。``None`` のとき env ``LLTERM_API_KEYS_FILE`` → 既定
        :data:`DEFAULT_API_KEYS_FILE` の順で解決する。

    Returns
    -------
    list[str]
        JSON から読み込んで env へ反映を試みたキー名のリスト (既に env にあって
        ``setdefault`` で上書きしなかったキーも含む)。fail-safe: ファイル非存在・JSON
        パース失敗・読み取り例外では空リストを返す (例外を投げない)。
    """
    target = _resolve_path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    loaded: list[str] = []
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue  # dict/list/数値/None は env に載せられないのでスキップ
        os.environ.setdefault(key, value)  # 既存 env を上書きしない (明示設定が勝つ)
        loaded.append(key)
    return loaded
