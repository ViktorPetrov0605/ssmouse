"""Transport backends for SteelSeries HID devices.

The app has three interchangeable transports, all offering the same small
interface (write an output report, read an input report, get/set a feature
report):

``hidapi-hidraw`` (Linux, preferred)
    The ``hidraw`` backend shipped by the PyPI ``hidapi`` package
    (``import hidraw``).  It talks to ``/dev/hidrawN`` and therefore works with
    the udev rule installed by ``install.sh`` without root privileges.

``hidapi-hid``
    The libusb backend shipped by the same ``hidapi`` package
    (``import hid``).  Used on Windows/macOS, and as a Linux fallback when
    ``/dev/hidraw*`` is unavailable.  On Linux it needs access to
    ``/dev/bus/usb``; the udev rule also tags the USB device with ``uaccess``.

``ctypes-hidraw`` (Linux, zero-dependency fallback)
    A pure ``ctypes``/``fcntl`` hidraw implementation used when the ``hidapi``
    package is not installed at all.  Slower and Linux-only, but keeps the
    tool usable in minimal environments.

All three take report bytes that are already encoded by
:class:`ssmouse.protocol.Framing`; the ``hidapi-hid`` backend is the only one
that needs the extra report-ID byte that libusb expects.
"""

from __future__ import annotations

import errno
import os
import select
import struct
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .protocol import DEFAULT_FRAMING, Framing

#: The ``hidapi`` package exposes two Cython modules with the same API.
try:  # pragma: no cover - depends on the host installation
    import hidraw as _hidapi_hidraw  # type: ignore
except Exception:  # noqa: BLE001 - any import problem means "not available"
    _hidapi_hidraw = None

try:  # pragma: no cover - depends on the host installation
    import hid as _hidapi_hid  # type: ignore
except Exception:  # noqa: BLE001
    _hidapi_hid = None


class TransportError(Exception):
    """A transport could not perform an operation."""


class TransportTimeout(TransportError):
    """No report arrived within the timeout."""


#: Optional debug hook: ``set_trace(fn)`` with ``fn(direction, data, backend)``.
_trace_hook = None


def set_trace(hook) -> None:  # noqa: ANN001 - simple callback
    """Install (or clear with ``None``) a debug trace callback."""
    global _trace_hook
    _trace_hook = hook


def _trace(direction: str, data: bytes | None, backend: str) -> None:
    if _trace_hook is not None and data is not None:
        try:
            _trace_hook(direction, bytes(data), backend)
        except Exception:  # noqa: BLE001 - tracing must never break I/O
            pass


@dataclass(frozen=True)
class HidInterface:
    """One HID interface of a device, as reported by enumeration."""

    path: bytes | str
    interface_number: Optional[int] = None
    usage_page: Optional[int] = None
    usage: Optional[int] = None
    vendor_id: int = 0
    product_id: int = 0
    product_string: str = ""
    manufacturer_string: str = ""
    serial_number: str = ""
    release_number: int = 0
    backend: str = ""

    @property
    def path_str(self) -> str:
        if isinstance(self.path, bytes):
            return self.path.decode("utf-8", "replace")
        return str(self.path)

    def as_dict(self) -> dict:
        return {
            "path": self.path_str,
            "interface_number": self.interface_number,
            "usage_page": _maybe_hex(self.usage_page),
            "usage": _maybe_hex(self.usage),
            "vendor_id": "%04X" % self.vendor_id,
            "product_id": "%04X" % self.product_id,
            "manufacturer": self.manufacturer_string or None,
            "product": self.product_string or None,
            "serial_number": self.serial_number or None,
            "release_number": "%04X" % self.release_number if self.release_number else None,
            "backend": self.backend,
        }


def _maybe_hex(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return "0x%04X" % value


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Transport(ABC):
    """Common transport interface used by :class:`ssmouse.device.Mouse`."""

    #: Human readable backend name, e.g. ``hidapi-hidraw``.
    name = "abstract"

    def __init__(self, framing: Framing = DEFAULT_FRAMING) -> None:
        self.framing = framing
        self.interface: Optional[HidInterface] = None

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    def open(self) -> None:
        """Open the underlying device handle."""

    @abstractmethod
    def close(self) -> None:
        """Close the underlying device handle (idempotent)."""

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- I/O ---------------------------------------------------------------
    @abstractmethod
    def write_output(self, report: bytes) -> int:
        """Send one output report and return the number of bytes written."""

    @abstractmethod
    def read_input(self, size: int, timeout_ms: int) -> Optional[bytes]:
        """Read one input report or return ``None`` on timeout."""

    @abstractmethod
    def get_feature(self, report_id: int, size: int) -> Optional[bytes]:
        """Read a feature report."""

    @abstractmethod
    def set_feature(self, data: bytes) -> None:
        """Send a feature report."""

    @property
    def description(self) -> str:
        if self.interface is None:
            return self.name
        return "%s (%s)" % (self.name, self.interface.path_str)


# ---------------------------------------------------------------------------
# hidapi transports
# ---------------------------------------------------------------------------


class HidApiTransport(Transport):
    """Transport built on the PyPI ``hidapi`` package.

    ``backend="hidraw"`` uses the Linux ``/dev/hidraw*`` backend,
    ``backend="hid"`` the cross-platform libusb backend.
    """

    def __init__(
        self,
        interface: HidInterface,
        framing: Framing = DEFAULT_FRAMING,
        backend: str = "hidraw",
    ) -> None:
        super().__init__(framing)
        if backend not in ("hidraw", "hid"):
            raise ValueError("unknown hidapi backend: %r" % (backend,))
        module = _hidapi_hidraw if backend == "hidraw" else _hidapi_hid
        if module is None:
            raise TransportError(
                "hidapi %r backend is not available (is the hidapi package installed?)"
                % backend
            )
        self._module = module
        self._backend = backend
        self._device = None
        self.interface = HidInterface(
            path=interface.path,
            interface_number=interface.interface_number,
            usage_page=interface.usage_page,
            usage=interface.usage,
            vendor_id=interface.vendor_id,
            product_id=interface.product_id,
            product_string=interface.product_string,
            manufacturer_string=interface.manufacturer_string,
            serial_number=interface.serial_number,
            release_number=interface.release_number,
            backend="hidapi-" + backend,
        )
        self.name = "hidapi-" + backend

    def open(self) -> None:
        if self._device is not None:
            return
        device = self._module.device()
        try:
            device.open_path(self.interface.path)
        except Exception as exc:  # noqa: BLE001 - hidapi raises OSError
            raise TransportError(
                "cannot open %s: %s" % (self.interface.path_str, exc)
            ) from exc
        # Match the timeout semantics of the other backends.
        try:
            device.set_nonblocking(True)
        except Exception:  # noqa: BLE001 - optional
            pass
        self._device = device

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self._device = None

    def _require_open(self):
        if self._device is None:
            raise TransportError("transport is not open")
        return self._device

    def _wire_bytes(self, report: bytes) -> bytes:
        """hidapi's libusb backend always wants a leading report-ID byte."""
        if self._backend == "hid" and self.framing.report_id is None:
            return b"\x00" + bytes(report)
        return bytes(report)

    def write_output(self, report: bytes) -> int:
        device = self._require_open()
        try:
            written = device.write(self._wire_bytes(report))
        except OSError as exc:
            raise TransportError(
                "write failed on %s: %s" % (self.interface.path_str, exc)
            ) from exc
        if written is None or written < 0:
            raise TransportError("write failed on %s" % self.interface.path_str)
        _trace("TX", report, self.name)
        return int(written)

    def read_input(self, size: int, timeout_ms: int) -> Optional[bytes]:
        device = self._require_open()
        try:
            data = device.read(size, timeout_ms)
        except OSError as exc:
            raise TransportError(
                "read failed on %s: %s" % (self.interface.path_str, exc)
            ) from exc
        if not data:
            return None
        payload = bytes(data[:size])
        _trace("RX", payload, self.name)
        return payload

    def get_feature(self, report_id: int, size: int) -> Optional[bytes]:
        device = self._require_open()
        try:
            data = device.get_feature_report(report_id, size)
        except OSError as exc:
            raise TransportError(
                "feature read failed on %s: %s" % (self.interface.path_str, exc)
            ) from exc
        if not data:
            return None
        return bytes(data)

    def set_feature(self, data: bytes) -> None:
        device = self._require_open()
        try:
            device.send_feature_report(bytes(data))
        except OSError as exc:
            raise TransportError(
                "feature write failed on %s: %s" % (self.interface.path_str, exc)
            ) from exc


# ---------------------------------------------------------------------------
# Linux ctypes hidraw fallback (no hidapi required)
# ---------------------------------------------------------------------------

HIDIOCGRAWINFO = 0x80084803
HIDIOCGRDESCSIZE = 0x80044801
HIDIOCGRDESC = 0x90044802


def _hidioctl_nr(nr: int, size: int) -> int:
    return (3 << 30) | (0x80 << 24) | (ord("H") << 8) | nr | ((size & 0x3FFF) << 16)


class RawHidrawTransport(Transport):
    """Pure ``ctypes`` Linux hidraw transport (fallback when hidapi is absent)."""

    name = "ctypes-hidraw"

    def __init__(
        self, path: str, framing: Framing = DEFAULT_FRAMING, interface: HidInterface | None = None
    ) -> None:
        super().__init__(framing)
        if not sys.platform.startswith("linux"):
            raise TransportError("the ctypes hidraw fallback is Linux-only")
        self._path = str(path)
        self._fd: Optional[int] = None
        self.interface = interface or HidInterface(path=self._path, backend=self.name)

    # -- ioctl helpers -----------------------------------------------------
    def _ioctl(self, request: int, buffer) -> int:  # noqa: ANN001 - buffer protocol
        import fcntl

        if self._fd is None:
            raise TransportError("transport is not open")
        return fcntl.ioctl(self._fd, request, buffer)

    def raw_info(self) -> dict:
        import ctypes

        buf = ctypes.create_string_buffer(12)
        self._ioctl(HIDIOCGRAWINFO, buf)
        bustype, vendor, product = struct.unpack_from("<IHH", buf.raw)
        return {"bustype": bustype, "vendor": vendor, "product": product}

    def report_descriptor(self) -> bytes:
        import ctypes

        size_buf = ctypes.create_string_buffer(4)
        self._ioctl(HIDIOCGRDESCSIZE, size_buf)
        size = struct.unpack("<I", size_buf.raw)[0]
        if size <= 0 or size > 8192:
            return b""
        buf = ctypes.create_string_buffer(4 + size)
        struct.pack_into("<I", buf, 0, size)
        self._ioctl(HIDIOCGRDESC, buf)
        return bytes(buf.raw[4 : 4 + size])

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise TransportError("cannot open %s: %s" % (self._path, exc)) from exc

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    # -- I/O ---------------------------------------------------------------
    def write_output(self, report: bytes) -> int:
        if self._fd is None:
            raise TransportError("transport is not open")
        try:
            written = os.write(self._fd, bytes(report))
        except OSError as exc:
            raise TransportError("write failed on %s: %s" % (self._path, exc)) from exc
        _trace("TX", report, self.name)
        return written

    def read_input(self, size: int, timeout_ms: int) -> Optional[bytes]:
        if self._fd is None:
            raise TransportError("transport is not open")
        try:
            ready, _, _ = select.select([self._fd], [], [], max(0.0, timeout_ms / 1000.0))
        except OSError as exc:
            raise TransportError("select failed on %s: %s" % (self._path, exc)) from exc
        if not ready:
            return None
        try:
            data = os.read(self._fd, size)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise TransportError("read failed on %s: %s" % (self._path, exc)) from exc
        if not data:
            return None
        payload = bytes(data[:size])
        _trace("RX", payload, self.name)
        return payload

    def get_feature(self, report_id: int, size: int) -> Optional[bytes]:
        import ctypes

        if self._fd is None:
            raise TransportError("transport is not open")
        if report_id:
            # hidraw ioctl expects the report-ID byte in the buffer.
            length = size + 1
        else:
            length = size
        buf = ctypes.create_string_buffer(length)
        if report_id:
            buf.raw = bytes([report_id]) + b"\x00" * size
        try:
            count = self._ioctl(_hidioctl_nr(0x07, length), buf)
        except OSError as exc:
            raise TransportError(
                "feature read failed on %s: %s" % (self._path, exc)
            ) from exc
        if not count:
            return None
        return bytes(buf.raw[:count])

    def set_feature(self, data: bytes) -> None:
        import ctypes

        if self._fd is None:
            raise TransportError("transport is not open")
        payload = bytes(data)
        buf = ctypes.create_string_buffer(payload, len(payload))
        try:
            self._ioctl(_hidioctl_nr(0x06, len(payload)), buf)
        except OSError as exc:
            raise TransportError(
                "feature write failed on %s: %s" % (self._path, exc)
            ) from exc


# ---------------------------------------------------------------------------
# Factory / backend capabilities
# ---------------------------------------------------------------------------


def hidapi_available() -> bool:
    return _hidapi_hidraw is not None or _hidapi_hid is not None


def available_backends() -> list[str]:
    """Return backend names usable on this host, preferred first."""
    backends: list[str] = []
    if _hidapi_hidraw is not None:
        backends.append("hidapi-hidraw")
    if _hidapi_hid is not None:
        backends.append("hidapi-hid")
    if sys.platform.startswith("linux"):
        backends.append("ctypes-hidraw")
    return backends


def default_backend() -> str:
    backends = available_backends()
    if not backends:
        raise TransportError(
            "no HID backend available: install the 'hidapi' package "
            "(pip install hidapi)"
        )
    return backends[0]


def make_hidapi_transport(
    interface: HidInterface, framing: Framing, backend: str = "auto"
) -> Transport:
    """Create a transport for *interface* using the requested/available backend."""
    if backend in ("auto", "", None):
        ordered = available_backends()
    else:
        ordered = [backend]
    last_error: Optional[Exception] = None
    for candidate in ordered:
        try:
            if candidate == "hidapi-hidraw":
                return HidApiTransport(interface, framing, backend="hidraw")
            if candidate == "hidapi-hid":
                return HidApiTransport(interface, framing, backend="hid")
            if candidate == "ctypes-hidraw":
                return RawHidrawTransport(
                    interface.path_str, framing, interface=interface
                )
            raise TransportError("unknown backend %r" % (candidate,))
        except TransportError as exc:
            last_error = exc
            continue
    raise last_error or TransportError("could not create a transport")


__all__ = [
    "TransportError",
    "TransportTimeout",
    "HidInterface",
    "Transport",
    "HidApiTransport",
    "RawHidrawTransport",
    "hidapi_available",
    "available_backends",
    "default_backend",
    "make_hidapi_transport",
]
