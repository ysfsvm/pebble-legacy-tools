# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Install an emoji pack on a watch as the file "emoji".

usage: python -m plt.emoji_pack PACK [ws://127.0.0.1:9000/ | qemu:PORT]

PACK is the output of tools/emoji_pack.py in the firmware tree. The target is
the Core app's developer connection by default, or the control port of a
running emulator. Without the file the watch draws its built-in emoji;
deleting the file, or installing a firmware without the emoji store, removes
the pack again. Needs libpebble2.
"""

import argparse
import time

from plt.config import DEVCONN_URL

EMOJI_FILE = "emoji"


def connect(target):
    """A running connection to a watch behind a developer connection or QEMU."""
    from libpebble2.communication import PebbleConnection

    if target.startswith("qemu:"):
        from libpebble2.communication.transports.qemu import (
            MessageTargetQemu,
            QemuTransport,
        )
        from libpebble2.communication.transports.qemu.protocol import (
            QemuBluetoothConnection,
        )

        pebble = PebbleConnection(QemuTransport("127.0.0.1", int(target[5:])))
        pebble.connect()
        pebble.transport.send_packet(
            QemuBluetoothConnection(connected=True), target=MessageTargetQemu()
        )
        time.sleep(1)
    else:
        from libpebble2.communication.transports.websocket import WebsocketTransport

        pebble = PebbleConnection(WebsocketTransport(target))
        pebble.connect()
    pebble.run_async()
    return pebble


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.emoji_pack",
        description='Install an emoji pack on a watch as the file "emoji".',
    )
    parser.add_argument("pack", help="emoji pack file")
    parser.add_argument(
        "target",
        nargs="?",
        default=DEVCONN_URL,
        help=f"developer connection URL or qemu:PORT (default {DEVCONN_URL})",
    )
    args = parser.parse_args()

    from libpebble2.services.putbytes import PutBytes, PutBytesType

    with open(args.pack, "rb") as f:
        pack = f.read()
    pebble = connect(args.target)
    print("watch:", pebble.watch_info.running.version_tag)
    start = time.time()
    PutBytes(pebble, PutBytesType.File, pack, bank=0, filename=EMOJI_FILE).send()
    print(f"emoji pack installed: {len(pack)} bytes in {time.time() - start:.1f} s")


if __name__ == "__main__":
    main()
