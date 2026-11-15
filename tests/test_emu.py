# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Emulator tests: what counts as passed, and what `plt emu test` runs and records."""

import subprocess

import pytest

from plt import emu
from plt.images import Registry
from plt.util import PltError

PASSED = {
    "itest_emu": "14 passed, 2 deselected in 90.49s (0:01:30)",
    "itest_emuwut": "14 passed, 2 deselected in 106.88s (0:01:46)",
    "fwupdate_emuwut": "transfer+install done in 65 s; waiting for the reboot / ok",
}


def test_update_passes_only_on_the_reported_version():
    version = "v4.47.0-30-g9ceecbba47"
    assert emu.update_verdict(version, version) == "ok"
    assert emu.update_verdict("v4.47.0-29-g0000000000", version).startswith("FAIL")
    assert emu.update_verdict(None, version).startswith("FAIL")
    assert emu.update_verdict(None, None).startswith("FAIL")


def test_tests_passed_needs_both_suites_and_the_update():
    assert emu.tests_passed({"tests": dict(PASSED)})
    assert not emu.tests_passed({})
    assert not emu.tests_passed({"tests": {}})
    for missing in PASSED:
        tests = {k: v for k, v in PASSED.items() if k != missing}
        assert not emu.tests_passed({"tests": tests}), missing


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("itest_emu", "13 passed, 1 failed in 95.00s"),
        ("itest_emuwut", "no output"),
        (
            "fwupdate_emuwut",
            (
                "transfer+install done in 65 s; waiting for the reboot / "
                "FAIL: the watch does not run the bundle's version"
            ),
        ),
        ("fwupdate_emuwut", "transfer+install done in 65 s; waiting for the reboot"),
        ("fwupdate_emuwut", "Traceback (most recent call last):"),
        ("fwupdate_emuwut", "no output"),
    ],
)
def test_a_failed_part_fails_the_image(key, value):
    assert not emu.tests_passed({"tests": {**PASSED, key: value}})


def test_the_update_verdict_is_what_tests_passed_reads():
    record = "transfer+install done in 65 s; waiting for the reboot / "
    tests = {**PASSED, "fwupdate_emuwut": record + emu.update_verdict("v1", "v1")}
    assert emu.tests_passed({"tests": tests})
    tests["fwupdate_emuwut"] = record + emu.update_verdict("v0", "v1")
    assert not emu.tests_passed({"tests": tests})


class FakeContainer:
    """Stands in for the build container: records the scripts, answers as told."""

    def __init__(self, suite="14 passed in 90.00s", update="transfer done\nok"):
        self.scripts = []
        self.suite = suite
        self.update = update

    def run_script(self, cfg, worktree, script, network=False):
        self.scripts.append(script)
        syntax = subprocess.run(
            ["bash", "-n", "-c", script], check=False, capture_output=True, text=True
        )
        assert syntax.returncode == 0, syntax.stderr
        if "itest.sh" in script:
            out = self.suite
        elif "emu_fw_update.py" in script:
            out = self.update
        else:
            out = "rc=0"
        return subprocess.CompletedProcess([], 0, out + "\n", "")


@pytest.fixture
def image(cfg, make_image, monkeypatch):
    make_image(
        "img",
        commit="abcdef012",
        extra_options=["CONFIG_TIMER_STM32_RTC_WUT_GUARD=y", "CONFIG_X=y"],
    )
    monkeypatch.setattr(emu, "worktree_commit", lambda worktree: "abcdef012")
    return "img"


def test_test_image_builds_both_variants_and_records(cfg, image, monkeypatch, capsys):
    fake = FakeContainer()
    monkeypatch.setattr(emu.container, "run_script", fake.run_script)
    assert emu.test_image(cfg, image, cfg.worktree) is True
    builds = [s for s in fake.scripts if "pbl configure" in s]
    assert len(builds) == 2
    assert "-DCONFIG_SOC_STM32F4_EMULATED=y -DCONFIG_X=y" in builds[0]
    assert "CONFIG_TIMER_STM32_RTC_WUT" not in builds[0]
    assert "pbl bundle" not in builds[0]
    assert (
        "-DCONFIG_SOC_STM32F4_EMULATED=y -DCONFIG_TIMER_STM32_RTC_WUT=y "
        "-DCONFIG_TIMER_STM32_RTC_WUT_GUARD=y -DCONFIG_X=y"
    ) in builds[1]
    assert "pbl bundle -b build-img-img-emuwut" in builds[1]
    assert sum("itest.sh" in s for s in fake.scripts) == 2
    assert "build-img-img-emuwut" in fake.scripts[-1]
    tests = Registry(cfg.images).get(image)["tests"]
    assert tests["itest_emu"] == "14 passed in 90.00s"
    assert tests["itest_emuwut"] == "14 passed in 90.00s"
    assert tests["fwupdate_emuwut"] == "transfer done / ok"
    assert "when" in tests
    assert capsys.readouterr().out.splitlines()[-1] == "PASS"


def test_test_image_fails_when_the_update_fails(cfg, image, monkeypatch, capsys):
    fake = FakeContainer(update="transfer done\n" + emu.UPDATE_FAIL)
    monkeypatch.setattr(emu.container, "run_script", fake.run_script)
    assert emu.test_image(cfg, image, cfg.worktree) is False
    assert not emu.tests_passed(Registry(cfg.images).get(image))
    assert capsys.readouterr().out.splitlines()[-1] == "FAIL"


def test_update_only_keeps_the_suite_results(cfg, image, monkeypatch):
    registry = Registry(cfg.images)
    registry.get(image)["tests"] = dict(PASSED, fwupdate_emuwut="no output")
    registry.save()
    fake = FakeContainer()
    monkeypatch.setattr(emu.container, "run_script", fake.run_script)
    assert emu.test_image(cfg, image, cfg.worktree, update_only=True) is True
    assert len(fake.scripts) == 1
    tests = Registry(cfg.images).get(image)["tests"]
    assert tests["itest_emu"] == PASSED["itest_emu"]
    assert tests["fwupdate_emuwut"] == "transfer done / ok"


def test_test_image_refuses_another_commit(cfg, image, monkeypatch):
    monkeypatch.setattr(emu, "worktree_commit", lambda worktree: "999999999")
    with pytest.raises(PltError, match="worktree is at 999999999"):
        emu.test_image(cfg, image, cfg.worktree)


def test_test_image_refuses_an_image_without_commit(cfg, make_image):
    make_image("added")
    with pytest.raises(PltError, match="not built by plt"):
        emu.test_image(cfg, "added", cfg.worktree)


def test_try_scripts(cfg, monkeypatch, capsys):
    fake = FakeContainer(suite="14 passed")
    monkeypatch.setattr(emu.container, "run_script", fake.run_script)
    emu.try_worktree(cfg, cfg.worktree, ["CONFIG_A=y"], wut=True)
    build = fake.scripts[0]
    assert "build-dev-emuwut" in build
    assert (
        "-DCONFIG_SOC_STM32F4_EMULATED=y -DCONFIG_TIMER_STM32_RTC_WUT=y -DCONFIG_A=y"
    ) in build
    assert "itest.sh" in fake.scripts[1]
    out = capsys.readouterr().out
    assert "14 passed" in out
    assert "results: " in out


def test_try_reports_a_failed_build(cfg, monkeypatch):
    def failing(cfg, worktree, script, network=False):
        return subprocess.CompletedProcess([], 0, "rc=1\n", "")

    monkeypatch.setattr(emu.container, "run_script", failing)
    with pytest.raises(PltError, match="build failed, log "):
        emu.try_worktree(cfg, cfg.worktree, [])
