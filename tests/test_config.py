# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Settings: where each comes from and which source wins."""

import sys
from pathlib import Path

from plt import config


def test_defaults(tmp_path):
    cfg = config.load({"PLT_CONFIG": str(tmp_path / "none")})
    home = Path("~/pebble-legacy").expanduser()
    assert cfg.home == home
    assert cfg.images == home / "images"
    assert cfg.registry == home / "images" / "registry.json"
    assert cfg.logs == home / "logs"
    assert cfg.bench == home / "bench"
    assert cfg.reference_screens == home / "bench" / "reference-screens.json"
    assert cfg.results == home / "bench" / "results"
    assert cfg.worktree == Path.cwd()
    assert cfg.base_worktree is None
    assert cfg.python == sys.executable
    assert cfg.qemu == "qemu-system-arm"
    assert cfg.container == config.DEFAULT_CONTAINER
    assert cfg.mounts == ()
    assert cfg.watch_bt is None
    assert cfg.hostnet is False


def test_file_overrides_default(tmp_path):
    conf = tmp_path / "plt.conf"
    conf.write_text(
        "# a comment\n"
        "\n"
        f"PLT_HOME = {tmp_path}/data\n"
        'PLT_CONTAINER="example.org/builder:v1"\n'
        "PLT_WATCH_BT='00:11:22:33:44:55'\n"
        "not a setting\n"
    )
    cfg = config.load({"PLT_CONFIG": str(conf)})
    assert cfg.home == tmp_path / "data"
    assert cfg.images == tmp_path / "data" / "images"
    assert cfg.container == "example.org/builder:v1"
    assert cfg.watch_bt == "00:11:22:33:44:55"


def test_environment_overrides_file(tmp_path):
    conf = tmp_path / "plt.conf"
    conf.write_text(f"PLT_HOME={tmp_path}/from-file\nPLT_QEMU=/opt/file/qemu\n")
    cfg = config.load(
        {
            "PLT_CONFIG": str(conf),
            "PLT_HOME": str(tmp_path / "from-env"),
            "PLT_LOGS": str(tmp_path / "logs-elsewhere"),
        }
    )
    assert cfg.home == tmp_path / "from-env"
    assert cfg.images == tmp_path / "from-env" / "images"
    assert cfg.logs == tmp_path / "logs-elsewhere"
    assert cfg.qemu == "/opt/file/qemu"


def test_empty_environment_value_falls_through(tmp_path):
    conf = tmp_path / "plt.conf"
    conf.write_text("PLT_CONTAINER=from-file\n")
    cfg = config.load({"PLT_CONFIG": str(conf), "PLT_CONTAINER": ""})
    assert cfg.container == "from-file"


def test_config_file_location(tmp_path):
    assert config.config_path({"PLT_CONFIG": "/x/y.conf"}) == Path("/x/y.conf")
    assert config.config_path({"XDG_CONFIG_HOME": "/xdg"}) == Path("/xdg/plt.conf")
    assert config.config_path({}) == Path("~/.config/plt.conf").expanduser()


def test_paths_are_expanded_and_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = config.load(
        {
            "PLT_CONFIG": str(tmp_path / "none"),
            "PLT_WORKTREE": "firmware",
            "PLT_BASE_WORKTREE": "~/core",
            "PLT_QEMU": "~/qemu/qemu-system-arm",
            "PLT_MOUNTS": f"{tmp_path}/a::relative",
            "PLT_HOSTNET": "1",
        }
    )
    assert cfg.worktree == tmp_path / "firmware"
    assert cfg.base_worktree == Path("~/core").expanduser()
    assert cfg.qemu == str(Path("~/qemu/qemu-system-arm").expanduser())
    assert cfg.mounts == (tmp_path / "a", tmp_path / "relative")
    assert cfg.hostnet is True
