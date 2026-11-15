# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Shared by the scripts that run in the build container, from the firmware worktree:
boot a snowy emulator build behind the stock bootloader and connect to it as a phone."""

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The worktree's own tools and test harness first, this package last.
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
sys.path.insert(0, os.path.join(os.getcwd(), "tests/integration"))
sys.path.append(str(Path(__file__).resolve().parents[2]))

from harness.connections import PHONE_OS_ANDROID, _phone_app_version
from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import (
    MessageTargetQemu,
    QemuTransport,
)
from libpebble2.communication.transports.qemu.protocol import (
    QemuBluetoothConnection,
)

from plt.stock_boot import BOARDS, build_stock_images

BOOT_SECONDS = 45


def announce_phone(pebble, settle):
    """Tell the watch that a phone is connected and which app it runs."""
    pebble.transport.send_packet(
        QemuBluetoothConnection(connected=True), target=MessageTargetQemu()
    )
    time.sleep(settle)


def send_phone_version(pebble):
    """Answer as the Android app does when asked for its version."""
    pebble.send_packet(_phone_app_version(PHONE_OS_ANDROID, False))


@contextlib.contextmanager
def booted_watch(build, qemu, out, port, quiet=False):
    """Boot a build and yield a connection to it; QEMU stops when the block ends.

    The console goes to <out>/console.log. `quiet` drops QEMU's own output.
    """
    os.makedirs(out, exist_ok=True)
    board = BOARDS["snowy"]
    console = os.path.join(out, "console.log")
    sink = subprocess.DEVNULL if quiet else None
    with tempfile.TemporaryDirectory() as tmp:
        micro, image = build_stock_images(board, build, tmp)
        spi = os.path.join(tmp, "spi.bin")
        Path(spi).write_bytes(image)
        cmd = [qemu, "-rtc", "base=localtime", "-serial", "null"]
        cmd += ["-serial", f"tcp:127.0.0.1:{port},server=on,wait=off,nodelay=on"]
        cmd += ["-serial", f"file:{console}"]
        cmd += ["-machine", "pebble-snowy-bb", "-cpu", "cortex-m4", "-kernel", micro]
        cmd += [*board["flash_args"](spi), "-display", "none"]
        emulator = subprocess.Popen(cmd, stdout=sink, stderr=sink)
        try:
            time.sleep(BOOT_SECONDS)
            pebble = PebbleConnection(QemuTransport("127.0.0.1", port))
            pebble.connect()
            announce_phone(pebble, 2)
            pebble.run_async()
            send_phone_version(pebble)
            yield pebble
        finally:
            emulator.terminate()
            emulator.wait()


def dehash_console(build, out):
    """Decode <out>/console.log with the build's log dictionary; returns the result's path."""
    console = os.path.join(os.path.abspath(out), "console.log")
    dehashed = os.path.join(os.path.abspath(out), "console.dehashed.log")
    dictionary = os.path.join(os.path.abspath(build), "log.dict.json")
    with open(console, "rb") as source, open(dehashed, "wb") as target:
        subprocess.run(
            [sys.executable, "dehash.py", dictionary],
            cwd="tools/log_hashing",
            stdin=source,
            stdout=target,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return dehashed
