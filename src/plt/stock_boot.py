# SPDX-FileCopyrightText: 2026 Brian Silvestri
# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Emulator images that boot through a stock bootloader, and a launcher for them.

usage: python -m plt.stock_boot --fw FILE --spi SPI.bin --out DIR [options]

Boots the stock snowy bootloader with a firmware (a build's pebbleos.hex, or a
raw image such as the stock recovery firmware, loaded at 0x08004000) on a
given NOR image. The console goes to DIR/console.log. The stock bootloaders
are read from the git history of the firmware repository, so a shallow clone
does not have them.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

FLASH_BASE = 0x08000000
FIRMWARE_BASE = 0x08004000
RTC_BACKUP_BASE = 0x40002850
DEFAULT_GDB_PORT = 3333

BOARDS = {
    "silk": {
        "protected": (
            ("PRF", 0x200000, 0x280000),
            ("stock shared PRF storage", 0x7FE000, 0x7FF000),
            ("MFG info", 0x7FF000, 0x800000),
        ),
        "bootloader": "85a10e0b5^:bin/boot/boot_silk@1478015115.hex",
        "flash_size": 0x800000,
        "resources_begin": 0x100000,
        "flash_args": lambda path: ["-drive", f"if=mtd,format=raw,file={path}"],
    },
    "snowy": {
        "protected": (
            ("PRF", 0x200000, 0x300000),
            ("stock shared PRF storage", 0x0C0000, 0x0E0000),
            ("MFG info", 0x0E0000, 0x100000),
        ),
        "bootloader": "c6288702d^:bin/boot/boot_snowy_dvt@1478015115.hex",
        "flash_size": 0x1000000,
        "resources_begin": 0x300000,
        "flash_args": lambda path: [
            "-drive",
            f"if=none,id=spi-flash,file={path},format=raw",
        ],
    },
}
BOARDS["spalding"] = dict(
    BOARDS["snowy"], bootloader="c6288702d^:bin/boot/boot_spalding@1478015115.hex"
)


def micro_image(board, firmware, out_dir, repo="."):
    """Write the internal flash image: the stock bootloader, then the firmware.

    `firmware` is an IntelHex; returns the path of the raw image.
    """
    from intelhex import IntelHex

    boot_hex = os.path.join(out_dir, "boot.hex")
    with open(boot_hex, "wb") as f:
        f.write(
            subprocess.check_output(["git", "-C", repo, "show", board["bootloader"]])
        )
    boot = IntelHex(boot_hex)
    if not boot.maxaddr() < firmware.minaddr():
        sys.exit("firmware overlaps the bootloader")

    micro = IntelHex()
    micro.merge(boot)
    micro.merge(firmware, overlap="error")
    micro.padding = 0xFF
    end = ((micro.maxaddr() + 511) // 512) * 512
    micro_bin = os.path.join(out_dir, "micro.bin")
    micro.tobinfile(micro_bin, start=FLASH_BASE, end=end - 1)
    return micro_bin


def build_stock_images(board, build_dir, out_dir, repo="."):
    """The images of a build: (internal flash image path, external flash content)."""
    from intelhex import IntelHex

    firmware = IntelHex(os.path.join(build_dir, "pebbleos.hex"))
    firmware.start_addr = None
    micro_bin = micro_image(board, firmware, out_dir, repo)

    spi = bytearray(b"\xff" * board["flash_size"])
    resources = Path(build_dir, "system_resources.pbpack").read_bytes()
    begin = board["resources_begin"]
    spi[begin : begin + len(resources)] = resources
    return micro_bin, spi


def _load_firmware(path):
    """A firmware file as an IntelHex: a .hex as it is, anything else as a raw image."""
    from intelhex import IntelHex

    firmware = IntelHex()
    if path.endswith(".hex"):
        firmware.loadhex(path)
        firmware.start_addr = None
    else:
        firmware.frombytes(Path(path).read_bytes(), offset=FIRMWARE_BASE)
    return firmware


def _preset_backup_registers(port, presets):
    """Write RTC backup registers of the halted machine through gdb."""
    gdb = (
        shutil.which("gdb-multiarch")
        or shutil.which("arm-none-eabi-gdb")
        or shutil.which("gdb")
    )
    if not gdb:
        sys.exit("--bkp needs an ARM gdb (gdb-multiarch or arm-none-eabi-gdb)")
    script = [f"target remote :{port}"]
    for preset in presets:
        name, value = preset.split("=")
        address = RTC_BACKUP_BASE + 4 * int(name.upper().replace("DR", ""))
        script.append(f"set *(unsigned int*)0x{address:x} = {value}")
        script.append(f"x/wx 0x{address:x}")
    script += ["detach", "quit"]
    args = [arg for line in script for arg in ("-ex", line)]
    subprocess.run([gdb, "-q", "-batch", *args], check=False)


def _screendump(monitor, path):
    """Ask the QEMU monitor for a screenshot; a monitor that is gone is ignored."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(monitor)
            s.sendall(f"screendump {path}\n".encode())
            time.sleep(0.5)
    except OSError:
        pass


def main():
    from plt import config

    cfg = config.load()
    parser = argparse.ArgumentParser(
        prog="python -m plt.stock_boot",
        description="Boot the stock snowy bootloader with a firmware on a NOR image.",
    )
    parser.add_argument(
        "--fw",
        required=True,
        help=f"firmware: a .hex, or a raw image loaded at {FIRMWARE_BASE:#x}",
    )
    parser.add_argument(
        "--spi", required=True, help="NOR image; used in place, the run modifies it"
    )
    parser.add_argument(
        "--out", required=True, help="directory for the console log and the images"
    )
    parser.add_argument(
        "--seconds", type=int, default=180, help="how long to run (default 180)"
    )
    parser.add_argument(
        "--gdb", type=int, default=0, metavar="PORT", help="open a gdb stub on PORT"
    )
    parser.add_argument(
        "--bkp",
        action="append",
        default=[],
        metavar="DRn=VALUE",
        help="preset an RTC backup register before the firmware starts, e.g. "
        "DR11=0xE5300000 (repeatable; needs an ARM gdb)",
    )
    parser.add_argument(
        "--screenshot-every",
        type=int,
        default=0,
        metavar="N",
        help="write a screenshot (PPM) every N seconds",
    )
    parser.add_argument(
        "--bt-port",
        type=int,
        default=0,
        metavar="PORT",
        help="connect the Bluetooth UART to a controller on 127.0.0.1:PORT "
        "(python -m plt.fake_cc2564)",
    )
    parser.add_argument(
        "--repo",
        default=str(cfg.worktree),
        help="firmware repository that has the stock bootloader in its history "
        "(default: PLT_WORKTREE)",
    )
    parser.add_argument(
        "--qemu", default=cfg.qemu, help="the patched QEMU (default: PLT_QEMU)"
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    board = BOARDS["snowy"]
    micro_bin = micro_image(
        board, _load_firmware(args.fw), args.out, os.path.expanduser(args.repo)
    )

    console = os.path.join(args.out, "console.log")
    monitor = os.path.join(args.out, f"monitor-{os.getpid()}.sock")
    cmd = [os.path.expanduser(args.qemu), "-rtc", "base=localtime"]
    cmd += ["-monitor", f"unix:{monitor},server=on,wait=off"]
    cmd += ["-serial", "null", "-serial", "null", "-serial", f"file:{console}"]
    cmd += ["-machine", "pebble-snowy-bb", "-cpu", "cortex-m4", "-kernel", micro_bin]
    cmd += [*board["flash_args"](args.spi), "-display", "none"]
    if args.bt_port:
        cmd += ["-serial", f"tcp:127.0.0.1:{args.bt_port}"]
    gdb_port = args.gdb or DEFAULT_GDB_PORT
    if args.gdb or args.bkp:
        cmd += ["-gdb", f"tcp::{gdb_port}"]
        if args.bkp:
            # Start halted, so that the registers are set before the first instruction.
            cmd += ["-S"]
    print(" ".join(cmd), flush=True)
    with open(os.path.join(args.out, "qemu.out"), "wb") as qemu_out:
        qemu = subprocess.Popen(cmd, stdout=qemu_out, stderr=subprocess.STDOUT)
        try:
            if args.bkp:
                time.sleep(1)
                _preset_backup_registers(gdb_port, args.bkp)
            start = time.time()
            while time.time() - start < args.seconds:
                time.sleep(1)
                elapsed = int(time.time() - start)
                if args.screenshot_every and elapsed % args.screenshot_every == 0:
                    _screendump(monitor, f"{args.out}/screen-{elapsed:04d}.ppm")
                if qemu.poll() is not None:
                    print("qemu exited", qemu.returncode)
                    break
        finally:
            qemu.terminate()
            qemu.wait()
            try:
                os.unlink(monitor)
            except OSError:
                pass
    print("console bytes:", os.path.getsize(console))


if __name__ == "__main__":
    main()
