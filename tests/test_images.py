# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The image registry: registration, roles, and merging of concurrent writers."""

import json
import threading

import pytest

from conftest import write_bundle
from plt import images
from plt.images import Registry
from plt.util import PltError, sha256


def test_empty_registry(cfg):
    registry = Registry(cfg.images)
    assert registry.images == {}
    assert registry.role("current") is None
    assert registry.role("recovery") is None
    assert not cfg.registry.exists()


def test_register_copies_files_and_records_them(cfg, tmp_path):
    pbz = write_bundle(tmp_path / "fw.pbz", "v4.47.0-3-g0123456789")
    dictionary = tmp_path / "log.dict.json"
    dictionary.write_text('{"1": {}}')
    elf = tmp_path / "pebbleos.elf"
    elf.write_bytes(b"\x7fELF")
    entry = Registry(cfg.images).register(
        "r1-0123456", pbz, dictionary, elf, commit="012345678"
    )
    directory = cfg.images / "r1-0123456"
    assert entry["files"] == {
        "pbz": str(directory / "r1-0123456.pbz"),
        "dict": str(directory / "log.dict.json"),
        "elf": str(directory / "pebbleos.elf"),
    }
    assert entry["version"] == "v4.47.0-3-g0123456789"
    assert entry["sha256"] == sha256(pbz)
    assert entry["tests"] == {}
    assert entry["installs"] == []
    assert entry["commit"] == "012345678"
    assert Registry(cfg.images).get("r1-0123456") == entry


def test_an_image_is_never_overwritten(cfg, make_image, tmp_path):
    make_image("one")
    pbz = write_bundle(tmp_path / "other.pbz", "v4.47.0-9-gffffffffff")
    with pytest.raises(PltError, match="never overwritten"):
        Registry(cfg.images).register("one", pbz, pbz)
    assert Registry(cfg.images).get("one")["version"] == "v4.47.0-1-gabcdef012"


@pytest.mark.parametrize("name", ["", "a b", "x;rm", "../up", "-D"])
def test_unusable_names_are_refused(cfg, tmp_path, name):
    pbz = write_bundle(tmp_path / "fw.pbz", "v1")
    with pytest.raises(PltError, match="not a usable image name"):
        Registry(cfg.images).register(name, pbz, pbz)


def test_missing_and_broken_files_are_errors(cfg, tmp_path):
    pbz = write_bundle(tmp_path / "fw.pbz", "v1")
    with pytest.raises(PltError, match="no such file"):
        Registry(cfg.images).register("a", pbz, tmp_path / "missing.json")
    not_a_bundle = tmp_path / "text.pbz"
    not_a_bundle.write_text("text")
    with pytest.raises(PltError, match="not a firmware bundle"):
        Registry(cfg.images).register("b", not_a_bundle, pbz)


def test_unknown_image(cfg):
    with pytest.raises(PltError, match="no image 'ghost'"):
        Registry(cfg.images).get("ghost")


def test_roles(cfg, make_image, capsys):
    make_image("one")
    images.set_role(cfg, "one", "recovery")
    assert capsys.readouterr().out == "recovery = one\n"
    assert Registry(cfg.images).role("recovery") == "one"
    with pytest.raises(PltError):
        images.set_role(cfg, "ghost", "current")


def test_find_version_prefers_the_named_then_the_current_image(cfg, make_image):
    make_image("a", version="v1")
    make_image("b", version="v1")
    make_image("c", version="v1")
    registry = Registry(cfg.images)
    assert registry.find_version("v1")[0] == "a"
    registry.set_role("current", "b")
    assert registry.find_version("v1")[0] == "b"
    assert registry.find_version("v1", prefer="c")[0] == "c"
    assert registry.find_version("v2") == (None, None)


def test_save_keeps_what_another_process_wrote(cfg, make_image):
    make_image("one")
    make_image("two")
    first, second = Registry(cfg.images), Registry(cfg.images)
    first.get("one")["tests"] = {"itest_emu": "14 passed"}
    first.set_role("recovery", "one")
    first.save()
    second.get("two")["installs"].append("2026-01-01 10:00 from v0")
    second.set_role("current", "two")
    second.save()
    merged = Registry(cfg.images)
    assert merged.get("one")["tests"] == {"itest_emu": "14 passed"}
    assert merged.get("two")["installs"] == ["2026-01-01 10:00 from v0"]
    assert merged.role("recovery") == "one"
    assert merged.role("current") == "two"


def test_save_twice_writes_only_new_changes(cfg, make_image):
    make_image("one")
    stale, other = Registry(cfg.images), Registry(cfg.images)
    stale.get("one")["tests"] = {"when": "first"}
    stale.save()
    other.get("one")["tests"] = {"when": "second"}
    other.save()
    stale.set_role("current", "one")
    stale.save()
    merged = Registry(cfg.images)
    assert merged.get("one")["tests"] == {"when": "second"}
    assert merged.role("current") == "one"


def test_concurrent_writers_lose_nothing(cfg, make_image):
    writers = 12
    for i in range(writers):
        make_image(f"img{i}")
    barrier = threading.Barrier(writers)
    errors = []

    def write(i):
        try:
            registry = Registry(cfg.images)
            barrier.wait()
            for step in range(5):
                registry.get(f"img{i}")["installs"].append(f"step {step}")
                registry.save()
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    merged = Registry(cfg.images)
    expected = [f"step {step}" for step in range(5)]
    assert all(merged.get(f"img{i}")["installs"] == expected for i in range(writers))
    json.loads(cfg.registry.read_text())
    assert not (cfg.images / "registry.json.tmp").exists()


def test_registry_file_format_is_stable(cfg, make_image):
    make_image("one")
    text = cfg.registry.read_text()
    assert text == json.dumps(json.loads(text), indent=1, sort_keys=True)


def test_list(cfg, make_image, capsys):
    make_image(
        "one",
        options=["CONFIG_RELEASE=y"],
        registered="2026-01-01 10:00",
    )
    make_image("two", registered="2026-01-02 10:00")
    registry = Registry(cfg.images)
    registry.get("one")["tests"] = {"itest_emu": "14 passed", "when": "now"}
    registry.get("one")["installs"] = ["a", "b", "c", "d"]
    registry.set_role("current", "one")
    registry.save()
    images.list_images(cfg)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "one",
        "v4.47.0-1-gabcdef012",
        registry.get("one")["sha256"][:12],
        "2026-01-01",
        "10:00",
        "current",
    ]
    assert lines[1].strip() == "options: CONFIG_RELEASE=y"
    assert lines[2].strip() == "tests: itest_emu: 14 passed, when: now"
    assert lines[3].strip() == "installed: b, c, d"
    assert lines[4].startswith("two ")
    assert lines[5].strip() == "options: -"
    assert lines[6].strip() == "tests: no tests recorded"
    assert len(lines) == 7
