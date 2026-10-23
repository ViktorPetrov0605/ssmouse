"""Device detection and high-level battery/DPI operations.

Responsibilities
----------------
* enumerate SteelSeries HID devices by vendor/product ID,
* rank the vendor control interfaces and pick a working one,
* retry/fail over between candidate interfaces,
* expose :class:`Mouse` with ``get_info()``, ``get_battery()`` and
  ``get_dpi()`` that the CLI can call without knowing about HID details.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from . import protocol

if TYPE_CHECKING:
    from .lighting import LightingEffect
from .protocol import (
    DEFAULT_FRAMING,
    KNOWN_PIDS,
    MODEL_NUMBERS,
    PRODUCT_NAMES,
    Protocol,
    RELATED_PIDS,
    VENDOR_ID,
    VENDOR_USAGE_PAGE_MIN,
)
from .transport import (
    HidInterface,
    Transport,
    TransportError,
    make_hidapi_transport,
)

#: Environment variable that forces the fake transport (CI / demo).
ENV_FAKE = "SSMOUSE_FAKE"
#: Optional raw hidraw path override, e.g. ``/dev/hidraw7``.
ENV_HIDRAW = "SSMOUSE_HIDRAW"
#: Optional interface-number override.
ENV_INTERFACE = "SSMOUSE_INTERFACE"
#: Optional framing preset name (see :data:`ssmouse.protocol.FRAMING_PRESETS`).
ENV_FRAMING = "SSMOUSE_FRAMING"
#: Optional backend override.
ENV_BACKEND = "SSMOUSE_BACKEND"


class DeviceNotFoundError(Exception):
    """No supported SteelSeries device is attached."""


@dataclass
class DeviceInfo:
    """Basic device metadata (from USB/HID enumeration)."""

    name: str
    vendor_id: int = VENDOR_ID
    product_id: int = protocol.PID_RIVAL3_WIRELESS
    model: Optional[str] = None
    manufacturer: Optional[str] = None
    serial_number: Optional[str] = None
    firmware: Optional[str] = None
    release_number: Optional[int] = None
    path: Optional[str] = None
    interface_number: Optional[int] = None
    usage_page: Optional[int] = None
    backend: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "vendor_id": "%04X" % self.vendor_id,
            "product_id": "%04X" % self.product_id,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "serial_number": self.serial_number,
            "firmware": self.firmware,
            "release_number": "%04X" % self.release_number
            if self.release_number
            else None,
            "path": self.path,
            "interface_number": self.interface_number,
            "usage_page": "0x%04X" % self.usage_page if self.usage_page else None,
            "backend": self.backend,
        }


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _enumerate_hidapi(
    pid_filter: Sequence[int], prefer_backend: Optional[str] = None
) -> list[HidInterface]:
    """Enumerate with whichever hidapi module is installed.

    ``prefer_backend="hidapi-hid"`` forces the libusb module first (useful on
    Linux when /dev/hidraw* is unusable but /dev/bus/usb is accessible).
    """
    records: list[dict] = []
    backend = ""
    modules: list[tuple[str, object]] = []
    if prefer_backend == "hidapi-hid":
        modules = [
            ("hidapi-hid", lambda: [dict(item) for item in __import__("hid").enumerate()]),
            ("hidapi-hidraw", lambda: list(__import__("hidraw").enumerate())),
        ]
    else:
        modules = [
            ("hidapi-hidraw", lambda: list(__import__("hidraw").enumerate())),
            ("hidapi-hid", lambda: [dict(item) for item in __import__("hid").enumerate()]),
        ]
    for name, loader in modules:
        try:
            records = list(loader())  # type: ignore[operator]
            backend = name
            if records is not None:
                break
        except Exception:  # noqa: BLE001 - module missing or enumeration failed
            continue
    if not records:
        return []

    interfaces: list[HidInterface] = []
    for record in records:
        if record.get("vendor_id") != VENDOR_ID:
            continue
        pid = record.get("product_id")
        if pid_filter and pid not in pid_filter:
            continue
        interface = HidInterface(
            path=record.get("path", b""),
            interface_number=record.get("interface_number"),
            usage_page=record.get("usage_page"),
            usage=record.get("usage"),
            vendor_id=record.get("vendor_id", 0),
            product_id=pid,
            product_string=record.get("product_string") or "",
            manufacturer_string=record.get("manufacturer_string") or "",
            serial_number=record.get("serial_number") or "",
            release_number=record.get("release_number") or 0,
            backend=backend,
        )
        if backend == "hidapi-hidraw":
            interface = _enrich_hidraw_interface(interface)
        interfaces.append(interface)
    return interfaces


def _read_hidraw_usage_page(node: str) -> Optional[int]:
    """Best-effort usage-page lookup for the ctypes fallback (Linux sysfs)."""
    try:
        with open(
            "/sys/class/hidraw/%s/device/report_descriptor" % node, "rb"
        ) as handle:
            descriptor = handle.read(4096)
    except OSError:
        return None
    return _usage_page_from_descriptor(descriptor)


def _usage_page_from_descriptor(descriptor: bytes) -> Optional[int]:
    """Return the first Usage Page item value (0x05 / 0x06) of a report descriptor."""
    index = 0
    length = len(descriptor)
    while index < length:
        prefix = descriptor[index]
        size_code = prefix & 0x03
        size = (4, 1, 2, 4)[size_code] if size_code != 3 else 4
        item = prefix & 0xFC
        if item in (0x04, 0x05, 0x06):  # Usage Page, 1/2/4 bytes
            if index + 1 + size <= length:
                raw = descriptor[index + 1 : index + 1 + size]
                value = int.from_bytes(raw, "little")
                return value
            return None
        index += 1 + (size if size_code != 3 else 4)
    return None


def _sysfs_interface_metadata(node: str) -> tuple[Optional[int], Optional[int], str, str]:
    """Return ``(interface_number, usage_page, hid_id, hid_name)`` for a node.

    ``HID_PHYS=usb-…/input3`` is the authoritative interface number; the
    hidraw node number itself is not stable across re-enumeration.
    """
    interface_number: Optional[int] = None
    hid_id = ""
    hid_name = ""
    uevent_path = "/sys/class/hidraw/%s/device/uevent" % node
    try:
        with open(uevent_path, "r", encoding="utf-8", errors="replace") as handle:
            uevent = handle.read()
    except OSError:
        uevent = ""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            hid_id = line.split("=", 1)[1]
        elif line.startswith("HID_PHYS=") and "/input" in line:
            try:
                interface_number = int(line.rsplit("input", 1)[1])
            except ValueError:
                interface_number = None
        elif line.startswith("HID_NAME="):
            hid_name = line.split("=", 1)[1]
    return interface_number, _read_hidraw_usage_page(node), hid_id, hid_name


def _enrich_hidraw_interface(interface: HidInterface) -> HidInterface:
    """Fill/correct interface_number and usage_page from sysfs.

    hidapi's Linux hidraw backend usually reports these correctly, but the
    authoritative values live in sysfs; a mismatch here would select the wrong
    node (interface 2 rejects writes with EPIPE).
    """
    if not sys.platform.startswith("linux"):
        return interface
    path = interface.path_str
    if not path.startswith("/dev/hidraw"):
        return interface
    node = path.rsplit("/", 1)[-1]
    interface_number, usage_page, hid_id, hid_name = _sysfs_interface_metadata(node)
    if not hid_id:
        return interface
    parts = hid_id.split(":")
    if len(parts) == 3:
        try:
            vendor = int(parts[1], 16) & 0xFFFF
            _product = int(parts[2], 16) & 0xFFFF
        except ValueError:
            return interface
        if vendor != VENDOR_ID:
            return interface
    # Prefer the sysfs usage page for vendor collections, but keep a
    # non-vendor collection value reported by hidapi (an interface can expose
    # more than one top-level collection, e.g. keyboard 0x0001 + consumer
    # 0x000C); the report descriptor only exposes the first usage page.
    resolved_usage = interface.usage_page
    if usage_page is not None:
        if resolved_usage is None or resolved_usage == 0 or usage_page >= VENDOR_USAGE_PAGE_MIN:
            resolved_usage = usage_page
    return replace(
        interface,
        interface_number=interface_number
        if interface_number is not None
        else interface.interface_number,
        usage_page=resolved_usage,
        product_string=interface.product_string or hid_name,
    )


def _enumerate_sysfs(pid_filter: Sequence[int]) -> list[HidInterface]:
    """Linux fallback enumeration straight from sysfs (no dependencies)."""
    if not sys.platform.startswith("linux"):
        return []
    interfaces: list[HidInterface] = []
    try:
        nodes = sorted(os.listdir("/sys/class/hidraw"))
    except OSError:
        return []
    for node in nodes:
        uevent_path = "/sys/class/hidraw/%s/device/uevent" % node
        try:
            with open(uevent_path, "r", encoding="utf-8", errors="replace") as handle:
                uevent = handle.read()
        except OSError:
            continue
        hid_id = ""
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                hid_id = line.split("=", 1)[1]
        # HID_ID=0003:00001038:00001830
        parts = hid_id.split(":")
        if len(parts) != 3:
            continue
        try:
            vendor = int(parts[1], 16) & 0xFFFF
            product = int(parts[2], 16) & 0xFFFF
        except ValueError:
            continue
        if vendor != VENDOR_ID:
            continue
        if pid_filter and product not in pid_filter:
            continue
        interface_number, usage_page, _hid_id, hid_name = _sysfs_interface_metadata(
            node
        )
        interfaces.append(
            HidInterface(
                path="/dev/%s" % node,
                interface_number=interface_number,
                usage_page=usage_page,
                vendor_id=vendor,
                product_id=product,
                product_string=hid_name,
                backend="ctypes-hidraw",
            )
        )
    return interfaces


def enumerate_devices(
    product_ids: Optional[Iterable[int]] = None,
    *,
    prefer_backend: Optional[str] = None,
) -> list[HidInterface]:
    """Return every HID interface of a supported SteelSeries device."""
    pid_filter = tuple(product_ids) if product_ids is not None else KNOWN_PIDS
    hidapi_interfaces = _enumerate_hidapi(pid_filter, prefer_backend)
    if hidapi_interfaces:
        return hidapi_interfaces
    return _enumerate_sysfs(pid_filter)


# ---------------------------------------------------------------------------
# Interface ranking
# ---------------------------------------------------------------------------


def _rank_interface(interface: HidInterface) -> Optional[int]:
    """Rank a HID interface as a control-channel candidate.

    Lower is better; ``None`` means "not a vendor control interface".

    Verified on the Rival 3 Wireless: interface 3 / usage page 0xFFC0 accepts
    output reports, while interface 2 / usage page 0xFF00 rejects them with
    EPIPE.  Both are still candidates so that other models keep working.
    """
    usage = interface.usage_page
    number = interface.interface_number
    if usage == protocol.USAGE_PAGE_CONTROL_0 and number == 3:
        return 0
    if usage == protocol.USAGE_PAGE_CONTROL_0:
        return 1
    if number == 3:
        return 2
    if usage == protocol.USAGE_PAGE_CONTROL_1 and number == 2:
        return 3
    if usage == protocol.USAGE_PAGE_CONTROL_1:
        return 4
    if usage is not None and usage >= protocol.VENDOR_USAGE_PAGE_MIN:
        return 5
    if number is not None and number >= 2:
        return 6
    return None


def rank_interfaces(
    interfaces: Iterable[HidInterface],
) -> list[HidInterface]:
    """Deduplicate by path and order by control-interface priority."""
    best: dict[str, tuple[int, HidInterface]] = {}
    for interface in interfaces:
        rank = _rank_interface(interface)
        if rank is None:
            continue
        key = interface.path_str
        current = best.get(key)
        if current is None or rank < current[0]:
            best[key] = (rank, interface)
    return [item[1] for item in sorted(best.values(), key=lambda pair: pair[0])]


def interface_override(interfaces: Sequence[HidInterface]) -> list[HidInterface]:
    """Apply ``SSMOUSE_HIDRAW`` / ``SSMOUSE_INTERFACE`` filters for debugging."""
    wanted_path = os.environ.get(ENV_HIDRAW)
    wanted_interface = os.environ.get(ENV_INTERFACE)
    selected = list(interfaces)
    if wanted_path:
        selected = [
            i
            for i in selected
            if i.path_str == wanted_path or i.path_str.endswith(wanted_path)
        ]
    if wanted_interface:
        try:
            number = int(wanted_interface, 0)
        except ValueError:
            number = None
        if number is not None:
            selected = [i for i in selected if i.interface_number == number]
    return selected


# ---------------------------------------------------------------------------
# High-level device
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result of attempting one command against one candidate interface."""

    interface: HidInterface
    transport: Transport


class Mouse:
    """A supported SteelSeries mouse.

    Typical use::

        with Mouse() as mouse:
            print(mouse.get_battery())
            print(mouse.get_dpi())

    Opening tries each candidate control interface in priority order; if a
    command fails with :class:`TransportError` the next candidate is tried
    automatically (``EPIPE`` on interface 2 is the real-world case).
    """

    def __init__(
        self,
        interfaces: Sequence[HidInterface],
        *,
        protocol_impl: Optional[Protocol] = None,
        backend: str = "auto",
        retries: Optional[int] = None,
        connect: bool = True,
        fake: bool = False,
    ) -> None:
        if not interfaces:
            raise DeviceNotFoundError("no supported SteelSeries HID interface")
        self.interfaces = list(interfaces)
        self.protocol = protocol_impl or Protocol()
        if retries is not None:
            self.protocol.retries = max(1, int(retries))
        self.backend = backend
        self.fake = fake
        self._transport: Optional[Transport] = None
        self._candidate_index = 0
        self._last_command_at = 0.0
        if connect:
            self.open()

    # -- construction helpers ---------------------------------------------
    @classmethod
    def detect(
        cls,
        product_ids: Optional[Iterable[int]] = None,
        *,
        backend: Optional[str] = None,
        framing_name: Optional[str] = None,
        retries: Optional[int] = None,
        connect: bool = True,
    ) -> "Mouse":
        """Detect and open a device, honouring environment overrides."""
        if _fake_requested():
            from .fake import make_fake_mouse

            return make_fake_mouse(connect=connect)

        framing = _framing_from_env(framing_name)
        protocol_impl = Protocol(framing=framing)
        interfaces = enumerate_devices(
            product_ids, prefer_backend=backend or os.environ.get(ENV_BACKEND)
        )
        interfaces = interface_override(interfaces)
        candidates = rank_interfaces(interfaces)
        if not candidates:
            raise DeviceNotFoundError(
                "no %04X:%s device found%s"
                % (
                    VENDOR_ID,
                    "/".join("%04X" % pid for pid in (product_ids or KNOWN_PIDS)),
                    " (is it plugged in / awake?)",
                )
            )
        return cls(
            candidates,
            protocol_impl=protocol_impl,
            backend=backend or os.environ.get(ENV_BACKEND) or "auto",
            retries=retries,
            connect=connect,
        )

    @classmethod
    def open_first(
        cls,
        *,
        backend: str = "auto",
        framing_name: Optional[str] = None,
        retries: Optional[int] = None,
        connect: bool = True,
    ) -> "Mouse":
        return cls.detect(
            backend=backend,
            framing_name=framing_name,
            retries=retries,
            connect=connect,
        )

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._transport is not None:
            return
        errors: list[str] = []
        for index in range(self._candidate_index, len(self.interfaces)):
            interface = self.interfaces[index]
            try:
                transport = self._make_transport(interface)
                transport.open()
            except TransportError as exc:
                errors.append("%s: %s" % (interface.path_str, exc))
                continue
            self._transport = transport
            self._candidate_index = index
            return
        raise TransportError(
            "could not open any control interface: %s" % "; ".join(errors)
        )

    def _make_transport(self, interface: HidInterface) -> Transport:
        if self.fake:
            from .fake import make_fake_transport

            return make_fake_transport(interface=interface)
        return make_hidapi_transport(interface, self.protocol.framing, self.backend)

    def close(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            finally:
                self._transport = None

    def __enter__(self) -> "Mouse":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- low level command execution --------------------------------------
    def _fail_over(self, error: TransportError) -> None:
        """Drop the current interface and try the next one."""
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self._transport = None
        self._candidate_index += 1
        if self._candidate_index >= len(self.interfaces):
            raise error
        self.open()

    def _respect_command_spacing(self) -> None:
        """Keep >= ``Protocol.min_command_interval_s`` between two commands."""
        interval = getattr(self.protocol, "min_command_interval_s", 0.0)
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_command_at
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _transact(self, request: bytes, timeout_ms: int) -> bytes:
        """Write *request* and return the first non-empty response report.

        Enforces the vendor's 30 ms inter-command spacing, retries the request
        ``Protocol.retries`` times and fails over to the next control
        interface if the write raises (the real-world EPIPE case).
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.protocol.retries):
            self.open()
            assert self._transport is not None
            self._respect_command_spacing()
            try:
                self._transport.write_output(request)
            except TransportError as exc:
                last_error = exc
                try:
                    self._fail_over(exc)
                except TransportError:
                    raise last_error
                continue
            finally:
                self._last_command_at = time.monotonic()
            response = self._transport.read_input(protocol.REPORT_SIZE, timeout_ms)
            if response:
                return response
            last_error = TransportTimeoutError(
                "no response within %d ms (attempt %d/%d)"
                % (timeout_ms, attempt + 1, self.protocol.retries)
            )
            if attempt + 1 < self.protocol.retries:
                time.sleep(0.05)
        raise last_error or TransportTimeoutError("no response")

    # -- public commands ---------------------------------------------------
    #: Reads that are allowed to fail return ``None`` instead of raising, so
    #: ``info`` can show whatever is available.  Battery is the exception: its
    #: own CLI command must report a failure.
    def _read_optional(
        self,
        request: bytes,
        parser,  # noqa: ANN001 - parser callable
        timeout_ms: Optional[int] = None,
    ):
        try:
            response = self._transact(
                request,
                timeout_ms if timeout_ms is not None else self.protocol.timeout_ms,
            )
        except TransportError:
            return None
        return parser(response)

    def _read_first(
        self,
        requests: Sequence[bytes],
        parser,  # noqa: ANN001 - parser callable
        timeout_ms: int,
    ):
        """Try several request variants; return the first parsed result."""
        for request in requests:
            try:
                response = self._transact(request, timeout_ms)
            except TransportError:
                continue
            parsed = parser(response)
            if parsed is not None:
                return parsed
        return None

    def get_battery(self) -> protocol.BatteryStatus:
        """Query the battery level (charge status is None; AA batteries)."""
        request = self.protocol.build_battery_request()
        response = self._transact(request, self.protocol.timeout_ms)
        return self.protocol.parse_battery_response(response)

    def get_dpi(self) -> Optional[protocol.DpiInfo]:
        """Query the DPI presets and active level (command ``A0``)."""
        return self._read_optional(
            self.protocol.build_dpi_request(),
            self.protocol.parse_dpi_response,
        )

    def get_active_dpi_level(self) -> Optional[int]:
        """Query only the active DPI level (command ``96``)."""
        return self._read_optional(
            self.protocol.build_dpi_level_request(),
            self.protocol.parse_dpi_level_response,
        )

    def get_polling_rate(self) -> Optional[int]:
        """Query the polling rate in Hz (command ``97``)."""
        return self._read_optional(
            self.protocol.build_polling_rate_request(),
            self.protocol.parse_polling_rate_response,
        )

    def get_firmware_details(self) -> Optional[dict]:
        """Query the firmware version, trying dongle ``D0`` then mouse ``90``.

        Returns ``{"version": "1.04", "source": "dongle"|"mouse",
        "opcode": 0xD0|0x90}`` or ``None`` when neither variant answers.
        """
        requests = self.protocol.build_firmware_requests()
        for index, request in enumerate(requests):
            try:
                response = self._transact(request, self.protocol.info_timeout_ms)
            except TransportError:
                continue
            version = self.protocol.parse_firmware_response(response)
            if version is None:
                continue
            source = "dongle" if index == 0 else "mouse"
            return {
                "version": protocol.format_firmware(version),
                "source": source,
                "opcode": request[0],
            }
        return None

    def get_firmware(self) -> Optional[str]:
        """Query the firmware version string, e.g. ``"1.04"``."""
        details = self.get_firmware_details()
        return details["version"] if details else None

    def get_rf_status(self) -> Optional[bool]:
        """Query the RF link status; ``True`` when the mouse is connected.

        Tries the descriptor's dongle read ``E8`` first, then ``A8``.
        """
        return self._read_first(
            self.protocol.build_rf_status_requests(),
            self.protocol.parse_rf_status_response,
            self.protocol.info_timeout_ms,
        )

    def is_connected(self) -> Optional[bool]:
        """Alias for :meth:`get_rf_status` (vendor ``get-connected-state``)."""
        return self.get_rf_status()

    def get_power_mode(self) -> Optional[protocol.PowerMode]:
        """Query the power-saving configuration (command ``AB``)."""
        return self._read_optional(
            self.protocol.build_power_mode_request(),
            self.protocol.parse_power_mode_response,
            self.protocol.info_timeout_ms,
        )

    def get_debounce(self) -> Optional[tuple[int, int]]:
        """Query button debounce ``(make_ms, break_ms)`` (command ``9E``)."""
        return self._read_optional(
            self.protocol.build_debounce_request(),
            self.protocol.parse_debounce_response,
        )

    # -- configuration writes ----------------------------------------------
    #: The mouse does not acknowledge writes; callers verify by reading back.
    def _write(self, request: bytes) -> None:
        last_error: Optional[TransportError] = None
        for _ in range(max(1, len(self.interfaces))):
            self.open()
            assert self._transport is not None
            self._respect_command_spacing()
            try:
                self._transport.write_output(request)
                return
            except TransportError as exc:
                last_error = exc
                self._fail_over(exc)
            finally:
                self._last_command_at = time.monotonic()
        raise last_error or TransportError("write failed")

    def write_payloads(self, payloads: Iterable[bytes]) -> None:
        """Send raw command payloads (opcode first) in order."""
        for payload in payloads:
            self._write(self.protocol.build_raw(payload))

    def set_dpi(self, cpi_presets: Sequence[int], active_level: int) -> list[int]:
        """Store 1..5 DPI presets and select one; returns the CPI actually used.

        Values snap to the sensor's 100-CPI grid (100..18000).
        """
        sensor = [protocol.sensor_value_from_cpi(int(cpi)) for cpi in cpi_presets]
        self._write(self.protocol.build_set_dpi(sensor, active_level))
        return [protocol.cpi_from_sensor_value(value) for value in sensor]

    def set_active_dpi_level(self, level: int) -> None:
        """Switch to another stored preset, keeping the presets themselves."""
        info = self.get_dpi()
        if info is None:
            raise TransportError("could not read the current DPI presets")
        sensor = [preset.sensor for preset in info.presets[: info.level_max]]
        self._write(self.protocol.build_set_dpi(sensor, level))

    def set_polling_rate(self, hz: int, settle_s: float = 5.0) -> None:
        """Change the report rate.

        The mouse drops the radio link for ~2 s while it switches; wait up to
        *settle_s* for it to come back so the next command is not lost.
        """
        self._write(self.protocol.build_set_polling_rate(hz))
        deadline = time.monotonic() + settle_s
        time.sleep(min(1.0, settle_s))
        while time.monotonic() < deadline:
            if self.get_rf_status():
                return
            time.sleep(0.25)

    def set_power_mode(self, mode: protocol.PowerMode) -> None:
        self._write(self.protocol.build_set_power_mode(mode))

    def set_debounce(self, make_ms: int, break_ms: int) -> None:
        self._write(self.protocol.build_set_debounce(make_ms, break_ms))

    def set_live_color(self, rgb: tuple[int, int, int]) -> None:
        """Show a colour right away without storing it (``restart_lighting`` undoes it)."""
        self._write(self.protocol.build_live_color(*rgb))

    def restart_lighting(self) -> None:
        self._write(self.protocol.build_restart_lighting())

    def set_lighting(self, effect: "LightingEffect") -> None:
        """Upload and activate a wheel-LED effect."""
        from .lighting import effect_payloads

        self.write_payloads(effect_payloads(effect))

    def set_bindings(self, bindings: dict[str, str]) -> None:
        """Remap buttons and wheel; controls not listed get their default action."""
        from .bindings import mapping_payloads

        self.write_payloads(mapping_payloads(bindings))

    def save_to_flash(self) -> None:
        """Persist the current settings in the mouse's flash memory."""
        self._write(self.protocol.build_save_to_flash())

    def reset_profile(self, persist: bool = True) -> None:
        """Restore the mouse's factory settings (and store them in flash)."""
        self._write(self.protocol.build_reset_profile())
        if persist:
            time.sleep(0.5)
            self.save_to_flash()

    def pair(self, timeout_s: float = 20.0, poll_s: float = 2.0) -> bool:
        """Run GG's pairing flow: ``71 00``, poll ``E8`` until linked, ``73`` on timeout.

        Put the mouse in pairing mode first.  Returns True once linked.
        """
        self._write(self.protocol.build_pair_dongle_request())
        deadline = time.monotonic() + timeout_s
        linked = 0
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            if self.get_rf_status():
                linked += 1
                if linked >= 2:
                    return True
            else:
                linked = 0
        self._write(self.protocol.build_reset_wireless_request())
        return False

    # -- metadata ----------------------------------------------------------
    def get_info(self) -> DeviceInfo:
        interface = self.interfaces[self._candidate_index] if self.interfaces else None
        pid = interface.product_id if interface else protocol.PID_RIVAL3_WIRELESS
        name = (
            (interface.product_string if interface else "")
            or PRODUCT_NAMES.get(pid)
            or "SteelSeries device"
        )
        return DeviceInfo(
            name=name,
            vendor_id=interface.vendor_id if interface else VENDOR_ID,
            product_id=pid,
            model=MODEL_NUMBERS.get(pid),
            manufacturer=(interface.manufacturer_string or None) if interface else None,
            serial_number=(interface.serial_number or None) if interface else None,
            firmware=None,  # TODO(protocol): decode firmware from AA 00 response
            release_number=interface.release_number if interface else None,
            path=interface.path_str if interface else None,
            interface_number=interface.interface_number if interface else None,
            usage_page=interface.usage_page if interface else None,
            backend=self._transport.name if self._transport else None,
        )

    def describe_interface(self) -> Optional[HidInterface]:
        if not self.interfaces:
            return None
        return self.interfaces[self._candidate_index]


class TransportTimeoutError(TransportError, TimeoutError):
    """Kept local to avoid importing transport internals into the CLI."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_requested() -> bool:
    return os.environ.get(ENV_FAKE, "").strip().lower() in ("1", "true", "yes", "on")


def _framing_from_env(framing_name: Optional[str]) -> protocol.Framing:
    name = framing_name or os.environ.get(ENV_FRAMING)
    if not name:
        return DEFAULT_FRAMING
    try:
        return protocol.FRAMING_PRESETS[name]
    except KeyError:
        raise ValueError(
            "unknown framing %r (choose from %s)"
            % (name, ", ".join(sorted(protocol.FRAMING_PRESETS)))
        )


def known_product_ids() -> tuple[int, ...]:
    return (protocol.PID_RIVAL3_WIRELESS,) + RELATED_PIDS


__all__ = [
    "DeviceNotFoundError",
    "DeviceInfo",
    "Mouse",
    "TransportTimeoutError",
    "enumerate_devices",
    "rank_interfaces",
    "interface_override",
    "known_product_ids",
    "ENV_FAKE",
    "ENV_HIDRAW",
    "ENV_INTERFACE",
    "ENV_FRAMING",
    "ENV_BACKEND",
]
