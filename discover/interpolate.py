"""Compose-style variable interpolation.

Every package in the wild leans on this: ``image: mysql:${MYSQL_VERSION}``,
``hostname: mysql${INSTANCE}``, ``--max-input-tokens "${MAX_INPUT_TOKENS:-4096}"``.
Without expanding these we cannot tell what image a service runs or what port it binds.

Implements the subset Docker Compose supports:

``$VAR`` ``${VAR}``            plain substitution
``${VAR:-default}``            default when unset *or* empty
``${VAR-default}``             default when unset
``${VAR:?message}``            error when unset or empty
``${VAR?message}``             error when unset
``$$``                         a literal ``$``

Unknown variables expand to an empty string rather than raising, because a compose file
is routinely written against an ``.env`` we were not given.
"""

from __future__ import annotations

import re

_PATTERN = re.compile(
    r"""
    \$(?:
        (?P<escaped>\$)
      | (?P<named>[A-Za-z_][A-Za-z0-9_]*)
      | \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)
           (?:(?P<sep>:?[-?])(?P<arg>[^}]*))?
        \}
    )
    """,
    re.VERBOSE,
)


class InterpolationError(ValueError):
    """Raised for ``${VAR:?message}`` when the variable is missing."""


def interpolate(text: str, values: dict[str, str], *, strict: bool = False) -> str:
    """Expand compose variables in ``text``.

    With ``strict=False`` (the default) a missing variable becomes an empty string; the
    ``:?`` form still raises, since that is the author explicitly demanding a value.
    """

    def replace(match: re.Match[str]) -> str:
        if match.group("escaped"):
            return "$"

        name = match.group("named") or match.group("braced")
        sep = match.group("sep")
        arg = match.group("arg") or ""
        present = name in values
        value = values.get(name, "")

        if sep in ("-", ":-"):
            missing = not present if sep == "-" else not value
            return arg if missing else value

        if sep in ("?", ":?"):
            missing = not present if sep == "?" else not value
            if missing:
                raise InterpolationError(f"{name}: {arg or 'required but not set'}")
            return value

        if not present and strict:
            raise InterpolationError(f"{name} is not set")
        return value

    return _PATTERN.sub(replace, text)


def interpolate_tree(node, values: dict[str, str], *, strict: bool = False):
    """Recursively interpolate every string in a parsed YAML tree.

    Mapping *keys* are interpolated too: compose allows ``${VAR}`` in service names and
    in ``environment:`` keys.
    """
    if isinstance(node, str):
        return interpolate(node, values, strict=strict)
    if isinstance(node, list):
        return [interpolate_tree(item, values, strict=strict) for item in node]
    if isinstance(node, dict):
        return {
            interpolate_tree(key, values, strict=strict): interpolate_tree(
                value, values, strict=strict
            )
            for key, value in node.items()
        }
    return node


def referenced_names(text: str) -> set[str]:
    """Return every variable name mentioned in ``text``."""
    names: set[str] = set()
    for match in _PATTERN.finditer(text):
        name = match.group("named") or match.group("braced")
        if name:
            names.add(name)
    return names


def strip_defaults(text: str) -> str:
    """Reduce every reference to its bare ``${NAME}``, dropping defaults and messages.

    Lets two strings be compared for what they will resolve to rather than for how they
    were typed: ``${X}`` and ``${X:-true}`` name the same value whenever ``X`` is set,
    and one package writing the default while another leaves it out is a difference in
    style, not in configuration.
    """

    def replace(match: re.Match[str]) -> str:
        if match.group("escaped"):
            return match.group(0)
        return "${" + (match.group("named") or match.group("braced")) + "}"

    return _PATTERN.sub(replace, text)


def has_default(text: str) -> bool:
    """True when any reference in ``text`` carries a ``:-``/``-`` fallback."""
    return any(match.group("sep") in ("-", ":-") for match in _PATTERN.finditer(text))


def rename_variables(text: str, mapping: dict[str, str]) -> str:
    """Rewrite variable *names* in ``text``, leaving everything else as written.

    Used when a key had to be renamed to survive the merge into one ``.env``: a label
    reading ``${TRAEFIK_DOMAIN}`` has to follow the key to ``${NGINX_TRAEFIK_DOMAIN}``
    while keeping its defaults and surrounding syntax intact.
    """
    if not mapping:
        return text

    def replace(match: re.Match[str]) -> str:
        if match.group("escaped"):
            return match.group(0)
        name = match.group("named") or match.group("braced")
        renamed = mapping.get(name)
        if renamed is None:
            return match.group(0)
        sep = match.group("sep")
        if sep is None:
            return "${" + renamed + "}"
        return "${" + renamed + sep + (match.group("arg") or "") + "}"

    return _PATTERN.sub(replace, text)
