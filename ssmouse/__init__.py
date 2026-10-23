"""ssmouse - read battery, DPI and info from SteelSeries mice.

Quick start::

    with ssmouse.Mouse.detect() as mouse:
        print(mouse.get_battery())
        print(mouse.get_dpi())

Command line::

    python -m ssmouse info
    python -m ssmouse --json battery
    python -m ssmouse --watch battery
"""

from __future__ import annotations

__version__ = "0.3.0"

from .device import DeviceInfo, DeviceNotFoundError, Mouse  # noqa: E402
from .protocol import (  # noqa: E402
    BatteryStatus,
    DpiInfo,
    Framing,
    Protocol,
    ProtocolError,
)
from .transport import Transport, TransportError  # noqa: E402

__all__ = [
    "__version__",
    "Mouse",
    "DeviceInfo",
    "DeviceNotFoundError",
    "Protocol",
    "ProtocolError",
    "Framing",
    "BatteryStatus",
    "DpiInfo",
    "Transport",
    "TransportError",
]
