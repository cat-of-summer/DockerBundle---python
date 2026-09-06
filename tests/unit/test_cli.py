"""CLI surface tests.

These drive the app the way a user and CI do, including the bare invocation a
double-clicked .exe produces — the one path that is easy to leave untested and that
crashed on first contact.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_bare_invocation_shows_help_when_there_is_no_manifest():
    # Regression: this used to reach the wizard through ctx.invoke, which skips Click's
    # parameter processing and passes the OptionInfo sentinel instead of a real path.
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output or "init" in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "dockerbundle" in result.output


def test_init_creates_a_manifest(tmp_path, catalog):
    result = runner.invoke(app, ["init", "--source", str(catalog), "--name", "shop"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "docker-bundle.yml").is_file()


def test_init_refuses_to_overwrite_without_force(catalog):
    runner.invoke(app, ["init", "--source", str(catalog)])
    result = runner.invoke(app, ["init", "--source", str(catalog)])
    assert result.exit_code == 3
    result = runner.invoke(app, ["init", "--source", str(catalog), "--force"])
    assert result.exit_code == 0, result.output


def test_generate_without_a_manifest_exits_usage():
    result = runner.invoke(app, ["generate", "--yes"])
    assert result.exit_code == 3


def test_generate_reports_a_nonzero_exit_when_blocked(tmp_path, catalog):
    # A blocked generation must fail loudly: CI would otherwise treat it as a success
    # and try to build an image that was never written.
    runner.invoke(app, ["init", "--source", str(catalog), "--name", "shop"])
    result = runner.invoke(app, ["generate", "--yes"])
    assert result.exit_code == 2, result.output


def test_generate_succeeds_once_conflicts_are_resolved(tmp_path, catalog):
    import yaml

    runner.invoke(app, ["init", "--source", str(catalog), "--name", "shop"])
    manifest = tmp_path / "docker-bundle.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["env"] = {"EXTERNAL_ACCESS": "prefix", "VITE_API_BASE_URL": "value:/api"}
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = runner.invoke(app, ["generate", "--yes"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "dist" / "Dockerfile").is_file()
    assert (tmp_path / "dist" / "docker-compose.yml").is_file()


def test_unknown_variant_is_rejected(tmp_path, catalog):
    runner.invoke(app, ["init", "--source", str(catalog)])
    result = runner.invoke(app, ["generate", "--yes", "--variant", "gpu"])
    assert result.exit_code == 3


def test_doctor_runs_without_a_manifest():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output


def test_scan_without_a_manifest_exits_usage():
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 3


def test_set_language_rejects_an_unknown_code():
    result = runner.invoke(app, ["set-language", "de"])
    assert result.exit_code == 3


def test_main_returns_the_exit_code():
    import main

    assert main.main(["--version"]) == 0
    assert main.main(["generate", "--yes"]) == 3


def test_bare_invocation_opens_the_shell_on_a_terminal(monkeypatch):
    # This is the double-clicked .exe: without the shell the console would close before
    # anything printed could be read.
    import main
    from app import shell

    monkeypatch.setattr(shell, "interactive", lambda: True)
    monkeypatch.setattr(shell, "run", lambda: 7)
    assert main.main([]) == 7


def test_bare_invocation_prints_help_without_a_terminal(monkeypatch, capsys):
    import main
    from app import shell

    monkeypatch.setattr(shell, "interactive", lambda: False)
    assert main.main([]) == 0
    assert "Usage" in capsys.readouterr().out
