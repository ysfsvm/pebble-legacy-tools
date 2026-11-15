# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The measurement log parser, the validity check and the tables.

The fixtures are cut from logs of a Pebble Time: the boot lines and the PERF
lines of one measurement each, with the watch's serial number replaced.
bench-valid.txt holds two runs that followed the script, bench-off-script.txt
one run that opened another app than the script expects.
"""

import json
import shutil

import pytest

from conftest import DATA
from plt import bench, config
from plt.util import PltError

VALID = DATA / "bench-valid.txt"
OFF_SCRIPT = DATA / "bench-off-script.txt"


@pytest.fixture(scope="module")
def reference():
    with open(config.DATA_DIR / "reference-screens.json") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def valid_runs():
    runs, finished = bench.parse(VALID)
    assert finished
    return runs


def edited(tmp_path, source, edit):
    """A copy of a fixture log with its lines passed through `edit`."""
    path = tmp_path / "log.txt"
    path.write_text("\n".join(edit(source.read_text().splitlines())) + "\n")
    return path


def test_parse_finds_the_runs(valid_runs):
    assert [run["n"] for run in valid_runs] == [1, 2]
    first = valid_runs[0]
    assert len(first["screens"]) == bench.KEYS_PER_RUN
    assert len(first["keys"]) == bench.KEYS_PER_RUN
    assert first["screens"][0] == "app TimeStyle window ? + modal ?"
    assert first["screens"][37] == "app Settings window ? + modal ?"
    assert first["keys"][0] == ("0/0", 0, 0, 0)
    assert first["end"] - first["start"] == 24
    assert first["marks"] == []
    assert first["jumps"] == []
    assert not first["booted"]
    assert first["profile"]["total"] == 22461


def test_runs_that_follow_the_script_are_valid(valid_runs, reference):
    assert [bench.check(run, reference) for run in valid_runs] == [[], []]


def test_a_run_that_leaves_the_script_is_invalid(reference):
    runs, finished = bench.parse(OFF_SCRIPT)
    assert finished
    assert [run["n"] for run in runs] == [2]
    assert bench.check(runs[0], reference) == [
        (
            "screens differ from the reference at key 37: "
            "got 'app Music window ? + modal ?', "
            "expected 'app Settings window ? + modal ?'"
        )
    ]
    assert bench.check(runs[0]) == []
    data = bench.evaluate("img", "v1", runs, reference)
    assert data["valid_runs"] == 0
    assert data["summary"] == {}
    assert data["frame_parts_us"] == {}
    assert data["keys"] == []


def test_metrics(valid_runs):
    assert bench.metrics(valid_runs[0]) == {
        "duration_s": 24,
        "frames": 218,
        "fps": 9.1,
        "frame_ms_avg": 25.0,
        "frame_ms_max": 36,
        "gap_ms_max": 2207,
        "key_to_frame_ms_avg": 116.8,
        "key_to_frame_ms_max": 2324,
        "stop_pct": 22.0,
        "clock_irq_per_s": 30,
        "clock_reprog_per_s": 1270,
        "clock_repend": 0,
        "flash_writes": 441,
        "profile_isr_pct": 5.0,
        "profile_outside_pct": 0.2,
        "profile_samples": 22461,
    }
    assert bench.metrics(valid_runs[1])["frames"] == 216


def test_summary(valid_runs):
    summary = bench.summary(valid_runs)
    assert summary["duration_s"] == {"mean": 23.5, "min": 23, "max": 24}
    assert summary["frames"] == {"mean": 217.0, "min": 216, "max": 218}
    assert summary["fps"] == {"mean": 9.2, "min": 9.1, "max": 9.4}
    assert bench.summary([]) == {}


def test_frame_parts_table(valid_runs):
    assert bench.frame_parts(valid_runs) == {
        "app": 34897,
        "comp": 1574,
        "flush": 23978,
        "wait": 1083,
        "busy": 13185,
    }
    assert bench.frame_parts([]) == {}


def test_profile_table_without_symbols(valid_runs):
    table = bench.profile_table(valid_runs, [])
    assert len(table) == 15
    assert table[:3] == [
        (56.1, "080bb400", "?"),
        (4.5, "080ba400", "?"),
        (3.3, "08043400", "?"),
    ]
    shares = [share for share, _, _ in table]
    assert shares == sorted(shares, reverse=True)


def test_profile_table_names_the_functions_in_a_bucket(valid_runs):
    functions = [
        (0x080BB3F0, 0x20, "starts_before"),
        (0x080BB410, 0x300, "biggest"),
        (0x080BB710, 0x40, "small"),
        (0x080BB800, 0x10, "next_bucket"),
        (0x080BA400, 0x400, "fills_second"),
    ]
    table = bench.profile_table(valid_runs, functions, top=2)
    assert table == [
        (56.1, "080bb400", "biggest, small, starts_before"),
        (4.5, "080ba400", "fills_second"),
    ]
    assert bench.profile_table([{"profile": None}], functions) == []


def test_key_table(valid_runs):
    table = bench.key_table(valid_runs)
    assert len(table) == bench.KEYS_PER_RUN
    key, before, after, values = table[0]
    assert (key, before, after) == (
        "0/0",
        "app TimeStyle window ? + modal ?",
        "app TimeStyle window ? + modal ?",
    )
    assert values == [(0, 0, 0), (0, 0, 0)]
    assert table[-1][2] == "(end)"
    assert bench.key_table([]) == []


def test_unfinished_run(tmp_path, reference):
    def cut(lines):
        end = next(i for i, line in enumerate(lines) if "PERF run %u done" in line)
        return lines[:end]

    runs, finished = bench.parse(edited(tmp_path, VALID, cut))
    assert not finished
    assert "did not finish" in bench.check(runs[0], reference)


def test_restart_during_a_run(tmp_path, reference):
    def restart(lines):
        at = next(i for i, line in enumerate(lines) if "PERF key" in line)
        boot = f"{lines[at][:8]}   0 init.c:43 *** Booting PebbleOS %s ***  <- `v1`"
        return [*lines[:at], boot, *lines[at:]]

    runs, _ = bench.parse(edited(tmp_path, VALID, restart))
    assert bench.check(runs[0], reference) == ["the watch restarted"]
    assert bench.check(runs[1], reference) == []


def test_notification_during_a_run(tmp_path, reference):
    def disturb(lines):
        at = next(i for i, line in enumerate(lines) if "PERF key" in line)
        mark = f"{lines[at][:8]} 100 perf_probe.c:1 PERF mark: notification"
        return [*lines[:at], mark, *lines[at:]]

    runs, _ = bench.parse(edited(tmp_path, VALID, disturb))
    assert bench.check(runs[0], reference) == ["disturbed: notification"]


def test_clock_jump_during_a_run(tmp_path, reference):
    def jump(lines):
        at = next(i for i, line in enumerate(lines) if "PERF key" in line)
        return [*lines[:at], "09:00:00 100 x.c:1 PERF mark:", *lines[at:]]

    runs, _ = bench.parse(edited(tmp_path, VALID, jump))
    reasons = bench.check(runs[0], reference)
    assert any(r.startswith("the watch clock jumped: 12:44 -> 09:00") for r in reasons)


def test_missing_keys(tmp_path, reference):
    def drop(lines):
        seen = 0
        kept = []
        for line in lines:
            if "PERF screen %u/%u: app" in line:
                seen += 1
                if seen == 5:
                    continue
            kept.append(line)
        return kept

    runs, _ = bench.parse(edited(tmp_path, VALID, drop))
    reasons = bench.check(runs[0], reference)
    assert "40 keys instead of 41" in reasons


def test_only_the_last_measurement_of_a_log_counts(tmp_path):
    def twice(lines):
        start = next(i for i, line in enumerate(lines) if "PERF bench start" in line)
        return [*lines, *lines[start:]]

    runs, finished = bench.parse(edited(tmp_path, VALID, twice))
    assert finished
    assert [run["n"] for run in runs] == [1, 2]


def test_emulator_console_lines(tmp_path):
    path = tmp_path / "console.log"
    path.write_text(
        "I 1 - 10:00:00.000] <inf> perf_probe.c:674 PERF bench start\n"
        "I 1 - 10:00:01.000] <inf> perf_probe.c:623 PERF run 1 start\n"
        "I 1 - 10:00:02.000] <inf> perf_probe.c:481 PERF screen 0/0: app Clock window ?\n"
        "I 1 - 10:00:09.000] <inf> perf_probe.c:629 PERF run 1 done\n"
        "I 1 - 10:00:10.000] <inf> perf_probe.c:641 PERF bench done\n"
    )
    runs, finished = bench.parse(path)
    assert finished
    assert runs[0]["screens"] == ["app Clock window ?"]
    assert runs[0]["end"] - runs[0]["start"] == 8


def test_evaluate_layout(valid_runs, reference):
    data = bench.evaluate("img", "v4.47.0-23-g6c0ba5c5f2", valid_runs, reference)
    assert list(data) == [
        "image",
        "version",
        "runs",
        "summary",
        "valid_runs",
        "frame_parts_us",
        "keys",
    ]
    assert data["valid_runs"] == 2
    assert list(data["runs"][0]) == ["n", "invalid", "metrics", "screens", "profile"]
    assert data["keys"][0] == {
        "key": "0/0",
        "from": "app TimeStyle window ? + modal ?",
        "to": "app TimeStyle window ? + modal ?",
        "first_last_frames": [(0, 0, 0), (0, 0, 0)],
    }
    json.dumps(data)


@pytest.fixture
def measured(cfg, make_image):
    """A measurement image whose result is stored, as `plt bench run` leaves it."""
    make_image("probe", options=[bench.PROBE_OPTION])
    runs, _ = bench.parse(VALID)
    off, _ = bench.parse(OFF_SCRIPT)
    with open(config.DATA_DIR / "reference-screens.json") as f:
        reference = json.load(f)
    data = bench.evaluate("probe", "v1", [*runs, *off], reference)
    cfg.results.mkdir(parents=True)
    with open(cfg.results / "20260101-100000-probe.json", "w") as f:
        json.dump(data, f)
    return "probe"


def test_report(cfg, measured, capsys):
    bench.report(cfg, [measured])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "== probe (v1, 20260101-100000-probe.json)"
    assert lines[1].split()[:3] == ["run", "frames/s", "frame"]
    assert lines[2].split()[:2] == ["1", "9.1"]
    assert lines[2].endswith("  yes")
    assert "NO: screens differ from the reference at key 37" in lines[4]
    assert lines[5].startswith("avg ")
    assert lines[5].endswith("(2 valid runs)")
    assert lines[6].split() == ["56.1", "%", "080bb400", "?"]
    assert len(lines) == 6 + 15


def test_report_of_an_unmeasured_image(cfg, make_image):
    make_image("never")
    with pytest.raises(PltError, match="no measurement of 'never'"):
        bench.report(cfg, ["never"])


def test_reference_comes_from_the_first_valid_run(cfg, measured, capsys, reference):
    bench.set_reference(cfg, measured)
    assert json.loads(cfg.reference_screens.read_text()) == reference
    assert capsys.readouterr().out == "reference: the 41 screens of probe, run 1\n"


def test_reference_needs_a_valid_run(cfg, make_image):
    make_image("off", options=[bench.PROBE_OPTION])
    runs, _ = bench.parse(OFF_SCRIPT)
    data = bench.evaluate("off", "v1", runs, ["something else"] * 41)
    cfg.results.mkdir(parents=True)
    with open(cfg.results / "20260101-100000-off.json", "w") as f:
        json.dump(data, f)
    with pytest.raises(PltError, match="no valid run"):
        bench.set_reference(cfg, "off")


def test_measure_refuses_an_image_without_the_probe(cfg, make_image):
    make_image("release", options=["CONFIG_RELEASE=y"])
    with pytest.raises(PltError, match="not a measurement image"):
        bench.measure(cfg, "release")


def test_measure_refuses_another_image_on_the_watch(cfg, make_image, monkeypatch):
    make_image("probe", version="v2", options=[bench.PROBE_OPTION])
    monkeypatch.setattr(bench, "require_phone", lambda: None)
    monkeypatch.setattr(bench, "watch_version", lambda cfg, timeout=30: "v1")
    with pytest.raises(PltError, match="the watch runs v1"):
        bench.measure(cfg, "probe")


def test_measure_evaluates_a_log_dumped_earlier(cfg, make_image, monkeypatch, capsys):
    make_image("probe", options=[bench.PROBE_OPTION])
    dump = cfg.logs / "watch" / "20260101-100000-bench-probe-gen0"
    dump.parent.mkdir(parents=True)
    shutil.copy(VALID, f"{dump}.txt")
    monkeypatch.setattr(
        bench, "dump_log", lambda *args, have=None, **kw: (have, None, False)
    )
    bench.measure(cfg, "probe", log=f"{dump}.bin")
    stored = list(cfg.results.glob("*-probe.json"))
    assert len(stored) == 1
    data = json.loads(stored[0].read_text())
    assert data["valid_runs"] == 2
    assert data["version"] == "v4.47.0-1-gabcdef012"
    assert capsys.readouterr().out.startswith(f"result {stored[0]}\nrun ")
