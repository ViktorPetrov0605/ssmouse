"""ssmouse-gui: a small window plus a system-tray battery indicator.

Usage::

    ssmouse-gui               # window + tray icon
    ssmouse-gui --minimized   # start hidden in the tray (used for autostart)
    ssmouse-gui --fake        # demo with captured data, no hardware needed
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..device import ENV_FAKE, DeviceNotFoundError, Mouse, _fake_requested
from ..events import Event, listen
from ..profile import Profile
from . import autostart
from .model import Link, Snapshot, StatusTracker, describe_age, next_poll_delay, poll_once
from .pages import (
    ButtonsPage,
    DevicePage,
    LightingPage,
    Page,
    PerformancePage,
    PowerPage,
    read_config,
)
from .widgets import TIER_COLORS, BatteryGauge, Chip, tray_icon

APP_ID = "ssmouse-gui"
INTERVALS = ((5, "5 seconds"), (10, "10 seconds"), (15, "15 seconds"),
             (30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"))
#: Link, DPI and battery changes arrive instantly as receiver events; the
#: poll is only a fallback, so it can stay fairly relaxed.
DEFAULT_INTERVAL = 15

LINK_COLORS = {
    Link.CONNECTED: TIER_COLORS["ok"],
    Link.ASLEEP: TIER_COLORS["low"],
    Link.NO_RECEIVER: TIER_COLORS["unknown"],
    Link.NO_PERMISSION: TIER_COLORS["critical"],
    Link.ERROR: TIER_COLORS["critical"],
}


class DeviceWorker(QObject):
    """Runs all device I/O on one background thread.

    Polls and configuration jobs are queued on a single-thread executor, so
    timeouts never freeze the UI and two commands never interleave on the HID
    interface.  This QObject itself lives in the GUI thread; Qt queues the
    signals emitted from the worker thread automatically.
    """

    result = Signal(object)
    job_done = Signal(str, object, object)  # tag, result, error

    def __init__(self) -> None:
        super().__init__()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ssmouse-device")

    def poll(self) -> None:
        self.executor.submit(lambda: self.result.emit(poll_once()))

    def run_job(self, tag: str, job) -> None:
        self.executor.submit(self._run_job, tag, job)

    def _run_job(self, tag: str, job) -> None:
        try:
            mouse = Mouse.detect()
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.job_done.emit(tag, None, exc)
            return
        try:
            self.job_done.emit(tag, job(mouse), None)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.job_done.emit(tag, None, exc)
        finally:
            mouse.close()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


class EventWatcher(QObject):
    """Forwards receiver push events (link, DPI level, battery) to the UI."""

    event = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=listen, args=(self.event.emit, self.stop),
                                       name="ssmouse-events", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
        self.thread.join(2.0)


class MainWindow(QWidget):
    refresh_requested = Signal()

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle("ssmouse")
        self.setWindowIcon(QIcon.fromTheme("input-mouse"))
        self.setMinimumWidth(460)
        self.resize(540, 620)

        self.name_label = QLabel("SteelSeries mouse")
        font = self.name_label.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self.name_label.setFont(font)
        self.status_chip = Chip("Searching...")

        header = QHBoxLayout()
        header.addWidget(self.name_label, 1)
        header.addWidget(self.status_chip)

        self.gauge = BatteryGauge()
        self.battery_note = QLabel(" ")
        self.battery_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.battery_note.setWordWrap(True)

        self.dpi_row = QHBoxLayout()
        self.dpi_row.setSpacing(6)
        self.dpi_chips: list[Chip] = []
        dpi_box = QVBoxLayout()
        dpi_title = QLabel("DPI presets")
        dpi_title.setStyleSheet("font-weight: bold;")
        dpi_box.addWidget(dpi_title)
        dpi_box.addLayout(self.dpi_row)

        self.polling_label = QLabel("-")
        self.firmware_label = QLabel("-")
        self.path_label = QLabel("-")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details = QFormLayout()
        details.addRow("Polling rate:", self.polling_label)
        details.addRow("Receiver firmware:", self.firmware_label)
        details.addRow("HID device:", self.path_label)

        self.interval_box = QComboBox()
        for seconds, label in INTERVALS:
            self.interval_box.addItem(label, seconds)
        index = self.interval_box.findData(self.interval())
        self.interval_box.setCurrentIndex(
            index if index >= 0 else self.interval_box.findData(DEFAULT_INTERVAL))
        self.interval_box.currentIndexChanged.connect(self._interval_changed)

        self.autostart_box = QCheckBox("Start on login")
        self.autostart_box.setVisible(autostart.supported())
        self.autostart_box.setChecked(autostart.is_enabled())
        self.autostart_box.toggled.connect(autostart.set_enabled)

        self.refresh_button = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh")
        self.refresh_button.clicked.connect(self.refresh_requested)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Update every"))
        controls.addWidget(self.interval_box)
        controls.addStretch(1)
        controls.addWidget(self.refresh_button)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        self.status_page = QWidget()
        layout = QVBoxLayout(self.status_page)
        layout.setSpacing(12)
        layout.addWidget(self.gauge)
        layout.addWidget(self.battery_note)
        layout.addLayout(dpi_box)
        layout.addLayout(details)
        layout.addWidget(line)
        layout.addLayout(controls)
        layout.addWidget(self.autostart_box)
        layout.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.status_page, "Status")
        self.message = QLabel("")
        self.message.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)
        outer.addLayout(header)
        outer.addWidget(self.tabs, 1)
        outer.addWidget(self.message)

        self.hide_to_tray = False
        self._tray_hint_shown = bool(settings.value("tray_hint_shown", False, type=bool))
        self.tray: Optional[QSystemTrayIcon] = None

    def add_page(self, page: QWidget, title: str) -> None:
        self.tabs.addTab(page, title)

    def show_message(self, text: str, error: bool = False) -> None:
        color = TIER_COLORS["critical"].name() if error else "palette(window-text)"
        self.message.setStyleSheet("color: %s;" % color)
        self.message.setText(text)

    def interval(self) -> int:
        return int(self.settings.value("interval", DEFAULT_INTERVAL, type=int))

    def _interval_changed(self) -> None:
        self.settings.setValue("interval", int(self.interval_box.currentData()))

    def show_snapshot(self, snap: Snapshot) -> None:
        if snap.name != Snapshot.name:  # keep the last real name while unreachable
            self.name_label.setText(snap.name)
        self.status_chip.setText(snap.status_text.split(" - ")[0])
        self.status_chip.set_style(LINK_COLORS[snap.link], filled=True)
        self.status_chip.setToolTip(snap.detail or snap.status_text)
        self.gauge.set_level(snap.battery, snap.battery_stale)

        if snap.link is Link.CONNECTED and not snap.battery_stale:
            note = "Updated %s" % time.strftime("%H:%M:%S", time.localtime(snap.taken_at))
        elif snap.battery is not None:
            note = "%s. Last reading %s." % (snap.status_text, describe_age(snap.battery_seen_at))
        else:
            note = snap.status_text
        if snap.link is Link.NO_PERMISSION:
            note += "\nRun install.sh (or copy udev/99-steelseries.rules) and replug the receiver."
        self.battery_note.setText(note)

        if snap.presets:
            self._set_presets(snap.presets, snap.active_level)
        self.polling_label.setText("%d Hz" % snap.polling_hz if snap.polling_hz else "-")
        self.firmware_label.setText(snap.firmware or "-")
        self.path_label.setText(snap.path or "-")

    def _set_presets(self, presets: list[Optional[int]], active: Optional[int]) -> None:
        while len(self.dpi_chips) < len(presets):
            chip = Chip()
            self.dpi_chips.append(chip)
            self.dpi_row.addWidget(chip)
        for index, chip in enumerate(self.dpi_chips):
            if index >= len(presets):
                chip.hide()
                continue
            chip.show()
            chip.setText("?" if presets[index] is None else str(presets[index]))
            is_active = active == index + 1
            chip.set_style(self.palette().highlight().color() if is_active else None,
                           filled=is_active)
            chip.setToolTip("Level %d%s" % (index + 1, " (active)" if is_active else ""))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.hide_to_tray:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown and self.tray is not None:
                self.tray.showMessage("ssmouse", "Still running in the system tray.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
                self._tray_hint_shown = True
                self.settings.setValue("tray_hint_shown", True)
            return
        super().closeEvent(event)


class Controller(QObject):
    poll_requested = Signal()
    job_requested = Signal(str, object)

    def __init__(self, app: QApplication, start_minimized: bool) -> None:
        super().__init__()
        self.app = app
        self.settings = QSettings("ssmouse", APP_ID)
        last = self.settings.value("last_battery", None)
        seen = self.settings.value("last_seen", None)
        self.tracker = StatusTracker(
            int(last) if last is not None else None,
            float(seen) if seen is not None else None,
        )
        self.busy = False
        self.last: Optional[Snapshot] = None

        self.window = MainWindow(self.settings)
        self.window.refresh_requested.connect(self.refresh)
        self.window.interval_box.currentIndexChanged.connect(self.refresh)

        self.worker = DeviceWorker()
        self.poll_requested.connect(self.worker.poll)
        self.job_requested.connect(self.worker.run_job)
        self.worker.result.connect(self.on_result)
        self.worker.job_done.connect(self.on_job_done)

        # Receiver push events: instant updates without polling the mouse.
        self.watcher: Optional[EventWatcher] = None
        self.event_refresh = QTimer(self)
        self.event_refresh.setSingleShot(True)
        self.event_refresh.timeout.connect(self.refresh)
        if not _fake_requested():
            self.watcher = EventWatcher()
            self.watcher.event.connect(self.on_event)
            self.watcher.start()

        self.linked = False
        self.config_loaded = False
        self.jobs_pending = 0
        try:
            self.profile = Profile.load()
        except (OSError, ValueError) as exc:
            self.profile = Profile()
            self.window.show_message("Ignoring unreadable profile: %s" % exc, error=True)
        self.pages: list[Page] = [
            PerformancePage(self.profile),
            LightingPage(self.profile),
            ButtonsPage(self.profile),
            PowerPage(self.profile),
            DevicePage(self.profile),
        ]
        for page, title in zip(self.pages, ("Performance", "Lighting", "Buttons", "Power",
                                             "Device")):
            self.window.add_page(page, title)
            page.apply_requested.connect(self.run_job)
            page.profile_changed.connect(self.save_profile)
            page.set_linked(False)
        self.pages[-1].profile_replaced.connect(self.replace_profile)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        self.tray = self._make_tray()
        self.window.tray = self.tray
        self.window.hide_to_tray = self.tray is not None
        app.setQuitOnLastWindowClosed(self.tray is None)

        if self.tracker.last_battery is not None:
            # Show the remembered level immediately while the first poll runs.
            self.on_snapshot(Snapshot(link=Link.ASLEEP), notify=False)
        if not (start_minimized and self.tray is not None):
            self.window.show()
        self.refresh()

    def _make_tray(self) -> Optional[QSystemTrayIcon]:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(tray_icon(None), self)
        menu = QMenu()
        show = QAction("Show window", menu)
        show.triggered.connect(self.show_window)
        refresh = QAction("Refresh now", menu)
        refresh.triggered.connect(self.refresh)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit)
        menu.addAction(show)
        menu.addAction(refresh)
        if autostart.supported():
            auto = QAction("Start on login", menu, checkable=True)
            auto.setChecked(autostart.is_enabled())
            auto.toggled.connect(self.window.autostart_box.setChecked)
            self.window.autostart_box.toggled.connect(auto.setChecked)
            menu.addAction(auto)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        self._tray_menu = menu  # keep a reference
        tray.setToolTip("ssmouse")
        tray.show()
        return tray

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    @Slot()
    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        self.timer.stop()
        if self.busy:
            return
        self.busy = True
        self.window.refresh_button.setEnabled(False)
        self.poll_requested.emit()

    @Slot(object)
    def on_event(self, event: Event) -> None:
        now = time.time()
        if event.kind == "battery":
            base = self.last or Snapshot(link=Link.CONNECTED)
            self.on_snapshot(replace(base, link=Link.CONNECTED, battery=event.value,
                                     battery_stale=False, battery_seen_at=now, taken_at=now))
        elif event.kind == "dpi" and self.last is not None:
            presets = self.last.presets
            cpi = presets[event.value - 1] if event.value <= len(presets) else None
            self.on_snapshot(replace(self.last, active_level=event.value, current_cpi=cpi,
                                     taken_at=now), notify=False)
            self.config_loaded = False  # refresh the Performance tab on the next poll
            self.event_refresh.start(500)
        elif event.kind == "link":
            # The link flaps briefly while the mouse (re)connects; settle first.
            self.event_refresh.start(1500 if event.value else 800)

    # -- configuration jobs ------------------------------------------------
    @Slot(str, object)
    def run_job(self, description: str, job) -> None:
        self.jobs_pending += 1
        self.window.show_message(description + "\u2026")
        self.job_requested.emit(description, job)

    def load_config(self) -> None:
        self.jobs_pending += 1
        self.job_requested.emit("read-config", read_config)

    @Slot(str, object, object)
    def on_job_done(self, tag: str, result, error) -> None:
        self.jobs_pending = max(0, self.jobs_pending - 1)
        if tag == "read-config":
            if error is None:
                self.config_loaded = True
                for page in self.pages:
                    page.load_config(result)
            return
        if error is not None:
            if isinstance(error, DeviceNotFoundError):
                text = "Receiver not found"
            else:
                text = str(error) or error.__class__.__name__
            self.window.show_message("Failed: %s" % text, error=True)
        else:
            self.window.show_message(str(result))
            if self.tray is not None and not self.window.isVisible():
                self.tray.showMessage("ssmouse", str(result),
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
        self.load_config()
        self.refresh()

    @Slot()
    def save_profile(self) -> None:
        try:
            self.profile.save()
        except (OSError, ValueError) as exc:
            self.window.show_message("Could not save profile: %s" % exc, error=True)

    @Slot(object)
    def replace_profile(self, profile: Profile) -> None:
        # Pages share this object, so update it in place.
        vars(self.profile).update(vars(profile))
        self.save_profile()
        for page in self.pages:
            page.reload()

    @Slot(object)
    def on_result(self, snapshot: Snapshot) -> None:
        self.busy = False
        self.window.refresh_button.setEnabled(True)
        self.on_snapshot(snapshot)
        linked = snapshot.link is Link.CONNECTED
        if linked != self.linked:
            self.linked = linked
            for page in self.pages:
                page.set_linked(linked)
        if linked and not self.config_loaded:
            self.load_config()
        self.timer.start(int(next_poll_delay(self.last, self.window.interval()) * 1000))

    def on_snapshot(self, snapshot: Snapshot, notify: bool = True) -> None:
        merged, alert = self.tracker.update(snapshot)
        self.last = merged
        if not merged.battery_stale and merged.battery is not None:
            self.settings.setValue("last_battery", merged.battery)
            self.settings.setValue("last_seen", merged.battery_seen_at)
        self.window.show_snapshot(merged)
        if self.tray is not None:
            self.tray.setIcon(tray_icon(merged.battery, merged.battery_stale))
            level = "--" if merged.battery is None else "%d%%" % merged.battery
            tip = "%s\nBattery: %s" % (merged.name, level)
            if merged.link is not Link.CONNECTED:
                tip += "\n%s" % merged.status_text
            self.tray.setToolTip(tip)
            if alert is not None and notify:
                self.tray.showMessage(
                    "Mouse battery low",
                    "%s is at %d%%. Replace or recharge the batteries soon."
                    % (merged.name, merged.battery),
                    QSystemTrayIcon.MessageIcon.Warning,
                    10000,
                )

    def stop_threads(self) -> None:
        if self.watcher is not None:
            self.watcher.shutdown()
        self.worker.shutdown()

    @Slot()
    def quit(self) -> None:
        self.stop_threads()
        self.app.quit()


def _already_running() -> bool:
    """Ask a running instance to show its window; True if one answered."""
    socket = QLocalSocket()
    socket.connectToServer(APP_ID)
    if socket.waitForConnected(300):
        socket.write(b"show\n")
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return True
    return False


def _listen_for_instances(controller: Controller) -> QLocalServer:
    QLocalServer.removeServer(APP_ID)  # stale socket after a crash
    server = QLocalServer(controller)
    server.newConnection.connect(
        lambda: (server.nextPendingConnection(), controller.show_window())
    )
    server.listen(APP_ID)
    return server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssmouse-gui", description=__doc__.splitlines()[0])
    parser.add_argument("--minimized", action="store_true",
                        help="start hidden in the system tray")
    parser.add_argument("--fake", action="store_true",
                        help="use captured demo data instead of hardware")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.fake:
        os.environ[ENV_FAKE] = "1"
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ssmouse")
    app.setDesktopFileName(APP_ID)
    if _already_running():
        return 0
    controller = Controller(app, start_minimized=args.minimized)
    server = _listen_for_instances(controller)  # noqa: F841 - keep alive
    code = app.exec()
    controller.stop_threads()
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
