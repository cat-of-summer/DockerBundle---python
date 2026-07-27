"""Assemble a :class:`~core.model.BundlePlan` from services, recipes and the manifest.

This is the heart of the tool and deliberately a pure function: no filesystem writes, no
Docker calls, no prompting. Everything it needs arrives as arguments and everything it
decides comes back in the plan, which makes the whole pipeline testable without a
container in sight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.manifest import Manifest
from core.model import (
    BundlePlan,
    CopyOp,
    MountKind,
    MountMode,
    Origin,
    PlannedService,
    ReadinessProbe,
    ServiceSpec,
    SupervisorProgram,
    env_prefix,
)
from plan import envmerge, graph, mounts, ports
from plan.substitute import substitute
from recipes import fallback
from recipes.match import Registry
from recipes.schema import Recipe

#: Programs with a recipe priority below this are data services: databases, caches,
#: brokers, model servers. They start before any service's init script runs, so that the
#: init has something to migrate against. Everything at or above it is an application
#: process and is started only once initialisation has finished.
INIT_BARRIER = 30


class PlanError(ValueError):
    """Generation cannot proceed. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Resolved:
    """A service paired with the recipe that claimed it."""

    spec: ServiceSpec
    recipe: Recipe
    is_fallback: bool
    bakeable: bool
    replicas: int = 1
    prefix: str = ""
    ports: list = field(default_factory=list)


def compute_prefixes(specs: list[ServiceSpec], overrides: dict[str, str]) -> dict[str, str]:
    """Pick a short, stable environment prefix per service.

    Slugs are qualified for uniqueness (``laravel_nginx_laravel``) which makes a poor
    prefix. The service's own name is used when it is unambiguous within the bundle,
    falling back to the full slug when it is not.
    """
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.name] = counts.get(spec.name, 0) + 1

    result: dict[str, str] = {}
    for spec in specs:
        override = overrides.get(spec.slug)
        if override:
            result[spec.slug] = override.upper()
        elif counts[spec.name] == 1:
            result[spec.slug] = env_prefix(spec.name)
        else:
            result[spec.slug] = env_prefix(spec.slug)
    return result


def _resolve_recipes(
    specs: list[ServiceSpec], manifest: Manifest, registry: Registry, problems: list[str]
) -> list[Resolved]:
    resolved: list[Resolved] = []
    for spec in specs:
        entry = manifest.services.get(spec.slug)
        forced = entry.recipe if entry else ""
        try:
            recipe, is_fallback = registry.resolve(spec, forced=forced)
        except KeyError:
            problems.append(
                f"{spec.slug}: bundle.yml asks for recipe {forced!r}, which does not exist"
            )
            continue

        bakeable = recipe.bakeable
        if entry is not None and entry.bakeable is not None:
            bakeable = entry.bakeable

        resolved.append(
            Resolved(
                spec=spec,
                recipe=recipe,
                is_fallback=is_fallback,
                bakeable=bakeable,
                replicas=entry.replicas if entry else 1,
            )
        )
    return resolved


def _copies_for(item: Resolved, port: int | None) -> tuple[list[CopyOp], dict[str, str]]:
    """Build the COPY operations for one service, from its recipe and its mounts.

    Also returns a map of original mount target -> where it ended up in the image, used
    to relocate paths that were meaningful in the source layout.
    """
    spec, recipe = item.spec, item.recipe
    values = {"slug": spec.slug, "port": port, "name": spec.name, "prefix": item.prefix}
    copies: list[CopyOp] = []
    consumed: set[str] = set()
    relocated: dict[str, str] = {}

    for rule in recipe.copy:
        dest = substitute(rule.dest, values)

        if rule.from_mount:
            mount = spec.mount_for(rule.from_mount)
            if mount is None or mount.mode is not MountMode.COPY:
                continue
            if spec.source_dir is None:
                continue
            source = (spec.source_dir / mount.source).resolve()
            consumed.add(mount.target)
            relocated[mount.target] = dest
        else:
            if spec.source_dir is None:
                continue
            source = (spec.source_dir / substitute(rule.src, values)).resolve()

        if not source.exists():
            if not rule.optional:
                raise PlanError([f"{spec.slug}: required source {source} is missing"])
            continue

        copies.append(
            CopyOp(
                context=f"{spec.slug}/{source.name}",
                target=dest,
                source=source,
                kind=rule.kind,
                chmod=rule.chmod,
            )
        )

    # Mounts the user marked COPY that no recipe rule already covers still need baking,
    # otherwise a deliberate choice in the wizard would quietly do nothing. A file the
    # recipe already placed is skipped: the recipe knows the right destination for the
    # bundle's layout, whereas the mount target is the upstream image's.
    placed = {copy.source for copy in copies}
    for mount in spec.mounts:
        if mount.mode is not MountMode.COPY or mount.target in consumed:
            continue
        if spec.source_dir is None or mount.named:
            continue
        source = (spec.source_dir / mount.source).resolve()
        if not source.exists() or source in placed:
            continue
        if any(copy.target == mount.target for copy in copies):
            continue
        copies.append(
            CopyOp(
                context=f"{spec.slug}/{source.name}",
                target=mount.target,
                source=source,
                kind=mount.kind,
            )
        )

    return copies, relocated


def _programs_for(
    item: Resolved, port: int | None, extra_env: dict[str, str], warnings: list[str]
) -> list[SupervisorProgram]:
    spec, recipe = item.spec, item.recipe
    values = {"slug": spec.slug, "port": port, "name": spec.name, "prefix": item.prefix}

    programs: list[SupervisorProgram] = []
    for rule in recipe.programs:
        name = substitute(rule.name or spec.slug, values)
        environment = {
            key: substitute(value, values) for key, value in rule.environment.items()
        }
        environment.update(extra_env)

        replicas_var = ""
        if rule.scalable:
            replicas_var = f"{item.prefix}_REPLICAS"
        elif item.replicas > 1:
            warnings.append(
                f"{spec.slug}: replicas={item.replicas} ignored for program {name!r} — it binds "
                f"port {port}, and extra copies would fail with EADDRINUSE. Scale the bundle "
                f"with compose instead."
            )

        programs.append(
            SupervisorProgram(
                name=name,
                command=substitute(rule.command, values),
                priority=rule.priority,
                directory=substitute(rule.directory, values),
                environment=environment,
                stopsignal=rule.stopsignal,
                stopwaitsecs=rule.stopwaitsecs,
                startsecs=rule.startsecs,
                replicas_var=replicas_var,
                scalable=rule.scalable,
                critical=rule.critical,
            )
        )
    return programs


def build(
    specs: list[ServiceSpec],
    manifest: Manifest,
    registry: Registry,
    *,
    variant: str = "cpu",
) -> BundlePlan:
    """Produce the plan, or raise :class:`PlanError` listing everything that blocks it."""
    problems: list[str] = []
    warnings: list[str] = []

    resolved = _resolve_recipes(specs, manifest, registry, problems)
    if problems:
        raise PlanError(problems)

    prefixes = compute_prefixes(
        [item.spec for item in resolved],
        {slug: entry.env_prefix for slug, entry in manifest.services.items() if entry.env_prefix},
    )
    for item in resolved:
        item.prefix = prefixes[item.spec.slug]

    family = "debian" if manifest.base.get(variant, "").find("alpine") < 0 else "alpine"

    # -- mounts ---------------------------------------------------------
    for item in resolved:
        mounts.classify(item.spec, item.recipe)
        entry = manifest.services.get(item.spec.slug)
        if entry and entry.mounts:
            warnings.extend(mounts.apply_overrides(item.spec, entry.mounts))

    # -- ports ----------------------------------------------------------
    baked = [item for item in resolved if item.bakeable]

    allocation = ports.allocate(
        [(item.spec, item.recipe) for item in baked],
        pinned={
            slug: entry.ports for slug, entry in manifest.services.items() if entry.ports
        },
        port_range=manifest.port_range,
    )
    warnings.extend(allocation.warnings)
    problems.extend(allocation.errors)

    # -- environment ----------------------------------------------------
    merged = envmerge.merge(
        [item.spec for item in resolved],
        globals_=manifest.globals,
        decisions=manifest.env_conflicts,
        prefixes=prefixes,
    )
    warnings.extend(merged.warnings)
    if merged.conflicts:
        problems.append(
            "unresolved environment conflicts — run `dockerbundle wizard`, or add "
            "decisions under env_conflicts in bundle.yml:\n  "
            + "\n  ".join(conflict.summary() for conflict in merged.conflicts)
        )

    if problems:
        raise PlanError(problems)

    # -- per-service assembly -------------------------------------------
    plan = BundlePlan(
        name=manifest.name,
        network=manifest.network,
        network_external=manifest.network_external,
        base_images=dict(manifest.base),
        family=family,
        env=merged.entries,
        env_renames=merged.renames,
    )

    install: dict[str, list[str]] = {}
    run_steps: dict[str, list[str]] = {}
    shared_seen: set[str] = set()

    for item in resolved:
        spec, recipe = item.spec, item.recipe
        assigned = allocation.for_service(spec.slug)
        port = assigned[0].container if assigned else None
        values = {"slug": spec.slug, "port": port, "name": spec.name, "prefix": item.prefix}

        planned = PlannedService(
            spec=spec,
            recipe_name=recipe.name,
            bakeable=item.bakeable,
            ports=assigned,
            rootfs_import=item.is_fallback,
        )

        if not item.bakeable:
            planned.programs = []
            plan.sidecars.append(planned)
            if recipe.reason:
                warnings.append(f"{spec.slug}: kept outside the image — {recipe.reason.strip()}")
            continue

        if not recipe.supports(family):
            problems.append(
                f"{spec.slug}: recipe {recipe.name!r} does not support the {family!r} base "
                f"({manifest.base.get(variant)}); choose a different base or recipe"
            )
            continue

        planned.copies, relocated = _copies_for(item, port)
        planned.volumes = [
            mount for mount in spec.mounts if mount.mode is MountMode.VOLUME
        ]

        working_dir = spec.working_dir or ""
        planned.init_cwd = relocated.get(working_dir, working_dir)

        extra_env = envmerge.process_environment(spec, merged)
        # The same mapping in shell form, for the service's own entrypoint at init time.
        planned.init_env = {
            key: "${" + value[len("%(ENV_") : -len(")s")] + "}"
            for key, value in extra_env.items()
            if value.startswith("%(ENV_") and value.endswith(")s")
        }

        # A shared runtime contributes its programs and packages exactly once, however
        # many services use it: one nginx master serving many server blocks, one php-fpm
        # master with a pool per service.
        if recipe.shared:
            if recipe.shared not in shared_seen:
                shared_seen.add(recipe.shared)
                planned.programs = _programs_for(item, port, {}, warnings)
            else:
                planned.programs = []
        else:
            planned.programs = _programs_for(item, port, extra_env, warnings)

        for rule in recipe.readiness:
            planned.readiness.append(
                ReadinessProbe(
                    kind=rule.type,
                    target=substitute(rule.target, values),
                    timeout=rule.timeout,
                    label=spec.slug,
                )
            )

        planned.build_steps = [substitute(command, values) for command in recipe.post_copy]
        planned.pre_init = [substitute(command, values) for command in recipe.pre_init]
        planned.post_init = [substitute(command, values) for command in recipe.post_init]

        if item.is_fallback and spec.effective_image:
            stage = fallback.stage_name(spec.slug)
            planned.stage = stage
            plan.stages.append((stage, spec.effective_image))

        for fam, packages in recipe.install.items():
            bucket = install.setdefault(fam, [])
            for package in packages:
                if package not in bucket:
                    bucket.append(package)
        for fam, commands in recipe.run.items():
            bucket = run_steps.setdefault(fam, [])
            for command in commands:
                rendered = substitute(command, values)
                if rendered not in bucket:
                    bucket.append(rendered)

        # The service's own entrypoint runs verbatim at container start, exactly as its
        # author intended. It is never parsed or split.
        if spec.source_dir is not None:
            entrypoint = spec.source_dir / "entrypoint.sh"
            if entrypoint.is_file():
                planned.init_script = f"/usr/local/bin/entrypoint-{spec.slug}.sh"
                planned.copies.append(
                    CopyOp(
                        context=f"{spec.slug}/entrypoint.sh",
                        target=planned.init_script,
                        source=entrypoint,
                        kind=MountKind.CONFIG,
                        chmod="0755",
                    )
                )

        plan.baked.append(planned)
        plan.readiness.extend(planned.readiness)

    if problems:
        raise PlanError(problems)

    # -- ordering -------------------------------------------------------
    edges: dict[str, list[str]] = {}
    base_priority: dict[str, int] = {}
    program_owner: dict[str, SupervisorProgram] = {}

    for planned in plan.baked:
        for program in planned.programs:
            edges[program.name] = []
            base_priority[program.name] = program.priority
            program_owner[program.name] = program

    # Translate service-level depends_on into program-level edges.
    programs_of = {p.spec.slug: [prog.name for prog in p.programs] for p in plan.baked}
    for planned in plan.baked:
        for dependency in planned.spec.depends_on:
            for name in programs_of.get(planned.spec.slug, []):
                edges[name].extend(programs_of.get(dependency, []))

    try:
        final = graph.priorities(edges, base_priority)
    except graph.CycleError as exc:
        raise PlanError([str(exc)]) from exc

    for name, priority in final.items():
        program_owner[name].priority = priority

    plan.programs = sorted(program_owner.values(), key=lambda p: (p.priority, p.name))

    # Split around the init barrier, but only when something actually needs initialising;
    # with nothing to prepare there is nothing to wait for and every program can autostart.
    if plan.needs_init:
        for program in plan.programs:
            program.autostart = program.priority < INIT_BARRIER
    plan.deferred = [p.name for p in plan.programs if not p.autostart]

    # -- volumes, replicas, warnings -------------------------------------
    for planned in plan.baked:
        for mount in planned.volumes:
            plan.named_volumes[_volume_name(planned.spec.slug, mount)] = mount.target

    plan.install = install
    plan.run_steps = run_steps
    plan.port_map = dict(allocation.ports)
    plan.warnings = warnings

    for item in resolved:
        if item.is_fallback:
            warnings.append(
                f"{item.spec.slug}: no recipe matched "
                f"{item.spec.effective_image or 'this service'}; "
                f"importing its image filesystem. The image will be large and its port cannot "
                f"be changed."
            )

    for planned in plan.sidecars:
        if planned.spec.origin is Origin.IMAGE:
            warnings.append(f"{planned.spec.slug}: kept as a separate compose service")

    return plan


def _volume_name(slug: str, mount) -> str:
    """Name a named volume after the service and what it holds."""
    if mount.named:
        return mount.source
    leaf = mount.target.rstrip("/").rsplit("/", 1)[-1] or "data"
    # `/var/lib/mysql` for the `mysql` service would otherwise become `mysql_mysql`.
    return f"{slug}_data" if leaf == slug else f"{slug}_{leaf}"
