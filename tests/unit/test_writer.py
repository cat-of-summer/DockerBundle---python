"""Staging rules for the build context, and how baked services' labels are re-emitted."""

from __future__ import annotations

from core.model import BundlePlan, Origin, PlannedService, PortSpec, ServiceSpec
from render import writer
from render.writer import _labels, stage_file


def _service(slug, labels=None, ports=(), raw_ports=(), published="", shm_size=""):
    return PlannedService(
        spec=ServiceSpec(
            slug=slug,
            name=slug,
            package=slug,
            origin=Origin.COMPOSE,
            raw_labels=dict(labels or {}),
            raw_ports=list(raw_ports),
            shm_size=shm_size,
        ),
        recipe_name="test",
        bakeable=True,
        ports=[PortSpec(container=c, original=o, published=published) for o, c in ports],
    )


def test_crlf_is_normalised_when_staging(tmp_path):
    # A Windows checkout hands us CRLF. Copied verbatim into the image, a shebang ending
    # in \r makes the Linux kernel report "no such file or directory".
    source = tmp_path / "entrypoint.sh"
    source.write_bytes(b"#!/bin/sh\r\nset -e\r\nexec \"$@\"\r\n")

    destination = tmp_path / "staged.sh"
    stage_file(source, destination)

    assert b"\r\n" not in destination.read_bytes()
    assert destination.read_bytes() == b'#!/bin/sh\nset -e\nexec "$@"\n'


def test_staging_is_byte_identical_regardless_of_line_endings(tmp_path):
    # The context digest recorded in the lock file must not depend on how git checked
    # the sources out, or the same commit produces different lock files per platform.
    crlf = tmp_path / "crlf.conf"
    crlf.write_bytes(b"server {\r\n    listen 80;\r\n}\r\n")
    lf = tmp_path / "lf.conf"
    lf.write_bytes(b"server {\n    listen 80;\n}\n")

    stage_file(crlf, tmp_path / "a")
    stage_file(lf, tmp_path / "b")

    assert (tmp_path / "a").read_bytes() == (tmp_path / "b").read_bytes()


def test_binary_files_are_copied_untouched(tmp_path):
    # 0x0d 0x0a inside binary content is data, not a line ending.
    payload = b"\x89PNG\r\n\x1a\n\x00\x01\x02\r\n\xff"
    source = tmp_path / "image.png"
    source.write_bytes(payload)

    destination = tmp_path / "out.png"
    stage_file(source, destination)

    assert destination.read_bytes() == payload


def test_invalid_utf8_is_copied_untouched(tmp_path):
    payload = b"\xff\xfe\x00binary\r\ndata"
    source = tmp_path / "blob.bin"
    source.write_bytes(payload)

    destination = tmp_path / "out.bin"
    stage_file(source, destination)

    assert destination.read_bytes() == payload


def test_base_image_arg_precedes_every_from(tmp_path):
    # An ARG written after a FROM belongs to that stage alone. With a rootfs import in
    # front of it, `FROM ${BASE_IMAGE}` then expands to nothing and the build dies with
    # "base name should not be blank" — which no bundle without imports ever hits.
    plan = BundlePlan(name="b", base_images={"cpu": "debian:bookworm-slim"})
    plan.stages = [("svc_x", "example.com/x:1")]
    plan.rootfs_stages = ["svc_x"]

    writer.render(plan, tmp_path / "dist")
    lines = (tmp_path / "dist" / "Dockerfile").read_text(encoding="utf-8").splitlines()

    arg = next(i for i, line in enumerate(lines) if line.startswith("ARG BASE_IMAGE="))
    first_from = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
    assert arg < first_from


# ---------------------------------------------------------------------------
# published ports
# ---------------------------------------------------------------------------


def test_published_port_keeps_the_env_reference():
    # Resolving it here would freeze the host port into the generated compose, and the
    # deployment's .env — the whole point of shipping one — could no longer move it.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service(
            "nginx",
            ports=[(80, 80)],
            raw_ports=["${EXTERNAL_ACCESS}"],
            published="127.0.0.1:8089",
        )
    ]
    plan.env_renames = {"nginx": {"EXTERNAL_ACCESS": "NGINX_EXTERNAL_ACCESS"}}
    assert writer._published(plan) == ["${NGINX_EXTERNAL_ACCESS}"]


def test_published_port_falls_back_to_a_literal_once_remapped():
    # The variable spells out a container port the service no longer listens on.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service(
            "web",
            ports=[(80, 20000)],
            raw_ports=["${EXTERNAL_ACCESS}"],
            published="127.0.0.1:8080",
        )
    ]
    assert writer._published(plan) == ["127.0.0.1:20000:20000"]


def test_published_port_falls_back_when_entries_cannot_be_paired():
    # One `ports:` entry interpolated to nothing and was dropped, so index N of the raw
    # list no longer describes port N.
    plan = BundlePlan(name="b")
    service = _service("web", ports=[(80, 80)], published="127.0.0.1:8080")
    service.spec.raw_ports = ["${A}", "${B}"]
    plan.baked = [service]
    assert writer._published(plan) == ["127.0.0.1:8080:80"]


def test_shm_size_is_carried_over():
    # Dropping it leaves Chromium on Docker's 64 MB default, where long pages crash
    # intermittently — the worst kind of regression to trace back to packaging.
    plan = BundlePlan(name="b")
    plan.baked = [_service("playwright", shm_size="${PLAYWRIGHT_SHM_SIZE:-2gb}")]
    value, warnings = writer._shm_size(plan)
    assert value == "${PLAYWRIGHT_SHM_SIZE:-2gb}"
    assert not warnings


def test_conflicting_shm_size_keeps_the_first_and_warns():
    plan = BundlePlan(name="b")
    plan.baked = [_service("a", shm_size="2gb"), _service("b", shm_size="512m")]
    value, warnings = writer._shm_size(plan)
    assert value == "2gb"
    assert any("shm_size" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def test_labels_are_carried_over_unresolved():
    # Baked to a literal, the routing domain could no longer be set from the .env.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service(
            "nginx",
            {
                "traefik.enable": "${TRAEFIK_ENABLE}",
                "traefik.http.routers.web.rule": "Host(`${TRAEFIK_DOMAIN}`)",
            },
        )
    ]
    labels, warnings = _labels(plan)
    assert "traefik.http.routers.web.rule=Host(`${TRAEFIK_DOMAIN}`)" in labels
    assert "traefik.enable=${TRAEFIK_ENABLE}" in labels
    assert not warnings


def test_labels_follow_a_renamed_env_key():
    plan = BundlePlan(name="b")
    plan.baked = [_service("nginx", {"traefik.http.routers.web.rule": "Host(`${TRAEFIK_DOMAIN}`)"})]
    plan.env_renames = {"nginx": {"TRAEFIK_DOMAIN": "NGINX_TRAEFIK_DOMAIN"}}
    labels, _ = _labels(plan)
    assert labels == ["traefik.http.routers.web.rule=Host(`${NGINX_TRAEFIK_DOMAIN}`)"]


def test_loadbalancer_port_follows_a_remapped_service():
    # The second nginx was pushed off :80; a router still aimed at 80 would reach the
    # first one's server block.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service(
            "web",
            {"traefik.http.services.web.loadbalancer.server.port": "80"},
            ports=[(80, 20000)],
        )
    ]
    labels, _ = _labels(plan)
    assert labels == ["traefik.http.services.web.loadbalancer.server.port=20000"]


def test_same_reference_written_differently_is_not_a_conflict():
    # One package spells the fallback out, the other does not. Both resolve alike once
    # the key is set, so warning about it would only teach people to ignore warnings.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service("nginx", {"traefik.enable": "${TRAEFIK_ENABLE:-true}"}),
        _service("vnu", {"traefik.enable": "${TRAEFIK_ENABLE}"}),
    ]
    labels, warnings = _labels(plan)
    # The spelling with a fallback wins: it also survives a .env that omits the key.
    assert labels == ["traefik.enable=${TRAEFIK_ENABLE:-true}"]
    assert not warnings


def test_fallback_spelling_wins_regardless_of_order():
    plan = BundlePlan(name="b")
    plan.baked = [
        _service("vnu", {"traefik.enable": "${TRAEFIK_ENABLE}"}),
        _service("nginx", {"traefik.enable": "${TRAEFIK_ENABLE:-true}"}),
    ]
    labels, warnings = _labels(plan)
    assert labels == ["traefik.enable=${TRAEFIK_ENABLE:-true}"]
    assert not warnings


def test_different_variables_are_still_a_conflict():
    # What prefixing TRAEFIK_ENABLE would produce: two keys, one container label.
    plan = BundlePlan(name="b")
    plan.baked = [
        _service("nginx", {"traefik.enable": "${NGINX_TRAEFIK_ENABLE}"}),
        _service("vnu", {"traefik.enable": "${VNU_TRAEFIK_ENABLE}"}),
    ]
    labels, warnings = _labels(plan)
    assert labels == ["traefik.enable=${NGINX_TRAEFIK_ENABLE}"]
    assert any("docker-bundle.yml" in warning for warning in warnings)


def test_manifest_labels_override_the_services():
    plan = BundlePlan(name="b")
    plan.baked = [_service("nginx", {"traefik.enable": "${NGINX_TRAEFIK_ENABLE}"})]
    labels, _ = _labels(plan, {"traefik.enable": "false"})
    assert labels == ["traefik.enable=false"]


def test_conflicting_label_keeps_the_first_and_warns():
    plan = BundlePlan(name="b")
    plan.baked = [
        _service("nginx", {"traefik.enable": "true"}),
        _service("vnu", {"traefik.enable": "false"}),
    ]
    labels, warnings = _labels(plan)
    assert labels == ["traefik.enable=true"]
    assert any("traefik.enable" in warning and "vnu" in warning for warning in warnings)


def test_identical_labels_from_two_services_are_not_a_conflict():
    plan = BundlePlan(name="b")
    plan.baked = [
        _service("nginx", {"traefik.enable": "${TRAEFIK_ENABLE}"}),
        _service("vnu", {"traefik.enable": "${TRAEFIK_ENABLE}"}),
    ]
    labels, warnings = _labels(plan)
    assert labels == ["traefik.enable=${TRAEFIK_ENABLE}"]
    assert not warnings
