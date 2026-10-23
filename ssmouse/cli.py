"""Command line interface for ``ssmouse``.

Commands
--------
``info``     device metadata, battery, DPI, polling rate, firmware, connection
``battery``  battery level (the Rival 3 Wireless does not report charging)
``dpi``      DPI presets and the active CPI level

Global options
--------------
``--json``        machine-readable output (JSON)
``--watch``       repeat the command periodically (``--interval`` seconds)
``--debug``       hexdump every HID report on stderr
``--list``        list detected SteelSeries devices and exit
``--fake``        replay captured data instead of using hardware (CI/demo)
``--backend``     ``auto`` (default), ``hidapi-hidraw``, ``hidapi-hid`` or
                  ``ctypes-hidraw``
``--path``        force a specific hidraw node, e.g. ``/dev/hidraw7``
``--interface``   force a specific HID interface number
``--framing``     report framing preset: ``default``, ``no-id``, ``canonical``,
                  ``short-3``, ``report-id-0``
``--retries``     command retries (default 2)
``--timeout-ms``  response timeout per attempt (default 400)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from . import __version__, cli_config
from .device import (
    ENV_BACKEND,
    ENV_FAKE,
    ENV_FRAMING,
    ENV_HIDRAW,
    ENV_INTERFACE,
    DeviceNotFoundError,
    Mouse,
)
from .firmware import FirmwareError
from .updates import UpdateError
from .protocol import FRAMING_PRESETS, ProtocolError
from .status import NOT_LINKED_MESSAGE, collect_info, require_link
from .transport import TransportError, available_backends, set_trace

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_DEVICE = 2

DEFAULT_WATCH_INTERVAL = 2.0

_DEBUG = False


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable JSON output",
    )
    common.add_argument(
        "--watch",
        action="store_true",
        default=argparse.SUPPRESS,
        help="repeat until Ctrl-C",
    )
    common.add_argument(
        "--interval",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="watch interval in seconds (default %s)" % DEFAULT_WATCH_INTERVAL,
    )
    common.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="hexdump HID reports on stderr",
    )
    common.add_argument(
        "--fake",
        action="store_true",
        default=argparse.SUPPRESS,
        help="replay captured data (no hardware, same as SSMOUSE_FAKE=1)",
    )
    common.add_argument(
        "--backend",
        choices=("auto",) + tuple(available_backends()),
        default=argparse.SUPPRESS,
        help="HID backend (default: auto)",
    )
    common.add_argument(
        "--path",
        default=argparse.SUPPRESS,
        metavar="/dev/hidrawN",
        help="force a specific hidraw device node",
    )
    common.add_argument(
        "--interface",
        default=argparse.SUPPRESS,
        metavar="N",
        help="force a specific HID interface number",
    )
    common.add_argument(
        "--framing",
        choices=tuple(sorted(FRAMING_PRESETS)),
        default=argparse.SUPPRESS,
        help="report framing preset (default: default)",
    )
    common.add_argument(
        "--retries",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="command retries (default 2)",
    )
    common.add_argument(
        "--timeout-ms",
        type=int,
        default=argparse.SUPPRESS,
        metavar="MS",
        help="response timeout per attempt (default 400)",
    )

    parser = argparse.ArgumentParser(
        prog="ssmouse",
        description="Read and configure a SteelSeries Rival 3 Wireless (battery, DPI, polling, power, lighting, buttons).",
        parents=[common],
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list detected SteelSeries devices and exit",
    )
    parser.add_argument(
        "--version", action="version", version="ssmouse %s" % __version__
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_text in (
        ("info", "device metadata, battery, DPI, polling rate, firmware"),
        ("battery", "battery level"),
        ("dpi", "DPI presets and active CPI"),
    ):
        subparsers.add_parser(name, parents=[common], help=help_text)
    cli_config.add_parsers(subparsers, common)
    return parser


def _opt(args: argparse.Namespace, name: str, default):
    return getattr(args, name, default)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False), flush=True)


def _hexdump(direction: str, data: bytes, backend: str) -> None:
    if not _DEBUG:
        return
    hexs = " ".join("%02X" % byte for byte in data)
    print("[%s %-14s %2dB] %s" % (direction, backend, len(data), hexs), file=sys.stderr)
    sys.stderr.flush()


def _install_debug(enabled: bool) -> None:
    global _DEBUG
    _DEBUG = enabled
    set_trace(_hexdump if enabled else None)


class Colors:
    """Minimal ANSI helpers; disabled automatically when not a TTY."""

    def __init__(self, stream=sys.stdout) -> None:
        self.enabled = bool(getattr(stream, "isatty", lambda: False)())

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return "\033[%sm%s\033[0m" % (code, text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)


def _format_cpi(value: Optional[int]) -> str:
    return "?" if value is None else "%d" % value


def _battery_value(battery: Optional[dict]) -> str:
    if not battery or battery.get("level") is None:
        return "unavailable"
    level = battery["level"]
    charging = battery.get("charging")
    if charging is True:
        return "%d%% (charging)" % level
    if charging is False:
        return "%d%% (not charging)" % level
    return "%d%% (charge status not reported by this device)" % level


def _battery_text(battery: Optional[dict]) -> str:
    return "Battery: " + _battery_value(battery)


def _dpi_value(dpi: Optional[dict]) -> str:
    if not dpi:
        return "unavailable"
    presets = dpi.get("presets") or []
    values = ", ".join(_format_cpi(preset.get("cpi")) for preset in presets)
    current = dpi.get("current_cpi")
    return "%s CPI (level %s/%s; presets: %s)" % (
        _format_cpi(current),
        dpi.get("active_level", "?"),
        dpi.get("level_max", "?"),
        values or "-",
    )


def _dpi_text(dpi: Optional[dict]) -> str:
    return "DPI: " + _dpi_value(dpi)


def _render_info(payload: dict, colors: Colors) -> str:
    lines = [
        colors.bold(payload.get("name") or "SteelSeries device"),
        "  vendor    : %s" % payload.get("vendor_id", "-"),
        "  product   : %s" % payload.get("product_id", "-"),
        "  model     : %s" % (payload.get("model") or "-"),
        "  serial    : %s" % (payload.get("serial_number") or "-"),
        "  firmware  : %s" % (payload.get("firmware") or "unknown"),
        "  interface : %s (if %s, usage %s)"
        % (
            payload.get("path") or "-",
            payload.get("interface_number"),
            payload.get("usage_page") or "?",
        ),
        "  transport : %s" % (payload.get("backend") or "-"),
        "  connected : %s"
        % (
            "yes"
            if payload.get("connected") is True
            else "no"
            if payload.get("connected") is False
            else "unknown"
        ),
        "  battery   : " + _battery_value(payload.get("battery")),
        "  dpi       : " + _dpi_value(payload.get("dpi")),
        "  polling   : %s Hz"
        % (payload.get("polling_rate_hz") if payload.get("polling_rate_hz") else "unknown"),
    ]
    power_mode = payload.get("power_mode")
    if power_mode:
        lines.append(
            "  power     : saving=%s movement=%s idle_light=%s idle_sleep=%s (raw)"
            % (
                power_mode.get("power_saving"),
                power_mode.get("movement_saving"),
                power_mode.get("idle_lighting"),
                power_mode.get("idle_sleep"),
            )
        )
    return "\n".join(lines)


def _render(command: str, payload: dict, colors: Colors) -> str:
    if command == "battery":
        return _battery_text(payload)
    if command == "dpi":
        return _dpi_text(payload if payload.get("available") else None)
    return _render_info(payload, colors)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def _collect_battery(mouse: Mouse) -> dict:
    require_link(mouse)
    battery = mouse.get_battery()
    return {"level": battery.level, "charging": battery.charging}


def _collect_dpi(mouse: Mouse) -> dict:
    require_link(mouse)
    info = mouse.get_dpi()
    if info is None:
        return {
            "available": False,
            "current_cpi": None,
            "active_level": None,
            "level_max": None,
            "presets": [],
            "reason": "no valid DPI response (device asleep or unsupported command)",
        }
    return {"available": True, **info.as_dict()}


# ---------------------------------------------------------------------------
# Device listing
# ---------------------------------------------------------------------------


def _list_devices(as_json: bool) -> int:
    from .device import enumerate_devices, rank_interfaces

    interfaces = enumerate_devices()
    ranked = rank_interfaces(interfaces)
    if as_json:
        _print_json({"devices": [interface.as_dict() for interface in interfaces]})
        return EXIT_OK
    if not interfaces:
        print("No SteelSeries device found (vendor 1038).", file=sys.stderr)
        return EXIT_NO_DEVICE
    print("Detected SteelSeries HID interfaces:")
    for interface in interfaces:
        marker = "*" if interface in ranked else " "
        print(
            " %s %-22s if=%-2s usage=%s  %04X:%04X  %s"
            % (
                marker,
                interface.path_str,
                interface.interface_number,
                ("0x%04X" % interface.usage_page) if interface.usage_page else "?",
                interface.vendor_id,
                interface.product_id,
                interface.product_string or "",
            )
        )
    if ranked:
        print("\n* = control-interface candidate (best first)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _apply_env_overrides(args: argparse.Namespace) -> None:
    if _opt(args, "fake", False):
        os.environ[ENV_FAKE] = "1"
    if _opt(args, "path", None):
        os.environ[ENV_HIDRAW] = str(_opt(args, "path", None))
    if _opt(args, "interface", None) is not None:
        os.environ[ENV_INTERFACE] = str(_opt(args, "interface", None))
    if _opt(args, "backend", None):
        os.environ[ENV_BACKEND] = str(_opt(args, "backend", None))
    if _opt(args, "framing", None):
        os.environ[ENV_FRAMING] = str(_opt(args, "framing", None))


def _make_mouse(args: argparse.Namespace) -> Mouse:
    mouse = Mouse.detect(
        backend=_opt(args, "backend", None),
        framing_name=_opt(args, "framing", None),
        retries=_opt(args, "retries", None),
    )
    timeout_ms = _opt(args, "timeout_ms", None)
    if timeout_ms:
        mouse.protocol.timeout_ms = max(1, int(timeout_ms))
        mouse.protocol.info_timeout_ms = max(1, int(timeout_ms))
    return mouse


def _collect(command: str, mouse: Mouse) -> dict:
    if command == "battery":
        return _collect_battery(mouse)
    if command == "dpi":
        return _collect_dpi(mouse)
    return collect_info(mouse)


def _watch(
    command: str,
    args: argparse.Namespace,
    as_json: bool,
    interval: float,
    colors: Colors,
) -> int:
    interactive = sys.stdout.isatty()
    while True:
        try:
            mouse = _make_mouse(args)
            try:
                payload = _collect(command, mouse)
            finally:
                mouse.close()
        except DeviceNotFoundError as exc:
            payload = {"error": str(exc)}
        except (TransportError, ProtocolError) as exc:
            payload = {"error": str(exc)}

        if as_json:
            print(json.dumps(payload, sort_keys=False), flush=True)
        else:
            if interactive:
                sys.stdout.write("\033[2J\033[H")
            if "error" in payload:
                print(
                    "%s  %s"
                    % (time.strftime("%H:%M:%S"), colors.red("error: %s" % payload["error"])),
                    flush=True,
                )
            else:
                print(
                    "%s  %s"
                    % (time.strftime("%H:%M:%S"), _render(command, payload, colors)),
                    flush=True,
                )
            if interactive:
                print(colors.dim("Ctrl-C to stop"), flush=True)
        try:
            time.sleep(max(0.2, interval))
        except KeyboardInterrupt:
            break
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point (Ctrl-C exits with the conventional 130)."""
    try:
        return _main_impl(argv)
    except KeyboardInterrupt:
        return 130


def _main_impl(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _install_debug(_opt(args, "debug", False))
    _apply_env_overrides(args)

    as_json = _opt(args, "json", False)

    if _opt(args, "list", False):
        return _list_devices(as_json)

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return EXIT_ERROR

    if command in cli_config.COMMANDS:
        try:
            return cli_config.run(args, lambda: _make_mouse(args), as_json)
        except DeviceNotFoundError as exc:
            print("ssmouse: %s" % exc, file=sys.stderr)
            return EXIT_NO_DEVICE
        except (TransportError, ProtocolError, ValueError, OSError, FirmwareError,
                UpdateError) as exc:
            print("ssmouse: %s" % exc, file=sys.stderr)
            return EXIT_ERROR

    watch = _opt(args, "watch", False)
    interval = _opt(args, "interval", DEFAULT_WATCH_INTERVAL)
    colors = Colors()

    if watch:
        return _watch(command, args, as_json, interval, colors)

    try:
        mouse = _make_mouse(args)
    except DeviceNotFoundError as exc:
        print("ssmouse: %s" % exc, file=sys.stderr)
        return EXIT_NO_DEVICE
    except (TransportError, ValueError) as exc:
        print("ssmouse: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    try:
        payload = _collect(command, mouse)
    except DeviceNotFoundError as exc:
        print("ssmouse: %s" % exc, file=sys.stderr)
        return EXIT_NO_DEVICE
    except (TransportError, ProtocolError) as exc:
        print("ssmouse: %s" % exc, file=sys.stderr)
        message = str(exc).lower()
        if "write failed" in message or "broken pipe" in message:
            print(
                "ssmouse: hint: this interface has no working OUT endpoint - use "
                "`ssmouse --list` and try --interface 3 / --path /dev/hidraw7; "
                "see README troubleshooting",
                file=sys.stderr,
            )
        elif NOT_LINKED_MESSAGE not in str(exc):
            print(
                "ssmouse: hint: wireless mice sleep when idle - move the mouse and retry; "
                "see README troubleshooting",
                file=sys.stderr,
            )
        return EXIT_ERROR
    finally:
        mouse.close()

    if as_json:
        _print_json(payload)
    else:
        print(_render(command, payload, colors))
        for error in payload.get("errors") or []:
            print(colors.yellow("warning: %s" % error), file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
