# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The watch's flash log: dump the log of one boot, decode a dump.

usage: python -m plt.flashlog dump GENERATION OUT.bin [--url URL]
       python -m plt.flashlog decode LOG.DICT.JSON DUMP.bin

Generation 0 is the current boot, 1 the one before, and so on; the watch keeps
about ten. A dump is decoded with the log dictionary of the build that wrote
it (log.dict.json, kept next to every registered image).

Decoded columns: time, level (0 always, 1 error, 50 warning, 100 info, 200
debug), file:line, the message and, after "<-", its arguments. Lines written
by another firmware, such as the stock recovery firmware, stay as they are.
"""

import argparse
import json
import struct
import sys
import threading
import time

from plt.config import DEVCONN_URL

LOG_DUMP_ENDPOINT = 2002
DUMP_CHUNK = 0x80
DUMP_DONE = 0x81
DUMP_EMPTY = 0x82
DUMP_COOKIE = 0x5EC0DE01
DUMP_SECONDS = 90
RECORD_HEADER = 24
HASHED_PREFIX = "NL:"


def decode(dictionary, raw):
    """Yield the lines of a flash log dump, hashed messages expanded."""
    offset = 0
    while offset + RECORD_HEADER <= len(raw):
        timestamp, level, length, line = struct.unpack_from(">IBBH", raw, offset)
        source = raw[offset + 8 : offset + RECORD_HEADER].split(b"\0")[0]
        source = source.decode("latin1")
        offset += RECORD_HEADER
        message = raw[offset : offset + length].decode("latin1")
        offset += length
        if message.startswith(HASHED_PREFIX):
            token, _, arguments = message[len(HASHED_PREFIX) :].partition(" ")
            try:
                entry = dictionary.get(str(int(token, 16)))
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                message = f"{entry['msg']}  <- {arguments}"
                source = entry["file"].split("/")[-1]
                line = entry["line"]
        clock = time.strftime("%H:%M:%S", time.localtime(timestamp))
        yield f"{clock} {level:3d} {source}:{line} {message}"


def decode_file(dict_json, dump, out):
    """Write a dump, decoded with a log dictionary file, to an open text file."""
    with open(dict_json) as f:
        dictionary = json.load(f)
    with open(dump, "rb") as f:
        raw = f.read()
    for line in decode(dictionary, raw):
        print(line, file=out)


def dump(generation, out, url=DEVCONN_URL):
    """Fetch one boot's flash log over the developer connection into a file."""
    from plt.devconn import connect, send_raw

    pebble = connect(url)
    print("watch:", pebble.watch_info.running.version_tag)
    done = threading.Event()
    chunks = []
    # libpebble2 dispatches registered packet types only: read the raw packets.
    read_packet = pebble.transport.read_packet

    def tapped_read_packet():
        target, data = read_packet()
        if isinstance(data, (bytes, bytearray)) and len(data) >= 4:
            length, endpoint = struct.unpack(">HH", data[:4])
            if endpoint == LOG_DUMP_ENDPOINT:
                body = data[4 : 4 + length]
                if body[0] == DUMP_CHUNK:
                    chunks.append(body[5:])
                elif body[0] == DUMP_DONE:
                    print("dump done", body.hex())
                    done.set()
                elif body[0] == DUMP_EMPTY:
                    print("no logs", body.hex())
                    done.set()
                else:
                    print("cmd", hex(body[0]), body[:16].hex())
        return target, data

    pebble.transport.read_packet = tapped_read_packet
    if generation <= 1:
        payload = struct.pack("<BI", generation, DUMP_COOKIE)
    else:
        payload = struct.pack("<BBI", 0x10, generation, DUMP_COOKIE)
    send_raw(pebble, LOG_DUMP_ENDPOINT, payload)
    done.wait(DUMP_SECONDS)
    print("chunks:", len(chunks), "bytes:", sum(map(len, chunks)))
    with open(out, "wb") as f:
        f.write(b"".join(chunks))


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.flashlog",
        description="Dump the watch's flash log, or decode a dump.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    dumper = commands.add_parser(
        "dump", help="fetch one boot's log over the developer connection"
    )
    dumper.add_argument(
        "generation", type=int, help="0: the current boot, 1: the one before, ..."
    )
    dumper.add_argument("out", help="file to write the raw log to")
    dumper.add_argument(
        "--url",
        default=DEVCONN_URL,
        help=f"developer connection (default {DEVCONN_URL})",
    )
    decoder = commands.add_parser("decode", help="print a dump as text")
    decoder.add_argument("dict", help="log.dict.json of the build that wrote the log")
    decoder.add_argument("dump", help="raw log, as `dump` writes it")
    args = parser.parse_args()
    if args.command == "dump":
        dump(args.generation, args.out, args.url)
    else:
        decode_file(args.dict, args.dump, sys.stdout)


if __name__ == "__main__":
    main()
