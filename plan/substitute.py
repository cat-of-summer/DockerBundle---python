"""Placeholder substitution for recipe strings.

``str.format`` cannot be used here: recipe commands legitimately contain shell syntax
with braces, such as ``${GUNICORN_WORKERS:-2}`` and ``%(program_name)s``. Only an
explicit, closed set of placeholders is replaced; anything else is left untouched.

Besides the fixed names there are two families:

* ``{param.NAME}`` — a value from the recipe's ``params:``, which a manifest recipe
  overrides with ``+params:``. Recipe loading has already checked that every name used
  is declared;
* ``{local:<slug>}`` — ``127.0.0.1:<port>`` of another baked service. Every service in
  the bundle shares one network namespace, so loopback and the port it was actually
  assigned is the way to reach it; a literal written before a collision moved the
  service points at nothing.
"""

from __future__ import annotations

import re

from recipes.schema import PARAM_REF

#: Placeholders a recipe may use.
PLACEHOLDERS = ("slug", "port", "name", "package", "prefix", "image")

_PATTERN = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")

#: ``{local:<slug>}``.
LOCAL_REF = re.compile(r"\{local:([A-Za-z0-9_.-]+)\}")

LOCAL_HOST = "127.0.0.1"


class SubstitutionError(ValueError):
    """A placeholder names something this bundle does not have."""


def expand_local(text: str, ports: dict[str, int]) -> str:
    """Replace ``{local:<slug>}`` with the address that service ended up on."""

    def replace(match: re.Match[str]) -> str:
        slug = match.group(1)
        port = ports.get(slug)
        if port is None:
            raise SubstitutionError(
                f"local:{slug} names a service that is not baked into this bundle, so it "
                f"has no port here"
            )
        return f"{LOCAL_HOST}:{port}"

    return LOCAL_REF.sub(replace, text)


def substitute(text: str, values: dict[str, object]) -> str:
    """Replace ``{slug}``, ``{port}``, ``{param.X}``, ``{local:x}`` and friends in ``text``.

    ``values`` may carry ``params`` (name -> value) and ``local`` (slug -> port).
    """
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        return match.group(0) if value is None else str(value)

    text = _PATTERN.sub(replace, text)

    params = values.get("params")
    if isinstance(params, dict):
        text = PARAM_REF.sub(lambda match: str(params.get(match.group(1), match.group(0))), text)

    local = values.get("local")
    if isinstance(local, dict):
        text = expand_local(text, local)
    return text
