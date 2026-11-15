# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Fixtures shared by the tests: settings in a temporary directory, firmware bundles."""

import json
import zipfile
from pathlib import Path

import pytest

from plt import config

DATA = Path(__file__).parent / "data"


@pytest.fixture
def cfg(tmp_path):
    """Settings whose data directories are all below the test's directory."""
    return config.load(
        {
            "PLT_CONFIG": str(tmp_path / "plt.conf"),
            "PLT_HOME": str(tmp_path / "home"),
            "PLT_WORKTREE": str(tmp_path / "worktree"),
            "PLT_PIP_CACHE": str(tmp_path / "pip"),
        }
    )


def write_bundle(path, version, firmware=b"firmware", resources=b"resources"):
    """Write a firmware bundle as `pbl bundle` lays it out."""
    manifest = {"firmware": {"name": "pebbleos.bin", "versionTag": version}}
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("pebbleos.bin", firmware)
        if resources is not None:
            manifest["resources"] = {"name": "system_resources.pbpack"}
            bundle.writestr("system_resources.pbpack", resources)
        bundle.writestr("manifest.json", json.dumps(manifest))
    return path


@pytest.fixture
def make_image(cfg, tmp_path):
    """Register an image with a bundle of its own; returns its registry entry."""
    from plt.images import Registry

    def make(name, version="v4.47.0-1-gabcdef012", **extra):
        source = tmp_path / f"{name}-source"
        source.mkdir()
        pbz = write_bundle(source / "bundle.pbz", version)
        dictionary = source / "log.dict.json"
        dictionary.write_text("{}")
        return Registry(cfg.images).register(name, pbz, dictionary, **extra)

    return make
