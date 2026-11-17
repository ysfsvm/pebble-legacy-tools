# SPDX-FileCopyrightText: 2026 retake
# SPDX-License-Identifier: Apache-2.0
"""The emoji pack: taken from a bundle only when it matches the manifest, sent where asked."""

import json
import zipfile

import pytest

from plt import cli, config, emoji_pack, watch
from plt.util import PltError

PACK = bytes(range(256)) * 3 + b"\x01\x02"


@pytest.mark.parametrize(
    "data, crc",
    [
        (b"", 0xFFFFFFFF),
        (b"1", 0x17F7AD5C),
        (b"12", 0xF85FE76C),
        (b"123", 0xA932511F),
        (b"1234", 0xC2091428),
        (b"123456789", 0xAFF19057),
        (PACK, 0x3003B626),
    ],
)
def test_crc_is_libpebble2s(data, crc):
    # Values from libpebble2.util.stm32_crc.crc32, the CRC the bundle tool writes.
    assert emoji_pack.stm32_crc(data) == crc


def write_bundle(path, pack=PACK, size=None, crc=None, files=True):
    manifest = {"firmware": {"name": "pebbleos.bin"}}
    if files:
        manifest["files"] = [
            {
                "name": "emoji_snowy_dvt.pbpack",
                "target": "emoji",
                "size": len(pack) if size is None else size,
                "crc": emoji_pack.stm32_crc(pack) if crc is None else crc,
            }
        ]
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("pebbleos.bin", b"firmware")
        bundle.writestr("emoji_snowy_dvt.pbpack", pack)
        bundle.writestr("manifest.json", json.dumps(manifest))
    return path


def test_pack_from_a_bundle(tmp_path):
    assert emoji_pack.read_pack(write_bundle(tmp_path / "fw.pbz")) == PACK


def test_pack_file_as_is(tmp_path):
    path = tmp_path / "emoji.pbpack"
    path.write_bytes(PACK)
    assert emoji_pack.read_pack(path) == PACK


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"crc": 1}, "does not match the manifest"),
        ({"size": 1}, "does not match the manifest"),
        ({"files": False}, "carries no emoji pack"),
    ],
)
def test_bad_bundle_is_refused(tmp_path, kwargs, message):
    with pytest.raises(PltError, match=message):
        emoji_pack.read_pack(write_bundle(tmp_path / "fw.pbz", **kwargs))


@pytest.mark.parametrize(
    "phone, url",
    [
        ("192.168.1.20", f"ws://192.168.1.20:{config.DEVCONN_PORT}/"),
        ("ws://10.0.0.2:9000/", "ws://10.0.0.2:9000/"),
        ("qemu:12344", "qemu:12344"),
    ],
)
def test_phone_url(phone, url):
    assert watch.phone_url(phone) == url


def test_emoji_command_sends_the_pack(cfg, tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(
        emoji_pack,
        "install",
        lambda pack, url: sent.append((pack, url)) or ("v4.47.0-legacy.1", 1.0),
    )
    monkeypatch.setattr(config, "load", lambda: cfg)
    bundle = write_bundle(tmp_path / "fw.pbz")
    assert cli.main(["watch", "emoji", str(bundle), "--phone", "192.168.1.20"]) == 0
    assert sent == [(PACK, f"ws://192.168.1.20:{config.DEVCONN_PORT}/")]
    assert "v4.47.0-legacy.1" in capsys.readouterr().out


def test_emoji_command_refuses_a_bad_bundle(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(emoji_pack, "install", lambda pack, url: pytest.fail("sent"))
    monkeypatch.setattr(config, "load", lambda: cfg)
    bundle = write_bundle(tmp_path / "fw.pbz", crc=1)
    assert cli.main(["watch", "emoji", str(bundle), "--phone", "192.168.1.20"]) == 1
    assert "does not match the manifest" in capsys.readouterr().err


def test_unreachable_phone_is_a_message(cfg, tmp_path, monkeypatch, capsys):
    pytest.importorskip("libpebble2")
    from libpebble2.exceptions import ConnectionError as PebbleConnectionError

    def refuse(target):
        raise PebbleConnectionError("[Errno 111] Connection refused")

    monkeypatch.setattr(emoji_pack, "connect", refuse)
    monkeypatch.setattr(config, "load", lambda: cfg)
    bundle = write_bundle(tmp_path / "fw.pbz")
    assert cli.main(["watch", "emoji", str(bundle), "--phone", "192.168.1.20"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("plt: no answer from ws://192.168.1.20:")
    assert "developer connection" in err
