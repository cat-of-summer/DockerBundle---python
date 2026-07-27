"""Parsing and merging of ``.env`` / ``.env.example`` files.

Comments are kept and attached to the assignment that follows them, because the
generated ``dist/.env.example`` is meant to stay as readable as the hand-written
originals it is stitched together from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.model import EnvVar

_QUOTES = ("'", '"')


@dataclass
class EnvFile:
    path: Path | None
    entries: list[EnvVar]

    def as_dict(self) -> dict[str, str]:
        return {entry.key: entry.value for entry in self.entries}

    def get(self, key: str, default: str = "") -> str:
        for entry in self.entries:
            if entry.key == key:
                return entry.value
        return default


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1]
    return value


def parse_text(text: str, *, source: str = "") -> list[EnvVar]:
    """Parse ``KEY=value`` lines, attaching preceding comments to each key."""
    entries: list[EnvVar] = []
    pending: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            # A blank line ends a comment block rather than carrying it further down.
            pending.clear()
            continue

        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue

        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            pending.clear()
            continue

        key = key.strip()
        if not key:
            pending.clear()
            continue

        entries.append(
            EnvVar(
                key=key,
                value=_strip_quotes(value.strip()),
                comment="\n".join(pending),
                source=source,
            )
        )
        pending.clear()

    return entries


def load(path: Path, *, source: str = "") -> EnvFile:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return EnvFile(path=path, entries=[])
    return EnvFile(path=path, entries=parse_text(text, source=source))


def load_optional(directory: Path, *names: str, source: str = "") -> EnvFile:
    """Load the first of ``names`` that exists in ``directory``.

    Packages ship ``.env.example``; a configured deployment also has ``.env``, which
    wins because it holds the values actually in use.
    """
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return load(candidate, source=source)
    return EnvFile(path=None, entries=[])


def render(entries: list[EnvVar]) -> str:
    """Render entries back to ``.env`` text, restoring comments."""
    lines: list[str] = []
    for entry in entries:
        if entry.comment:
            lines.extend(f"# {part}" for part in entry.comment.split("\n"))
        lines.append(f"{entry.key}={entry.value}")
    return "\n".join(lines) + ("\n" if lines else "")
