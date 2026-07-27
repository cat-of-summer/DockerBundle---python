from __future__ import annotations

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
