# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The PC-side install, against a stand-in watch on an emulator-style TCP port."""

import socket
import struct
import sys
import threading
import types
import zlib

import pytest

from conftest import write_bundle
from plt import recover

FIRMWARE = bytes(range(256)) * 20
RESOURCES = b"resources" * 300


class FakeWatch(threading.Thread):
    """Speaks enough Pebble Protocol, in QEMU frames, to take a firmware update."""

    def __init__(self, answer_status=False, nack=None):
        super().__init__(daemon=True)
        self.server = socket.create_server(("127.0.0.1", 0))
        self.address = f"tcp:127.0.0.1:{self.server.getsockname()[1]}"
        self.answer_status = answer_status
        self.nack = nack
        self.objects = {}
        self.inits = []
        self.starts = []
        self.crcs = {}
        self.complete = False
        self.connections = 0
        self.current = None
        self.ended = threading.Semaphore(0)

    def stop(self):
        """Wait until every connection so far has ended, then stop listening."""
        for _ in range(self.connections):
            assert self.ended.acquire(timeout=5)
        self.server.close()

    def written(self, kind):
        return len(self.objects.get(kind, b""))

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                self.serve(conn)
            self.ended.release()

    def serve(self, conn):
        buffer = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            buffer += data
            while len(buffer) >= 6:
                protocol, length = struct.unpack(">HH", buffer[2:6])
                if len(buffer) < 6 + length + 2:
                    break
                frame, buffer = buffer[6 : 6 + length], buffer[6 + length + 2 :]
                if protocol == recover.QEMU_PROTOCOL_PEBBLE:
                    size, endpoint = struct.unpack(">HH", frame[:4])
                    self.handle(conn, endpoint, frame[4 : 4 + size])

    def reply(self, conn, endpoint, body):
        packet = struct.pack(">HH", len(body), endpoint) + body
        header = struct.pack(">HH", recover.QEMU_PROTOCOL_PEBBLE, len(packet))
        conn.sendall(recover.QEMU_HEADER + header + packet + recover.QEMU_FOOTER)

    def handle(self, conn, endpoint, body):
        if endpoint == recover.ENDPOINT_SYSTEM:
            if body[1] == recover.UPDATE_STATUS and self.answer_status:
                status = struct.pack(
                    "<BBBBIIII",
                    0,
                    recover.UPDATE_STATUS,
                    0,
                    0,
                    self.written(recover.OBJECT_RESOURCES),
                    0,
                    self.written(recover.OBJECT_FIRMWARE),
                    0,
                )
                self.reply(conn, endpoint, status)
            elif body[1] == recover.UPDATE_START:
                self.starts.append(struct.unpack("<II", body[2:10]))
                self.reply(conn, endpoint, b"\x00\x0a\x01")
            elif body[1] == recover.UPDATE_COMPLETE:
                self.complete = True
        elif endpoint == recover.ENDPOINT_PUTBYTES:
            command = body[0]
            if command == self.nack:
                self.reply(conn, endpoint, b"\x02\x00\x00\x00\x00")
                return
            if command == recover.PUT_INIT:
                size, kind = struct.unpack(">IB", body[1:6])
                offset = 0
                if len(body) > 7:
                    magic, offset = struct.unpack(">II", body[7:15])
                    assert magic == recover.RESUME_MAGIC
                self.inits.append((kind, size, offset))
                self.current = kind
                self.objects[kind] = self.objects.get(kind, b"")[:offset]
            elif command == recover.PUT_DATA:
                self.objects[self.current] += body[9:]
            elif command == recover.PUT_COMMIT:
                self.crcs[self.current] = struct.unpack(">I", body[5:9])[0]
            self.reply(conn, endpoint, b"\x01" + struct.pack(">I", 7))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """No waiting, and a checksum module in place of libpebble2's."""
    monkeypatch.setattr(recover.time, "sleep", lambda seconds: None)
    crc = types.ModuleType("libpebble2.util.stm32_crc")
    crc.crc32 = zlib.crc32
    for name in ("libpebble2", "libpebble2.util"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "libpebble2.util.stm32_crc", crc)


@pytest.fixture
def bundle(tmp_path):
    return write_bundle(tmp_path / "fw.pbz", "v4.47.0-1-gabc", FIRMWARE, RESOURCES)


def start(**kwargs):
    watch = FakeWatch(**kwargs)
    watch.start()
    return watch


def test_install_sends_firmware_then_resources_then_complete(bundle, capsys):
    watch = start(answer_status=True)
    assert recover.install(watch.address, bundle, windows=2) is True
    watch.stop()
    assert watch.objects == {
        recover.OBJECT_FIRMWARE: FIRMWARE,
        recover.OBJECT_RESOURCES: RESOURCES,
    }
    assert watch.inits == [
        (recover.OBJECT_FIRMWARE, len(FIRMWARE), 0),
        (recover.OBJECT_RESOURCES, len(RESOURCES), 0),
    ]
    assert watch.starts == [(0, len(FIRMWARE) + len(RESOURCES))]
    assert watch.crcs == {
        recover.OBJECT_FIRMWARE: zlib.crc32(FIRMWARE),
        recover.OBJECT_RESOURCES: zlib.crc32(RESOURCES),
    }
    assert watch.complete
    out = capsys.readouterr().out
    assert out.startswith(
        f"bundle v4.47.0-1-gabc fw {len(FIRMWARE)} B res {len(RESOURCES)} B\n"
    )
    assert "firmware: committed+installed" in out
    assert "FirmwareUpdateComplete sent" in out


def test_an_interrupted_install_goes_on_where_the_watch_is(bundle, capsys):
    watch = start(answer_status=True)
    assert recover.install(watch.address, bundle, windows=3, cut_after=3000) is True
    watch.stop()
    assert watch.connections == 2
    assert watch.objects[recover.OBJECT_FIRMWARE] == FIRMWARE
    assert watch.objects[recover.OBJECT_RESOURCES] == RESOURCES
    kind, size, offset = watch.inits[1]
    assert kind == recover.OBJECT_FIRMWARE
    assert offset == 2000
    assert size == len(FIRMWARE) - 2000
    assert watch.starts[1] == (2000, len(FIRMWARE) - 2000 + len(RESOURCES))
    assert watch.crcs[recover.OBJECT_FIRMWARE] == zlib.crc32(FIRMWARE)
    assert "window ended: ConnectionError: test cut" in capsys.readouterr().out


def test_a_refused_write_ends_the_window(bundle, capsys):
    watch = start(answer_status=True, nack=recover.PUT_COMMIT)
    assert recover.install(watch.address, bundle, windows=2) is False
    watch.stop()
    assert watch.connections == 2
    assert not watch.complete
    assert "firmware commit: NACK" in capsys.readouterr().out


def test_resume_offset():
    assert recover._resume_offset(0, 100) == 0
    assert recover._resume_offset(40, 100) == 40
    assert recover._resume_offset(100, 100) == 100
    assert recover._resume_offset(150, 100) == 100


def test_packets_without_frames_are_split_on_their_length():
    ours, theirs = socket.socketpair()
    link = recover.Link.__new__(recover.Link)
    link.relayed = link.framed = False
    link.cut = None
    link.buffer = b""
    link.sent = 0
    link.sock = ours
    first = struct.pack(">HH", 3, 0x10) + b"abc"
    second = struct.pack(">HH", 2, 0xBEEF) + b"\x01\x02"
    theirs.sendall(first + second[:3])
    theirs.sendall(second[3:])
    assert link.recv(0xBEEF, timeout=2) == b"\x01\x02"
    link.send(0x11, b"xy")
    assert theirs.recv(64) == struct.pack(">HH", 2, 0x11) + b"xy"
    theirs.close()
    with pytest.raises(ConnectionError):
        link.recv(0x10, timeout=2)
    link.close()


def test_only_bluetooth_addresses_are_paired(monkeypatch):
    def no_bluetoothctl(*args, **kwargs):
        raise AssertionError("bluetoothctl must not run")

    monkeypatch.setattr(recover, "_bluetoothctl", no_bluetoothctl)
    assert recover.pair("tcp:127.0.0.1:1")
    assert recover.pair("ws://127.0.0.1:9000/")
    recover.forget("tcp:127.0.0.1:1")


def test_pairing_scans_only_for_an_unknown_device(monkeypatch):
    address = "00:11:22:33:44:55"
    calls = []

    def bluetoothctl(*args, limit=None):
        command = next(a for a in args if a in ("devices", "scan", "pair"))
        calls.append(command)
        out = ""
        if command == "devices" and "scan" in calls:
            out = f"Device {address} Pebble Time"
        elif command == "pair":
            out = "Pairing successful"
        return types.SimpleNamespace(stdout=out, stderr="")

    monkeypatch.setattr(recover, "_bluetoothctl", bluetoothctl)
    assert recover.pair(address)
    assert calls == ["devices", "scan", "devices", "pair"]
    calls.clear()
    calls.append("scan")
    assert recover.pair(address)
    assert calls == ["scan", "devices", "pair"]
