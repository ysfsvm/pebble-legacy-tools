# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Operations over the Core app's developer connection.

usage: python -m plt.devconn [--url URL] version
       python -m plt.devconn [--url URL] screenshot OUT.png
       python -m plt.devconn [--url URL] install BUNDLE.pbz
       python -m plt.devconn [--url URL] measure

The phone's developer connection must be on and its port forwarded
(adb forward tcp:9000 tcp:9000). Needs libpebble2, pypng and websocket-client.
"""

import argparse
import struct
import time

from plt.config import DEVCONN_URL

LOG_DUMP_ENDPOINT = 2002
# Developer connection frames.
FRAME_FROM_WATCH = 0x00
FRAME_PHONE_LOG = 0x02
FRAME_INSTALL_STATUS = 0x05
FRAME_INSTALL_FIRMWARE = 0x0D
INSTALL_SECONDS = 600
# Log dump request that starts the measurement protocol instead: three runs
# with Bluetooth stopped.
MEASURE_GENERATION = 0xF2
MEASURE_COOKIE = 0x5EC0DE03


def connect(url=DEVCONN_URL):
    """A running connection to the watch behind the developer connection."""
    from libpebble2.communication import PebbleConnection
    from libpebble2.communication.transports.websocket import WebsocketTransport

    pebble = PebbleConnection(WebsocketTransport(url))
    pebble.connect()
    pebble.run_async()
    return pebble


def send_raw(pebble, endpoint, payload):
    """Send a Pebble Protocol packet libpebble2 has no type for."""
    pebble.transport.send_packet(struct.pack(">HH", len(payload), endpoint) + payload)


def version(url):
    """Print the version of the firmware the watch runs."""
    print("VERSION", connect(url).watch_info.running.version_tag)


def screenshot(url, out):
    """Save the watch's screen as a PNG."""
    import png
    from libpebble2.services.screenshot import Screenshot

    image = Screenshot(connect(url)).grab_image()
    png.from_array(image, mode="RGB;8").save(out)
    print("SHOT", len(image), "rows")


def install(url, pbz):
    """Have the phone app install a firmware bundle; print what it reports."""
    import websocket

    with open(pbz, "rb") as f:
        bundle = f.read()
    ws = websocket.create_connection(url, timeout=10)
    ws.send_binary(bytes([FRAME_INSTALL_FIRMWARE]) + bundle)
    print(f"sent InstallFirmware, {len(bundle)} bytes", flush=True)
    deadline = time.time() + INSTALL_SECONDS
    while time.time() < deadline:
        try:
            _, data = ws.recv_data()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as error:  # noqa: BLE001
            print("socket closed:", error, flush=True)
            break
        if not data or data[0] == FRAME_FROM_WATCH:
            continue
        kind, body = data[0], data[1:]
        if kind == FRAME_INSTALL_STATUS:
            status = int.from_bytes(body[:4], "big") if len(body) >= 4 else -1
            verdict = "ok" if status == 0 else "fail"
            print(f"InstallStatus {status} ({verdict})", flush=True)
            if status != 0:
                break
        elif kind == FRAME_PHONE_LOG:
            print("phone:", body.decode(errors="replace").strip()[:200], flush=True)
        else:
            print(f"type 0x{kind:02x} len {len(body)}", flush=True)


def measure(url):
    """Start the measurement protocol on a measurement image."""
    pebble = connect(url)
    send_raw(
        pebble,
        LOG_DUMP_ENDPOINT,
        struct.pack("<BBI", 0x10, MEASURE_GENERATION, MEASURE_COOKIE),
    )
    time.sleep(1)
    print("SENT")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.devconn",
        description="Operations over the Core app's developer connection.",
    )
    parser.add_argument(
        "--url",
        default=DEVCONN_URL,
        help=f"developer connection (default {DEVCONN_URL})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="print the firmware version of the watch")
    shot = commands.add_parser("screenshot", help="save the watch's screen as a PNG")
    shot.add_argument("out", help="PNG file to write")
    inst = commands.add_parser(
        "install", help="install a firmware bundle through the phone app"
    )
    inst.add_argument("bundle", help="firmware bundle (.pbz)")
    commands.add_parser(
        "measure", help="start the measurement protocol on a measurement image"
    )
    args = parser.parse_args()
    if args.command == "version":
        version(args.url)
    elif args.command == "screenshot":
        screenshot(args.url, args.out)
    elif args.command == "install":
        install(args.url, args.bundle)
    else:
        measure(args.url)


if __name__ == "__main__":
    main()
