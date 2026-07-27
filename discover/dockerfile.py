"""Extract the effective base image from a service's own Dockerfile.

A service built with ``build:`` has no ``image:`` to match a recipe against, but its
Dockerfile says what it really is. The packages lean on build args for this:

    ARG PHP_VERSION
    FROM php:${PHP_VERSION}

with ``PHP_VERSION`` supplied by ``build.args`` and ultimately by ``.env.example``.
Resolving that yields ``php:fpm-alpine``, which is the signal recipe matching needs.
"""

from __future__ import annotations

import re
from pathlib import Path

from discover.interpolate import interpolate

_ARG = re.compile(r"^\s*ARG\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=(?P<default>.*))?$", re.I)
_FROM = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.I
)


def _join_continuations(text: str) -> list[str]:
    """Fold Dockerfile backslash line-continuations into single logical lines."""
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        lines.append(buffer + stripped)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def base_image(path: Path, args: dict[str, str] | None = None) -> str:
    """Return the image the final build stage is based on, or an empty string.

    Multi-stage files resolve through named stages, so a final ``FROM builder`` reports
    whatever ``builder`` was based on rather than the stage name.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""

    values = dict(args or {})
    stages: dict[str, str] = {}
    last = ""

    for line in _join_continuations(text):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        arg_match = _ARG.match(line)
        if arg_match:
            name = arg_match.group("name")
            default = (arg_match.group("default") or "").strip().strip("\"'")
            # A value passed via build.args wins over the Dockerfile's own default.
            if name not in values or not values[name]:
                values[name] = default
            continue

        from_match = _FROM.match(line)
        if from_match:
            image = interpolate(from_match.group("image"), values)
            # `FROM builder` refers to an earlier stage, not a registry image.
            image = stages.get(image, image)
            stage = from_match.group("stage")
            if stage:
                stages[stage] = image
            last = image

    return last.strip()


def find(directory: Path, name: str = "Dockerfile") -> Path | None:
    candidate = directory / name
    return candidate if candidate.is_file() else None
