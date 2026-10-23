"""Device status collection shared by the CLI and the GUI."""

from __future__ import annotations

from .device import Mouse
from .protocol import ProtocolError
from .transport import TransportError

#: Shown when the receiver reports no RF link (rf_status != 4).
NOT_LINKED_MESSAGE = (
    "mouse is not linked to the receiver (asleep or switched off) - "
    "move or click it and retry"
)


def require_link(mouse: Mouse) -> None:
    """Fail fast when the dongle says the mouse is not linked.

    The receiver answers ``rf_status`` itself, instantly; every mouse-side
    command would otherwise just time out while the mouse sleeps.
    """
    if mouse.get_rf_status() is False:
        raise ProtocolError(NOT_LINKED_MESSAGE)


def collect_info(mouse: Mouse) -> dict:
    """Read everything about the device; JSON-ready dict (``ssmouse --json info``)."""
    errors: list[str] = []
    battery = None
    dpi = None
    polling_rate = None
    power_mode = None
    connected = mouse.get_rf_status()
    if connected is False:
        errors.append(NOT_LINKED_MESSAGE)
    else:
        try:
            battery = mouse.get_battery().as_dict()
        except (TransportError, ProtocolError) as exc:
            errors.append("battery: %s" % exc)
            # A sleeping mouse will not answer any other query either; avoid
            # spending retries on each of them (keeps `info` responsive).
            mouse.protocol.retries = 1
        try:
            dpi_info = mouse.get_dpi()
            dpi = dpi_info.as_dict() if dpi_info is not None else None
        except (TransportError, ProtocolError) as exc:
            errors.append("dpi: %s" % exc)
        polling_rate = mouse.get_polling_rate()
        power_mode = mouse.get_power_mode()

    firmware_details = mouse.get_firmware_details()
    firmware = firmware_details["version"] if firmware_details else None

    device = mouse.get_info()
    device.firmware = firmware
    payload = device.as_dict()
    payload.update(
        {
            "battery": battery,
            "dpi": dpi,
            "firmware_source": firmware_details["source"]
            if firmware_details
            else None,
            "polling_rate_hz": polling_rate,
            "connected": connected,
            "power_mode": power_mode.as_dict() if power_mode is not None else None,
            "protocol": {
                "framing": mouse.protocol.framing.describe(),
                "source": "decrypted .edevice descriptors (rival-3-wireless.lisp)",
            },
        }
    )
    if errors:
        payload["errors"] = errors
    return payload
