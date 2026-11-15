# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The source gate: reduction of a file by a configuration, and which changes reach Core."""

import subprocess

import pytest

from plt import config, gate
from plt.util import PltError

CORE = {"CONFIG_CORE_DISPLAY": "1", "CONFIG_BT_SLOTS": "4"}


def test_evaluate():
    defines = {"CONFIG_A": "1", "CONFIG_N": "3", "CONFIG_S": '"text"'}
    assert gate.evaluate("defined(CONFIG_A)", defines) is True
    assert gate.evaluate("defined CONFIG_B", defines) is False
    assert gate.evaluate("!defined(CONFIG_B) && CONFIG_N > 2", defines) is True
    assert gate.evaluate("CONFIG_B || CONFIG_N == 4", defines) is False
    assert gate.evaluate("CONFIG_N >= 0x2U /* note */", defines) is True
    assert gate.evaluate("CONFIG_A != 1", defines) is False
    assert gate.evaluate("defined(__ARM__)", defines) is None
    assert gate.evaluate("CONFIG_A && PLATFORM_SNOWY", defines) is None
    assert gate.evaluate("CONFIG_S", defines) is None
    assert gate.evaluate("CONFIG_A ? 1 : 0", defines) is None


def test_reduce_resolves_config_conditions_only():
    source = """\
/* header */
#include "a.h"
#ifdef CONFIG_LEGACY_BOARD
legacy();   // not for Core
#else
core();
#endif
#if defined(__GNUC__)
gnu();
#endif
#if CONFIG_BT_SLOTS > 2
many(CONFIG_BT_SLOTS);
#elif defined(CONFIG_CORE_DISPLAY)
few();
#else
none();
#endif
char *s = "// kept";
"""
    assert gate.reduce(source, CORE) == [
        '#include "a.h"',
        "core();",
        "#if defined(__GNUC__)",
        "gnu();",
        "#endif",
        "many(4);",
        'char *s = "// kept";',
    ]
    assert gate.reduce(source, {"CONFIG_LEGACY_BOARD": "1"})[1:5] == [
        "legacy();",
        "#if defined(__GNUC__)",
        "gnu();",
        "#endif",
    ]


def test_reduce_keeps_chains_it_cannot_resolve():
    source = """\
#if PLATFORM_A
a();
#elif CONFIG_CORE_DISPLAY
b();
#elif PLATFORM_C
c();
#else
d();
#endif
"""
    assert gate.reduce(source, CORE) == [
        "#if PLATFORM_A",
        "a();",
        "#else",
        "b();",
        "#endif",
    ]
    assert gate.reduce(source, {}) == [
        "#if PLATFORM_A",
        "a();",
        "#elif PLATFORM_C",
        "c();",
        "#else",
        "d();",
        "#endif",
    ]


def test_classify():
    boards = [("asterix", CORE, CORE), ("obelix@pvt", CORE, {**CORE, "CONFIG_X": "1"})]
    old = "void f(void) {\n  core();\n}\n"
    gated = (
        "void f(void) {\n#ifdef CONFIG_LEGACY_BOARD\n  legacy();\n#endif\n"
        "  core();   /* moved */\n}\n"
    )
    assert gate.classify(old, gated, boards) == ([], [])
    ungated = "void f(void) {\n  legacy();\n  core();\n}\n"
    assert gate.classify(old, ungated, boards) == (
        ["asterix", "obelix@pvt"],
        ["+legacy();"],
    )
    one_board = "void f(void) {\n#ifdef CONFIG_X\n  extra();\n#endif\n  core();\n}\n"
    assert gate.classify(old, one_board, boards) == (["obelix@pvt"], ["+extra();"])
    assert gate.classify(old, ungated, []) == ([], [])


def git(repo, *args):
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=t@example.org",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def write_autoconf(tree, board, defines):
    build = tree / gate.comparison_build(board)
    build.mkdir(parents=True)
    lines = [f"#define {name} {value}" for name, value in defines.items()]
    (build / "autoconf.h").write_text("\n".join(lines) + "\n")


@pytest.fixture
def port(tmp_path):
    """A port worktree on a tagged Core release, and the pristine tree next to it."""
    worktree = tmp_path / "port"
    worktree.mkdir()
    git(worktree, "init", "-q", "-b", "main")
    files = {
        "gated.c": "int a(void) {\n  return 1;\n}\n",
        "ungated.c": "int b(void) {\n  return 1;\n}\n",
        "comment.h": "#define X 1\n",
        "notes.md": "text\n",
        "CMakeLists.txt": "add_library(x)\n",
    }
    for name, text in files.items():
        (worktree / name).write_text(text)
    git(worktree, "add", ".")
    git(worktree, "commit", "-q", "-m", "core")
    git(worktree, "tag", "v9.9.0")
    (worktree / "gated.c").write_text(
        "int a(void) {\n#if defined(CONFIG_LEGACY_BOARD)\n  legacy();\n#endif\n"
        "  return 1;\n}\n"
    )
    (worktree / "ungated.c").write_text("int b(void) {\n  return 2;\n}\n")
    (worktree / "comment.h").write_text("// why\n#define X 1\n")
    (worktree / "notes.md").write_text("other text\n")
    (worktree / "CMakeLists.txt").write_text("add_library(y)\n")
    (worktree / "new.c").write_text("int c;\n")
    git(worktree, "add", ".")
    git(worktree, "commit", "-q", "-m", "port")
    return worktree


def test_core_release_is_the_nearest_version_tag(port):
    assert gate.core_release(port) == "v9.9.0"
    git(port, "tag", "-d", "v9.9.0")
    with pytest.raises(PltError, match="no Core release tag"):
        gate.core_release(port)


def test_changed_sources_are_modified_c_h_ld_files(port):
    assert gate.changed_sources(port, "v9.9.0") == ["comment.h", "gated.c", "ungated.c"]


def test_source_check_lists_the_files_that_reach_a_board(port, capsys):
    boards = [("asterix", CORE, CORE)]
    assert gate.source_check(port, "v9.9.0", boards) == 1
    assert capsys.readouterr().out.splitlines() == [
        "UNGATED ungated.c (2 lines; asterix)",
        "    -return 1;",
        "    +return 2;",
        "3 Core files changed, 1 change Core boards (asterix)",
    ]


def test_source_check_says_when_no_board_was_checked(port, capsys):
    assert gate.source_check(port, "v9.9.0", []) == 0
    out = capsys.readouterr().out
    assert out.startswith("3 Core files changed; not checked against Core's boards")


def test_board_configurations_need_both_builds(port):
    base = port.parent / "base"
    assert gate.board_configurations(base, port) == []
    write_autoconf(base, "asterix", CORE)
    assert gate.board_configurations(base, port) == []
    write_autoconf(port, "asterix", {**CORE, "CONFIG_EMPTY": ""})
    write_autoconf(port, "obelix@pvt", CORE)
    assert gate.board_configurations(base, port) == [
        ("asterix", CORE, {**CORE, "CONFIG_EMPTY": "1"})
    ]


def test_gate_fails_on_an_ungated_change(port, capsys):
    cfg = config.load({"PLT_CONFIG": str(port / "none"), "PLT_HOME": str(port / "h")})
    base = port.parent / "base"
    write_autoconf(base, "getafix@dvt2", CORE)
    write_autoconf(port, "getafix@dvt2", CORE)
    assert gate.gate(cfg, port) is False
    assert "1 change Core boards (getafix@dvt2)" in capsys.readouterr().out
    (port / "ungated.c").write_text(
        "int b(void) {\n#ifdef CONFIG_LEGACY_BOARD\n  return 2;\n#else\n"
        "  return 1;\n#endif\n}\n"
    )
    git(port, "commit", "-q", "-am", "gate it")
    assert gate.gate(cfg, port) is True
    assert "0 change Core boards (getafix@dvt2)" in capsys.readouterr().out


def test_objects_need_the_pristine_worktree(port):
    cfg = config.load({"PLT_CONFIG": str(port / "none"), "PLT_HOME": str(port / "h")})
    with pytest.raises(PltError, match="no pristine Core worktree"):
        gate.gate(cfg, port, objects=True)


def test_the_object_comparison_decides_when_it_runs(port, monkeypatch):
    cfg = config.load({"PLT_CONFIG": str(port / "none"), "PLT_HOME": str(port / "h")})
    base = port.parent / "base"
    write_autoconf(base, "asterix", CORE)
    write_autoconf(port, "asterix", CORE)
    assert gate.gate(cfg, port) is False
    for same in (True, False):
        monkeypatch.setattr(gate, "object_comparison", lambda *args, same=same: same)
        assert gate.gate(cfg, port, objects=True) is same


def test_gate_outside_a_worktree(tmp_path, cfg):
    with pytest.raises(PltError):
        gate.gate(cfg, tmp_path)
