# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The build container: which image, what it sees, how a script runs in it."""

import glob
import os
import re
import shlex
import subprocess
from pathlib import Path

from plt import config
from plt.util import PltError, output, run

CI_IMAGE_RE = re.compile(r"ghcr\.io/coredevices/pebbleos-docker:v\d+")

# What every script needs before it can build in a mounted worktree.
PRELUDE = (
    "git config --system --add safe.directory '*'; git status >/dev/null; "
    "pip install -q -r requirements.txt >/dev/null 2>&1; "
    "pip install -q intelhex >/dev/null 2>&1; "
)


def _present(image):
    return run([config.PODMAN, "image", "exists", image]).returncode == 0


def image_for(cfg, worktree):
    """The container the worktree's CI names, if present locally, else the configured one.

    Nothing is pulled: an image that is not present is an error.
    """
    for workflow in sorted(glob.glob(f"{worktree}/.github/workflows/*.yml")):
        match = CI_IMAGE_RE.search(Path(workflow).read_text(errors="replace"))
        if match and _present(match.group(0)):
            return match.group(0)
    if not _present(cfg.container):
        raise PltError(
            f"build container {cfg.container} is not present "
            f"(podman pull {cfg.container}, or set PLT_CONTAINER)"
        )
    return cfg.container


def mount_args(cfg, worktree, extra=()):
    """The podman arguments that mount each needed directory at its own path.

    PLT_MOUNTS replaces the computed list: the worktree with its git
    directory, this package, the logs and the directory of an absolute QEMU.
    """
    if cfg.mounts:
        dirs = [str(d) for d in cfg.mounts]
    else:
        common = output(
            [
                config.GIT,
                "-C",
                str(worktree),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ]
        )
        qemu_dir = os.path.dirname(cfg.qemu) if os.path.isabs(cfg.qemu) else None
        candidates = [worktree, common, config.PACKAGE_DIR, cfg.logs, qemu_dir, *extra]
        dirs = sorted({os.path.abspath(d) for d in candidates if d})
        # A directory inside another mounted one is already visible.
        dirs = [
            d
            for d in dirs
            if not any(d != o and d.startswith(o.rstrip("/") + "/") for o in dirs)
        ]
    cfg.pip_cache.mkdir(parents=True, exist_ok=True)
    cfg.logs.mkdir(parents=True, exist_ok=True)
    args = ["-v", f"{cfg.pip_cache}:{config.CONTAINER_PIP_CACHE}:Z"]
    for d in dirs:
        args += ["-v", f"{d}:{d}:Z"]
    return args


def run_in(cfg, worktree, argv, extra_mounts=(), network=False):
    """Run a command in the build container and return the completed process.

    The container has its own network namespace, so emulator runs in two
    worktrees do not compete for ports; PLT_HOSTNET puts a run that asks for
    the network on the host's.
    """
    cmd = [config.PODMAN, "run", "--rm", "--pull=never"]
    if network and cfg.hostnet:
        cmd += ["--network", "host"]
    cmd += mount_args(cfg, worktree, extra_mounts)
    cmd += [image_for(cfg, worktree), *argv]
    return run(cmd, capture_output=True)


def run_script(cfg, worktree, script, network=False):
    """Run a shell script in the container, from the prepared worktree."""
    script = f"cd {shlex.quote(str(worktree))} && {PRELUDE}{script}"
    return run_in(cfg, worktree, ["bash", "-c", script], network=network)


def toolchain_nm(cfg, elf):
    """Output of the toolchain's nm for an ELF, through the container if needed."""
    nm = config.TOOLCHAIN_PREFIX + "nm"
    flags = ["-S", "-n", "--defined-only"]
    if os.path.exists(nm):
        cmd = [nm, *flags, str(elf)]
    else:
        directory, name = os.path.split(os.path.abspath(elf))
        cmd = [config.PODMAN, "run", "--rm", "--pull=never"]
        cmd += ["-v", f"{directory}:/e:Z", cfg.container, nm, *flags, f"/e/{name}"]
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ""
