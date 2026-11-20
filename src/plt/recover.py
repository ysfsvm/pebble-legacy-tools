# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Reach a watch from the PC without the phone: install, log, liveness, core dump.

usage: python -m plt.recover install ADDRESS BUNDLE.pbz [--windows N] [--cut-after BYTES]
       python -m plt.recover log ADDRESS OUT_PREFIX [--complete]
       python -m plt.recover alive ADDRESS [--minutes M]
       python -m plt.recover coredump ADDRESS OUT.bin [--windows N]

ADDRESS is the watch's Bluetooth Classic address (RFCOMM channel 1, paired
with bluetoothctl), tcp:HOST:PORT for the control port of an emulator, or
ws://HOST:PORT/ for the Core app's developer connection.

The stock recovery firmware keeps no bonds and drops idle links, so the work
is done in windows: pair, connect, go on until the link drops, try again.
`install` needs libpebble2 for the checksum the watch expects.
"""

import argparse
import contextlib
import json
import socket
import struct
import subprocess
import sys
import time
import zipfile

from plt import config, flashlog

# Pebble Protocol endpoints.
ENDPOINT_VERSION = 0x10
ENDPOINT_PHONE_VERSION = 0x11
ENDPOINT_SYSTEM = 0x12
ENDPOINT_LOG_DUMP = 0x7D2
ENDPOINT_GETBYTES = 0x2328
ENDPOINT_PUTBYTES = 0xBEEF

# System messages.
UPDATE_START = 0x01
UPDATE_COMPLETE = 0x02
UPDATE_STATUS = 0x0B

# PutBytes.
PUT_INIT, PUT_DATA, PUT_COMMIT, PUT_INSTALL = 0x01, 0x02, 0x03, 0x05
OBJECT_FIRMWARE = 0x01
OBJECT_RESOURCES = 0x03
RESUME_MAGIC = 0xBE4354EF
CHUNK = 2000

# QEMU wraps Pebble Protocol packets in its own frames on the control port.
QEMU_HEADER, QEMU_FOOTER = b"\xfe\xed", b"\xbe\xef"
QEMU_PROTOCOL_PEBBLE = 1
QEMU_PROTOCOL_BLUETOOTH = 3

COREDUMP_ERRORS = {
    1: "MalformedRequest",
    2: "InProgress",
    3: "DoesNotExist",
    4: "Corrupted",
}
# What the reboot reasons in the recovery firmware's log mention.
LOG_KEYWORDS = ("prf", "rebooted", "Stuck", "NEW_FW")


class LinkCut:
    """Drops a link once, after a number of sent bytes, to exercise the resume."""

    def __init__(self, after):
        self.after = after

    def check(self, sent):
        if self.after and sent > self.after:
            self.after = 0
            raise ConnectionError("test cut")


class Link:
    """A Pebble Protocol link over RFCOMM, an emulator port or the developer connection."""

    def __init__(self, address, cut=None):
        self.relayed = address.startswith("ws://")
        self.framed = address.startswith("tcp:")
        self.cut = cut
        self.buffer = b""
        self.sent = 0
        if self.relayed:
            import websocket

            self.ws_timeout = websocket.WebSocketTimeoutException
            self.ws = websocket.create_connection(address, timeout=15)
        elif self.framed:
            _, host, port = address.split(":")
            self.sock = socket.create_connection((host, int(port)), timeout=15)
            connected = struct.pack(">HH", QEMU_PROTOCOL_BLUETOOTH, 1) + b"\x01"
            self.sock.sendall(QEMU_HEADER + connected + QEMU_FOOTER)
            time.sleep(1)
        else:
            self.sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            self.sock.settimeout(15)
            self.sock.connect((address, 1))

    def send(self, endpoint, body):
        """Send one packet."""
        packet = struct.pack(">HH", len(body), endpoint) + body
        self.sent += len(body)
        if self.cut:
            self.cut.check(self.sent)
        if self.relayed:
            self.ws.send_binary(b"\x01" + packet)
            return
        if self.framed:
            header = struct.pack(">HH", QEMU_PROTOCOL_PEBBLE, len(packet))
            packet = QEMU_HEADER + header + packet + QEMU_FOOTER
        self.sock.sendall(packet)

    def _buffered(self):
        """Yield the complete (endpoint, body) packets in the receive buffer."""
        while True:
            if self.framed:
                start = self.buffer.find(QEMU_HEADER)
                if start < 0 or len(self.buffer) < start + 6:
                    return
                protocol, length = struct.unpack(
                    ">HH", self.buffer[start + 2 : start + 6]
                )
                end = start + 6 + length
                if len(self.buffer) < end + 2:
                    return
                frame, self.buffer = (
                    self.buffer[start + 6 : end],
                    self.buffer[end + 2 :],
                )
                if protocol != QEMU_PROTOCOL_PEBBLE or len(frame) < 4:
                    continue
                length, endpoint = struct.unpack(">HH", frame[:4])
                yield endpoint, frame[4 : 4 + length]
            else:
                if len(self.buffer) < 4:
                    return
                length, endpoint = struct.unpack(">HH", self.buffer[:4])
                if len(self.buffer) < 4 + length:
                    return
                body = self.buffer[4 : 4 + length]
                self.buffer = self.buffer[4 + length :]
                yield endpoint, body

    def recv(self, endpoint, timeout=10):
        """The next packet on an endpoint; other endpoints are dropped."""
        end = time.time() + timeout
        while time.time() < end:
            if self.relayed:
                self.ws.settimeout(max(0.2, end - time.time()))
                try:
                    frame = self.ws.recv()
                except self.ws_timeout:
                    continue
                # Frame type 0 relays a packet from the watch.
                if isinstance(frame, bytes) and len(frame) >= 5 and frame[0] == 0:
                    length, got = struct.unpack(">HH", frame[1:5])
                    if got == endpoint:
                        return frame[5 : 5 + length]
                continue
            for got, body in self._buffered():
                if got == endpoint:
                    return body
            self.sock.settimeout(max(0.2, end - time.time()))
            try:
                data = self.sock.recv(8192)
            except TimeoutError:
                continue
            if not data:
                raise ConnectionError("closed")
            self.buffer += data
        raise TimeoutError(f"no answer on {endpoint:#x}")

    def announce_phone(self):
        """Answer as a phone app, which the watch expects before anything else."""
        if self.relayed:
            return
        body = struct.pack(">BIIIBBBBQ", 1, 0xFFFFFFFF, 0, 2, 2, 4, 4, 3, 0x8FF)
        self.send(ENDPOINT_PHONE_VERSION, body)
        time.sleep(0.3)

    def close(self):
        with contextlib.suppress(Exception):
            (self.ws if self.relayed else self.sock).close()


def _bluetoothctl(*args, limit=None):
    """Run bluetoothctl, stopped after `limit` seconds if one is given."""
    command = [config.BLUETOOTHCTL, *args]
    if limit:
        command = ["timeout", str(limit), *command]
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _is_bluetooth(address):
    return not address.startswith(("tcp:", "ws://"))


def pair(address):
    """Pair with the watch; scans only while BlueZ does not know the device."""
    if not _is_bluetooth(address):
        return True
    if address not in _bluetoothctl("devices").stdout:
        _bluetoothctl("--timeout", "6", "scan", "bredr", limit=8)
        if address not in _bluetoothctl("devices").stdout:
            return False
    result = _bluetoothctl("--agent", "NoInputNoOutput", "pair", address, limit=10)
    return (
        "successful" in result.stdout
        or "already" in (result.stdout + result.stderr).lower()
    )


def forget(address):
    """Remove the bond, so that the next window pairs from scratch.

    The kernel can keep a link key that BlueZ no longer lists: the pairing
    then reports "already paired" and the link fails. Unpairing through the
    management interface drops that key; it is skipped where btmgmt is missing.
    """
    if not _is_bluetooth(address):
        return
    _bluetoothctl("remove", address)
    with contextlib.suppress(OSError):
        subprocess.run(
            ["timeout", "5", config.BTMGMT, "unpair", address],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )


def in_windows(address, windows, action, cut=None):
    """Run `action(link)` on a fresh link, in up to `windows` windows.

    Returns True once the action has completed.
    """
    for window in range(1, windows + 1):
        print(f"== window {window} {time.strftime('%H:%M:%S')}", flush=True)
        if not pair(address):
            print("  not paired", flush=True)
            time.sleep(1)
            continue
        link = None
        try:
            link = Link(address, cut)
            link.announce_phone()
            action(link)
            link.close()
            return True
        except Exception as error:  # noqa: BLE001
            print(f"  window ended: {type(error).__name__}: {error}", flush=True)
            if link:
                link.close()
            forget(address)
            # After a dropped transfer the watch times the old PutBytes out
            # before it accepts a new one.
            time.sleep(12 if "NACK" in str(error) else 1)
    return False


def _putbytes(link, body, what, timeout=15):
    """Send a PutBytes request and return the cookie of its acknowledgement."""
    link.send(ENDPOINT_PUTBYTES, body)
    answer = link.recv(ENDPOINT_PUTBYTES, timeout)
    if answer[0] != 1:
        raise RuntimeError(f"{what}: NACK {answer.hex()}")
    return struct.unpack(">I", answer[1:5])[0]


def put_object(link, kind, data, offset, what):
    """Write an object with PutBytes from an offset, then commit and install it."""
    from libpebble2.util.stm32_crc import crc32 as stm32_crc

    rest = data[offset:]
    init = struct.pack(">BIBB", PUT_INIT, len(rest), kind, 0)
    if offset:
        init += struct.pack(">II", RESUME_MAGIC, offset)
    cookie = _putbytes(link, init, f"{what} init", 60)
    start = time.time()
    sent = 0
    for i in range(0, len(rest), CHUNK):
        chunk = rest[i : i + CHUNK]
        # The first write waits for the region to be erased: seconds per sector.
        _putbytes(
            link,
            struct.pack(">BII", PUT_DATA, cookie, len(chunk)) + chunk,
            f"{what} put",
            90 if i == 0 else 20,
        )
        sent += len(chunk)
        if sent % 100000 < CHUNK:
            rate = sent / 1024 / max(time.time() - start, 1e-3)
            print(
                f"  {what}: {offset + sent}/{len(data)} ({rate:.1f} KiB/s)", flush=True
            )
    committing = time.time()
    # The watch checks the whole object in flash, and an install may erase the
    # resources in use: both take long.
    _putbytes(
        link,
        struct.pack(">BII", PUT_COMMIT, cookie, stm32_crc(data)),
        f"{what} commit",
        90,
    )
    print(f"  {what}: commit acked in {time.time() - committing:.1f} s", flush=True)
    _putbytes(link, struct.pack(">BI", PUT_INSTALL, cookie), f"{what} install", 60)
    seconds = time.time() - start
    print(
        f"  {what}: committed+installed ({len(rest)} B in {seconds:.0f} s)",
        flush=True,
    )


def read_bundle(pbz):
    """(version, firmware, resources) of a firmware bundle."""
    with zipfile.ZipFile(pbz) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        firmware = bundle.read(manifest["firmware"]["name"])
        resources = b""
        if "resources" in manifest:
            resources = bundle.read(manifest["resources"]["name"])
    return manifest["firmware"].get("versionTag"), firmware, resources


def _resume_offset(written, size):
    """Where to go on with an object of which the watch has `written` bytes."""
    if written >= size:
        return size
    return max(written, 0)


def send_bundle(link, firmware, resources):
    """Write the firmware and its resources and tell the watch the update is complete.

    Asks the watch first how much of an interrupted update it holds and goes
    on from there; a watch that does not answer gets everything.
    """
    link.send(ENDPOINT_SYSTEM, struct.pack("<BB", 0, UPDATE_STATUS))
    try:
        status = link.recv(ENDPOINT_SYSTEM, 5)
        res_written, _, fw_written, _ = struct.unpack("<IIII", status[4:20])
        print(
            f"  status: fw {fw_written} B written, res {res_written} B written",
            flush=True,
        )
    except TimeoutError:
        fw_written = res_written = 0
        print("  no status answer: starting from 0", flush=True)
    fw_offset = _resume_offset(fw_written, len(firmware))
    res_offset = _resume_offset(res_written, len(resources)) if resources else 0
    remaining = (len(firmware) - fw_offset) + (len(resources) - res_offset)
    link.send(
        ENDPOINT_SYSTEM,
        struct.pack("<BBII", 0, UPDATE_START, fw_offset + res_offset, remaining),
    )
    try:
        answer = link.recv(ENDPOINT_SYSTEM, 10)
        print(f"  fw-update-start response {answer.hex()}", flush=True)
    except TimeoutError:
        print("  no start response (continuing)", flush=True)
    if fw_offset < len(firmware):
        put_object(link, OBJECT_FIRMWARE, firmware, fw_offset, "firmware")
    if resources and res_offset < len(resources):
        put_object(link, OBJECT_RESOURCES, resources, res_offset, "resources")
    send_update_complete(link)
    print(
        "  FirmwareUpdateComplete sent; the watch should reboot into the new firmware",
        flush=True,
    )


def send_update_complete(link):
    """Tell the watch the update is complete; it then restarts to install it."""
    link.send(ENDPOINT_SYSTEM, struct.pack("<BB", 0, UPDATE_COMPLETE))
    time.sleep(2)


def install(address, pbz, windows=40, cut_after=0):
    """Install a full bundle; returns True when the watch took all of it."""
    try:
        import libpebble2.util.stm32_crc  # noqa: F401
    except ImportError:
        sys.exit(
            "install needs libpebble2 (in a clone of this repository: pip install '.[watch]')"
        )
    version, firmware, resources = read_bundle(pbz)
    print(f"bundle {version} fw {len(firmware)} B res {len(resources)} B", flush=True)
    cut = LinkCut(cut_after) if cut_after else None
    return in_windows(
        address, windows, lambda link: send_bundle(link, firmware, resources), cut
    )


def _dump_generation(link, generation):
    """The raw flash log of one boot."""
    cookie = 0x5EC0DE00 + generation
    link.send(ENDPOINT_LOG_DUMP, struct.pack("<BBI", 0x10, generation, cookie))
    chunks = []
    while True:
        packet = link.recv(ENDPOINT_LOG_DUMP, 15)
        if packet[0] == flashlog.DUMP_CHUNK:
            chunks.append(packet[5:])
        elif packet[0] in (flashlog.DUMP_DONE, flashlog.DUMP_EMPTY):
            return b"".join(chunks)


def dump_logs(address, prefix, complete=False, windows=200):
    """Save the flash logs of the three most recent boots; True when all came."""

    def action(link):
        for generation in (0, 1, 2):
            raw = _dump_generation(link, generation)
            with open(f"{prefix}-gen{generation}.bin", "wb") as f:
                f.write(raw)
            reasons = [
                line
                for line in flashlog.decode({}, raw)
                if any(keyword in line for keyword in LOG_KEYWORDS)
            ]
            preview = " | ".join(line[-70:] for line in reasons[:3])
            print(f"  gen {generation}: {len(raw)} B {preview}", flush=True)
        if complete:
            send_update_complete(link)
            print("  FirmwareUpdateComplete sent", flush=True)

    return in_windows(address, windows, action)


def probe(address, minutes=5, windows=400):
    """Ask the watch its version every 10 s and report how long each link lasts."""
    end = time.time() + minutes * 60
    for window in range(1, windows + 1):
        if time.time() > end:
            break
        print(f"== window {window} {time.strftime('%H:%M:%S')}", flush=True)
        if not pair(address):
            print("  not paired", flush=True)
            time.sleep(1)
            continue
        start = time.time()
        replies = 0
        link = None
        try:
            link = Link(address)
            link.announce_phone()
            while time.time() < end:
                link.send(ENDPOINT_VERSION, b"\x00")
                link.recv(ENDPOINT_VERSION, 10)
                replies += 1
                print(
                    f"  alive {time.strftime('%H:%M:%S')} ({replies} replies, "
                    f"link {time.time() - start:.0f} s)",
                    flush=True,
                )
                time.sleep(10)
            link.close()
            print(
                f"  link held for {time.time() - start:.0f} s until the end of the test",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001
            print(
                f"  link lost after {time.time() - start:.0f} s: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
            if link:
                link.close()
            forget(address)
            time.sleep(1)


def fetch_coredump(address, out, windows=20):
    """Save the watch's core dump; True also when the watch says it has none."""
    transaction = 7

    def action(link):
        link.send(ENDPOINT_GETBYTES, bytes([0x00, transaction]))
        start = time.time()
        info = link.recv(ENDPOINT_GETBYTES, 15)
        if info[0] != 0x01 or info[1] != transaction:
            raise RuntimeError(f"unexpected answer {info[:8].hex()}")
        error, total = struct.unpack(">BI", info[2:7])
        if error:
            reason = COREDUMP_ERRORS.get(error, error)
            print(f"  watch says: {reason} (no core dump to fetch)", flush=True)
            return
        print(f"  core dump: {total} bytes", flush=True)
        data = bytearray(total)
        received = 0
        while received < total:
            packet = link.recv(ENDPOINT_GETBYTES, 20)
            if packet[0] != 0x02 or packet[1] != transaction:
                continue
            offset = struct.unpack(">I", packet[2:6])[0]
            chunk = packet[6:]
            data[offset : offset + len(chunk)] = chunk
            received += len(chunk)
            if received % 50000 < len(chunk):
                rate = received / 1024 / max(time.time() - start, 1e-3)
                print(f"  {received}/{total} ({rate:.1f} KiB/s)", flush=True)
        with open(out, "wb") as f:
            f.write(data)
        seconds = time.time() - start
        print(f"  saved {total} bytes to {out} in {seconds:.0f} s", flush=True)

    return in_windows(address, windows, action)


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.recover",
        description="Reach a watch from the PC without the phone.",
        epilog="ADDRESS: a Bluetooth Classic address, tcp:HOST:PORT (emulator "
        "control port) or ws://HOST:PORT/ (developer connection).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sub = commands.add_parser(
        "install", help="install a full firmware bundle (firmware and resources)"
    )
    sub.add_argument("address", metavar="ADDRESS")
    sub.add_argument("bundle", help="firmware bundle (.pbz)")
    sub.add_argument(
        "--windows", type=int, default=40, help="pairing attempts (default 40)"
    )
    sub.add_argument(
        "--cut-after",
        type=int,
        default=0,
        metavar="BYTES",
        help="test aid: drop the link once after BYTES sent, to exercise the resume",
    )

    sub = commands.add_parser(
        "log", help="save the flash logs of the three most recent boots"
    )
    sub.add_argument("address", metavar="ADDRESS")
    sub.add_argument("prefix", help="writes PREFIX-gen0.bin to PREFIX-gen2.bin")
    sub.add_argument(
        "--complete",
        action="store_true",
        help="also send the update-complete message",
    )

    sub = commands.add_parser(
        "alive", help="ask the version every 10 s and report how long the link lasts"
    )
    sub.add_argument("address", metavar="ADDRESS")
    sub.add_argument(
        "--minutes", type=float, default=5, help="how long to probe (default 5)"
    )

    sub = commands.add_parser("coredump", help="save the watch's core dump")
    sub.add_argument("address", metavar="ADDRESS")
    sub.add_argument("out", help="file to write the core dump to")
    sub.add_argument(
        "--windows", type=int, default=20, help="pairing attempts (default 20)"
    )

    args = parser.parse_args()
    if args.command == "alive":
        probe(args.address, args.minutes)
        print("PROBE-DONE", flush=True)
        return 0
    if args.command == "install":
        done = install(args.address, args.bundle, args.windows, args.cut_after)
    elif args.command == "log":
        done = dump_logs(args.address, args.prefix, args.complete)
    else:
        done = fetch_coredump(args.address, args.out, args.windows)
    print("DONE" if done else "GAVE UP", flush=True)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
