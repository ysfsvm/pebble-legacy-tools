# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Decoding of flash log dumps."""

import io
import json
import struct
import time

from plt import flashlog


def record(timestamp, level, line, source, message):
    body = message.encode("latin1")
    header = struct.pack(">IBBH", timestamp, level, len(body), line)
    return header + source.encode().ljust(16, b"\0") + body


def clock(timestamp):
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


DICTIONARY = {
    str(0x1234): {"msg": "Battery %u mV", "file": "src/fw/battery.c", "line": 77},
}


def test_hashed_lines_are_expanded_and_plain_lines_kept():
    raw = (
        record(1000, 100, 5, "x.c", "NL:1234 e74")
        + record(1001, 50, 139, "reboot_reason.c", "Safely rebooted")
        + record(1002, 200, 9, "y.c", "NL:1234")
    )
    assert list(flashlog.decode(DICTIONARY, raw)) == [
        f"{clock(1000)} 100 battery.c:77 Battery %u mV  <- e74",
        f"{clock(1001)}  50 reboot_reason.c:139 Safely rebooted",
        f"{clock(1002)} 200 battery.c:77 Battery %u mV  <- ",
    ]


def test_unknown_and_malformed_tokens_stay_as_they_are():
    raw = record(5, 0, 1, "a.c", "NL:ffff 1 2") + record(6, 0, 2, "b.c", "NL:zz")
    assert list(flashlog.decode(DICTIONARY, raw)) == [
        f"{clock(5)}   0 a.c:1 NL:ffff 1 2",
        f"{clock(6)}   0 b.c:2 NL:zz",
    ]


def test_a_truncated_tail_is_ignored():
    raw = record(7, 0, 1, "a.c", "whole") + b"\x00" * 10
    assert len(list(flashlog.decode({}, raw))) == 1


def test_decode_file(tmp_path):
    dictionary = tmp_path / "log.dict.json"
    dictionary.write_text(json.dumps(DICTIONARY))
    dump = tmp_path / "dump.bin"
    dump.write_bytes(record(1000, 100, 5, "x.c", "NL:1234 1"))
    out = io.StringIO()
    flashlog.decode_file(dictionary, dump, out)
    assert out.getvalue() == f"{clock(1000)} 100 battery.c:77 Battery %u mV  <- 1\n"
