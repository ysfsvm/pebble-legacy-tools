# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Runs in the build container: compare the objects of two builds of one board.

usage: compare_objects.py <base build> <port build> <base worktree> <port worktree>

Prints ADDED, REMOVED or CHANGED for every object that is not equivalent in
the two builds. Only objects the builds still produce are compared. Two
objects are equivalent if one of these holds:

1. They are equal byte for byte without debug info and the compiler comment.
2. They are equal once log tokens and pointers into string sections are
   replaced by the index of their string, and line numbers, the build's
   version, __DATE__ and __TIME__ are masked in the strings. A PBL_LOG call
   site keeps "file:line:..." in .log_strings and refers to it by offset, so
   a line that moves changes the object without changing the code.
3. They are equal after both are compiled again with __LINE__ fixed at 0,
   which covers asserts and other uses of the line number.

version.c holds the build's revision and is skipped.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from plt.config import TOOLCHAIN_PREFIX

READELF = TOOLCHAIN_PREFIX + "readelf"
OBJCOPY = TOOLCHAIN_PREFIX + "objcopy"
SKIPPED_SECTIONS = re.compile(
    r"^(\.debug|\.comment|\.rel|\.symtab|\.strtab|\.shstrtab|\.ARM\.attributes|"
    r"\.note|\.group$)"
)
STRING_SECTIONS = re.compile(
    r"^(\.log_strings|\.rodata\..*\.str\d\.\d+|\.rodata\.str\d\.\d+)$"
)
VERSION = re.compile(rb"v\d+\.\d+\.\d+[-0-9A-Za-z.]*")
# A log token keeps flags above the offset of its string.
LOG_OFFSET_MASK = 0x7FFFF


def _run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def sections(obj):
    """(name, type, size) of each section of an object."""
    found = []
    for line in _run(READELF, "-W", "-S", obj).splitlines():
        match = re.match(
            r"\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+([0-9a-f]+)", line
        )
        if match:
            found.append((match.group(1), match.group(2), int(match.group(3), 16)))
    return found


def section_bytes(obj, name, tmp):
    """The content of one section."""
    path = os.path.join(tmp, "section")
    subprocess.run(
        [OBJCOPY, "-O", "binary", f"--only-section={name}", obj, path],
        check=True,
        capture_output=True,
    )
    data = Path(path).read_bytes()
    os.unlink(path)
    return data


def relocations(obj):
    """{section name: [(offset, type, symbol and addend)]} of an object."""
    found = {}
    current = None
    for line in _run(READELF, "-W", "-r", obj).splitlines():
        match = re.match(r"Relocation section '\.rela?(\S+)'", line)
        if match:
            current = found.setdefault(match.group(1), [])
            continue
        match = re.match(
            r"\s*([0-9a-f]{8})\s+[0-9a-f]+\s+(\S+)\s+[0-9a-f]+\s+(.*)$", line
        )
        if match and current is not None:
            current.append(
                (int(match.group(1), 16), match.group(2), match.group(3).strip())
            )
    return found


def string_index(data):
    """{offset of a string in a string section: its index}."""
    index, offset = {}, 0
    for i, string in enumerate(data.split(b"\0")):
        index[offset] = i
        offset += len(string) + 1
    return index


def masked_strings(data, log):
    """The strings of a section with everything that varies between builds masked."""
    strings = []
    for string in data.split(b"\0"):
        string = VERSION.sub(b"<version>", string)
        string = re.sub(rb"[A-Z][a-z]{2} [ \d]\d \d{4}", b"<date>", string)
        string = re.sub(rb"\d\d:\d\d:\d\d", b"<time>", string)
        if log:
            string = re.sub(rb"^([^:]*):\d+:", rb"\1:#:", string)
        strings.append(string)
    return strings


def normalise(obj, tmp):
    """The content of an object in a form that ignores moved strings and lines."""
    found = sections(obj)
    relocs = relocations(obj)
    result = {}
    tables = {}
    for name, _, _ in found:
        if STRING_SECTIONS.match(name):
            data = section_bytes(obj, name, tmp)
            tables[name] = string_index(data)
            result[name] = masked_strings(data, log=(name == ".log_strings"))
    for name, kind, size in found:
        if SKIPPED_SECTIONS.match(name) or name in tables:
            continue
        if kind == "NOBITS":
            result[name] = ("nobits", size)
            continue
        data = bytearray(section_bytes(obj, name, tmp))
        entries = []
        for offset, reloc_type, symbol in relocs.get(name, []):
            target = symbol.split()[0]
            if (
                target in tables
                and reloc_type == "R_ARM_ABS32"
                and offset + 4 <= len(data)
            ):
                word = int.from_bytes(data[offset : offset + 4], "little")
                mask = LOG_OFFSET_MASK if target == ".log_strings" else 0xFFFFFFFF
                index = tables[target].get(word & mask, LOG_OFFSET_MASK)
                word = (word & ~mask & 0xFFFFFFFF) | index
                data[offset : offset + 4] = word.to_bytes(4, "little")
                symbol = target
            entries.append((offset, reloc_type, symbol))
        result[name] = (bytes(data), entries)
    return result


def stripped(obj, tmp):
    """An object without debug info and compiler comment, or None if it cannot be read."""
    path = os.path.join(tmp, "stripped")
    done = subprocess.run(
        [OBJCOPY, "--strip-debug", "--remove-section=.comment", obj, path],
        check=False,
        capture_output=True,
    )
    if done.returncode:
        return None
    data = Path(path).read_bytes()
    os.unlink(path)
    return data


def compare(a, b, tmp):
    """(whether two objects are equivalent, the reason if they are not)."""
    plain_a, plain_b = stripped(a, tmp), stripped(b, tmp)
    if plain_a is None or plain_b is None:
        return False, "object missing or unreadable"
    if plain_a == plain_b:
        return True, ""
    try:
        normal_a, normal_b = normalise(a, tmp), normalise(b, tmp)
    except subprocess.CalledProcessError as error:
        return False, (error.stderr or str(error)).strip()[-200:]
    differing = sorted(
        name
        for name in set(normal_a) | set(normal_b)
        if normal_a.get(name) != normal_b.get(name)
    )
    return not differing, "differs in " + ", ".join(differing)


def objects(build, root):
    """{path within the worktree: file} of the objects a build produces."""
    targets = subprocess.run(
        ["ninja", "-C", build, "-t", "targets", "all"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    found = {}
    for line in targets.splitlines():
        target = line.split(":")[0]
        if target.endswith(".obj"):
            # Sources outside the build tree carry the worktree's absolute path.
            key = target.replace(root.lstrip("/"), "<tree>")
            found[key] = os.path.join(build, target)
    return found


def compile_without_lines(build, target, out):
    """Compile one object again with __LINE__ fixed at 0; False if that fails."""
    commands = subprocess.run(
        ["ninja", "-C", build, "-t", "commands", "-s", target],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not commands:
        return False
    command = commands.splitlines()[-1]
    command = re.sub(r" -Werror(?=\s)", " ", command)
    command = re.sub(r" -M[DT]\b| -MF \S+| -MT \S+", " ", command)
    command = re.sub(r" -o \S+", " -o " + out, command)
    command += " -D__LINE__=0 -Wno-builtin-macro-redefined"
    done = subprocess.run(
        command, shell=True, cwd=build, check=False, capture_output=True
    )
    return done.returncode == 0


def main():
    base_build, port_build, base_root, port_root = sys.argv[1:5]
    base, port = objects(base_build, base_root), objects(port_build, port_root)
    for key in sorted(set(port) - set(base)):
        print("ADDED  ", key)
    for key in sorted(set(base) - set(port)):
        print("REMOVED", key)
    with tempfile.TemporaryDirectory() as tmp:
        for key in sorted(set(base) & set(port)):
            same, reason = compare(base[key], port[key], tmp)
            if same or key.endswith("/version.c.obj"):
                continue
            again_base = os.path.join(tmp, "base.obj")
            again_port = os.path.join(tmp, "port.obj")
            if (
                compile_without_lines(
                    base_build, os.path.relpath(base[key], base_build), again_base
                )
                and compile_without_lines(
                    port_build, os.path.relpath(port[key], port_build), again_port
                )
                and compare(again_base, again_port, tmp)[0]
            ):
                continue
            print("CHANGED", key, f"({reason})")


if __name__ == "__main__":
    main()
