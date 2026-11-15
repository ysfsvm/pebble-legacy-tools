# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The command line: every command has help, and expected failures are messages."""

import argparse

import pytest

from plt import cli

COMMANDS = [
    ["images"],
    ["images", "add"],
    ["images", "role"],
    ["build"],
    ["emu"],
    ["emu", "test"],
    ["emu", "try"],
    ["watch"],
    ["watch", "status"],
    ["watch", "log"],
    ["watch", "shot"],
    ["watch", "flash"],
    ["watch", "recover"],
    ["bench"],
    ["bench", "run"],
    ["bench", "report"],
    ["bench", "reference"],
    ["gate"],
]


@pytest.mark.parametrize("command", [[], *COMMANDS], ids=" ".join)
def test_help(command, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([*command, "--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: plt")


def test_every_argument_has_help():
    def walk(parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    assert sub.description or len(sub._actions) == 1, sub.prog
                    walk(sub)
            else:
                assert action.help, f"{parser.prog}: {action.dest} has no help"

    walk(cli.build_parser())


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("plt ")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PLT_CONFIG", str(tmp_path / "plt.conf"))
    monkeypatch.setenv("PLT_HOME", str(tmp_path / "home"))
    return tmp_path


def test_expected_failures_are_one_line_and_status_1(home, capsys):
    assert cli.main(["images", "role", "ghost", "recovery"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "plt: no image 'ghost' in the registry (plt images)\n"
    assert captured.out == ""
    assert cli.main(["watch", "flash", "ghost"]) == 1
    assert cli.main(["bench", "report", "ghost"]) == 1
    assert cli.main(["emu", "test", "ghost"]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_images_on_an_empty_registry(home, capsys):
    assert cli.main(["images"]) == 0
    assert capsys.readouterr().out == ""


def test_missing_program_is_a_message(home, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(home))
    assert cli.main(["watch", "status"]) == 1
    assert capsys.readouterr().err == "plt: adb is not installed or not on PATH\n"


def test_build_outside_a_worktree(home, capsys):
    assert cli.main(["build", "x1", "--wt", str(home)]) == 1
    assert "is not a git worktree" in capsys.readouterr().err
