"""Configuration commands for the ``ssmouse`` CLI (everything that writes).

Each change is written to the mouse, stored in its flash (``--no-save``
skips that; lighting and button mappings are then lost at power-off) and
recorded in the profile (``ssmouse profile path``).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Callable

from . import protocol
from .bindings import (
    ACTION_LABELS,
    DEFAULT_BINDINGS,
    MEDIA_KEYS,
    TRIGGER_LABELS,
    TRIGGERS,
    describe_binding,
    parse_binding,
)
from .device import Mouse
from .lighting import LightingEffect, format_color, parse_color
from .profile import ALL_SECTIONS, Profile, apply_profile, default_path
from .status import require_link

COMMANDS = ("set", "light", "bind", "profile", "save", "reset", "pair", "firmware")


def _on_off(text: str) -> bool:
    value = text.strip().lower()
    if value in ("on", "yes", "true", "1", "enable", "enabled"):
        return True
    if value in ("off", "no", "false", "0", "disable", "disabled"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def _duration_s(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(s|sec|m|min)?\s*", text.lower())
    if not match:
        raise argparse.ArgumentTypeError("use seconds or minutes, e.g. 300 or 5m")
    value = int(match.group(1))
    return value * 60 if match.group(2) in ("m", "min") else value


def _colors(text: str) -> list[tuple[int, int, int]]:
    """``red``, ``ff0000,0000ff`` or a single ``255,0,0`` triple."""
    parts = [part for part in re.split(r"[\s,;/]+", text.strip()) if part]
    if len(parts) == 3 and all(re.fullmatch(r"\d{1,3}", part) for part in parts):
        parts = [",".join(parts)]
    try:
        return [parse_color(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _cpi_list(text: str) -> list[int]:
    try:
        return [int(part) for part in text.replace(" ", "").split(",") if part]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma separated CPI values, e.g. 400,800,1600") from exc


def add_parsers(subparsers, common: argparse.ArgumentParser) -> None:
    sp = subparsers.add_parser("set", parents=[common], help="change DPI, polling rate, power")
    what = sp.add_subparsers(dest="setting", required=True, metavar="SETTING")
    p = what.add_parser("dpi", parents=[common], help="DPI presets, e.g. 400,800,1600")
    p.add_argument("presets", type=_cpi_list)
    p.add_argument("--active", type=int, default=None, help="preset to select (1-based)")
    p = what.add_parser("dpi-level", parents=[common], help="select a stored DPI preset")
    p.add_argument("level", type=int)
    p = what.add_parser("polling", parents=[common], help="report rate in Hz")
    p.add_argument("hz", type=int, choices=sorted(protocol.POLLING_RATE_LEVEL))
    p = what.add_parser("sleep", parents=[common], help="sleep timer, e.g. 5m (1-20 min)")
    p.add_argument("duration", type=_duration_s)
    p = what.add_parser("sensor-rest", parents=[common], help="lower sensor power when idle")
    p.add_argument("state", type=_on_off)
    p = what.add_parser("high-efficiency", parents=[common],
                        help="high-efficiency mode (125 Hz, longest battery life)")
    p.add_argument("state", type=_on_off)
    p = what.add_parser("debounce", parents=[common], help="button debounce in ms")
    p.add_argument("make_ms", type=int)
    p.add_argument("break_ms", type=int)

    lp = subparsers.add_parser("light", parents=[common], help="wheel LED effect")
    lp.add_argument("mode", choices=("steady", "colorshift", "breathe", "reactive", "off", "preview"))
    lp.add_argument("colors", nargs="?", type=_colors,
                    help="colours: rrggbb, r,g,b or names; several for colorshift/breathe")
    lp.add_argument("--duration", type=int, default=None, metavar="MS", help="cycle length")
    lp.add_argument("--flash", type=parse_color, default=None, metavar="COLOR",
                    help="reactive: colour shown on click (default white)")
    lp.add_argument("--fade", type=int, default=None, metavar="MS",
                    help="reactive: fade-back time (default 500)")

    bp = subparsers.add_parser("bind", parents=[common], help="remap buttons and the wheel")
    bp.add_argument("control", nargs="?", choices=TRIGGERS)
    bp.add_argument("action", nargs="?",
                    help="left, right, middle, back, forward, dpi, scroll-up, scroll-down, "
                         "disabled, key:<combo> (e.g. key:ctrl+c), media:<%s>"
                         % "|".join(MEDIA_KEYS))
    bp.add_argument("--reset", action="store_true", help="restore the default mapping")

    pp = subparsers.add_parser("profile", parents=[common], help="show/apply/export settings")
    pp.add_argument("action", choices=("show", "apply", "path", "export", "import"))
    pp.add_argument("file", nargs="?", type=Path)

    subparsers.add_parser("save", parents=[common], help="store settings in the mouse's flash")
    for parser in (lp, bp, pp, *what.choices.values()):
        parser.add_argument(
            "--no-save", action="store_true",
            help="do not store the change in the mouse's flash (lost at power-off)")
    rp = subparsers.add_parser("reset", parents=[common], help="restore factory settings")
    rp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    fw = subparsers.add_parser("firmware", parents=[common],
                               help="show firmware versions or install an update")
    fw.add_argument("action", nargs="?", choices=("status", "update"), default="status")
    fw.add_argument("folder", nargs="?", type=Path,
                    help="update: the firmware/272111664 folder of a SteelSeries GG install")
    fw.add_argument("--dry-run", action="store_true", help="validate and show the plan only")
    fw.add_argument("--force", action="store_true",
                    help="also flash when the installed firmware is the same or newer")
    fw.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    subparsers.add_parser("pair", parents=[common],
                          help="pair the mouse with the receiver (put it in pairing mode first)")


def _show_profile(profile: Profile, as_json: bool) -> None:
    if as_json:
        print(json.dumps(profile.as_dict(), indent=2))
        return
    if profile.dpi_presets:
        print("dpi       : %s (active %d)" % (", ".join(map(str, profile.dpi_presets)),
                                              profile.dpi_active or 1))
    if profile.polling_hz:
        print("polling   : %d Hz" % profile.polling_hz)
    power = profile.power
    if power.sleep_s is not None:
        print("sleep     : %d min" % (power.sleep_s // 60))
    if power.sensor_rest is not None:
        print("sensor rest: %s" % ("on" if power.sensor_rest else "off"))
    if power.high_efficiency is not None:
        print("high efficiency: %s" % ("on" if power.high_efficiency else "off"))
    if profile.lighting:
        light = profile.lighting
        print("lighting  : %s %s" % (light.mode, " ".join(format_color(c) for c in light.colors)))
    for trigger in TRIGGERS:
        spec = profile.bindings.get(trigger, DEFAULT_BINDINGS[trigger])
        print("  %-22s %s" % (TRIGGER_LABELS[trigger], describe_binding(spec)))
    if profile.debounce:
        print("debounce  : make %d ms, break %d ms" % profile.debounce)
    print("(stored in %s)" % default_path())


def _light_effect(args: argparse.Namespace, previous) -> LightingEffect:
    colors = args.colors or (previous.colors if previous else None)
    kwargs = {"mode": args.mode}
    if colors:
        kwargs["colors"] = colors
    if args.duration is not None:
        kwargs["duration_ms"] = args.duration
    if args.mode == "reactive":
        if args.flash is not None:
            kwargs["react_color"] = args.flash
        if args.fade is not None:
            kwargs["react_ms"] = args.fade
    if args.mode == "steady" and colors:
        kwargs["colors"] = colors[:1]
    return LightingEffect(**kwargs)


def run(args: argparse.Namespace, make_mouse: Callable[[], Mouse], as_json: bool) -> int:
    """Execute a configuration command; returns the process exit code."""
    command = args.command
    profile = Profile.load()

    if command == "profile" and args.action in ("show", "path", "export"):
        if args.action == "path":
            print(default_path())
        elif args.action == "export":
            if not args.file:
                raise ValueError("profile export needs a file name")
            profile.save(args.file)
            print("exported to %s" % args.file)
        else:
            _show_profile(profile, as_json)
        return 0
    if command == "bind" and not args.control and not args.reset:
        for trigger in TRIGGERS:
            spec = profile.bindings.get(trigger, DEFAULT_BINDINGS[trigger])
            print("%-11s %s" % (trigger, describe_binding(spec)))
        print("\nactions: %s, key:<combo>, media:<%s>"
              % (", ".join(ACTION_LABELS), "|".join(MEDIA_KEYS)))
        return 0
    if command == "firmware":
        return _firmware(args, make_mouse, as_json)
    if command == "reset" and not args.yes:
        answer = input("Restore the mouse's factory settings? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1

    # Validate before touching the hardware.
    if command == "profile" and args.action == "import":
        if not args.file:
            raise ValueError("profile import needs a file name")
        profile = Profile.from_dict(json.loads(args.file.read_text(encoding="utf-8")))
    if command == "bind" and args.control:
        if not args.action and not args.reset:
            raise ValueError("bind needs an action (or --reset)")
        if args.action:
            parse_binding(args.action)
    effect = None
    if command == "light" and args.mode != "preview":
        effect = _light_effect(args, profile.lighting)

    mouse = make_mouse()
    try:
        if command != "pair":  # pairing is how an unlinked mouse gets linked
            require_link(mouse)
        message = _execute(args, mouse, profile, effect)
        persist = not getattr(args, "no_save", False)
        writes = command in ("set", "light", "bind") or (
            command == "profile" and args.action in ("apply", "import"))
        if writes and persist and not (command == "light" and args.mode == "preview"):
            mouse.save_to_flash()
    finally:
        mouse.close()
    if as_json:
        print(json.dumps({"ok": True, "message": message, "profile": profile.as_dict()}))
    else:
        print(message)
    return 0


def _execute(args, mouse: Mouse, profile: Profile, effect) -> str:
    command = args.command
    if command == "set":
        setting = args.setting
        if setting == "dpi":
            active = args.active or min(profile.dpi_active or 1, len(args.presets))
            used = mouse.set_dpi(args.presets, active)
            profile.dpi_presets, profile.dpi_active = used, active
            message = "DPI presets %s, active %d (%d CPI)" % (
                ", ".join(map(str, used)), active, used[active - 1])
        elif setting == "dpi-level":
            mouse.set_active_dpi_level(args.level)
            if profile.dpi_presets:
                profile.dpi_active = args.level
            message = "DPI preset %d selected" % args.level
        elif setting == "polling":
            mouse.set_polling_rate(args.hz)
            profile.polling_hz = args.hz
            message = "polling rate %d Hz" % args.hz
        elif setting == "debounce":
            mouse.set_debounce(args.make_ms, args.break_ms)
            profile.debounce = (args.make_ms, args.break_ms)
            message = "debounce make %d ms, break %d ms" % profile.debounce
        else:
            if setting == "sleep":
                if not 60 <= args.duration <= 1200:
                    raise ValueError("sleep timer must be 1..20 minutes")
                profile.power.sleep_s = args.duration
                message = "sleep after %d min idle" % (args.duration // 60)
            elif setting == "sensor-rest":
                profile.power.sensor_rest = args.state
                message = "sensor rest %s" % ("on" if args.state else "off")
            else:
                profile.power.high_efficiency = args.state
                message = "high-efficiency mode %s" % ("on (125 Hz)" if args.state else "off")
            apply_profile(mouse, profile, ("power", "polling") if setting == "high-efficiency"
                          else ("power",))
        profile.save()
        return message
    if command == "light":
        if args.mode == "preview":
            if not args.colors:
                raise ValueError("preview needs a colour")
            mouse.set_live_color(args.colors[0])
            return "showing %s (not stored; `ssmouse profile apply` restores the effect)" % (
                format_color(args.colors[0]))
        mouse.set_lighting(effect)
        profile.lighting = effect
        profile.save()
        return "lighting: %s %s" % (effect.mode, " ".join(format_color(c) for c in effect.colors))
    if command == "bind":
        if args.reset and not args.control:
            profile.bindings = {}
        elif args.reset:
            profile.bindings.pop(args.control, None)
        else:
            profile.bindings[args.control] = args.action.strip().lower()
        mouse.set_bindings(profile.bindings)
        profile.save()
        if args.control:
            spec = profile.bindings.get(args.control, DEFAULT_BINDINGS[args.control])
            return "%s -> %s" % (TRIGGER_LABELS[args.control], describe_binding(spec))
        return "all buttons restored to defaults"
    if command == "profile":  # apply / import
        applied = apply_profile(mouse, profile, ALL_SECTIONS, persist=False)
        if args.action == "import":
            profile.save()
        return "applied: %s" % (", ".join(applied) or "nothing configured")
    if command == "save":
        mouse.save_to_flash()
        return "settings saved to the mouse's flash"
    if command == "reset":
        mouse.reset_profile()
        Profile().save()  # forget customisations too
        return "factory settings restored"
    if command == "pair":
        print("pairing... switch the mouse off and on again in 2.4 GHz mode now")
        return "paired" if mouse.pair() else "pairing timed out (receiver reset)"
    raise ValueError("unknown command %s" % command)


def _firmware(args, make_mouse: Callable[[], Mouse], as_json: bool) -> int:
    from . import firmware

    package = None
    if args.action == "update":
        if not args.folder:
            raise ValueError("firmware update needs the GG firmware folder "
                             "(…/SteelSeries GG/apps/engine/firmware/272111664)")
        package = firmware.load_package(args.folder)
    mouse = make_mouse()
    try:
        if args.action == "status":
            status = firmware.read_status(mouse)
            if as_json:
                print(json.dumps(status.as_dict()))
            else:
                print("mouse     : %s" % (status.mouse or "unknown (mouse asleep?)"))
                print("receiver  : %s" % (status.receiver or "unknown"))
                print("latest    : %s (SteelSeries GG 122)" % status.latest_known)
                verdict = {True: "up to date", False: "an update is available",
                           None: "unknown"}[status.up_to_date]
                print("status    : %s" % verdict)
            return 0
        steps = firmware.update(mouse, package, force=args.force, dry_run=True)
        writes = sum(1 for step in steps if step.kind.startswith("write"))
        seconds = sum(step.seconds for step in steps) + writes * firmware.UPDATE_INTERVAL_S
        print("firmware %s from %s: %d commands, about %d s"
              % (package.version, package.source, writes, seconds))
        if args.dry_run:
            return 0
        if not args.yes:
            print("Do NOT switch off the mouse or unplug the receiver while updating.")
            answer = input("Type 'flash' to install firmware %s: " % package.version)
            if answer.strip().lower() != "flash":
                print("cancelled")
                return 1
        last = [-1]

        def progress(percent: int) -> None:
            if percent != last[0]:
                last[0] = percent
                print("\r  %3d%%" % percent, end="", flush=True)

        firmware.update(mouse, package, force=args.force, progress=progress)
        print("\nupdate sent; the mouse and receiver restart now. "
              "Check with `ssmouse firmware` in a few seconds.")
        return 0
    finally:
        mouse.close()
