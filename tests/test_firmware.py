"""Firmware package validation and the update plan (synthetic images only)."""

from __future__ import annotations

import json
import struct

import pytest

from ssmouse import firmware
from ssmouse.fake import make_fake_mouse


def _image(size: int, fill: int) -> bytes:
    vectors = struct.pack("<II", 0x20005FE0, 0x00025521)
    return vectors + bytes([fill]) * (size - len(vectors))


@pytest.fixture
def package_dir(tmp_path):
    (tmp_path / "firmware-rival-3-wireless-mouse-v1.05.bin").write_bytes(_image(0x12000, 1))
    (tmp_path / "nordic-dongle-firmware-rival-3-wireless-v1.05.bin").write_bytes(
        _image(0x9000, 2))
    (tmp_path / "holtek-dongle-firmware-rival-3-wireless-v1.05.bin").write_bytes(
        _image(0x4000, 3))
    (tmp_path / "version.json").write_text(
        json.dumps({"firmware_version": "1.05.0.0", "device_name": "rival_3_wireless"}))
    return tmp_path


def test_versions() -> None:
    assert firmware.parse_version("1.04.0.0") == (1, 4)
    assert firmware.format_version((1, 4)) == "1.04"
    assert firmware.FirmwareStatus("1.04", "1.04").up_to_date is True
    assert firmware.FirmwareStatus("1.03", "1.04").up_to_date is False
    assert firmware.FirmwareStatus(None, None).up_to_date is None


def test_load_package(package_dir) -> None:
    package = firmware.load_package(package_dir)
    assert package.version == "1.05"
    assert len(package.images["mouse"]) == 0x12000


def test_rejects_bad_images(package_dir) -> None:
    (package_dir / "holtek-dongle-firmware-rival-3-wireless-v1.05.bin").write_bytes(b"\xff" * 0x4000)
    with pytest.raises(firmware.FirmwareError, match="does not look like"):
        firmware.load_package(package_dir)
    (package_dir / "holtek-dongle-firmware-rival-3-wireless-v1.05.bin").write_bytes(b"\x00" * 10)
    with pytest.raises(firmware.FirmwareError, match="bytes"):
        firmware.load_package(package_dir)


def test_plan_matches_vendor_sequence(package_dir) -> None:
    package = firmware.load_package(package_dir)
    plan = firmware.build_plan(package, power=(1, 50, 1200, 300), polling_level=1)
    writes = [step.payload for step in plan if step.kind.startswith("write")]
    # Normalise: low-power gaming mode off, polling 1000 Hz.
    assert writes[0] == bytes.fromhex("2b0032b0042c01")
    assert writes[1] == bytes.fromhex("1700")
    # Mouse: erase entry 2, then 3 buffer writes + 1 push per 0x80 block.
    assert writes[2] == bytes.fromhex("08000200")
    assert writes[3][:6] == bytes.fromhex("030000003000") and len(writes[3]) == 6 + 0x30
    assert writes[4][:6] == bytes.fromhex("030030003000")
    assert writes[5][:6] == bytes.fromhex("030060002000") and len(writes[5]) == 6 + 0x20
    assert writes[6] == bytes.fromhex("050002000000000080 00".replace(" ", ""))
    mouse_blocks = 0x12000 // 0x80
    radio_erase = 2 + 1 + mouse_blocks * 4
    assert writes[radio_erase] == bytes.fromhex("48000200")
    assert writes[radio_erase + 1][0] == 0x43
    usb_erase = radio_erase + 1 + (0x9000 // 0x80) * 4
    assert writes[usb_erase] == bytes.fromhex("48000300")
    assert writes[usb_erase + 4] == bytes.fromhex("45000300000000008000")
    # Reset mouse, restore polling and power, reset receiver.
    assert writes[-4:] == [bytes([0x01]), bytes([0x17, 1]),
                           bytes.fromhex("2b0132b0042c01"), bytes([0x41])]
    assert plan[-2].kind == "write-nofail"
    percents = [step.percent for step in plan if step.kind == "progress"]
    assert percents[-1] == 100 and percents == sorted(percents)


def test_plan_skips_normalisation_when_not_needed(package_dir) -> None:
    plan = firmware.build_plan(firmware.load_package(package_dir), (0, 50, 1200, 300), 0)
    writes = [step.payload for step in plan if step.kind.startswith("write")]
    assert writes[0] == bytes.fromhex("08000200")
    assert writes[-2:] == [bytes([0x01]), bytes([0x41])]


def test_update_refuses_same_version(package_dir) -> None:
    package = firmware.load_package(package_dir)
    package.version = "1.04"
    with pytest.raises(firmware.FirmwareError, match="already"):
        firmware.update(make_fake_mouse(), package)


def test_update_dry_run_sends_nothing(package_dir) -> None:
    mouse = make_fake_mouse()
    steps = firmware.update(mouse, firmware.load_package(package_dir), dry_run=True)
    assert steps
    transport = mouse._transport  # noqa: SLF001
    assert all(write[0] not in (0x03, 0x05, 0x08, 0x43, 0x45, 0x48) for write in transport.writes)
