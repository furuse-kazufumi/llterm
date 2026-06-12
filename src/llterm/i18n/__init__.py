# SPDX-License-Identifier: Apache-2.0
"""llterm i18n — 軽量テーブル方式の多言語対応 (gettext 不使用)。

メッセージは :mod:`llterm.i18n.messages` の ``MESSAGES`` (key → {locale: text}) に
集約し、:func:`t` で参照する::

    from llterm.i18n import t

    print(t("gui.msg.promote_failed", error=exc))

locale 解決 (優先順):
1. 環境変数 ``LLTERM_LANG`` (例: ``ja`` / ``en`` / ``en_US.UTF-8``。未対応値は無視)
2. OS locale の自動検出 (``locale.getlocale()`` / ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG``)
3. 既定 ``"ja"``

fail-safe 契約 (i18n 起因で**絶対に例外を外へ出さない**):
- 未知 key → key をそのまま返す
- 対象 locale に訳が無い → ja へ fallback (それも無ければ任意の既存訳 → key)
- ``str.format`` 失敗 (placeholder 不一致等) → 未整形のテンプレート文字列を返す

zh / ko 対応は ``SUPPORTED_LOCALES`` への追加 + ``MESSAGES`` 各エントリへの
``"zh"`` / ``"ko"`` キー追加のみで完了する (構造は準備済み)。
"""
from __future__ import annotations

import locale as _locale_mod
import os

from llterm.i18n.messages import MESSAGES

__all__ = ["DEFAULT_LOCALE", "MESSAGES", "SUPPORTED_LOCALES", "resolve_locale", "t"]

DEFAULT_LOCALE = "ja"
# 訳が完備している locale。zh / ko はここへ追加 + MESSAGES 各エントリに訳を足す。
SUPPORTED_LOCALES: tuple[str, ...] = ("ja", "en")

# Windows の locale 名 ("Japanese_Japan" 等) → 言語コードの別名表
_LANGUAGE_ALIASES: dict[str, str] = {
    "japanese": "ja",
    "english": "en",
    "chinese": "zh",
    "korean": "ko",
}

# OS locale 検出のキャッシュ (env LLTERM_LANG は毎回評価するためテストはそちらで切替可能)
_OS_LOCALE_UNSET = object()
_os_locale_cache: object = _OS_LOCALE_UNSET


def _normalize_locale(raw: str) -> str | None:
    """locale 表現 ("ja", "en_US.UTF-8", "Japanese_Japan.932" 等) を言語コードへ正規化する。

    対応 locale (:data:`SUPPORTED_LOCALES`) に解決できなければ None (fail-safe)。
    """
    try:
        s = raw.strip().lower().replace("-", "_")
        s = s.split(".", 1)[0].split("@", 1)[0]
        lang = s.split("_", 1)[0]
        lang = _LANGUAGE_ALIASES.get(lang, lang)
        return lang if lang in SUPPORTED_LOCALES else None
    except (AttributeError, TypeError):
        return None


def _detect_os_locale() -> str | None:
    """OS locale を検出して対応言語コードを返す (検出不能・未対応言語は None)。"""
    candidates: list[str] = []
    try:
        loc = _locale_mod.getlocale()[0]
        if loc:
            candidates.append(loc)
    except (ValueError, TypeError):
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    for cand in candidates:
        norm = _normalize_locale(cand)
        if norm is not None:
            return norm
    return None


def _os_locale() -> str | None:
    """OS locale 検出のキャッシュ付きラッパ (プロセス内で locale は変わらない前提)。"""
    global _os_locale_cache
    if _os_locale_cache is _OS_LOCALE_UNSET:
        _os_locale_cache = _detect_os_locale()
    return _os_locale_cache  # type: ignore[return-value]


def resolve_locale() -> str:
    """表示 locale を解決する: ``LLTERM_LANG`` → OS locale → 既定 "ja"。

    ``LLTERM_LANG`` が未対応値 (例: "fr") の場合は無視して次の段へ進む (fail-safe)。
    """
    env = os.environ.get("LLTERM_LANG", "")
    if env:
        norm = _normalize_locale(env)
        if norm is not None:
            return norm
    return _os_locale() or DEFAULT_LOCALE


def t(key: str, /, **kwargs: object) -> str:
    """key に対応する現 locale の文字列を返す (placeholder は ``str.format`` で整形)。

    fail-safe 契約: 未知 key → key / 訳欠落 → ja fallback / format 失敗 → 未整形文字列。
    いかなる入力でも例外を呼び出し側へ漏らさない (i18n が UI を殺さない)。
    """
    try:
        entry = MESSAGES.get(key)
        if not isinstance(entry, dict) or not entry:
            return key
        template = entry.get(resolve_locale()) or entry.get(DEFAULT_LOCALE)
        if not isinstance(template, str) or not template:
            # ja すら無い (または型不正な) 不完全エントリ → 任意の既存 str 訳で粘る
            template = next((v for v in entry.values() if isinstance(v, str) and v), None)
            if template is None:
                return key
        if kwargs:
            try:
                return template.format(**kwargs)
            except (KeyError, IndexError, ValueError, TypeError):
                return template  # placeholder 不一致等 — 未整形でも表示は守る
        return template
    except Exception:  # noqa: BLE001 — i18n 起因の例外は絶対に外へ出さない (契約)
        return key
