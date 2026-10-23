"""Events the receiver pushes on its "sync" interface (usage page 0xFF00).

SteelSeries GG's descriptor (``translate_sync_data``) and a capture on the
hardware agree on three input reports, opcode first:

=========  ============================================
``28 NN``  link changed: 01 = mouse connected, 00 = lost
``16 NN``  active DPI preset changed to NN (1..5)
``2A NN``  battery level NN %
=========  ============================================

Listening costs nothing on the radio, so the GUI reacts to these instantly
and only polls as a fallback.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from . import protocol
from .device import enumerate_devices
from .transport import HidInterface, TransportError, make_hidapi_transport

EVENT_LINK = 0x28
EVENT_DPI_LEVEL = 0x16
EVENT_BATTERY = 0x2A


@dataclass(frozen=True)
class Event:
    kind: str  # "link", "dpi", "battery"
    value: int


def parse_event(data: bytes) -> Optional[Event]:
    if len(data) < 2:
        return None
    opcode, value = data[0], data[1]
    if opcode == EVENT_LINK:
        return Event("link", 1 if value == 1 else 0)
    if opcode == EVENT_DPI_LEVEL and 1 <= value <= protocol.MAX_DPI_LEVELS:
        return Event("dpi", value)
    if opcode == EVENT_BATTERY and value <= 100:
        return Event("battery", value)
    return None


def find_event_interface() -> Optional[HidInterface]:
    candidates = [
        interface for interface in enumerate_devices()
        if interface.usage_page == protocol.USAGE_PAGE_CONTROL_1
    ]
    candidates.sort(key=lambda interface: interface.interface_number != 2)
    return candidates[0] if candidates else None


def listen(
    on_event: Callable[[Event], None],
    stop: threading.Event,
    *,
    retry_s: float = 3.0,
) -> None:
    """Deliver events until *stop* is set; survives receiver unplug/replug."""
    while not stop.is_set():
        interface = find_event_interface()
        if interface is None:
            stop.wait(retry_s)
            continue
        transport = make_hidapi_transport(interface, protocol.DEFAULT_FRAMING, "auto")
        try:
            transport.open()
            while not stop.is_set():
                data = transport.read_input(protocol.REPORT_SIZE, 500)
                event = parse_event(data) if data else None
                if event is not None:
                    on_event(event)
        except TransportError:
            stop.wait(retry_s)
        finally:
            transport.close()
