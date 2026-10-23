"""Qt-free state for the GUI: polling, last-known values and alert logic.

Everything here is plain Python so it can be unit-tested without a display.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..device import DeviceNotFoundError, Mouse
from ..protocol import ProtocolError
from ..status import collect_info
from ..transport import TransportError


class Link(enum.Enum):
    CONNECTED = "connected"
    ASLEEP = "asleep"  # receiver present, mouse asleep/off (rf_status != 4)
    NO_RECEIVER = "no-receiver"
    NO_PERMISSION = "no-permission"
    ERROR = "error"


LINK_TEXT = {
    Link.CONNECTED: "Connected",
    Link.ASLEEP: "Asleep - move the mouse to wake it",
    Link.NO_RECEIVER: "Receiver not found",
    Link.NO_PERMISSION: "No permission - install the udev rule",
    Link.ERROR: "Error",
}

#: Battery percentages that trigger a desktop notification when crossed downwards.
ALERT_THRESHOLDS = (20, 10)
#: Hysteresis: an alert re-arms once the level is this far above its threshold
#: (fresh batteries), so a reading that wobbles 20 -> 19 -> 20 alerts once.
ALERT_REARM_MARGIN = 5


@dataclass
class Snapshot:
    """One poll result, merged with the last-known battery level."""

    link: Link
    name: str = "SteelSeries mouse"
    battery: Optional[int] = None
    #: True when ``battery`` is a remembered value (mouse asleep right now).
    battery_stale: bool = False
    battery_seen_at: Optional[float] = None
    current_cpi: Optional[int] = None
    active_level: Optional[int] = None
    presets: list[int] = field(default_factory=list)
    polling_hz: Optional[int] = None
    firmware: Optional[str] = None
    path: Optional[str] = None
    detail: Optional[str] = None
    taken_at: float = field(default_factory=time.time)

    @property
    def status_text(self) -> str:
        return LINK_TEXT[self.link]


def battery_tier(level: Optional[int]) -> str:
    """Colour bucket for a battery level: ``ok``, ``low``, ``critical``, ``unknown``."""
    if level is None:
        return "unknown"
    if level <= 10:
        return "critical"
    if level <= 25:
        return "low"
    return "ok"


def snapshot_from_payload(payload: dict) -> Snapshot:
    """Build a :class:`Snapshot` from :func:`ssmouse.status.collect_info` output."""
    connected = payload.get("connected")
    battery = (payload.get("battery") or {}).get("level")
    dpi = payload.get("dpi") or {}
    link = Link.ASLEEP if connected is False else Link.CONNECTED
    if connected is None and battery is None:
        # Receiver did not answer rf_status and the mouse did not answer either.
        link = Link.ASLEEP
    return Snapshot(
        link=link,
        name=payload.get("name") or "SteelSeries mouse",
        battery=battery,
        battery_seen_at=time.time() if battery is not None else None,
        current_cpi=dpi.get("current_cpi"),
        active_level=dpi.get("active_level"),
        presets=[preset.get("cpi") for preset in dpi.get("presets") or []],
        polling_hz=payload.get("polling_rate_hz"),
        firmware=payload.get("firmware"),
        path=payload.get("path"),
        detail="; ".join(payload.get("errors") or []) or None,
    )


def snapshot_from_error(exc: BaseException) -> Snapshot:
    if isinstance(exc, DeviceNotFoundError):
        return Snapshot(link=Link.NO_RECEIVER, detail=str(exc))
    message = str(exc)
    if "permission" in message.lower() or "errno 13" in message.lower():
        return Snapshot(link=Link.NO_PERMISSION, detail=message)
    return Snapshot(link=Link.ERROR, detail=message)


def poll_once(detect: Callable[[], Mouse] = Mouse.detect) -> Snapshot:
    """Open the device, read everything, close it again (never raises)."""
    try:
        mouse = detect()
    except (DeviceNotFoundError, TransportError, ProtocolError, OSError) as exc:
        return snapshot_from_error(exc)
    try:
        return snapshot_from_payload(collect_info(mouse))
    except (TransportError, ProtocolError, OSError) as exc:
        return snapshot_from_error(exc)
    finally:
        mouse.close()


class StatusTracker:
    """Keeps the last-known battery across sleeps and decides on alerts."""

    def __init__(self, last_battery: Optional[int] = None, last_seen: Optional[float] = None):
        self.last_battery = last_battery
        self.last_seen = last_seen
        self._armed = {threshold: True for threshold in ALERT_THRESHOLDS}
        if last_battery is not None:
            self._update_alerts(last_battery)  # don't re-alert for a known low level

    def update(self, snapshot: Snapshot) -> tuple[Snapshot, Optional[int]]:
        """Merge ``snapshot`` with remembered state.

        Returns the merged snapshot and, if a low-battery threshold was just
        crossed, that threshold (otherwise ``None``).
        """
        if snapshot.battery is not None:
            self.last_battery = snapshot.battery
            self.last_seen = snapshot.battery_seen_at or snapshot.taken_at
            return snapshot, self._update_alerts(snapshot.battery)
        if self.last_battery is not None:
            snapshot = replace(
                snapshot,
                battery=self.last_battery,
                battery_stale=True,
                battery_seen_at=self.last_seen,
            )
        return snapshot, None

    def _update_alerts(self, level: int) -> Optional[int]:
        fired = None
        for threshold in ALERT_THRESHOLDS:  # highest first
            if level > threshold + ALERT_REARM_MARGIN:
                self._armed[threshold] = True
            elif level <= threshold and self._armed[threshold]:
                self._armed[threshold] = False
                fired = threshold
        return fired


def next_poll_delay(snapshot: Snapshot, interval_s: float) -> float:
    """Seconds until the next poll.

    While the mouse sleeps only the receiver is queried (cheap, no RF
    traffic), so poll faster to pick the mouse up soon after it wakes.
    """
    if snapshot.link in (Link.ASLEEP, Link.NO_RECEIVER):
        return min(interval_s, 5.0)
    return interval_s


def describe_age(seen_at: Optional[float], now: Optional[float] = None) -> str:
    if seen_at is None:
        return "never"
    seconds = max(0, int((now if now is not None else time.time()) - seen_at))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d h ago" % (seconds // 3600)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seen_at))
