# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""A stand-in TI CC2564B for QEMU's USART1: enough HCI to boot the transport and the host.

usage: python -m plt.fake_cc2564 PORT LOG [wedge|dead]

Listens on 127.0.0.1:PORT (QEMU: -serial tcp:127.0.0.1:PORT), answers every HCI command with a
Command Complete (vendor commands of TI's init script included) and logs every packet. Once the
host has enabled eHCILL and been quiet for a second, it asks to sleep (GO_TO_SLEEP_IND), as the
chip does, and from then on answers the host's WAKE_UP_IND with WAKE_UP_ACK. Packets the host
sends while the controller is asleep, without waking it first, are logged as errors.

After the first sleep, a phone "connects": the controller wakes the host (WAKE_UP_IND, expecting
WAKE_UP_ACK) and reports an LE connection. Commands that take a Command Status get one and their
completion event; ACL data from the host is acknowledged with Number Of Completed Packets. The
phone never answers at the ATT level, so the host's requests time out.

With "wedge", the controller stops answering once the phone has connected, until the driver
boots it again (Read Local Version, as after a power cycle). With "dead", it never answers
again.
"""

import argparse
import os
import select
import socket
import struct

BD_ADDR = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])  # little endian

HCILL = {
    0x30: "GO_TO_SLEEP_IND",
    0x31: "GO_TO_SLEEP_ACK",
    0x32: "WAKE_UP_IND",
    0x33: "WAKE_UP_ACK",
}


def ret_params(opcode, params):
    ogf = opcode >> 10
    ok = b"\x00"
    if ogf == 0x3F:
        return ok  # vendor (init script, baud rate, sleep mode)
    table = {
        0x1001: ok + struct.pack("<BHBHH", 6, 0x0000, 6, 0x000D, 0x1B0F),  # 4.0, TI
        0x1002: ok + bytes(64),
        0x1003: ok
        + bytes([0, 0, 0, 0, 0x60, 0, 0, 0]),  # LE supported (controller), no BR/EDR
        0x1005: ok + struct.pack("<HBHH", 1021, 0, 4, 0),
        0x1009: ok + BD_ADDR,
        0x2002: ok + struct.pack("<HB", 27, 4),
        0x2003: ok + bytes([0x01, 0, 0, 0, 0, 0, 0, 0]),  # LE encryption
        0x2007: ok + b"\x00",  # adv TX power
        0x200F: ok + b"\x08",  # white list size
        0x2018: ok + os.urandom(8),
        0x2017: ok + bytes(16),  # LE Encrypt (not used: no real AES here)
        0x201C: ok + bytes([0xFF] * 8),
    }
    if opcode in table:
        return table[opcode]
    if ogf in (0x01, 0x02):  # link control / policy: none expected
        return b"\x01"
    return ok


HANDLE = 0x0040
PEER = bytes([0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xC6])  # random static


# Commands answered with Command Status, and the event that completes them.
def status_cmd(opcode, params):
    if opcode == 0x0406:  # Disconnect
        return bytes([0x05, 4, 0x00]) + struct.pack("<H", HANDLE) + bytes([0x16])
    if opcode == 0x041D:  # Read Remote Version
        return bytes([0x0C, 8, 0x00]) + struct.pack("<HBHH", HANDLE, 9, 0x000F, 0x0001)
    if opcode == 0x2016:  # LE Read Remote Features
        return (
            bytes([0x3E, 12, 0x04, 0x00])
            + struct.pack("<H", HANDLE)
            + bytes([0x01] + [0] * 7)
        )
    if opcode == 0x2013:  # LE Connection Update
        return bytes([0x3E, 10, 0x03, 0x00]) + struct.pack("<HHHH", HANDLE, 36, 4, 600)
    if opcode == 0x2019:  # LE Start Encryption
        return bytes([0x08, 4, 0x00]) + struct.pack("<H", HANDLE) + bytes([0x01])
    return None


def conn_complete():
    p = bytes([0x01, 0x00]) + struct.pack("<H", HANDLE) + bytes([0x01, 0x01]) + PEER
    p += struct.pack("<HHHB", 24, 0, 500, 0)
    return bytes([0x04, 0x3E, len(p)]) + p


def serve(port, log, mode=None):
    """Accept one connection from QEMU and play the controller until it closes."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    log.write(f"listening on {port}\n")
    conn, _ = srv.accept()
    log.write("QEMU connected\n")
    buf = b""
    ncmd = 0
    hcill = False  # the host sent the sleep mode command
    asleep = False
    sleeps = wakes = 0
    phase = "idle"  # idle -> waking -> connected
    wedged = False
    while True:
        ready, _, _ = select.select([conn], [], [], 1.0)
        if not ready:
            if phase == "idle" and asleep and sleeps >= 1:
                conn.sendall(b"\x32")
                phase = "waking"
                log.write("-> WAKE_UP_IND (phone connects)\n")
                continue
            if hcill and not asleep and phase != "waking":
                conn.sendall(b"\x30")
                asleep = True
                sleeps += 1
                log.write(f"-> GO_TO_SLEEP_IND ({sleeps})\n")
            continue
        data = conn.recv(4096)
        if not data:
            break
        buf += data
        while buf:
            t = buf[0]
            if t in HCILL:
                log.write(f"HCILL {HCILL[t]}\n")
                buf = buf[1:]
                if t == 0x32:
                    asleep = False
                    wakes += 1
                    conn.sendall(b"\x33")
                    log.write(f"-> WAKE_UP_ACK ({wakes})\n")
                elif t == 0x33 and phase == "waking":
                    asleep = False
                    phase = "connected"
                    conn.sendall(conn_complete())
                    log.write("-> LE Connection Complete\n")
                    if mode:
                        wedged = True
                        log.write("WEDGED: no more answers until the next boot\n")
                continue
            if asleep:
                log.write(f"ERROR: packet type {t:02x} while asleep\n")
            if t == 0x01:
                if len(buf) < 4 or len(buf) < 4 + buf[3]:
                    break
                opcode = buf[1] | (buf[2] << 8)
                params = buf[4 : 4 + buf[3]]
                buf = buf[4 + buf[3] :]
                ncmd += 1
                if wedged:
                    if opcode != 0x1001 or mode == "dead":
                        log.write(f"CMD {ncmd} {opcode:04x} ignored (wedged)\n")
                        continue
                    wedged = False
                    hcill = False
                    phase = "rebooted"
                    log.write("UNWEDGED: the driver booted the controller again\n")
                if opcode == 0xFD0C:
                    hcill = True
                done = status_cmd(opcode, params)
                if done is not None:
                    conn.sendall(
                        bytes([0x04, 0x0F, 4, 0x00, 0x01]) + struct.pack("<H", opcode)
                    )
                    conn.sendall(bytes([0x04]) + done)
                    log.write(
                        f"CMD {ncmd} {opcode:04x} -> status, then event {done[0]:02x}\n"
                    )
                    if opcode == 0x0406:
                        phase = "disconnected"
                    continue
                rp = ret_params(opcode, params)
                evt = bytes([0x01]) + struct.pack("<H", opcode) + rp
                conn.sendall(bytes([0x04, 0x0E, len(evt)]) + evt)
                if ncmd <= 400 or opcode >> 10 != 0x3F:
                    log.write(
                        f"CMD {ncmd} {opcode:04x} ({opcode >> 10:02x}:{opcode & 0x3FF:03x}) "
                        f"len {len(params)} -> status {rp[0]:02x}\n"
                    )
                continue
            if t == 0x02 and wedged:
                if len(buf) < 5 or len(buf) < 5 + (buf[3] | (buf[4] << 8)):
                    break
                log.write("ACL ignored (wedged)\n")
                buf = buf[5 + (buf[3] | (buf[4] << 8)) :]
                continue
            if t == 0x02:
                if len(buf) < 5:
                    break
                ln = buf[3] | (buf[4] << 8)
                if len(buf) < 5 + ln:
                    break
                handle = (buf[1] | (buf[2] << 8)) & 0x0FFF
                cid = buf[7] | (buf[8] << 8) if ln >= 4 else -1
                log.write(
                    f"ACL handle {handle:03x} len {ln} cid {cid:04x} {buf[9 : 9 + 8].hex()}\n"
                )
                buf = buf[5 + ln :]
                conn.sendall(
                    bytes([0x04, 0x13, 5, 0x01]) + struct.pack("<HH", handle, 1)
                )
                continue
            log.write(f"junk byte {t:02x}\n")
            buf = buf[1:]
    log.write("closed\n")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m plt.fake_cc2564",
        description="A stand-in TI CC2564B for the Bluetooth UART of the emulator.",
    )
    parser.add_argument("port", type=int, help="TCP port to listen on (127.0.0.1)")
    parser.add_argument("log", help="file that receives one line per packet")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["wedge", "dead"],
        help="wedge: stop answering once the phone has connected, until the driver "
        "boots the controller again; dead: never answer again",
    )
    args = parser.parse_args()
    with open(args.log, "w", buffering=1) as log:
        serve(args.port, log, args.mode)


if __name__ == "__main__":
    main()
