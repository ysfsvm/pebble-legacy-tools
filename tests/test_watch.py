# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The guarded install: what may be flashed, in which order, and what counts as done."""

import subprocess

import pytest

from plt import watch
from plt.images import Registry
from plt.util import PltError

PASSED = {
    "itest_emu": "14 passed in 90.00s",
    "itest_emuwut": "14 passed in 100.00s",
    "fwupdate_emuwut": "transfer+install done in 65 s; waiting for the reboot / ok",
}
OLD, NEW = "v4.47.0-1-g0000000000", "v4.47.0-2-g1111111111"


@pytest.fixture
def images(cfg, make_image):
    """A tested image, an untested one, and a recovery image."""
    make_image("tested", version=NEW)
    make_image("untested", version=NEW)
    make_image("fallback", version=OLD)
    registry = Registry(cfg.images)
    registry.get("tested")["tests"] = dict(PASSED)
    registry.set_role("recovery", "fallback")
    registry.save()
    return registry


class FakeWatch:
    """Stands in for the phone and the watch; records what the install does."""

    def __init__(self, monkeypatch, versions, install="InstallStatus 0 (ok)"):
        self.calls = []
        self.versions = list(versions)
        self.install = install
        monkeypatch.setattr(watch, "adb_ready", lambda: True)
        monkeypatch.setattr(watch, "watch_version", self.version)
        monkeypatch.setattr(watch, "dump_log", self.dump_log)
        monkeypatch.setattr(watch, "install_bundle", self.install_bundle)
        monkeypatch.setattr(watch, "summarise", lambda txt: None)
        monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)

    def version(self, cfg, timeout=30):
        self.calls.append("version")
        return self.versions.pop(0) if self.versions else None

    def dump_log(self, cfg, registry, generation, tag, image=None, have=None):
        self.calls.append(f"log {tag}")
        return f"/logs/{tag}", None, False

    def install_bundle(self, cfg, pbz):
        self.calls.append("install")
        return self.install


def test_flash_problems(cfg, images):
    assert watch.flash_problems(images, images.get("tested")) == []
    problems = watch.flash_problems(images, images.get("untested"))
    assert len(problems) == 1
    assert "plt emu test" in problems[0]
    images.set_role("recovery", None)
    problems = watch.flash_problems(images, images.get("untested"))
    assert len(problems) == 2
    assert "recovery" in problems[1]


@pytest.mark.parametrize("missing", list(PASSED))
def test_every_test_result_is_required(images, missing):
    entry = images.get("tested")
    del entry["tests"][missing]
    assert watch.flash_problems(images, entry)


def test_flash_refuses_an_untested_image(cfg, images, monkeypatch):
    fake = FakeWatch(monkeypatch, [OLD])
    with pytest.raises(PltError, match="not flashing"):
        watch.flash(cfg, "untested")
    assert fake.calls == []


def test_flash_refuses_without_recovery_image(cfg, images, monkeypatch):
    images.set_role("recovery", None)
    images.save()
    fake = FakeWatch(monkeypatch, [OLD])
    with pytest.raises(PltError, match="no recovery image set"):
        watch.flash(cfg, "tested")
    assert fake.calls == []


def test_flash_refuses_a_changed_image_file_even_when_forced(cfg, images, monkeypatch):
    fake = FakeWatch(monkeypatch, [OLD])
    with open(images.get("tested")["files"]["pbz"], "ab") as f:
        f.write(b"x")
    with pytest.raises(PltError, match="sha256"):
        watch.flash(cfg, "tested", force="because")
    assert fake.calls == []


def test_flash_needs_the_phone_and_the_watch(cfg, images, monkeypatch):
    fake = FakeWatch(monkeypatch, [None])
    with pytest.raises(PltError, match="does not answer"):
        watch.flash(cfg, "tested")
    assert fake.calls == ["version"]
    monkeypatch.setattr(watch, "adb_ready", lambda: False)
    with pytest.raises(PltError, match="phone not on adb"):
        watch.flash(cfg, "tested")


def test_flash_backs_up_the_log_then_installs_then_verifies(
    cfg, images, monkeypatch, capsys
):
    fake = FakeWatch(monkeypatch, [OLD, None, NEW])
    watch.flash(cfg, "tested")
    assert fake.calls == [
        "version",
        "log pre-tested",
        "install",
        "version",
        "version",
        "log boot-tested",
    ]
    registry = Registry(cfg.images)
    assert registry.role("current") == "tested"
    installs = registry.get("tested")["installs"]
    assert len(installs) == 1
    assert installs[0].endswith(f"from {OLD}")
    out = capsys.readouterr().out
    assert "log backup: /logs/pre-tested.bin" in out
    assert f"watch booted {NEW}" in out


def test_flash_fails_when_the_watch_reports_another_version(cfg, images, monkeypatch):
    FakeWatch(monkeypatch, [OLD, OLD])
    with pytest.raises(PltError, match=f"answers '{OLD}', expected {NEW}"):
        watch.flash(cfg, "tested")
    registry = Registry(cfg.images)
    assert registry.role("current") is None
    assert registry.get("tested")["installs"][0].endswith(
        f"-> FAILED, watch answers {OLD}"
    )


def test_flash_fails_when_the_watch_never_comes_back(cfg, images, monkeypatch):
    fake = FakeWatch(monkeypatch, [OLD])
    with pytest.raises(PltError, match="answers 'None'"):
        watch.flash(cfg, "tested")
    assert fake.calls.count("version") == 1 + watch.BOOT_POLLS
    assert Registry(cfg.images).role("current") is None


def test_flash_stops_when_the_phone_reports_no_install(cfg, images, monkeypatch):
    fake = FakeWatch(monkeypatch, [OLD], install="InstallStatus 1 (fail)")
    with pytest.raises(PltError, match="did not report a completed install"):
        watch.flash(cfg, "tested")
    assert fake.calls == ["version", "log pre-tested", "install"]
    assert Registry(cfg.images).get("tested")["installs"] == []


def test_forced_flash_records_the_reason(cfg, images, monkeypatch):
    FakeWatch(monkeypatch, [OLD, NEW])
    watch.flash(cfg, "untested", force="bisecting a display fault")
    install = Registry(cfg.images).get("untested")["installs"][0]
    assert install.endswith(f"from {OLD} (forced: bisecting a display fault)")


def test_install_bundle_reads_the_last_install_status(cfg, monkeypatch):
    def fake_run(cmd, seconds, **kwargs):
        out = "sent InstallFirmware, 9 bytes\nInstallStatus 0 (ok)\nsocket closed: x\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(watch, "run_limited", fake_run)
    assert watch.install_bundle(cfg, "x.pbz") == "InstallStatus 0 (ok)"


def test_watch_version(cfg, monkeypatch):
    answers = iter(["VERSION v4.47.0-2-g1111111111\n", "Traceback\n"])

    def fake_run(cmd, seconds, **kwargs):
        assert cmd[-2:] == ["plt.devconn", "version"]
        return subprocess.CompletedProcess(cmd, 0, next(answers), "")

    monkeypatch.setattr(watch, "run_limited", fake_run)
    assert watch.watch_version(cfg) == NEW
    assert watch.watch_version(cfg) is None


def test_status_without_phone(cfg, monkeypatch):
    monkeypatch.setattr(watch, "adb_ready", lambda: False)
    with pytest.raises(PltError, match="phone not on adb"):
        watch.status(cfg)


def test_status(cfg, images, monkeypatch, capsys):
    FakeWatch(monkeypatch, [OLD])
    watch.status(cfg)
    out = capsys.readouterr().out.splitlines()
    assert out[1] == f"watch: {OLD} (fallback)"
    assert out[2] == "registry: current=None recovery=fallback"


def test_summarise_prints_warnings_and_errors_only(tmp_path, capsys):
    log = tmp_path / "log.txt"
    log.write_text(
        "10:00:00   0 init.c:43 Booting\n"
        "10:00:01 100 a.c:1 routine\n"
        "10:00:02  50 b.c:2 a warning\n"
        "10:00:03   1 c.c:3 an error\n"
        "10:00:04  50 pfs.c:543 noise\n"
        "10:00:05 200 d.c:4 debug\n"
    )
    watch.summarise(log)
    assert capsys.readouterr().out.splitlines() == [
        "  6 lines, 10:00:02 .. 10:00:05",
        "  10:00:02  50 b.c:2 a warning",
        "  10:00:03   1 c.c:3 an error",
    ]


def test_recover_needs_an_image_and_an_address(cfg, images, monkeypatch):
    with pytest.raises(PltError, match="PLT_WATCH_BT"):
        watch.recover(cfg)
    images.set_role("recovery", None)
    images.save()
    with pytest.raises(PltError, match="no recovery image set"):
        watch.recover(cfg)


def test_recovery_tail_drops_the_progress_lines(tmp_path):
    log = tmp_path / "recover.log"
    log.write_text(
        "== window 1 10:00:00\n"
        "  firmware: 100000/923043 (15.9 KiB/s)\n"
        "  firmware: commit acked in 1.5 s\n"
        "  resources: 100000/519017 (15.9 KiB/s)\n"
        "DONE\n"
    )
    assert watch._recovery_tail(log) == [
        "== window 1 10:00:00",
        "  firmware: commit acked in 1.5 s",
        "DONE",
    ]


@pytest.mark.parametrize(("last", "recorded"), [("DONE", True), ("GAVE UP", False)])
def test_recover_records_only_a_finished_install(
    tmp_path, images, monkeypatch, last, recorded
):
    from plt import config

    cfg = config.load(
        {
            "PLT_CONFIG": str(tmp_path / "none"),
            "PLT_HOME": str(tmp_path / "home"),
            "PLT_WATCH_BT": "00:11:22:33:44:55",
        }
    )
    commands = []

    def fake_run(cmd, seconds, stdout=None, stderr=None):
        commands.append(cmd)
        stdout.write(f"== window 1 10:00:00\n{last}\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(watch, "run_limited", fake_run)
    if recorded:
        watch.recover(cfg)
    else:
        with pytest.raises(PltError):
            watch.recover(cfg)
    pbz = images.get("fallback")["files"]["pbz"]
    assert commands[0][-6:] == [
        "plt.recover",
        "install",
        "00:11:22:33:44:55",
        pbz,
        "--windows",
        "40",
    ]
    registry = Registry(cfg.images)
    assert (registry.role("current") == "fallback") is recorded
    assert len(registry.get("fallback")["installs"]) == (1 if recorded else 0)
