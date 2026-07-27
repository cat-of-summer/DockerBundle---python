"""Shared fixtures.

Tests never touch the user's real Docker daemon or home directory: ``DOCKERBUNDLE_HOME``
is redirected to a temp dir and the daemon client is reset between tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="Rewrite the golden files from the current output instead of comparing.",
    )


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKERBUNDLE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOCKERBUNDLE_LANG", "en")

    from discover import dockerclient
    from ui import i18n

    dockerclient.reset()
    i18n.set_language("en")
    yield
    dockerclient.reset()


@pytest.fixture
def catalog(tmp_path) -> Path:
    """A writable copy of the sample package catalogue."""
    destination = tmp_path / "packages"
    shutil.copytree(FIXTURES / "packages", destination)
    return destination


@pytest.fixture
def registry():
    from recipes.match import Registry

    return Registry.load()


def docker_available() -> bool:
    if os.environ.get("DOCKERBUNDLE_SKIP_DOCKER"):
        return False
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_docker = pytest.mark.skipif(
    not docker_available(), reason="needs a reachable Docker daemon"
)
