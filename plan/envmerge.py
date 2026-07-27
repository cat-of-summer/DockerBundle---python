"""Merge every service's ``.env.example`` into one, without losing values.

The old ``bundle.sh`` concatenated the files and ran ``sort -u``. That silently discarded
data: ``EXTERNAL_ACCESS``, ``TRAEFIK_DOMAIN`` and ``HTTP_LOGIN`` are defined by almost
every package with *different* values, and whichever line sorted first won.

Here a key defined once, or defined identically everywhere, is simply kept. A key with
genuinely different values is a **conflict** and is reported for a decision rather than
resolved by guesswork. Decisions are recorded in ``bundle.yml`` so they are made once.

Resolving a conflict with ``prefix`` renames the key per service (``LARAVEL_DB_HOST``).
The application must still see ``DB_HOST``, so :func:`process_environment` builds the
mapping supervisord uses to hand each program the names it expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.model import EnvVar, ServiceSpec
from discover.interpolate import referenced_names


@dataclass
class Conflict:
    """One key defined with different values by different services."""

    key: str
    values: dict[str, str]
    """Slug -> the value that service declares."""

    def summary(self) -> str:
        pairs = ", ".join(f"{slug}={value!r}" for slug, value in sorted(self.values.items()))
        return f"{self.key}: {pairs}"


@dataclass
class Merge:
    entries: list[EnvVar] = field(default_factory=list)
    """The merged ``.env.example``, in a stable order."""

    renames: dict[str, dict[str, str]] = field(default_factory=dict)
    """Slug -> {original key: renamed key} for keys resolved by prefixing."""

    conflicts: list[Conflict] = field(default_factory=list)
    """Conflicts with no recorded decision. Generation stops on these."""

    warnings: list[str] = field(default_factory=list)

    def rename_for(self, slug: str, key: str) -> str:
        return self.renames.get(slug, {}).get(key, key)


def _collect(specs: list[ServiceSpec]) -> dict[str, dict[str, EnvVar]]:
    """Build ``key -> {slug: entry}`` preserving first-seen key order."""
    table: dict[str, dict[str, EnvVar]] = {}
    for spec in specs:
        for entry in spec.env_vars:
            table.setdefault(entry.key, {})[spec.slug] = entry
    return table


def merge(
    specs: list[ServiceSpec],
    *,
    globals_: list[str],
    decisions: dict[str, str] | None = None,
    prefixes: dict[str, str] | None = None,
) -> Merge:
    """Merge per-service environments.

    ``globals_`` names keys that are infrastructure-wide and deliberately shared, so they
    never conflict. ``decisions`` maps a key to ``prefix`` | ``keep:<slug>`` |
    ``value:<literal>``. ``prefixes`` maps a slug to its environment prefix.
    """
    decisions = decisions or {}
    prefixes = prefixes or {}
    global_keys = set(globals_)
    result = Merge()

    for key, by_slug in _collect(specs).items():
        entries = list(by_slug.values())
        distinct = {entry.value for entry in entries}
        rule = decisions.get(key, "")

        # An explicit decision is honoured whether or not the key currently conflicts.
        # Ignoring it because the values happen to agree today would silently drop an
        # instruction the user wrote down, and would start applying it again the moment
        # some other package changed its default.
        if rule.startswith("value:"):
            literal = rule.split(":", 1)[1]
            result.entries.append(EnvVar(key=key, value=literal, comment="", source="manual"))
            continue

        if key in global_keys:
            # Declared shared by the user. Prefer a non-empty value; several packages
            # leave INSTANCE blank while one sets it.
            chosen = next((e for e in entries if e.value), entries[0])
            if len(distinct) > 1:
                result.warnings.append(
                    f"{key} is declared global but differs between "
                    f"{', '.join(sorted(by_slug))}; using {chosen.value!r}"
                )
            result.entries.append(
                EnvVar(key=key, value=chosen.value, comment=chosen.comment, source="global")
            )
            continue

        if len(distinct) == 1:
            first = entries[0]
            result.entries.append(
                EnvVar(
                    key=key,
                    value=first.value,
                    comment=first.comment,
                    source=first.source if len(by_slug) == 1 else "shared",
                )
            )
            continue

        if not rule:
            values = {s: e.value for s, e in by_slug.items()}
            result.conflicts.append(Conflict(key=key, values=values))
            continue

        if rule == "prefix":
            for slug, entry in sorted(by_slug.items()):
                prefix = prefixes.get(slug) or slug.upper()
                renamed = f"{prefix}_{key}"
                result.renames.setdefault(slug, {})[key] = renamed
                result.entries.append(
                    EnvVar(key=renamed, value=entry.value, comment=entry.comment, source=slug)
                )
        elif rule.startswith("keep:"):
            slug = rule.split(":", 1)[1]
            entry = by_slug.get(slug)
            if entry is None:
                result.warnings.append(
                    f"{key}: bundle.yml keeps the value from {slug!r}, which is not in this "
                    f"bundle; falling back to {sorted(by_slug)[0]!r}"
                )
                entry = by_slug[sorted(by_slug)[0]]
            result.entries.append(
                EnvVar(key=key, value=entry.value, comment=entry.comment, source=slug)
            )
        elif rule.startswith("value:"):
            literal = rule.split(":", 1)[1]
            result.entries.append(EnvVar(key=key, value=literal, comment="", source="manual"))

    result.entries.sort(key=lambda e: (e.source != "global", e.source, e.key))
    return result


def process_environment(spec: ServiceSpec, merged: Merge) -> dict[str, str]:
    """Environment a service's supervisord programs need beyond the container's own.

    Only keys that were renamed need an entry: everything else is already in the
    container environment under the name the application expects. The value is a
    supervisord ``%(ENV_X)s`` reference to the renamed key.
    """
    mapping: dict[str, str] = {}
    renames = merged.renames.get(spec.slug, {})
    if not renames:
        return mapping

    # `environment: DB_HOST: ${DB_HOST}` tells us the process variable DB_HOST is fed
    # from the .env key DB_HOST. After renaming, point it at the new key.
    for process_key, raw_value in spec.raw_environment.items():
        for referenced in referenced_names(raw_value):
            renamed = renames.get(referenced)
            if renamed:
                mapping[process_key] = f"%(ENV_{renamed})s"

    # A renamed key the service never mentioned in `environment:` is still likely read
    # straight from the environment, so restore it under its original name too.
    for original, renamed in renames.items():
        mapping.setdefault(original, f"%(ENV_{renamed})s")

    return mapping
