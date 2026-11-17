# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The `plt` command line: argument parsing and dispatch."""

import argparse
import os
import sys
from pathlib import Path

from plt import __version__, bench, build, config, emu, gate, images, watch
from plt.util import PltError

DESCRIPTION = """\
Build, test, install and measure a PebbleOS port for the Pebble Time family.

  plt images                      list the registered images
  plt build NAME                  clean release build of the worktree's commit
  plt emu test NAME               emulator tests and update test of an image
  plt emu try                     emulator build of the working tree as it is
  plt watch status                phone, developer connection, firmware on the watch
  plt watch log [GEN]             dump and decode a flash log
  plt watch shot [OUT.png]        screenshot
  plt watch flash NAME            checked install, verified after the restart
  plt watch recover [NAME]        install from the PC over Bluetooth Classic
  plt bench run NAME              measure the image on the watch
  plt bench report NAME...        tables of the latest measurements
  plt bench reference NAME        take the expected screens from a measurement
  plt gate [--objects]            check that Core's own boards are unchanged
"""

EPILOG = """\
Settings are PLT_* variables, read from the environment, then from the file
PLT_CONFIG names (default ~/.config/plt.conf), then from their defaults.
`plt watch flash` and `plt watch recover` write to the watch.
"""


def _worktree_option(parser):
    parser.add_argument(
        "--wt",
        metavar="PATH",
        type=Path,
        help="firmware worktree (default: PLT_WORKTREE, else the current directory)",
    )


def _define_option(parser, what):
    parser.add_argument(
        "-D",
        dest="options",
        action="append",
        default=[],
        metavar="OPTION=VALUE",
        help=f"Kconfig option {what} (repeatable)",
    )


def _add_images(commands):
    parser = commands.add_parser(
        "images",
        help="list the registered images, register one, set a role",
        description="Without a subcommand: list every image, oldest first, with its "
        "version, checksum, options, recorded tests and last installs.",
    )
    sub = parser.add_subparsers(dest="action", metavar="{add,role}")
    add = sub.add_parser(
        "add",
        help="register an image that was built outside plt",
        description="Copy a bundle, its log dictionary and optionally its ELF into "
        "the images directory and register them. Such an image has no recorded "
        "commit, so it cannot be tested with `plt emu test`.",
    )
    add.add_argument("name", help="name of the new image")
    add.add_argument("pbz", help="firmware bundle (.pbz)")
    add.add_argument("dict", help="the build's log dictionary (log.dict.json)")
    add.add_argument("elf", nargs="?", help="the build's ELF, for profile reports")
    role = sub.add_parser(
        "role",
        help="make an image the current or the recovery image",
        description="current: the image the watch runs (set by a verified install). "
        "recovery: the image `plt watch recover` installs; `plt watch flash` "
        "refuses to run while none is set.",
    )
    role.add_argument("name", help="a registered image")
    role.add_argument(
        "role", choices=["current", "recovery"], help="the role the image gets"
    )


def _add_build(commands):
    parser = commands.add_parser(
        "build",
        help="clean release build of the worktree's commit, registered",
        description="Build the worktree's commit for snowy@dvt with the release "
        "options, from an empty build directory in the build container, and "
        "register the bundle, its log dictionary and its ELF under NAME. The "
        "worktree must have no uncommitted changes. About 5 minutes.",
    )
    parser.add_argument("name", help="name of the new image; a name is never reused")
    _define_option(parser, "added to the release options")
    _worktree_option(parser)


def _add_emu(commands):
    parser = commands.add_parser(
        "emu",
        help="emulator builds and tests",
        description="Build for the emulated SoC and run the firmware in the "
        "patched QEMU, inside the build container.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    test = sub.add_parser(
        "test",
        help="emulator tests and update test of an image",
        description="Build the image's commit and options for the emulated SoC in "
        "both clock-event variants, run the integration suite on each, then the "
        "firmware update test, and record the results in the registry. The "
        "worktree must be at the image's commit. Exit status 1 if a test failed. "
        "About 12 minutes.",
    )
    test.add_argument("name", help="an image built by `plt build`")
    test.add_argument(
        "--update-only",
        action="store_true",
        help="repeat only the update test, on the emulator build that exists",
    )
    _worktree_option(test)
    attempt = sub.add_parser(
        "try",
        help="emulator build of the working tree as it is",
        description="Build the working tree incrementally, uncommitted changes "
        "included, and run the integration suite on it. Nothing is registered.",
    )
    attempt.add_argument(
        "--wut",
        action="store_true",
        help="take the clock event from the RTC wakeup timer instead of SysTick",
    )
    attempt.add_argument(
        "--perf",
        choices=["f0", "f2"],
        help="play the measurement key sequence instead of the suite: "
        "f0 one run, f2 the protocol (three runs, Bluetooth stopped)",
    )
    attempt.add_argument(
        "--hw",
        action="store_true",
        help="only compile the hardware (release) build",
    )
    _define_option(attempt, "added to the build")
    _worktree_option(attempt)


def _add_watch(commands):
    parser = commands.add_parser(
        "watch",
        help="the watch behind the phone",
        description="Reach the watch through the Core app's developer connection "
        "on a phone on adb; `recover` goes from this PC over Bluetooth Classic.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "status",
        help="phone, developer connection, firmware on the watch",
        description="Print whether a phone is on adb, which firmware the watch "
        "reports and which registered image that is.",
    )
    log = sub.add_parser(
        "log",
        help="dump and decode a flash log",
        description="Dump the flash log of one boot into the logs directory, decode "
        "it with the log dictionary of the image that wrote it and print its "
        "warnings and errors.",
    )
    log.add_argument(
        "gen",
        nargs="?",
        type=int,
        default=0,
        help="0: the current boot (default), 1: the one before, ...",
    )
    shot = sub.add_parser(
        "shot",
        help="screenshot",
        description="Save the watch's screen as a PNG and print the file's path.",
    )
    shot.add_argument(
        "out", nargs="?", help="PNG file (default: a new file in the logs directory)"
    )
    flash = sub.add_parser(
        "flash",
        help="checked install, verified after the restart",
        description="Install an image through the phone app. Refused unless the "
        "image's emulator tests and update test are recorded as passed and a "
        "recovery image is set. The watch's flash log is saved first; after the "
        "restart the version the watch reports must be the image's.",
    )
    flash.add_argument("name", help="a registered image")
    flash.add_argument(
        "--force",
        metavar="WHY",
        help="install although tests or a recovery image are missing; "
        "the reason is recorded with the install",
    )
    emoji = sub.add_parser(
        "emoji",
        help="install the emoji pack",
        description="Install the emoji pack of a firmware bundle (.pbz), or a pack "
        "file, on the watch. Without --phone it goes through the phone on adb; "
        "with --phone, straight to the Core app's LAN developer connection, "
        "which is how an iPhone gets it.",
    )
    emoji.add_argument("bundle", help="firmware bundle (.pbz) or emoji pack file")
    emoji.add_argument(
        "--phone",
        metavar="ADDRESS",
        help="the phone's IP address on the LAN (or a ws:// URL, or qemu:PORT)",
    )
    recover = sub.add_parser(
        "recover",
        help="install from the PC over Bluetooth Classic",
        description="Install an image on a watch that shows the stock recovery "
        "firmware's screen, from this PC, without the phone. Needs PLT_WATCH_BT.",
    )
    recover.add_argument(
        "name", nargs="?", help="a registered image (default: the recovery image)"
    )


def _add_bench(commands):
    parser = commands.add_parser(
        "bench",
        help="the measurement protocol",
        description="Measure the speed of the user interface with a measurement "
        "image: a scripted key sequence, three runs, Bluetooth stopped.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser(
        "run",
        help="measure the image on the watch",
        description="Start the measurement on the watch, wait until it has "
        "reconnected, pull its log and store and print the result. The image must "
        "be a measurement image and the one the watch runs. About 5 minutes; "
        "the watch must not be touched.",
    )
    run.add_argument("name", help="a measurement image (CONFIG_LEGACY_PERF_PROBE=y)")
    run.add_argument(
        "--log",
        metavar="DUMP.bin",
        help="evaluate this log dump of a measurement instead of measuring",
    )
    report = sub.add_parser(
        "report",
        help="tables of the latest measurements",
        description="Print, for each image, its latest measurement: one line per "
        "run, the mean of the valid runs and the busiest profile buckets.",
    )
    report.add_argument("names", nargs="+", metavar="name", help="a measured image")
    reference = sub.add_parser(
        "reference",
        help="take the expected screens from a measurement",
        description="Store the screens of the first valid run of an image's latest "
        "measurement as the reference later runs are checked against. Check the "
        "screens by eye first.",
    )
    reference.add_argument("name", help="a measured image")


def _add_gate(commands):
    parser = commands.add_parser(
        "gate",
        help="check that Core's own boards are unchanged",
        description="List the source files whose change since the Core release "
        "may reach a Core board. Exit status 1 if a file is listed or, with "
        "--objects, if an object differs.",
    )
    parser.add_argument(
        "--objects",
        action="store_true",
        help="also build Core's boards from the pristine tree and from the "
        "worktree and compare every object; the comparison then decides "
        "(about 10 minutes)",
    )
    _worktree_option(parser)


def build_parser():
    """The argument parser of `plt`."""
    parser = argparse.ArgumentParser(
        prog="plt",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"plt {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    _add_images(commands)
    _add_build(commands)
    _add_emu(commands)
    _add_watch(commands)
    _add_bench(commands)
    _add_gate(commands)
    return parser


def _dispatch(args, cfg):
    """Run the command the arguments name; returns its exit status."""
    worktree = cfg.worktree
    if getattr(args, "wt", None):
        worktree = Path(os.path.abspath(os.path.expanduser(args.wt)))
    if args.command == "images":
        if args.action == "add":
            images.add_image(cfg, args.name, args.pbz, args.dict, args.elf)
        elif args.action == "role":
            images.set_role(cfg, args.name, args.role)
        else:
            images.list_images(cfg)
    elif args.command == "build":
        build.build_image(cfg, args.name, worktree, args.options)
    elif args.command == "emu":
        if args.action == "test":
            return (
                0 if emu.test_image(cfg, args.name, worktree, args.update_only) else 1
            )
        emu.try_worktree(cfg, worktree, args.options, args.wut, args.perf, args.hw)
    elif args.command == "watch":
        if args.action == "status":
            watch.status(cfg)
        elif args.action == "log":
            watch.log(cfg, args.gen)
        elif args.action == "shot":
            watch.shot(cfg, args.out)
        elif args.action == "flash":
            watch.flash(cfg, args.name, args.force)
        elif args.action == "emoji":
            watch.emoji(cfg, args.bundle, args.phone)
        else:
            watch.recover(cfg, args.name)
    elif args.command == "bench":
        if args.action == "run":
            bench.measure(cfg, args.name, args.log)
        elif args.action == "report":
            bench.report(cfg, args.names)
        else:
            bench.set_reference(cfg, args.name)
    else:
        return 0 if gate.gate(cfg, worktree, args.objects) else 1
    return 0


def main(argv=None):
    """Entry point of the `plt` command; returns the exit status."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args, config.load())
    except PltError as error:
        print(f"plt: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
