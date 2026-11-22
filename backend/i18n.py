# -*- coding: utf-8 -*-
"""插件运行时文案。

当前只提供简体中文；新增语言时在 ``TRANSLATIONS`` 里按中文原文登记译文即可，
不必改调用点。控制台插件面板由前端 ``ui/index.js`` 的同名机制处理。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUPPORTED = ("zh",)

#: 语言 -> {中文原文: 译文}。中文留空表示直接使用原文。
TRANSLATIONS: dict[str, dict[str, str]] = {"zh": {}}


def _primary_language(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].split(";", 1)[0].strip().lower()
    if not first:
        return None
    return first.split("-", 1)[0]


def _console_language() -> str | None:
    """Console 的语言设置；读不到时返回 None。"""
    try:
        from qwenpaw.constant import WORKING_DIR

        data = json.loads(
            (Path(WORKING_DIR) / "settings.json").read_text(encoding="utf-8"),
        )
        language = data.get("language") if isinstance(data, dict) else None
        return str(language) if language else None
    except Exception:  # pylint: disable=broad-except
        return None


def resolve_lang(request=None) -> str:
    """按 ?lang → Accept-Language → Console 语言 → 中文 的顺序判定。"""
    candidate = None
    if request is not None:
        query = getattr(request, "query_params", None)
        if query is not None:
            candidate = query.get("lang")
        if not candidate:
            candidate = request.headers.get("accept-language")
    primary = _primary_language(str(candidate) if candidate else None)
    if primary in SUPPORTED:
        return primary
    console = _primary_language(_console_language())
    if console in SUPPORTED:
        return console
    return "zh"


def t(text: str, lang: str = "zh", **kwargs: Any) -> str:
    """取译文并按需格式化；缺失时回退中文原文。"""
    template = TRANSLATIONS.get(lang, {}).get(text, text)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
