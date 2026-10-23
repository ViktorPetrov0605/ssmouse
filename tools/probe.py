#!/usr/bin/env python3
"""Hardware probe for the SteelSeries Rival 3 Wireless protocol.

This is the experiment tool used while recovering the protocol.  It is useful
for re-verifying every command against a live device, trying framing variants,
and checking interface selection after a firmware update.

Examples::

    python tools/probe.py --list
    python tools/probe.py --commands battery,dpi,firmware
    python tools/probe.py --framing canonical --commands battery
    python tools/probe.py --raw aa01 --framing default
    python tools/probe.py --descriptor

Requires the ``ssmouse`` package (run from the app directory) and, for real
hardware, the udev rule installed by ``install.sh``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running from the repository without installing the package.
APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from ssmouse.device import (  # noqa: E402
    enumerate_devices,
    rank_interfaces,
)
from ssmouse.protocol import FRAMING_PRESETS, Protocol  # noqa: E402
from ssmouse.transport import (  # noqa: E402
    TransportError,
    available_backends,
    make_hidapi_transport,
)


def hexs(data: bytes | None, limit: int = 32) -> str:
    if data is None:
        return "(no response)"
    shown = data[:limit]
    text = " ".join("%02X" % byte for byte in shown)
    if len(data) > limit:
        text += " ... (%d bytes)" % len(data)
    return text


def list_interfaces() -> int:
    interfaces = enumerate_devices()
    if not interfaces:
        print("No SteelSeries device found (vendor 1038).")
        return 2
    ranked = rank_interfaces(interfaces)
    print("SteelSeries HID interfaces:")
    for interface in interfaces:
        marker = "*" if interface in ranked else " "
        print(
            " %s %-20s if=%-2s usage=%-7s %04X:%04X %s [%s]"
            % (
                marker,
                interface.path_str,
                interface.interface_number,
                ("0x%04X" % interface.usage_page) if interface.usage_page else "?",
                interface.vendor_id,
                interface.product_id,
                interface.product_string or "",
                interface.backend or "?",
            )
        )
    return 0


def dump_descriptors() -> int:
    """Print report descriptors for the detected hidraw nodes (Linux)."""
    interface_list = enumerate_devices()
    if not interface_list:
        print("No SteelSeries device found.", file=sys.stderr)
        return 2
    for interface in interface_list:
        print("== %s (if %s)" % (interface.path_str, interface.interface_number))
        if interface.path_str.startswith("/dev/hidraw"):
            node = interface.path_str.rsplit("/", 1)[-1]
            sysfs = Path("/sys/class/hidraw") / node / "device/report_descriptor"
            try:
                data = sysfs.read_bytes()
            except OSError as exc:
                print("   cannot read %s: %s" % (sysfs, exc))
                continue
            print("   %s" % hexs(data, limit=256))
        else:
            print("   descriptor dump is Linux-only; use --probe to identify")
    return 0


COMMAND_MAP = {
    "battery": (lambda p: p.build_battery_request(), lambda p, d: p.parse_battery_response(d).as_dict()),
    "dpi": (lambda p: p.build_dpi_request(), lambda p, d: p.parse_dpi_response(d)),
    "dpi-level": (lambda p: p.build_dpi_level_request(), lambda p, d: p.parse_dpi_level_response(d)),
    "polling": (lambda p: p.build_polling_rate_request(), lambda p, d: p.parse_polling_rate_response(d)),
    "firmware": (lambda p: p.build_firmware_request(), lambda p, d: p.parse_firmware_response(d)),
    "rf-status": (lambda p: p.build_rf_status_request(), lambda p, d: p.parse_rf_status_response(d)),
    "power-mode": (lambda p: p.build_power_mode_request(), lambda p, d: p.parse_power_mode_response(d)),
}


def probe_commands(commands: list[str], framing_name: str, backend: str, retries: int) -> int:
    framing = FRAMING_PRESETS[framing_name]
    protocol = Protocol(framing=framing, retries=retries)
    candidates = rank_interfaces(enumerate_devices())
    if not candidates:
        print("No control interface candidate found.", file=sys.stderr)
        return 2

    exit_code = 0
    for interface in candidates:
        print(
            "== probing %s (if %s, usage %s) framing=%s"
            % (
                interface.path_str,
                interface.interface_number,
                ("0x%04X" % interface.usage_page) if interface.usage_page else "?",
                framing_name,
            )
        )
        try:
            transport = make_hidapi_transport(interface, framing, backend)
            transport.open()
        except TransportError as exc:
            print("   open failed: %s" % exc)
            continue
        try:
            for name in commands:
                builder, parser = COMMAND_MAP[name]
                request = builder(protocol)
                try:
                    transport.write_output(request)
                except TransportError as exc:
                    print("   %-12s write failed: %s" % (name, exc))
                    exit_code = 1
                    break
                response = transport.read_input(64, protocol.timeout_ms)
                print("   %-12s TX %s" % (name, hexs(request, limit=16)))
                print("   %-12s RX %s" % ("", hexs(response)))
                if response is not None:
                    try:
                        parsed = parser(protocol, response)
                    except Exception as exc:  # noqa: BLE001 - probe must not crash
                        parsed = "parse error: %s" % exc
                    print("   %-12s -> %s" % ("", parsed))
                # Respect the vendor's 30 ms command spacing.
                time.sleep(protocol.min_command_interval_s)
        finally:
            transport.close()
        if exit_code == 0:
            break
    return exit_code


def probe_raw(hex_command: str, framing_name: str, backend: str) -> int:
    try:
        payload = bytes.fromhex(hex_command.replace(" ", ""))
    except ValueError as exc:
        print("error: --raw expects hex bytes, e.g. aa01 (%s)" % exc, file=sys.stderr)
        return 2
    framing = FRAMING_PRESETS[framing_name]
    protocol = Protocol(framing=framing)
    candidates = rank_interfaces(enumerate_devices())
    if not candidates:
        print("No control interface candidate found.", file=sys.stderr)
        return 2
    for interface in candidates:
        print("== raw %s on %s" % (hex_command, interface.path_str))
        try:
            transport = make_hidapi_transport(interface, framing, backend)
            transport.open()
        except TransportError as exc:
            print("   open failed: %s" % exc)
            continue
        try:
            request = framing.encode(payload)
            print("   TX %s" % hexs(request))
            transport.write_output(request)
            response = transport.read_input(64, protocol.info_timeout_ms)
            print("   RX %s" % hexs(response))
            return 0 if response is not None else 1
        except TransportError as exc:
            print("   failed: %s" % exc)
        finally:
            transport.close()
    return 1


#: Dongle pairing sequence recovered from SteelSeries GG's Electron modules:
#: write 0x71 (pair_dongle), poll rf_status (0xE8) every 2 s for up to 20 s,
#: and write 0x73 (reset_wireless) on failure.
PAIR_POLL_INTERVAL_S = 2.0
PAIR_TIMEOUT_S = 20.0


def _write_command(transport, request: bytes) -> None:
    transport.write_output(request)


def _read_rf_status(transport, protocol) -> bool | None:
    """Send E8 (fallback A8) and parse rf_status; None on no response."""
    for request in protocol.build_rf_status_requests():
        try:
            transport.write_output(request)
        except TransportError:
            return None
        response = transport.read_input(64, protocol.info_timeout_ms)
        if response is None:
            continue
        status = protocol.parse_rf_status_response(response)
        if status is not None:
            return status
    return None


def pair_device(framing_name: str, backend: str, timeout_s: float) -> int:
    """Try the vendor pairing flow: write 0x71, poll rf_status every 2 s.

    Pairing only works while the mouse is in pairing mode (its LED blinks).
    A failed attempt sends reset_wireless (0x73), mirroring SteelSeries GG.
    This is a *write* operation and therefore lives in the probe tool, not in
    the read-only ``ssmouse`` CLI.
    """
    framing = FRAMING_PRESETS[framing_name]
    protocol = Protocol(framing=framing)
    candidates = rank_interfaces(enumerate_devices())
    if not candidates:
        print("No control interface candidate found.", file=sys.stderr)
        return 2
    interface = candidates[0]
    print(
        "== pairing on %s (if %s, usage %s) framing=%s"
        % (
            interface.path_str,
            interface.interface_number,
            ("0x%04X" % interface.usage_page) if interface.usage_page else "?",
            framing_name,
        )
    )
    try:
        transport = make_hidapi_transport(interface, framing, backend)
        transport.open()
    except TransportError as exc:
        print("open failed: %s" % exc, file=sys.stderr)
        return 1
    try:
        status = _read_rf_status(transport, protocol)
        print("   rf_status before: %s" % ("connected" if status else status))
        print("   TX pair: 71 00 ... (put the mouse in pairing mode now)")
        _write_command(transport, protocol.build_pair_dongle_request())
        deadline = time.time() + max(PAIR_POLL_INTERVAL_S, timeout_s)
        consecutive = 0
        paired = False
        while time.time() < deadline:
            time.sleep(PAIR_POLL_INTERVAL_S)
            status = _read_rf_status(transport, protocol)
            print(
                "   rf_status: %s (t=%.1fs)"
                % ("connected" if status else status, timeout_s - (deadline - time.time()))
            )
            if status is True:
                consecutive += 1
                if consecutive >= 2:
                    paired = True
                    break
            else:
                consecutive = 0
        if paired:
            print("PAIRED: rf_status == 4 twice consecutively")
            return 0
        print("pairing timed out; sending reset_wireless (73)")
        try:
            _write_command(transport, protocol.build_reset_wireless_request())
        except TransportError as exc:
            print("   reset failed: %s" % exc)
        return 1
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe the SteelSeries mouse protocol on real hardware."
    )
    parser.add_argument("--list", action="store_true", help="list HID interfaces")
    parser.add_argument(
        "--descriptor", action="store_true", help="dump Linux report descriptors"
    )
    parser.add_argument(
        "--commands",
        default="battery,dpi,dpi-level,polling,firmware,rf-status,power-mode",
        help="comma separated commands to probe (default: all)",
    )
    parser.add_argument(
        "--framing",
        choices=tuple(sorted(FRAMING_PRESETS)),
        default="default",
        help="report framing preset",
    )
    parser.add_argument(
        "--backend",
        choices=("auto",) + tuple(available_backends()),
        default="auto",
    )
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--raw", metavar="HEX", help="send an arbitrary hex command")
    parser.add_argument(
        "--pair",
        action="store_true",
        help="try pairing the mouse (write 0x71, poll rf_status; write op)",
    )
    parser.add_argument(
        "--pair-timeout",
        type=float,
        default=PAIR_TIMEOUT_S,
        metavar="SECONDS",
        help="pairing poll timeout (default 20)",
    )
    args = parser.parse_args(argv)

    if args.list:
        return list_interfaces()
    if args.descriptor:
        return dump_descriptors()
    if args.raw:
        return probe_raw(args.raw, args.framing, args.backend)
    if args.pair:
        return pair_device(args.framing, args.backend, args.pair_timeout)

    commands = [item.strip() for item in args.commands.split(",") if item.strip()]
    unknown = [item for item in commands if item not in COMMAND_MAP]
    if unknown:
        print(
            "error: unknown command(s) %s (choose from %s)"
            % (", ".join(unknown), ", ".join(COMMAND_MAP)),
            file=sys.stderr,
        )
        return 2
    return probe_commands(commands, args.framing, args.backend, args.retries)


if __name__ == "__main__":
    sys.exit(main())
