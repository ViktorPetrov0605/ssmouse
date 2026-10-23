"""User profile: the settings ssmouse manages, stored as JSON.

Every section is optional; ``None`` means "leave the mouse as it is".  The
mouse cannot report its lighting or button mappings, so ssmouse remembers
them here; on the mouse they are made permanent with a save to flash.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import protocol
from .bindings import DEFAULT_BINDINGS, TRIGGERS, parse_binding
from .device import Mouse
from .lighting import LightingEffect
from .transport import TransportError

#: Default sensor-rest value written when it is switched on (GG uses 100/0).
SENSOR_REST_ON = 100
#: High-efficiency mode forces this report rate (as GG does).
HIGH_EFFICIENCY_HZ = 125


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "ssmouse"


def default_path() -> Path:
    return config_dir() / "profile.json"


@dataclass
class PowerSettings:
    sleep_s: Optional[int] = None
    sensor_rest: Optional[bool] = None
    high_efficiency: Optional[bool] = None

    def validate(self) -> None:
        if self.sleep_s is not None and not 60 <= self.sleep_s <= 3600:
            raise ValueError("sleep timer must be 60..3600 s")


@dataclass
class Profile:
    dpi_presets: Optional[list[int]] = None
    dpi_active: Optional[int] = None
    polling_hz: Optional[int] = None
    power: PowerSettings = field(default_factory=PowerSettings)
    lighting: Optional[LightingEffect] = None
    bindings: dict[str, str] = field(default_factory=dict)
    debounce: Optional[tuple[int, int]] = None

    def validate(self) -> None:
        if self.dpi_presets is not None:
            if not 1 <= len(self.dpi_presets) <= protocol.MAX_DPI_LEVELS:
                raise ValueError("1..5 DPI presets are required")
            if any(not 100 <= cpi <= 18000 for cpi in self.dpi_presets):
                raise ValueError("DPI presets must be 100..18000")
            active = self.dpi_active or 1
            if not 1 <= active <= len(self.dpi_presets):
                raise ValueError("active DPI level out of range")
        if self.polling_hz is not None and self.polling_hz not in protocol.POLLING_RATE_LEVEL:
            raise ValueError("polling rate must be 125, 250, 500 or 1000")
        self.power.validate()
        for trigger, spec in self.bindings.items():
            if trigger not in TRIGGERS:
                raise ValueError("unknown control %r" % trigger)
            parse_binding(spec)
        if self.debounce is not None and not all(0 <= v <= 255 for v in self.debounce):
            raise ValueError("debounce must be 0..255 ms")

    # -- JSON -----------------------------------------------------------------
    def as_dict(self) -> dict:
        data: dict = {}
        if self.dpi_presets is not None:
            data["dpi"] = {"presets": list(self.dpi_presets), "active": self.dpi_active or 1}
        if self.polling_hz is not None:
            data["polling_hz"] = self.polling_hz
        power = {k: v for k, v in vars(self.power).items() if v is not None}
        if power:
            data["power"] = power
        if self.lighting is not None:
            data["lighting"] = self.lighting.as_dict()
        bindings = {k: v for k, v in self.bindings.items() if DEFAULT_BINDINGS.get(k) != v}
        if bindings:
            data["bindings"] = bindings
        if self.debounce is not None:
            data["debounce"] = {"make_ms": self.debounce[0], "break_ms": self.debounce[1]}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        dpi = data.get("dpi") or {}
        power = data.get("power") or {}
        debounce = data.get("debounce")
        profile = cls(
            dpi_presets=[int(v) for v in dpi["presets"]] if dpi.get("presets") else None,
            dpi_active=int(dpi["active"]) if dpi.get("active") else None,
            polling_hz=int(data["polling_hz"]) if data.get("polling_hz") else None,
            power=PowerSettings(
                sleep_s=power.get("sleep_s"),
                sensor_rest=power.get("sensor_rest"),
                high_efficiency=power.get("high_efficiency"),
            ),
            lighting=LightingEffect.from_dict(data["lighting"]) if data.get("lighting") else None,
            bindings=dict(data.get("bindings") or {}),
            debounce=(int(debounce["make_ms"]), int(debounce["break_ms"])) if debounce else None,
        )
        profile.validate()
        return profile

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Profile":
        path = path or default_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        return cls.from_dict(json.loads(text))

    def save(self, path: Optional[Path] = None) -> Path:
        self.validate()
        path = path or default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

ALL_SECTIONS = ("dpi", "polling", "power", "debounce", "lighting", "bindings")


def apply_power(mouse: Mouse, power: PowerSettings) -> None:
    current = mouse.get_power_mode()
    if current is None:
        raise TransportError("could not read the current power settings")
    movement = current.movement_saving
    if power.sensor_rest is not None:
        movement = (movement or SENSOR_REST_ON) if power.sensor_rest else 0
    mode = protocol.PowerMode(
        power_saving=current.power_saving
        if power.high_efficiency is None
        else int(power.high_efficiency),
        movement_saving=movement,
        idle_lighting=current.idle_lighting,
        idle_sleep=current.idle_sleep if power.sleep_s is None else int(power.sleep_s),
    )
    if mode != current:
        mouse.set_power_mode(mode)


def apply_profile(
    mouse: Mouse,
    profile: Profile,
    sections: tuple[str, ...] = ALL_SECTIONS,
    persist: bool = True,
) -> list[str]:
    """Write the configured sections; returns the names of those applied.

    Lighting and button mappings live in RAM and are lost when the mouse is
    switched off unless *persist* stores them in flash (verified on hardware).
    """
    profile.validate()
    applied = []
    if "dpi" in sections and profile.dpi_presets:
        mouse.set_dpi(profile.dpi_presets, profile.dpi_active or 1)
        applied.append("dpi")
    if "power" in sections and any(v is not None for v in vars(profile.power).values()):
        apply_power(mouse, profile.power)
        applied.append("power")
    if "polling" in sections:
        hz = profile.polling_hz
        if profile.power.high_efficiency:
            hz = HIGH_EFFICIENCY_HZ
        if hz is not None and mouse.get_polling_rate() != hz:
            mouse.set_polling_rate(hz)
            applied.append("polling")
    if "debounce" in sections and profile.debounce is not None:
        mouse.set_debounce(*profile.debounce)
        applied.append("debounce")
    if "lighting" in sections and profile.lighting is not None:
        mouse.set_lighting(profile.lighting)
        applied.append("lighting")
    if "bindings" in sections and profile.bindings:
        mouse.set_bindings(profile.bindings)
        applied.append("bindings")
    if persist and applied:
        mouse.save_to_flash()
    return applied
