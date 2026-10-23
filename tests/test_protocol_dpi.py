"""DPI, polling-rate, firmware and status protocol tests.

Layouts come from the decrypted ``rival-3-wireless.lisp`` descriptor; the CPI
conversion table comes from ``truemove-air.lisp``.
"""

from __future__ import annotations

import pytest

from ssmouse.fake import (
    CAPTURED_DONGLE_FIRMWARE_RESPONSE,
    CAPTURED_RF_STATUS_UNPAIRED_RESPONSE,
    FAKE_DPI_LEVEL_RESPONSE,
    FAKE_DPI_RESPONSE,
    FAKE_FIRMWARE_RESPONSE,
    FAKE_POLLING_RATE_RESPONSE,
    FAKE_POWER_MODE_RESPONSE,
    FAKE_RF_STATUS_RESPONSE,
)
from ssmouse.protocol import (
    DpiInfo,
    POLLING_RATE_HZ,
    PowerMode,
    Protocol,
    cpi_from_sensor_value,
    format_firmware,
)


# ---------------------------------------------------------------------------
# CPI table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sensor", "cpi"),
    [
        (0x00, 100),
        (0x04, 400),
        (0x06, 600),
        (0x10, 1400),
        (0x20, 2800),
        (0x30, 4100),
        (0x7F, 10100),  # missing from table -> vendor approximation
        (0xD6, 18000),
    ],
)
def test_cpi_table(sensor: int, cpi: int) -> None:
    assert cpi_from_sensor_value(sensor) == cpi


def test_cpi_out_of_range_is_invalid() -> None:
    assert cpi_from_sensor_value(0xD7) is None
    assert cpi_from_sensor_value(0xFFFF) is None


def test_cpi_approximation_formula() -> None:
    # Missing values use 100 * floor(sensor * 0.8).
    assert cpi_from_sensor_value(0x08) == 100 * int(0x08 * 0.8)


# ---------------------------------------------------------------------------
# DPI request/response
# ---------------------------------------------------------------------------


def test_dpi_request_uses_a0() -> None:
    request = Protocol().build_dpi_request()
    assert request[:1] == b"\xa0"
    assert len(request) == 64


def test_parse_dpi_response() -> None:
    info = Protocol().parse_dpi_response(FAKE_DPI_RESPONSE)
    assert isinstance(info, DpiInfo)
    assert info.level_max == 5
    assert info.active_level == 2
    assert [preset.level for preset in info.presets] == [1, 2, 3, 4, 5]
    assert [preset.sensor for preset in info.presets] == [0x04, 0x06, 0x10, 0x20, 0x30]
    assert [preset.cpi for preset in info.presets] == [400, 600, 1400, 2800, 4100]
    assert info.current_cpi == 600
    assert info.current_sensor == 0x06
    assert info.as_dict()["current_cpi"] == 600


@pytest.mark.parametrize(
    "response",
    [
        b"",
        bytes([0x00, 0x02]) + b"\x00" * 62,  # level_max 0
        bytes([0x05, 0x00]) + b"\x00" * 62,  # active_level 0
        bytes([0x06, 0x02]) + b"\x00" * 62,  # level_max out of range
        bytes([0x05, 0x02]) + b"\x00" * 62,  # resolution_1 == 0
        bytes([0x05, 0x02, 0x04, 0x00]),  # truncated
    ],
)
def test_parse_dpi_rejects_invalid_reports(response: bytes) -> None:
    assert Protocol().parse_dpi_response(response) is None


def test_invalid_sensor_yields_none_cpi() -> None:
    # level_max=2, active=1, resolution_1=0xE0 (> 0xD6), resolution_2=0x04
    response = bytes([0x02, 0x01, 0xE0, 0x00, 0x04, 0x00]) + b"\x00" * 58
    info = Protocol().parse_dpi_response(response)
    assert info is not None
    assert info.presets[0].cpi is None
    assert info.current_cpi is None


def test_active_level_above_level_max() -> None:
    # active_level=4 but level_max=2 -> current_cpi is None, still valid report.
    response = bytes([0x02, 0x04, 0x04, 0x00, 0x06, 0x00]) + b"\x00" * 58
    info = Protocol().parse_dpi_response(response)
    assert info is not None
    assert info.current_cpi is None


def test_dpi_level_response() -> None:
    protocol = Protocol()
    assert protocol.build_dpi_level_request()[:1] == b"\x96"
    assert protocol.parse_dpi_level_response(FAKE_DPI_LEVEL_RESPONSE) == 2
    assert protocol.parse_dpi_level_response(bytes([0x00])) is None
    assert protocol.parse_dpi_level_response(bytes([0x06])) is None


# ---------------------------------------------------------------------------
# Polling rate
# ---------------------------------------------------------------------------


def test_polling_rate_request_and_parse() -> None:
    protocol = Protocol()
    assert protocol.build_polling_rate_request()[:1] == b"\x97"
    assert protocol.parse_polling_rate_response(FAKE_POLLING_RATE_RESPONSE) == 250
    for level, hz in POLLING_RATE_HZ.items():
        assert protocol.parse_polling_rate_response(bytes([level])) == hz


def test_polling_rate_invalid() -> None:
    assert Protocol().parse_polling_rate_response(bytes([0x04])) is None
    assert Protocol().parse_polling_rate_response(b"") is None


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------


def test_firmware_request_and_parse() -> None:
    protocol = Protocol()
    # Descriptor dongle read D0 is primary; 0x90 (mouse read) is the fallback.
    assert protocol.build_firmware_request()[:1] == b"\xd0"
    assert [request[0] for request in protocol.build_firmware_requests()] == [0xD0, 0x90]
    assert protocol.parse_firmware_response(FAKE_FIRMWARE_RESPONSE) == (1, 4)
    assert format_firmware((1, 4)) == "1.04"
    assert format_firmware((0, 9)) == "0.09"
    assert format_firmware(None) is None
    assert protocol.parse_firmware_response(b"\x01") is None


# ---------------------------------------------------------------------------
# RF / connection status
# ---------------------------------------------------------------------------


def test_rf_status_parse() -> None:
    protocol = Protocol()
    # Descriptor dongle read E8 is primary; 0xA8 is the fallback.
    assert protocol.build_rf_status_request()[:1] == b"\xe8"
    assert [request[0] for request in protocol.build_rf_status_requests()] == [0xE8, 0xA8]
    assert protocol.parse_rf_status_response(FAKE_RF_STATUS_RESPONSE) is True
    assert protocol.parse_rf_status_response(bytes([0x00])) is False
    assert protocol.parse_rf_status_response(b"") is None


# ---------------------------------------------------------------------------
# Power mode
# ---------------------------------------------------------------------------


def test_power_mode_parse() -> None:
    protocol = Protocol()
    assert protocol.build_power_mode_request()[:1] == b"\xab"
    mode = protocol.parse_power_mode_response(FAKE_POWER_MODE_RESPONSE)
    assert isinstance(mode, PowerMode)
    assert mode.power_saving == 0
    assert mode.movement_saving == 0
    assert mode.idle_lighting == 600
    assert mode.idle_sleep == 900
    assert mode.as_dict()["idle_sleep"] == 900
    assert protocol.parse_power_mode_response(b"\x00\x00") is None


# ---------------------------------------------------------------------------
# Engine-capture correlation
# ---------------------------------------------------------------------------


def test_requests_match_engine_capture() -> None:
    """Primary/fallback requests match SteelSeriesEngine's observed reports.

    The engine traffic was captured with usbmon; it shows the read-op variants
    for firmware (0x90) and RF status (0xA8) while the descriptor declares the
    dongle variants (0xD0/0xE8).  Both are implemented, primary first.
    """
    from ssmouse.fake import ENGINE_OBSERVED_REQUESTS

    protocol = Protocol()
    primary_builders = {
        "battery": protocol.build_battery_request,
        "dpi": protocol.build_dpi_request,
        "dpi_level": protocol.build_dpi_level_request,
        "polling_rate": protocol.build_polling_rate_request,
        "power_mode": protocol.build_power_mode_request,
    }
    for name, builder in primary_builders.items():
        request = builder()
        observed = ENGINE_OBSERVED_REQUESTS[name]
        assert request.startswith(observed), name
        assert len(request) == 64, name
        assert request[len(observed):] == b"\x00" * (64 - len(observed)), name

    # Firmware: engine sent 0x90, which is our fallback.
    firmware_requests = protocol.build_firmware_requests()
    assert firmware_requests[0][0] == 0xD0
    assert firmware_requests[1].startswith(ENGINE_OBSERVED_REQUESTS["firmware"])

    # RF status: engine sent 0xA8, which is our fallback.
    rf_requests = protocol.build_rf_status_requests()
    assert rf_requests[0][0] == 0xE8
    assert rf_requests[1].startswith(ENGINE_OBSERVED_REQUESTS["rf_status"])


def test_dpi_parser_tolerates_report_id_prefix() -> None:
    """A leading 0x00 (report-ID-included shape) must also parse."""
    protocol = Protocol()
    prefixed = b"\x00" + FAKE_DPI_RESPONSE
    info = protocol.parse_dpi_response(prefixed)
    assert info is not None
    assert info.level_max == 5
    assert info.active_level == 2
    assert info.current_cpi == 600


def test_live_dongle_firmware_capture() -> None:
    """Live capture: D0 -> 01 04 -> firmware 1.04 (dongle read verified)."""
    assert Protocol().parse_firmware_response(
        CAPTURED_DONGLE_FIRMWARE_RESPONSE
    ) == (1, 4)
    assert format_firmware((1, 4)) == "1.04"


def test_live_unpaired_rf_status_capture() -> None:
    """Live capture: E8 -> 01 means the receiver is not connected (4 = up)."""
    protocol = Protocol()
    assert protocol.parse_rf_status_response(
        CAPTURED_RF_STATUS_UNPAIRED_RESPONSE
    ) is False


def test_firmware_parser_tolerates_report_id_prefix() -> None:
    assert Protocol().parse_firmware_response(b"\x00\x01\x04") == (1, 4)


def test_rf_status_tolerates_report_id_prefix() -> None:
    protocol = Protocol()
    assert protocol.parse_rf_status_response(b"\x00\x04") is True
    assert protocol.parse_rf_status_response(b"\x00\x00") is False


def test_dpi_level_and_polling_tolerate_report_id_prefix() -> None:
    protocol = Protocol()
    assert protocol.parse_dpi_level_response(b"\x00\x02") == 2
    assert protocol.parse_polling_rate_response(b"\x00\x02") == 250


def test_pairing_write_requests() -> None:
    """Pairing sequence from GG: 71 (pair_dongle) and 73 (reset_wireless)."""
    protocol = Protocol()
    pair = protocol.build_pair_dongle_request()
    reset = protocol.build_reset_wireless_request()
    assert pair[:2] == b"\x71\x00"
    assert reset[:1] == b"\x73"
    assert len(pair) == 64 and len(reset) == 64
