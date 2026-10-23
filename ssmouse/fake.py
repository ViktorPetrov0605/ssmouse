"""Fake transport and captured byte sequences for tests and demos.

The battery capture is *real*: it was observed on the Rival 3 Wireless
(USB 1038:1830, interface 3 / usage page 0xFFC0) before the device was
unplugged::

    ->  AA 01 00 00 ... 00                      (64-byte output report)
    <-  2F 00 00 00 ... 00                      (uint16 LE level = 47 %)

The remaining responses follow the exact struct layouts recovered from the
decrypted ``rival-3-wireless.lisp`` descriptor; they are *synthetic* values
until the hardware is reconnected for live verification.  They let the whole
app run with ``SSMOUSE_FAKE=1`` on a machine without the hardware, and are
replayed by the unit tests.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .protocol import DEFAULT_FRAMING, MODEL_NUMBERS, Protocol, REPORT_SIZE
from .transport import HidInterface, Transport, _trace

#: Replayed battery request exactly as sent (no report ID, 64 bytes, padded).
CAPTURED_BATTERY_REQUEST = bytes([0xAA, 0x01]) + b"\x00" * (REPORT_SIZE - 2)

#: Replayed battery response (uint16 LE 0x002F = 47 %; charge byte constant 0).
CAPTURED_BATTERY_RESPONSE = bytes([0x2F, 0x00, 0x00]) + b"\x00" * (REPORT_SIZE - 3)

#: Synthetic ``A0`` batch-resolution response:
#: level_max=5, active_level=2, sensors 0x04/0x06/0x10/0x20/0x30
#: -> CPI 400/600/1400/2800/4100 (truemove-air table), current 600.
FAKE_DPI_RESPONSE = bytes(
    [0x05, 0x02]
    + [0x04, 0x00]  # level 1: 400 CPI
    + [0x06, 0x00]  # level 2: 600 CPI  <- active
    + [0x10, 0x00]  # level 3: 1400 CPI
    + [0x20, 0x00]  # level 4: 2800 CPI
    + [0x30, 0x00]  # level 5: 4100 CPI
) + b"\x00" * (REPORT_SIZE - 12)

#: ``96`` active DPI level response: level 2.
FAKE_DPI_LEVEL_RESPONSE = bytes([0x02]) + b"\x00" * (REPORT_SIZE - 1)

#: ``97`` polling-rate response: level 2 -> 250 Hz.
FAKE_POLLING_RATE_RESPONSE = bytes([0x02]) + b"\x00" * (REPORT_SIZE - 1)

#: Live capture from the receiver: ``D0`` answered ``01 04`` -> firmware "1.04".
CAPTURED_DONGLE_FIRMWARE_RESPONSE = bytes([0x01, 0x04]) + b"\x00" * (REPORT_SIZE - 2)

#: Live capture from the unpaired receiver: ``E8`` answered ``01 00`` ->
#: rf_status == 1 (not connected; the descriptor treats 4 as connected).
CAPTURED_RF_STATUS_UNPAIRED_RESPONSE = bytes([0x01]) + b"\x00" * (REPORT_SIZE - 1)

#: Firmware response: major 1, minor 4 -> "1.04" (same layout for ``D0``/``90``).
FAKE_FIRMWARE_RESPONSE = CAPTURED_DONGLE_FIRMWARE_RESPONSE

#: RF status response: 4 = connected (same layout for ``E8``/``A8``).
FAKE_RF_STATUS_RESPONSE = bytes([0x04]) + b"\x00" * (REPORT_SIZE - 1)

#: ``AB`` power-mode response: power_saving=0, movement_saving=0,
#: idle_lighting=600 (0x0258), idle_sleep=900 (0x0384).
FAKE_POWER_MODE_RESPONSE = bytes(
    [0x00, 0x00, 0x58, 0x02, 0x84, 0x03]
) + b"\x00" * (REPORT_SIZE - 6)

#: Requests observed from SteelSeriesEngine.exe under Wine (usbmon capture of
#: bus 3, SET_REPORT to interface 3, 64-byte data, report ID byte 0 in wValue).
#: These are the *real* engine bytes and independently confirm the opcode and
#: framing used by :class:`ssmouse.protocol.Protocol`:
#:
#:     37x AA 01   battery        10x A8 00   RF status
#:      8x 90 00   firmware        7x A0 00   DPI presets
#:      6x 97 00   polling rate    3x AB 00   power mode
#:      1x 96 00   active DPI level
ENGINE_OBSERVED_REQUESTS: dict[str, bytes] = {
    "battery": bytes.fromhex("AA01"),
    "dpi": bytes.fromhex("A0"),
    "dpi_level": bytes.fromhex("96"),
    "polling_rate": bytes.fromhex("97"),
    "firmware": bytes.fromhex("90"),
    "rf_status": bytes.fromhex("A8"),
    "power_mode": bytes.fromhex("AB"),
}

#: Default script: request prefix -> response bytes.
DEFAULT_SCRIPT: tuple[tuple[bytes, bytes], ...] = (
    (bytes([0xAA, 0x01]), CAPTURED_BATTERY_RESPONSE),
    (bytes([0xA0]), FAKE_DPI_RESPONSE),
    (bytes([0x96]), FAKE_DPI_LEVEL_RESPONSE),
    (bytes([0x97]), FAKE_POLLING_RATE_RESPONSE),
    (bytes([0xD0]), FAKE_FIRMWARE_RESPONSE),  # dongle firmware (primary)
    (bytes([0x90]), FAKE_FIRMWARE_RESPONSE),  # mouse firmware (fallback)
    (bytes([0xE8]), FAKE_RF_STATUS_RESPONSE),  # dongle RF status (primary)
    (bytes([0xA8]), FAKE_RF_STATUS_RESPONSE),  # read-op variant (fallback)
    (bytes([0xAB]), FAKE_POWER_MODE_RESPONSE),
)


class FakeTransport(Transport):
    """A transport that replays captured responses instead of touching hardware."""

    name = "fake"

    def __init__(
        self,
        framing=DEFAULT_FRAMING,
        script: Optional[Iterable[tuple[bytes, Optional[bytes]]]] = None,
        interface: Optional[HidInterface] = None,
    ) -> None:
        super().__init__(framing)
        self.script = list(script if script is not None else DEFAULT_SCRIPT)
        self.interface = interface or FAKE_INTERFACE
        self.writes: list[bytes] = []
        self.feature_writes: list[bytes] = []
        self._queue: list[bytes] = []
        self._opened = False

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._queue.clear()

    # -- I/O ---------------------------------------------------------------
    def write_output(self, report: bytes) -> int:
        if not self._opened:
            raise RuntimeError("fake transport is not open")
        data = bytes(report)
        self.writes.append(data)
        _trace("TX", data, self.name)
        for prefix, response in self.script:
            if data.startswith(bytes(prefix)):
                if response is not None:
                    self._queue.append(bytes(response))
                return len(data)
        # Unknown command: mimic a device that silently ignores it.
        return len(data)

    def read_input(self, size: int, timeout_ms: int) -> Optional[bytes]:
        if not self._queue:
            return None
        data = self._queue.pop(0)[:size]
        _trace("RX", data, self.name)
        return data

    def get_feature(self, report_id: int, size: int) -> Optional[bytes]:
        return None

    def set_feature(self, data: bytes) -> None:
        self.feature_writes.append(bytes(data))


#: Fake interface description (shape matches a real hidapi enumeration record).
FAKE_INTERFACE = HidInterface(
    path="/dev/hidraw7",
    interface_number=3,
    usage_page=0xFFC0,
    usage=0x01,
    vendor_id=0x1038,
    product_id=0x1830,
    product_string="SteelSeries Rival 3 Wireless",
    manufacturer_string="SteelSeries",
    serial_number="",
    release_number=0x0104,
    backend="fake",
)


def make_fake_transport(
    interface: Optional[HidInterface] = None,
    script: Optional[Iterable[tuple[bytes, Optional[bytes]]]] = None,
    framing=DEFAULT_FRAMING,
) -> FakeTransport:
    return FakeTransport(framing=framing, script=script, interface=interface)


def make_fake_mouse(connect: bool = True):
    """Build a :class:`ssmouse.device.Mouse` backed by captured fake data."""
    from .device import Mouse

    protocol_impl = Protocol()
    mouse = Mouse(
        [FAKE_INTERFACE],
        protocol_impl=protocol_impl,
        retries=1,
        connect=False,
        fake=False,
    )
    transport = FakeTransport(framing=protocol_impl.framing)
    transport.open()
    mouse._transport = transport  # noqa: SLF001 - deliberate wiring for fake mode
    return mouse


__all__ = [
    "CAPTURED_BATTERY_REQUEST",
    "CAPTURED_BATTERY_RESPONSE",
    "FAKE_DPI_RESPONSE",
    "FAKE_DPI_LEVEL_RESPONSE",
    "FAKE_POLLING_RATE_RESPONSE",
    "FAKE_FIRMWARE_RESPONSE",
    "FAKE_RF_STATUS_RESPONSE",
    "CAPTURED_DONGLE_FIRMWARE_RESPONSE",
    "CAPTURED_RF_STATUS_UNPAIRED_RESPONSE",
    "FAKE_POWER_MODE_RESPONSE",
    "ENGINE_OBSERVED_REQUESTS",
    "DEFAULT_SCRIPT",
    "FakeTransport",
    "FAKE_INTERFACE",
    "make_fake_transport",
    "make_fake_mouse",
    "MODEL_NUMBERS",
]
