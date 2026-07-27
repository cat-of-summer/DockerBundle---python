"""Scan a directory whose subfolders are each a self-contained compose package.

This is the shape of ``Desktop/packages``: one folder per package, holding a compose
file plus the Dockerfile, entrypoint and configs it mounts. Folders are often prefixed
with an ordering number (``3. mysql``, ``5. hf-text``); the prefix is a sort key for
humans and is stripped before deriving slugs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.model import Origin, ServiceSpec
from discover import composefile

#: ``3. mysql`` / ``05 - redis`` — a leading ordering prefix, not part of the name.
_ORDER_PREFIX = re.compile(r"^\s*\d+\s*[.)\-]\s*")

#: Directories that never hold a package.
_SKIP_DIRS = frozenset(
    {".git", ".github", ".idea", ".vscode", "node_modules", "venv", ".venv", "dist", "__pycache__"}
)


def strip_order_prefix(name: str) -> str:
    """``"3. mysql"`` -> ``"mysql"``; names without a prefix pass through unchanged."""
    stripped = _ORDER_PREFIX.sub("", name).strip()
    return stripped or name


@dataclass
class PackageDir:
    """A package folder and the compose file found inside it."""

    path: Path
    compose: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def slug_base(self) -> str:
        return strip_order_prefix(self.path.name)


def find_packages(root: Path) -> list[PackageDir]:
    """Return every immediate subdirectory of ``root`` that holds a compose file."""
    if not root.is_dir():
        return []

    packages: list[PackageDir] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name in _SKIP_DIRS or entry.name.startswith("."):
            continue
        compose = composefile.find_compose_file(entry)
        if compose is not None:
            packages.append(PackageDir(path=entry, compose=compose))
    return packages


def load(root: Path) -> tuple[list[ServiceSpec], list[str]]:
    """Load every package under ``root``.

    Returns the services alongside human-readable warnings; one malformed package must
    not stop the rest of a 23-package catalogue from being usable.
    """
    specs: list[ServiceSpec] = []
    warnings: list[str] = []

    for package in find_packages(root):
        try:
            specs.extend(
                composefile.load_services(
                    package.compose,
                    package=package.name,
                    slug_base=package.slug_base,
                    origin=Origin.CATALOG,
                )
            )
        except composefile.ComposeError as exc:
            warnings.append(f"{package.name}: {exc}")

    return specs, warnings
