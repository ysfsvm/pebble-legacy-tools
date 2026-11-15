# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The watch behind the phone: status, flash log, screenshot, guarded install, recovery."""

import os
import re
import subprocess
import time

from plt import config, flashlog
from plt.emu import tests_passed
from plt.images import Registry
from plt.util import PltError, now, run, run_limited, sha256, stamp

FIRMWARE_VERSION_RE = re.compile(rb"v4\.\d+\.\d+-\d+-g[0-9a-f]+")
# Routine lines the summary of a log leaves out.
LOG_NOISE = ("dls_", "data length", "pfs.c:543", "No AppInbox", "Booting")
SUMMARY_MAX_LEVEL = 50
INSTALL_SECONDS = 560
BOOT_POLLS = 12
BOOT_POLL_SECONDS = 20
RECOVERY_SECONDS = 900
RECOVERY_WINDOWS = 40


def adb_ready():
    """Forward the developer connection's port to the phone on adb; False without a phone."""
    lines = run([config.ADB, "devices"], capture_output=True).stdout.splitlines()
    devices = [line.split("\t")[0] for line in lines if line.endswith("\tdevice")]
    if not devices:
        return False
    # A phone on the cable and on the network shows up twice: the network one is used.
    serial = next((d for d in devices if ":" in d), devices[0])
    os.environ["ANDROID_SERIAL"] = serial
    port = f"tcp:{config.DEVCONN_PORT}"
    for device in devices:
        if device != serial:
            run(
                [config.ADB, "-s", device, "forward", "--remove", port],
                capture_output=True,
            )
    run([config.ADB, "forward", port, port], capture_output=True)
    return True


def require_phone():
    """Stop unless a phone is on adb."""
    if not adb_ready():
        raise PltError("phone not on adb")


def watch_version(cfg, timeout=30):
    """The version the watch reports over the developer connection, or None."""
    result = run_limited([*cfg.module_command("plt.devconn"), "version"], timeout)
    match = re.search(r"VERSION (\S+)", result.stdout)
    return match.group(1) if match else None


def install_bundle(cfg, pbz):
    """Install a bundle through the phone app; returns the app's last status line."""
    result = run_limited(
        [*cfg.module_command("plt.devconn"), "install", pbz], INSTALL_SECONDS
    )
    lines = result.stdout.strip().splitlines() or ["no output"]
    return next(
        (line for line in reversed(lines) if "InstallStatus" in line), lines[-1]
    )


def dump_log(cfg, registry, generation, tag, image=None, have=None):
    """Dump a flash log generation and decode it if its firmware is registered.

    Returns (path without suffix, version that wrote the log, whether it was
    decoded), or None when the watch gave no log. `have` names a dump made
    earlier, to decode again.
    """
    directory = cfg.logs / "watch"
    directory.mkdir(parents=True, exist_ok=True)
    base = have or str(directory / f"{stamp()}-{tag}-gen{generation}")
    if not have:
        command = cfg.module_command("plt.flashlog")
        result = run_limited([*command, "dump", str(generation), f"{base}.bin"], 120)
        if "dump done" not in result.stdout:
            return None
    with open(f"{base}.bin", "rb") as f:
        match = FIRMWARE_VERSION_RE.search(f.read())
    version = match.group(0).decode() if match else None
    entry = registry.find_version(version, image)[1] if version else None
    if not entry and generation == 0 and not have:
        # The log does not name its firmware: it is the one that runs now.
        running = watch_version(cfg)
        entry = registry.find_version(running, image)[1] if running else None
    if entry:
        with open(f"{base}.txt", "w") as out:
            flashlog.decode_file(entry["files"]["dict"], f"{base}.bin", out)
    return base, version, bool(entry)


def summarise(txt):
    """Print the size and time span of a decoded log and its warnings and errors."""
    with open(txt, errors="replace") as f:
        lines = f.read().splitlines()
    if not lines:
        print("  empty log")
        return
    first = lines[min(2, len(lines) - 1)][:8]
    print(f"  {len(lines)} lines, {first} .. {lines[-1][:8]}")
    for line in lines:
        fields = line.split(None, 2)
        if (
            len(fields) == 3
            and fields[1].isdigit()
            and int(fields[1]) <= SUMMARY_MAX_LEVEL
            and not any(noise in line for noise in LOG_NOISE)
        ):
            print("  " + line[:170])


def status(cfg):
    """Print whether the phone and the watch answer and which image the watch runs."""
    registry = Registry(cfg.images)
    require_phone()
    print(f"phone: on adb, port {config.DEVCONN_PORT} forwarded")
    version = watch_version(cfg)
    if not version:
        print(
            "watch: no answer on the dev connection "
            "(Core app not connected, or the dev connection is off)"
        )
        return
    name, _ = registry.find_version(version)
    print(f"watch: {version} ({name or 'not in the registry'})")
    print(
        f"registry: current={registry.role('current')} "
        f"recovery={registry.role('recovery')}"
    )


def log(cfg, generation):
    """Dump a flash log generation, decode it and print its summary."""
    registry = Registry(cfg.images)
    require_phone()
    dumped = dump_log(cfg, registry, generation, "log")
    if not dumped:
        raise PltError("no log came back (dev connection down?)")
    base, version, decoded = dumped
    print(f"{base}.bin  written by {version or 'unknown firmware'}")
    if decoded:
        summarise(f"{base}.txt")
    else:
        print("  no log dictionary for that firmware in the registry: not decoded")


def shot(cfg, out=None):
    """Save a screenshot of the watch and print its path."""
    require_phone()
    directory = cfg.logs / "watch"
    directory.mkdir(parents=True, exist_ok=True)
    dest = out or str(directory / f"{stamp()}-shot.png")
    result = run_limited([*cfg.module_command("plt.devconn"), "screenshot", dest], 90)
    if "SHOT" not in result.stdout:
        reason = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise PltError(f"screenshot failed: {reason}")
    print(dest)


def flash_problems(registry, entry):
    """Reasons an image should not go to the watch; --force overrides them."""
    problems = []
    if not tests_passed(entry):
        problems.append(
            "emulator tests and update test not recorded as passed (plt emu test)"
        )
    if not registry.role("recovery"):
        problems.append("no recovery image set (plt images role NAME recovery)")
    return problems


def flash(cfg, name, force=None):
    """Install an image on the watch and verify that the watch then runs it.

    Refuses an image without passed tests or without a recovery image set
    (unless forced, with a reason), and an image file that does not match its
    checksum. Backs up the watch's log first.
    """
    registry = Registry(cfg.images)
    entry = registry.get(name)
    problems = flash_problems(registry, entry)
    if sha256(entry["files"]["pbz"]) != entry["sha256"]:
        raise PltError("the image file does not match its registered sha256")
    if problems and not force:
        raise PltError(
            "not flashing:\n  "
            + "\n  ".join(problems)
            + "\n(--force 'reason' records the reason and goes on)"
        )
    require_phone()
    before = watch_version(cfg)
    if not before:
        raise PltError("the watch does not answer on the dev connection")
    print(f"watch runs {before}; installing {name} ({entry['version']})")
    backup = dump_log(cfg, registry, 0, f"pre-{name}")
    print(f"log backup: {backup[0] + '.bin' if backup else 'FAILED'}")
    install_status = install_bundle(cfg, entry["files"]["pbz"])
    print(f"phone: {install_status}")
    if "InstallStatus 0" not in install_status:
        raise PltError(
            "the phone did not report a completed install; "
            "the watch still runs what it ran"
        )
    after = None
    for _ in range(BOOT_POLLS):
        time.sleep(BOOT_POLL_SECONDS)
        adb_ready()
        after = watch_version(cfg, timeout=25)
        if after:
            break
    record = f"{now()} from {before}" + (f" (forced: {force})" if force else "")
    if after != entry["version"]:
        entry["installs"].append(f"{record} -> FAILED, watch answers {after}")
        registry.save()
        raise PltError(
            f"after the install the watch answers '{after}', expected "
            f"{entry['version']}. If it shows the recovery screen: plt watch recover"
        )
    entry["installs"].append(record)
    registry.set_role("current", name)
    registry.save()
    print(f"watch booted {after}")
    boot = dump_log(cfg, registry, 0, f"boot-{name}")
    if boot and boot[2]:
        summarise(f"{boot[0]}.txt")
    print(f"installed {name}")


def recover(cfg, name=None):
    """Install the recovery image, or a named one, over Bluetooth Classic."""
    registry = Registry(cfg.images)
    name = name or registry.role("recovery")
    if not name:
        raise PltError("no recovery image set (plt images role NAME recovery)")
    entry = registry.get(name)
    if not cfg.watch_bt:
        raise PltError(
            "PLT_WATCH_BT is not set (the watch's Bluetooth Classic address: "
            "bluetoothctl scan bredr)"
        )
    print(
        f"Recovery install of {name} ({entry['version']}) over Bluetooth Classic.\n"
        "Before this runs: phone Bluetooth OFF, watch on the charger, watch on the "
        "PRF screen.\n"
        "In PRF: never hold Down (factory reset); hold Select if no pairing screen "
        "comes."
    )
    directory = cfg.logs / "watch"
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{stamp()}-recover-{name}.log"
    command = cfg.module_command("plt.recover")
    command += ["install", cfg.watch_bt, entry["files"]["pbz"]]
    command += ["--windows", str(RECOVERY_WINDOWS)]
    with open(log_path, "w") as f:
        run_limited(command, RECOVERY_SECONDS, stdout=f, stderr=subprocess.STDOUT)
    tail = _recovery_tail(log_path)
    print("\n".join(tail))
    print(f"log: {log_path}")
    if not any("DONE" in line for line in tail):
        raise PltError("the recovery install did not finish")
    entry["installs"].append(f"{now()} recovery install from PRF")
    registry.set_role("current", name)
    registry.save()


def _recovery_tail(log_path, count=8):
    """The last lines of a recovery log, without the transfer progress."""
    progress = re.compile(r"^  (firmware|resources): \d+/")
    with open(log_path, errors="replace") as f:
        lines = [line.rstrip("\n") for line in f if not progress.match(line)]
    return lines[-count:]
