"""The rendered scripts behave right where a text comparison with the golden copy cannot tell."""
import re
import subprocess
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent.parent / "golden" / "shop"


def _check_program() -> str:
    text = (GOLDEN / "healthcheck.sh").read_text(encoding="utf-8")
    match = re.search(r"^check_program\(\) \{.*?^\}$", text, re.M | re.S)
    assert match, "check_program is gone from healthcheck.sh"
    return match.group(0)


@pytest.mark.parametrize(
    ("name", "states", "healthy"),
    [
        ("worker:*", ["RUNNING", "RUNNING"], True),
        ("worker:*", ["RUNNING", "STOPPED"], True),
        ("worker:*", ["RUNNING", "FATAL"], False),
        ("worker:*", ["RUNNING", "BACKOFF"], False),
        ("worker:*", ["STOPPED", "STOPPED"], False),
        ("api", ["RUNNING"], True),
        ("api", ["STOPPED"], False),
        ("api", [], False),
    ],
)
def test_scaled_group_is_healthy_with_a_running_process(name, states, healthy):
    lines = "".join(f"p:{i} {state} x\\n" for i, state in enumerate(states))
    script = f"""
SOCK=unused
supervisorctl() {{ printf '{lines}'; }}
{_check_program()}
check_program '{name}'
"""
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is healthy, result.stdout + result.stderr


def test_entrypoint_waits_until_supervisord_has_stopped():
    text = (GOLDEN / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'while kill -0 "$SUPERVISORD_PID"' in text
    compose = (GOLDEN / "docker-compose.yml").read_text(encoding="utf-8")
    assert "stop_grace_period: ${STOP_GRACE_PERIOD:-120s}" in compose
