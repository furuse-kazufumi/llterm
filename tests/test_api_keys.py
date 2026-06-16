# SPDX-License-Identifier: Apache-2.0
"""api-keys.json → os.environ ローダ (fail-safe) の回帰テスト。

os.environ を汚さないよう monkeypatch.setenv/delenv で隔離する。実ファイル
(D:/api-keys.json) には触れず、tmp_path に書いた JSON を LLTERM_API_KEYS_FILE 経由で指す。
"""
from __future__ import annotations

import json
import pathlib

from llterm.host.api_keys import (
    API_KEYS_FILE_ENV,
    DEFAULT_API_KEYS_FILE,
    load_api_keys_into_env,
)


def _write_json(path: pathlib.Path, obj: object) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_loads_str_values_into_env(tmp_path, monkeypatch) -> None:
    """JSON 最上位の str 値が os.environ へ setdefault される。"""
    keys_file = tmp_path / "api-keys.json"
    _write_json(keys_file, {"GEMINI_API_KEY": "g-key", "GROQ_API_KEY": "q-key"})
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(keys_file))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    loaded = load_api_keys_into_env()

    assert set(loaded) == {"GEMINI_API_KEY", "GROQ_API_KEY"}
    import os
    assert os.environ["GEMINI_API_KEY"] == "g-key"
    assert os.environ["GROQ_API_KEY"] == "q-key"


def test_does_not_overwrite_existing_env(tmp_path, monkeypatch) -> None:
    """既存 env は上書きしない (明示設定が勝つ = setdefault)。"""
    keys_file = tmp_path / "api-keys.json"
    _write_json(keys_file, {"GEMINI_API_KEY": "from-file"})
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(keys_file))
    monkeypatch.setenv("GEMINI_API_KEY", "from-shell")  # 既に明示設定済み

    loaded = load_api_keys_into_env()

    import os
    assert os.environ["GEMINI_API_KEY"] == "from-shell"  # 上書きされない
    assert "GEMINI_API_KEY" in loaded  # 反映を試みたキーには含まれる


def test_missing_file_returns_empty_failsafe(tmp_path, monkeypatch) -> None:
    """ファイル非存在では例外を投げず空リスト (fail-safe)。"""
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(missing))
    assert load_api_keys_into_env() == []


def test_invalid_json_returns_empty_failsafe(tmp_path, monkeypatch) -> None:
    """JSON パース失敗でも例外を投げず空リスト (fail-safe)。"""
    keys_file = tmp_path / "api-keys.json"
    keys_file.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(keys_file))
    assert load_api_keys_into_env() == []


def test_skips_non_str_values(tmp_path, monkeypatch) -> None:
    """dict/list/数値/None など非 str 値はスキップする (env に載せられない)。"""
    keys_file = tmp_path / "api-keys.json"
    _write_json(keys_file, {
        "GEMINI_API_KEY": "g-key",      # str → 載る
        "agent_email_allowed_senders": ["a@x.com"],  # list → スキップ
        "nested": {"k": "v"},           # dict → スキップ
        "count": 42,                    # int → スキップ
        "flag": True,                   # bool → スキップ (str ではない)
        "none_val": None,               # None → スキップ
    })
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(keys_file))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    loaded = load_api_keys_into_env()

    assert loaded == ["GEMINI_API_KEY"]
    import os
    assert os.environ["GEMINI_API_KEY"] == "g-key"
    assert "agent_email_allowed_senders" not in os.environ
    assert "nested" not in os.environ
    assert "count" not in os.environ
    assert "flag" not in os.environ
    assert "none_val" not in os.environ


def test_explicit_path_arg_beats_env(tmp_path, monkeypatch) -> None:
    """引数 path は env LLTERM_API_KEYS_FILE より優先される。"""
    arg_file = tmp_path / "arg.json"
    env_file = tmp_path / "env.json"
    _write_json(arg_file, {"FROM_ARG": "arg-val"})
    _write_json(env_file, {"FROM_ENV": "env-val"})
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(env_file))
    monkeypatch.delenv("FROM_ARG", raising=False)
    monkeypatch.delenv("FROM_ENV", raising=False)

    loaded = load_api_keys_into_env(arg_file)

    assert loaded == ["FROM_ARG"]
    import os
    assert os.environ.get("FROM_ARG") == "arg-val"
    assert "FROM_ENV" not in os.environ


def test_non_dict_toplevel_returns_empty(tmp_path, monkeypatch) -> None:
    """JSON 最上位が dict でない (list 等) なら空リスト (fail-safe)。"""
    keys_file = tmp_path / "api-keys.json"
    _write_json(keys_file, ["not", "a", "dict"])
    monkeypatch.setenv(API_KEYS_FILE_ENV, str(keys_file))
    assert load_api_keys_into_env() == []


def test_default_path_constant() -> None:
    """既定パスは D:/api-keys.json (ユーザー鍵束)。"""
    assert DEFAULT_API_KEYS_FILE == pathlib.Path("D:/api-keys.json")
