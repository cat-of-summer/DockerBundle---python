"""dockerbundle entry point.

Mirrors the layout of the sibling ccas project: pick the language before anything can
print, then hand off to the CLI. Imports stay inside the functions so that a PyInstaller
binary starts quickly and a missing optional dependency only breaks the command that
needs it.
"""

from __future__ import annotations

import os
import sys


def _init_language() -> None:
    from ui import i18n

    language = ""
    try:
        from core.config import Config

        language = Config.load().language
    except (OSError, ValueError):
        language = ""

    i18n.set_language(language or None)


def owns_console() -> bool:
    """True when this process created the console window it is printing to.

    Double-clicking a .exe in Explorer gives it a fresh console that Windows destroys the
    moment the process exits, so everything printed vanishes before it can be read.
    Launching the same binary from cmd or PowerShell attaches it to an existing console
    instead, and that one must not be held open.

    ``GetConsoleProcessList`` distinguishes the two: a count of 1 means we are the only
    process on this console, so it is ours and closing it loses the output.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # The buffer only has to be big enough to tell "1" from "more than 1".
        buffer = (wintypes.DWORD * 4)()
        return kernel32.GetConsoleProcessList(buffer, len(buffer)) == 1
    except (AttributeError, OSError, ValueError):
        return False


def _pause_if_launched_by_double_click() -> None:
    if not owns_console():
        return
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return
    except (AttributeError, ValueError):
        return

    import contextlib

    from ui.i18n import t

    # Closing regardless is fine: the pause is a courtesy, not a step that can fail.
    with contextlib.suppress(EOFError, KeyboardInterrupt, OSError):
        input(f"\n{t('cli.press_enter')}")


def main(argv: list[str] | None = None) -> int:
    _init_language()

    from app.cli import app

    # standalone_mode=True on purpose: click then converts its own Exit/Abort exceptions
    # into SystemExit with the right status. Handling them by hand loses the exit code —
    # a blocked `generate` would report success and CI would build nothing.
    try:
        app(args=argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    return 0


if __name__ == "__main__":
    code = main()
    _pause_if_launched_by_double_click()
    sys.exit(code)
