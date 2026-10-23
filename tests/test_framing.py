"""Report framing tests.

The verified Rival 3 Wireless framing is a 64-byte report starting with the
opcode: the Linux kernel maps the first buffer byte to the HID report
``wValue``.  The descriptor-canonical form is 65 bytes with a leading
``report_id`` byte of 0x00; both are supported.
"""

from __future__ import annotations

import pytest

from ssmouse.fake import FAKE_INTERFACE, FakeTransport
from ssmouse.protocol import (
    DEFAULT_FRAMING,
    FRAMING_PRESETS,
    Framing,
    Protocol,
    ProtocolError,
)


def test_default_framing_is_padded_64_byte_report_without_id() -> None:
    encoded = DEFAULT_FRAMING.encode(b"\xaa\x01")
    assert len(encoded) == 64
    assert encoded[:2] == b"\xaa\x01"
    assert encoded[2:] == b"\x00" * 62
    assert DEFAULT_FRAMING.report_id is None
    assert DEFAULT_FRAMING.report_size == 64


def test_short_3_byte_framing() -> None:
    encoded = FRAMING_PRESETS["short-3"].encode(b"\xaa\x01")
    assert encoded == b"\xaa\x01\x00"


def test_canonical_65_byte_framing_has_report_id_zero() -> None:
    encoded = FRAMING_PRESETS["canonical"].encode(b"\xaa\x01")
    assert len(encoded) == 65
    assert encoded[0] == 0x00
    assert encoded[1:3] == b"\xaa\x01"
    assert encoded[3:] == b"\x00" * 62


def test_report_id_zero_preset_matches_canonical() -> None:
    assert FRAMING_PRESETS["report-id-0"] == FRAMING_PRESETS["canonical"]


def test_encode_rejects_oversized_payload() -> None:
    framing = Framing(report_id=None, report_size=4, pad=True)
    with pytest.raises(ProtocolError):
        framing.encode(b"\xaa\x01\x00\x00\x00")


def test_invalid_report_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        Framing(report_id=0x100)


def test_decode_strips_report_id() -> None:
    framing = Framing(report_id=0x00, report_size=65, pad=True)
    response = bytes([0x00, 0x2F, 0x00, 0x00]) + b"\x00" * 61
    assert framing.decode(response)[:3] == bytes([0x2F, 0x00, 0x00])


def test_decode_leaves_unexpected_first_byte_alone() -> None:
    framing = Framing(report_id=0x00, report_size=65, pad=True)
    assert framing.decode(b"\x2f\x00\x00") == b"\x2f\x00\x00"


def test_describe_is_stable() -> None:
    assert DEFAULT_FRAMING.describe() == "report_id=none size=64 pad=True"
    assert (
        FRAMING_PRESETS["canonical"].describe()
        == "report_id=0x00 size=65 pad=True"
    )


def test_hidapi_libusb_backend_prepends_report_id() -> None:
    """The libusb hidapi backend always expects a leading report-ID byte."""
    try:
        from ssmouse.transport import HidApiTransport, _hidapi_hid
    except Exception:  # noqa: BLE001
        pytest.skip("hidapi libusb backend not installed")
    if _hidapi_hid is None:
        pytest.skip("hidapi libusb backend not installed")
    transport = HidApiTransport(FAKE_INTERFACE, DEFAULT_FRAMING, backend="hid")
    wire = transport._wire_bytes(b"\xaa\x01")  # noqa: SLF001 - deliberate
    assert wire[:3] == b"\x00\xaa\x01"

    canonical = HidApiTransport(
        FAKE_INTERFACE, FRAMING_PRESETS["canonical"], backend="hid"
    )
    assert canonical._wire_bytes(b"\x00\xaa\x01")[0] == 0x00  # noqa: SLF001


def test_fake_transport_replays_canonical_framing() -> None:
    """Canonical framing is applied to the wire and parsed back correctly."""
    framing = FRAMING_PRESETS["canonical"]
    protocol = Protocol(framing=framing)
    response = bytes([0x00, 0x2F, 0x00, 0x00]) + b"\x00" * 61
    transport = FakeTransport(
        framing=framing, script=[(bytes([0x00, 0xAA, 0x01]), response)]
    )
    transport.open()
    request = protocol.build_battery_request()
    assert len(request) == 65
    assert transport.write_output(request) == 65
    raw = transport.read_input(65, 100)
    assert raw is not None
    status = protocol.parse_battery_response(raw)
    assert status.level == 47
    assert status.charging is None
