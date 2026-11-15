# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Clean release builds of a firmware worktree, registered as images."""

import glob
import os
import shlex

from plt import config, container
from plt.images import Registry, bundle_version
from plt.util import PltError, check_name, matching_lines, output

BOARD = "snowy@dvt"
RELEASE_OPTIONS = (
    "CONFIG_RELEASE=y",
    "CONFIG_LEGACY_EMOJI_PACK=y",
    "CONFIG_BT_LE_IDLE_SLAVE_LATENCY=30",
)


def _git(worktree, *args):
    return output([config.GIT, "-C", str(worktree), *args])


def worktree_commit(worktree):
    """The short commit of a worktree; uncommitted changes are an error."""
    if not _git(worktree, "rev-parse", "--is-inside-work-tree") == "true":
        raise PltError(f"{worktree} is not a git worktree (--wt, PLT_WORKTREE)")
    dirty = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PltError(
            f"worktree {worktree} has uncommitted changes; commit them first:\n{dirty}"
        )
    return _git(worktree, "rev-parse", "--short=9", "HEAD")


def worktree_branch(worktree):
    """The branch a worktree is on."""
    return _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")


def define_args(options):
    """Kconfig options as the -D arguments of `pbl configure`."""
    return " ".join(shlex.quote(f"-D{option}") for option in options)


def memory_use(log):
    """The FLASH and RAM use the linker printed last, e.g. 'FLASH: 90.57% RAM: 88.39%'."""
    regions = []
    with open(log, errors="replace") as f:
        for line in f:
            fields = line.split()
            if fields and ("FLASH:" in line or "RAM:" in line):
                regions.append(f"{fields[0]} {fields[-1]}")
    return " ".join(regions[-2:])


def newest_bundle(build_dir):
    """The newest firmware bundle in a build directory."""
    bundles = sorted(glob.glob(f"{build_dir}/*.pbz"), key=os.path.getmtime)
    if not bundles:
        raise PltError(f"the build left no bundle in {build_dir}")
    return bundles[-1]


def configure_build(cfg, worktree, build_dir, options, log, bundle=True):
    """Configure and build from an empty build directory, in the container."""
    bdir = shlex.quote(build_dir)
    steps = f"pbl configure -b {bdir} --board {BOARD} {define_args(options)}"
    steps += f" && pbl build -b {bdir}"
    if bundle:
        steps += f" && pbl bundle -b {bdir}"
    script = f"rm -rf {bdir}; ({steps}) > {shlex.quote(str(log))} 2>&1; echo rc=$?"
    result = container.run_script(cfg, worktree, script)
    if "rc=0" not in result.stdout:
        errors = ""
        if os.path.exists(log):
            errors = matching_lines(log, "error", skip="error.c")
        raise PltError(f"build failed, log {log}\n{errors}".rstrip())


def build_image(cfg, name, worktree, extra_options):
    """Build the worktree's commit with the release options and register the image."""
    check_name(name)
    registry = Registry(cfg.images)
    if name in registry.images:
        raise PltError(
            f"image '{name}' exists; pick another name (an image is never overwritten)"
        )
    commit = worktree_commit(worktree)
    options = [*RELEASE_OPTIONS, *extra_options]
    log = cfg.logs / "build" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    build_dir = f"build-img-{name}"
    print(f"building {name} from {commit} ({' '.join(options)}), about 5 min")
    configure_build(cfg, worktree, build_dir, options, log)
    pbz = newest_bundle(worktree / build_dir)
    version = bundle_version(pbz)
    if version.endswith("-dirty") or commit not in version:
        raise PltError(
            f"the build labelled itself {version}, not a clean {commit}: something "
            "touched the worktree or its git index while it built. "
            "Not registered; build again."
        )
    memory = memory_use(log)
    entry = registry.register(
        name,
        pbz,
        worktree / build_dir / "log.dict.json",
        worktree / build_dir / "pebbleos.elf",
        commit=commit,
        options=options,
        extra_options=list(extra_options),
        worktree=str(worktree),
        memory=memory,
        branch=worktree_branch(worktree),
    )
    print(f"{name}: {entry['version']} sha {entry['sha256'][:16]} {memory}")
