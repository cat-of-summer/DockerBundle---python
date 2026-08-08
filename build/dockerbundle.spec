# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the dockerbundle binary.

Artifacts are named ``dockerbundle-<os>-<arch>`` so a GitHub release can carry Windows
and Linux builds side by side. Set DOCKERBUNDLE_ARTIFACT_NAME to override.
"""

import os
import platform
import sys
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(SPEC))).parent

PROJECT = "dockerbundle"

OS_NAMES = {
    "win32": "windows",
    "cygwin": "windows",
    "darwin": "macos",
    "linux": "linux",
}

ARCH_NAMES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
}


def resolve_os():
    for prefix, name in OS_NAMES.items():
        if sys.platform.startswith(prefix):
            return name
    return sys.platform


def resolve_arch():
    machine = platform.machine().lower()
    return ARCH_NAMES.get(machine, machine or "unknown")


ARTIFACT_NAME = (
    os.environ.get("DOCKERBUNDLE_ARTIFACT_NAME")
    or f"{PROJECT}-{resolve_os()}-{resolve_arch()}"
)

# Everything looked up through core.paths.resource_dir has to be bundled, because those
# directories do not exist next to a onefile binary.
DATAS = []
DATAS += [(str(p), "lang") for p in sorted((ROOT / "lang").glob("*.json"))]
DATAS += [(str(p), "recipes/builtin") for p in sorted((ROOT / "recipes" / "builtin").glob("*.yml"))]
DATAS += [(str(p), "render/templates") for p in sorted((ROOT / "render" / "templates").glob("*.j2"))]
DATAS += [(str(p), "render/assets") for p in sorted((ROOT / "render" / "assets").glob("*"))]

HIDDEN = [
    "app.cli",
    "app.pipeline",
    "app.shell",
    "app.wizard.app",
    "core.config",
    "core.log",
    "core.manifest",
    "core.model",
    "core.paths",
    "core.version",
    "discover.catalog",
    "discover.composefile",
    "discover.dockerclient",
    "discover.dockerfile",
    "discover.dockerps",
    "discover.envfile",
    "discover.imageref",
    "discover.interpolate",
    "discover.resolve",
    "ops.supervisorctl",
    "plan.builder",
    "plan.envmerge",
    "plan.graph",
    "plan.mounts",
    "plan.ports",
    "plan.substitute",
    "recipes.fallback",
    "recipes.match",
    "recipes.schema",
    "render.writer",
    "ui.i18n",
]

analysis = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "test",
        "lib2to3",
        "pydoc_data",
        "numpy",
        "PIL",
        "pytest",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name=ARTIFACT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
