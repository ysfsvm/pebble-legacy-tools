# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Runs in the build container, from the firmware worktree: the firmware update test.

usage: emu_fw_update.py <build dir> <qemu> <bundle.pbz> <out dir>

Boots a snowy emulator build and sends it a full bundle (firmware and system
resources) over the emulated Pebble Protocol link, as the phone app does. The
watch restarts and the stock bootloader installs the bundle. The last line is
"ok" only when the watch then reports the bundle's version.
"""

import sys
import time

from emu_session import announce_phone, booted_watch, dehash_console, send_phone_version
from harness.helpers.firmware import FirmwareBundle, install_firmware
from libpebble2.protocol.system import WatchVersion, WatchVersionRequest

from plt.emu import update_verdict

PORT = 5563
REBOOT_SECONDS = 60
VERSION_ATTEMPTS = 6


def reported_version(pebble):
    """The version the restarted watch reports, or None if it never answers."""
    for _ in range(VERSION_ATTEMPTS):
        try:
            announce_phone(pebble, 2)
            send_phone_version(pebble)
            time.sleep(1)
            answer = pebble.send_and_read(
                WatchVersion(data=WatchVersionRequest()), WatchVersion, timeout=10
            )
            return answer.data.running.version_tag
        except Exception as error:  # noqa: BLE001
            print("version read after the update failed:", repr(error), flush=True)
            time.sleep(10)
    return None


def main():
    build, qemu, pbz, out = sys.argv[1:5]
    with booted_watch(build, qemu, out, PORT) as pebble:
        time.sleep(2)
        print("fw before:", pebble.watch_info.running.version_tag, flush=True)
        bundle = FirmwareBundle(pbz)
        print("bundle:", bundle.version_tag, bundle.size, "bytes", flush=True)
        start = time.time()
        install_firmware(pebble, bundle, start_timeout=60)
        print(
            f"transfer+install done in {time.time() - start:.0f} s; "
            "waiting for the reboot",
            flush=True,
        )
        time.sleep(REBOOT_SECONDS)
        after = reported_version(pebble)
        print("fw after:", after, flush=True)
        print(update_verdict(after, bundle.version_tag), flush=True)
    dehash_console(build, out)


if __name__ == "__main__":
    main()
