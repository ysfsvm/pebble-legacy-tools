# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The registry of built images: their files, tests, installs and the two roles."""

import copy
import fcntl
import json
import os
import shutil
import zipfile
from pathlib import Path

from plt.util import PltError, check_name, now, sha256

ROLES = ("current", "recovery")


def bundle_version(pbz):
    """The version a firmware bundle carries in its manifest."""
    try:
        with zipfile.ZipFile(pbz) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        return manifest["firmware"].get("versionTag", "?")
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        raise PltError(f"{pbz} is not a firmware bundle") from None


def _empty():
    return {"images": {}, "current": None, "recovery": None}


class Registry:
    """The registry file, loaded; `save` writes back what changed since."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "registry.json"
        self.data = self._read()
        self._loaded = copy.deepcopy(self.data)

    def _read(self):
        if not self.path.exists():
            return _empty()
        with open(self.path) as f:
            return json.load(f)

    @property
    def images(self):
        """Image entries by name."""
        return self.data["images"]

    def role(self, role):
        """The name of the image that has a role, or None."""
        return self.data.get(role)

    def set_role(self, role, name):
        """Give a role to an image."""
        self.data[role] = name

    def get(self, name):
        """The entry of an image; an unknown name is an error."""
        if name not in self.images:
            raise PltError(f"no image '{name}' in the registry (plt images)")
        return self.images[name]

    def find_version(self, version, prefer=None):
        """(name, entry) of an image with that version, or (None, None).

        Images built from one commit share a version: the preferred name and
        the current image are tried first.
        """
        for name in (prefer, self.role("current")):
            if name in self.images and self.images[name]["version"] == version:
                return name, self.images[name]
        for name, entry in self.images.items():
            if entry["version"] == version:
                return name, entry
        return None, None

    def save(self):
        """Merge this process's changes into the file, under the registry lock.

        Only what changed since the load is written, so that another process
        that saved in between keeps its changes.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(f"{self.path}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            disk = self._read()
            for role in ROLES:
                if self.data.get(role) != self._loaded.get(role):
                    disk[role] = self.data.get(role)
            for name in set(self.images) | set(self._loaded["images"]):
                if self.images.get(name) == self._loaded["images"].get(name):
                    continue
                if name in self.images:
                    disk["images"][name] = self.images[name]
                else:
                    disk["images"].pop(name, None)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                json.dump(disk, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        self._loaded = copy.deepcopy(self.data)

    def register(self, name, pbz, dict_json, elf=None, **extra):
        """Copy an image's files into the images directory and record it."""
        check_name(name)
        if name in self.images:
            raise PltError(
                f"image '{name}' exists; pick another name "
                "(an image is never overwritten)"
            )
        for source in (pbz, dict_json, elf):
            if source and not os.path.isfile(source):
                raise PltError(f"no such file: {source}")
        directory = self.directory / name
        directory.mkdir(parents=True, exist_ok=True)
        files = {
            "pbz": str(directory / f"{name}.pbz"),
            "dict": str(directory / "log.dict.json"),
        }
        shutil.copy(pbz, files["pbz"])
        shutil.copy(dict_json, files["dict"])
        if elf:
            files["elf"] = str(directory / "pebbleos.elf")
            shutil.copy(elf, files["elf"])
        entry = {
            "version": bundle_version(files["pbz"]),
            "sha256": sha256(files["pbz"]),
            "files": files,
            "registered": now(),
            "tests": {},
            "installs": [],
        }
        entry.update(extra)
        self.images[name] = entry
        self.save()
        return entry


def list_images(cfg):
    """Print every image, oldest first, with its options, tests and installs."""
    registry = Registry(cfg.images)
    indent = " " * 34
    for name, entry in sorted(
        registry.images.items(), key=lambda item: item[1]["registered"]
    ):
        roles = " ".join(role for role in ROLES if registry.role(role) == name)
        tests = ", ".join(f"{k}: {v}" for k, v in entry["tests"].items())
        print(
            f"{name:34s} {entry['version']:26s} {entry['sha256'][:12]} "
            f"{entry['registered']} {roles}"
        )
        print(f"{indent} options: {' '.join(entry.get('options', [])) or '-'}")
        print(f"{indent} tests: {tests or 'no tests recorded'}")
        if entry["installs"]:
            print(f"{indent} installed: {', '.join(entry['installs'][-3:])}")


def add_image(cfg, name, pbz, dict_json, elf=None):
    """Register an image that was built outside this tool."""
    entry = Registry(cfg.images).register(
        name, pbz, dict_json, elf, note="registered from an earlier build"
    )
    print(f"{name}: {entry['version']} sha {entry['sha256'][:16]}")


def set_role(cfg, name, role):
    """Make an image the current or the recovery image."""
    registry = Registry(cfg.images)
    registry.get(name)
    registry.set_role(role, name)
    registry.save()
    print(f"{role} = {name}")
