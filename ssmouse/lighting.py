"""Wheel LED effects: the SteelSeries "libsteel" effect format.

The encoder below reproduces ``get-libsteel-effect-bytes`` from SteelSeries
GG's ``libsteel_lighting_engine.lisp``.  An effect is uploaded in three
steps (``wheel_lighting`` in the device descriptor):

1. ``03 00 <offset u16> <size u16> <data>``  flash-buffer writes, 48-byte chunks
2. ``05 00 <id u16> <offset u32> <size u16>`` push the buffer to effect 0xFF10
3. ``1C <zone> <x i16> <y i16> <effect index>`` bind the LED to that effect

Effect layout (92 bytes)::

    u32  duration_ms         full cycle length
    i32  propagation         wave speed (0: no wave, single-LED mouse)
    i16  focal_x, focal_y
    u8   scale_x, 0, scale_y, 0
    u8   overflow, 0, 0, 0   0 wrap around, 1 hold
    u32  trigger_mask        reactive mode only
    u8   section_count
    rgb  initial_color
    16 x (rgb colour, u8 share of the cycle in 1/255)
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Optional

RGB = tuple[int, int, int]

EFFECT_ID_BASE = 0xFF10
CHUNK_SIZE = 0x30
MAX_SECTIONS = 16
OVERFLOW_WRAPAROUND = 0
OVERFLOW_HOLD = 1

MODES = ("steady", "colorshift", "breathe", "reactive", "off")
#: Colour stops supported per mode (section table holds 16 entries).
MAX_COLORS = {"colorshift": 14, "breathe": 7}
#: Reactive trigger mask: every button.
REACTIVE_ALL_BUTTONS = 0xFFFFFFFF
DEFAULT_COLOR: RGB = (255, 85, 0)  # SteelSeries orange
DEFAULT_DURATION_MS = 5000
DEFAULT_REACT_MS = 500

_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "orange": (255, 85, 0),
    "purple": (128, 0, 255), "pink": (255, 64, 160),
}


def parse_color(text: str) -> RGB:
    """``"ff5500"``, ``"#ff5500"``, ``"255,85,0"`` or a name such as ``"red"``."""
    value = text.strip().lower()
    if value in _NAMED_COLORS:
        return _NAMED_COLORS[value]
    match = re.fullmatch(r"#?([0-9a-f]{6})", value)
    if match:
        raw = int(match.group(1), 16)
        return (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF
    parts = value.split(",")
    if len(parts) == 3 and all(part.strip().isdigit() for part in parts):
        rgb = tuple(int(part) for part in parts)
        if all(0 <= channel <= 255 for channel in rgb):
            return rgb  # type: ignore[return-value]
    raise ValueError("invalid colour %r (use rrggbb, r,g,b or a colour name)" % text)


def format_color(rgb: RGB) -> str:
    return "%02x%02x%02x" % rgb


def _share(delta: float) -> int:
    """Position difference (0..100) -> share of the cycle in 1/255 units."""
    return max(0, min(255, int(delta * 2.55)))


@dataclass
class LightingEffect:
    """A wheel-LED effect in the terms SteelSeries GG uses."""

    mode: str = "steady"
    colors: list[RGB] = field(default_factory=lambda: [DEFAULT_COLOR])
    #: Colour-stop positions 0..100; evenly spaced when omitted.
    positions: Optional[list[int]] = None
    duration_ms: int = DEFAULT_DURATION_MS
    #: Reactive mode: colour flashed on click and how long it fades.
    react_color: RGB = (255, 255, 255)
    react_ms: int = DEFAULT_REACT_MS

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        self.colors = [tuple(color) for color in self.colors]  # type: ignore[misc]
        self.react_color = tuple(self.react_color)  # type: ignore[assignment]
        if not self.colors and self.mode != "off":
            raise ValueError("at least one colour is required")
        limit = MAX_COLORS.get(self.mode)
        if limit is not None and len(self.colors) > limit:
            raise ValueError("%s supports at most %d colours" % (self.mode, limit))
        if self.positions is not None:
            if len(self.positions) != len(self.colors):
                raise ValueError("one position per colour is required")
            if any(not 0 <= pos <= 100 for pos in self.positions) or sorted(
                self.positions
            ) != list(self.positions):
                raise ValueError("positions must be ascending values in 0..100")
        if not 0 < self.duration_ms <= 600_000:
            raise ValueError("duration must be 1 ms .. 10 min")
        if not 0 <= self.react_ms <= 20_000:
            raise ValueError("reactive fade must be 0..20000 ms")

    def stop_positions(self) -> list[int]:
        if self.positions is not None:
            return list(self.positions)
        count = len(self.colors)
        return [index * 100 // count for index in range(count)]

    # -- serialisation ------------------------------------------------------
    def as_dict(self) -> dict:
        data = {
            "mode": self.mode,
            "colors": [format_color(color) for color in self.colors],
            "duration_ms": self.duration_ms,
        }
        if self.positions is not None:
            data["positions"] = list(self.positions)
        if self.mode == "reactive":
            data["react_color"] = format_color(self.react_color)
            data["react_ms"] = self.react_ms
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LightingEffect":
        return cls(
            mode=data.get("mode", "steady"),
            colors=[parse_color(color) for color in data.get("colors", [])] or [DEFAULT_COLOR],
            positions=data.get("positions"),
            duration_ms=int(data.get("duration_ms", DEFAULT_DURATION_MS)),
            react_color=parse_color(data.get("react_color", "ffffff")),
            react_ms=int(data.get("react_ms", DEFAULT_REACT_MS)),
        )

    # -- encoding -------------------------------------------------------------
    def encode(self) -> bytes:
        """The 92-byte effect blob written to flash buffer 0xFF10."""
        overflow = OVERFLOW_WRAPAROUND
        trigger_mask = 0
        sections: list[tuple[RGB, int]]
        duration_ms = self.duration_ms

        if self.mode in ("steady", "off"):
            color = (0, 0, 0) if self.mode == "off" else self.colors[0]
            overflow = OVERFLOW_HOLD
            initial = color
            sections = [(color, 0)]
        elif self.mode == "reactive":
            # Flash react_color, fade back to the base colour over react_ms.
            overflow = OVERFLOW_HOLD
            trigger_mask = REACTIVE_ALL_BUTTONS
            initial = self.react_color
            sections = [(self.react_color, 0), (self.colors[0], 255)]
            duration_ms = max(10, self.react_ms)
        elif self.mode == "colorshift":
            pos = self.stop_positions()
            initial = self.colors[0]
            sections = [
                (color, _share(pos[i] - (pos[i - 1] if i else 0)))
                for i, color in enumerate(self.colors)
            ]
            sections.append((initial, _share(100 - pos[-1])))
            duration_ms = max(duration_ms, len(sections) * 330)
        else:  # breathe: every colour fades in and out through black
            pos = self.stop_positions()
            count = len(self.colors)
            initial = self.colors[0]
            sections = []
            for i, color in enumerate(self.colors):
                prev = pos[i - 1] if i else 0
                nxt = pos[i + 1] if i + 1 < count else 100
                sections.append((color, _share(pos[i] - (prev + (pos[i] - prev) // 2))))
                sections.append(((0, 0, 0), _share((pos[i] + (nxt - pos[i]) // 2) - pos[i])))
            last = pos[-1]
            sections.append((initial, _share(100 - (last + (100 - last) // 2))))
            duration_ms = max(duration_ms, 2 * count * 330)

        duration_ms -= duration_ms % 10  # the descriptor stores centiseconds * 10
        table = b"".join(bytes(color) + bytes([share]) for color, share in sections)
        table += b"\x00" * (4 * MAX_SECTIONS - len(table))
        return (
            struct.pack("<IihhBBBB", duration_ms, 0, 0, 0, 0, 0, 0, 0)
            + bytes([overflow, 0, 0, 0])
            + struct.pack("<I", trigger_mask)
            + bytes([len(sections)])
            + bytes(initial)
            + table
        )


def effect_payloads(effect: LightingEffect, zone: int = 0) -> list[bytes]:
    """Command payloads (opcode first) that upload *effect* to the wheel LED."""
    blob = effect.encode()
    payloads = []
    for offset in range(0, len(blob), CHUNK_SIZE):
        chunk = blob[offset:offset + CHUNK_SIZE]
        payloads.append(struct.pack("<BBHH", 0x03, 0x00, offset, len(chunk)) + chunk)
    payloads.append(struct.pack("<BBHIH", 0x05, 0x00, EFFECT_ID_BASE + zone, 0, len(blob)))
    payloads.append(struct.pack("<BBhhB", 0x1C, zone, 0, 0, 0))
    return payloads
