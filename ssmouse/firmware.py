"""Firmware version check and update (Rival 3 Wireless).

The update procedure is a direct port of ``fw_update`` from SteelSeries GG's
device descriptor.  Three images are written, each as 0x80-byte blocks:

========================  ======  ==========  ======================
Image                     Size    Device mask  Flash entry id
========================  ======  ==========  ======================
mouse (nRF)               0x12000  0x00        2  (sent over 2.4 GHz)
receiver radio (nRF)      0x9000   0x40        2
receiver USB (Holtek)     0x4000   0x40        3
========================  ======  ==========  ======================

Per image: erase ``(08|mask) 00 <id>``, wait 1 s, then for every block three
buffer writes ``(03|mask) 00 <off> <len> <data>`` (0x30 + 0x30 + 0x20 bytes)
and a push ``(05|mask) 00 <id> <block offset u32> 0080``.  Before starting,
low-power gaming mode and the polling rate are normalised; afterwards the
mouse (``01``) and receiver (``41``) are reset and the settings restored.

The protocol has no checksum or verification step.  An interrupted update
can leave the mouse or receiver unusable (the receiver then enumerates as the
bootloader, PID 0x1831), which is why :func:`update` refuses to run unless
the images are newer than the installed firmware or ``force`` is given.
SteelSeries firmware cannot be redistributed: point ssmouse at the
``firmware/272111664`` folder of a SteelSeries GG installation.
"""

from __future__ import annotations

import json
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .device import Mouse
from .protocol import POLLING_RATE_LEVEL

#: Newest firmware known to ssmouse (shipped with SteelSeries GG 122).
LATEST_KNOWN = "1.04"

BLOCK = 0x80
BLOCK_CHUNKS = ((0x00, 0x30), (0x30, 0x30), (0x60, 0x20))
#: SteelSeries GG's command spacing during updates.
UPDATE_INTERVAL_S = 0.012
MIN_BATTERY = 30


@dataclass(frozen=True)
class Image:
    name: str
    filename_pattern: str
    size: int
    mask: int
    entry_id: int


IMAGES = (
    Image("mouse", r"firmware-rival-3-wireless-mouse-v[\d.]+\.bin", 0x12000, 0x00, 2),
    Image("receiver radio", r"nordic-dongle-firmware-rival-3-wireless-v[\d.]+\.bin",
          0x9000, 0x40, 2),
    Image("receiver USB", r"holtek-dongle-firmware-rival-3-wireless-v[\d.]+\.bin",
          0x4000, 0x40, 3),
)


class FirmwareError(Exception):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    """``"1.04.0.0"`` / ``"1.04"`` -> ``(1, 4)`` (trailing zeros dropped)."""
    parts = [int(part) for part in re.findall(r"\d+", text)]
    while len(parts) > 2 and parts[-1] == 0:
        parts.pop()
    if not parts:
        raise FirmwareError("invalid version %r" % text)
    return tuple(parts)


def format_version(version: tuple[int, ...]) -> str:
    return ".".join([str(version[0])] + ["%02d" % part for part in version[1:]])


@dataclass
class FirmwareStatus:
    mouse: Optional[str]
    receiver: Optional[str]
    latest_known: str = LATEST_KNOWN

    @property
    def up_to_date(self) -> Optional[bool]:
        versions = [v for v in (self.mouse, self.receiver) if v]
        if not versions:
            return None
        latest = parse_version(self.latest_known)
        return all(parse_version(v) >= latest for v in versions)

    def as_dict(self) -> dict:
        return {"mouse": self.mouse, "receiver": self.receiver,
                "latest_known": self.latest_known, "up_to_date": self.up_to_date}


def read_status(mouse: Mouse) -> FirmwareStatus:
    """Mouse firmware (``90``, needs the radio link) and receiver firmware (``D0``)."""
    versions: dict[int, Optional[str]] = {}
    for opcode in (0x90, 0xD0):
        try:
            response = mouse._transact(  # noqa: SLF001 - single raw read
                mouse.protocol.build_raw(bytes([opcode])), mouse.protocol.info_timeout_ms)
        except Exception:  # noqa: BLE001 - mouse asleep: version unknown
            versions[opcode] = None
            continue
        parsed = mouse.protocol.parse_firmware_response(response)
        versions[opcode] = "%d.%02d" % parsed if parsed else None
    return FirmwareStatus(mouse=versions[0x90], receiver=versions[0xD0])


@dataclass
class FirmwarePackage:
    version: str
    images: dict[str, bytes]  # Image.name -> data
    source: Path


def _check_image(image: Image, data: bytes) -> None:
    if len(data) != image.size:
        raise FirmwareError("%s image is %d bytes, expected %d" % (image.name, len(data), image.size))
    stack, reset = struct.unpack_from("<II", data, 0)
    # ARM Cortex-M vector table: initial SP in RAM, Thumb reset vector in flash.
    if not 0x20000000 <= stack <= 0x20100000 or not reset & 1 or reset >= 0x100000:
        raise FirmwareError("%s image does not look like Rival 3 Wireless firmware" % image.name)


def load_package(folder: Path) -> FirmwarePackage:
    """Load the three images (and version.json) from a GG firmware folder."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FirmwareError("%s is not a folder" % folder)
    files = {path.name: path for path in folder.iterdir()}
    images = {}
    versions = set()
    for image in IMAGES:
        matches = [name for name in files if re.fullmatch(image.filename_pattern, name)]
        if len(matches) != 1:
            raise FirmwareError("expected one %s image in %s" % (image.name, folder))
        data = files[matches[0]].read_bytes()
        _check_image(image, data)
        images[image.name] = data
        versions.add(re.search(r"v([\d.]+)\.bin$", matches[0]).group(1))
    version = versions.pop() if len(versions) == 1 else None
    if "version.json" in files:
        meta = json.loads(files["version.json"].read_text())
        if meta.get("device_name") not in (None, "rival_3_wireless"):
            raise FirmwareError("firmware is for %s" % meta.get("device_name"))
        version = meta.get("firmware_version", version)
    if not version:
        raise FirmwareError("could not determine the firmware version")
    return FirmwarePackage(format_version(parse_version(version)), images, folder)


# ---------------------------------------------------------------------------
# Update plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    kind: str  # "write", "write-nofail", "sleep", "progress"
    payload: bytes = b""
    seconds: float = 0.0
    percent: int = 0


def build_plan(package: FirmwarePackage, power: Optional[tuple[int, int, int, int]],
               polling_level: Optional[int]) -> list[Step]:
    """Every command of the update, in order (mirrors GG's ``fw_update``).

    *power* is the current ``(power_saving, movement, idle_light, idle_sleep)``
    and *polling_level* the current level (0 = 1000 Hz).
    """
    steps: list[Step] = []
    total = sum(len(package.images[image.name]) for image in IMAGES)

    def write(payload: bytes) -> None:
        steps.append(Step("write", payload))

    def sleep(seconds: float) -> None:
        steps.append(Step("sleep", seconds=seconds))

    # Normalise power mode and polling rate (only when they differ from the
    # update-friendly values, as GG does).
    if power and power[0] != 0:
        write(struct.pack("<BBBHH", 0x2B, 0x00, power[1], power[2], power[3]))
        sleep(3.0)
    if polling_level:
        write(bytes([0x17, 0x00]))
        sleep(1.0)

    done = 0
    for image in IMAGES:
        data = package.images[image.name]
        sleep(0.1)
        write(struct.pack("<BBH", 0x08 | image.mask, 0x00, image.entry_id))
        sleep(1.0)
        for base in range(0, len(data), BLOCK):
            for offset, size in BLOCK_CHUNKS:
                chunk = data[base + offset:base + offset + size]
                write(struct.pack("<BBHH", 0x03 | image.mask, 0x00, offset, size) + chunk)
            write(struct.pack("<BBHIH", 0x05 | image.mask, 0x00, image.entry_id, base, BLOCK))
            done += BLOCK
            steps.append(Step("progress", percent=done * 100 // total))

    write(bytes([0x01]))  # reset the mouse so the new firmware runs
    sleep(2.0)
    if polling_level:
        write(bytes([0x17, polling_level]))
        sleep(1.0)
    if power and power[0] != 0:
        write(struct.pack("<BBBHH", 0x2B, *power))
        sleep(3.0)
    steps.append(Step("write-nofail", bytes([0x41])))  # reset the receiver
    sleep(2.0)
    return steps


def update(
    mouse: Mouse,
    package: FirmwarePackage,
    *,
    force: bool = False,
    dry_run: bool = False,
    progress: Optional[Callable[[int], None]] = None,
) -> list[Step]:
    """Flash *package*.  Returns the executed (or, for *dry_run*, planned) steps."""
    if not mouse.get_rf_status():
        raise FirmwareError("the mouse must be switched on and linked to the receiver")
    status = read_status(mouse)
    if status.mouse is None or status.receiver is None:
        raise FirmwareError("could not read the installed firmware versions")
    installed = min(parse_version(status.mouse), parse_version(status.receiver))
    if parse_version(package.version) <= installed and not force:
        raise FirmwareError(
            "installed firmware (mouse %s, receiver %s) is already %s or newer"
            % (status.mouse, status.receiver, package.version))
    battery = mouse.get_battery().level
    if battery < MIN_BATTERY:
        raise FirmwareError("battery at %d%%; replace the batteries first (need %d%%)"
                            % (battery, MIN_BATTERY))
    power_mode = mouse.get_power_mode()
    power = None if power_mode is None else (
        power_mode.power_saving, power_mode.movement_saving,
        power_mode.idle_lighting, power_mode.idle_sleep)
    hz = mouse.get_polling_rate()
    steps = build_plan(package, power, POLLING_RATE_LEVEL.get(hz) if hz else None)
    if dry_run:
        return steps

    saved = mouse.protocol.min_command_interval_s
    mouse.protocol.min_command_interval_s = UPDATE_INTERVAL_S
    try:
        for step in steps:
            if step.kind == "sleep":
                time.sleep(step.seconds)
            elif step.kind == "progress":
                if progress:
                    progress(step.percent)
            elif step.kind == "write":
                mouse._write(mouse.protocol.build_raw(step.payload))  # noqa: SLF001
            else:
                try:
                    mouse._write(mouse.protocol.build_raw(step.payload))  # noqa: SLF001
                except Exception:  # noqa: BLE001 - receiver resets mid-write
                    pass
    finally:
        mouse.protocol.min_command_interval_s = saved
    return steps
