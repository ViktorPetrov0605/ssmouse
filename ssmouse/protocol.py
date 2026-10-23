"""SteelSeries Rival 3 Wireless HID protocol.

The protocol below was recovered from SteelSeries' own decrypted ``.edevice``
Lisp descriptors (``rival-3-wireless.lisp``, ``truemove-air.lisp``) and
verified against the hardware where noted.  This module contains only packet
builders and response parsers; :mod:`ssmouse.transport` moves the bytes.

Report framing
--------------
The descriptor declares a 65-byte report buffer whose byte 0 is a constant
``report_id = 0x00`` (``io-report-size`` is 65)::

    [0x00, op_code, ...payload..., pad to 65]

On Linux, the kernel maps the first buffer byte to the report ``wValue``, so a
64-byte buffer that starts with the opcode also works:

    [op_code, ...payload..., pad to 64]        # verified on hardware

Both are supported: :data:`DEFAULT_FRAMING` is the verified no-report-ID
64-byte form, and ``FRAMING_PRESETS["canonical"]`` produces the 65-byte
descriptor form.

Input reports are unnumbered 64-byte interrupt-IN transfers; on Linux they
arrive *without* a report-ID prefix, so ``data[0]`` is the first struct field
after the descriptor's ``report_id`` field.

Read commands
-------------
=========================  ==========================  ====================
Purpose                    Request                     Response
=========================  ==========================  ====================
Battery / charge status    ``AA 01``                   uint16 LE level %, byte 2 charge status (constant 0)
DPI presets (batch)        ``A0`` (read-op 0x20)       level_max, active_level, 5x uint16 LE sensor
Active DPI level           ``96`` (read-op 0x16)       active level 1..5
Polling rate               ``97`` (read-op 0x17)       level 0..3 => 1000/500/250/125 Hz
Firmware version           ``D0`` (dongle read 0x10)   fw_major, fw_minor
                           ``90`` fallback (read-op 0x10, mouse FW)
RF / connection status     ``E8`` (dongle read 0xA8)   byte 0 == 4 => connected
                           ``A8`` fallback (read-op 0xA8)
Power mode                 ``AB`` (read-op 0x2B)       power_saving, movement_saving, uint16 idle_lighting, uint16 idle_sleep
Debounce                   ``9E`` (read-op 0x1E)       make_ms, break_ms
=========================  ==========================  ====================

Write commands (no response; verify by reading back)
-----------------------------------------------------
=========================  ===========================================
Polling rate               ``17 level`` (0..3 = 1000/500/250/125 Hz)
DPI presets (batch)        ``20 count active s1 s2 s3 s4 s5`` (uint16 LE sensor values)
Power mode                 ``2B saving movement u16 idle_lighting u16 idle_sleep_s``
Debounce                   ``1E make break``
Live LED colour            ``13 01 r g b`` (until ``18`` restart-lighting)
Lighting effect            ``03`` flash buffer writes, ``05`` push, ``1C`` element info
Button / wheel mapping     ``19`` 7 x (function, 4 key bytes), ``1A`` 2 x (...)
Save to flash              ``09``
Factory reset              ``1B``
=========================  ===========================================

Timing rules from the vendor code: keep at least ~30 ms between commands, use
a 300-800 ms read timeout and 2 retries.

CPI conversion
--------------
The DPI struct stores *sensor values*, not CPI.  ``truemove-air.lisp`` maps
them with an explicit table (0x00 -> 100, 0x02 -> 200, ... 0xD6 -> 18000).  For
values missing from the table the vendor approximation is
``100 * floor(sensor * 0.8)``; out-of-range values are invalid.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

#: SteelSeries USB vendor ID.
VENDOR_ID = 0x1038

#: Rival 3 Wireless (wired / dongle presentation).
PID_RIVAL3_WIRELESS = 0x1830

#: Related wireless family PIDs kept for future auto-detection.
RELATED_PIDS = (0x1832, 0x1834, 0x1836)

#: All product IDs the tool will consider.
KNOWN_PIDS = (PID_RIVAL3_WIRELESS,) + RELATED_PIDS

#: Human-readable model name per PID.
PRODUCT_NAMES = {
    PID_RIVAL3_WIRELESS: "SteelSeries Rival 3 Wireless",
    0x1832: "SteelSeries wireless mouse (0x1832)",
    0x1834: "SteelSeries wireless mouse (0x1834)",
    0x1836: "SteelSeries wireless mouse (0x1836)",
}

#: SteelSeries model numbers where known.
MODEL_NUMBERS = {
    PID_RIVAL3_WIRELESS: "M-0000022",
}

#: Size of a HID input/output report (64 data bytes, unnumbered).
REPORT_SIZE = 64

#: ``io-report-size`` declared by the descriptor (report ID + 64 data bytes).
IO_REPORT_SIZE = 65

#: Usage pages of the SteelSeries vendor interfaces.
USAGE_PAGE_CONTROL_0 = 0xFFC0  # interface 3 on the Rival 3 Wireless
USAGE_PAGE_CONTROL_1 = 0xFF00  # interface 2 on the Rival 3 Wireless

#: Vendor usage pages are all in 0xFF00..0xFFFF.
VENDOR_USAGE_PAGE_MIN = 0xFF00

#: Minimum spacing between two commands, from the vendor descriptors.
MIN_COMMAND_INTERVAL_S = 0.030

#: Default per-command read timeout (vendor uses 300-800 ms).
DEFAULT_TIMEOUT_MS = 400

#: Default number of retries per command.
DEFAULT_RETRIES = 2


class ProtocolError(Exception):
    """Raised when a response cannot be interpreted."""


# ---------------------------------------------------------------------------
# Report framing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Framing:
    """How a logical command is placed into a HID report.

    Parameters
    ----------
    report_id:
        ``None`` for devices without numbered reports (verified Rival 3
        Wireless behaviour on Linux).  Otherwise the report ID byte placed in
        front of the payload.
    report_size:
        Total report size in bytes (including the report-ID byte, if any).
        ``0`` means "do not pad".
    pad:
        Pad the encoded report with zero bytes to ``report_size``.
    """

    report_id: Optional[int] = None
    report_size: int = REPORT_SIZE
    pad: bool = True

    def __post_init__(self) -> None:
        if self.report_id is not None and not 0 <= self.report_id <= 0xFF:
            raise ValueError("report_id must fit in one byte")
        if self.report_size < 0:
            raise ValueError("report_size must be >= 0")

    # -- encoding ---------------------------------------------------------
    def encode(self, payload: bytes | bytearray) -> bytes:
        """Encode *payload* into the report data handed to the transport."""
        data = bytes(payload)
        if self.report_id is not None:
            data = bytes([self.report_id]) + data
        if not self.pad:
            return data
        size = self.report_size or len(data)
        if len(data) > size:
            raise ProtocolError(
                "command is %d bytes but report_size is %d" % (len(data), size)
            )
        return data + b"\x00" * (size - len(data))

    # -- decoding ---------------------------------------------------------
    def decode(self, data: bytes | bytearray) -> bytes:
        """Strip a report-ID byte from an input report when one is used."""
        data = bytes(data)
        if self.report_id is not None and data and data[0] == self.report_id:
            return data[1:]
        return data

    def describe(self) -> str:
        rid = "none" if self.report_id is None else "0x%02X" % self.report_id
        return "report_id=%s size=%s pad=%s" % (rid, self.report_size, self.pad)


#: Framing verified on the Rival 3 Wireless (no report ID, 64-byte report).
DEFAULT_FRAMING = Framing(report_id=None, report_size=REPORT_SIZE, pad=True)

#: Framings that can be selected with ``--framing`` / ``SSMOUSE_FRAMING``.
FRAMING_PRESETS = {
    #: Verified on the Rival 3 Wireless: opcode first, 64 bytes padded.
    "default": DEFAULT_FRAMING,
    #: Explicit alias for the verified framing.
    "no-id": Framing(report_id=None, report_size=REPORT_SIZE, pad=True),
    #: Descriptor-canonical 65-byte frame: [0x00, opcode, ...].
    "canonical": Framing(report_id=0x00, report_size=IO_REPORT_SIZE, pad=True),
    #: Legacy three-byte variant (opcode, format byte, pad 1), no report ID.
    "short-3": Framing(report_id=None, report_size=3, pad=True),
    #: Report ID 0 in front of a 65-byte report (same bytes as canonical here).
    "report-id-0": Framing(report_id=0x00, report_size=IO_REPORT_SIZE, pad=True),
}


# ---------------------------------------------------------------------------
# CPI conversion (truemove-air sensor)
# ---------------------------------------------------------------------------

#: Sensor value -> CPI table extracted verbatim from ``truemove-air.lisp``.
_TRUEMOVE_AIR_TABLE = {
    0x00: 100, 0x02: 200, 0x03: 300, 0x04: 400, 0x05: 500, 0x06: 600,
    0x07: 700, 0x09: 800, 0x0A: 900, 0x0B: 1000, 0x0C: 1100, 0x0D: 1200,
    0x0E: 1300, 0x10: 1400, 0x11: 1500, 0x12: 1600, 0x13: 1700, 0x14: 1800,
    0x16: 1900, 0x17: 2000, 0x18: 2100, 0x19: 2200, 0x1A: 2300, 0x1B: 2400,
    0x1D: 2500, 0x1E: 2600, 0x1F: 2700, 0x20: 2800, 0x21: 2900, 0x23: 3000,
    0x25: 3100, 0x26: 3200, 0x27: 3300, 0x28: 3400, 0x29: 3500, 0x2A: 3600,
    0x2C: 3700, 0x2D: 3800, 0x2E: 3900, 0x2F: 4000, 0x30: 4100, 0x32: 4200,
    0x33: 4300, 0x34: 4400, 0x35: 4500, 0x36: 4600, 0x38: 4700, 0x39: 4800,
    0x3A: 4900, 0x3B: 5000, 0x3C: 5100, 0x3E: 5200, 0x3F: 5300, 0x40: 5400,
    0x41: 5500, 0x42: 5600, 0x44: 5700, 0x45: 5800, 0x46: 5900, 0x47: 6000,
    0x48: 6100, 0x4A: 6200, 0x4B: 6300, 0x4C: 6400, 0x4D: 6500, 0x4E: 6600,
    0x50: 6700, 0x51: 6800, 0x52: 6900, 0x53: 7000, 0x54: 7100, 0x56: 7200,
    0x57: 7300, 0x58: 7400, 0x59: 7500, 0x5A: 7600, 0x5C: 7700, 0x5D: 7800,
    0x5E: 7900, 0x5F: 8000, 0x60: 8100, 0x62: 8200, 0x63: 8300, 0x64: 8400,
    0x65: 8500, 0x66: 8600, 0x68: 8700, 0x69: 8800, 0x6A: 8900, 0x6B: 9000,
    0x6C: 9100, 0x6E: 9200, 0x6F: 9300, 0x70: 9400, 0x71: 9500, 0x72: 9600,
    0x74: 9700, 0x75: 9800, 0x76: 9900, 0x77: 10000, 0x78: 10100, 0x7A: 10200,
    0x7B: 10300, 0x7C: 10400, 0x7D: 10500, 0x7E: 10600, 0x80: 10700, 0x81: 10800,
    0x82: 10900, 0x83: 11000, 0x84: 11100, 0x86: 11200, 0x87: 11300, 0x88: 11400,
    0x89: 11500, 0x8A: 11600, 0x8C: 11700, 0x8D: 11800, 0x8E: 11900, 0x8F: 12000,
    0x90: 12100, 0x92: 12200, 0x93: 12300, 0x94: 12400, 0x95: 12500, 0x96: 12600,
    0x98: 12700, 0x99: 12800, 0x9A: 12900, 0x9B: 13000, 0x9C: 13100, 0x9E: 13200,
    0x9F: 13300, 0xA0: 13400, 0xA1: 13500, 0xA2: 13600, 0xA4: 13700, 0xA5: 13800,
    0xA6: 13900, 0xA7: 14000, 0xA8: 14100, 0xAA: 14200, 0xAB: 14300, 0xAC: 14400,
    0xAD: 14500, 0xAE: 14600, 0xB0: 14700, 0xB1: 14800, 0xB2: 14900, 0xB3: 15000,
    0xB4: 15100, 0xB5: 15200, 0xB6: 15300, 0xB7: 15400, 0xB8: 15500, 0xB9: 15600,
    0xBA: 15700, 0xBB: 15800, 0xBC: 15900, 0xBD: 16000, 0xBF: 16100, 0xC0: 16200,
    0xC2: 16300, 0xC3: 16400, 0xC4: 16500, 0xC5: 16600, 0xC6: 16700, 0xC7: 16800,
    0xC9: 16900, 0xCA: 17000, 0xCB: 17100, 0xCC: 17200, 0xCD: 17300, 0xCF: 17400,
    0xD0: 17500, 0xD1: 17600, 0xD2: 17700, 0xD3: 17800, 0xD5: 17900, 0xD6: 18000,
}

TRUEMOVE_AIR_MIN_SENSOR = 0x00
TRUEMOVE_AIR_MAX_SENSOR = 0xD6


def sensor_value_from_cpi(cpi: int) -> int:
    """Inverse of :func:`cpi_from_sensor_value`: the table entry nearest *cpi*."""
    if cpi <= 0:
        raise ValueError("CPI must be positive")
    return min(_TRUEMOVE_AIR_TABLE, key=lambda value: (abs(_TRUEMOVE_AIR_TABLE[value] - cpi), value))


#: CPI values the sensor supports (100..18000 in steps of 100).
SUPPORTED_CPI = tuple(sorted(_TRUEMOVE_AIR_TABLE.values()))


def cpi_from_sensor_value(sensor_value: int) -> Optional[int]:
    """Convert a truemove-air sensor value to CPI (vendor table + fallback).

    Returns ``None`` for values outside 0x00..0xD6 (invalid per the descriptor).
    """
    if not TRUEMOVE_AIR_MIN_SENSOR <= sensor_value <= TRUEMOVE_AIR_MAX_SENSOR:
        return None
    known = _TRUEMOVE_AIR_TABLE.get(sensor_value)
    if known is not None:
        return known
    # Vendor approximation for values missing from the table.
    return 100 * int(sensor_value * 0.8)


# ---------------------------------------------------------------------------
# Protocol data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatteryStatus:
    """Battery state reported by the device.

    ``charging`` is ``None`` for devices that do not report charge status:
    the Rival 3 Wireless runs on AA batteries and its descriptor declares the
    charge-status byte as a constant 0x00, so it must not be guessed.
    """

    level: int
    charging: Optional[bool] = None
    raw: bytes = field(default=b"", repr=False)

    def as_dict(self) -> dict:
        return {"level": self.level, "charging": self.charging}


@dataclass(frozen=True)
class DpiPreset:
    """One DPI level: 1-based *level*, raw *sensor* value and derived *cpi*."""

    level: int
    sensor: int
    cpi: Optional[int]

    def as_dict(self) -> dict:
        return {"level": self.level, "sensor": self.sensor, "cpi": self.cpi}


@dataclass(frozen=True)
class DpiInfo:
    """DPI configuration read with command ``A0`` (batch resolution)."""

    level_max: int
    active_level: int
    presets: tuple[DpiPreset, ...]
    raw: bytes = field(default=b"", repr=False)

    @property
    def current_cpi(self) -> Optional[int]:
        for preset in self.presets:
            if preset.level == self.active_level:
                return preset.cpi
        return None

    @property
    def current_sensor(self) -> Optional[int]:
        for preset in self.presets:
            if preset.level == self.active_level:
                return preset.sensor
        return None

    def as_dict(self) -> dict:
        return {
            "level_max": self.level_max,
            "active_level": self.active_level,
            "presets": [preset.as_dict() for preset in self.presets],
            "current_cpi": self.current_cpi,
            "current_sensor": self.current_sensor,
        }


@dataclass(frozen=True)
class PowerMode:
    """Power-saving configuration read with command ``AB``."""

    power_saving: int
    movement_saving: int
    idle_lighting: int
    idle_sleep: int

    def as_dict(self) -> dict:
        return {
            "power_saving": self.power_saving,
            "movement_saving": self.movement_saving,
            "idle_lighting": self.idle_lighting,
            "idle_sleep": self.idle_sleep,
        }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

#: Polling-rate level -> Hz, from the vendor descriptor.
POLLING_RATE_HZ = {0: 1000, 1: 500, 2: 250, 3: 125}
POLLING_RATE_LEVEL = {hz: level for level, hz in POLLING_RATE_HZ.items()}

#: Number of DPI presets the mouse stores.
MAX_DPI_LEVELS = 5
#: Sleep-timer choices offered by SteelSeries GG (seconds; 1..20 minutes).
SLEEP_TIMER_CHOICES_S = tuple(60 * minutes for minutes in range(1, 21))


class Protocol:
    """Packet builders and response parsers for the Rival 3 Wireless."""

    # -- opcodes -----------------------------------------------------------
    #: Battery / charge status: [0xAA, response_format 0x01].
    CMD_BATTERY = (0xAA, 0x01)
    #: DPI presets: read-op(batch_cpi_op 0x20) = 0xA0.
    CMD_DPI_READ = (0xA0,)
    #: Active DPI level: read-op(cpi_level_op 0x16) = 0x96.
    CMD_DPI_LEVEL = (0x96,)
    #: Polling rate: read-op(polling_rate_op 0x17) = 0x97.
    CMD_POLLING_RATE = (0x97,)
    #: Firmware: dongle-read-op(firmware_version_op 0x10) = 0xC0 | 0x10 = 0xD0.
    #: ``0x90`` (read-op 0x10, the "mouse firmware" variant) is kept as a
    #: fallback: it is not declared by the v1 0x1830 descriptor but is what
    #: rivalcfg and the observed engine traffic send.
    CMD_FIRMWARE = (0xD0,)
    CMD_FIRMWARE_FALLBACKS = ((0xD0,), (0x90,))
    #: RF / connection status: dongle-read-op(connection_status 0xA8) = 0xE8.
    #: ``0xA8`` (read-op 0xA8) is kept as a fallback (also observed in engine
    #: usbmon traffic even though the descriptor declares the dongle variant).
    CMD_RF_STATUS = (0xE8,)
    CMD_RF_STATUS_FALLBACKS = ((0xE8,), (0xA8,))
    #: Power mode: read-op(power_mode_op 0x2B) = 0xAB.
    CMD_POWER_MODE = (0xAB,)
    #: Pairing (write): dongle-write-op(pairing_op 0x31) = 0x40 | 0x31 = 0x71,
    #: followed by the struct's constant dummy byte 0x00.  Used by
    #: ``Mouse.pair`` and ``tools/probe.py --pair``.
    CMD_PAIR_DONGLE = (0x71, 0x00)
    #: Wireless reset (write): dongle-write-op(wireless_reset 0x33) = 0x73.
    CMD_RESET_WIRELESS = (0x73,)
    #: Debounce: read-op(button_debounce_op 0x1E) = 0x9E.
    CMD_DEBOUNCE_READ = (0x9E,)

    # Write opcodes (write-op = opcode itself; the mouse sends no response).
    OP_POLLING_RATE = 0x17
    OP_BATCH_CPI = 0x20
    OP_POWER_MODE = 0x2B
    OP_DEBOUNCE = 0x1E
    OP_LED_DIRECT = 0x13
    OP_RESTART_LIGHTING = 0x18
    OP_BUTTON_MAPPING = 0x19
    OP_WHEEL_MAPPING = 0x1A
    OP_SAVE_FLASH = 0x09
    OP_RESET_PROFILE = 0x1B

    def __init__(
        self,
        framing: Framing = DEFAULT_FRAMING,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        info_timeout_ms: int = 800,
        retries: int = DEFAULT_RETRIES,
        min_command_interval_s: float = MIN_COMMAND_INTERVAL_S,
        charging_supported: bool = False,
        battery_charging_byte: int = 2,
    ) -> None:
        self.framing = framing
        self.timeout_ms = timeout_ms
        self.info_timeout_ms = info_timeout_ms
        self.retries = max(1, retries)
        self.min_command_interval_s = max(0.0, min_command_interval_s)
        #: Rival 3 Wireless does not report charge status (AA batteries).
        self.charging_supported = charging_supported
        self.battery_charging_byte = battery_charging_byte

    # -- generic builders --------------------------------------------------
    def _build(self, command: tuple[int, ...]) -> bytes:
        return self.framing.encode(bytes(command))

    def build_battery_request(self) -> bytes:
        return self._build(self.CMD_BATTERY)

    def build_dpi_request(self) -> bytes:
        return self._build(self.CMD_DPI_READ)

    def build_dpi_level_request(self) -> bytes:
        return self._build(self.CMD_DPI_LEVEL)

    def build_polling_rate_request(self) -> bytes:
        return self._build(self.CMD_POLLING_RATE)

    def build_firmware_request(self) -> bytes:
        return self._build(self.CMD_FIRMWARE)

    def build_rf_status_request(self) -> bytes:
        return self._build(self.CMD_RF_STATUS)

    def build_firmware_requests(self) -> list[bytes]:
        """Firmware requests to try, primary (dongle ``D0``) first."""
        return [self._build(command) for command in self.CMD_FIRMWARE_FALLBACKS]

    def build_rf_status_requests(self) -> list[bytes]:
        """RF-status requests to try, primary (dongle ``E8``) first."""
        return [self._build(command) for command in self.CMD_RF_STATUS_FALLBACKS]

    def build_power_mode_request(self) -> bytes:
        return self._build(self.CMD_POWER_MODE)

    def build_debounce_request(self) -> bytes:
        return self._build(self.CMD_DEBOUNCE_READ)

    def build_raw(self, payload: bytes | bytearray) -> bytes:
        """Frame an already assembled command payload (opcode first)."""
        return self.framing.encode(bytes(payload))

    # -- configuration writes ---------------------------------------------
    def build_set_polling_rate(self, hz: int) -> bytes:
        if hz not in POLLING_RATE_LEVEL:
            raise ValueError("polling rate must be one of %s Hz" % sorted(POLLING_RATE_LEVEL))
        return self._build((self.OP_POLLING_RATE, POLLING_RATE_LEVEL[hz]))

    def build_set_dpi(self, sensor_values: list[int], active_level: int) -> bytes:
        """Batch DPI write: 1..5 presets (sensor values) and the active level."""
        count = len(sensor_values)
        if not 1 <= count <= MAX_DPI_LEVELS:
            raise ValueError("between 1 and %d DPI presets are required" % MAX_DPI_LEVELS)
        if not 1 <= active_level <= count:
            raise ValueError("active level must be between 1 and %d" % count)
        for value in sensor_values:
            if not TRUEMOVE_AIR_MIN_SENSOR <= value <= TRUEMOVE_AIR_MAX_SENSOR:
                raise ValueError("sensor value 0x%02X out of range" % value)
        padded = list(sensor_values) + [0] * (MAX_DPI_LEVELS - count)
        return self.build_raw(
            struct.pack("<BBB5H", self.OP_BATCH_CPI, count, active_level, *padded)
        )

    def build_set_power_mode(self, mode: "PowerMode") -> bytes:
        return self.build_raw(
            struct.pack(
                "<BBBHH",
                self.OP_POWER_MODE,
                mode.power_saving & 0xFF,
                mode.movement_saving & 0xFF,
                mode.idle_lighting & 0xFFFF,
                mode.idle_sleep & 0xFFFF,
            )
        )

    def build_set_debounce(self, make_ms: int, break_ms: int) -> bytes:
        if not (0 <= make_ms <= 255 and 0 <= break_ms <= 255):
            raise ValueError("debounce times must be 0..255 ms")
        return self._build((self.OP_DEBOUNCE, make_ms, break_ms))

    def build_live_color(self, r: int, g: int, b: int) -> bytes:
        """Show a colour immediately (not stored; cleared by restart-lighting)."""
        return self._build((self.OP_LED_DIRECT, 0x01, r & 0xFF, g & 0xFF, b & 0xFF))

    def build_restart_lighting(self) -> bytes:
        return self._build((self.OP_RESTART_LIGHTING,))

    def build_save_to_flash(self) -> bytes:
        return self._build((self.OP_SAVE_FLASH,))

    def build_reset_profile(self) -> bytes:
        return self._build((self.OP_RESET_PROFILE,))

    # -- debounce ----------------------------------------------------------
    def parse_debounce_response(self, data: bytes | bytearray) -> Optional[tuple[int, int]]:
        """Parse the ``9E`` response: ``(make_ms, break_ms)``."""
        decoded = self.framing.decode(data)
        if len(decoded) < 2:
            return None
        return decoded[0], decoded[1]

    # -- pairing writes ----------------------------------------------------
    def build_pair_dongle_request(self) -> bytes:
        """Pairing request ``71 00``; only effective in the mouse's pairing mode."""
        return self._build(self.CMD_PAIR_DONGLE)

    def build_reset_wireless_request(self) -> bytes:
        """Wireless-reset request ``73`` (sent on pairing failure by GG)."""
        return self._build(self.CMD_RESET_WIRELESS)

    # -- battery -----------------------------------------------------------
    def parse_battery_response(self, data: bytes | bytearray) -> BatteryStatus:
        """Parse the ``AA 01`` response.

        Verified layout on the Rival 3 Wireless::

            data[0:2] : battery level, uint16 little-endian (0..100)
            data[2]   : charge status (constant 0x00 on this AA-battery model)

        A captured response is ``2F 00 00 ...`` -> 47 %.
        """
        decoded = self.framing.decode(data)
        if len(decoded) < 2:
            raise ProtocolError("battery response too short (%d bytes)" % len(decoded))
        level = struct.unpack_from("<H", decoded, 0)[0]
        if level > 100:
            raise ProtocolError(
                "battery response level out of range: %d (raw: %s)"
                % (level, _hex(decoded[:8]))
            )
        charging: Optional[bool] = None
        if self.charging_supported and len(decoded) > self.battery_charging_byte:
            charging = bool(decoded[self.battery_charging_byte] & 0x01)
        return BatteryStatus(level=level, charging=charging, raw=bytes(decoded))

    # -- DPI ---------------------------------------------------------------
    def parse_dpi_response(self, data: bytes | bytearray) -> Optional[DpiInfo]:
        """Parse the ``A0`` batch-resolution response.

        Layout from the descriptor::

            data[0]     : level_max (1..5)
            data[1]     : active_level (1..5)
            data[2:12]  : resolution_1..5, uint16 little-endian sensor values

        Validation mirrors the vendor Lisp: ``level_max != 0``,
        ``active_level != 0`` and ``resolution_1 != 0``.  Out-of-range sensor
        values yield ``cpi = None`` for that preset.  Returns ``None`` when the
        report cannot be a valid resolution response.
        """
        decoded = self.framing.decode(data)
        # Some transports deliver the report-ID byte as part of the buffer
        # (leading 0x00).  level_max/active_level validation disambiguates the
        # two shapes, as recommended by the static analysis.
        for offset in (0, 1):
            if len(decoded) < offset + 12:
                continue
            level_max = decoded[offset]
            active_level = decoded[offset + 1]
            if not 1 <= level_max <= 5 or not 1 <= active_level <= 5:
                continue
            sensors = [
                struct.unpack_from("<H", decoded, offset + 2 + 2 * index)[0]
                for index in range(5)
            ]
            if sensors[0] == 0:
                continue
            presets = tuple(
                DpiPreset(
                    level=level,
                    sensor=sensors[level - 1],
                    cpi=cpi_from_sensor_value(sensors[level - 1]),
                )
                for level in range(1, level_max + 1)
            )
            return DpiInfo(
                level_max=level_max,
                active_level=active_level,
                presets=presets,
                raw=bytes(decoded),
            )
        return None

    def parse_dpi_level_response(self, data: bytes | bytearray) -> Optional[int]:
        """Parse the ``96`` active-level response: ``data[0] = level (1..5)``."""
        decoded = self.framing.decode(data)
        if not decoded:
            return None
        if 1 <= decoded[0] <= 5:
            return decoded[0]
        # Prefix shape: [0x00, level, ...]
        if decoded[0] == 0x00 and len(decoded) > 1 and 1 <= decoded[1] <= 5:
            return decoded[1]
        return None

    # -- polling rate ------------------------------------------------------
    def parse_polling_rate_response(self, data: bytes | bytearray) -> Optional[int]:
        """Parse the ``97`` response: ``data[0] = level`` -> Hz."""
        decoded = self.framing.decode(data)
        if not decoded:
            return None
        level = decoded[0]
        if level == 0x00 and len(decoded) > 1 and decoded[1] in (1, 2, 3):
            # Prefix shape: [0x00, level, ...]; level 0 itself is valid
            # (1000 Hz) and indistinguishable from a prefix plus padding, but
            # both decode to 1000 Hz, so the value is still correct.
            level = decoded[1]
        return POLLING_RATE_HZ.get(level)

    # -- firmware ----------------------------------------------------------
    def parse_firmware_response(
        self, data: bytes | bytearray
    ) -> Optional[tuple[int, int]]:
        """Parse the ``90`` response into ``(major, minor)``.

        The captured response was ``01 04`` -> firmware ``1.04``.
        """
        decoded = self.framing.decode(data)
        if len(decoded) < 2:
            return None
        if decoded[0] == 0x00 and len(decoded) > 2 and decoded[1] != 0:
            # Prefix shape: [0x00, fw_major, fw_minor, ...]
            return decoded[1], decoded[2]
        return decoded[0], decoded[1]

    # -- RF / connection ---------------------------------------------------
    def parse_rf_status_response(self, data: bytes | bytearray) -> Optional[bool]:
        """Parse the ``A8`` response; byte 0 == 4 means connected."""
        decoded = self.framing.decode(data)
        if not decoded:
            return None
        if decoded[0] == 4:
            return True
        # Prefix shape: [0x00, rf_status, ...]
        if decoded[0] == 0x00 and len(decoded) > 1:
            return decoded[1] == 4
        return False

    # -- power mode --------------------------------------------------------
    def parse_power_mode_response(
        self, data: bytes | bytearray
    ) -> Optional[PowerMode]:
        """Parse the ``AB`` response: two uint8 then two uint16 little-endian."""
        decoded = self.framing.decode(data)
        if len(decoded) < 6:
            return None
        return PowerMode(
            power_saving=decoded[0],
            movement_saving=decoded[1],
            idle_lighting=struct.unpack_from("<H", decoded, 2)[0],
            idle_sleep=struct.unpack_from("<H", decoded, 4)[0],
        )


def format_firmware(version: Optional[tuple[int, int]]) -> Optional[str]:
    """Format ``(1, 4)`` as ``"1.04"``."""
    if version is None:
        return None
    major, minor = version
    return "%d.%02d" % (major, minor)


def _hex(data: bytes) -> str:
    return " ".join("%02X" % byte for byte in data)


__all__ = [
    "VENDOR_ID",
    "PID_RIVAL3_WIRELESS",
    "RELATED_PIDS",
    "KNOWN_PIDS",
    "PRODUCT_NAMES",
    "MODEL_NUMBERS",
    "REPORT_SIZE",
    "IO_REPORT_SIZE",
    "USAGE_PAGE_CONTROL_0",
    "USAGE_PAGE_CONTROL_1",
    "VENDOR_USAGE_PAGE_MIN",
    "MIN_COMMAND_INTERVAL_S",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_RETRIES",
    "POLLING_RATE_HZ",
    "POLLING_RATE_LEVEL",
    "MAX_DPI_LEVELS",
    "SLEEP_TIMER_CHOICES_S",
    "SUPPORTED_CPI",
    "sensor_value_from_cpi",
    "TRUEMOVE_AIR_MIN_SENSOR",
    "TRUEMOVE_AIR_MAX_SENSOR",
    "cpi_from_sensor_value",
    "ProtocolError",
    "Framing",
    "DEFAULT_FRAMING",
    "FRAMING_PRESETS",
    "BatteryStatus",
    "DpiPreset",
    "DpiInfo",
    "PowerMode",
    "Protocol",
    "format_firmware",
]
