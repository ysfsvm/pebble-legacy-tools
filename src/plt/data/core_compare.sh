#!/bin/bash
# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
#
# Runs in the build container: build Core's boards from a pristine tree and from the port's,
# then compare every object. Prints "== <board>: <n> differing objects" per board; the list
# is in <out dir>/diff-<board>.txt.
# usage: core_compare.sh <base worktree> <port worktree> <out dir> <board>...
set -u
BASE=$1; PORT=$2; OUT=$3; shift 3
DATA=$(cd "$(dirname "$0")" && pwd)
git config --system --add safe.directory '*'
(cd "$PORT" && pip install -q -r requirements.txt >/dev/null 2>&1)
mkdir -p "$OUT"
for board in "$@"; do
  tag=${board//@/_}
  d=build-cmp-$tag
  for side in base port; do
    src=$BASE; [ $side = port ] && src=$PORT
    (cd "$src" \
      && { [ -f $d/CMakeCache.txt ] || pbl configure -b $d --board $board >"$OUT/configure-$side-$tag.log" 2>&1; } \
      && ninja -C $d -k 0 >"$OUT/build-$side-$tag.log" 2>&1) \
      && echo "$board $side build ok" || echo "$board $side build FAIL"
  done
  r=$OUT/diff-$tag.txt
  python3 "$DATA/compare_objects.py" "$BASE/$d" "$PORT/$d" "$BASE" "$PORT" > "$r"
  echo "== $board: $(grep -c . "$r") differing objects"
done
