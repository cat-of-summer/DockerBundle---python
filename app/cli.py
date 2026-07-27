"""Typer command line.

Every command works without the TUI, because CI calls this and CI has no terminal.
``generate --yes`` is the entry point a GitHub Actions ``BUILD_COMMAND`` uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from core import config as config_mod
from core.manifest import Manifest, ManifestError
from core.paths import MANIFEST_NAME
from core.version import __version__
from ui.i18n import t

app = typer.Typer(
    name="dockerbundle",
    help="Bake several docker-compose services into one supervisord-managed image.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
errors = Console(stderr=True)

EXIT_BLOCKED = 2
EXIT_USAGE = 3


def _manifest_path(explicit: Path | None) -> Path:
    return explicit if explicit else Path.cwd() / MANIFEST_NAME


def _load_manifest(path: Path) -> Manifest:
    if not path.is_file():
        errors.print(f"[red]{t('cli.no_manifest', path=path)}[/red]")
        raise typer.Exit(EXIT_USAGE)
    try:
        return Manifest.load(path)
    except ManifestError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_USAGE) from exc


def _show_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        errors.print(f"[yellow]![/yellow] {warning}")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
    language: str = typer.Option("", "--lang", help="Interface language (en, ru)."),
) -> None:
    if language:
        from ui import i18n

        i18n.set_language(language)
    if version:
        console.print(f"dockerbundle {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # In a configured project, opening the wizard is the friendly default. Anywhere
        # else — notably a double-clicked .exe in some arbitrary folder — the useful
        # answer is the help text, not a wizard complaining about a missing manifest.
        if (Path.cwd() / MANIFEST_NAME).is_file():
            _run_wizard(None, False)
        else:
            # typer.echo, not console.print: rich would try to read `[OPTIONS]` and
            # `[ARGS]` in the help text as style markup.
            typer.echo(ctx.get_help())


@app.command()
def init(
    path: Path = typer.Option(None, "--manifest", "-m", help="Where to write bundle.yml."),
    source: list[str] = typer.Option(
        [], "--source", "-s", help="Catalogue directory or compose file to scan. Repeatable."
    ),
    name: str = typer.Option("", "--name", help="Bundle name. Defaults to the directory name."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing bundle.yml."),
) -> None:
    """Create a bundle.yml in the current project."""
    target = _manifest_path(path)
    if target.exists() and not force:
        errors.print(f"[red]{t('cli.manifest_exists', path=target)}[/red]")
        raise typer.Exit(EXIT_USAGE)

    from core.manifest import SourceRef

    refs: list[SourceRef] = []
    for entry in source:
        candidate = Path(entry)
        if candidate.is_file():
            refs.append(SourceRef(type="compose", path=entry))
        elif candidate.is_dir():
            refs.append(SourceRef(type="catalog", path=entry))
        else:
            refs.append(SourceRef(type="image", ref=entry))

    if not refs:
        here = target.parent
        for candidate in ("compose.yaml", "compose.yml", "docker-compose.yml"):
            if (here / candidate).is_file():
                refs.append(SourceRef(type="compose", path=candidate))
                break

    manifest = Manifest(name=name or target.parent.name.lower(), sources=refs)
    manifest.save(target)

    console.print(f"[green]{t('cli.init_done', path=target)}[/green]")
    if not refs:
        console.print(t("cli.init_no_sources"))


@app.command()
def scan(
    path: Path = typer.Option(None, "--manifest", "-m"),
    source: list[str] = typer.Option(
        [], "--source", "-s", help="Scan this instead of bundle.yml."
    ),
    pull: bool = typer.Option(False, "--pull", help="Pull images that are missing locally."),
) -> None:
    """List the services the configured sources provide, and the recipe each matches."""
    from app import pipeline
    from core.manifest import SourceRef

    if source:
        manifest = Manifest(
            sources=[
                SourceRef(type="catalog" if Path(s).is_dir() else "compose", path=s)
                for s in source
            ],
            path=Path.cwd() / MANIFEST_NAME,
        )
    else:
        manifest = _load_manifest(_manifest_path(path))

    context = pipeline.load(manifest, pull=pull)

    table = Table(title=t("cli.scan_title", count=len(context.discovery.services)))
    table.add_column(t("cli.col_service"), style="cyan", no_wrap=True)
    table.add_column(t("cli.col_image"))
    table.add_column(t("cli.col_recipe"), style="magenta")
    table.add_column(t("cli.col_where"))

    for spec in context.discovery.services:
        entry = manifest.services.get(spec.slug)
        forced = entry.recipe if entry else ""
        recipe, is_fallback = context.registry.resolve(spec, forced=forced)
        override = entry.bakeable if entry else None
        bakeable = recipe.bakeable if override is None else override
        where = t("cli.in_image") if bakeable else t("cli.sidecar")
        name = recipe.name + (" *" if is_fallback else "")
        table.add_row(spec.slug, spec.effective_image or "-", name, where)

    console.print(table)
    _show_warnings(context.warnings)
    used_fallback = any(context.registry.resolve(s)[1] for s in context.discovery.services)
    if used_fallback:
        console.print(t("cli.scan_fallback_note"))


@app.command()
def generate(
    path: Path = typer.Option(None, "--manifest", "-m"),
    output: Path = typer.Option(None, "--output", "-o", help="Override the output directory."),
    variant: str = typer.Option("", "--variant", help="Which base to build against (cpu, cuda)."),
    image: str = typer.Option(
        "", "--image", help="Image reference written into compose and .env."
    ),
    pull: bool = typer.Option(False, "--pull", help="Pull images that are missing locally."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Non-interactive: fail instead of asking anything."
    ),
) -> None:
    """Render bundle.yml into dist/. This is what CI runs."""
    from app import pipeline

    manifest = _load_manifest(_manifest_path(path))
    context = pipeline.load(manifest, pull=pull)
    pipeline.ensure_entries(context)

    if not context.selected:
        errors.print(f"[red]{t('cli.nothing_selected')}[/red]")
        raise typer.Exit(EXIT_BLOCKED)

    chosen = variant or (manifest.variants[0] if manifest.variants else "cpu")
    if chosen not in manifest.base:
        errors.print(f"[red]{t('cli.unknown_variant', variant=chosen)}[/red]")
        raise typer.Exit(EXIT_USAGE)

    try:
        written = pipeline.generate(context, variant=chosen, image_ref=image, output=output)
    except pipeline.PlanError as exc:
        errors.print(f"[red]{t('cli.blocked')}[/red]")
        for problem in exc.problems:
            errors.print(f"  [red]-[/red] {problem}")
        if not yes:
            errors.print(t("cli.blocked_hint"))
        raise typer.Exit(EXIT_BLOCKED) from exc

    _show_warnings(written.warnings)
    console.print(f"[green]{t('cli.generated', path=written.directory)}[/green]")
    console.print(t("cli.build_hint", path=written.directory))


def _run_wizard(path: Path | None, pull: bool) -> None:
    """Shared body so the bare invocation and the subcommand behave identically.

    Kept out of the Typer command because ``ctx.invoke`` on a Typer command skips Click's
    parameter processing and hands the function its ``OptionInfo`` sentinels instead of
    real values.
    """
    target = _manifest_path(path)
    if not target.is_file():
        console.print(t("cli.wizard_needs_init", path=target))
        raise typer.Exit(EXIT_USAGE)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        errors.print(f"[red]{t('cli.wizard_needs_tty')}[/red]")
        raise typer.Exit(EXIT_USAGE)

    from app.wizard.app import run

    manifest = _load_manifest(target)
    run(manifest, pull=pull)


@app.command()
def wizard(
    path: Path = typer.Option(None, "--manifest", "-m"),
    pull: bool = typer.Option(False, "--pull"),
) -> None:
    """Edit bundle.yml interactively."""
    _run_wizard(path, pull)


@app.command()
def doctor() -> None:
    """Check the environment: Docker, recipes, manifest."""
    from discover import dockerclient
    from recipes.match import Registry

    table = Table(title=t("cli.doctor_title"))
    table.add_column(t("cli.col_check"), style="cyan")
    table.add_column(t("cli.col_status"))

    docker_ok = dockerclient.available()
    docker_status = (
        f"[green]{t('cli.ok')}[/green]"
        if docker_ok
        else f"[yellow]{t('cli.docker_missing')}[/yellow]"
    )
    table.add_row("Docker", docker_status)

    manifest_path = Path.cwd() / MANIFEST_NAME
    table.add_row(
        MANIFEST_NAME,
        f"[green]{manifest_path}[/green]"
        if manifest_path.is_file()
        else f"[yellow]{t('cli.not_found')}[/yellow]",
    )

    registry = Registry.load(Path.cwd())
    table.add_row("recipes", f"[green]{len(registry.recipes)}[/green]")

    console.print(table)
    _show_warnings(registry.warnings)


@app.command()
def ps(
    container: str = typer.Argument(..., help="Bundle container name or id."),
) -> None:
    """Show the supervisord programs inside a running bundle."""
    from ops import supervisorctl

    output, code = supervisorctl.run(container, ["status"])
    console.print(output.rstrip())
    raise typer.Exit(code if code in (0, 3) else code)


@app.command()
def restart(
    container: str = typer.Argument(..., help="Bundle container name or id."),
    program: str = typer.Argument(..., help="Program name, or 'all'."),
) -> None:
    """Restart one service inside a bundle without restarting the container."""
    from ops import supervisorctl

    output, code = supervisorctl.run(container, ["restart", program])
    console.print(output.rstrip())
    raise typer.Exit(code)


@app.command()
def logs(
    container: str = typer.Argument(..., help="Bundle container name or id."),
    program: str = typer.Argument(..., help="Program name."),
    lines: int = typer.Option(200, "--lines", "-n"),
) -> None:
    """Show one service's log from inside a bundle."""
    from ops import supervisorctl

    output, code = supervisorctl.run(container, ["tail", f"-{lines}", program])
    console.print(output.rstrip())
    raise typer.Exit(code)


@app.command("set-language")
def set_language(code: str = typer.Argument(..., help="en or ru")) -> None:
    """Remember an interface language for future runs."""
    from ui import i18n

    options = ", ".join(i18n.available_languages())
    normalised = i18n.normalise(code)
    if not normalised:
        errors.print(
            f"[red]{t('cli.bad_language', code=code, options=options)}[/red]"
        )
        raise typer.Exit(EXIT_USAGE)

    settings = config_mod.Config.load()
    settings.language = normalised
    settings.save()
    i18n.set_language(normalised)
    console.print(f"[green]{t('cli.language_set', code=normalised)}[/green]")
