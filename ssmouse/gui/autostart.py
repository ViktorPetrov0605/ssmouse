"""Start on login: XDG autostart (Linux), Run key (Windows), LaunchAgent (macOS)."""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import sys
from pathlib import Path

LABEL = "io.github.viktorpetrov0605.ssmouse"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ssmouse"


def supported() -> bool:
    return sys.platform.startswith(("linux", "win")) or sys.platform == "darwin"


def launch_args() -> list[str]:
    """Command that starts the tray app hidden."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable, "--minimized"]
    exe = shutil.which("ssmouse-gui")
    if exe:
        return [exe, "--minimized"]
    python = sys.executable
    if sys.platform.startswith("win"):
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw) if pythonw.exists() else python
    return [python, "-m", "ssmouse.gui", "--minimized"]


def launch_command() -> str:
    return " ".join(shlex.quote(arg) for arg in launch_args())


def entry_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(config) / "autostart" / "ssmouse-gui.desktop"


# -- Windows --------------------------------------------------------------------


def _win_get() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def _win_set(enabled: bool) -> None:
    import subprocess
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                              subprocess.list2cmdline(launch_args()))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


# -- public API -------------------------------------------------------------------


def is_enabled() -> bool:
    if sys.platform.startswith("win"):
        return _win_get()
    return entry_path().is_file()


def set_enabled(enabled: bool) -> None:
    if sys.platform.startswith("win"):
        _win_set(enabled)
        return
    path = entry_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        with path.open("wb") as handle:
            plistlib.dump({"Label": LABEL, "ProgramArguments": launch_args(),
                           "RunAtLoad": True, "ProcessType": "Interactive"}, handle)
        return
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
