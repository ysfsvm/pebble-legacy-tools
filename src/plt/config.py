# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""Settings, paths and external programs.

A setting is read from the environment first, then from the configuration
file, then it takes its default. The file holds KEY=VALUE lines; PLT_CONFIG
names it, the default is plt.conf in $XDG_CONFIG_HOME or ~/.config.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"

# External programs, as found on PATH.
PODMAN = "podman"
GIT = "git"
ADB = "adb"
BLUETOOTHCTL = "bluetoothctl"
BTMGMT = "btmgmt"

# Inside the build container.
TOOLCHAIN_PREFIX = "/opt/pebbleos-sdk/arm-none-eabi/bin/arm-none-eabi-"
CONTAINER_PIP_CACHE = "/root/.cache/pip"

# The Core app's developer connection, once adb forwards its port.
DEVCONN_PORT = 9000
DEVCONN_URL = f"ws://127.0.0.1:{DEVCONN_PORT}/"

DEFAULT_CONTAINER = "ghcr.io/coredevices/pebbleos-docker:v13"

# Runs a module of this package with any Python: the package is appended to
# that interpreter's search path, behind its own packages.
_RUN_MODULE = (
    "import runpy, sys; sys.path.append(sys.argv[1]); module = sys.argv[2]; "
    "del sys.argv[1:3]; runpy.run_module(module, run_name='__main__', alter_sys=True)"
)


def config_path(environ):
    """The configuration file the environment selects."""
    explicit = environ.get("PLT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "plt.conf"


def read_config_file(path):
    """KEY=VALUE pairs of a configuration file; an absent file has none."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class Settings:
    """Every setting, resolved."""

    home: Path
    images: Path
    logs: Path
    bench: Path
    worktree: Path
    base_worktree: Path | None
    python: str
    qemu: str
    container: str
    pip_cache: Path
    mounts: tuple[Path, ...]
    watch_bt: str | None
    hostnet: bool

    @property
    def registry(self):
        """The registry file of the built images."""
        return self.images / "registry.json"

    @property
    def reference_screens(self):
        """The screens a valid measurement run shows, one per key."""
        return self.bench / "reference-screens.json"

    @property
    def results(self):
        """The directory of the measurement results."""
        return self.bench / "results"

    def module_command(self, module):
        """The command that runs a module of this package with the watch's Python."""
        return [self.python, "-u", "-c", _RUN_MODULE, str(PACKAGE_DIR.parent), module]


def load(environ=None):
    """Resolve the settings from an environment (default: the process's own)."""
    environ = os.environ if environ is None else environ
    file_values = read_config_file(config_path(environ))

    def text(name, default=None):
        return environ.get(name) or file_values.get(name) or default

    def path(name, default=None):
        value = text(name, default)
        return Path(os.path.abspath(os.path.expanduser(value))) if value else None

    home = path("PLT_HOME", "~/pebble-legacy")
    return Settings(
        home=home,
        images=path("PLT_IMAGES", home / "images"),
        logs=path("PLT_LOGS", home / "logs"),
        bench=path("PLT_BENCH", home / "bench"),
        worktree=path("PLT_WORKTREE", "."),
        base_worktree=path("PLT_BASE_WORKTREE"),
        python=os.path.expanduser(text("PLT_PYTHON", sys.executable)),
        qemu=os.path.expanduser(text("PLT_QEMU", "qemu-system-arm")),
        container=text("PLT_CONTAINER", DEFAULT_CONTAINER),
        pip_cache=path("PLT_PIP_CACHE", "~/.cache/pip-pebble"),
        mounts=tuple(
            Path(os.path.abspath(os.path.expanduser(d)))
            for d in (text("PLT_MOUNTS") or "").split(":")
            if d
        ),
        watch_bt=text("PLT_WATCH_BT"),
        hostnet=bool(text("PLT_HOSTNET")),
    )
