# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Helpers the commands share: the error type, external programs, time stamps."""

import datetime
import hashlib
import re
import subprocess

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class PltError(Exception):
    """An expected failure, reported as one message and exit status 1."""


def run(cmd, **kwargs):
    """Run a program to its end and return the completed process."""
    try:
        return subprocess.run(cmd, check=False, text=True, **kwargs)
    except FileNotFoundError:
        raise PltError(f"{cmd[0]} is not installed or not on PATH") from None


def output(cmd):
    """Standard output of a program, without surrounding white space."""
    return run(cmd, capture_output=True).stdout.strip()


def run_limited(cmd, seconds, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """Run a program, kill it after `seconds`, and return what it wrote until then."""
    try:
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=True)
    except FileNotFoundError:
        raise PltError(f"{cmd[0]} is not installed or not on PATH") from None
    try:
        out, err = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, out or "", err or "")


def now():
    """The local time as written into the registry."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def stamp():
    """The local time as used in file names."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256(path):
    """SHA-256 of a file, in hexadecimal."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_name(name):
    """Refuse an image name that cannot be used in file names and build scripts."""
    if not NAME_RE.fullmatch(name):
        raise PltError(
            f"'{name}' is not a usable image name: letters, digits, '.', '_' and '-'"
        )
    return name


def matching_lines(path, needle, skip=None, limit=5, after=0):
    """The first lines of a file that contain `needle`, numbered as grep -n does.

    `after` adds that many following lines to each match; `limit` counts every
    line returned.
    """
    found = []
    pending = 0
    with open(path, errors="replace") as f:
        for number, line in enumerate(f, 1):
            line = line.rstrip()
            if skip and skip in line:
                continue
            if needle in line:
                found.append(f"{number}:{line}")
                pending = after
            elif pending:
                found.append(f"{number}-{line}")
                pending -= 1
            if len(found) >= limit:
                break
    return "\n".join(found[:limit])
