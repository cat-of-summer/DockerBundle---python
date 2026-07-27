"""Compare a generated ``dist/`` against committed expected output.

Golden files make every change to a template or a recipe visible in review, which is the
only practical way to keep an eye on generated shell and Dockerfiles.

Regenerate after an intentional change::

    docker/dev.sh pytest tests/test_golden.py --update-golden
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.manifest import Manifest, ServiceEntry, SourceRef
from discover import resolve
from plan import builder
from render import writer

GOLDEN = Path(__file__).parent / "golden" / "shop"

SLUGS = [
    "mysql",
    "laravel_nginx_laravel",
    "laravel_nginx_nginx",
    "vue_nginx_vite_vue",
    "vue_nginx_vite_nginx",
    "traefik",
]


@pytest.fixture
def generated(catalog, registry, tmp_path) -> Path:
    discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
    specs = [spec for spec in discovery.services if spec.slug in SLUGS]

    manifest = Manifest(
        name="shop",
        env_conflicts={"EXTERNAL_ACCESS": "prefix", "VITE_API_BASE_URL": "value:/api"},
        services={spec.slug: ServiceEntry(slug=spec.slug) for spec in specs},
    )
    plan = builder.build(specs, manifest, registry)

    output = tmp_path / "dist"
    writer.render(plan, output, image_ref="ghcr.io/acme/shop-bundle:test")
    return output


#: Only the rendered artefacts are compared. context/ holds verbatim copies of the
#: fixture files, which the copy tests already cover.
COMPARED = (
    "Dockerfile",
    "supervisord.conf",
    "entrypoint.sh",
    "healthcheck.sh",
    "docker-compose.yml",
    ".env.example",
    "bundle.lock.yml",
)


def test_output_matches_golden(generated, request):
    if request.config.getoption("--update-golden", default=False):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for name in COMPARED:
            shutil.copyfile(generated / name, GOLDEN / name)
        pytest.skip("golden files updated")

    missing = [name for name in COMPARED if not (GOLDEN / name).is_file()]
    if missing:
        pytest.fail(f"no golden files for {missing}; run pytest --update-golden")

    for name in COMPARED:
        actual = (generated / name).read_text(encoding="utf-8")
        expected = (GOLDEN / name).read_text(encoding="utf-8")
        assert actual == expected, f"{name} differs from the golden copy"


def test_generation_is_deterministic(catalog, registry, tmp_path):
    # Two runs from the same inputs must be byte-identical, or golden tests and
    # `git diff` on a regenerated dist/ are both worthless.
    outputs = []
    for index in range(2):
        discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
        specs = [spec for spec in discovery.services if spec.slug in SLUGS]
        manifest = Manifest(
            name="shop",
            env_conflicts={"EXTERNAL_ACCESS": "prefix", "VITE_API_BASE_URL": "value:/api"},
            services={spec.slug: ServiceEntry(slug=spec.slug) for spec in specs},
        )
        plan = builder.build(specs, manifest, registry)
        destination = tmp_path / f"run{index}"
        writer.render(plan, destination, image_ref="ghcr.io/acme/shop-bundle:test")
        outputs.append(destination)

    for name in COMPARED:
        assert (outputs[0] / name).read_text(encoding="utf-8") == (
            outputs[1] / name
        ).read_text(encoding="utf-8"), name


def test_shell_output_uses_lf_only(generated):
    # A CRLF shebang makes the Linux kernel report "no such file or directory".
    for name in ("entrypoint.sh", "healthcheck.sh"):
        assert b"\r\n" not in (generated / name).read_bytes(), name


def test_context_is_self_contained(generated):
    # Every COPY in the Dockerfile must resolve inside dist/, so `docker build dist/`
    # works from anywhere and CI can set BUILD_CONTEXT=dist.
    dockerfile = (generated / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        source = line.split()[1]
        assert (generated / source).exists(), f"{source} is missing from the build context"
