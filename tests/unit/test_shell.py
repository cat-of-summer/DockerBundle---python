"""Interactive shell tests.

This is the path a double-clicked .exe takes, and the one nobody exercises by hand: a
command that escapes as SystemExit closes the console again, which is the bug the shell
exists to prevent. So every case here checks that the loop survives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import shell


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def script(monkeypatch, *lines: str) -> None:
    """Feed the prompt a fixed list of lines, then EOF."""
    remaining = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def test_a_command_runs_and_the_prompt_returns(monkeypatch, capsys):
    script(monkeypatch, "doctor", "exit")
    assert shell.run() == 0
    assert "Docker" in capsys.readouterr().out


def test_an_unknown_command_does_not_close_the_shell(monkeypatch):
    script(monkeypatch, "nosuchcommand", "exit")
    assert shell.run() == 0


def test_help_does_not_close_the_shell(monkeypatch, capsys):
    script(monkeypatch, "--help", "exit")
    assert shell.run() == 0
    assert "Usage" in capsys.readouterr().out


def test_a_failing_command_does_not_close_the_shell(monkeypatch):
    # `generate` without a manifest raises typer.Exit(EXIT_USAGE); click turns that into
    # SystemExit, which would take the whole process down if the shell let it through.
    script(monkeypatch, "generate --yes", "exit")
    assert shell.run() == 0


def test_eof_ends_the_shell(monkeypatch):
    script(monkeypatch)
    assert shell.run() == 0


def test_blank_lines_are_ignored(monkeypatch):
    script(monkeypatch, "", "   ", "quit")
    assert shell.run() == 0


def test_cd_changes_the_working_directory(monkeypatch, tmp_path):
    target = tmp_path / "project"
    target.mkdir()

    script(monkeypatch, f"cd {target}", "exit")
    assert shell.run() == 0
    assert Path.cwd() == target


def test_cd_accepts_a_path_with_spaces(monkeypatch, tmp_path):
    target = tmp_path / "my project"
    target.mkdir()

    script(monkeypatch, f"cd {target}", "exit")
    shell.run()
    assert Path.cwd() == target


def test_a_bare_path_changes_the_working_directory(monkeypatch, tmp_path):
    target = tmp_path / "packages"
    target.mkdir()

    script(monkeypatch, str(target), "exit")
    shell.run()
    assert Path.cwd() == target


def test_a_missing_directory_is_reported_and_the_shell_survives(monkeypatch, tmp_path, capsys):
    script(monkeypatch, "cd nosuchplace", "exit")
    assert shell.run() == 0
    assert Path.cwd() == tmp_path
    assert "nosuchplace" in capsys.readouterr().err


def test_commands_run_in_the_directory_cd_moved_to(monkeypatch, tmp_path):
    target = tmp_path / "project"
    target.mkdir()

    script(monkeypatch, f"cd {target}", "init --name shop", "exit")
    shell.run()
    assert (target / "docker-bundle.yml").is_file()


def test_pwd_prints_the_working_directory(monkeypatch, tmp_path, capsys):
    script(monkeypatch, "pwd", "exit")
    shell.run()
    assert tmp_path.name in capsys.readouterr().out


def test_split_keeps_quoted_arguments_together():
    assert shell.split('scan -s "a b"') == ["scan", "-s", "a b"]


def test_split_keeps_windows_backslashes(monkeypatch):
    # POSIX splitting would turn ..\packages into ..packages and scan the wrong place.
    monkeypatch.setattr(shell.os, "name", "nt")
    assert shell.split(r"init --source ..\packages") == ["init", "--source", r"..\packages"]
