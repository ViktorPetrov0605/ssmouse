"""Battery protocol tests using the captured hardware bytes.

Verified layout: ``data[0:2]`` = uint16 LE level percent, ``data[2]`` =
charge status.  The Rival 3 Wireless runs on AA batteries and its descriptor
declares the charge byte as a constant 0x00, so ``charging`` is ``None``.
"""

from __future__ import annotations

import pytest

from ssmouse.fake import CAPTURED_BATTERY_REQUEST, CAPTURED_BATTERY_RESPONSE
from ssmouse.protocol import BatteryStatus, Framing, Protocol, ProtocolError


def test_battery_request_matches_capture() -> None:
    """The generated request must be byte-identical to the hardware capture."""
    protocol = Protocol()
    request = protocol.build_battery_request()
    assert request == CAPTURED_BATTERY_REQUEST
    assert len(request) == 64
    assert request[:2] == b"\xaa\x01"
    assert request[2:] == b"\x00" * 62


def test_parse_captured_battery_response() -> None:
    """Real capture: uint16 LE 0x002F = 47 %; charging is not reported."""
    protocol = Protocol()
    status = protocol.parse_battery_response(CAPTURED_BATTERY_RESPONSE)
    assert isinstance(status, BatteryStatus)
    assert status.level == 47
    assert status.charging is None
    assert status.as_dict() == {"level": 47, "charging": None}
    assert status.raw[:3] == b"\x2f\x00\x00"


def test_charging_can_be_enabled_for_other_models() -> None:
    """Charging is opt-in: only devices whose descriptor reports it use byte 2."""
    protocol = Protocol(charging_supported=True, battery_charging_byte=2)
    status = protocol.parse_battery_response(bytes([50, 0x00, 0x01]) + b"\x00" * 61)
    assert status.level == 50
    assert status.charging is True
    status = protocol.parse_battery_response(bytes([50, 0x00, 0x00]) + b"\x00" * 61)
    assert status.charging is False


def test_level_is_little_endian_uint16() -> None:
    protocol = Protocol()
    status = protocol.parse_battery_response(bytes([0x64, 0x00, 0x00]) + b"\x00" * 61)
    assert status.level == 100
    status = protocol.parse_battery_response(bytes([0x2F, 0x00, 0x00]) + b"\x00" * 61)
    assert status.level == 47


def test_short_response_is_rejected() -> None:
    protocol = Protocol()
    with pytest.raises(ProtocolError):
        protocol.parse_battery_response(b"")
    with pytest.raises(ProtocolError):
        protocol.parse_battery_response(b"\x2f")


def test_unsupported_response_is_rejected() -> None:
    """0xFFFF (unsupported command echo) is not a valid battery level."""
    protocol = Protocol()
    with pytest.raises(ProtocolError):
        protocol.parse_battery_response(bytes([0xFF, 0xFF]) + b"\x00" * 62)


def test_report_id_framing_is_stripped_before_parsing() -> None:
    framing = Framing(report_id=0x00, report_size=65, pad=True)
    protocol = Protocol(framing=framing)
    response = bytes([0x00, 0x2F, 0x00, 0x00]) + b"\x00" * 61
    status = protocol.parse_battery_response(response)
    assert status.level == 47
    assert status.charging is None
