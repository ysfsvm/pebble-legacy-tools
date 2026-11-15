# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Parsing of core dumps."""

import struct

import pytest

from plt import coredump

STACK = 0x20001000
TCB = 0x20002000


def chunk(key, body):
    return struct.pack("<II", key, len(body)) + body


def make_dump():
    header = struct.pack("<III", coredump.MAGIC, (3 << 8) | 1, 1700000000)
    header += b"X00000000000".ljust(16, b"\0") + b"v4.47.0-1-gabc".ljust(64, b"\0")
    registers = list(range(13)) + [STACK, 0x08004101, 0x08004200, 0x01000000]
    thread = b"KernelMain".ljust(16, b"\0") + struct.pack("<IB", TCB, 1)
    thread += struct.pack("<17I", *registers)
    stack = struct.pack("<4I", 0x08004123, 0x12345678, 0x20000000, 0x08004124)
    tcb = struct.pack("<I", STACK) + bytes(60)
    return (
        b"\xff" * 8
        + header
        + chunk(coredump.CHUNK_MEMORY, struct.pack("<I", STACK) + stack)
        + chunk(coredump.CHUNK_MEMORY, struct.pack("<I", TCB) + tcb)
        + chunk(coredump.CHUNK_THREAD, thread)
        + chunk(coredump.CHUNK_EXTRA_REGISTERS, b"\x01\x02")
        + chunk(coredump.CHUNK_END, b"")
        + b"ignored"
    )


def test_header_memory_and_threads():
    dump = coredump.CoreDump(make_dump())
    assert (dump.version, dump.core, dump.timestamp) == (3, 1, 1700000000)
    assert dump.serial == "X00000000000"
    assert dump.build == "v4.47.0-1-gabc"
    assert [(start, len(content)) for start, content in dump.memory] == [
        (STACK, 16),
        (TCB, 64),
    ]
    assert dump.extra_registers == b"\x01\x02"
    (thread,) = dump.threads
    assert thread["name"] == "KernelMain"
    assert thread["tcb"] == TCB
    assert thread["running"] == 1
    assert thread["registers"]["r12"] == 12
    assert thread["registers"]["sp"] == STACK
    assert thread["registers"]["pc"] == 0x08004200
    assert dump.read(STACK + 4, 4) == struct.pack("<I", 0x12345678)
    assert dump.read(STACK + 12, 8) is None


def test_no_magic():
    with pytest.raises(ValueError, match="F00DCAFE"):
        coredump.CoreDump(b"\0" * 512)


def test_return_addresses_are_marked(capsys):
    dump = coredump.CoreDump(make_dump())
    firmware = coredump.Firmware(bytes(0x1000), 0x08004000)
    firmware.disassembler = None
    coredump.print_thread(dump, dump.threads[0], firmware, 4)
    out = capsys.readouterr().out
    assert f"{STACK:#x}: 0x08004123  <- return address?" in out
    assert f"{STACK + 4:#x}: 0x12345678\n" in out
    # An even address in the firmware is no Thumb return address.
    assert f"{STACK + 12:#x}: 0x08004124\n" in out
    assert f"tcb: top-of-stack {STACK:#x}" in out


def test_stack_outside_the_dump(capsys):
    dump = coredump.CoreDump(make_dump())
    coredump.print_thread(dump, dump.threads[0], None, 96)
    assert "stack not in dump" in capsys.readouterr().out
