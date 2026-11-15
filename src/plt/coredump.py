# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Parse a Pebble core dump: memory regions, and every thread's registers and stack.

usage: python -m plt.coredump DUMP [--at OFFSET] [--fw FIRMWARE.bin] [--fw-base ADDRESS]
                              [--thread NAME] [--stack-words N]

DUMP is a raw dump as GetBytes returns it (python -m plt.recover coredump),
or a flash image with --at. With --fw, stack words that point into the
firmware are marked as possible return addresses, and the code around each is
disassembled if capstone is installed.
"""

import argparse
import struct
import sys

MAGIC = 0xF00DCAFE
HEADER_SIZE = 92
CHUNK_THREAD = 2
CHUNK_EXTRA_REGISTERS = 3
CHUNK_MEMORY = 4
CHUNK_END = 0xFFFFFFFF
REGISTERS = (
    *(f"r{n}" for n in range(13)),
    "sp",
    "lr",
    "pc",
    "xpsr",
)
# Stack words shown even when they are no return address.
STACK_HEAD_WORDS = 16


class CoreDump:
    """The content of a core dump."""

    def __init__(self, data, offset=0):
        start = data.find(struct.pack("<I", MAGIC), offset, offset + 0x100)
        if start < 0:
            raise ValueError("no F00DCAFE magic near the given offset")
        _, version, self.timestamp = struct.unpack_from("<III", data, start)
        self.version = version >> 8
        self.core = version & 0xFF
        self.serial = _text(data[start + 12 : start + 28])
        self.build = _text(data[start + 28 : start + HEADER_SIZE])
        self.memory = []
        self.threads = []
        self.extra_registers = None
        offset = start + HEADER_SIZE
        while offset + 8 <= len(data):
            key, size = struct.unpack_from("<II", data, offset)
            if key == CHUNK_END:
                break
            body = data[offset + 8 : offset + 8 + size]
            if key == CHUNK_MEMORY:
                self.memory.append((struct.unpack_from("<I", body)[0], body[4:]))
            elif key == CHUNK_THREAD:
                tcb, running = struct.unpack_from("<IB", body, 16)
                registers = struct.unpack_from("<17I", body, 21)
                self.threads.append(
                    {
                        "name": _text(body[:16]),
                        "tcb": tcb,
                        "running": running,
                        "registers": dict(zip(REGISTERS, registers)),
                    }
                )
            elif key == CHUNK_EXTRA_REGISTERS:
                self.extra_registers = body
            offset += 8 + size

    def read(self, address, length):
        """Bytes of the dumped memory, or None if the dump does not hold them."""
        for start, content in self.memory:
            if start <= address and address + length <= start + len(content):
                return content[address - start : address - start + length]
        return None


def _text(raw):
    return raw.split(b"\0")[0].decode(errors="replace")


class Firmware:
    """A firmware image, to recognise and disassemble return addresses."""

    def __init__(self, image, base):
        self.image = image
        self.base = base
        try:
            import capstone

            self.disassembler = capstone.Cs(
                capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB
            )
        except ImportError:
            self.disassembler = None

    def holds(self, address):
        return self.base <= address < self.base + len(self.image)

    def around(self, address, before=6, after=1):
        """Disassembly lines around an address, the address itself marked."""
        if not self.disassembler:
            return []
        start = (address - before * 2) & ~1
        code = self.image[start - self.base : address + after * 4 - self.base]
        lines = []
        for insn in self.disassembler.disasm(code, start):
            mark = "=>" if insn.address == (address & ~1) else "  "
            lines.append(
                f"      {mark} {insn.address:08x}: {insn.mnemonic} {insn.op_str}"
            )
        return lines


def print_thread(dump, thread, firmware, stack_words):
    """Print a thread's registers, its stack and where its control block is queued."""
    registers = thread["registers"]
    print(
        f"\n== thread {thread['name']} tcb {thread['tcb']:#x} "
        f"running {thread['running']}"
    )
    print("   " + " ".join(f"{name}={value:#x}" for name, value in registers.items()))
    sp = registers["sp"]
    words = dump.read(sp, stack_words * 4)
    if words is None:
        print("   stack not in dump")
        return
    print(f"   stack from sp={sp:#x}:")
    for i in range(0, len(words), 4):
        value = struct.unpack_from("<I", words, i)[0]
        # A return address is an odd (Thumb) address inside the firmware.
        returns = firmware is not None and firmware.holds(value) and value & 1
        if returns or i < STACK_HEAD_WORDS * 4:
            tag = "  <- return address?" if returns else ""
            print(f"   {sp + i:#x}: {value:#010x}{tag}")
        if returns:
            for line in firmware.around(value - 1):
                print(line)
    # FreeRTOS control block (Cortex-M3 port, no MPU): top of stack at 0, the
    # state list item at 4 and the event list item at 24, each with the list
    # that holds it at +16.
    tcb = dump.read(thread["tcb"], 64)
    if tcb:
        (top,) = struct.unpack_from("<I", tcb, 0)
        (state_list,) = struct.unpack_from("<I", tcb, 4 + 16)
        (event_list,) = struct.unpack_from("<I", tcb, 24 + 16)
        print(
            f"   tcb: top-of-stack {top:#x} state-list {state_list:#x} "
            f"event-list {event_list:#x} "
            "(event-list != 0 -> blocked on that queue's wait list)"
        )


def main():
    def number(text):
        return int(text, 0)

    parser = argparse.ArgumentParser(
        prog="python -m plt.coredump",
        description="Parse a Pebble core dump: memory regions, and every "
        "thread's registers and stack.",
    )
    parser.add_argument("dump", help="raw core dump, or a flash image with --at")
    parser.add_argument(
        "--at",
        type=number,
        default=0,
        metavar="OFFSET",
        help="offset of the core dump in the file",
    )
    parser.add_argument(
        "--fw", metavar="FIRMWARE.bin", help="raw firmware image that crashed"
    )
    parser.add_argument(
        "--fw-base",
        type=number,
        default=0x8004000,
        metavar="ADDRESS",
        help="load address of --fw (default 0x8004000)",
    )
    parser.add_argument("--thread", metavar="NAME", help="print only this thread")
    parser.add_argument(
        "--stack-words",
        type=int,
        default=96,
        metavar="N",
        help="stack words to read per thread (default 96)",
    )
    args = parser.parse_args()

    with open(args.dump, "rb") as f:
        data = f.read()
    try:
        dump = CoreDump(data, args.at)
    except ValueError as error:
        sys.exit(str(error))
    print(
        f"core dump: version {dump.version} core {dump.core} time {dump.timestamp} "
        f"serial {dump.serial!r} build {dump.build}"
    )
    print("memory:", ", ".join(f"{s:#x}+{len(b):#x}" for s, b in dump.memory))
    if dump.extra_registers:
        print("extra regs:", dump.extra_registers.hex())
    firmware = None
    if args.fw:
        with open(args.fw, "rb") as f:
            firmware = Firmware(f.read(), args.fw_base)
    for thread in dump.threads:
        if args.thread and thread["name"] != args.thread:
            continue
        print_thread(dump, thread, firmware, args.stack_words)


if __name__ == "__main__":
    main()
