"""Interactive prompt for the bare invocation.

A double-clicked .exe gets a console that Windows destroys the instant the process
exits, so a run that only prints help is unreadable. Instead of trying to hold that
console open, the bare invocation opens a prompt and keeps running until the user says
``exit`` — the same shape the sibling ccas project uses.

Commands are dispatched through the very same Typer app the command line uses, so there
is one definition of every command. ``cd`` is handled here because every command resolves
its paths against ``Path.cwd()``: switching directory switches the whole project, which
is what makes the binary usable from wherever it happens to sit.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from core.paths import MANIFEST_NAME
from core.version import __version__
from ui.i18n import t

PROMPT = "dockerbundle> "
QUIT_WORDS = {"exit", "quit", "q"}
QUOTES = ("'", '"')


def interactive() -> bool:
    """True when both ends of the console are a terminal we can prompt on."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in QUOTES:
        return token[1:-1]
    return token


def split(line: str) -> list[str]:
    """Split a prompt line into argv.

    POSIX splitting eats backslashes, which would turn ``--source ..\\packages`` into
    ``..packages``. On Windows the non-POSIX mode keeps them, at the price of leaving
    quotes attached to the token — so strip those back off by hand.
    """
    if os.name == "nt":
        return [_unquote(token) for token in shlex.split(line, posix=False)]
    return shlex.split(line)


def _print_location() -> None:
    from app.cli import console

    cwd = Path.cwd()
    console.print(t("cli.shell.cwd", path=cwd))
    manifest = cwd / MANIFEST_NAME
    if manifest.is_file():
        console.print(t("cli.shell.manifest", path=manifest))
    else:
        console.print(t("cli.shell.no_manifest", name=MANIFEST_NAME))


def _change_directory(raw: str) -> None:
    from app.cli import console, errors

    target = _unquote(raw.strip())
    if not target:
        _print_location()
        return

    try:
        os.chdir(Path(target).expanduser())
    except OSError:
        errors.print(f"[red]{t('cli.shell.no_dir', path=target)}[/red]")
        return

    _print_location()


def _dispatch(tokens: list[str]) -> None:
    from app.cli import app, console

    try:
        app(args=tokens)
    except SystemExit:
        # standalone_mode is on, so click has already reported whatever went wrong and
        # turned it into SystemExit — including --help and every typer.Exit a command
        # raises. Swallowing it keeps the prompt alive; the exit code is meaningless here.
        pass
    except KeyboardInterrupt:
        console.print()


def run() -> int:
    from app.cli import console

    console.print(t("cli.shell.banner", version=__version__))
    _print_location()
    console.print()
    console.print(t("cli.shell.hint"))
    console.print()

    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0

        if not line:
            continue
        if line.lower() in QUIT_WORDS:
            return 0

        head, _, rest = line.partition(" ")
        head = head.lower()
        if head == "pwd":
            _print_location()
            console.print()
            continue
        if head == "cd":
            # The remainder is taken verbatim rather than split: pasted Windows paths
            # carry spaces far more often than they carry quotes.
            _change_directory(rest)
            console.print()
            continue
        if Path(_unquote(line)).expanduser().is_dir():
            # A pasted path on its own line means "go there".
            _change_directory(line)
            console.print()
            continue

        try:
            tokens = split(line)
        except ValueError:
            console.print(t("cli.shell.unknown"))
            console.print()
            continue

        if tokens:
            _dispatch(tokens)
        console.print()
