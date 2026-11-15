# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Runs in the build container, from the firmware worktree: the measurement key sequence.

usage: emu_perf_script.py <build dir> <qemu> <out dir> [f0|f2]

Boots a snowy emulator build made with CONFIG_LEGACY_PERF_PROBE, triggers the
key sequence and prints the PERF lines of the console. f0 (default) plays one
run, f2 the whole protocol: Bluetooth stopped, three runs.
"""

import struct
import sys
import time

from emu_session import booted_watch, dehash_console

PORT = 5564
LOG_DUMP_ENDPOINT = 2002
ONE_RUN = 0xF0
PROTOCOL = 0xF2
SECONDS = {ONE_RUN: 50, PROTOCOL: 270}


def main():
    build, qemu, out = sys.argv[1:4]
    mode = int(sys.argv[4], 16) if len(sys.argv) > 4 else ONE_RUN
    with booted_watch(build, qemu, out, PORT, quiet=True) as pebble:
        time.sleep(3)
        payload = struct.pack("<BBI", 0x10, mode, 0x1234)
        pebble.transport.send_packet(
            struct.pack(">HH", len(payload), LOG_DUMP_ENDPOINT) + payload
        )
        time.sleep(SECONDS.get(mode, SECONDS[ONE_RUN]))
    with open(dehash_console(build, out), errors="replace") as console:
        for line in console:
            if "PERF" in line:
                print(line.rstrip()[:230])


if __name__ == "__main__":
    main()
