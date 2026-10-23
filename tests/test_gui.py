"""GUI tests: the Qt-free model always, widgets only when PySide6 is installed."""

from __future__ import annotations

import os
import sys

import pytest

from ssmouse.device import DeviceNotFoundError, Mouse
from ssmouse.fake import FAKE_INTERFACE, FakeTransport, make_fake_mouse
from ssmouse.gui import autostart
from ssmouse.gui.model import (
    Link,
    Snapshot,
    StatusTracker,
    battery_tier,
    describe_age,
    next_poll_delay,
    poll_once,
)
from ssmouse.transport import TransportError


def test_poll_once_with_fake_device() -> None:
    snap = poll_once(lambda: make_fake_mouse())
    assert snap.link is Link.CONNECTED
    assert snap.battery == 47
    assert snap.current_cpi == 600
    assert snap.active_level == 2
    assert snap.presets == [400, 600, 1400, 2800, 4100]
    assert snap.polling_hz == 250
    assert snap.firmware == "1.04"


def test_poll_once_asleep() -> None:
    from ssmouse.fake import CAPTURED_RF_STATUS_UNPAIRED_RESPONSE, FAKE_FIRMWARE_RESPONSE

    def detect() -> Mouse:
        mouse = Mouse([FAKE_INTERFACE], connect=False, retries=1)
        transport = FakeTransport(script=[
            (bytes([0xE8]), CAPTURED_RF_STATUS_UNPAIRED_RESPONSE),
            (bytes([0xD0]), FAKE_FIRMWARE_RESPONSE),
        ])
        transport.open()
        mouse._transport = transport  # noqa: SLF001
        return mouse

    snap = poll_once(detect)
    assert snap.link is Link.ASLEEP
    assert snap.battery is None
    assert snap.firmware == "1.04"


@pytest.mark.parametrize(
    ("exc", "link"),
    [
        (DeviceNotFoundError("no 1038:1830 device found"), Link.NO_RECEIVER),
        (TransportError("open failed: [Errno 13] Permission denied"), Link.NO_PERMISSION),
        (TransportError("something odd"), Link.ERROR),
    ],
)
def test_poll_once_errors(exc: Exception, link: Link) -> None:
    def detect() -> Mouse:
        raise exc

    assert poll_once(detect).link is link


def test_tracker_remembers_battery_while_asleep() -> None:
    tracker = StatusTracker()
    tracker.update(Snapshot(link=Link.CONNECTED, battery=70, battery_seen_at=100.0))
    merged, alert = tracker.update(Snapshot(link=Link.ASLEEP))
    assert merged.battery == 70
    assert merged.battery_stale is True
    assert merged.battery_seen_at == 100.0
    assert alert is None


def test_tracker_alerts_once_per_threshold_and_rearms() -> None:
    tracker = StatusTracker()
    alerts = [tracker.update(Snapshot(link=Link.CONNECTED, battery=level))[1]
              for level in (30, 21, 20, 19, 20, 11, 10, 9, 10)]
    assert alerts == [None, None, 20, None, None, None, 10, None, None]
    # Fresh batteries re-arm both thresholds.
    assert tracker.update(Snapshot(link=Link.CONNECTED, battery=100))[1] is None
    assert tracker.update(Snapshot(link=Link.CONNECTED, battery=20))[1] == 20


def test_tracker_does_not_realert_remembered_low_level() -> None:
    tracker = StatusTracker(last_battery=15, last_seen=0.0)
    assert tracker.update(Snapshot(link=Link.CONNECTED, battery=15))[1] is None
    assert tracker.update(Snapshot(link=Link.CONNECTED, battery=9))[1] == 10


def test_battery_tier() -> None:
    assert battery_tier(None) == "unknown"
    assert battery_tier(5) == "critical"
    assert battery_tier(10) == "critical"
    assert battery_tier(25) == "low"
    assert battery_tier(26) == "ok"


def test_poll_faster_while_asleep() -> None:
    assert next_poll_delay(Snapshot(link=Link.ASLEEP), 60) == 5.0
    assert next_poll_delay(Snapshot(link=Link.CONNECTED), 60) == 60
    assert next_poll_delay(Snapshot(link=Link.ASLEEP), 2) == 2


def test_describe_age() -> None:
    assert describe_age(None) == "never"
    assert describe_age(1000.0, now=1030.0) == "just now"
    assert describe_age(1000.0, now=1000.0 + 5 * 60) == "5 min ago"
    assert describe_age(1000.0, now=1000.0 + 3 * 3600) == "3 h ago"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows uses the registry")
def test_autostart_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    text = autostart.entry_path().read_text()
    assert "--minimized" in text
    assert text.startswith("[Desktop Entry]")
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not autostart.is_enabled()
    autostart.set_enabled(False)  # idempotent


# -- widgets (need PySide6) ---------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_tray_icon_renders_all_states(qapp) -> None:
    from ssmouse.gui.widgets import tray_icon

    for level, stale in ((100, False), (50, False), (5, False), (60, True), (None, False)):
        icon = tray_icon(level, stale)
        assert not icon.isNull()
        assert not icon.pixmap(22, 22).isNull()


def test_main_window_shows_snapshot(qapp, tmp_path) -> None:
    from PySide6.QtCore import QSettings

    from ssmouse.gui.app import MainWindow

    settings = QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)
    window = MainWindow(settings)
    window.show_snapshot(poll_once(lambda: make_fake_mouse()))
    assert window.name_label.text() == "SteelSeries Rival 3 Wireless"
    assert window.status_chip.text() == "Connected"
    assert [chip.text() for chip in window.dpi_chips] == ["400", "600", "1400", "2800", "4100"]
    assert window.polling_label.text() == "250 Hz"
    assert window.gauge.accessibleName() == "Battery 47%"

    window.show_snapshot(Snapshot(link=Link.NO_PERMISSION))
    assert window.name_label.text() == "SteelSeries Rival 3 Wireless"
    assert "udev" in window.battery_note.text()


def test_autostart_macos_launch_agent(tmp_path, monkeypatch) -> None:
    import plistlib

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart.Path, "home", classmethod(lambda cls: tmp_path))
    autostart.set_enabled(True)
    data = plistlib.loads(autostart.entry_path().read_bytes())
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][-1] == "--minimized"
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_key_combination_to_spec(qapp) -> None:
    from PySide6.QtCore import QKeyCombination, Qt

    from ssmouse.gui.pages import key_combination_to_spec, spec_to_portable

    combo = QKeyCombination(Qt.KeyboardModifier.ShiftModifier, Qt.Key.Key_F13)
    assert key_combination_to_spec(combo) == "key:shift+f13"
    combo = QKeyCombination(Qt.KeyboardModifier.AltModifier, Qt.Key.Key_PageDown)
    assert key_combination_to_spec(combo) == "key:alt+pagedown"
    combo = QKeyCombination(Qt.KeyboardModifier.NoModifier, Qt.Key.Key_VolumeUp)
    assert key_combination_to_spec(combo) is None
    assert spec_to_portable("key:ctrl+shift+t") == "Ctrl+Shift+T"


def test_settings_pages(qapp) -> None:
    from ssmouse.gui.pages import ButtonsPage, DeviceConfig, LightingPage, PerformancePage
    from ssmouse.profile import Profile
    from ssmouse.protocol import DpiInfo, DpiPreset

    profile = Profile()
    perf = PerformancePage(profile)
    perf.load_config(DeviceConfig(
        dpi=DpiInfo(2, 2, (DpiPreset(1, 4, 400), DpiPreset(2, 9, 800))), polling_hz=500))
    assert perf.values() == ([400, 800], 2, 500)
    perf.spins[0].setValue(450)
    assert perf.values()[0] == [500, 800]  # half-up to the 100 CPI grid

    light = LightingPage(profile)
    light.mode.setCurrentIndex(light.mode.findData("colorshift"))
    light._add_color((0, 0, 255))
    effect = light.effect()
    assert effect.mode == "colorshift" and len(effect.colors) == 2

    buttons = ButtonsPage(profile)
    buttons.rows["back"].set_spec("media:mute")
    assert buttons.bindings()["back"] == "media:mute"
    buttons.rows["left"].set_spec("disabled")
    with pytest.raises(ValueError, match="Left click"):
        buttons.bindings()
