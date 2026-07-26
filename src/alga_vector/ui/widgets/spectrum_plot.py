"""Spectrum and waterfall rendering without OpenGL or invented live data."""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from collections import deque
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QSplitter, QVBoxLayout, QWidget

from ..runtime import attr
from ..theme import Colors


def _deterministic_power(sequence: int, count: int = 256) -> list[float]:
    """Generate a stable demo trace; the same sequence always yields the same values."""

    phase = (sequence % 97) / 97.0
    values: list[float] = []
    for index in range(count):
        x = index / max(1, count - 1)
        floor = -94.0 + 2.2 * math.sin((x * 31.0 + phase) * math.tau)
        broad = 44.0 * math.exp(-((x - 0.50) / 0.045) ** 2)
        side = 21.0 * math.exp(-((x - 0.28 - phase * 0.02) / 0.022) ** 2)
        narrow = 15.0 * math.exp(-((x - 0.72) / 0.012) ** 2)
        values.append(floor + broad + side + narrow)
    return values


def _coerce_power(frame: object | None) -> list[float]:
    raw = attr(frame, "power_dbm")
    if raw is None:
        return []
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    try:
        values = [float(value) for value in raw]
    except (TypeError, ValueError):
        return []
    if not values:
        return []
    maximum = 320
    step = max(1, len(values) // maximum)
    return values[::step][:maximum]


class SpectrumPlot(QWidget):
    """CPU-painted trace suitable for deterministic CI snapshots."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._sequence = 0
        self._power: list[float] = []
        self._center_hz = 433_920_000
        self._span_hz = 2_000_000
        self._threshold_level = -72.4
        self._unit = "dBFS"
        self._marker_index = 0

    @property
    def power_values(self) -> tuple[float, ...]:
        return tuple(self._power)

    def set_frame(self, frame: object | None) -> None:
        self._sequence = int(attr(frame, "sequence", self._sequence + 1))
        self._center_hz = int(attr(frame, "center_frequency_hz", self._center_hz))
        self._span_hz = int(attr(frame, "span_hz", self._span_hz))
        self._unit = str(attr(frame, "unit", self._unit))
        self._power = _coerce_power(frame)
        self._marker_index = (
            max(range(len(self._power)), key=self._power.__getitem__)
            if self._power
            else 0
        )
        self.update()

    def set_demo_sequence(self, sequence: int) -> None:
        self._sequence = sequence
        self._power = _deterministic_power(sequence)
        self._marker_index = max(range(len(self._power)), key=self._power.__getitem__)
        self.update()

    def set_threshold(self, value: float) -> None:
        self._threshold_level = max(-200.0, min(30.0, float(value)))
        self.update()

    def clear(self) -> None:
        """Remove the last frame instead of fabricating a replacement trace."""

        self._power = []
        self._marker_index = 0
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.BG))
        plot = self.rect().adjusted(46, 12, -12, -28)
        if plot.width() <= 1 or plot.height() <= 1:
            return

        grid_pen = QPen(QColor(Colors.BORDER), 1)
        painter.setPen(grid_pen)
        for index in range(1, 5):
            y = plot.top() + plot.height() * index / 5
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
        for index in range(1, 8):
            x = plot.left() + plot.width() * index / 8
            painter.drawLine(int(x), plot.top(), int(x), plot.bottom())

        font = QFont(self.font())
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor(Colors.MUTED))
        for index, label in enumerate(("-20", "-40", "-60", "-80", "-100")):
            y = plot.top() + plot.height() * index / 5 + 5
            painter.drawText(4, int(y), label)

        threshold_y = self._value_y(self._threshold_level, plot)
        painter.setPen(QPen(QColor(Colors.WARNING), 1, Qt.PenStyle.DashLine))
        painter.drawLine(plot.left(), int(threshold_y), plot.right(), int(threshold_y))
        painter.drawText(plot.left() + 5, int(threshold_y - 4), "ПОРОГ")

        if not self._power:
            painter.setPen(QColor(Colors.TEXT_SECONDARY))
            message = "НЕТ ИЗМЕРЕННЫХ ДАННЫХ"
            width = painter.fontMetrics().horizontalAdvance(message)
            painter.drawText(
                int(plot.center().x() - width / 2),
                int(plot.center().y()),
                message,
            )
            self._draw_frequency_axis(painter, plot)
            return

        path = QPainterPath()
        for index, value in enumerate(self._power):
            x = plot.left() + plot.width() * index / max(1, len(self._power) - 1)
            y = self._value_y(value, plot)
            point = QPointF(x, y)
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(QColor(Colors.READY), 1.5))
        painter.drawPath(path)

        marker_x = (
            plot.left()
            + plot.width() * self._marker_index / max(1, len(self._power) - 1)
        )
        marker_value = self._power[self._marker_index]
        marker_y = self._value_y(marker_value, plot)
        painter.setPen(QPen(QColor(Colors.TEAL), 1))
        painter.drawLine(int(marker_x), plot.top(), int(marker_x), plot.bottom())
        painter.setBrush(QColor(Colors.TEAL))
        painter.drawEllipse(QPointF(marker_x, marker_y), 3.0, 3.0)
        painter.setPen(QColor(Colors.TEXT))
        painter.drawText(
            int(min(marker_x + 6, plot.right() - 118)),
            int(max(plot.top() + 14, marker_y - 7)),
            f"M1  {marker_value:.1f} {self._unit}",
        )

        self._draw_frequency_axis(painter, plot)

    def _draw_frequency_axis(self, painter: QPainter, plot: QRect) -> None:
        left_mhz = (self._center_hz - self._span_hz / 2) / 1_000_000
        center_mhz = self._center_hz / 1_000_000
        right_mhz = (self._center_hz + self._span_hz / 2) / 1_000_000
        painter.setPen(QColor(Colors.MUTED))
        painter.drawText(plot.left(), self.height() - 8, f"{left_mhz:.3f} МГц")
        center_text = f"{center_mhz:.3f} МГц"
        center_width = painter.fontMetrics().horizontalAdvance(center_text)
        painter.drawText(
            int(plot.center().x() - center_width / 2), self.height() - 8, center_text
        )
        right_text = f"{right_mhz:.3f} МГц"
        right_width = painter.fontMetrics().horizontalAdvance(right_text)
        painter.drawText(plot.right() - right_width, self.height() - 8, right_text)

    @staticmethod
    def _value_y(value: float, rect: QRect) -> float:
        bounded = max(-110.0, min(-10.0, value))
        ratio = (-10.0 - bounded) / 100.0
        return float(rect.top() + rect.height() * ratio)


class WaterfallPlot(QWidget):
    """Discrete power bands; deliberately no QGradient usage."""

    _PALETTE = (
        QColor("#07100F"),
        QColor("#102421"),
        QColor("#17443A"),
        QColor(Colors.TEAL),
        QColor(Colors.READY),
        QColor(Colors.WARNING),
        QColor(Colors.CRITICAL),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(210)
        self._rows: deque[list[float]] = deque(maxlen=54)

    @property
    def row_count(self) -> int:
        return len(self._rows)

    def append_power(self, power: Sequence[float]) -> None:
        values = [float(value) for value in power]
        if not values:
            return
        maximum = 220
        step = max(1, len(values) // maximum)
        self._rows.append(values[::step][:maximum])
        self.update()

    def clear(self) -> None:
        self._rows.clear()
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Colors.BG))
        if not self._rows:
            painter.setPen(QColor(Colors.TEXT_SECONDARY))
            message = "ВОДОПАД · ОЖИДАНИЕ ИЗМЕРЕННЫХ ДАННЫХ"
            width = painter.fontMetrics().horizontalAdvance(message)
            painter.drawText(
                int(self.rect().center().x() - width / 2),
                int(self.rect().center().y()),
                message,
            )
            return
        rows = tuple(self._rows)
        row_height = self.height() / len(rows)
        for row_index, row in enumerate(reversed(rows)):
            cell_width = self.width() / max(1, len(row))
            y = row_index * row_height
            for column, value in enumerate(row):
                painter.fillRect(
                    QRectF(column * cell_width, y, cell_width + 0.6, row_height + 0.6),
                    self._color_for(value),
                )
        font = QFont(self.font())
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        painter.drawText(8, 17, "ВОДОПАД · НОВЫЕ ДАННЫЕ СВЕРХУ")

    @classmethod
    def _color_for(cls, value: float) -> QColor:
        thresholds = (-92.0, -84.0, -74.0, -62.0, -50.0, -38.0)
        index = 0
        for threshold in thresholds:
            if value >= threshold:
                index += 1
        return cls._PALETTE[min(index, len(cls._PALETTE) - 1)]


class SpectrumDisplay(QWidget):
    """Vertically coupled spectrum and waterfall."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {Colors.BORDER}; height: 5px; }}"
        )
        self.spectrum = SpectrumPlot()
        self.waterfall = WaterfallPlot()
        splitter.addWidget(self.spectrum)
        splitter.addWidget(self.waterfall)
        splitter.setSizes([260, 360])
        layout.addWidget(splitter)

    def set_frame(self, frame: object | None) -> None:
        self.spectrum.set_frame(frame)
        self.waterfall.append_power(self.spectrum.power_values)

    def set_demo_sequence(self, sequence: int) -> None:
        self.spectrum.set_demo_sequence(sequence)
        self.waterfall.append_power(self.spectrum.power_values)

    def clear(self) -> None:
        self.spectrum.clear()
        self.waterfall.clear()

    def set_threshold(self, value: float) -> None:
        self.spectrum.set_threshold(value)
