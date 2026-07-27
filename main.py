"""dockerbundle entry point.

Mirrors the layout of the sibling ccas project: pick the language before anything can
print, then hand off to the CLI. Imports stay inside the functions so that a PyInstaller
binary starts quickly and a missing optional dependency only breaks the command that
needs it.
"""

from __future__ import annotations

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
    sys.exit(main())
