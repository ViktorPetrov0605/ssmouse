"""Painted widgets and the tray icon."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from .model import battery_tier

TIER_COLORS = {
    "ok": QColor("#2ea043"),
    "low": QColor("#d29922"),
    "critical": QColor("#da3633"),
    "unknown": QColor("#6e7781"),
}
STALE_COLOR = QColor("#6e7781")


def level_color(level: Optional[int], stale: bool = False) -> QColor:
    return STALE_COLOR if stale else TIER_COLORS[battery_tier(level)]


def tray_icon(level: Optional[int], stale: bool = False) -> QIcon:
    """Rounded badge with the battery percentage, legible at 16-24 px."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        icon.addPixmap(_tray_pixmap(level, stale, size))
    return icon


def _tray_pixmap(level: Optional[int], stale: bool, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    rect = QRectF(0.5, 0.5, size - 1, size - 1)
    radius = size * 0.22
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 90))
    painter.drawRoundedRect(rect, radius, radius)

    # Fill from the bottom proportional to the level.
    color = level_color(level, stale)
    if level is not None:
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        height = rect.height() * max(0.12, min(level, 100) / 100.0)
        painter.setBrush(color)
        painter.drawRect(QRectF(rect.left(), rect.bottom() - height, rect.width(), height))
        painter.setClipping(False)

    painter.setPen(QPen(color.lighter(130), max(1.0, size / 22.0)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, radius, radius)

    text = "?" if level is None else str(min(level, 100))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * (0.58 if len(text) < 3 else 0.42)))
    painter.setFont(font)
    outline = QPainterPath()
    metrics = painter.fontMetrics()
    x = (size - metrics.horizontalAdvance(text)) / 2.0
    y = (size + metrics.capHeight()) / 2.0
    outline.addText(x, y, font, text)
    painter.setPen(QPen(QColor(0, 0, 0, 160), max(1.0, size / 16.0)))
    painter.drawPath(outline)
    painter.fillPath(outline, QColor("white"))
    painter.end()
    return pixmap


class BatteryGauge(QWidget):
    """A large horizontal battery with the percentage inside."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._level: Optional[int] = None
        self._stale = False
        self.setMinimumSize(220, 90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(300, 100)

    def set_level(self, level: Optional[int], stale: bool = False) -> None:
        self._level, self._stale = level, stale
        self.setAccessibleName(
            "Battery %s%s" % ("unknown" if level is None else "%d%%" % level,
                              " (last known)" if stale else "")
        )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        fg = palette.color(QPalette.ColorRole.WindowText)

        nub_w = 10.0
        body = QRectF(4, 6, self.width() - 8 - nub_w, self.height() - 12)
        nub = QRectF(body.right(), body.center().y() - body.height() * 0.18,
                     nub_w - 2, body.height() * 0.36)

        frame = QColor(fg)
        frame.setAlpha(150)
        painter.setPen(QPen(frame, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body, 12, 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(frame)
        painter.drawRoundedRect(nub, 3, 3)

        color = level_color(self._level, self._stale)
        if self._level is not None:
            inner = body.adjusted(7, 7, -7, -7)
            width = inner.width() * max(0.0, min(self._level, 100)) / 100.0
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(inner.left(), inner.top(), width, inner.height()), 7, 7)

        text = "--" if self._level is None else "%d%%" % self._level
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(int(body.height() * 0.48))
        painter.setFont(font)
        path = QPainterPath()
        metrics = painter.fontMetrics()
        x = body.center().x() - metrics.horizontalAdvance(text) / 2.0
        y = body.center().y() + metrics.capHeight() / 2.0
        path.addText(x, y, font, text)
        painter.setPen(QPen(QColor(0, 0, 0, 130), 3))
        painter.drawPath(path)
        painter.fillPath(path, QColor("white"))
        painter.end()


class Chip(QLabel):
    """Small rounded label used for DPI presets and the status pill."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_style(None)

    def set_style(self, color: Optional[QColor], filled: bool = False) -> None:
        if color is None:
            self.setStyleSheet(
                "QLabel { border: 1px solid palette(mid); border-radius: 9px;"
                " padding: 3px 10px; }"
            )
            return
        if filled:
            self.setStyleSheet(
                "QLabel { background: %s; color: white; border-radius: 9px;"
                " padding: 3px 10px; font-weight: bold; }" % color.name()
            )
        else:
            self.setStyleSheet(
                "QLabel { border: 1px solid %s; color: %s; border-radius: 9px;"
                " padding: 3px 10px; }" % (color.name(), color.name())
            )
