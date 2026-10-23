"""Device detection, interface ranking, command wiring and failover tests."""

from __future__ import annotations

import pytest

from ssmouse.device import (
    DeviceNotFoundError,
    Mouse,
    TransportTimeoutError,
    _rank_interface,
    enumerate_devices,
    rank_interfaces,
)
from ssmouse.fake import (
    CAPTURED_BATTERY_RESPONSE,
    FAKE_INTERFACE,
    FakeTransport,
)
from ssmouse.protocol import PowerMode
from ssmouse.transport import HidInterface, Transport, TransportError


def _interface(number: int, usage: int | None) -> HidInterface:
    return HidInterface(
        path="/dev/hidraw%d" % (number + 4),
        interface_number=number,
        usage_page=usage,
        vendor_id=0x1038,
        product_id=0x1830,
        product_string="SteelSeries Rival 3 Wireless",
    )


def test_rank_prefers_interface_3_usage_ffc0() -> None:
    interfaces = [
        _interface(0, 0x0001),
        _interface(1, 0x0001),
        _interface(2, 0xFF00),
        _interface(3, 0xFFC0),
    ]
    ranked = rank_interfaces(interfaces)
    assert [item.interface_number for item in ranked] == [3, 2]


def test_rank_accepts_usage_page_without_interface_number() -> None:
    interface = HidInterface(path="/dev/hidraw9", interface_number=None, usage_page=0xFFC0)
    assert _rank_interface(interface) == 1


def test_rank_rejects_non_vendor_interfaces() -> None:
    assert _rank_interface(_interface(0, 0x0001)) is None
    assert _rank_interface(_interface(1, None)) is None


def test_rank_deduplicates_same_path() -> None:
    first = HidInterface(path="/dev/hidraw5", interface_number=1, usage_page=0x0001)
    better = HidInterface(path="/dev/hidraw5", interface_number=1, usage_page=0xFFC0)
    ranked = rank_interfaces([first, better])
    assert len(ranked) == 1
    assert ranked[0].usage_page == 0xFFC0


def _fake_mouse() -> Mouse:
    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport()
    transport.open()
    mouse._transport = transport  # noqa: SLF001 - test wiring
    return mouse


def test_fake_mouse_reports_captured_battery() -> None:
    mouse = _fake_mouse()
    try:
        battery = mouse.get_battery()
        assert battery.level == 47
        assert battery.charging is None
    finally:
        mouse.close()


def test_fake_mouse_reports_full_info() -> None:
    mouse = _fake_mouse()
    try:
        info = mouse.get_info()
        assert info.name == "SteelSeries Rival 3 Wireless"
        assert info.model == "M-0000022"
        assert info.interface_number == 3
        assert info.usage_page == 0xFFC0
        assert info.backend == "fake"

        dpi = mouse.get_dpi()
        assert dpi is not None
        assert dpi.level_max == 5
        assert dpi.active_level == 2
        assert dpi.current_cpi == 600

        assert mouse.get_active_dpi_level() == 2
        assert mouse.get_polling_rate() == 250
        details = mouse.get_firmware_details()
        assert details is not None
        assert details["version"] == "1.04"
        assert details["source"] == "dongle"
        assert mouse.get_firmware() == "1.04"
        assert mouse.get_rf_status() is True
        assert mouse.is_connected() is True
        power_mode = mouse.get_power_mode()
        assert isinstance(power_mode, PowerMode)
        assert power_mode.idle_sleep == 900
    finally:
        mouse.close()


def test_firmware_fallback_to_mouse_variant() -> None:
    """When only the 0x90 (mouse) firmware read answers, source is "mouse"."""
    from ssmouse.fake import FAKE_FIRMWARE_RESPONSE

    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport(script=[(bytes([0x90]), FAKE_FIRMWARE_RESPONSE)])
    transport.open()
    mouse._transport = transport  # noqa: SLF001
    details = mouse.get_firmware_details()
    assert details is not None
    assert details["version"] == "1.04"
    assert details["source"] == "mouse"
    assert details["opcode"] == 0x90


def test_rf_status_fallback_to_a8() -> None:
    """When only the 0xA8 read answers, RF status still resolves."""
    from ssmouse.fake import FAKE_RF_STATUS_RESPONSE

    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport(script=[(bytes([0xA8]), FAKE_RF_STATUS_RESPONSE)])
    transport.open()
    mouse._transport = transport  # noqa: SLF001
    assert mouse.get_rf_status() is True


def test_fake_transport_records_writes() -> None:
    mouse = _fake_mouse()
    mouse.get_battery()
    transport = mouse._transport  # noqa: SLF001
    assert isinstance(transport, FakeTransport)
    assert len(transport.writes) == 1
    assert transport.writes[0][:2] == b"\xaa\x01"
    assert len(transport.writes[0]) == 64


def test_commands_use_expected_opcodes() -> None:
    mouse = _fake_mouse()
    mouse.get_battery()
    mouse.get_dpi()
    mouse.get_polling_rate()
    mouse.get_firmware()
    mouse.get_rf_status()
    mouse.get_power_mode()
    transport = mouse._transport  # noqa: SLF001
    opcodes = [write[0] for write in transport.writes]
    # Firmware D0 and RF E8 are the descriptor-declared dongle reads.
    assert opcodes == [0xAA, 0xA0, 0x97, 0xD0, 0xE8, 0xAB]


def test_no_interfaces_raises() -> None:
    with pytest.raises(DeviceNotFoundError):
        Mouse([])


def test_timeout_raises_transport_timeout() -> None:
    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport(script=[])  # no responses at all
    transport.open()
    mouse._transport = transport  # noqa: SLF001
    with pytest.raises(TransportTimeoutError):
        mouse.get_battery()


def test_optional_reads_return_none_on_timeout() -> None:
    """DPI/polling/firmware degrade to None instead of raising."""
    mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
    transport = FakeTransport(script=[])
    transport.open()
    mouse._transport = transport  # noqa: SLF001
    assert mouse.get_dpi() is None
    assert mouse.get_polling_rate() is None
    assert mouse.get_firmware() is None
    assert mouse.get_rf_status() is None
    assert mouse.get_power_mode() is None


class _FailingTransport(Transport):
    """Simulates the real EPIPE failure on interface 2 / usage page 0xFF00."""

    name = "failing"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write_output(self, report: bytes) -> int:
        raise TransportError("write failed: [Errno 32] Broken pipe")

    def read_input(self, size: int, timeout_ms: int):
        return None

    def get_feature(self, report_id: int, size: int):
        return None

    def set_feature(self, data: bytes) -> None:
        pass


def test_failover_to_next_interface(monkeypatch) -> None:
    primary = _interface(2, 0xFF00)
    fallback = _interface(3, 0xFFC0)
    mouse = Mouse([primary, fallback], connect=False, retries=2)
    fallback_transport = FakeTransport(script=[(b"\xaa\x01", CAPTURED_BATTERY_RESPONSE)])
    fallback_transport.open()

    def fake_make_transport(self, interface):  # noqa: ANN001
        if interface is primary:
            return _FailingTransport()
        return fallback_transport

    monkeypatch.setattr(Mouse, "_make_transport", fake_make_transport)
    battery = mouse.get_battery()
    assert battery.level == 47
    assert mouse.describe_interface().interface_number == 3


def test_detect_fake_env(monkeypatch) -> None:
    monkeypatch.setenv("SSMOUSE_FAKE", "1")
    mouse = Mouse.detect()
    try:
        assert mouse.get_battery().level == 47
        assert mouse.get_info().backend == "fake"
    finally:
        mouse.close()


def test_enumerate_devices_returns_vendor_interfaces(monkeypatch) -> None:
    interfaces = [_interface(2, 0xFF00), _interface(3, 0xFFC0)]
    monkeypatch.setattr(
        "ssmouse.device._enumerate_hidapi", lambda pids, prefer_backend=None: interfaces
    )
    assert enumerate_devices() == interfaces
