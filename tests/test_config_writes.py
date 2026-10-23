"""Write commands: packet layouts verified on a Rival 3 Wireless (fw 1.04)."""

from __future__ import annotations

import json

import pytest

from ssmouse.bindings import mapping_payloads, parse_binding
from ssmouse.fake import FakeTransport, make_fake_mouse
from ssmouse.lighting import LightingEffect, effect_payloads, parse_color
from ssmouse.profile import PowerSettings, Profile, apply_profile
from ssmouse.protocol import PowerMode, Protocol, sensor_value_from_cpi


def _cmd(request: bytes) -> str:
    """Hex of a framed request without the zero padding."""
    return request.rstrip(b"\x00").hex(" ")


# -- protocol builders ------------------------------------------------------


def test_dpi_write_matches_hardware_capture() -> None:
    proto = Protocol()
    sensor = [sensor_value_from_cpi(c) for c in (400, 800, 1200, 1600, 2000)]
    assert sensor == [0x04, 0x09, 0x0D, 0x12, 0x17]
    assert _cmd(proto.build_set_dpi(sensor, 3)) == "20 05 03 04 00 09 00 0d 00 12 00 17"


def test_dpi_write_pads_unused_presets() -> None:
    request = Protocol().build_set_dpi([0x09, 0x17], 1)
    assert request[:13] == bytes([0x20, 2, 1, 0x09, 0, 0x17, 0, 0, 0, 0, 0, 0, 0])


@pytest.mark.parametrize("presets,active", [([], 1), ([4] * 6, 1), ([4, 9], 3), ([0xD7], 1)])
def test_dpi_write_validation(presets, active) -> None:
    with pytest.raises(ValueError):
        Protocol().build_set_dpi(presets, active)


def test_sensor_value_snaps_to_grid() -> None:
    assert sensor_value_from_cpi(800) == 0x09
    assert sensor_value_from_cpi(830) == 0x09
    assert sensor_value_from_cpi(18000) == 0xD6
    assert sensor_value_from_cpi(50) == 0x00


def test_other_writes() -> None:
    proto = Protocol()
    assert _cmd(proto.build_set_polling_rate(500)) == "17 01"
    assert proto.build_set_polling_rate(1000)[:2] == b"\x17\x00"
    with pytest.raises(ValueError):
        proto.build_set_polling_rate(333)
    mode = PowerMode(power_saving=0, movement_saving=50, idle_lighting=1200, idle_sleep=300)
    assert _cmd(proto.build_set_power_mode(mode)) == "2b 00 32 b0 04 2c 01"
    assert _cmd(proto.build_set_debounce(6, 20)) == "1e 06 14"
    assert _cmd(proto.build_live_color(255, 0, 0)) == "13 01 ff"
    assert _cmd(proto.build_save_to_flash()) == "09"
    assert _cmd(proto.build_reset_profile()) == "1b"
    assert proto.parse_debounce_response(bytes([6, 20]) + bytes(62)) == (6, 20)


# -- lighting ----------------------------------------------------------------


def test_steady_effect_matches_hardware_capture() -> None:
    payloads = effect_payloads(LightingEffect("steady", [(255, 0, 0)]))
    assert len(payloads) == 4
    first, second, push, element = payloads
    assert first[:6] == bytes.fromhex("030000003000")
    assert first[6:].hex(" ").startswith(
        "88 13 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 01 ff 00 00 ff"
    )
    assert second[:6] == bytes.fromhex("030030002c00")
    assert push == bytes.fromhex("050010ff000000005c00")
    assert element == bytes.fromhex("1c000000000000")  # zone 0, x 0, y 0, effect 0


def test_colorshift_sections() -> None:
    blob = LightingEffect("colorshift", [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
                          duration_ms=3000).encode()
    assert len(blob) == 92
    assert int.from_bytes(blob[0:4], "little") == 3000
    assert blob[16] == 0  # wrap around
    assert blob[24] == 4  # three colours + return to the first
    sections = [blob[28 + 4 * i:32 + 4 * i] for i in range(4)]
    assert sections == [bytes([255, 0, 0, 0]), bytes([0, 255, 0, 84]),
                        bytes([0, 0, 255, 84]), bytes([255, 0, 0, 86])]


def test_breathe_and_reactive_sections() -> None:
    blob = LightingEffect("breathe", [(0, 255, 255)], duration_ms=4000).encode()
    assert blob[24] == 3
    assert [blob[28 + 4 * i:32 + 4 * i] for i in range(3)] == [
        bytes([0, 255, 255, 0]), bytes([0, 0, 0, 127]), bytes([0, 255, 255, 127])]

    blob = LightingEffect("reactive", [(0, 0, 255)], react_color=(255, 255, 255),
                          react_ms=800).encode()
    assert int.from_bytes(blob[0:4], "little") == 800
    assert blob[16] == 1  # hold
    assert blob[20:24] == b"\xff\xff\xff\xff"
    assert blob[24:28] == bytes([2, 255, 255, 255])
    assert blob[28:36] == bytes([255, 255, 255, 0, 0, 0, 255, 255])


def test_effect_validation_and_roundtrip() -> None:
    with pytest.raises(ValueError):
        LightingEffect("disco")
    with pytest.raises(ValueError):
        LightingEffect("breathe", [(1, 2, 3)] * 8)
    effect = LightingEffect("reactive", [(0, 0, 255)], react_ms=700)
    assert LightingEffect.from_dict(effect.as_dict()) == effect
    assert parse_color("#FF5500") == (255, 85, 0)
    assert parse_color("10,20,30") == (10, 20, 30)
    assert parse_color("red") == (255, 0, 0)
    with pytest.raises(ValueError):
        parse_color("nope")


# -- bindings ----------------------------------------------------------------


def test_binding_encoding_matches_hardware() -> None:
    # Verified: Shift+F13 arrives as keyboard report 01 02 00 68.
    assert parse_binding("key:shift+f13").encode() == bytes.fromhex("51e1680000")
    assert parse_binding("key:ctrl+alt+delete").encode() == bytes.fromhex("51e0e24c00")
    assert parse_binding("media:volume-up").encode() == bytes.fromhex("61e9000000")
    assert parse_binding("dpi").encode() == bytes.fromhex("3000000000")
    assert parse_binding("disabled").encode() == bytes(5)
    for bad in ("key:", "key:ctrl+alt+shift+super+x", "media:louder", "jump", "key:hyper+x"):
        with pytest.raises(ValueError):
            parse_binding(bad)


def test_mapping_payloads_default_and_custom() -> None:
    buttons, wheel = mapping_payloads({})
    assert buttons == bytes.fromhex("19" "0100000000" "0200000000" "0300000000"
                                    "0400000000" "0500000000" "3000000000" "0000000000")
    assert wheel == bytes.fromhex("1a" "3100000000" "3200000000")
    buttons, wheel = mapping_payloads({"back": "key:shift+f13", "wheel-up": "media:volume-up"})
    assert buttons[16:21] == bytes.fromhex("51e1680000")
    assert wheel[1:6] == bytes.fromhex("61e9000000")
    with pytest.raises(ValueError):
        mapping_payloads({"thumb": "left"})


# -- profile -----------------------------------------------------------------


def test_profile_roundtrip(tmp_path) -> None:
    profile = Profile(
        dpi_presets=[400, 1600], dpi_active=2, polling_hz=500,
        power=PowerSettings(sleep_s=600, sensor_rest=False),
        lighting=LightingEffect("breathe", [(0, 255, 255)]),
        bindings={"back": "key:ctrl+c", "left": "left"},
        debounce=(6, 20),
    )
    path = profile.save(tmp_path / "p.json")
    data = json.loads(path.read_text())
    assert "left" not in data["bindings"]  # defaults are not stored
    loaded = Profile.load(path)
    assert loaded.dpi_presets == [400, 1600] and loaded.dpi_active == 2
    assert loaded.power.sleep_s == 600 and loaded.power.sensor_rest is False
    assert loaded.lighting == profile.lighting
    assert loaded.bindings == {"back": "key:ctrl+c"}
    assert Profile.load(tmp_path / "missing.json").as_dict() == {}
    with pytest.raises(ValueError):
        Profile.from_dict({"polling_hz": 300})


def test_apply_profile_writes_and_persists() -> None:
    mouse = make_fake_mouse()
    profile = Profile(
        dpi_presets=[800], dpi_active=1, power=PowerSettings(sleep_s=600, sensor_rest=False),
        lighting=LightingEffect("steady", [(0, 0, 255)]), bindings={"back": "media:mute"},
    )
    applied = apply_profile(mouse, profile)
    assert applied == ["dpi", "power", "lighting", "bindings"]
    transport = mouse._transport  # noqa: SLF001
    assert isinstance(transport, FakeTransport)
    opcodes = [write[0] for write in transport.writes]
    assert 0x20 in opcodes and 0x2B in opcodes and 0x1C in opcodes and 0x19 in opcodes
    assert opcodes[-1] == 0x09  # saved to flash last
    power = next(w for w in transport.writes if w[0] == 0x2B)
    # Fake power mode is saving=0 movement=0 idle_light=600 idle_sleep=900.
    assert power[:7] == bytes([0x2B, 0, 0, 0x58, 0x02, 0x58, 0x02])
