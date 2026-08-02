"""Compact, honest angular view for validated direction observations."""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Colors


@dataclass(frozen=True, slots=True)
class SectorViewState:
    """Only explicit angular evidence; range and geographic position are absent."""

    available: bool
    label: str
    detail: str
    bearing_deg: float | None = None
    sector_start_deg: float | None = None
    sector_end_deg: float | None = None
    source: str = ""


class _SectorCanvas(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sectorCanvas")
        self.setMinimumHeight(74)
        self._state = SectorViewState(
            available=False,
            label="Направление недоступно",
            detail="Нет валидного пеленга.",
        )

    def set_state(self, state: SectorViewState) -> None:
        self._state = state
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.NAV))

        diameter = float(min(self.width() - 72, self.height() - 18))
        diameter = max(64.0, diameter)
        center = QPointF(self.width() / 2.0, self.height() / 2.0 + 2.0)
        compass = QRectF(
            center.x() - diameter / 2.0,
            center.y() - diameter / 2.0,
            diameter,
            diameter,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.BORDER_STRONG), 1.0))
        painter.drawEllipse(compass)

        radius = diameter / 2.0
        for bearing in range(0, 360, 30):
            outer = self._point(center, radius, float(bearing))
            inner = self._point(
                center,
                radius - (6.0 if bearing % 90 else 10.0),
                float(bearing),
            )
            painter.drawLine(inner, outer)

        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        painter.drawText(
            QRectF(center.x() - 10.0, compass.top() + 4.0, 20.0, 16.0),
            Qt.AlignmentFlag.AlignCenter,
            "N",
        )
        painter.drawText(
            QRectF(compass.right() - 19.0, center.y() - 8.0, 16.0, 16.0),
            Qt.AlignmentFlag.AlignCenter,
            "E",
        )
        painter.drawText(
            QRectF(center.x() - 10.0, compass.bottom() - 20.0, 20.0, 16.0),
            Qt.AlignmentFlag.AlignCenter,
            "S",
        )
        painter.drawText(
            QRectF(compass.left() + 3.0, center.y() - 8.0, 16.0, 16.0),
            Qt.AlignmentFlag.AlignCenter,
            "W",
        )

        if not self._state.available:
            painter.setPen(QPen(QColor(Colors.MUTED), 2.0))
            painter.drawLine(
                QPointF(center.x() - 12.0, center.y()),
                QPointF(center.x() + 12.0, center.y()),
            )
            painter.setPen(QColor(Colors.MUTED))
            painter.drawText(
                QRectF(4.0, center.y() + 14.0, self.width() - 8.0, 18.0),
                Qt.AlignmentFlag.AlignCenter,
                "НЕТ ВАЛИДНОГО ПЕЛЕНГА",
            )
            return

        start = self._state.sector_start_deg
        end = self._state.sector_end_deg
        if start is not None and end is not None:
            span = (end - start) % 360.0
            if math.isclose(span, 0.0):
                span = 1.0
            sector_color = QColor(Colors.TEAL)
            sector_color.setAlpha(72)
            painter.setBrush(sector_color)
            painter.setPen(QPen(QColor(Colors.TEAL), 1.0))
            painter.drawPie(
                compass,
                int((90.0 - start) * 16.0),
                int(-span * 16.0),
            )

        if self._state.bearing_deg is not None:
            endpoint = self._point(
                center,
                radius - 8.0,
                self._state.bearing_deg,
            )
            painter.setPen(QPen(QColor(Colors.READY), 2.0))
            painter.drawLine(center, endpoint)
            painter.setBrush(QColor(Colors.READY))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(endpoint, 3.5, 3.5)

        painter.setBrush(QColor(Colors.READY))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, 3.0, 3.0)

    @staticmethod
    def _point(center: QPointF, radius: float, bearing_deg: float) -> QPointF:
        radians = math.radians(bearing_deg - 90.0)
        return QPointF(
            center.x() + math.cos(radians) * radius,
            center.y() + math.sin(radians) * radius,
        )


class CompactSectorView(QFrame):
    """Compact sector card; never displays distance rings or a target location."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("compactSectorView")
        self.setProperty("panel", "true")
        self.setMinimumHeight(146)
        self.setMaximumHeight(166)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 8)
        layout.setSpacing(3)
        caption = QLabel("НАПРАВЛЕНИЕ")
        caption.setProperty("sectionHeading", "true")
        layout.addWidget(caption)

        self.canvas = _SectorCanvas()
        layout.addWidget(self.canvas, 1)

        self.value_label = QLabel("Пеленгация недоступна")
        self.value_label.setObjectName("situationDirection")
        self.value_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

        self.detail_label = QLabel("Нет подтверждённых данных направления.")
        self.detail_label.setObjectName("situationDirectionDetail")
        self.detail_label.setProperty("muted", "true")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

    def set_direction(self, state: SectorViewState) -> None:
        self.canvas.set_state(state)
        self.value_label.setText(state.label)
        detail = state.detail
        if state.available and state.source:
            detail = f"{detail} · Источник: {state.source}"
        self.detail_label.setText(detail)
        self.setToolTip(
            f"{state.label}\n{detail}\n"
            "Дальность и положение по одному пеленгу не определяются."
        )


__all__ = ["CompactSectorView", "SectorViewState"]
