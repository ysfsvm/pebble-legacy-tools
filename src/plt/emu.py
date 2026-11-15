# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Emulator builds, the integration suite and the firmware update test."""

import os
import shlex

from plt import config, container
from plt.build import (
    BOARD,
    RELEASE_OPTIONS,
    configure_build,
    define_args,
    memory_use,
    worktree_commit,
)
from plt.images import Registry
from plt.util import PltError, matching_lines, now, stamp

EMULATED = "CONFIG_SOC_STM32F4_EMULATED=y"
WAKEUP_TIMER = "CONFIG_TIMER_STM32_RTC_WUT"
# The clock event comes from SysTick, or from the RTC wakeup timer.
VARIANTS = (("emu", ()), ("emuwut", (f"{WAKEUP_TIMER}=y",)))
SUITES = "system ui notifications"
SUITE_PASS = "14 passed"
UPDATE_PASS = "ok"
UPDATE_FAIL = "FAIL: the watch does not run the bundle's version"


def update_verdict(reported, bundle):
    """The last line of the update test: it passes only on the reported version."""
    return UPDATE_PASS if reported is not None and reported == bundle else UPDATE_FAIL


def tests_passed(entry):
    """Whether an image's recorded emulator tests and update test all passed."""
    tests = entry.get("tests", {})
    suites = all(
        SUITE_PASS in tests.get(f"itest_{variant}", "") for variant, _ in VARIANTS
    )
    return suites and tests.get("fwupdate_emuwut", "").endswith(UPDATE_PASS)


def _itest(cfg, worktree, build_dir, results, tail):
    """Run the integration suite on an emulator build; returns its last lines."""
    script = (
        f"PEBBLE_QEMU_BIN={shlex.quote(cfg.qemu)} "
        f"bash {shlex.quote(str(config.DATA_DIR / 'itest.sh'))} "
        f"{shlex.quote(str(worktree))} {shlex.quote(build_dir)} "
        f"{shlex.quote(str(results))} -q {SUITES} 2>&1 | tail -{tail}"
    )
    return container.run_script(cfg, worktree, script, network=True).stdout.strip()


def _update_test(cfg, worktree, build_dir, results):
    """Send an emulator build its own bundle; returns the test's result lines."""
    bdir = shlex.quote(build_dir)
    log = shlex.quote(str(results / "fwupdate.log"))
    script = (
        f"python3 {shlex.quote(str(config.DATA_DIR / 'emu_fw_update.py'))} {bdir} "
        f"{shlex.quote(cfg.qemu)} $(ls -t {bdir}/*.pbz | head -1) "
        f"{shlex.quote(str(results / 'fwupdate'))} > {log} 2>&1; "
        f"grep -E '^(transfer|ok|FAIL|Traceback)' {log}"
    )
    result = container.run_script(cfg, worktree, script, network=True)
    return " / ".join(result.stdout.strip().splitlines()) or "no output"


def test_image(cfg, name, worktree, update_only=False):
    """Build an image's commit for the emulator, test it and record the results.

    Returns True when both suites and the update test passed.
    """
    registry = Registry(cfg.images)
    entry = registry.get(name)
    if "commit" not in entry:
        raise PltError(
            "this image was not built by plt: its commit and options are unknown"
        )
    commit = worktree_commit(worktree)
    if commit != entry["commit"]:
        raise PltError(
            f"worktree is at {commit}, the image was built from {entry['commit']}"
        )
    extra = entry.get("extra_options", [])
    results = cfg.logs / "emu" / name
    results.mkdir(parents=True, exist_ok=True)
    tests = dict(entry.get("tests", {})) if update_only else {}
    if update_only:
        print("update test only, about 2 min")
    else:
        print("emulator builds and tests, about 12 min")
        for variant, variant_options in VARIANTS:
            build_dir = f"build-img-{name}-{variant}"
            # Options of the wakeup timer do not exist in the build without it.
            kept = [
                option
                for option in extra
                if variant_options or not option.startswith(f"{WAKEUP_TIMER}_")
            ]
            configure_build(
                cfg,
                worktree,
                build_dir,
                [EMULATED, *variant_options, *kept],
                results / f"{variant}-build.log",
                bundle=(variant == "emuwut"),
            )
            lines = _itest(cfg, worktree, build_dir, results / f"itest-{variant}", 1)
            line = (lines.splitlines() or ["no output"])[-1]
            tests[f"itest_{variant}"] = line
            print(f"  {variant}: {line}")
    tests["fwupdate_emuwut"] = _update_test(
        cfg, worktree, f"build-img-{name}-emuwut", results
    )
    print(f"  update: {tests['fwupdate_emuwut']}")
    tests["when"] = now()
    entry["tests"] = tests
    registry.save()
    passed = tests_passed(entry)
    print("PASS" if passed else "FAIL")
    return passed


def try_worktree(cfg, worktree, options, wut=False, perf=None, hw=False):
    """Build the working tree as it is, incrementally, and run it in the emulator.

    Runs the integration suite, or the measurement key sequence with `perf`;
    with `hw` only compiles the hardware build.
    """
    variant = "hw" if hw else ("emuwut" if wut else "emu")
    build_dir = f"build-dev-{variant}"
    results = cfg.logs / "emu" / f"dev-{stamp()}-{os.path.basename(str(worktree))}"
    results.mkdir(parents=True, exist_ok=True)
    if hw:
        build_options = [*RELEASE_OPTIONS, *options]
    else:
        build_options = [EMULATED, *([f"{WAKEUP_TIMER}=y"] if wut else []), *options]
    defines = define_args(build_options)
    marker = f"{build_dir}/.plt-options"
    # Configure again only when the options changed.
    script = (
        f'[ "$(cat {marker} 2>/dev/null)" = {shlex.quote(defines)} ] || '
        f"{{ rm -rf {build_dir}; pbl configure -b {build_dir} --board {BOARD} "
        f"{defines} > {shlex.quote(str(results / 'configure.log'))} 2>&1 && "
        f"echo {shlex.quote(defines)} > {marker}; }}; "
        f"pbl build -b {build_dir} > {shlex.quote(str(results / 'build.log'))} 2>&1; "
        "echo rc=$?"
    )
    result = container.run_script(cfg, worktree, script)
    build_log = results / "build.log"
    if "rc=0" not in result.stdout:
        errors = ""
        if build_log.exists():
            errors = matching_lines(
                build_log, "error", skip="error.c", limit=8, after=2
            )
        raise PltError(f"build failed, log {build_log}\n{errors}".rstrip())
    if hw:
        print(f"hardware build compiles: {memory_use(build_log)}")
        warnings = matching_lines(build_log, "warning:")
        if warnings:
            print(warnings)
        return
    if perf:
        script = (
            f"python3 {shlex.quote(str(config.DATA_DIR / 'emu_perf_script.py'))} "
            f"{build_dir} {shlex.quote(cfg.qemu)} "
            f"{shlex.quote(str(results / 'perf'))} {perf} 2>&1"
        )
        result = container.run_script(cfg, worktree, script, network=True)
        print(result.stdout.strip() or result.stderr.strip()[-400:])
    else:
        print(_itest(cfg, worktree, build_dir, results / "itest", 3))
    print(f"results: {results}")
