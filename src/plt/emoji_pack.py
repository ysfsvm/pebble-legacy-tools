# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Install an emoji pack on a watch as the file "emoji".

usage: python -m plt.emoji_pack PACK [ws://127.0.0.1:9000/ | qemu:PORT]

PACK is a firmware bundle (.pbz) that carries the pack, or the pack itself
(the output of tools/emoji_pack.py in the firmware tree). The target is the
Core app's developer connection by default, or the control port of a running
emulator. Without the file the watch draws its built-in emoji; deleting the
file, or installing a firmware without the emoji store, removes the pack
again. Talking to the watch needs libpebble2.
"""

import argparse
import json
import time
import zipfile

from plt.config import DEVCONN_URL
from plt.util import PltError

EMOJI_FILE = "emoji"
CRC_POLY = 0x04C11DB7


def stm32_crc(data):
    """The STM32 CRC the bundle manifest lists for its files, as libpebble2 computes it."""
    crc = 0xFFFFFFFF
    for i in range(0, len(data), 4):
        word = data[i : i + 4]
        if len(word) < 4:
            word = bytes(reversed(word)).ljust(4, b"\0")
        crc ^= int.from_bytes(word, "little")
        for _ in range(32):
            crc = (crc << 1) ^ CRC_POLY if crc & 0x80000000 else crc << 1
        crc &= 0xFFFFFFFF
    return crc


def read_pack(path):
    """The emoji pack in a firmware bundle, checked against its manifest, or a pack file."""
    if not zipfile.is_zipfile(path):
        with open(path, "rb") as f:
            return f.read()
    with zipfile.ZipFile(path) as bundle:
        try:
            manifest = json.loads(bundle.read("manifest.json"))
        except (KeyError, ValueError) as error:
            raise PltError(f"{path}: no readable manifest.json") from error
        entry = next(
            (f for f in manifest.get("files", []) if f.get("target") == EMOJI_FILE),
            None,
        )
        if entry is None:
            raise PltError(f"{path}: the bundle carries no emoji pack")
        try:
            pack = bundle.read(entry["name"])
        except KeyError as error:
            raise PltError(f"{path}: {entry['name']} is missing") from error
    if len(pack) != entry.get("size") or stm32_crc(pack) != entry.get("crc"):
        raise PltError(f"{path}: {entry['name']} does not match the manifest")
    return pack


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


def install(pack, target):
    """Send the pack to the watch behind target; returns (watch firmware, seconds)."""
    try:
        from libpebble2.exceptions import PebbleError
        from libpebble2.services.putbytes import PutBytes, PutBytesType
    except ImportError as error:
        raise PltError(
            "talking to the watch needs libpebble2: install plt with [watch]"
        ) from error
    try:
        pebble = connect(target)
        version = pebble.watch_info.running.version_tag
    except (PebbleError, OSError) as error:
        raise PltError(
            f"no answer from {target} ({error or type(error).__name__}): is the "
            "app's LAN developer connection on, the app open and the watch connected?"
        ) from error
    start = time.time()
    try:
        PutBytes(pebble, PutBytesType.File, pack, bank=0, filename=EMOJI_FILE).send()
    except (PebbleError, OSError) as error:
        raise PltError(
            f"the transfer stopped ({error or type(error).__name__}); run it again"
        ) from error
    return version, time.time() - start


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.emoji_pack",
        description='Install an emoji pack on a watch as the file "emoji".',
    )
    parser.add_argument("pack", help="firmware bundle (.pbz) or emoji pack file")
    parser.add_argument(
        "target",
        nargs="?",
        default=DEVCONN_URL,
        help=f"developer connection URL or qemu:PORT (default {DEVCONN_URL})",
    )
    args = parser.parse_args()
    pack = read_pack(args.pack)
    version, seconds = install(pack, args.target)
    print("watch:", version)
    print(f"emoji pack installed: {len(pack)} bytes in {seconds:.1f} s")


if __name__ == "__main__":
    main()
