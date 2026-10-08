"""Assemble a :class:`~core.model.BundlePlan` from services, recipes and the configuration.

This is the heart of the tool and deliberately a pure function: no filesystem writes, no
Docker calls, no prompting. Everything it needs arrives as arguments and everything it
decides comes back in the plan, which makes the whole pipeline testable without a
container in sight.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from core.manifest import Manifest
from core.model import (
    BundlePlan,
    CopyOp,
    MountKind,
    MountMode,
    Origin,
    PlannedBind,
    PlannedService,
    PortSpec,
    ReadinessProbe,
    ServiceMode,
    ServiceSpec,
    SupervisorProgram,
    env_prefix,
    normalise_slug,
)
from plan import envmerge, graph, mounts, ports
from plan.substitute import SubstitutionError, substitute
from recipes import fallback
from recipes.match import Registry
from recipes.schema import Recipe

#: Programs with a recipe priority below this are data services: databases, caches,
#: brokers, model servers. They start before any service's init script runs, so that the
#: init has something to migrate against. Everything at or above it is an application
#: process and is started only once initialisation has finished.
INIT_BARRIER = 30

#: Where a cron daemon reads schedules from. A file baked or bound here is what "the
#: service declares a crontab" means, and it brings the shared cron runtime with it.
CRON_PATHS = ("/etc/crontab", "/etc/cron.d", "/var/spool/cron")


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
    mode: ServiceMode
    replicas: int = 1
    prefix: str = ""
    ports: list = field(default_factory=list)
    local: dict[str, int] = field(default_factory=dict)
    """Slug -> port of every baked service, for ``{local:<slug>}`` in recipe strings."""

    @property
    def bakeable(self) -> bool:
        return self.mode is ServiceMode.BAKE


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
    specs: list[ServiceSpec],
    manifest: Manifest,
    registry: Registry,
    features: dict[str, bool],
    problems: list[str],
) -> list[Resolved]:
    resolved: list[Resolved] = []
    for spec in specs:
        entry = manifest.services.get(spec.slug)
        forced = entry.recipe if entry else ""
        try:
            recipe, is_fallback = registry.resolve(spec, forced=forced)
        except KeyError:
            problems.append(
                f"{spec.slug}: docker-bundle.yml asks for recipe {forced!r}, "
                f"which does not exist"
            )
            continue

        # A recipe that cannot be a process inside the bundle asks for a sidecar; the
        # configuration has the last word either way.
        default = ServiceMode.BAKE if recipe.bakeable else ServiceMode.SIDECAR
        mode = manifest.service_mode(spec.slug, features, default=default)
        if mode is ServiceMode.OFF:
            continue

        resolved.append(
            Resolved(
                spec=spec,
                recipe=recipe,
                is_fallback=is_fallback,
                mode=mode,
                replicas=entry.replicas if entry else 1,
            )
        )
    return resolved


def _copies_for(
    item: Resolved, port: int | None, features: dict[str, bool]
) -> tuple[list[CopyOp], dict[str, str]]:
    """Build the COPY operations for one service, from its recipe and its mounts.

    Also returns a map of original mount target -> where it ended up in the image, used
    to relocate paths that were meaningful in the source layout.
    """
    spec, recipe = item.spec, item.recipe
    values = _values(item, port)
    copies: list[CopyOp] = []
    consumed: set[str] = set()
    relocated: dict[str, str] = {}

    for rule in recipe.copies_for(features):
        dest = substitute(rule.dest, values)

        # Straight out of another image: nothing to stage locally, and nothing we could
        # check either — whether the path exists is the build's business.
        if rule.from_image:
            copies.append(
                CopyOp(
                    context="",
                    target=dest,
                    source=Path(),
                    kind=rule.kind,
                    chmod=rule.chmod,
                    from_image=substitute(rule.from_image, values),
                    from_path=substitute(rule.src, values),
                )
            )
            continue

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


def _publish_for(
    slug: str, assigned: list[PortSpec], manifest: Manifest, warnings: list[str]
) -> dict[int, str]:
    """The ``publish:`` overrides of one service, minus those naming no port it has."""
    entry = manifest.services.get(slug)
    if not entry or not entry.publish:
        return {}
    known = {port.original for port in assigned}
    for original in sorted(set(entry.publish) - known):
        warnings.append(
            f"{slug}: publish names port {original}, which the service does not listen on "
            f"(it has {', '.join(map(str, sorted(known))) or 'none'}); ignored"
        )
    return {original: spec for original, spec in entry.publish.items() if original in known}


def _values(item: Resolved, port: int | None) -> dict[str, object]:
    """The placeholders a recipe's strings may use for this service."""
    spec = item.spec
    return {
        "slug": spec.slug,
        "port": port,
        "name": spec.name,
        "package": spec.package,
        "prefix": item.prefix,
        # Lets a recipe lift files out of the very image the service runs, without
        # pinning the version a second time inside the recipe.
        "image": spec.effective_image,
        "params": item.recipe.params,
        "local": item.local,
    }


def _programs_for(
    item: Resolved,
    port: int | None,
    extra_env: dict[str, str],
    features: dict[str, bool],
    warnings: list[str],
) -> list[SupervisorProgram]:
    spec, recipe = item.spec, item.recipe
    values = _values(item, port)

    programs: list[SupervisorProgram] = []
    for rule in recipe.programs_for(features):
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


def _warn_shared_runtime(baked: list[Resolved], warnings: list[str]) -> None:
    """Point out services that share a runtime but asked for different versions of it.

    A shared runtime is installed once, from the base image's packages — the bundle gets
    Debian's PHP whatever tag the source compose named. Two services built against
    ``php:7.4-fpm-alpine`` and ``php:8.3-fpm-alpine`` therefore both end up on whichever
    version the base ships, and nothing about the build fails to say so. It is a warning
    and not an error because the version was never taken from those images in the first
    place; what is worth knowing is that one of the two is no longer running what it was
    written against.
    """
    groups: dict[str, dict[str, list[str]]] = {}
    for item in baked:
        if not item.recipe.shared:
            continue
        image = item.spec.effective_image
        if not image:
            continue
        groups.setdefault(item.recipe.shared, {}).setdefault(image, []).append(item.spec.slug)

    for runtime, by_image in sorted(groups.items()):
        if len(by_image) < 2:
            continue
        described = "; ".join(
            f"{image} ({', '.join(sorted(slugs))})" for image, slugs in sorted(by_image.items())
        )
        warnings.append(
            f"{runtime}: one runtime is installed for the whole image, but these services "
            f"were built against different ones — {described}. They will all run the "
            f"version the base image provides."
        )


def _collect_packages(
    recipe: Recipe,
    values: dict[str, object],
    features: dict[str, bool],
    install: dict[str, list[str]],
    run_steps: dict[str, list[str]],
) -> None:
    """Add a recipe's packages and image-wide RUN steps, each one once per family."""
    for fam, packages in recipe.install.items():
        bucket = install.setdefault(fam, [])
        for package in packages:
            if package not in bucket:
                bucket.append(package)
    for fam in recipe.run:
        bucket = run_steps.setdefault(fam, [])
        for command in recipe.runs_for(fam, features):
            rendered = substitute(command, values)
            if rendered not in bucket:
                bucket.append(rendered)


def _is_cron_path(target: str) -> bool:
    path = PurePosixPath(target)
    roots = [PurePosixPath(root) for root in CRON_PATHS]
    return any(path == root or root in path.parents for root in roots)


def _add_cron_runtime(
    plan: BundlePlan,
    resolved: dict[str, Resolved],
    manifest: Manifest,
    registry: Registry,
    features: dict[str, bool],
    shared_seen: set[str],
    install: dict[str, list[str]],
    run_steps: dict[str, list[str]],
    problems: list[str],
    warnings: list[str],
) -> list[str]:
    """Give the bundle one cron daemon when anything in it carries a schedule.

    No service matches the cron recipe: a crontab is a file a service brings, not a
    service of its own. Recipes only put it where cron reads schedules from, and this
    adds the daemon that reads them, once for the whole image. The service baking the
    first crontab carries the program — the first baked one, when the schedule comes in
    through a bind — the way the first nginx service carries the shared nginx master.

    Returns the names of the programs it added. Nothing waits for cron, so the caller
    keeps them out of the depends_on graph: a service depending on the carrier would
    otherwise start after the daemon too.
    """
    carrier = next(
        (s for s in plan.baked if any(_is_cron_path(copy.target) for copy in s.copies)),
        None,
    )
    if carrier is None:
        bound = any(_is_cron_path(bind.path) for bind in manifest.active_binds(features).values())
        if not bound or not plan.baked:
            return []
        carrier = plan.baked[0]

    recipe = registry.get("cron")
    if recipe is None or recipe.shared in shared_seen:
        return []
    if not recipe.supports(plan.family):
        problems.append(
            f"{carrier.spec.slug}: bakes a crontab, but recipe 'cron' does not support the "
            f"{plan.family!r} base; choose a different base or override the recipe"
        )
        return []

    # replicas belong to the carrier's own programs; the daemon is never scaled.
    item = replace(resolved[carrier.spec.slug], recipe=recipe, replicas=1)
    try:
        programs = _programs_for(item, None, {}, features, warnings)
        values = _values(item, None)
        owners = {
            program.name: service for service in plan.baked for program in service.programs
        }
        # Recipes written before the runtime was added by itself declare the program by
        # hand. Two [program:cron] sections would not even load, so theirs stays.
        declared = [program.name for program in programs if program.name in owners]
        if declared:
            for name in declared:
                owner = owners[name]
                warnings.append(
                    f"{owner.spec.slug}: recipe {owner.recipe_name!r} declares program "
                    f"{name!r} itself; the cron runtime is now added whenever a crontab is "
                    f"baked, so that entry can go"
                )
            return []
        _collect_packages(recipe, values, features, install, run_steps)
    except SubstitutionError as exc:
        problems.append(f"{carrier.spec.slug}: {exc}")
        return []

    if recipe.shared:
        shared_seen.add(recipe.shared)
    carrier.programs.extend(programs)
    return [program.name for program in programs]


def build(
    specs: list[ServiceSpec],
    manifest: Manifest,
    registry: Registry,
    *,
    variant: str = "cpu",
    features: dict[str, bool] | None = None,
) -> BundlePlan:
    """Produce the plan, or raise :class:`PlanError` listing everything that blocks it."""
    problems: list[str] = []
    warnings: list[str] = []
    features = dict(features if features is not None else manifest.features)

    resolved = _resolve_recipes(specs, manifest, registry, features, problems)
    if problems:
        raise PlanError(problems)

    prefixes = compute_prefixes(
        [item.spec for item in resolved],
        {slug: entry.env_prefix for slug, entry in manifest.services.items() if entry.env_prefix},
    )
    for item in resolved:
        item.prefix = prefixes[item.spec.slug]

    family = "debian" if manifest.base.get(variant, "").find("alpine") < 0 else "alpine"

    # A service kept outside the bundle contributes nothing to the image, and everything
    # to the deployment's `.env`: that is the whole point of saying `mode: external`
    # instead of switching it off.
    external = [item for item in resolved if item.mode is ServiceMode.EXTERNAL]
    inside = [item for item in resolved if item.mode is not ServiceMode.EXTERNAL]

    # -- mounts ---------------------------------------------------------
    for item in inside:
        mounts.classify(item.spec, item.recipe)
        entry = manifest.services.get(item.spec.slug)
        if entry and entry.mounts:
            warnings.extend(mounts.apply_overrides(item.spec, entry.mounts))

    # -- ports ----------------------------------------------------------
    baked = [item for item in inside if item.bakeable]
    _warn_shared_runtime(baked, warnings)

    allocation = ports.allocate(
        [(item.spec, item.recipe) for item in baked],
        pinned={slug: entry.ports for slug, entry in manifest.services.items() if entry.ports},
        port_range=manifest.port_range,
    )
    warnings.extend(allocation.warnings)
    problems.extend(allocation.errors)

    # -- environment ----------------------------------------------------
    assigned_ports = {
        slug: entries[0].container for slug, entries in allocation.ports.items() if entries
    }
    merged = envmerge.merge(
        [item.spec for item in resolved],
        globals_=manifest.globals,
        rules=manifest.active_env(features),
        prefixes=prefixes,
        ports=assigned_ports,
    )
    warnings.extend(merged.warnings)
    problems.extend(merged.errors)
    if merged.conflicts:
        problems.append(
            "unresolved environment conflicts — run `dockerbundle wizard`, or add "
            "decisions under env: in docker-bundle.yml:\n  "
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
        features=features,
        external=[item.spec for item in external],
        env=merged.entries,
        env_renames=merged.renames,
    )

    install: dict[str, list[str]] = {}
    run_steps: dict[str, list[str]] = {}
    shared_seen: set[str] = set()
    image_stages: dict[str, str] = {}

    for item in inside:
        spec, recipe = item.spec, item.recipe
        assigned = allocation.for_service(spec.slug)
        port = assigned[0].container if assigned else None
        item.local = assigned_ports
        values = _values(item, port)

        planned = PlannedService(
            spec=spec,
            recipe_name=recipe.name,
            bakeable=item.bakeable,
            ports=assigned,
            publish=_publish_for(spec.slug, assigned, manifest, warnings),
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

        # Every recipe string is expanded below; a {local:x} naming a service that is not
        # in the bundle is a mistake in the configuration, reported with the rest.
        try:
            planned.copies, relocated = _copies_for(item, port, features)
            planned.volumes = [mount for mount in spec.mounts if mount.mode is MountMode.VOLUME]

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
                    planned.programs = _programs_for(item, port, {}, features, warnings)
                else:
                    planned.programs = []
            else:
                planned.programs = _programs_for(item, port, extra_env, features, warnings)

            for rule in recipe.readiness_for(features):
                planned.readiness.append(
                    ReadinessProbe(
                        kind=rule.type,
                        target=substitute(rule.target, values),
                        timeout=rule.timeout,
                        label=spec.slug,
                    )
                )

            planned.build_steps = [
                substitute(command, values) for command in recipe.post_copy_for(features)
            ]
            planned.pre_init = [
                substitute(command, values) for command in recipe.pre_init_for(features)
            ]
            planned.post_init = [
                substitute(command, values) for command in recipe.post_init_for(features)
            ]

            if item.is_fallback and spec.effective_image:
                stage = fallback.stage_name(spec.slug)
                planned.stage = stage
                plan.stages.append((stage, spec.effective_image))
                plan.rootfs_stages.append(stage)

            # One stage per distinct image, however many recipes copy out of it.
            for copy in planned.copies:
                if not copy.from_image:
                    continue
                stage = image_stages.get(copy.from_image)
                if stage is None:
                    stage = f"img_{normalise_slug(copy.from_image)}"
                    image_stages[copy.from_image] = stage
                    plan.stages.append((stage, copy.from_image))
                copy.from_stage = stage

            _collect_packages(recipe, values, features, install, run_steps)
        except SubstitutionError as exc:
            problems.append(f"{spec.slug}: {exc}")
            continue

        # The service's own entrypoint runs verbatim at container start, exactly as its
        # author intended. It is never parsed or split. A recipe opts out with
        # `entrypoint: skip` when the script would not return — one ending in
        # `exec <server>` would hang the init phase — and starts the service itself.
        entrypoint = spec.find_entrypoint() if recipe.entrypoint != "skip" else None
        if entrypoint is not None:
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

    detached = _add_cron_runtime(
        plan,
        {item.spec.slug: item for item in inside},
        manifest,
        registry,
        features,
        shared_seen,
        install,
        run_steps,
        problems,
        warnings,
    )

    if problems:
        raise PlanError(problems)

    if not plan.baked and not plan.sidecars:
        raise PlanError(
            [
                "nothing is left to build: every service is either mode: off or held "
                "back by a when: that does not hold with these features"
            ]
        )

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
    programs_of = {
        p.spec.slug: [prog.name for prog in p.programs if prog.name not in detached]
        for p in plan.baked
    }
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
    # Declared by hand for state that has no mount of its own to classify.
    for name, target in manifest.active_volumes(features).items():
        existing = plan.named_volumes.get(name)
        if existing is not None and existing != target:
            problems.append(
                f"volumes.{name}: already mounted at {existing} by a service; one volume "
                f"cannot also be {target}. Rename it."
            )
            continue
        plan.named_volumes[name] = target
    plan.binds = _binds(manifest, features, plan, problems, warnings)

    if problems:
        raise PlanError(problems)

    plan.install = install
    plan.run_steps = run_steps
    plan.port_map = dict(allocation.ports)
    plan.warnings = warnings

    for item in inside:
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

    for spec in plan.external:
        warnings.append(
            f"{spec.slug}: left outside the bundle; its keys stay in .env.example so the "
            f"deployment can point at wherever it actually runs"
        )

    return plan


def _volume_name(slug: str, mount) -> str:
    """Name a named volume after the service and what it holds."""
    if mount.named:
        return mount.source
    leaf = mount.target.rstrip("/").rsplit("/", 1)[-1] or "data"
    # `/var/lib/mysql` for the `mysql` service would otherwise become `mysql_mysql`.
    return f"{slug}_data" if leaf == slug else f"{slug}_{leaf}"


def _overlaps(a: str, b: str) -> bool:
    left, right = PurePosixPath(a), PurePosixPath(b)
    return left == right or left in right.parents or right in left.parents


def _binds(
    manifest: Manifest,
    features: dict[str, bool],
    plan: BundlePlan,
    problems: list[str],
    warnings: list[str],
) -> list[PlannedBind]:
    """Turn ``binds:`` into mounts, checked against what the services already mount.

    The manifest itself refuses overlaps among its own entries; the volumes a service's
    compose file declared are only known here.
    """
    planned: list[PlannedBind] = []
    env_keys = {entry.key for entry in plan.env}
    for name, bind in manifest.active_binds(features).items():
        for volume, target in plan.named_volumes.items():
            if _overlaps(bind.path, target):
                problems.append(
                    f"binds.{name}: {bind.path} overlaps volume {volume} at {target}; "
                    f"one of them would hide the other"
                )
        source = None
        if bind.ship:
            source = manifest.resolve(bind.ship)
            if not source.exists():
                problems.append(f"binds.{name}: ship {bind.ship} does not exist ({source})")
                continue
        # A bind hides whatever the image has at that path. Baking it as well is not an
        # error, just wasted layers and a trap: edits to the baked copy never show.
        for service in plan.baked:
            for copy in service.copies:
                if _overlaps(bind.path, copy.target):
                    warnings.append(
                        f"binds.{name}: {copy.target} is baked by {service.spec.slug} but "
                        f"the bind at {bind.path} hides it in the container"
                    )
        item = PlannedBind(
            name=name,
            target=bind.path,
            host=bind.host,
            source=source,
            is_file=bool(source and source.is_file()),
        )
        if item.env in env_keys:
            problems.append(
                f"binds.{name}: {item.env} is already a key of a service's .env; "
                f"rename the bind"
            )
            continue
        planned.append(item)
    return planned
