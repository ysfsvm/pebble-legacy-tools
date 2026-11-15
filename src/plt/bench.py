# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The measurement protocol: run it on the watch, parse its log, report the results.

A measurement image (CONFIG_LEGACY_PERF_PROBE) plays a scripted key sequence
three times with Bluetooth stopped and writes PERF lines to its flash log:
the screen after every key, frame and key-to-frame times, processor and clock
counters, and a sampling profile. A run counts only if it followed the script.
"""

import json
import os
import re
import time

from plt import container
from plt.images import Registry
from plt.util import PltError, run_limited, stamp
from plt.watch import adb_ready, dump_log, require_phone, watch_version

PROBE_OPTION = "CONFIG_LEGACY_PERF_PROBE=y"
KEYS_PER_RUN = 41
WATCH_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\s+\d+\s+\S+\s+(.*?)(?:\s+<- (.*))?$")
EMU_LINE = re.compile(r"- (\d\d):(\d\d):(\d\d)\.\d+\] <\w+> \S+ (.*)$")
CONVERSION = re.compile(r"%(?:l?[udx]|s|hhu|zu)")
NUMBER = re.compile(r"(?<![\w.])\d+(?![\w.])")
# Seconds a report may trail the end of its run and still belong to it.
REPORT_SLACK = 6
PROFILE_BUCKET = 1024
FRAME_PARTS = ("app", "comp", "flush", "wait", "busy")
REPORT_COLUMNS = (
    ("fps", "frames/s"),
    ("frame_ms_avg", "frame ms"),
    ("gap_ms_max", "gap ms"),
    ("key_to_frame_ms_avg", "key->frame ms"),
    ("key_to_frame_ms_max", "worst"),
    ("duration_s", "sequence s"),
    ("stop_pct", "STOP %"),
    ("clock_irq_per_s", "clock irq/s"),
    ("clock_reprog_per_s", "re-arm/s"),
    ("profile_isr_pct", "in irq %"),
)
# The watch plays the sequence for about four minutes before it reconnects.
RUN_SECONDS = 200
RECONNECT_POLLS = 30
RECONNECT_POLL_SECONDS = 20


def _seconds(hours, minutes, seconds):
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _clock(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def _expand(fmt, args):
    """Put a decoded log line's format and its raw arguments together."""
    tokens = re.findall(r"`[^`]*`|\S+", args or "")

    def substitute(match):
        if not tokens:
            return match.group(0)
        token = tokens.pop(0)
        if token.startswith("`"):
            return token.strip("`")
        if match.group(0).endswith("x"):
            return token
        try:
            return str(int(token, 16))
        except ValueError:
            return token

    return CONVERSION.sub(substitute, fmt)


def _lines(path):
    """Yield (seconds of day, text) of each line of a decoded watch log or emulator console."""
    with open(path, errors="replace") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            match = EMU_LINE.search(raw)
            if match:
                yield _seconds(*match.group(1, 2, 3)), match.group(4)
                continue
            match = WATCH_LINE.match(raw)
            if match:
                yield (
                    _seconds(*match.group(1, 2, 3)),
                    _expand(match.group(4), match.group(5)),
                )


def _numbers(text):
    return [int(number) for number in NUMBER.findall(text)]


def _report(run, t, index):
    """The periodic report of a run with that index; reports come in several lines."""
    return run["reports"].setdefault(index, {"t": t})


def _new_run(number, start):
    return {
        "n": number,
        "start": start,
        "end": None,
        "screens": [],
        "screen_ids": [],
        "keys": [],
        "marks": [],
        "reports": {},
        "profile": None,
        "booted": False,
        "jumps": [],
    }


def parse(path):
    """The runs of the last measurement in a log, and whether it finished."""
    runs, run, finished, previous = [], None, False, None
    for t, text in _lines(path):
        open_run = run is not None and run["end"] is None
        if open_run and previous is not None and not 0 <= t - previous <= 60:
            run["jumps"].append(f"{_clock(previous)} -> {_clock(t)}")
        previous = t

        if "PERF bench start" in text:
            runs, run, finished = [], None, False
        elif "PERF bench done" in text:
            finished = True
        elif re.search(r"PERF run \d+ start", text):
            run = _new_run(_numbers(text)[0], t)
            runs.append(run)
        elif "Booting PebbleOS" in text:
            if open_run:
                run["booted"] = True
        elif run is None:
            continue
        elif re.search(r"PERF run \d+ done", text):
            run["end"] = t
        elif "PERF screen" in text and ": modal " in text:
            name = text.split(": modal ", 1)[1].strip()
            if name != "Timeline Peek" and run["screens"]:
                run["screens"][-1] += " + modal " + name
        elif "PERF screen" in text:
            run["screens"].append(text.split(": ", 1)[1].strip())
            key = text.split("PERF screen", 1)[1].split(":")[0].strip()
            run["screen_ids"].append(key)
        elif "PERF key" in text:
            match = re.search(
                r"PERF key (\d+/\d+): first (\d+) last (\d+) ms, (\d+) frames", text
            )
            if match:
                first, last, frames = (int(match.group(i)) for i in (2, 3, 4))
                run["keys"].append((match.group(1), first, last, frames))
        elif "frame parts" in text:
            a = _numbers(text)
            _report(run, t, a[0]).update(parts=a[1:6])
        elif "PERF mark" in text:
            run["marks"].append(text.split("PERF mark:", 1)[1].strip())
        elif re.search(r"PERF \d+ frames", text):
            a = _numbers(text)
            _report(run, t, a[0]).update(
                frames=a[1], span=a[2], favg=a[3], fmax=a[4], gap=a[5]
            )
        elif "key to frame" in text:
            a = _numbers(text)
            _report(run, t, a[0]).update(keys=a[1], kavg=a[2], kmax=a[3])
        elif re.search(r"PERF \d+ cpu run", text):
            a = _numbers(text)
            _report(run, t, a[0]).update(
                run=a[1], wfi=a[2], stop=a[3], ferase=a[4], fwrite=a[5]
            )
        elif re.search(r"PERF \d+ clock irq", text):
            a = _numbers(text)
            _report(run, t, a[0]).update(irq=a[1], reprog=a[2], stale=a[3], repend=a[4])
        elif "profile samples" in text:
            a = _numbers(text)
            run["profile"] = {"total": a[0], "isr": a[1], "other": a[2], "buckets": []}
        elif "PERF pc" in text and run["profile"] is not None:
            fields = text.split("PERF pc", 1)[1].split()
            run["profile"]["buckets"] += [
                (int(fields[i], 16), int(fields[i + 1]))
                for i in range(0, len(fields) - 1, 2)
                if int(fields[i + 1])
            ]
    return runs, finished


def check(run, reference=None):
    """The reasons a run does not count; none if it does."""
    reasons = []
    if run["end"] is None:
        reasons.append("did not finish")
    if run["booted"]:
        reasons.append("the watch restarted")
    if len(run["screens"]) != KEYS_PER_RUN:
        reasons.append(f"{len(run['screens'])} keys instead of {KEYS_PER_RUN}")
    if run.get("jumps"):
        reasons.append("the watch clock jumped: " + ", ".join(run["jumps"]))
    if run["marks"]:
        reasons.append("disturbed: " + ", ".join(run["marks"]))
    if reference and run["screens"] != reference:
        pairs = zip(run["screens"], reference)
        key = next((i for i, (got, want) in enumerate(pairs) if got != want), None)
        if key is None:
            reasons.append("screens differ from the reference")
        else:
            reasons.append(
                f"screens differ from the reference at key {key}: "
                f"got '{run['screens'][key]}', expected '{reference[key]}'"
            )
    return reasons


def metrics(run):
    """The numbers of one run, from the reports that fall into it."""
    end = run["end"] or 0
    reports = [
        r
        for r in run["reports"].values()
        if all(k in r for k in ("frames", "keys", "run", "irq"))
        and run["start"] < r["t"] <= end + REPORT_SLACK
    ]
    duration = max((run["end"] or run["start"]) - run["start"], 1)
    frames = sum(r["frames"] for r in reports)
    keys = sum(r["keys"] for r in reports)
    span = max(sum(r["span"] for r in reports), 1) / 1000
    result = {
        "duration_s": duration,
        "frames": frames,
        "fps": round(frames / duration, 1),
        "frame_ms_avg": round(
            sum(r["favg"] * r["frames"] for r in reports) / max(frames, 1), 1
        ),
        "frame_ms_max": max((r["fmax"] for r in reports), default=0),
        "gap_ms_max": max((r["gap"] for r in reports), default=0),
        "key_to_frame_ms_avg": round(
            sum(r["kavg"] * r["keys"] for r in reports) / max(keys, 1), 1
        ),
        "key_to_frame_ms_max": max((r["kmax"] for r in reports), default=0),
        "stop_pct": round(
            sum(r["stop"] * r["span"] for r in reports) / (span * 1000), 1
        ),
        "clock_irq_per_s": round(sum(r["irq"] for r in reports) / span),
        "clock_reprog_per_s": round(sum(r["reprog"] for r in reports) / span),
        "clock_repend": sum(r["repend"] for r in reports),
        "flash_writes": sum(r["fwrite"] for r in reports),
    }
    profile = run["profile"]
    if profile and profile["total"]:
        result["profile_isr_pct"] = round(100 * profile["isr"] / profile["total"], 1)
        result["profile_outside_pct"] = round(
            100 * profile["other"] / profile["total"], 1
        )
        result["profile_samples"] = profile["total"]
    return result


def symbols(cfg, elf):
    """(address, size, name) of every function in an ELF."""
    found = []
    for line in container.toolchain_nm(cfg, elf).splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[2] in "tTwW":
            found.append((int(fields[0], 16) & ~1, int(fields[1], 16), fields[3]))
    return found


def profile_table(runs, functions, top=15):
    """The busiest profile buckets of some runs.

    Returns (share of the samples in percent, bucket address, names of the
    functions with most code in the bucket), busiest first.
    """
    total = sum(run["profile"]["total"] for run in runs if run["profile"])
    if not total:
        return []
    buckets = {}
    for run in runs:
        for address, count in (run["profile"] or {"buckets": []})["buckets"]:
            buckets[address] = buckets.get(address, 0) + count
    rows = []
    for address, count in sorted(buckets.items(), key=lambda item: -item[1])[:top]:
        end = address + PROFILE_BUCKET
        inside = sorted(
            (
                (min(start + size, end) - max(start, address), name)
                for start, size, name in functions
                if min(start + size, end) > max(start, address)
            ),
            reverse=True,
        )
        names = ", ".join(name for _, name in inside[:4]) or "?"
        rows.append((round(100 * count / total, 1), f"{address:08x}", names))
    return rows


def frame_parts(runs):
    """Mean microseconds per frame of each part of a frame, over some runs."""
    totals, frames = [0] * len(FRAME_PARTS), 0
    for run in runs:
        for report in run["reports"].values():
            if "parts" in report and report.get("frames"):
                frames += report["frames"]
                totals = [
                    total + part * report["frames"]
                    for total, part in zip(totals, report["parts"])
                ]
    if not frames:
        return {}
    return {name: round(total / frames) for name, total in zip(FRAME_PARTS, totals)}


def key_table(runs):
    """Per scripted key: the screen before and after it, and what each run measured.

    The measurements are (ms to the first frame, ms to the last frame, frames).
    """
    if not runs:
        return []
    first = runs[0]
    rows = []
    for i, (key, *_) in enumerate(first["keys"]):
        at = first["screen_ids"].index(key) if key in first["screen_ids"] else None
        before = first["screens"][at] if at is not None else "?"
        after = "(end)"
        if at is not None and at + 1 < len(first["screens"]):
            after = first["screens"][at + 1]
        values = [
            run["keys"][i][1:]
            for run in runs
            if i < len(run["keys"]) and run["keys"][i][0] == key
        ]
        rows.append((key, before, after, values))
    return rows


def summary(valid):
    """Mean, minimum and maximum of each number over the valid runs."""
    if not valid:
        return {}
    per_run = [metrics(run) for run in valid]
    return {
        key: {
            "mean": round(sum(m[key] for m in per_run) / len(per_run), 1),
            "min": min(m[key] for m in per_run),
            "max": max(m[key] for m in per_run),
        }
        for key in per_run[0]
        if all(key in m for m in per_run)
    }


def evaluate(name, version, runs, reference):
    """The result of a measurement, as it is stored."""
    valid = [run for run in runs if not check(run, reference)]
    return {
        "image": name,
        "version": version,
        "runs": [
            {
                "n": run["n"],
                "invalid": check(run, reference),
                "metrics": metrics(run),
                "screens": run["screens"],
                "profile": run["profile"],
            }
            for run in runs
        ],
        "summary": summary(valid),
        "valid_runs": len(valid),
        "frame_parts_us": frame_parts(valid),
        "keys": [
            {"key": key, "from": before, "to": after, "first_last_frames": values}
            for key, before, after, values in key_table(valid)
        ],
    }


def _save(cfg, name, data):
    cfg.results.mkdir(parents=True, exist_ok=True)
    path = cfg.results / f"{stamp()}-{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    return path


def _latest(cfg, name):
    """The newest stored result of an image, and its file name."""
    files = []
    if cfg.results.is_dir():
        files = sorted(
            p for p in cfg.results.iterdir() if p.name.endswith(f"-{name}.json")
        )
    if not files:
        raise PltError(f"no measurement of '{name}' (plt bench run {name})")
    with open(files[-1]) as f:
        return json.load(f), files[-1].name


def _reference(cfg):
    if not cfg.reference_screens.exists():
        return None
    with open(cfg.reference_screens) as f:
        return json.load(f)


def print_result(cfg, data, elf):
    """Print one line per run, the mean of the valid runs and the profile."""
    header = "  ".join(f"{title:>16s}" for _, title in REPORT_COLUMNS)
    print(f"run  {header}  valid")
    for run in data["runs"]:
        values = "  ".join(
            f"{run['metrics'].get(key, '-')!s:>16s}" for key, _ in REPORT_COLUMNS
        )
        valid = "yes" if not run["invalid"] else "NO: " + "; ".join(run["invalid"])
        print(f"{run['n']:3d}  {values}  {valid}")
    means = data["summary"]
    if means:
        values = "  ".join(
            f"{means[key]['mean'] if key in means else '-'!s:>16s}"
            for key, _ in REPORT_COLUMNS
        )
        print(f"avg  {values}  ({data['valid_runs']} valid runs)")
    profiled = [run for run in data["runs"] if not run["invalid"] and run["profile"]]
    functions = symbols(cfg, elf) if profiled and elf and os.path.exists(elf) else []
    for share, address, names in profile_table(profiled, functions):
        print(f"  {share:5.1f} %  {address}  {names[:110]}")


def report(cfg, names):
    """Print the latest measurement of each named image."""
    registry = Registry(cfg.images)
    for name in names:
        data, filename = _latest(cfg, name)
        print(f"== {name} ({data['version']}, {filename})")
        print_result(cfg, data, registry.get(name)["files"].get("elf"))


def set_reference(cfg, name):
    """Take the reference screens from the first valid run of an image's measurement."""
    data, _ = _latest(cfg, name)
    valid = [run for run in data["runs"] if not run["invalid"]]
    if not valid:
        raise PltError("no valid run to take the reference from")
    cfg.bench.mkdir(parents=True, exist_ok=True)
    with open(cfg.reference_screens, "w") as f:
        json.dump(valid[0]["screens"], f, indent=1)
    print(
        f"reference: the {len(valid[0]['screens'])} screens of {name}, "
        f"run {valid[0]['n']}"
    )


def _store_and_print(cfg, name, entry, version, runs):
    data = evaluate(name, version, runs, _reference(cfg))
    path = _save(cfg, name, data)
    print(f"result {path}")
    print_result(cfg, data, entry["files"].get("elf"))


def measure(cfg, name, log=None):
    """Measure the image the watch runs, or evaluate a log dumped earlier."""
    registry = Registry(cfg.images)
    entry = registry.get(name)
    if PROBE_OPTION not in entry.get("options", []):
        raise PltError(f"not a measurement image (built without {PROBE_OPTION})")
    if log:
        dumped = dump_log(cfg, registry, 0, "", name, have=log.removesuffix(".bin"))
        txt = f"{dumped[0]}.txt"
        if not dumped[2] and not os.path.exists(txt):
            raise PltError("no log dictionary for that log in the registry")
        runs, finished = parse(txt)
        if not finished:
            raise PltError("that log holds no finished measurement")
        _store_and_print(cfg, name, entry, entry["version"], runs)
        return
    require_phone()
    version = watch_version(cfg)
    current = registry.role("current")
    if version != entry["version"] or current != name:
        raise PltError(
            f"the watch runs {version} ({current}), not {name} "
            f"({entry['version']}): plt watch flash {name}"
        )
    sent = run_limited([*cfg.module_command("plt.devconn"), "measure"], 40)
    if "SENT" not in sent.stdout:
        raise PltError("could not send the trigger")
    print(
        "measurement started: Bluetooth off on the watch for about 4-5 min; "
        "do not touch the watch"
    )
    time.sleep(RUN_SECONDS)
    for _ in range(RECONNECT_POLLS):
        time.sleep(RECONNECT_POLL_SECONDS)
        adb_ready()
        if not watch_version(cfg, timeout=25):
            continue
        time.sleep(8)
        dumped = dump_log(cfg, registry, 0, f"bench-{name}", name)
        if not dumped or not dumped[2]:
            continue
        runs, finished = parse(f"{dumped[0]}.txt")
        if not finished:
            continue
        print(f"log {dumped[0]}.txt")
        if not cfg.reference_screens.exists():
            print(
                f"(no reference screens yet: plt bench reference {name} "
                "after checking them)"
            )
        _store_and_print(cfg, name, entry, version, runs)
        return
    raise PltError(
        "the watch did not come back with a finished measurement within 10 min"
    )
