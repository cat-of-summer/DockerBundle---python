"""Build and run a real bundle.

The unit and golden tests prove the generator emits what we intended; only an actual
``docker build`` proves what we intended is valid. This uses a deliberately small stack
(nginx + redis, both plain distro packages) so it stays a few minutes rather than the
half hour a PHP or CUDA bundle would take.

Skipped automatically when no Docker daemon is reachable.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from core.manifest import Manifest, ServiceEntry, SourceRef
from discover import resolve
from plan import builder
from render import writer
from tests.conftest import requires_docker

pytestmark = [pytest.mark.docker, requires_docker]

COMPOSE = """\
services:
  web:
    image: nginx:alpine
    ports:
      - "127.0.0.1:8080:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/site.conf
  cache:
    image: redis:alpine
    ports:
      - "127.0.0.1:6379:6379"
    volumes:
      - ./data:/data
"""

NGINX_CONF = """\
server {
    listen 80;
    server_name _;
    location / { return 200 'bundle ok\\n'; }
}
"""


def run(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture
def smoke_project(tmp_path):
    project = tmp_path / "smoke"
    project.mkdir()
    (project / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (project / "nginx.conf").write_text(NGINX_CONF, encoding="utf-8")
    (project / "data").mkdir()
    (project / ".env.example").write_text("NETWORK=network\nINSTANCE=\n", encoding="utf-8")
    return project


def test_generated_bundle_builds_and_runs(smoke_project, registry, tmp_path):
    discovery = resolve.collect(
        [SourceRef(type="compose", path="docker-compose.yml")], smoke_project
    )
    specs = discovery.services
    assert {spec.slug for spec in specs} == {"smoke_web", "smoke_cache"}

    manifest = Manifest(
        name="smoke",
        services={spec.slug: ServiceEntry(slug=spec.slug) for spec in specs},
    )
    plan = builder.build(specs, manifest, registry)
    output = tmp_path / "dist"
    writer.render(plan, output, image_ref="dockerbundle-smoke:test")

    tag = f"dockerbundle-smoke:{uuid.uuid4().hex[:8]}"
    build = run("docker", "build", "-t", tag, str(output))
    assert build.returncode == 0, (
        f"docker build failed:\n{build.stdout[-4000:]}\n{build.stderr[-4000:]}"
    )

    name = f"dockerbundle-smoke-{uuid.uuid4().hex[:8]}"
    try:
        started = run("docker", "run", "-d", "--name", name, tag, timeout=120)
        assert started.returncode == 0, started.stderr

        healthy = False
        for _ in range(30):
            probe = run("docker", "exec", name, "/usr/local/bin/bundle-healthcheck.sh", timeout=60)
            if probe.returncode == 0:
                healthy = True
                break
            time.sleep(2)

        logs = run("docker", "logs", name).stdout
        assert healthy, f"healthcheck never passed.\nlogs:\n{logs[-4000:]}"

        served = run("docker", "exec", name, "curl", "-fsS", "http://127.0.0.1:80/")
        assert "bundle ok" in served.stdout, served.stdout + served.stderr

        redis = run("docker", "exec", name, "redis-cli", "-p", "6379", "ping")
        assert "PONG" in redis.stdout, redis.stdout + redis.stderr
    finally:
        run("docker", "rm", "-f", name, timeout=120)
        run("docker", "rmi", "-f", tag, timeout=120)
