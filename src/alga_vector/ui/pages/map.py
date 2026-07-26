"""Offline/online map with honest cartographic navigation tools.

The overlays in this module never infer an RF emitter position. Rings, map
measurements and manual bearings are drawn only relative to the locally held
base point and are explicitly labelled as cartographic/operator input.
"""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from alga_vector.location import (
    WEB_MERCATOR_MAX_LATITUDE,
    GeoPoint,
    destination_point,
    geodesic_ring,
    haversine_distance_m,
    initial_bearing_deg,
    latlon_to_world,
    world_to_latlon,
    wrapped_world_x_delta,
)
from alga_vector.location.geometry import EARTH_MEAN_RADIUS_M

from ..runtime import attr, call_runtime, current_snapshot, value_of
from ..theme import Colors
from ..widgets import InlineNotice, MetricTile, Panel
from .common import OperatorPage

_TILE_SIZE = 256
_WEB_MERCATOR_RADIUS_M = 6_378_137.0
_MAX_UNAMBIGUOUS_RING_RADIUS_M = math.nextafter(
    math.pi * EARTH_MEAN_RADIUS_M,
    0.0,
)
_FALLBACK_GRID_LABEL = "ФОНОВАЯ СЕТКА · НЕТ ДОСТУПНЫХ ТАЙЛОВ"
_USABLE_LOCATION_STATUSES = frozenset(
    {"verified", "manual_unverified", "collecting"}
)


class MapInteractionMode(StrEnum):
    OVERVIEW = "overview"
    MEASURE = "measure"
    MANUAL_BEARING = "manual_bearing"


@dataclass(slots=True, frozen=True)
class CartographicMeasurement:
    """Coordinate-free result safe to present or include in diagnostics."""

    distance_m: float
    bearing_deg: float | None

    def __post_init__(self) -> None:
        if not math.isfinite(self.distance_m) or self.distance_m < 0.0:
            raise ValueError("distance_m must be finite and non-negative")
        if self.bearing_deg is not None and not math.isfinite(self.bearing_deg):
            raise ValueError("bearing_deg must be finite")

    @property
    def summary_ru(self) -> str:
        distance = _format_distance(self.distance_m)
        bearing = (
            f"{self.bearing_deg % 360.0:05.1f}° ист."
            if self.bearing_deg is not None
            else "азимут не определён"
        )
        return (
            f"{distance} · {bearing}\n"
            "Картографическое измерение от базы — не RF-позиция."
        )


@dataclass(slots=True, frozen=True)
class MapLocationGate:
    status: str
    has_base: bool
    tools_allowed: bool
    tone: str
    title_ru: str
    message_ru: str


def map_location_gate(
    status: str,
    *,
    has_base: bool,
    base_in_coverage: bool | None = None,
) -> MapLocationGate:
    """Map every location state to an explicit and testable UI policy."""

    normalized = str(status)
    if (
        base_in_coverage is False
        and has_base
        and normalized in _USABLE_LOCATION_STATUSES
    ):
        return MapLocationGate(
            normalized,
            True,
            False,
            "critical",
            "База вне покрытия карты",
            "Картографические инструменты заблокированы: выберите карту, "
            "которая покрывает базовую точку.",
        )
    if normalized == "verified" and has_base:
        return MapLocationGate(
            normalized,
            True,
            True,
            "ready",
            "База подтверждена",
            "Кольца и измерения считаются от проверенной локальной точки.",
        )
    if normalized == "manual_unverified" and has_base:
        return MapLocationGate(
            normalized,
            True,
            True,
            "warning",
            "Ручная база не проверена",
            "Геометрия доступна, но результат сместится при ошибке ручной точки.",
        )
    if normalized == "collecting" and has_base:
        return MapLocationGate(
            normalized,
            True,
            True,
            "warning",
            "GPS-проверка ещё идёт",
            "Можно работать как со справочной картой; база пока не подтверждена.",
        )
    if normalized == "stale" and has_base:
        return MapLocationGate(
            normalized,
            True,
            False,
            "critical",
            "GPS-данные устарели",
            "Новые измерения заблокированы до свежей проверки базы.",
        )
    if normalized == "conflict" and has_base:
        return MapLocationGate(
            normalized,
            True,
            False,
            "critical",
            "Конфликт положения базы",
            "GPS и сохранённая точка расходятся; измерения заблокированы.",
        )
    if normalized == "jump_suspected" and has_base:
        return MapLocationGate(
            normalized,
            True,
            False,
            "critical",
            "GPS-скачок отклонён",
            "Резкое изменение не принято; база сохранена, измерения заблокированы.",
        )
    if normalized == "collecting":
        return MapLocationGate(
            normalized,
            False,
            False,
            "warning",
            "Собираются GPS-данные",
            "Инструменты включатся после появления устойчивой базовой точки.",
        )
    return MapLocationGate(
        normalized,
        has_base,
        False,
        "warning",
        "База не готова",
        "Задайте ручную базу или завершите GPS-проверку.",
    )


class OfflineMapCanvas(QWidget):
    """Pan/zoom map plus base-relative, non-RF navigation overlays."""

    measurement_changed = Signal(object)
    manual_bearing_changed = Signal(float)
    interaction_blocked = Signal(str)

    def __init__(
        self,
        tile_provider: Callable[[int, int, int], bytes | None] | None = None,
        generation_provider: Callable[[], int] | None = None,
        viewport_provider: Callable[[tuple[tuple[int, int, int], ...]], None]
        | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(520, 320)
        self._tile_provider = tile_provider
        self._generation_provider = generation_provider
        self._viewport_provider = viewport_provider
        self._center = GeoPoint(0.0, 0.0)
        self._base: GeoPoint | None = None
        self._location_gate = map_location_gate("unset", has_base=False)
        self._mode = MapInteractionMode.OVERVIEW
        self._measurement_point: GeoPoint | None = None
        self._measurement: CartographicMeasurement | None = None
        self._manual_bearing_deg = 0.0
        self._manual_assumed_range_km: float | None = None
        self._manual_bearing_active = False
        self._zoom = 2
        self._minimum_zoom = 0
        self._maximum_zoom = 18
        self._map_ready = False
        self._can_request_tiles = False
        self._attribution = ""
        self._image_cache: OrderedDict[tuple[int, int, int], QImage] = OrderedDict()
        self._cache_identity = ""
        self._tile_generation = -1
        self._last_viewport: tuple[tuple[int, int, int], ...] = ()
        self._drag_origin: QPoint | None = None
        self._drag_center_world: tuple[float, float] | None = None
        self._has_dragged = False
        self._tile_timer = QTimer(self)
        self._tile_timer.setInterval(250)
        self._tile_timer.timeout.connect(self._poll_tile_generation)
        self._tile_timer.start()

    @property
    def interaction_mode(self) -> MapInteractionMode:
        return self._mode

    @property
    def location_gate(self) -> MapLocationGate:
        return self._location_gate

    @property
    def measurement(self) -> CartographicMeasurement | None:
        return self._measurement

    @property
    def manual_bearing_deg(self) -> float:
        return self._manual_bearing_deg

    @property
    def manual_assumed_range_km(self) -> float | None:
        return self._manual_assumed_range_km

    @property
    def manual_bearing_active(self) -> bool:
        return self._manual_bearing_active

    def set_state(
        self,
        *,
        map_status: object | None,
        location: object | None,
        default_zoom: int,
    ) -> None:
        was_ready = self._map_ready
        self._map_ready = bool(attr(map_status, "available", False))
        self._can_request_tiles = self._map_ready or bool(
            attr(map_status, "network_enabled", False)
        )
        minimum = attr(map_status, "minimum_zoom", 0)
        maximum = attr(map_status, "maximum_zoom", 18)
        self._minimum_zoom = int(minimum if minimum is not None else 0)
        self._maximum_zoom = int(maximum if maximum is not None else 18)
        self._attribution = str(attr(map_status, "attribution", ""))
        cache_identity = "|".join(
            (
                str(attr(map_status, "source", "")),
                str(attr(map_status, "package_path", "")),
                str(bool(attr(map_status, "network_enabled", False))),
            )
        )
        if cache_identity != self._cache_identity:
            self._cache_identity = cache_identity
            self._image_cache.clear()
            self._last_viewport = ()

        previous_base = self._base
        base = attr(location, "base")
        location_status = value_of(attr(location, "status", "unset"))
        self._base = base if isinstance(base, GeoPoint) else None
        self._location_gate = map_location_gate(
            location_status,
            has_base=self._base is not None,
            base_in_coverage=attr(map_status, "base_in_coverage"),
        )
        if self._base is not None and previous_base is None:
            self._center = self._base
            self._zoom = max(
                self._minimum_zoom,
                min(self._maximum_zoom, default_zoom),
            )
        if previous_base != self._base:
            self.clear_operator_overlay()
        if not self._location_gate.tools_allowed:
            self._mode = MapInteractionMode.OVERVIEW
            self.clear_operator_overlay()

        center = attr(map_status, "center")
        if (
            self._base is None
            and self._map_ready
            and not was_ready
            and isinstance(center, (tuple, list))
            and len(center) == 3
        ):
            longitude, latitude, zoom = center
            self._center = GeoPoint(float(latitude), float(longitude))
            self._zoom = int(zoom)
        self._zoom = max(
            self._minimum_zoom,
            min(self._maximum_zoom, self._zoom),
        )
        self.update()

    def set_interaction_mode(self, mode: MapInteractionMode | str) -> bool:
        selected = MapInteractionMode(mode)
        if (
            selected is not MapInteractionMode.OVERVIEW
            and not self._location_gate.tools_allowed
        ):
            self.interaction_blocked.emit(self._location_gate.message_ru)
            return False
        self._mode = selected
        self.update()
        return True

    def set_manual_bearing(
        self,
        bearing_deg: float,
        *,
        assumed_range_km: float | None,
    ) -> None:
        bearing = float(bearing_deg)
        if not math.isfinite(bearing):
            raise ValueError("bearing_deg must be finite")
        if assumed_range_km is not None:
            assumed_range_km = float(assumed_range_km)
            if not math.isfinite(assumed_range_km) or assumed_range_km <= 0.0:
                raise ValueError("assumed_range_km must be finite and positive")
        self._manual_bearing_deg = bearing % 360.0
        self._manual_assumed_range_km = assumed_range_km
        self._manual_bearing_active = True
        self.update()

    def measure_point(
        self,
        point: GeoPoint,
    ) -> CartographicMeasurement | None:
        if not self._location_gate.tools_allowed or self._base is None:
            self.interaction_blocked.emit(self._location_gate.message_ru)
            return None
        distance = haversine_distance_m(self._base, point)
        try:
            bearing = (
                initial_bearing_deg(self._base, point)
                if distance > 1e-6
                else None
            )
        except ValueError:
            bearing = None
        result = CartographicMeasurement(distance, bearing)
        self._measurement_point = point
        self._measurement = result
        self.measurement_changed.emit(result)
        self.update()
        return result

    def clear_operator_overlay(self) -> None:
        self._measurement_point = None
        self._measurement = None
        self._manual_bearing_deg = 0.0
        self._manual_assumed_range_km = None
        self._manual_bearing_active = False
        self.update()

    def center_on_base(self) -> None:
        if self._base is not None:
            self._center = self._base
            self.update()

    def refresh_visible(self) -> None:
        """Re-evaluate only tiles in the current viewport."""

        self._image_cache.clear()
        self.update()

    def point_to_viewport(self, point: GeoPoint) -> QPointF:
        """Project a point with wrapped X so the antimeridian stays continuous."""

        point_x, point_y = latlon_to_world(point, self._zoom)
        center_x, center_y = latlon_to_world(self._center, self._zoom)
        world_width = float(_TILE_SIZE * (1 << self._zoom))
        x = self.width() / 2 + wrapped_world_x_delta(
            point_x,
            center_x,
            world_width,
        )
        y = self.height() / 2 + (point_y - center_y)
        return QPointF(x, y)

    def viewport_to_point(self, position: QPointF) -> GeoPoint:
        center_x, center_y = latlon_to_world(self._center, self._zoom)
        world_width = float(_TILE_SIZE * (1 << self._zoom))
        world_x = (center_x + position.x() - self.width() / 2) % world_width
        world_y = max(
            0.0,
            min(
                world_width,
                center_y + position.y() - self.height() / 2,
            ),
        )
        return world_to_latlon(world_x, world_y, self._zoom)

    def _poll_tile_generation(self) -> None:
        if self._generation_provider is None or not self.isVisible():
            return
        try:
            generation = int(self._generation_provider())
        except Exception:
            return
        if generation == self._tile_generation:
            return
        self._tile_generation = generation
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(Colors.BG))
        drawn_tiles = self._draw_tiles(painter) if self._can_request_tiles else 0
        if drawn_tiles == 0:
            self._draw_fallback_grid(painter)
        self._draw_navigation_overlay(painter)
        self._draw_base(painter)
        self._draw_scale(painter)
        painter.setPen(QColor(Colors.MUTED))
        attribution = self._attribution or "ПАКЕТ КАРТЫ НЕ ЗАГРУЖЕН"
        painter.drawText(12, self.height() - 12, attribution[:160])

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = 1 if event.angleDelta().y() > 0 else -1
        self._zoom = max(
            self._minimum_zoom,
            min(self._maximum_zoom, self._zoom + delta),
        )
        self.update()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self._drag_center_world = latlon_to_world(self._center, self._zoom)
            self._has_dragged = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin is None or self._drag_center_world is None:
            return
        delta = event.position().toPoint() - self._drag_origin
        if delta.manhattanLength() < 4:
            return
        self._has_dragged = True
        scale = _TILE_SIZE * (1 << self._zoom)
        world_x = (self._drag_center_world[0] - delta.x()) % scale
        world_y = max(
            0.0,
            min(float(scale), self._drag_center_world[1] - delta.y()),
        )
        self._center = world_to_latlon(world_x, world_y, self._zoom)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        should_handle_click = (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_origin is not None
            and not self._has_dragged
        )
        self._drag_origin = None
        self._drag_center_world = None
        self._has_dragged = False
        self.unsetCursor()
        if should_handle_click:
            self._handle_map_click(self.viewport_to_point(event.position()))
        event.accept()

    def _handle_map_click(self, point: GeoPoint) -> None:
        if self._mode is MapInteractionMode.MEASURE:
            self.measure_point(point)
            return
        if self._mode is not MapInteractionMode.MANUAL_BEARING:
            return
        if not self._location_gate.tools_allowed or self._base is None:
            self.interaction_blocked.emit(self._location_gate.message_ru)
            return
        if haversine_distance_m(self._base, point) <= 1e-6:
            self.interaction_blocked.emit(
                "Для ручного азимута выберите точку в стороне от базы."
            )
            return
        self._manual_bearing_deg = initial_bearing_deg(self._base, point)
        self._manual_bearing_active = True
        self.manual_bearing_changed.emit(self._manual_bearing_deg)
        self.update()

    def _draw_tiles(self, painter: QPainter) -> int:
        if self._tile_provider is None:
            return 0
        center_pixel_x, center_pixel_y = latlon_to_world(self._center, self._zoom)
        tile_count = 1 << self._zoom
        left = center_pixel_x - self.width() / 2
        top = center_pixel_y - self.height() / 2
        first_x = math.floor(left / _TILE_SIZE)
        last_x = math.floor((left + self.width()) / _TILE_SIZE)
        first_y = math.floor(top / _TILE_SIZE)
        last_y = math.floor((top + self.height()) / _TILE_SIZE)
        drawn = 0
        visible_keys = tuple(
            sorted(
                {
                    (self._zoom, raw_x % tile_count, tile_y)
                    for tile_y in range(first_y, last_y + 1)
                    if 0 <= tile_y < tile_count
                    for raw_x in range(first_x, last_x + 1)
                }
            )
        )
        if (
            self._viewport_provider is not None
            and visible_keys != self._last_viewport
        ):
            try:
                self._viewport_provider(visible_keys)
                self._last_viewport = visible_keys
            except Exception:
                pass
        for tile_y in range(first_y, last_y + 1):
            if not 0 <= tile_y < tile_count:
                continue
            for raw_x in range(first_x, last_x + 1):
                tile_x = raw_x % tile_count
                key = (self._zoom, tile_x, tile_y)
                image = self._image_cache.get(key)
                if image is None:
                    payload = self._tile_provider(self._zoom, tile_x, tile_y)
                    if payload is None:
                        continue
                    candidate = QImage.fromData(payload)
                    if candidate.isNull():
                        continue
                    image = candidate
                    if len(self._image_cache) >= 256:
                        self._image_cache.popitem(last=False)
                    self._image_cache[key] = image
                else:
                    self._image_cache.move_to_end(key)
                target = QRectF(
                    raw_x * _TILE_SIZE - left,
                    tile_y * _TILE_SIZE - top,
                    _TILE_SIZE,
                    _TILE_SIZE,
                )
                painter.drawImage(target, image)
                drawn += 1
        return drawn

    def _draw_fallback_grid(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor(Colors.BORDER), 1))
        for x in range(0, self.width(), 56):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 56):
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(QColor(Colors.MUTED))
        painter.drawText(12, 22, _FALLBACK_GRID_LABEL)

    def _draw_navigation_overlay(self, painter: QPainter) -> None:
        if (
            self._base is None
            or not self._location_gate.tools_allowed
            or self.width() <= 0
            or self.height() <= 0
        ):
            return
        ring_color = (
            Colors.READY
            if self._location_gate.status == "verified"
            else Colors.WARNING
        )
        requested_radius_m = max(
            1.0,
            min(self.width(), self.height())
            * 0.43
            * self._meters_per_pixel(),
        )
        ring_radii = _navigation_ring_distances(requested_radius_m)
        outer_radius_m = ring_radii[-1]
        painter.setPen(QPen(QColor(ring_color), 1, Qt.PenStyle.DashLine))
        for radius_m in ring_radii:
            self._draw_geodesic_path(
                painter,
                geodesic_ring(self._base, radius_m),
            )
            label_point = self.point_to_viewport(
                destination_point(self._base, 90.0, radius_m)
            )
            if self._point_near_viewport(label_point):
                painter.drawText(
                    QPointF(label_point.x() + 4.0, label_point.y() - 4.0),
                    _format_ring_km(radius_m),
                )

        painter.setPen(QPen(QColor(ring_color), 1))
        base_screen = self.point_to_viewport(self._base)
        for bearing in range(0, 360, 45):
            endpoint = destination_point(self._base, float(bearing), outer_radius_m)
            endpoint_screen = self.point_to_viewport(endpoint)
            self._draw_clipped_line(painter, base_screen, endpoint_screen)
            cardinal = {
                0: "N · 0°",
                90: "E · 90°",
                180: "S · 180°",
                270: "W · 270°",
            }.get(bearing, f"{bearing}°")
            if self._point_near_viewport(endpoint_screen):
                painter.drawText(
                    QPointF(endpoint_screen.x() + 4.0, endpoint_screen.y() - 4.0),
                    cardinal,
                )

        if (
            self._mode is MapInteractionMode.MEASURE
            and self._measurement_point is not None
            and self._measurement is not None
        ):
            target = self.point_to_viewport(self._measurement_point)
            painter.setPen(QPen(QColor(Colors.TEAL), 2))
            self._draw_clipped_line(painter, base_screen, target)
            if self._point_near_viewport(target):
                painter.setBrush(QColor(Colors.TEAL))
                painter.drawEllipse(target, 5.0, 5.0)
                painter.drawText(
                    QPointF(target.x() + 8.0, target.y() - 8.0),
                    (
                        f"КАРТА · {_format_distance(self._measurement.distance_m)} · "
                        f"{_format_bearing(self._measurement.bearing_deg)}"
                    ),
                )
                painter.drawText(
                    QPointF(target.x() + 8.0, target.y() + 10.0),
                    "НЕ RF-ПОЗИЦИЯ",
                )

        if (
            self._mode is MapInteractionMode.MANUAL_BEARING
            and self._manual_bearing_active
        ):
            range_m = (
                self._manual_assumed_range_km * 1000.0
                if self._manual_assumed_range_km is not None
                else outer_radius_m
            )
            endpoint = destination_point(
                self._base,
                self._manual_bearing_deg,
                range_m,
            )
            target = self.point_to_viewport(endpoint)
            painter.setPen(QPen(QColor(Colors.WARNING), 3))
            self._draw_clipped_line(painter, base_screen, target)
            range_label = (
                f"{self._manual_assumed_range_km:g} км · РУЧНОЙ ВВОД"
                if self._manual_assumed_range_km is not None
                else "ДАЛЬНОСТЬ НЕИЗВЕСТНА"
            )
            if self._point_near_viewport(target):
                painter.setBrush(QColor(Colors.WARNING))
                painter.drawEllipse(target, 5.0, 5.0)
                painter.drawText(
                    QPointF(target.x() + 8.0, target.y() - 8.0),
                    f"{self._manual_bearing_deg:05.1f}° ист. · {range_label}",
                )
                painter.drawText(
                    QPointF(target.x() + 8.0, target.y() + 10.0),
                    "АЗИМУТ ОПЕРАТОРА · НЕ RF-ЛОКАЛИЗАЦИЯ",
                )

    def _point_near_viewport(self, point: QPointF, *, margin: float = 64.0) -> bool:
        return (
            math.isfinite(point.x())
            and math.isfinite(point.y())
            and -margin <= point.x() <= self.width() + margin
            and -margin <= point.y() <= self.height() + margin
        )

    def _draw_clipped_line(
        self,
        painter: QPainter,
        start: QPointF,
        end: QPointF,
    ) -> None:
        viewport = QRectF(self.rect()).adjusted(-1.0, -1.0, 1.0, 1.0)
        clipped = _clip_line_to_rect(start, end, viewport)
        if clipped is not None:
            painter.drawLine(*clipped)

    def _draw_geodesic_path(
        self,
        painter: QPainter,
        points: tuple[GeoPoint, ...],
    ) -> None:
        path = QPainterPath()
        previous: QPointF | None = None
        world_width = float(_TILE_SIZE * (1 << self._zoom))
        for point in points:
            screen = self.point_to_viewport(point)
            if previous is None or abs(screen.x() - previous.x()) > world_width / 2.0:
                path.moveTo(screen)
            else:
                path.lineTo(screen)
            previous = screen
        painter.drawPath(path)

    def _draw_base(self, painter: QPainter) -> None:
        if self._base is None:
            return
        point = self.point_to_viewport(self._base)
        x = point.x()
        y = point.y()
        if not (-20 <= x <= self.width() + 20 and -20 <= y <= self.height() + 20):
            return
        color, dark = {
            "verified": (Colors.READY, Colors.READY_DARK),
            "conflict": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "stale": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "jump_suspected": (Colors.CRITICAL, Colors.CRITICAL_DARK),
        }.get(self._location_gate.status, (Colors.WARNING, Colors.WARNING_DARK))
        painter.setPen(QPen(QColor(color), 2))
        painter.setBrush(QColor(dark))
        painter.drawEllipse(point, 8.0, 8.0)
        painter.drawLine(int(x - 14), int(y), int(x + 14), int(y))
        painter.drawLine(int(x), int(y - 14), int(x), int(y + 14))
        painter.setPen(QColor(Colors.TEXT))
        state = {
            "verified": "ПОДТВЕРЖДЕНА",
            "manual_unverified": "РУЧНАЯ · НЕ ПРОВЕРЕНА",
            "collecting": "GPS-ПРОВЕРКА ИДЁТ",
            "conflict": "КОНФЛИКТ ПОЛОЖЕНИЯ",
            "stale": "GPS УСТАРЕЛ",
            "jump_suspected": "GPS-СКАЧОК ОТКЛОНЁН",
        }.get(self._location_gate.status, "НЕ ГОТОВА")
        painter.drawText(
            int(x + 12),
            int(y - 10),
            f"БАЗА · {state} · КООРДИНАТЫ СКРЫТЫ",
        )

    def _meters_per_pixel(self) -> float:
        projected_latitude = max(
            -WEB_MERCATOR_MAX_LATITUDE,
            min(WEB_MERCATOR_MAX_LATITUDE, self._center.latitude_deg),
        )
        return (
            math.cos(math.radians(projected_latitude))
            * 2.0
            * math.pi
            * _WEB_MERCATOR_RADIUS_M
            / (_TILE_SIZE * (1 << self._zoom))
        )

    def _draw_scale(self, painter: QPainter) -> None:
        meters_per_pixel = self._meters_per_pixel()
        target_pixels = 120
        raw_meters = meters_per_pixel * target_pixels
        magnitude = 10 ** math.floor(math.log10(raw_meters))
        normalized = raw_meters / magnitude
        nice = 1 if normalized < 2 else 2 if normalized < 5 else 5
        meters = nice * magnitude
        pixels = int(meters / meters_per_pixel)
        x = self.width() - pixels - 18
        y = self.height() - 30
        painter.setPen(QPen(QColor(Colors.TEXT_SECONDARY), 2))
        painter.drawLine(x, y, x + pixels, y)
        painter.drawLine(x, y - 4, x, y + 4)
        painter.drawLine(x + pixels, y - 4, x + pixels, y + 4)
        painter.drawText(x, y - 7, _format_distance(meters))


class MapPage(OperatorPage):
    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "Карта и навигация",
            "Километровые кольца, истинные азимуты и ручные измерения от базы",
            action_text="Загрузить / обновить карту",
        )
        self.header.action.clicked.connect(self.refresh_map)
        self._default_zoom = int(
            attr(attr(attr(runtime, "config"), "map"), "default_zoom", 12)
        )
        self.header.subtitle.setWordWrap(True)
        self.header.subtitle.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.offline_notice = InlineNotice(
            "Автоматическая карта с локальным кэшем",
            "Запрашиваются только тайлы текущего окна по HTTPS. Точные координаты "
            "не выводятся в интерфейс, журналы или support bundle.",
            level="info",
        )
        self.root_layout.addWidget(self.offline_notice)
        content = QHBoxLayout()
        map_panel = Panel("Рабочая область")
        map_panel.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        map_help = QLabel(
            "Колесо — масштаб · перетаскивание — обзор · "
            "клик — выбранный инструмент"
        )
        map_help.setWordWrap(True)
        map_help.setProperty("muted", "true")
        map_panel.content_layout.addWidget(map_help)
        provider = getattr(runtime, "map_tile", None)
        generation_provider = getattr(runtime, "map_tile_generation", None)
        viewport_provider = getattr(runtime, "map_visible_tiles", None)
        self.canvas = OfflineMapCanvas(
            provider if callable(provider) else None,
            generation_provider if callable(generation_provider) else None,
            viewport_provider if callable(viewport_provider) else None,
        )
        self.canvas.measurement_changed.connect(self._show_measurement)
        self.canvas.manual_bearing_changed.connect(self._bearing_selected_on_map)
        self.canvas.interaction_blocked.connect(self._show_blocked_reason)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Режим карты"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Обзор", MapInteractionMode.OVERVIEW.value)
        self.mode_combo.addItem(
            "Измерить по карте",
            MapInteractionMode.MEASURE.value,
        )
        self.mode_combo.addItem(
            "Ручной азимут",
            MapInteractionMode.MANUAL_BEARING.value,
        )
        self.mode_combo.currentIndexChanged.connect(self._select_mode)
        toolbar.addWidget(self.mode_combo, 1)
        center_button = QPushButton("На базу")
        center_button.clicked.connect(self.canvas.center_on_base)
        toolbar.addWidget(center_button)
        clear_button = QPushButton("Очистить отметку")
        clear_button.clicked.connect(self._clear_overlay)
        toolbar.addWidget(clear_button)
        map_panel.content_layout.addLayout(toolbar)
        map_panel.content_layout.addWidget(self.canvas)
        content.addWidget(map_panel, 2)

        side = Panel(
            "Навигация",
        )
        side.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Minimum,
        )
        side_scope = QLabel("Все расчёты локальны и относятся только к карте")
        side_scope.setWordWrap(True)
        side_scope.setProperty("muted", "true")
        side.content_layout.addWidget(side_scope)
        metric_row = QHBoxLayout()
        self.map_metric = MetricTile("Карта", "НЕ ЗАГРУЖЕНА")
        self.location_metric = MetricTile("База", "НЕ ЗАДАНА")
        self.map_metric.value_label.setWordWrap(True)
        self.location_metric.value_label.setWordWrap(True)
        metric_row.addWidget(self.map_metric)
        metric_row.addWidget(self.location_metric)
        side.content_layout.addLayout(metric_row)
        self.location_notice = InlineNotice(
            "База не готова",
            "Задайте ручную базу или завершите GPS-проверку.",
            level="warning",
        )
        side.content_layout.addWidget(self.location_notice)

        manual_form = QFormLayout()
        self.bearing_input = QDoubleSpinBox()
        self.bearing_input.setRange(0.0, 359.9)
        self.bearing_input.setDecimals(1)
        self.bearing_input.setSingleStep(1.0)
        self.bearing_input.setSuffix("° ист.")
        self.bearing_input.setToolTip(
            "Истинный азимут, введённый оператором; это не измерение RTL-SDR."
        )
        self.range_input = QDoubleSpinBox()
        self.range_input.setRange(0.0, 1000.0)
        self.range_input.setDecimals(1)
        self.range_input.setSingleStep(1.0)
        self.range_input.setSpecialValueText("не задана")
        self.range_input.setSuffix(" км")
        self.range_input.setToolTip(
            "Необязательная предполагаемая дальность оператора. "
            "Система её не измеряет."
        )
        manual_form.addRow("Ручной истинный азимут", self.bearing_input)
        manual_form.addRow("Дальность (ручной ввод)", self.range_input)
        side.content_layout.addLayout(manual_form)
        self.bearing_input.valueChanged.connect(self._manual_inputs_changed)
        self.range_input.valueChanged.connect(self._manual_inputs_changed)

        self.result_notice = InlineNotice(
            "Что показано",
            "Кольца — расстояние по поверхности от базы. Лучи — истинные "
            "азимуты. Это не обнаружение источника сигнала.",
            level="info",
        )
        side.content_layout.addWidget(self.result_notice)
        self.measurement_detail = QLabel(
            "Выберите «Измерить по карте» и укажите точку."
        )
        self.measurement_detail.setWordWrap(True)
        self.measurement_detail.setProperty("secondary", "true")
        side.content_layout.addWidget(self.measurement_detail)

        self.import_button = QPushButton("Импортировать MBTiles…")
        self.import_button.clicked.connect(self.import_map)
        experience = str(
            attr(attr(attr(runtime, "config"), "ui"), "experience_level", "guided")
        )
        self.import_button.setVisible(experience == "expert")
        self.import_button.setToolTip(
            "Экспертный локальный пакет имеет приоритет над сетевой картой."
        )
        side.content_layout.addWidget(self.import_button)
        side.content_layout.addWidget(
            InlineNotice(
                "Ограничение одиночного приёмника",
                "Одиночный RTL-SDR или tinySA не измеряет направление и дальность. "
                "Ручной луч — только запись наблюдения оператора.",
                level="warning",
            )
        )
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setProperty("secondary", "true")
        side.content_layout.addWidget(self.detail)
        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.side_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.side_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.side_scroll.setWidget(side)
        content.addWidget(self.side_scroll, 1)
        self.root_layout.addLayout(content, 1)
        self._sync_mode_controls()

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        map_status = attr(snapshot, "map_status")
        location = attr(snapshot, "location")
        self.canvas.set_state(
            map_status=map_status,
            location=location,
            default_zoom=self._default_zoom,
        )
        self._sync_mode_from_canvas()
        gate = self.canvas.location_gate
        self.location_notice.set_notice(
            gate.title_ru,
            gate.message_ru,
            level=gate.tone,
        )
        self.mode_combo.setEnabled(gate.tools_allowed)

        if bool(attr(map_status, "available", False)):
            self.map_metric.set_value(
                str(attr(map_status, "name", "Карта")),
                Colors.READY,
            )
        else:
            online_state = str(attr(map_status, "online_state", "disabled"))
            if online_state in {"configured", "loading"}:
                self.map_metric.set_value("ЗАГРУЗКА…", Colors.WARNING)
            elif online_state == "error":
                self.map_metric.set_value("ОШИБКА СЕТИ", Colors.CRITICAL)
            else:
                self.map_metric.set_value("НЕ ЗАГРУЖЕНА", Colors.WARNING)
        location_status = gate.status
        location_text = {
            "unset": "НЕ ЗАДАНА",
            "collecting": "СБОР GPS",
            "manual_unverified": "РУЧНАЯ · НЕ ПРОВЕРЕНА",
            "verified": "ПОДТВЕРЖДЕНА",
            "conflict": "КОНФЛИКТ",
            "stale": "GPS УСТАРЕЛ",
            "jump_suspected": "GPS-СКАЧОК",
        }.get(location_status, "НЕИЗВЕСТНО")
        location_color = (
            Colors.READY
            if location_status == "verified"
            else Colors.CRITICAL
            if location_status in {"conflict", "stale", "jump_suspected"}
            else Colors.WARNING
        )
        self.location_metric.set_value(location_text, location_color)
        map_message = str(attr(map_status, "message_ru", "Пакет карты не выбран."))
        location_message = str(
            attr(
                location,
                "message_ru",
                "База не задана; абсолютные наложения отключены.",
            )
        )
        coverage = attr(map_status, "base_in_coverage")
        coverage_message = (
            " База входит в покрытие карты."
            if coverage is True
            else " База вне покрытия карты."
            if coverage is False
            else " Пакет не объявляет bounds; покрытие базы неизвестно."
            if bool(attr(map_status, "available", False))
            else ""
        )
        cached_tiles = int(attr(map_status, "online_cached_tiles", 0))
        pending_tiles = int(attr(map_status, "online_pending_tiles", 0))
        online_error = attr(map_status, "online_last_error_code")
        network_state = (
            "Сеть для карты включена."
            if bool(attr(map_status, "network_enabled", False))
            else "Сеть для карты отключена; доступен только локальный кэш/MBTiles."
        )
        diagnostics = (
            f"{network_state} Кэш: {cached_tiles}; ожидают загрузки: {pending_tiles}."
        )
        if online_error:
            diagnostics += f" Диагностика: {online_error}."
        gps_state = str(attr(location, "gps_fix_state", "disconnected"))
        gps_state_text = {
            "disconnected": "GPS не подключён",
            "searching": "GPS ищет фиксацию",
            "no_fix": "GPS: фиксации нет",
            "fix": "GPS: фиксация есть, размерность не сообщена",
            "fix_2d": "GPS: 2D-фиксация",
            "fix_3d": "GPS: 3D-фиксация",
            "stale": "GPS: данные устарели",
            "jump_suspected": "GPS: подозрительный скачок отклонён",
        }.get(gps_state, "GPS: состояние неизвестно")
        self.detail.setText(
            f"{map_message}\n\n{diagnostics}\n\n"
            f"{gps_state_text}. {location_message}{coverage_message}"
        )
        if location_status == "verified" and coverage is not False:
            self.header.status.set_status("НАВИГАЦИЯ ГОТОВА", "ready")
        elif (
            location_status in {"conflict", "stale", "jump_suspected"}
            or coverage is False
        ):
            self.header.status.set_status("НАВИГАЦИЯ ЗАБЛОКИРОВАНА", "critical")
        elif gate.tools_allowed:
            self.header.status.set_status("СПРАВОЧНЫЙ РЕЖИМ", "warning")
        else:
            self.header.status.set_status("ТРЕБУЕТСЯ БАЗА", "warning")

    def refresh_map(self) -> None:
        ok, message = call_runtime(self.runtime, "retry_online_map")
        self.canvas.refresh_visible()
        if ok:
            self.header.status.set_status("ОБНОВЛЯЮТСЯ ВИДИМЫЕ ТАЙЛЫ", "info")
        else:
            self.header.status.set_status("СЕТЕВАЯ КАРТА НЕДОСТУПНА", "warning")
        self.detail.setText(str(message))

    def import_map(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Импорт локальной карты",
            "",
            "MBTiles (*.mbtiles)",
        )
        if not selected:
            return
        ok, result = call_runtime(self.runtime, "import_map_package", selected)
        if ok:
            self.header.status.set_status("КАРТА ИМПОРТИРОВАНА", "ready")
            self.refresh(current_snapshot(self.runtime))
        else:
            self.header.status.set_status("ОШИБКА КАРТЫ", "critical")
            self.detail.setText(str(result))

    def _select_mode(self, index: int) -> None:
        del index
        selected = str(self.mode_combo.currentData())
        if not self.canvas.set_interaction_mode(selected):
            self._sync_mode_from_canvas()
        self._sync_mode_controls()

    def _sync_mode_from_canvas(self) -> None:
        target = self.mode_combo.findData(self.canvas.interaction_mode.value)
        if target >= 0 and target != self.mode_combo.currentIndex():
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(target)
            self.mode_combo.blockSignals(False)
        self._sync_mode_controls()

    def _sync_mode_controls(self) -> None:
        manual = (
            self.canvas.interaction_mode is MapInteractionMode.MANUAL_BEARING
        )
        enabled = manual and self.canvas.location_gate.tools_allowed
        self.bearing_input.setEnabled(enabled)
        self.range_input.setEnabled(enabled)
        if manual:
            if self.canvas.manual_bearing_active:
                self._update_manual_result()
            else:
                self._reset_manual_inputs()
                self.measurement_detail.setText(
                    "Введите истинный азимут или укажите направление кликом по карте."
                )
                self.result_notice.set_notice(
                    "Ручной азимут",
                    "Луч появится только после ввода оператора; приёмник не "
                    "измеряет направление и дальность.",
                    level="warning",
                )

    def _manual_inputs_changed(self, value: float) -> None:
        del value
        assumed_range = self.range_input.value()
        self.canvas.set_manual_bearing(
            self.bearing_input.value(),
            assumed_range_km=assumed_range if assumed_range > 0.0 else None,
        )
        self._update_manual_result()

    def _bearing_selected_on_map(self, bearing_deg: float) -> None:
        self.bearing_input.blockSignals(True)
        self.bearing_input.setValue(bearing_deg)
        self.bearing_input.blockSignals(False)
        self.canvas.set_manual_bearing(
            bearing_deg,
            assumed_range_km=(
                self.range_input.value()
                if self.range_input.value() > 0.0
                else None
            ),
        )
        self._update_manual_result()

    def _show_measurement(self, result: object) -> None:
        if isinstance(result, CartographicMeasurement):
            self.measurement_detail.setText(result.summary_ru)
            explanation = (
                "Расстояние рассчитано от базы; единственный истинный азимут "
                "для этой точки не определён. "
                if result.bearing_deg is None
                else "Расстояние и истинный азимут рассчитаны от базы. "
            )
            self.result_notice.set_notice(
                "Измерено по карте",
                explanation
                + "Это не координаты и не классификация RF-источника.",
                level="info",
            )

    def _show_blocked_reason(self, message: str) -> None:
        self.result_notice.set_notice(
            "Инструмент недоступен",
            str(message),
            level="warning",
        )

    def _update_manual_result(self) -> None:
        range_text = (
            f"{self.range_input.value():g} км — условная дальность оператора"
            if self.range_input.value() > 0.0
            else "дальность неизвестна"
        )
        self.measurement_detail.setText(
            f"{self.bearing_input.value():05.1f}° ист. · {range_text}\n"
            "Ручной ввод оператора — не RF-локализация."
        )
        self.result_notice.set_notice(
            "Ручной луч",
            "Направление и условная дальность введены человеком; приёмник "
            "их не измерял.",
            level="warning",
        )

    def _clear_overlay(self) -> None:
        self.canvas.clear_operator_overlay()
        self._reset_manual_inputs()
        self.measurement_detail.setText(
            "Отметка очищена. Выберите инструмент и укажите точку."
        )
        self.result_notice.set_notice(
            "Отметка очищена",
            "На карте нет измеренной или введённой оператором позиции.",
            level="info",
        )

    def _reset_manual_inputs(self) -> None:
        self.bearing_input.blockSignals(True)
        self.range_input.blockSignals(True)
        self.bearing_input.setValue(0.0)
        self.range_input.setValue(0.0)
        self.bearing_input.blockSignals(False)
        self.range_input.blockSignals(False)


def _nice_ring_step_m(target_m: float) -> float:
    target = max(1.0, float(target_m))
    magnitude = 10.0 ** math.floor(math.log10(target))
    normalized = target / magnitude
    if normalized <= 1.0:
        multiplier = 1.0
    elif normalized <= 2.0:
        multiplier = 2.0
    elif normalized <= 5.0:
        multiplier = 5.0
    else:
        multiplier = 10.0
    return multiplier * magnitude


def _navigation_ring_distances(maximum_radius_m: float) -> tuple[float, ...]:
    """Choose readable radii that remain valid shortest surface distances."""

    bounded_radius = min(
        max(1.0, float(maximum_radius_m)),
        _MAX_UNAMBIGUOUS_RING_RADIUS_M,
    )
    step_m = _nice_ring_step_m(bounded_radius / 4.0)
    ring_count = max(1, min(6, int(bounded_radius // step_m)))
    return tuple(step_m * index for index in range(1, ring_count + 1))


def _clip_line_to_rect(
    start: QPointF,
    end: QPointF,
    rect: QRectF,
) -> tuple[QPointF, QPointF] | None:
    """Clip a finite line segment before handing it to the Qt rasterizer."""

    x0 = float(start.x())
    y0 = float(start.y())
    x1 = float(end.x())
    y1 = float(end.y())
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return None
    delta_x = x1 - x0
    delta_y = y1 - y0
    minimum = 0.0
    maximum = 1.0
    for direction, offset in (
        (-delta_x, x0 - rect.left()),
        (delta_x, rect.right() - x0),
        (-delta_y, y0 - rect.top()),
        (delta_y, rect.bottom() - y0),
    ):
        if direction == 0.0:
            if offset < 0.0:
                return None
            continue
        ratio = offset / direction
        if direction < 0.0:
            if ratio > maximum:
                return None
            minimum = max(minimum, ratio)
        else:
            if ratio < minimum:
                return None
            maximum = min(maximum, ratio)
    return (
        QPointF(x0 + minimum * delta_x, y0 + minimum * delta_y),
        QPointF(x0 + maximum * delta_x, y0 + maximum * delta_y),
    )


def _format_ring_km(distance_m: float) -> str:
    kilometers = distance_m / 1000.0
    if kilometers >= 10.0:
        return f"{kilometers:.0f} км"
    if kilometers >= 1.0:
        return f"{kilometers:g} км"
    return f"{kilometers:.2g} км"


def _format_distance(distance_m: float) -> str:
    if distance_m >= 1000.0:
        return f"{distance_m / 1000.0:.2f} км"
    if distance_m < 0.01:
        return f"{distance_m * 1000.0:.0f} мм"
    if distance_m < 1.0:
        return f"{distance_m * 100.0:.0f} см"
    return f"{distance_m:.0f} м"


def _format_bearing(bearing_deg: float | None) -> str:
    return (
        f"{bearing_deg % 360.0:05.1f}° ист."
        if bearing_deg is not None
        else "азимут не определён"
    )


__all__ = [
    "CartographicMeasurement",
    "MapInteractionMode",
    "MapLocationGate",
    "MapPage",
    "OfflineMapCanvas",
    "map_location_gate",
]
