"""Merge every service's ``.env.example`` into one, without losing values.

The old ``bundle.sh`` concatenated the files and ran ``sort -u``. That silently discarded
data: ``EXTERNAL_ACCESS``, ``TRAEFIK_DOMAIN`` and ``HTTP_LOGIN`` are defined by almost
every package with *different* values, and whichever line sorted first won.

Here a key defined once, or defined identically everywhere, is simply kept. A key with
genuinely different values is a **conflict** and is reported for a decision rather than
resolved by guesswork. Decisions are recorded in ``docker-bundle.yml`` so they are made
once.

Renaming a key is what lets several services keep reading the name they were written
against. The application still sees ``TRAEFIK_DOMAIN``; the deployment's ``.env`` holds
``TRAEFIK_DOMAIN_APP`` and ``TRAEFIK_DOMAIN_VNU``, and :func:`process_environment` builds
the mapping supervisord uses to hand each program the names it expects. Whether those
outer names are derived (``prefix``) or written out by hand (``per_service``) changes
nothing downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.manifest import EnvRule
from core.model import EnvVar, ServiceSpec
from discover.interpolate import referenced_names

#: ``{local:<slug>}`` inside a ``value:`` literal, expanded to ``127.0.0.1:<port>``.
_LOCAL_REF = re.compile(r"\{local:([A-Za-z0-9_.-]+)\}")

#: Every service in the bundle shares one network namespace, so the way to reach another
#: one is the loopback address and the port it was actually assigned.
LOCAL_HOST = "127.0.0.1"


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
    """Slug -> {original key: renamed key} for keys the merge had to move."""

    conflicts: list[Conflict] = field(default_factory=list)
    """Conflicts with no recorded decision. Generation stops on these."""

    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    """Decisions that cannot be carried out. Generation stops on these too."""

    def rename_for(self, slug: str, key: str) -> str:
        return self.renames.get(slug, {}).get(key, key)


def _collect(specs: list[ServiceSpec]) -> dict[str, dict[str, EnvVar]]:
    """Build ``key -> {slug: entry}`` preserving first-seen key order."""
    table: dict[str, dict[str, EnvVar]] = {}
    for spec in specs:
        for entry in spec.env_vars:
            table.setdefault(entry.key, {})[spec.slug] = entry
    return table


def _expand_local(text: str, ports: dict[str, int], where: str, errors: list[str]) -> str:
    """Replace ``{local:<slug>}`` with the address that service ended up on.

    Written out rather than pinned by hand because the port is not the author's to know:
    two services wanting ``:80`` inside one bundle means one of them is moved, and a
    literal written before that happened points at nothing.
    """

    def replace(match: re.Match[str]) -> str:
        slug = match.group(1)
        port = ports.get(slug)
        if port is None:
            errors.append(
                f"{where}: local:{slug} names a service that is not baked into this "
                f"bundle, so it has no port here"
            )
            return match.group(0)
        return f"{LOCAL_HOST}:{port}"

    return _LOCAL_REF.sub(replace, text)


def _literal(rule: str, ports: dict[str, int], where: str, errors: list[str]) -> str:
    """The value a ``value:`` or ``local:`` rule produces."""
    if rule.startswith("local:"):
        slug = rule[len("local:") :].strip()
        return _expand_local("{local:" + slug + "}", ports, where, errors)
    return _expand_local(rule[len("value:") :], ports, where, errors)


def merge(
    specs: list[ServiceSpec],
    *,
    globals_: list[str],
    rules: dict[str, EnvRule] | None = None,
    prefixes: dict[str, str] | None = None,
    ports: dict[str, int] | None = None,
) -> Merge:
    """Merge per-service environments.

    ``globals_`` names keys that are infrastructure-wide and deliberately shared, so they
    never conflict. ``rules`` carries the ``env:`` section, ``prefixes`` maps a slug to
    its environment prefix, and ``ports`` maps a slug to the port it was assigned inside
    the bundle, which is what ``local:`` resolves against.
    """
    rules = rules or {}
    prefixes = prefixes or {}
    ports = ports or {}
    global_keys = set(globals_)
    result = Merge()
    declared = _collect(specs)

    # A `value:` or `local:` rule for a key no package declares is an *addition*, not a
    # resolution: `DB_ADDR: local:mysql` exists precisely because nothing in the sources
    # knows the address a service ends up on inside the bundle. Emitting only keys the
    # packages already mention would drop those without a word.
    for key, rule in rules.items():
        if key in declared or not rule.rule.startswith(("value:", "local:")):
            continue
        literal = _literal(rule.rule, ports, f"env.{key}", result.errors)
        result.entries.append(EnvVar(key=key, value=literal, comment="", source="manual"))

    for key, by_slug in declared.items():
        entries = list(by_slug.values())
        distinct = {entry.value for entry in entries}
        rule = rules.get(key) or EnvRule()
        where = f"env.{key}"

        # Explicit per-service names come first and are honoured whether or not the key
        # currently conflicts. Ignoring them because the values happen to agree today
        # would drop an instruction the author wrote down, and start applying it again
        # the moment some other package changed its default.
        if rule.per_service:
            _apply_per_service(key, by_slug, rule, result)
            continue

        if rule.rule.startswith(("value:", "local:")):
            literal = _literal(rule.rule, ports, where, result.errors)
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

        if not rule.rule:
            values = {slug: entry.value for slug, entry in by_slug.items()}
            result.conflicts.append(Conflict(key=key, values=values))
            continue

        if rule.rule == "prefix":
            for slug, entry in sorted(by_slug.items()):
                prefix = prefixes.get(slug) or slug.upper()
                renamed = f"{prefix}_{key}"
                result.renames.setdefault(slug, {})[key] = renamed
                result.entries.append(
                    EnvVar(key=renamed, value=entry.value, comment=entry.comment, source=slug)
                )
        elif rule.rule.startswith("keep:"):
            slug = rule.rule.split(":", 1)[1]
            entry = by_slug.get(slug)
            if entry is None:
                result.warnings.append(
                    f"{key}: keep:{slug} names a service that is not in this bundle; "
                    f"falling back to {sorted(by_slug)[0]!r}"
                )
                entry = by_slug[sorted(by_slug)[0]]
            result.entries.append(
                EnvVar(key=key, value=entry.value, comment=entry.comment, source=slug)
            )

    _check_collisions(result)
    result.entries.sort(key=lambda e: (e.source != "global", e.source, e.key))
    return result


def _apply_per_service(
    key: str, by_slug: dict[str, EnvVar], rule: EnvRule, result: Merge
) -> None:
    """Emit one entry per service under the name the author chose for it.

    A service the mapping does not mention keeps the original key. That is deliberate:
    naming two of three stands is a complete instruction when the third is the one that
    should keep the plain name. It stops being complete when the services left over still
    disagree, and then the key is reported as the conflict it still is.
    """
    listed = {slug: name for slug, name in rule.per_service.items() if slug in by_slug}
    for slug in sorted(rule.per_service):
        if slug not in by_slug:
            result.warnings.append(
                f"env.{key}.per_service.{slug}: no such service in this bundle; ignored"
            )

    for slug, renamed in sorted(listed.items()):
        entry = by_slug[slug]
        result.renames.setdefault(slug, {})[key] = renamed
        result.entries.append(
            EnvVar(key=renamed, value=entry.value, comment=entry.comment, source=slug)
        )

    rest = {slug: entry for slug, entry in by_slug.items() if slug not in listed}
    if not rest:
        return

    values = {entry.value for entry in rest.values()}
    if len(values) > 1:
        result.conflicts.append(
            Conflict(key=key, values={slug: entry.value for slug, entry in rest.items()})
        )
        return

    first = next(iter(rest.values()))
    result.entries.append(
        EnvVar(
            key=key,
            value=first.value,
            comment=first.comment,
            source=first.source if len(rest) == 1 else "shared",
        )
    )


def _check_collisions(result: Merge) -> None:
    """Refuse two services whose renamed keys landed on the same name.

    Two values cannot share one line of ``.env``, and the one that lost would be gone
    with nothing said about it.
    """
    owners: dict[str, list[str]] = {}
    for entry in result.entries:
        owners.setdefault(entry.key, []).append(entry.source or "shared")

    for key, sources in sorted(owners.items()):
        if len(sources) > 1:
            result.errors.append(
                f"{key} is written by {', '.join(sorted(sources))}; two values cannot "
                f"share one .env key. Give them distinct names under env.{key}.per_service."
            )


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
