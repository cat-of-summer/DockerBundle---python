"""Drive a running bundle's supervisord from outside the container.

Lets an operator restart one service without bouncing the whole bundle — the main thing
you give up by collapsing a stack into a single container, handed back.
"""

from __future__ import annotations

import subprocess

SOCKET = "unix:///run/bundle/supervisor.sock"


def command(container: str, args: list[str]) -> list[str]:
    return ["docker", "exec", container, "supervisorctl", "-s", SOCKET, *args]


def run(container: str, args: list[str], *, timeout: int = 30) -> tuple[str, int]:
    """Run supervisorctl inside ``container`` and return its combined output and status."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command(container, args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return ("docker is not on PATH", 127)
    except subprocess.TimeoutExpired:
        return (f"timed out talking to {container}", 124)

    output = completed.stdout + completed.stderr
    if completed.returncode != 0 and "No such container" in output:
        return (f"no container named {container!r} is running", completed.returncode)
    return (output, completed.returncode)
