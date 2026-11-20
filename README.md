<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo-light.svg" alt="PebbleOS legacy" width="320">
  </picture>
</p>

# pebble-legacy-tools

`plt` builds, tests, installs and measures a port of
[Core Devices' PebbleOS](https://github.com/coredevices/PebbleOS) to the Pebble Time
family (`snowy`, `spalding`). The firmware is in
[PebbleOS-legacy](https://github.com/ysfsvm/PebbleOS-legacy); this repository holds no
firmware source.

A Pebble Time is sealed and has no reachable debug port. An image that does not boot
leaves you with the stock recovery firmware and a Bluetooth link. `plt` exists to make
that unlikely: every image is built from a clean commit, registered with its checksum,
tested in an emulator that boots through the stock bootloader, and installed only when
those tests are on record.

| Command | What it does |
|---|---|
| [`plt images`](#plt-images) | The registry of built images. |
| [`plt build`](#plt-build) | Clean release build of a commit, registered as an image. |
| [`plt emu test`](#plt-emu-test), [`plt emu try`](#plt-emu-try) | Emulator builds, Core's integration suite, the firmware update test. |
| [`plt watch`](#plt-watch-status) | Status, flash log, screenshot, guarded install, recovery install. |
| [`plt bench`](#plt-bench-run) | The measurement protocol: run, tables, reference screens. |
| [`plt gate`](#plt-gate) | Check that the port leaves Core's own boards unchanged. |

## Requirements

A Linux PC. Nothing needs the watch opened.

| What | Needed for | Notes |
|---|---|---|
| Python 3.11 or newer | everything | |
| A firmware worktree | `build`, `emu`, `gate` | A checkout of the port with its submodules. |
| podman | `build`, `emu`, `gate`, profile names in `bench report` | Every build and test runs in a container. |
| Core's build container | same | `ghcr.io/coredevices/pebbleos-docker`. `plt` uses the tag the worktree's CI files name if that image is present, else `PLT_CONTAINER`. It never pulls: `podman pull` the image yourself. |
| The patched QEMU | `emu` | See [Building QEMU](#building-qemu). |
| A pristine Core worktree | `gate --objects` | The Core release the port is based on, checked out next to the port's worktree. |
| adb and an Android phone with the Core app | `watch status`, `log`, `shot`, `flash`, `bench run` | Turn on the app's developer connection. `plt` forwards TCP port 9000 itself. |
| libpebble2, pypng, websocket-client | the same, and `watch recover` | The `watch` extra installs them. |
| A Bluetooth adapter and BlueZ (`bluetoothctl`) | `watch recover` | Bluetooth Classic, not LE. |

### Building QEMU

The emulator is Core's QEMU fork with the four patches in [`qemu/`](qemu/): a Bluetooth
UART for a controller stand-in, a minimal RNG, CTS asserted again after a reset, and the
real sector layout of the snowy NOR. The patches apply on commit `8a0a7c6`
(10.1.5-pebble19). Build it inside the build container, so that it runs there:

```
git clone https://github.com/coredevices/qemu && cd qemu
git checkout 8a0a7c6
git am /path/to/pebble-legacy-tools/qemu/*.patch
mkdir build && cd build
../configure --target-list=arm-softmmu --disable-sdl --disable-install-blobs
ninja qemu-system-arm
```

Set `PLT_QEMU` to the absolute path of `build/qemu-system-arm`. `plt` mounts its
directory into the container.

## Install

With [pipx](https://pipx.pypa.io/), from a release, without git:

```
pipx install "pebble-legacy-tools[watch] @ https://github.com/ysfsvm/pebble-legacy-tools/archive/refs/tags/v0.2.2.zip"
plt --help
```

From a clone:

```
git clone https://github.com/ysfsvm/pebble-legacy-tools && cd pebble-legacy-tools
pip install '.[watch]'
plt --help
```

Extras: `watch` (everything that talks to a watch), `emulator` (intelhex, to run
`plt.stock_boot` and `plt.canary` outside the container), `coredump` (capstone, for
disassembly in `plt.coredump`), `dev` (build, pytest, ruff).

## Configuration

Each setting is read from the environment, then from the configuration file, then takes
its default. The file is the one `PLT_CONFIG` names, by default
`$XDG_CONFIG_HOME/plt.conf` or `~/.config/plt.conf`. It holds `KEY=VALUE` lines; `#`
starts a comment and `~` is expanded.

```
# ~/.config/plt.conf
PLT_HOME=~/pebble-legacy
PLT_WORKTREE=~/src/PebbleOS-legacy
PLT_QEMU=~/src/qemu/build/qemu-system-arm
PLT_WATCH_BT=AA:BB:CC:DD:EE:FF
```

| Setting | Default | Meaning |
|---|---|---|
| `PLT_HOME` | `~/pebble-legacy` | Data root. |
| `PLT_IMAGES` | `<home>/images` | Registered images and `registry.json`. |
| `PLT_LOGS` | `<home>/logs` | Build, emulator, gate and watch logs. |
| `PLT_BENCH` | `<home>/bench` | `reference-screens.json` and `results/`. |
| `PLT_WORKTREE` | current directory | Firmware worktree when `--wt` is not given. |
| `PLT_BASE_WORKTREE` | `base` next to the worktree | Pristine Core worktree for `gate --objects`. |
| `PLT_PYTHON` | the Python that runs `plt` | Python with libpebble2, pypng and websocket-client, for everything that talks to the watch. |
| `PLT_QEMU` | `qemu-system-arm` | The patched QEMU. It runs in the container: give an absolute path, or a name the container has on its `PATH`. |
| `PLT_CONTAINER` | `ghcr.io/coredevices/pebbleos-docker:v13` | Build container when the worktree's CI files name none that is present. |
| `PLT_PIP_CACHE` | `~/.cache/pip-pebble` | pip cache kept between container runs. |
| `PLT_MOUNTS` | computed | Directories the container sees, each at its own path, colon separated. Computed: the worktree, its git directory, the installed package, the logs and the directory of an absolute `PLT_QEMU`. |
| `PLT_WATCH_BT` | none | The watch's Bluetooth Classic address, for `watch recover`. |
| `PLT_HOSTNET` | unset | Any value runs the emulator tests on the host network instead of the container's own. |

These settings were called `PTOOL_*` when the tool was the script `ptool`, and the file
was `ptool.conf`. Rename both; the values and the data directories are unchanged.

The registry stores absolute paths of the image files. If you move the images directory,
edit `registry.json`.

## Commands

Every command has `--help`. A failure you can expect (no such image, no phone, no
container) is one line on standard error and exit status 1.

### plt images

```
plt images
plt images add NAME BUNDLE.pbz LOG.DICT.JSON [ELF]
plt images role NAME current|recovery
```

Without arguments it lists every image, oldest first:

```
$ plt images
r447f-9ceecbb                      v4.47.0-30-g9ceecbba47     a1a8cbbc23f5 2026-11-15 18:24 current
                                   options: CONFIG_RELEASE=y CONFIG_LEGACY_EMOJI_PACK=y CONFIG_BT_LE_IDLE_SLAVE_LATENCY=30
                                   tests: fwupdate_emuwut: transfer+install done in 65 s; waiting for the reboot / ok, itest_emu: 14 passed, 2 deselected in 90.49s (0:01:30), itest_emuwut: 14 passed, 2 deselected in 106.88s (0:01:46), when: 2026-11-15 18:37
```

`add` registers a bundle that was built outside `plt`. Such an image has no recorded
commit, so `plt emu test` cannot test it and `plt watch flash` needs `--force`.

`role` marks the image the watch runs (`current`; a verified install sets it) or the one
`plt watch recover` installs (`recovery`).

```
$ plt images role nowut-136 recovery
recovery = nowut-136
```

### plt build

```
plt build NAME [-D OPTION=VALUE ...] [--wt PATH]
```

Builds the worktree's commit for `snowy@dvt` from an empty build directory, with
`CONFIG_RELEASE=y CONFIG_LEGACY_EMOJI_PACK=y CONFIG_BT_LE_IDLE_SLAVE_LATENCY=30` and
your `-D` options, and registers the bundle, its log dictionary and its ELF under NAME.
About 5 minutes.

```
$ plt build r447f-9ceecbb
building r447f-9ceecbb from 9ceecbba4 (CONFIG_RELEASE=y CONFIG_LEGACY_EMOJI_PACK=y CONFIG_BT_LE_IDLE_SLAVE_LATENCY=30), about 5 min
r447f-9ceecbb: v4.47.0-30-g9ceecbba47 sha a1a8cbbc23f53870 FLASH: 90.57% RAM: 88.39%
```

It refuses a worktree with uncommitted changes and a name that exists. If the bundle
labels itself `-dirty` or with another commit, it is not registered. The build log is
`<logs>/build/NAME.log`.

A measurement image is a release image with one more option:

```
plt build pp2-259b271 -D CONFIG_LEGACY_PERF_PROBE=y
```

### plt emu test

```
plt emu test NAME [--update-only] [--wt PATH]
```

The worktree must be at the image's commit. The command builds that commit with the
image's options for the emulated SoC twice (clock event from SysTick, and from the RTC
wakeup timer), runs the `system`, `ui` and `notifications` parts of Core's integration
suite on each, then runs the update test on the second build: the build's own bundle is
sent over the emulated Pebble Protocol link, the stock bootloader installs it, and the
watch must then report the bundle's version. The results go into the registry. About
12 minutes; exit status 1 if anything failed.

```
$ plt emu test r447f-9ceecbb
emulator builds and tests, about 12 min
  emu: 14 passed, 2 deselected in 90.49s (0:01:30)
  emuwut: 14 passed, 2 deselected in 106.88s (0:01:46)
  update: transfer+install done in 65 s; waiting for the reboot / ok
PASS
```

`--update-only` repeats the update test on the emulator build that exists. Logs and
consoles are in `<logs>/emu/NAME/`.

The emulator does not run the RTC alarm code the watch uses for its clock event, and it
has no real Bluetooth controller: a stand-in answers on the emulated UART.

To test an update between two versions, run the update test by hand in the build
container, from the worktree of the version that runs, with absolute paths. The script
is in the installed package (`python -c "import plt.config as c; print(c.DATA_DIR)"`):

```
python3 <data>/emu_fw_update.py <build dir of the running version> <qemu> <other version>.pbz <out dir>
```

It prints `fw before`, `fw after`, and `ok` only if `fw after` is the bundle's version.

### plt emu try

```
plt emu try [--wut] [--perf f0|f2] [--hw] [-D OPTION=VALUE ...] [--wt PATH]
```

Builds the working tree as it is, uncommitted changes included, incrementally in
`build-dev-<variant>`, and runs the integration suite on it. Nothing is registered.

- `--wut` takes the clock event from the RTC wakeup timer.
- `--perf f0` plays the measurement key sequence once instead of the suite; `--perf f2`
  plays the whole protocol. Build with `-D CONFIG_LEGACY_PERF_PROBE=y`.
- `--hw` only compiles the hardware build with the release options and prints its memory
  use and first warnings.

```
plt emu try --wut -D CONFIG_LEGACY_PERF_PROBE=y --perf f0
```

It prints the last three lines of the suite's output, or the `PERF` lines of the
console with `--perf`, and the directory that holds the logs
(`<logs>/emu/dev-<time>-<worktree>`).

### plt watch status

```
$ plt watch status
phone: on adb, port 9000 forwarded
watch: v4.47.0-30-g9ceecbba47 (r447f-9ceecbb)
registry: current=r447f-9ceecbb recovery=nowut-136
```

Read only. If the phone is on the cable and on the network, the network device is used.

### plt watch log

```
plt watch log [GEN]
```

Dumps the flash log of one boot (0: the current boot, 1: the one before; the watch keeps
about ten) into `<logs>/watch/`, decodes it with the log dictionary of the image that
wrote it, and prints its size, its time span and every line at warning level or above.

```
$ plt watch log
/home/you/pebble-legacy/logs/watch/20261115-111604-log-gen0.bin  written by v4.46.0-147-g6f262625de
  628 lines, 21:52:47 .. 11:16:02
  21:52:47   0 debug.c:20 Platform: %u, hw: %s, sn: %s  <- 8 `snowy23` `X00000000000`
  21:52:47   0 debug.c:26 BUILD ID: %s  <- `675faa536af73405ad32751eeb8a85e922266443`
  21:52:47  50 reboot_reason.c:142 Safely rebooted due to SoftwareUpdate
  23:43:36   1 app_message_outbox.c:154 Can't call app_message_outbox_begin() now, wait for sent_callback!  <-
  ...
```

The decoded log is the `.txt` next to the `.bin`. Columns: time, level (0 always,
1 error, 50 warning, 100 info, 200 debug), file and line, the message and, after `<-`,
its arguments.

### plt watch shot

```
plt watch shot [OUT.png]
```

Saves the watch's screen as a PNG and prints the file's path: OUT.png, or
`<logs>/watch/<time>-shot.png`.

### plt watch emoji

```
plt watch emoji BUNDLE [--phone ADDRESS]
```

Installs the emoji pack on the watch. BUNDLE is a firmware bundle (`.pbz`) that carries
the pack, checked against its manifest, or the pack file itself. Without `--phone` it goes
through the phone on adb. With `--phone` it goes straight to the Core app's developer
connection on the LAN: turn on "Use LAN developer connection" in the app, keep the app open,
and give the phone's IP address. That is the way for an iPhone, where the app does not
install the pack itself.

```
plt watch emoji normal_snowy_s3_v4.47.0-legacy.1.pbz --phone 192.168.1.20
```

The pack has to come from the bundle of the firmware the watch runs.

### plt watch flash

```
plt watch flash NAME [--force WHY]
```

Installs an image through the phone app and checks that the watch then runs it. This
writes to the watch. In order, the command:

1. refuses an image whose emulator tests and update test are not recorded as passed,
   and refuses when no recovery image is set (`--force 'reason'` overrides these two and
   records the reason);
2. refuses an image file whose SHA-256 is not the registered one, a phone that is not
   on adb and a watch that does not answer (not overridable);
3. saves the watch's current flash log to `<logs>/watch/` (if the log cannot be read it says so and goes on);
4. sends the bundle and waits for the phone to report the install as complete;
5. asks the watch for its version up to twelve times, 20 s apart, and compares the
   answer with the image's version. A match makes the image `current`; anything else
   is recorded as a failed install and the command exits with status 1.

```
$ plt watch flash r447e-93800bd
watch runs v4.47.0-24-g72a0d241f1; installing r447e-93800bd (v4.47.0-27-g93800bd4af)
log backup: /home/you/pebble-legacy/logs/watch/20261115-174218-pre-r447e-93800bd-gen0.bin
phone: InstallStatus 0 (ok)
watch booted v4.47.0-27-g93800bd4af
  46 lines, 17:45:37 .. 17:46:02
  17:45:37   0 debug.c:20 Platform: %u, hw: %s, sn: %s  <- 8 `snowy23` `X00000000000`
  17:45:37   0 debug.c:26 BUILD ID: %s  <- `0b272c00c1610a56868f09b05c729c4214a38339`
  17:45:37  50 reboot_reason.c:139 Safely rebooted due to SoftwareUpdate
installed r447e-93800bd
```

### plt watch recover

```
plt watch recover [NAME]
```

Installs the recovery image, or NAME, from the PC over Bluetooth Classic, on a watch
that shows the stock recovery firmware's screen. The phone is not involved. Set
`PLT_WATCH_BT` to the watch's address (`bluetoothctl scan bredr` shows it).

Before you run it: turn the phone's Bluetooth off, put the watch on the charger, and
have the recovery screen on the watch. On that screen never hold Down, which starts a
factory reset.

This command has not been run on a watch in its packaged form. The scripts it was made
from installed a firmware from the recovery firmware three times.

The recovery firmware keeps no bonds and drops idle links, so the install runs in up to
40 windows within 15 minutes: pair, connect, write until the link drops, try again.

```
$ plt watch recover
Recovery install of nowut-136 (v4.46.0-136-gc8f72252a) over Bluetooth Classic.
Before this runs: phone Bluetooth OFF, watch on the charger, watch on the PRF screen.
In PRF: never hold Down (factory reset); hold Select if no pairing screen comes.
```

When the transfer has ended, the command prints its last eight lines and the path of
the full log in `<logs>/watch/`. The last line is `DONE` when the watch took the whole
bundle and `GAVE UP` when the windows ran out. An install that ends with `DONE` is
recorded and makes the image `current`; the command does not check which version the
watch then runs, so follow it with `plt watch status` once the phone is connected
again.

### plt bench run

```
plt bench run NAME [--log DUMP.bin]
```

Measures the speed of the user interface. The image must be a measurement image
(`CONFIG_LEGACY_PERF_PROBE=y`) and the one the watch runs. The command sends a trigger;
the watch then waits 5 s, stops Bluetooth, waits 20 s, plays a script of 41 keys three
times, 15 s apart, and starts Bluetooth again. When the watch has reconnected, `plt`
pulls its log, checks every run, stores the result in `<bench>/results/` and prints the
tables of [`plt bench report`](#plt-bench-report). About 5 minutes.

Keep these the same for every measurement, or the numbers do not compare:

- Two images that are compared differ in one thing only.
- The watch is off the charger, at 40 to 90 % battery, on a table, with the same watch
  face, backlight setting and launcher order. The phone is on Do Not Disturb.
- The watch is left alone for 2 minutes after an install, and nobody touches it, pulls
  a log or takes a screenshot while it measures.

A run counts only if it followed the script: the screen after each key equals the
reference, all 41 keys were played, no notification or call arrived, the watch did not
restart, and its clock neither went back nor skipped more than a minute. Other runs are
listed with the reason and left out of the means.

`--log DUMP.bin` evaluates a log dump that holds a finished measurement, without the
watch.

Limits: time is measured in 4 ms steps. The profile and the STOP share exist only on
the watch; in the emulator (`plt emu try --perf f2`) only the sequence and the counters
work. Nothing is measured while a phone is connected.

### plt bench report

```
plt bench report NAME...
```

Prints the latest measurement of each image: one line per run, the mean of the valid
runs, then the 15 busiest 1 KiB buckets of the sampling profile with their share, their
address and the functions in them.

```
$ plt bench report pp2-259b271
== pp2-259b271 (v4.47.0-22-g259b27183a, 20261115-123606-pp2-259b271.json)
run          frames/s          frame ms            gap ms     key->frame ms             worst        sequence s            STOP %       clock irq/s          re-arm/s          in irq %  valid
  1               9.4              24.7              2207             112.6              2328                23              48.3                20               829               4.9  yes
  2               9.0              24.3              2219             117.5              2328                24               2.0                37              1657               5.0  yes
  3               9.5              24.7              2222             117.1              2328                23              32.1                26              1122               5.0  yes
avg               9.3              24.6            2216.0             115.7            2328.0              23.3              27.5              27.7            1202.7               5.0  (3 valid runs)
```

The result file also holds, for the valid runs, the mean time per frame of each part of
a frame in microseconds (`frame_parts_us`: app draw, compositor, display transfer, wait
before the frame, and the part of the transfer spent waiting for the display's BUSY
line) and a table per key (`keys`).

### plt bench reference

```
$ plt bench reference pp2-259b271
reference: the 41 screens of pp2-259b271, run 1
```

Stores the screens of the first valid run of an image's latest measurement as
`<bench>/reference-screens.json`. Check the screens by eye first: later runs are valid
only if they show the same ones. The reference belongs to one watch set-up. The package
ships the author's (`reference-screens.json` in the package's `data` directory: watch
face TimeStyle, Settings first in the launcher) as an example of the format.

### plt gate

```
plt gate [--objects] [--wt PATH]
```

Checks that the port leaves Core's own boards (`asterix`, `obelix@pvt`, `getafix@dvt2`)
as they were. The source check takes every C, header and linker-script file the port
modifies since the Core release the worktree is based on (the nearest `v*` tag),
resolves the preprocessor conditions that depend only on `CONFIG_` symbols with each
board's configuration, and lists the files that still differ:

```
$ plt gate
...
UNGATED pebble/resource/resource_storage_file.c (1 lines; asterix,obelix@pvt,getafix@dvt2)
    +#include <string.h>
...
76 Core files changed, 6 change Core boards (asterix, obelix@pvt, getafix@dvt2)
```

A listed file may still compile to the same code, because conditions on other macros
are kept as written. `--objects` settles it: it builds the three boards from the
pristine Core worktree and from the port (in `build-cmp-<board>` in each) and compares
every object, ignoring debug information, moved log strings and line numbers. About
10 minutes.

```
$ plt gate --objects
...
object comparison for asterix, obelix@pvt, getafix@dvt2, about 10 min
== asterix: 0 differing objects
== obelix@pvt: 0 differing objects
== getafix@dvt2: 0 differing objects
logs: /home/you/pebble-legacy/logs/gate/20261115-151702
```

Exit status: without `--objects`, 1 if a file is listed; with it, 1 if an object
differs or a build fails. The source check needs the board configurations that
`--objects` leaves behind; before the first such run it only counts the changed files.

## Stand-alone modules

These run on their own with `python -m`; each has `--help`.

| Module | What it does |
|---|---|
| `plt.recover` | From the PC, without the phone: `install` a bundle, save the `log` of the last three boots, probe how long a link stays `alive`, fetch a `coredump`. Takes a Bluetooth Classic address, `tcp:HOST:PORT` (an emulator) or `ws://HOST:PORT/` (the developer connection). |
| `plt.coredump` | Print the threads, registers and stacks of a core dump. |
| `plt.flashlog` | `dump` a flash log over the developer connection; `decode` a dump with a log dictionary. |
| `plt.devconn` | `version`, `screenshot`, `install` and `measure` over the developer connection. |
| `plt.emoji_pack` | Install the emoji pack of a bundle, or a pack file, on a watch or on a running emulator. |
| `plt.fake_cc2564` | The stand-in for the TI CC2564B on the emulator's Bluetooth UART, with modes for a controller that stops answering. |
| `plt.stock_boot` | Boot the stock bootloader with any firmware on a NOR image; can preset RTC backup registers through gdb. |
| `plt.canary` | Boot a build and check that the flash regions the stock firmware owns stay untouched. |

## Safety rules

What `plt` enforces:

- `watch flash` installs only an image with passed emulator tests and a passed update
  test on record, and only while a recovery image is set. `--force` needs a reason,
  which is written into the registry.
- It never installs an image file whose checksum differs from the registered one.
- It saves the watch's flash log before the install, when the log can be read.
- An install counts only when the watch reports the image's version after the restart.
  The update test in the emulator applies the same rule: a finished transfer is not a
  pass.
- `build` registers only a bundle built from a clean commit and never overwrites an
  image.
- Registry writes from concurrent `plt` processes are merged under a lock.
- `bench run` measures only a measurement image that is the one on the watch, and a
  run counts only if it followed the script.
- No command builds or writes a recovery firmware, a bootloader or factory data, and
  none sends the watch into the recovery firmware.

What it leaves to you: one change between two consecutive watch images, a charged
watch, and the decision to write to the watch at all.

## Development

```
pip install -e '.[dev]'
pytest
ruff check && ruff format --check
```

The tests need no network, container, phone or watch.

## Licence

Apache License 2.0, see [LICENSE](LICENSE). `plt.canary` and the image builder in
`plt.stock_boot` come from
[bjsilvest/PebbleOS-legacy-watches](https://github.com/bjsilvest/PebbleOS-legacy-watches)
by Brian Silvestri. Not made or endorsed by Core Devices, Google or Pebble Technology.
