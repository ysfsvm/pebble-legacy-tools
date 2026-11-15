# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The package as installed: its data files, and its modules under another Python."""

import subprocess

from plt import config


def test_module_command_runs_a_module_of_the_package(cfg):
    done = subprocess.run(
        [*cfg.module_command("plt.flashlog"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert "decode" in done.stdout


def test_packaged_data_is_present():
    for name in (
        "itest.sh",
        "core_compare.sh",
        "compare_objects.py",
        "emu_session.py",
        "emu_fw_update.py",
        "emu_perf_script.py",
        "reference-screens.json",
    ):
        assert (config.DATA_DIR / name).is_file(), name
