#!/bin/bash
# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
#
# Runs in the build container: the integration suite on a snowy emulator build, with the
# stand-in CC2564 as its Bluetooth controller. PEBBLE_QEMU_BIN names the patched QEMU
# (default qemu-system-arm).
# usage: itest.sh <worktree> <build dir> <results dir> [pytest args...]
wt=$1; cd "$wt" || exit 1
bdir=$2; res=$3; shift 3
mkdir -p "$res"
port=5556
package_parent=$(cd "$(dirname "$0")/../.." && pwd)
PYTHONPATH=$package_parent python3 -m plt.fake_cc2564 $port "$res/fake_cc2564.log" &
fake=$!
sleep 1
export PEBBLE_QEMU_BIN=${PEBBLE_QEMU_BIN:-qemu-system-arm}
cd tests/integration || exit 1
timeout 3000 python3 -m pytest --build-dir="$wt/$bdir" --results-dir="$res" \
  --bt-hci="tcp:127.0.0.1:$port" -p no:cacheprovider -rA "$@" 2>&1 | tail -80
kill $fake 2>/dev/null
