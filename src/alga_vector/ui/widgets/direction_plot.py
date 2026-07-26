"""CPU-painted 360° direction panel with strict provenance."""

from __future__ import annotations

# ruff: noqa: RUF001
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from alga_vector.direction import (
    SOURCE_LABELS_RU,
    DirectionObservation,
    DirectionSnapshot,
    DirectionSource,
    DirectionTrailPoint,
)

from ..theme import Colors


class DirectionPlot(QWidget):
    """Render a bearing, uncertainty cone, and angular history.

    The widget has no API for position or range.  An unavailable observation
    always produces a clear empty state instead of retaining the previous ray.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 360)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._snapshot: DirectionSnapshot | None = None
        self._empty_reason = "Валидный источник направления не подключён."

    @property
    def snapshot(self) -> DirectionSnapshot | None:
        return self._snapshot

    @property
    def current_observation(self) -> DirectionObservation | None:
        return self._snapshot.current if self._snapshot is not None else None

    @property
    def empty_reason(self) -> str:
        return self._empty_reason

    def sizeHint(self) -> QSize:
        return QSize(620, 520)

    def set_snapshot(self, snapshot: DirectionSnapshot | None) -> None:
        self._snapshot = snapshot
        if snapshot is None:
            self._empty_reason = "Состояние направления не получено."
        elif not snapshot.available:
            self._empty_reason = snapshot.current.message_ru
        self.update()

    def clear(self, reason_ru: str = "Направление недоступно.") -> None:
        self._snapshot = None
        self._empty_reason = str(reason_ru).strip() or "Направление недоступно."
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.BG))

        margin = 28.0
        footer_space = 32.0
        available_height = max(1.0, self.height() - footer_space - margin * 2.0)
        radius = max(
            48.0,
            min((self.width() - margin * 2.0) / 2.0, available_height / 2.0),
        )
        center = QPointF(self.width() / 2.0, margin + available_height / 2.0)
        circle = QRectF(
            center.x() - radius,
            center.y() - radius,
            radius * 2.0,
            radius * 2.0,
        )

        self._draw_angular_grid(painter, center, radius, circle)
        snapshot = self._snapshot
        if snapshot is None or not snapshot.available:
            self._draw_empty_state(painter, center, radius)
        else:
            self._draw_trail(painter, center, radius, snapshot.trail)
            self._draw_current(painter, center, radius, snapshot.current)

        painter.setPen(QColor(Colors.MUTED))
        footer_font = QFont(self.font())
        footer_font.setPixelSize(10)
        footer_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        painter.setFont(footer_font)
        painter.drawText(
            QRectF(0.0, self.height() - 27.0, float(self.width()), 20.0),
            Qt.AlignmentFlag.AlignCenter,
            "ТОЛЬКО УГЛОВОЕ НАБЛЮДЕНИЕ · ПОЛОЖЕНИЕ НЕ РАССЧИТЫВАЕТСЯ",
        )

    def _draw_angular_grid(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
        circle: QRectF,
    ) -> None:
        painter.setBrush(QColor(Colors.NAV))
        painter.setPen(QPen(QColor(Colors.BORDER_STRONG), 1.2))
        painter.drawEllipse(circle)

        for fraction in (0.33, 0.66):
            inner_radius = radius * fraction
            painter.setPen(QPen(QColor(Colors.BORDER), 1))
            painter.drawEllipse(
                QRectF(
                    center.x() - inner_radius,
                    center.y() - inner_radius,
                    inner_radius * 2.0,
                    inner_radius * 2.0,
                )
            )

        for bearing in range(0, 360, 15):
            major = bearing % 30 == 0
            inner = radius * (0.18 if major else 0.90)
            start = self._point(center, inner, float(bearing))
            end = self._point(center, radius, float(bearing))
            painter.setPen(
                QPen(
                    QColor(Colors.BORDER_STRONG if major else Colors.BORDER),
                    1.0,
                )
            )
            painter.drawLine(start, end)

        label_font = QFont(self.font())
        label_font.setPixelSize(10)
        painter.setFont(label_font)
        painter.setPen(QColor(Colors.MUTED))
        for bearing in range(0, 360, 30):
            point = self._point(center, radius * 0.80, float(bearing))
            rect = QRectF(point.x() - 18.0, point.y() - 8.0, 36.0, 16.0)
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                f"{bearing:03d}°",
            )

        cardinal_font = QFont(self.font())
        cardinal_font.setPixelSize(13)
        cardinal_font.setBold(True)
        painter.setFont(cardinal_font)
        for cardinal_bearing, label in (
            (0.0, "С"),
            (90.0, "В"),
            (180.0, "Ю"),
            (270.0, "З"),
        ):
            point = self._point(center, radius + 16.0, cardinal_bearing)
            rect = QRectF(point.x() - 14.0, point.y() - 10.0, 28.0, 20.0)
            painter.setPen(
                QColor(Colors.READY if cardinal_bearing == 0.0 else Colors.TEXT_SECONDARY)
            )
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QPen(QColor(Colors.BORDER_STRONG), 1.2))
        painter.setBrush(QColor(Colors.SURFACE))
        painter.drawEllipse(center, 5.0, 5.0)

    def _draw_trail(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
        trail: tuple[DirectionTrailPoint, ...],
    ) -> None:
        points = trail[-16:]
        if len(points) < 2:
            return
        denominator = max(1, len(points) - 1)
        previous: QPointF | None = None
        for index, item in enumerate(points):
            fraction = index / denominator
            point = self._point(
                center,
                radius * (0.68 + fraction * 0.18),
                item.bearing_deg,
            )
            alpha = int(35 + fraction * 125)
            color = self._source_color(item.source)
            color.setAlpha(alpha)
            if previous is not None:
                painter.setPen(QPen(color, 1.0, Qt.PenStyle.DotLine))
                painter.drawLine(previous, point)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(point, 2.0 + fraction * 1.6, 2.0 + fraction * 1.6)
            previous = point

    def _draw_current(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
        observation: DirectionObservation,
    ) -> None:
        bearing = observation.bearing_deg
        uncertainty = observation.uncertainty_deg
        if bearing is None or uncertainty is None:
            self._draw_empty_state(
                painter,
                center,
                radius,
                "Измерение не содержит валидного угла.",
            )
            return

        cone = QPainterPath()
        cone.moveTo(center)
        cone.arcTo(
            QRectF(
                center.x() - radius * 0.94,
                center.y() - radius * 0.94,
                radius * 1.88,
                radius * 1.88,
            ),
            90.0 - (bearing + uncertainty),
            2.0 * uncertainty,
        )
        cone.closeSubpath()
        cone_color = self._source_color(observation.source)
        cone_color.setAlpha(44)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(cone_color)
        painter.drawPath(cone)

        beam_color = self._source_color(observation.source)
        endpoint = self._point(center, radius * 0.94, bearing)
        painter.setPen(QPen(beam_color, 2.4))
        painter.drawLine(center, endpoint)
        painter.setPen(QPen(QColor(Colors.BG), 1.0))
        painter.setBrush(beam_color)
        painter.drawEllipse(endpoint, 6.0, 6.0)

        bearing_font = QFont(self.font())
        bearing_font.setPixelSize(max(23, int(radius * 0.12)))
        bearing_font.setBold(True)
        painter.setFont(bearing_font)
        painter.setPen(QColor(Colors.TEXT))
        painter.drawText(
            QRectF(
                center.x() - radius * 0.42,
                center.y() - 42.0,
                radius * 0.84,
                42.0,
            ),
            Qt.AlignmentFlag.AlignCenter,
            f"{bearing:05.1f}°",
        )

        detail_font = QFont(self.font())
        detail_font.setPixelSize(11)
        painter.setFont(detail_font)
        painter.setPen(beam_color)
        source_label = SOURCE_LABELS_RU[observation.source]
        painter.drawText(
            QRectF(
                center.x() - radius * 0.48,
                center.y() + 2.0,
                radius * 0.96,
                38.0,
            ),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            f"±{uncertainty:.1f}° · {source_label}",
        )

    def _draw_empty_state(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
        reason_ru: str | None = None,
    ) -> None:
        title_font = QFont(self.font())
        title_font.setPixelSize(19)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        painter.drawText(
            QRectF(
                center.x() - radius * 0.62,
                center.y() - 34.0,
                radius * 1.24,
                32.0,
            ),
            Qt.AlignmentFlag.AlignCenter,
            "Направление недоступно",
        )

        message_font = QFont(self.font())
        message_font.setPixelSize(11)
        painter.setFont(message_font)
        painter.setPen(QColor(Colors.MUTED))
        painter.drawText(
            QRectF(
                center.x() - radius * 0.60,
                center.y() + 4.0,
                radius * 1.20,
                58.0,
            ),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            reason_ru or self._empty_reason,
        )

    @staticmethod
    def _point(center: QPointF, radius: float, bearing_deg: float) -> QPointF:
        angle = math.radians(bearing_deg)
        return QPointF(
            center.x() + radius * math.sin(angle),
            center.y() - radius * math.cos(angle),
        )

    @staticmethod
    def _source_color(source: DirectionSource) -> QColor:
        return QColor(Colors.TEAL if source is DirectionSource.EXTERNAL else Colors.WARNING)


__all__ = ["DirectionPlot"]
