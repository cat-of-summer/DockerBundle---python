from __future__ import annotations

import pytest

from core.model import Origin
from discover import composefile


def test_split_volume_handles_windows_drive():
    # The drive colon must not be mistaken for the source/target separator.
    assert composefile.split_volume(r"C:\src:/app:ro") == (r"C:\src", "/app", "ro")
    assert composefile.split_volume("./data:/var/www/html") == ("./data", "/var/www/html", "")
    assert composefile.split_volume("named:/var/lib/mysql") == ("named", "/var/lib/mysql", "")
    assert composefile.split_volume("/anonymous") == ("", "/anonymous", "")


def test_is_bind_source():
    assert composefile.is_bind_source("./data")
    assert composefile.is_bind_source("/var/run/docker.sock")
    assert composefile.is_bind_source(r"C:\data")
    assert not composefile.is_bind_source("mysql_data")


def test_parse_port_forms():
    assert composefile.parse_port("127.0.0.1:8081:80").container == 80
    assert composefile.parse_port("127.0.0.1:8081:80").published == "127.0.0.1:8081"
    assert composefile.parse_port("3000").container == 3000
    assert composefile.parse_port("80:80/udp").protocol == "udp"
    assert composefile.parse_port({"target": 5432, "published": 5433}).container == 5432


def test_parse_port_ignores_empty_interpolation():
    # `${EXTERNAL_ACCESS}` with no .env collapses to an empty string.
    assert composefile.parse_port("") is None
    assert composefile.parse_port(":") is None


def test_loads_catalog_package(catalog):
    path = catalog / "laravel-nginx" / "docker-compose.yml"
    specs = composefile.load_services(path, package="laravel-nginx", origin=Origin.CATALOG)

    by_slug = {spec.slug: spec for spec in specs}
    assert set(by_slug) == {"laravel_nginx_laravel", "laravel_nginx_nginx"}

    nginx = by_slug["laravel_nginx_nginx"]
    assert nginx.image == "nginx:alpine"
    assert nginx.ports[0].container == 80
    assert nginx.ports[0].published == "127.0.0.1:8081"
    # depends_on is rewritten from compose service names to our slugs.
    assert nginx.depends_on == ["laravel_nginx_laravel"]

    laravel = by_slug["laravel_nginx_laravel"]
    # No `image:`; the base comes from the Dockerfile with build args resolved.
    assert laravel.image is None
    assert laravel.base_image == "php:fpm-alpine"
    assert laravel.effective_image == "php:fpm-alpine"
    # The un-interpolated environment is kept so renames can be mapped back later.
    assert laravel.raw_environment["DB_HOST"] == "${DB_HOST}"
    assert laravel.environment["DB_HOST"] == "mysql"


def test_single_service_package_uses_package_slug(catalog):
    path = catalog / "3. mysql" / "docker-compose.yml"
    specs = composefile.load_services(
        path, package="3. mysql", slug_base="mysql", origin=Origin.CATALOG
    )
    assert [spec.slug for spec in specs] == ["mysql"]
    assert specs[0].image == "mysql:8.0"


def test_named_and_bind_mounts_are_distinguished(catalog):
    path = catalog / "3. mysql" / "docker-compose.yml"
    spec = composefile.load_services(path, slug_base="mysql")[0]
    targets = {mount.target: mount for mount in spec.mounts}
    assert not targets["/var/lib/mysql"].named
    assert targets["/etc/mysql/conf.d/custom.cnf"].read_only


# ---------------------------------------------------------------------------
# include:
# ---------------------------------------------------------------------------


def _write_included_package(root):
    """A package shaped like docker_toolkit: an entry file that is nothing but include:."""
    (root / ".env").write_text("INSTANCE=_x\nAPP_PORT=8931\n", encoding="utf-8")
    (root / "docker-compose.yml").write_text(
        "include:\n"
        "  - path: services/app/docker-compose.yml\n"
        "    project_directory: .\n"
        "    env_file: .env\n"
        "\n"
        "services:\n"
        "  app:\n"
        "    environment:\n"
        "      - EXTRA=yes\n",
        encoding="utf-8",
    )
    service_dir = root / "services" / "app"
    service_dir.mkdir(parents=True)
    (service_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  app:\n"
        "    build:\n"
        "      context: .\n"
        "      dockerfile: services/app/Dockerfile.app\n"
        "    container_name: app${INSTANCE}\n"
        "    ports:\n"
        '      - "127.0.0.1:${APP_PORT}:${APP_PORT}"\n',
        encoding="utf-8",
    )
    (service_dir / "Dockerfile.app").write_text("FROM debian:bookworm-slim\n", encoding="utf-8")
    (service_dir / "entrypoint.app.sh").write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    return root / "docker-compose.yml"


def test_include_pulls_in_services_from_another_file(tmp_path):
    path = _write_included_package(tmp_path)
    specs = composefile.load_services(path, package="stand", origin=Origin.CATALOG)

    assert [spec.slug for spec in specs] == ["stand"]
    spec = specs[0]
    # The included service is real: its build context resolved and its ports parsed.
    assert spec.base_image == "debian:bookworm-slim"
    assert spec.ports[0].container == 8931
    # The including file refines what the included one declared rather than replacing it.
    assert spec.environment["EXTRA"] == "yes"


def test_include_resolves_paths_against_project_directory(tmp_path):
    path = _write_included_package(tmp_path)
    spec = composefile.load_services(path, package="stand")[0]
    # Not services/app/: `context: .` in the included file means the project root, which
    # is the only place its `services/app/Dockerfile.app` can be found.
    assert spec.source_dir == tmp_path


def test_include_env_file_feeds_interpolation(tmp_path):
    path = _write_included_package(tmp_path)
    spec = composefile.load_services(path, package="stand")[0]
    # ${INSTANCE} came from the .env named by the include entry.
    assert spec.ports[0].published == "127.0.0.1:8931"
    assert {entry.key for entry in spec.env_vars} >= {"INSTANCE", "APP_PORT"}
    # The package .env and the include's env_file are the same file here; keys must not
    # be listed twice or the merged dist/.env.example would repeat them.
    keys = [entry.key for entry in spec.env_vars]
    assert len(keys) == len(set(keys))


def test_entrypoint_is_found_beside_the_dockerfile(tmp_path):
    path = _write_included_package(tmp_path)
    spec = composefile.load_services(path, package="stand")[0]
    # Qualified name, and two directories away from the package root: only anchoring on
    # the Dockerfile finds it.
    assert spec.find_entrypoint() == tmp_path / "services" / "app" / "entrypoint.app.sh"


def test_entrypoint_lookup_returns_none_when_absent(tmp_path):
    path = _write_included_package(tmp_path)
    (tmp_path / "services" / "app" / "entrypoint.app.sh").unlink()
    spec = composefile.load_services(path, package="stand")[0]
    assert spec.find_entrypoint() is None


def test_include_cycle_is_reported(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "include:\n  - other.yml\nservices: {}\n", encoding="utf-8"
    )
    (tmp_path / "other.yml").write_text(
        "include:\n  - docker-compose.yml\nservices: {}\n", encoding="utf-8"
    )
    with pytest.raises(composefile.ComposeError, match="cycle"):
        composefile.load_services(tmp_path / "docker-compose.yml", package="loop")


def test_include_missing_file_is_reported(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "include:\n  - nope.yml\nservices: {}\n", encoding="utf-8"
    )
    with pytest.raises(composefile.ComposeError, match="does not exist"):
        composefile.load_services(tmp_path / "docker-compose.yml", package="missing")
