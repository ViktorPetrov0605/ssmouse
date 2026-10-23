"""Transport-level tests: descriptor parsing, backend selection, factories."""

from __future__ import annotations

import sys

import pytest

from ssmouse.device import _usage_page_from_descriptor
from ssmouse.transport import (
    HidInterface,
    TransportError,
    available_backends,
    default_backend,
    make_hidapi_transport,
)

# Real report descriptors from the Rival 3 Wireless on the development machine.
DESCRIPTOR_INTERFACE_2 = bytes.fromhex(
    "0600ff0901a1010601ff150026ff00750809f09540810209f195409102c0"
)
DESCRIPTOR_INTERFACE_3 = bytes.fromhex(
    "06c0ff0901a10106c1ff150026ff00750809f09540810209f19540910209f2"
    "960202b102c0"
)


def test_usage_page_from_real_descriptors() -> None:
    assert _usage_page_from_descriptor(DESCRIPTOR_INTERFACE_2) == 0xFF00
    assert _usage_page_from_descriptor(DESCRIPTOR_INTERFACE_3) == 0xFFC0


def test_usage_page_from_empty_descriptor() -> None:
    assert _usage_page_from_descriptor(b"") is None
    assert _usage_page_from_descriptor(b"\x05") is None  # truncated Usage Page


def test_usage_page_from_short_form() -> None:
    # 0x05 = Usage Page (1-byte data), value 0x01 -> generic desktop.
    assert _usage_page_from_descriptor(bytes.fromhex("05010902")) == 0x01


def test_available_backends_contains_linux_choices() -> None:
    backends = available_backends()
    assert backends, "at least one backend must be available"
    if sys.platform.startswith("linux"):
        assert "ctypes-hidraw" in backends


def test_default_backend_is_available() -> None:
    assert default_backend() in available_backends()


def test_make_transport_rejects_unknown_backend() -> None:
    interface = HidInterface(path="/dev/null")
    with pytest.raises(TransportError):
        make_hidapi_transport(interface, framing=None, backend="does-not-exist")  # type: ignore[arg-type]


def test_make_transport_returns_expected_backend() -> None:
    interface = HidInterface(
        path="/dev/hidraw7",
        interface_number=3,
        usage_page=0xFFC0,
        vendor_id=0x1038,
        product_id=0x1830,
    )
    if "hidapi-hidraw" not in available_backends():
        pytest.skip("hidapi hidraw backend unavailable")
    transport = make_hidapi_transport(interface, framing=None)  # type: ignore[arg-type]
    assert transport.name == "hidapi-hidraw"
    assert transport.interface.interface_number == 3


def test_interface_as_dict_hex_formats() -> None:
    interface = HidInterface(
        path=b"/dev/hidraw7",
        interface_number=3,
        usage_page=0xFFC0,
        usage=1,
        vendor_id=0x1038,
        product_id=0x1830,
        release_number=0x0104,
    )
    payload = interface.as_dict()
    assert payload["path"] == "/dev/hidraw7"
    assert payload["usage_page"] == "0xFFC0"
    assert payload["usage"] == "0x0001"
    assert payload["vendor_id"] == "1038"
    assert payload["release_number"] == "0104"


def _control_node() -> str | None:
    """Find the current /dev/hidraw node with usage page 0xFFC0.

    Node numbers are not stable across re-enumeration (on the test machine
    hidraw6/hidraw7 swapped when the receiver was replugged), so tests must
    discover the node instead of hard-coding it.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import hidraw  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        for record in hidraw.enumerate(0x1038, 0x1830):
            if record.get("usage_page") == 0xFFC0:
                path = record["path"]
                return path.decode() if isinstance(path, bytes) else path
    except Exception:  # noqa: BLE001
        return None
    return None


CONTROL_NODE = _control_node()


@pytest.mark.skipif(CONTROL_NODE is None, reason="needs the Linux hidraw control node")
def test_ctypes_transport_roundtrip() -> None:
    """The pure-ctypes fallback can open and write the real control interface."""
    from ssmouse.transport import RawHidrawTransport

    transport = RawHidrawTransport(str(CONTROL_NODE))
    transport.open()
    try:
        assert transport.interface.path_str == CONTROL_NODE
        assert isinstance(transport.raw_info(), dict)
        descriptor = transport.report_descriptor()
        assert descriptor[:2] == bytes.fromhex("06c0")
        written = transport.write_output(bytes([0xAA, 0x01]) + b"\x00" * 62)
        assert written == 64
        # The mouse may be asleep; a timeout (None) is acceptable here.
        transport.read_input(64, 100)
    finally:
        transport.close()
