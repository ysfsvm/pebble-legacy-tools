# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The Core gate: the port must leave Core's own boards as they were.

The source check reduces every C, header and linker-script file the port
changes with the configuration of each Core board, as unifdef would, and
lists the files that still differ. The object comparison builds Core's boards
from a pristine tree and from the port and compares every object.
"""

import difflib
import os
import re
import subprocess

from plt import config, container
from plt.util import PltError, output, stamp

CORE_BOARDS = ("asterix", "obelix@pvt", "getafix@dvt2")
SOURCE_RE = re.compile(r"\.(c|h|ld)$")
DIFF_LINES = 6

IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")
DIRECTIVE = re.compile(
    r"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$"
)
# String and character literals (kept), then comments (dropped).
COMMENT = re.compile(
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*.*?\*/|//[^\n]*', re.DOTALL
)
NUMBER = re.compile(r"-?(0x[0-9a-fA-F]+|\d+)")
# What is left of a condition once every symbol has its value.
ARITHMETIC = re.compile(r"(?:[\s\d()<>=!&|+\-*/%~^]|0[xX][0-9a-fA-F]+|and|or|not)*")


def comparison_build(board):
    """The build directory, in a worktree, of a Core board's comparison build."""
    return "build-cmp-" + board.replace("@", "_")


def load_defines(build_dir):
    """The CONFIG_ symbols a configured build directory defines, with their values."""
    defines = {}
    with open(os.path.join(build_dir, "autoconf.h")) as f:
        for line in f:
            match = re.match(r"#define\s+(CONFIG_\w+)\s*(.*)$", line)
            if match:
                defines[match.group(1)] = match.group(2).strip() or "1"
    return defines


def evaluate(expression, defines):
    """Whether a condition holds, or None if it depends on more than CONFIG_ symbols."""
    text = re.sub(r"//.*|/\*.*?\*/", "", expression).strip()
    text = re.sub(r"\bdefined\s*\(\s*(\w+)\s*\)", r"defined_\1", text)
    text = re.sub(r"\bdefined\s+(\w+)", r"defined_\1", text)
    python = text
    for name in sorted(set(IDENTIFIER.findall(text)), key=len, reverse=True):
        if name.startswith("defined_"):
            symbol = name[len("defined_") :]
            if not symbol.startswith("CONFIG_"):
                return None
            value = "1" if symbol in defines else "0"
        elif name.startswith("CONFIG_"):
            value = defines.get(name, "0")
            if value.startswith('"'):
                return None
        else:
            return None
        python = re.sub(rf"\b{name}\b", value, python)
    python = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", python)
    python = python.replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"!(?!=)", " not ", python)
    if not ARITHMETIC.fullmatch(python):
        return None
    try:
        return bool(eval(python, {"__builtins__": {}}))
    except Exception:  # noqa: BLE001
        return None


def _condition(kind, rest, defines):
    if kind in ("if", "elif"):
        return evaluate(rest, defines)
    symbol = rest.strip().split()[0] if rest.strip() else ""
    if not symbol.startswith("CONFIG_"):
        return None
    present = symbol in defines
    return present if kind in ("ifdef", "elifdef") else not present


def _strip_comments(text):
    def replace(match):
        found = match.group(0)
        if found[0] in "\"'":
            return found
        return "\n" * found.count("\n")

    return COMMENT.sub(replace, text)


def reduce(text, defines):
    """The lines of a source file that a configuration keeps.

    Directives that depend only on CONFIG_ symbols are resolved, a symbol that
    is not defined counting as undefined; anything else stays as written.
    Comments and blank lines are dropped, white space is normalised, and
    numeric CONFIG_ values are substituted in the code.
    """
    text = re.sub(r"\\\n", " ", _strip_comments(text))
    kept = []
    # One entry per open #if: [state, whether the enclosing block is kept,
    # whether a branch was taken]. "known": every branch so far was resolved;
    # "unknown": kept as written; "closed": kept, and a branch that resolved
    # true has ended it.
    stack = []
    emit = True
    for line in text.split("\n"):
        match = DIRECTIVE.match(line)
        if not match:
            if emit:
                kept.append(line)
            continue
        kind, rest = match.group(1), match.group(2)
        if kind in ("if", "ifdef", "ifndef"):
            value = _condition(kind, rest, defines)
            if value is None:
                stack.append(["unknown", emit, False])
                if emit:
                    kept.append(line)
            else:
                stack.append(["known", emit, value])
                emit = emit and value
        elif kind in ("elif", "elifdef", "elifndef"):
            top = stack[-1]
            value = _condition(kind, rest, defines)
            parent = top[1]
            if top[0] == "known":
                if top[2]:
                    emit = False
                elif value is None:
                    top[0] = "unknown"
                    emit = parent
                    if parent:
                        kept.append(
                            re.sub(
                                r"#\s*elif(def|ndef)?",
                                lambda m: "#if" + (m.group(1) or ""),
                                line,
                                count=1,
                            )
                        )
                else:
                    top[2] = value
                    emit = parent and value
            elif top[0] == "closed" or value is False:
                emit = False
            elif value is True:
                emit = parent
                if parent:
                    kept.append("#else")
                top[0] = "closed"
            else:
                emit = parent
                if parent:
                    kept.append(line)
        elif kind == "else":
            top = stack[-1]
            parent = top[1]
            if top[0] == "known":
                emit = parent and not top[2]
            elif top[0] == "closed":
                emit = False
            else:
                emit = parent
                if parent:
                    kept.append(line)
        elif kind == "endif":
            top = stack.pop()
            if top[0] != "known" and top[1]:
                kept.append(line)
            emit = top[1]

    def substitute(match):
        value = defines.get(match.group(0), "")
        return value if NUMBER.fullmatch(value) else match.group(0)

    return [
        re.sub(r"\s+", " ", re.sub(r"\bCONFIG_\w+\b", substitute, line)).strip()
        for line in kept
        if line.strip()
    ]


def classify(old_text, new_text, boards):
    """Which Core boards a change to one file reaches.

    `boards` holds (name, defines of the pristine build, defines of the
    port's build). Returns the names of the boards whose reduced source
    differs, and the differing lines for the first of them.
    """
    reached, lines = [], None
    for name, base_defines, port_defines in boards:
        old, new = reduce(old_text, base_defines), reduce(new_text, port_defines)
        if old == new:
            continue
        reached.append(name)
        if lines is None:
            diff = difflib.unified_diff(old, new, "core", "fork", n=0, lineterm="")
            lines = [
                line
                for line in diff
                if line[:1] in "+-" and not line.startswith(("+++", "---"))
            ]
    return reached, lines or []


def _git(repo, *args):
    result = subprocess.run(
        [config.GIT, "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise PltError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def core_release(worktree):
    """The Core release a worktree is based on: the nearest version tag."""
    try:
        return _git(
            worktree, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD"
        ).strip()
    except PltError:
        raise PltError(
            f"{worktree} has no Core release tag (v*) in its history"
        ) from None


def changed_sources(worktree, base):
    """The C, header and linker-script files HEAD modifies since a base revision."""
    names = _git(worktree, "diff", "--name-only", "--diff-filter=M", base, "HEAD")
    return [name for name in names.splitlines() if SOURCE_RE.search(name)]


def board_configurations(base_worktree, worktree):
    """The Core boards whose comparison build is configured in both worktrees."""
    boards = []
    for board in CORE_BOARDS:
        builds = [
            os.path.join(tree, comparison_build(board))
            for tree in (base_worktree, worktree)
        ]
        if all(os.path.exists(os.path.join(build, "autoconf.h")) for build in builds):
            boards.append((board, load_defines(builds[0]), load_defines(builds[1])))
    return boards


def source_check(worktree, base, boards):
    """Print the files whose change reaches a Core board; returns how many."""
    files = changed_sources(worktree, base)
    reaching = 0
    for name in files:
        reached, lines = classify(
            _git(worktree, "show", f"{base}:{name}"),
            _git(worktree, "show", f"HEAD:{name}"),
            boards,
        )
        if not reached:
            continue
        reaching += 1
        print(f"UNGATED {name} ({len(lines)} lines; {','.join(reached)})")
        for line in lines[:DIFF_LINES]:
            print("    " + line[:110])
    if boards:
        checked = ", ".join(name for name, _, _ in boards)
        print(
            f"{len(files)} Core files changed, {reaching} change Core boards "
            f"({checked})"
        )
    else:
        print(
            f"{len(files)} Core files changed; not checked against Core's boards: "
            "no comparison builds yet (plt gate --objects makes them)"
        )
    return reaching


def object_comparison(cfg, worktree, base_worktree):
    """Build and compare Core's boards in the container; True if no object differs."""
    print(f"object comparison for {', '.join(CORE_BOARDS)}, about 10 min")
    results = cfg.logs / "gate" / stamp()
    script = str(config.DATA_DIR / "core_compare.sh")
    argv = ["bash", script, str(base_worktree), str(worktree), str(results)]
    result = container.run_in(
        cfg, worktree, [*argv, *CORE_BOARDS], extra_mounts=[base_worktree]
    )
    same = True
    boards = 0
    for line in result.stdout.splitlines():
        if line.startswith("=="):
            print(line)
            boards += 1
            same = same and line.endswith(": 0 differing objects")
        elif line.endswith("build FAIL"):
            print(line)
            same = False
    if boards != len(CORE_BOARDS):
        print(f"the comparison did not finish: {result.stderr.strip()[-400:]}")
        same = False
    print(f"logs: {results}")
    return same


def gate(cfg, worktree, objects=False):
    """Run the source check and, on request, the object comparison; True if it passes.

    The source check cannot resolve conditions on anything but CONFIG_ symbols,
    so a file it lists may still compile to the same code. The object
    comparison settles that: when it runs, its result is the gate's.
    """
    if output([config.GIT, "-C", str(worktree), "rev-parse", "--git-dir"]) == "":
        raise PltError(f"{worktree} is not a git worktree (--wt, PLT_WORKTREE)")
    base_worktree = cfg.base_worktree or worktree.parent / "base"
    if objects and not os.path.isdir(base_worktree):
        raise PltError(
            f"no pristine Core worktree at {base_worktree} (PLT_BASE_WORKTREE)"
        )
    boards = board_configurations(base_worktree, worktree)
    reaching = source_check(worktree, core_release(worktree), boards)
    if objects:
        return object_comparison(cfg, worktree, base_worktree)
    return reaching == 0
