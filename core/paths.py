"""Filesystem locations for bundled resources and user state.

Resources (``lang/``, ``recipes/builtin/``, ``render/templates/``) ship inside the
PyInstaller onefile bundle and are extracted to ``sys._MEIPASS`` at runtime, so every
lookup has to go through :func:`resource_dir` rather than ``__file__`` arithmetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "dockerbundle"

#: Name of the per-project configuration, resolved relative to the current directory.
#: One file: sources, service selection, recipes and environment decisions.
MANIFEST_NAME = "docker-bundle.yml"

#: Name of the lock file written beside the generated output.
LOCK_NAME = "docker-bundle.lock.yml"

#: Default output directory, relative to the project root.
DEFAULT_OUTPUT_DIRNAME = "dist"


def _bundle_root() -> Path | None:
    """Return the PyInstaller extraction directory, or ``None`` when running from source."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else None


def project_root() -> Path:
    """Return the source tree root (the directory holding ``main.py``)."""
    return Path(__file__).resolve().parent.parent


def resource_dir(name: str) -> Path:
    """Return the directory of a bundled resource set, e.g. ``"lang"``.

    ``name`` may be a nested path such as ``"recipes/builtin"``.
    """
    root = _bundle_root() or project_root()
    return root.joinpath(*name.split("/"))


def state_home() -> Path:
    """Return the directory holding global user state (language, known sources)."""
    override = os.environ.get("DOCKERBUNDLE_HOME")
    if override:
        return Path(override)
    return Path.home() / f".{APP_NAME}"


def config_file() -> Path:
    return state_home() / "config.json"


def log_file() -> Path:
    return state_home() / f"{APP_NAME}.log"
