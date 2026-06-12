# SPDX-License-Identifier: Apache-2.0
"""i18n モジュール (llterm.i18n) の unit test — locale 解決 / fallback / format / fail-safe。

conftest がプロセス全体に ``LLTERM_LANG=ja`` を固定するため、各テストは monkeypatch で
env を切り替えて検証する (テスト後は自動復元)。
"""
from __future__ import annotations

import re

import pytest

from llterm import i18n
from llterm.i18n import DEFAULT_LOCALE, MESSAGES, SUPPORTED_LOCALES, resolve_locale, t

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)")


@pytest.fixture()
def _no_os_locale(monkeypatch: pytest.MonkeyPatch):
    """OS locale 検出を「検出不能」に固定する (既定 fallback の検証用)。"""
    monkeypatch.setattr(i18n, "_os_locale_cache", None)  # キャッシュ済み=None (検出不能)
    yield


# ─── locale 解決 ───────────────────────────────────────────────────


def test_env_var_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert resolve_locale() == "en"
    monkeypatch.setenv("LLTERM_LANG", "ja")
    assert resolve_locale() == "ja"


def test_env_var_with_region_and_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "en_US.UTF-8")
    assert resolve_locale() == "en"
    monkeypatch.setenv("LLTERM_LANG", "ja-JP")
    assert resolve_locale() == "ja"


def test_env_var_windows_style_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "Japanese_Japan.932")
    assert resolve_locale() == "ja"
    monkeypatch.setenv("LLTERM_LANG", "English_United States.1252")
    assert resolve_locale() == "en"


def test_unsupported_env_falls_through_to_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """未対応の LLTERM_LANG は無視され OS 検出に進む (fail-safe)。"""
    monkeypatch.setenv("LLTERM_LANG", "fr")
    monkeypatch.setattr(i18n, "_os_locale_cache", "en")  # OS 検出 = en と仮定
    assert resolve_locale() == "en"


def test_default_is_ja_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch, _no_os_locale: None
) -> None:
    monkeypatch.delenv("LLTERM_LANG", raising=False)
    assert resolve_locale() == DEFAULT_LOCALE == "ja"


def test_os_locale_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLTERM_LANG", raising=False)
    monkeypatch.setattr(i18n, "_os_locale_cache", "en")
    assert resolve_locale() == "en"


def test_detect_os_locale_reads_lang_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """locale.getlocale() が空でも LANG 系 env から検出できる。"""
    monkeypatch.setattr(i18n._locale_mod, "getlocale", lambda: (None, None))
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert i18n._detect_os_locale() == "en"


def test_normalize_locale_rejects_garbage() -> None:
    assert i18n._normalize_locale("") is None
    assert i18n._normalize_locale("xx_YY") is None
    assert i18n._normalize_locale("12345") is None


# ─── t(): 取得 / fallback / format / fail-safe ─────────────────────


def test_t_returns_locale_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "ja")
    assert t("gui.btn.refresh") == "↻ 更新"
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert t("gui.btn.refresh") == "↻ Refresh"


def test_t_unknown_key_returns_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "ja")
    assert t("no.such.key") == "no.such.key"
    assert t("") == ""


def test_t_missing_translation_falls_back_to_ja(monkeypatch: pytest.MonkeyPatch) -> None:
    """対象 locale の訳が無いエントリは ja へ fallback する。"""
    monkeypatch.setitem(MESSAGES, "test.ja_only", {"ja": "日本語のみ"})
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert t("test.ja_only") == "日本語のみ"


def test_t_empty_entry_returns_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(MESSAGES, "test.empty", {})
    assert t("test.empty") == "test.empty"


def test_t_formats_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLTERM_LANG", "ja")
    assert t("gui.msg.task_sent", turn=3) == "▶ 指令送信 (turn 3)"
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert t("gui.msg.task_sent", turn=3) == "▶ Instruction sent (turn 3)"


def test_t_format_failure_returns_unformatted(monkeypatch: pytest.MonkeyPatch) -> None:
    """placeholder 不一致でも例外を出さず未整形テンプレートを返す (fail-safe)。"""
    monkeypatch.setenv("LLTERM_LANG", "ja")
    out = t("gui.msg.task_sent", wrong_name=1)  # {turn} を渡さない
    assert out == "▶ 指令送信 (turn {turn})"


def test_t_format_with_conversion_and_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """{value!r} や {ctx:,} のような変換/書式指定つき placeholder も整形できる。"""
    monkeypatch.setenv("LLTERM_LANG", "en")
    assert "'x=1'" in t("cli.emit.bad_arg", value="x=1")
    assert "12,345" in t("virtual.turn_text", prompt="p", sid="s", ctx=12345, n=1)


def test_t_never_raises_on_hostile_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """i18n 起因で例外を外へ出さない契約 — 壊れたエントリでも文字列が返る。"""
    monkeypatch.setitem(MESSAGES, "test.broken", {"ja": None, "en": 123})  # type: ignore[dict-item]
    assert isinstance(t("test.broken"), str)
    monkeypatch.setitem(MESSAGES, "test.badtmpl", {"ja": "{0[}"})
    assert isinstance(t("test.badtmpl", x=1), str)


# ─── テーブルの完備性 (ja/en) と placeholder parity ─────────────────


def test_all_messages_have_ja_and_en() -> None:
    """ja / en は全 key で完備 (zh/ko は将来追加)。"""
    missing = [
        f"{key}:{loc}"
        for key, entry in MESSAGES.items()
        for loc in ("ja", "en")
        if not isinstance(entry.get(loc), str) or not entry[loc]
    ]
    assert missing == []


def test_placeholder_parity_between_locales() -> None:
    """ja と en で placeholder 集合が一致する (片方だけ {x} を持つ事故を防ぐ)。"""
    mismatched = []
    for key, entry in MESSAGES.items():
        sets = {loc: set(_PLACEHOLDER_RE.findall(entry.get(loc, ""))) for loc in ("ja", "en")}
        if sets["ja"] != sets["en"]:
            mismatched.append((key, sets))
    assert mismatched == []


def test_supported_locales_shape() -> None:
    assert DEFAULT_LOCALE in SUPPORTED_LOCALES
    assert "en" in SUPPORTED_LOCALES
