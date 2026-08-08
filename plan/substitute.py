"""Placeholder substitution for recipe strings.

``str.format`` cannot be used here: recipe commands legitimately contain shell syntax
with braces, such as ``${GUNICORN_WORKERS:-2}`` and ``%(program_name)s``. Only an
explicit, closed set of placeholders is replaced; anything else is left untouched.
"""

from __future__ import annotations

import re

#: Placeholders a recipe may use.
PLACEHOLDERS = ("slug", "port", "name", "package", "prefix", "image")

_PATTERN = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def substitute(text: str, values: dict[str, object]) -> str:
    """Replace ``{slug}``, ``{port}`` and friends in ``text``."""
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        return match.group(0) if value is None else str(value)

    return _PATTERN.sub(replace, text)
