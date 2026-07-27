"""Shared access to the Docker daemon.

Every daemon-backed feature is optional: generation must work on a machine with no
Docker at all, falling back to what the compose files say. So callers ask for a client
and handle :class:`DockerUnavailable` rather than assuming one exists.
"""

from __future__ import annotations

import functools
from typing import Any

DAEMON_HINT = (
    "cannot reach the Docker daemon — is Docker running? "
    "Generation still works without it, using compose files only."
)


class DockerUnavailable(RuntimeError):
    """The Docker daemon could not be reached."""


@functools.lru_cache(maxsize=1)
def _cached_client() -> Any:
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise DockerUnavailable(f"docker SDK is not installed: {exc}") from exc

    try:
        instance = docker.from_env()
        instance.ping()
    except Exception as exc:
        raise DockerUnavailable(f"{DAEMON_HINT} ({exc})") from exc
    return instance


def client() -> Any:
    """Return a connected Docker client, or raise :class:`DockerUnavailable`."""
    return _cached_client()


def available() -> bool:
    try:
        client()
    except DockerUnavailable:
        return False
    return True


def reset() -> None:
    """Drop the cached client. Used by tests."""
    _cached_client.cache_clear()
