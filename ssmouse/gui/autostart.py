"""Start-on-login via an XDG autostart entry (Linux desktops)."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path


def supported() -> bool:
    return sys.platform.startswith("linux")


def entry_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(config) / "autostart" / "ssmouse-gui.desktop"


def launch_command() -> str:
    exe = shutil.which("ssmouse-gui")
    if exe:
        return "%s --minimized" % shlex.quote(exe)
    return "%s -m ssmouse.gui --minimized" % shlex.quote(sys.executable)


def is_enabled() -> bool:
    return entry_path().is_file()


def set_enabled(enabled: bool) -> None:
    path = entry_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ssmouse\n"
        "Comment=SteelSeries mouse battery in the system tray\n"
        "Exec=%s\n"
        "Icon=input-mouse\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n" % launch_command()
    )
