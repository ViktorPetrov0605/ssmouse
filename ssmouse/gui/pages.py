"""Settings pages: performance, lighting, buttons, power and device.

Each page edits part of the :class:`~ssmouse.profile.Profile` and, on Apply,
emits ``apply_requested(description, job)``.  ``job(mouse)`` runs on the
device worker thread, writes the change and stores it in the mouse's flash.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QKeyCombination, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import firmware, protocol
from ..bindings import (
    ACTION_LABELS,
    DEFAULT_BINDINGS,
    MEDIA_KEYS,
    TRIGGER_LABELS,
    TRIGGERS,
    parse_binding,
)
from ..device import Mouse
from ..lighting import DEFAULT_COLOR, MAX_COLORS, LightingEffect
from ..profile import (
    HIGH_EFFICIENCY_HZ,
    Profile,
    apply_power,
    apply_profile,
)

Job = Callable[[Mouse], str]


@dataclass
class DeviceConfig:
    """Settings read back from the mouse (lighting/buttons cannot be read)."""

    dpi: Optional[protocol.DpiInfo] = None
    polling_hz: Optional[int] = None
    power: Optional[protocol.PowerMode] = None
    debounce: Optional[tuple[int, int]] = None
    firmware: Optional[firmware.FirmwareStatus] = None


def read_config(mouse: Mouse) -> DeviceConfig:
    return DeviceConfig(
        dpi=mouse.get_dpi(),
        polling_hz=mouse.get_polling_rate(),
        power=mouse.get_power_mode(),
        debounce=mouse.get_debounce(),
        firmware=firmware.read_status(mouse),
    )


def _persisting(job: Job) -> Job:
    def run(mouse: Mouse) -> str:
        message = job(mouse)
        mouse.save_to_flash()
        return message
    return run


class Page(QWidget):
    apply_requested = Signal(str, object)  # description, Job
    profile_changed = Signal()

    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._apply)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(self.reload)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(placeholder-text);")
        self.config = DeviceConfig()

    def _footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(self.hint, 1)
        row.addWidget(self.revert_button)
        row.addWidget(self.apply_button)
        return row

    def set_linked(self, linked: bool) -> None:
        self.apply_button.setEnabled(linked)
        self.apply_button.setToolTip("" if linked else "Wake the mouse first")

    def load_config(self, config: DeviceConfig) -> None:
        self.config = config
        self.reload()

    def reload(self) -> None:  # pragma: no cover - overridden
        pass

    def _apply(self) -> None:  # pragma: no cover - overridden
        pass

    def _submit(self, description: str, job: Job) -> None:
        try:
            self.profile.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "ssmouse", str(exc))
            return
        self.profile_changed.emit()
        self.apply_requested.emit(description, _persisting(job))


# ---------------------------------------------------------------------------
# Performance: DPI presets + polling rate
# ---------------------------------------------------------------------------


class PerformancePage(Page):
    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(profile, parent)
        self.count = QSpinBox()
        self.count.setRange(1, protocol.MAX_DPI_LEVELS)
        self.count.valueChanged.connect(self._update_rows)
        self.group = QButtonGroup(self)
        self.spins: list[QSpinBox] = []
        self.radios: list[QRadioButton] = []

        presets = QGroupBox("DPI presets (the DPI button cycles through them)")
        grid = QFormLayout(presets)
        grid.addRow("Number of presets:", self.count)
        for index in range(protocol.MAX_DPI_LEVELS):
            spin = QSpinBox()
            spin.setRange(100, 18000)
            spin.setSingleStep(100)
            spin.setSuffix(" CPI")
            spin.setKeyboardTracking(False)
            radio = QRadioButton("active")
            self.group.addButton(radio, index + 1)
            row = QHBoxLayout()
            row.addWidget(spin, 1)
            row.addWidget(radio)
            self.spins.append(spin)
            self.radios.append(radio)
            grid.addRow("Preset %d:" % (index + 1), row)

        self.polling = QComboBox()
        for hz in (1000, 500, 250, 125):
            self.polling.addItem("%d Hz" % hz, hz)
        rate = QGroupBox("Polling rate")
        rate_form = QFormLayout(rate)
        rate_form.addRow("Report rate:", self.polling)
        self.hint.setText("Values snap to the sensor's 100 CPI steps.")

        layout = QVBoxLayout(self)
        layout.addWidget(presets)
        layout.addWidget(rate)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self.reload()

    def _update_rows(self) -> None:
        count = self.count.value()
        for index, (spin, radio) in enumerate(zip(self.spins, self.radios)):
            spin.setEnabled(index < count)
            radio.setEnabled(index < count)
        if self.group.checkedId() > count:
            self.radios[0].setChecked(True)

    def reload(self) -> None:
        presets = self.profile.dpi_presets
        active = self.profile.dpi_active or 1
        if self.config.dpi is not None:
            info = self.config.dpi
            presets = [p.cpi or 800 for p in info.presets[: info.level_max]]
            active = info.active_level
        presets = presets or [400, 800, 1200, 1600, 2000]
        self.count.setValue(len(presets))
        defaults = [400, 800, 1200, 1600, 2000]
        for index, spin in enumerate(self.spins):
            spin.setValue(presets[index] if index < len(presets) else defaults[index])
        self.radios[min(active, len(presets)) - 1].setChecked(True)
        hz = self.config.polling_hz or self.profile.polling_hz or 1000
        self.polling.setCurrentIndex(max(0, self.polling.findData(hz)))
        self._update_rows()

    def values(self) -> tuple[list[int], int, int]:
        count = self.count.value()
        presets = [(spin.value() + 50) // 100 * 100 for spin in self.spins[:count]]
        return presets, max(1, self.group.checkedId()), int(self.polling.currentData())

    def _apply(self) -> None:
        presets, active, hz = self.values()
        self.profile.dpi_presets, self.profile.dpi_active = presets, active
        self.profile.polling_hz = hz
        high_efficiency = bool(self.profile.power.high_efficiency)

        def job(mouse: Mouse) -> str:
            used = mouse.set_dpi(presets, active)
            message = "DPI %s (active %d CPI)" % ("/".join(map(str, used)), used[active - 1])
            if not high_efficiency and mouse.get_polling_rate() != hz:
                mouse.set_polling_rate(hz)
                message += ", %d Hz" % hz
            return message

        self._submit("Saving DPI and polling rate", job)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------

LIGHT_MODES = (
    ("steady", "Steady colour"),
    ("colorshift", "Colour shift"),
    ("breathe", "Breathe"),
    ("reactive", "Reactive (flash on click)"),
    ("off", "Off"),
)


class ColorButton(QPushButton):
    def __init__(self, color: tuple[int, int, int], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(40, 28)
        self.clicked.connect(self._pick)
        self.set_color(color)

    def set_color(self, color: tuple[int, int, int]) -> None:
        self.color = tuple(color)
        self.setStyleSheet(
            "QPushButton { background: rgb(%d,%d,%d); border: 1px solid palette(mid);"
            " border-radius: 4px; }" % self.color
        )
        self.setToolTip("#%02x%02x%02x - click to change" % self.color)

    def _pick(self) -> None:
        color = QColorDialog.getColor(QColor(*self.color), self, "Choose colour")
        if color.isValid():
            self.set_color((color.red(), color.green(), color.blue()))


class LightingPage(Page):
    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(profile, parent)
        self.mode = QComboBox()
        for key, label in LIGHT_MODES:
            self.mode.addItem(label, key)
        self.mode.currentIndexChanged.connect(self._update_mode)

        self.colors_row = QHBoxLayout()
        self.colors_row.setSpacing(6)
        self.color_buttons: list[ColorButton] = []
        self.add_color = QPushButton("+")
        self.add_color.setFixedWidth(32)
        self.add_color.setToolTip("Add a colour")
        self.add_color.clicked.connect(lambda: self._add_color(DEFAULT_COLOR))
        self.remove_color = QPushButton("−")
        self.remove_color.setFixedWidth(32)
        self.remove_color.setToolTip("Remove the last colour")
        self.remove_color.clicked.connect(self._remove_color)
        colors_box = QHBoxLayout()
        colors_box.addLayout(self.colors_row)
        colors_box.addWidget(self.add_color)
        colors_box.addWidget(self.remove_color)
        colors_box.addStretch(1)

        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.5, 60.0)
        self.duration.setSingleStep(0.5)
        self.duration.setSuffix(" s")
        self.flash = ColorButton((255, 255, 255))
        self.fade = QSpinBox()
        self.fade.setRange(50, 5000)
        self.fade.setSingleStep(50)
        self.fade.setSuffix(" ms")

        box = QGroupBox("Scroll-wheel LED")
        self.form = QFormLayout(box)
        self.form.addRow("Effect:", self.mode)
        self.form.addRow("Colours:", colors_box)
        self.form.addRow("Cycle length:", self.duration)
        self.form.addRow("Flash colour:", self.flash)
        self.form.addRow("Fade back in:", self.fade)
        self.hint.setText("The mouse cannot report its lighting; this shows the last "
                          "effect ssmouse applied.")

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self.reload()

    def _add_color(self, color) -> None:
        button = ColorButton(color)
        self.color_buttons.append(button)
        self.colors_row.addWidget(button)
        self._update_mode()

    def _drop_color(self) -> None:
        # Remove synchronously: deleteLater() on a widget Python still wraps can
        # be freed twice at shutdown.
        button = self.color_buttons.pop()
        self.colors_row.removeWidget(button)
        button.setParent(None)

    def _remove_color(self) -> None:
        if len(self.color_buttons) > 1:
            self._drop_color()
        self._update_mode()

    def _update_mode(self) -> None:
        mode = self.mode.currentData()
        limit = {"steady": 1, "reactive": 1, "off": 0}.get(mode, MAX_COLORS.get(mode, 1))
        # Row visibility first: setRowVisible(True) re-shows every widget in the row.
        self.form.setRowVisible(1, mode != "off")
        self.form.setRowVisible(self.duration, mode in MAX_COLORS)
        self.form.setRowVisible(self.flash, mode == "reactive")
        self.form.setRowVisible(self.fade, mode == "reactive")
        for index, button in enumerate(self.color_buttons):
            button.setVisible(index < max(limit, 0))
        multi = mode in MAX_COLORS
        self.add_color.setVisible(multi)
        self.remove_color.setVisible(multi)
        self.add_color.setEnabled(multi and len(self.color_buttons) < limit)
        self.remove_color.setEnabled(multi and len(self.color_buttons) > 1)

    def reload(self) -> None:
        effect = self.profile.lighting or LightingEffect()
        while self.color_buttons:
            self._drop_color()
        for color in effect.colors:
            self._add_color(color)
        self.mode.setCurrentIndex(max(0, self.mode.findData(effect.mode)))
        self.duration.setValue(effect.duration_ms / 1000.0)
        self.flash.set_color(effect.react_color)
        self.fade.setValue(effect.react_ms)
        self._update_mode()

    def effect(self) -> LightingEffect:
        mode = self.mode.currentData()
        colors = [button.color for button in self.color_buttons]
        if mode in ("steady", "reactive"):
            colors = colors[:1]
        return LightingEffect(
            mode=mode,
            colors=colors or [DEFAULT_COLOR],
            duration_ms=int(self.duration.value() * 1000),
            react_color=self.flash.color,
            react_ms=self.fade.value(),
        )

    def _apply(self) -> None:
        try:
            effect = self.effect()
        except ValueError as exc:
            QMessageBox.warning(self, "ssmouse", str(exc))
            return
        self.profile.lighting = effect

        def job(mouse: Mouse) -> str:
            mouse.set_lighting(effect)
            return "Lighting: %s" % dict(LIGHT_MODES)[effect.mode]

        self._submit("Saving lighting", job)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

KEYBOARD_CHOICE = "key"

_QT_KEY_NAMES = {
    Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter", Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Space: "space",
    Qt.Key.Key_Minus: "minus", Qt.Key.Key_Equal: "equal", Qt.Key.Key_BracketLeft: "leftbrace",
    Qt.Key.Key_BracketRight: "rightbrace", Qt.Key.Key_Backslash: "backslash",
    Qt.Key.Key_Semicolon: "semicolon", Qt.Key.Key_Apostrophe: "apostrophe",
    Qt.Key.Key_QuoteLeft: "grave", Qt.Key.Key_Comma: "comma", Qt.Key.Key_Period: "dot",
    Qt.Key.Key_Slash: "slash", Qt.Key.Key_CapsLock: "capslock", Qt.Key.Key_Print: "printscreen",
    Qt.Key.Key_ScrollLock: "scrolllock", Qt.Key.Key_Pause: "pause", Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home", Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_End: "end", Qt.Key.Key_PageDown: "pagedown", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Down: "down", Qt.Key.Key_Up: "up",
    Qt.Key.Key_Menu: "menu",
}


def key_combination_to_spec(combo: QKeyCombination) -> Optional[str]:
    """Qt key combination -> ``key:ctrl+shift+t`` (None if unsupported)."""
    key = combo.key()
    mods = combo.keyboardModifiers()
    names = []
    ctrl, meta = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.MetaModifier
    if sys.platform == "darwin":  # Qt maps Cmd to Control on macOS
        ctrl, meta = meta, ctrl
    if mods & ctrl:
        names.append("ctrl")
    if mods & Qt.KeyboardModifier.ShiftModifier:
        names.append("shift")
    if mods & Qt.KeyboardModifier.AltModifier:
        names.append("alt")
    if mods & meta:
        names.append("super")
    value = int(key.value) if hasattr(key, "value") else int(key)
    if Qt.Key.Key_A.value <= value <= Qt.Key.Key_Z.value:
        name = chr(ord("a") + value - Qt.Key.Key_A.value)
    elif Qt.Key.Key_0.value <= value <= Qt.Key.Key_9.value:
        name = chr(ord("0") + value - Qt.Key.Key_0.value)
    elif Qt.Key.Key_F1.value <= value <= Qt.Key.Key_F24.value:
        name = "f%d" % (value - Qt.Key.Key_F1.value + 1)
    else:
        name = _QT_KEY_NAMES.get(key)
    if name is None or len(names) > 3:
        return None
    return "key:" + "+".join(names + [name])


def spec_to_portable(spec: str) -> str:
    """``key:ctrl+shift+t`` -> ``Ctrl+Shift+T`` for QKeySequence."""
    parts = spec.partition(":")[2].split("+")
    mapping = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "super": "Meta",
               "esc": "Esc", "pageup": "PgUp", "pagedown": "PgDown", "delete": "Del",
               "insert": "Ins", "printscreen": "Print", "enter": "Return"}
    return "+".join(mapping.get(part, part.upper() if len(part) == 1 else part.capitalize())
                    for part in parts)


class BindingRow(QWidget):
    def __init__(self, trigger: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.trigger = trigger
        self.combo = QComboBox()
        for spec, label in ACTION_LABELS.items():
            self.combo.addItem(label, spec)
        self.combo.insertSeparator(self.combo.count())
        for name in MEDIA_KEYS:
            self.combo.addItem("Media: " + name.replace("-", " ").title(), "media:" + name)
        self.combo.insertSeparator(self.combo.count())
        self.combo.addItem("Keyboard shortcut…", KEYBOARD_CHOICE)
        self.keys = QKeySequenceEdit()
        self.keys.setMaximumSequenceLength(1)
        self.keys.setClearButtonEnabled(True)
        self.keys.setToolTip("Press the shortcut (up to three modifiers + one key)")
        self.combo.currentIndexChanged.connect(self._update)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.combo, 1)
        row.addWidget(self.keys, 1)
        self._update()

    def _update(self) -> None:
        self.keys.setVisible(self.combo.currentData() == KEYBOARD_CHOICE)

    def set_spec(self, spec: str) -> None:
        if spec.startswith("key:"):
            self.combo.setCurrentIndex(self.combo.findData(KEYBOARD_CHOICE))
            self.keys.setKeySequence(QKeySequence.fromString(spec_to_portable(spec)))
        else:
            self.combo.setCurrentIndex(max(0, self.combo.findData(spec)))
        self._update()

    def spec(self) -> str:
        data = self.combo.currentData()
        if data != KEYBOARD_CHOICE:
            return data
        sequence = self.keys.keySequence()
        if sequence.isEmpty():
            raise ValueError("%s: press a keyboard shortcut" % TRIGGER_LABELS[self.trigger])
        spec = key_combination_to_spec(sequence[0])
        if spec is None:
            raise ValueError("%s: that key cannot be sent by the mouse"
                             % TRIGGER_LABELS[self.trigger])
        parse_binding(spec)
        return spec


class ButtonsPage(Page):
    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(profile, parent)
        box = QGroupBox("Button assignments")
        form = QFormLayout(box)
        self.rows: dict[str, BindingRow] = {}
        for trigger in TRIGGERS:
            row = BindingRow(trigger)
            self.rows[trigger] = row
            form.addRow(TRIGGER_LABELS[trigger] + ":", row)
        defaults = QPushButton("Restore defaults")
        defaults.clicked.connect(self._defaults)
        self.hint.setText("Changes are stored in the mouse, so they work on any computer.")
        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(defaults, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self.reload()

    def reload(self) -> None:
        for trigger, row in self.rows.items():
            row.set_spec(self.profile.bindings.get(trigger, DEFAULT_BINDINGS[trigger]))

    def _defaults(self) -> None:
        for trigger, row in self.rows.items():
            row.set_spec(DEFAULT_BINDINGS[trigger])

    def bindings(self) -> dict[str, str]:
        result = {trigger: row.spec() for trigger, row in self.rows.items()}
        if "left" not in result.values():
            raise ValueError("Keep at least one control on \"Left click\", "
                             "or you will not be able to click.")
        return result

    def _apply(self) -> None:
        try:
            bindings = self.bindings()
        except ValueError as exc:
            QMessageBox.warning(self, "ssmouse", str(exc))
            return
        self.profile.bindings = {k: v for k, v in bindings.items() if DEFAULT_BINDINGS[k] != v}

        def job(mouse: Mouse) -> str:
            mouse.set_bindings(bindings)
            return "Button assignments saved"

        self._submit("Saving button assignments", job)


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


class PowerPage(Page):
    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(profile, parent)
        self.sleep = QComboBox()
        for seconds in protocol.SLEEP_TIMER_CHOICES_S:
            minutes = seconds // 60
            self.sleep.addItem("%d minute%s" % (minutes, "" if minutes == 1 else "s"), seconds)
        self.sensor_rest = QCheckBox("Sensor rest: lower sensor power while the mouse is still")
        self.high_efficiency = QCheckBox(
            "High-efficiency mode: 125 Hz polling for maximum battery life")
        self.make = QSpinBox()
        self.make.setRange(0, 255)
        self.make.setSuffix(" ms")
        self.brk = QSpinBox()
        self.brk.setRange(0, 255)
        self.brk.setSuffix(" ms")

        battery = QGroupBox("Battery")
        form = QFormLayout(battery)
        form.addRow("Sleep after idle:", self.sleep)
        form.addRow(self.sensor_rest)
        form.addRow(self.high_efficiency)
        debounce = QGroupBox("Button debounce (advanced)")
        dform = QFormLayout(debounce)
        dform.addRow("Press:", self.make)
        dform.addRow("Release:", self.brk)
        self.hint.setText("Factory values: 5 min, sensor rest on, debounce 6/20 ms.")

        layout = QVBoxLayout(self)
        layout.addWidget(battery)
        layout.addWidget(debounce)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self.reload()

    def reload(self) -> None:
        power, profile_power = self.config.power, self.profile.power
        sleep = power.idle_sleep if power else profile_power.sleep_s or 300
        index = self.sleep.findData(sleep)
        if index < 0:
            self.sleep.addItem("%d s (custom)" % sleep, sleep)
            index = self.sleep.count() - 1
        self.sleep.setCurrentIndex(index)
        self.sensor_rest.setChecked(
            bool(power.movement_saving) if power else profile_power.sensor_rest is not False)
        self.high_efficiency.setChecked(
            bool(power.power_saving) if power else bool(profile_power.high_efficiency))
        make, brk = self.config.debounce or self.profile.debounce or (6, 20)
        self.make.setValue(make)
        self.brk.setValue(brk)

    def _apply(self) -> None:
        power = self.profile.power
        power.sleep_s = int(self.sleep.currentData())
        power.sensor_rest = self.sensor_rest.isChecked()
        power.high_efficiency = self.high_efficiency.isChecked()
        debounce = (self.make.value(), self.brk.value())
        self.profile.debounce = debounce
        settings = (power.sleep_s, power.sensor_rest, power.high_efficiency)
        polling_hz = self.profile.polling_hz or 1000

        def job(mouse: Mouse) -> str:
            from ..profile import PowerSettings

            apply_power(mouse, PowerSettings(*settings))
            target = HIGH_EFFICIENCY_HZ if settings[2] else polling_hz
            if mouse.get_polling_rate() != target:
                mouse.set_polling_rate(target)
            if mouse.get_debounce() != debounce:
                mouse.set_debounce(*debounce)
            return "Power settings saved"

        self._submit("Saving power settings", job)


# ---------------------------------------------------------------------------
# Device: pairing, factory reset, profile import/export
# ---------------------------------------------------------------------------


class DevicePage(Page):
    profile_replaced = Signal(object)
    check_requested = Signal()
    download_requested = Signal()

    def __init__(self, profile: Profile, parent: Optional[QWidget] = None) -> None:
        super().__init__(profile, parent)
        self.apply_button.hide()
        self.revert_button.hide()

        pair = QPushButton("Pair mouse with receiver…")
        pair.clicked.connect(self._pair)
        reset = QPushButton("Restore factory settings…")
        reset.clicked.connect(self._reset)
        reapply = QPushButton("Write saved profile to mouse")
        reapply.clicked.connect(self._reapply)
        export = QPushButton("Export profile…")
        export.clicked.connect(self._export)
        import_ = QPushButton("Import profile…")
        import_.clicked.connect(self._import)
        self.firmware_label = QLabel("Installed: checking when the mouse is awake\u2026")
        self.firmware_label.setWordWrap(True)
        self.online_label = QLabel("New firmware comes with SteelSeries GG releases; "
                                   "ssmouse checks SteelSeries' release feed for them.")
        self.online_label.setWordWrap(True)
        self.online_label.setOpenExternalLinks(True)
        self.progress = QProgressBar()
        self.progress.hide()
        self.check_button = QPushButton("Check for updates")
        self.check_button.clicked.connect(self.check_requested)
        self.download_button = QPushButton("Download firmware")
        self.download_button.setToolTip("Downloads the SteelSeries GG installer (about 430 MB) "
                                        "from SteelSeries and keeps only the firmware")
        self.download_button.clicked.connect(self.download_requested)
        self.download_button.hide()
        self.install_button = QPushButton("Install firmware")
        self.install_button.clicked.connect(lambda: self._confirm_install(self.package))
        self.install_button.hide()
        from_folder = QPushButton("From folder\u2026")
        from_folder.setToolTip("Select the firmware/272111664 folder of a SteelSeries GG install")
        from_folder.clicked.connect(self._update_firmware)
        self.auto_check = QCheckBox("Check for firmware and ssmouse updates daily")
        self.package: Optional[firmware.FirmwarePackage] = None
        self.writers = [reset, reapply, import_, from_folder, self.install_button]
        firmware_box = QGroupBox("Firmware and updates")
        fwl = QVBoxLayout(firmware_box)
        fwl.addWidget(self.firmware_label)
        fwl.addWidget(self.online_label)
        fwl.addWidget(self.progress)
        buttons = QHBoxLayout()
        for button in (self.check_button, self.download_button, self.install_button,
                       from_folder):
            buttons.addWidget(button)
        buttons.addStretch(1)
        fwl.addLayout(buttons)
        fwl.addWidget(self.auto_check)

        wireless = QGroupBox("Wireless")
        wl = QVBoxLayout(wireless)
        pair_note = QLabel("Use pairing after replacing the receiver, or if the wheel keeps "
                           "blinking white with fresh batteries.")
        pair_note.setWordWrap(True)
        wl.addWidget(pair_note)
        wl.addWidget(pair, 0, Qt.AlignmentFlag.AlignLeft)
        profiles = QGroupBox("Profile")
        pl = QHBoxLayout(profiles)
        pl.addWidget(reapply)
        pl.addWidget(export)
        pl.addWidget(import_)
        pl.addStretch(1)
        factory = QGroupBox("Factory reset")
        fl = QVBoxLayout(factory)
        fl.addWidget(QLabel("Resets DPI, polling rate, power, lighting and buttons."))
        fl.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)

        layout = QVBoxLayout(self)
        layout.addWidget(wireless)
        layout.addWidget(profiles)
        layout.addWidget(factory)
        layout.addWidget(firmware_box)
        layout.addStretch(1)
        layout.addLayout(self._footer())

    def set_linked(self, linked: bool) -> None:
        for button in self.writers:
            button.setEnabled(linked)

    def reload(self) -> None:
        status = self.config.firmware
        if status is None:
            return
        newest = self.package.version if self.package else status.latest_known
        installed = [v for v in (status.mouse, status.receiver) if v]
        outdated = bool(installed) and any(
            firmware.parse_version(v) < firmware.parse_version(newest) for v in installed)
        self.firmware_label.setText(
            "Installed: mouse %s, receiver %s. Newest available: %s \u2014 %s."
            % (status.mouse or "?", status.receiver or "?", newest,
               "update available" if outdated else "up to date"))
        self.install_button.setVisible(outdated and self.package is not None)
        if self.package is not None:
            self.install_button.setText("Install firmware %s" % self.package.version)

    def set_package(self, package: Optional[firmware.FirmwarePackage]) -> None:
        self.package = package
        self.reload()

    def show_check_result(self, gg_release, app_release, error: Optional[str]) -> None:
        import time as _time

        stamp = _time.strftime("%H:%M")
        if error:
            self.online_label.setText("Update check failed (%s): %s" % (stamp, error))
            return
        parts = []
        if gg_release is None:
            parts.append("No newer SteelSeries GG release, so no new firmware (checked %s)."
                         % stamp)
        else:
            parts.append("SteelSeries GG %s is out and may contain new firmware."
                         % gg_release.version)
        if app_release is not None:
            parts.append('ssmouse %s is available: <a href="%s">download</a>.'
                         % (app_release.version, app_release.url))
        self.online_label.setText(" ".join(parts))
        self.download_button.setVisible(gg_release is not None)

    def set_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)

    def show_progress(self, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, 100)
            self.progress.setValue(done * 100 // total)
            self.progress.setFormat("Downloading SteelSeries GG: %%p%% of %d MB" % (total >> 20))

    def _update_firmware(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "SteelSeries GG firmware folder (firmware/272111664)")
        if not folder:
            return
        from pathlib import Path

        try:
            package = firmware.load_package(Path(folder))
        except (OSError, ValueError, firmware.FirmwareError) as exc:
            QMessageBox.warning(self, "Firmware", str(exc))
            return
        self._confirm_install(package)

    def _confirm_install(self, package: Optional[firmware.FirmwarePackage]) -> None:
        if package is None:
            return
        answer = QMessageBox.warning(
            self, "Install firmware %s" % package.version,
            "This takes about a minute. Do not switch the mouse off or unplug the "
            "receiver until it finishes; an interrupted update can make them unusable."
            "\n\nInstall firmware %s now?" % package.version,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def job(mouse: Mouse) -> str:
            firmware.update(mouse, package)
            return "Firmware %s installed; the mouse and receiver restart now" % package.version

        self.apply_requested.emit(
            "Installing firmware %s \u2014 do not switch off the mouse" % package.version, job)

    def _pair(self) -> None:
        answer = QMessageBox.information(
            self, "Pair mouse",
            "After you press OK, switch the mouse off and on again in 2.4 GHz mode "
            "within 20 seconds.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return
        self.apply_requested.emit(
            "Pairing (switch the mouse off and on now)",
            lambda mouse: "Mouse paired" if mouse.pair() else "Pairing timed out",
        )

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self, "Restore factory settings",
            "Reset all mouse settings to factory defaults?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        fresh = Profile()
        self.profile_replaced.emit(fresh)

        def job(mouse: Mouse) -> str:
            mouse.reset_profile()
            return "Factory settings restored"

        self.apply_requested.emit("Restoring factory settings", job)

    def _reapply(self) -> None:
        profile = self.profile
        self.apply_requested.emit(
            "Writing saved profile",
            lambda mouse: "Applied: %s" % (", ".join(apply_profile(mouse, profile))
                                            or "nothing configured"),
        )

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", "ssmouse-profile.json",
                                              "JSON (*.json)")
        if path:
            from pathlib import Path

            self.profile.save(Path(path))

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "JSON (*.json)")
        if not path:
            return
        import json
        from pathlib import Path

        try:
            profile = Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "ssmouse", "Could not import: %s" % exc)
            return
        self.profile_replaced.emit(profile)
        self.apply_requested.emit(
            "Applying imported profile",
            lambda mouse: "Applied: %s" % (", ".join(apply_profile(mouse, profile))
                                            or "nothing configured"),
        )
