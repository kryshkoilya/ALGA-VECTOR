from __future__ import annotations

import math
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel

from alga_vector.location import (
    GeoPoint,
    destination_point,
    geodesic_ring,
    haversine_distance_m,
    initial_bearing_deg,
    latlon_to_world,
    wrapped_world_x_delta,
)
from alga_vector.ui.pages.map import (
    _FALLBACK_GRID_LABEL,
    _MAX_UNAMBIGUOUS_RING_RADIUS_M,
    MapInteractionMode,
    MapPage,
    OfflineMapCanvas,
    _navigation_ring_distances,
    map_location_gate,
)
from alga_vector.ui.theme import Colors


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication(["map-navigation-test"])


def _map_status(**overrides: object) -> object:
    values: dict[str, object] = {
        "available": False,
        "network_enabled": False,
        "minimum_zoom": 0,
        "maximum_zoom": 18,
        "attribution": "",
        "source": "fallback",
        "package_path": None,
        "online_state": "disabled",
        "online_cached_tiles": 0,
        "online_pending_tiles": 0,
        "message_ru": "Тайлы недоступны.",
        "base_in_coverage": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _location(status: str, base: GeoPoint | None) -> object:
    return SimpleNamespace(
        status=status,
        base=base,
        message_ru=f"Состояние базы: {status}.",
    )


def test_bearing_is_undefined_for_vertical_only_displacement() -> None:
    lower = GeoPoint(55.0, 37.0, 100.0)
    upper = GeoPoint(55.0, 37.0, 300.0)

    assert haversine_distance_m(lower, upper) == 0.0
    with pytest.raises(ValueError, match="undefined"):
        initial_bearing_deg(lower, upper)


def test_bearing_is_undefined_for_antipodal_points() -> None:
    base = GeoPoint(0.0, 0.0)
    antipode = GeoPoint(0.0, 180.0)

    with pytest.raises(ValueError, match="antipodal"):
        initial_bearing_deg(base, antipode)


def test_geodesic_ring_vertices_have_requested_surface_radius() -> None:
    base = GeoPoint(55.75, 37.62)
    ring = geodesic_ring(base, 12_500.0, vertex_count=72)

    assert len(ring) == 73
    assert ring[0] == ring[-1]
    for vertex in ring[:-1]:
        assert haversine_distance_m(base, vertex) == pytest.approx(
            12_500.0,
            abs=1e-6,
        )
    north = destination_point(base, 0.0, 12_500.0)
    east = destination_point(base, 90.0, 12_500.0)
    assert initial_bearing_deg(base, north) == pytest.approx(0.0, abs=1e-9)
    assert initial_bearing_deg(base, east) == pytest.approx(90.0, abs=1e-9)


def test_world_x_delta_wraps_across_antimeridian() -> None:
    zoom = 10
    east_x, _ = latlon_to_world(GeoPoint(0.0, 179.99), zoom)
    west_x, _ = latlon_to_world(GeoPoint(0.0, -179.99), zoom)
    world_width = float(256 * (1 << zoom))

    raw_delta = west_x - east_x
    wrapped = wrapped_world_x_delta(west_x, east_x, world_width)

    assert abs(raw_delta) > world_width * 0.99
    assert wrapped == pytest.approx(world_width * 0.02 / 360.0)


@pytest.mark.parametrize(
    ("status", "has_base", "allowed", "tone"),
    (
        ("unset", False, False, "warning"),
        ("collecting", False, False, "warning"),
        ("collecting", True, True, "warning"),
        ("manual_unverified", True, True, "warning"),
        ("verified", True, True, "ready"),
        ("stale", True, False, "critical"),
        ("conflict", True, False, "critical"),
        ("jump_suspected", True, False, "critical"),
    ),
)
def test_location_states_gate_map_tools(
    status: str,
    has_base: bool,
    allowed: bool,
    tone: str,
) -> None:
    gate = map_location_gate(status, has_base=has_base)

    assert gate.tools_allowed is allowed
    assert gate.tone == tone
    if status != "verified":
        assert gate.tone != "ready"


def test_map_coverage_blocks_otherwise_usable_base() -> None:
    gate = map_location_gate(
        "verified",
        has_base=True,
        base_in_coverage=False,
    )

    assert not gate.tools_allowed
    assert gate.tone == "critical"
    assert "вне покрытия" in gate.title_ru.lower()


def test_planet_scale_rings_remain_unambiguous_shortest_distances() -> None:
    base = GeoPoint(0.0, 0.0)
    radii = _navigation_ring_distances(100_000_000.0)

    assert radii
    assert radii[-1] < _MAX_UNAMBIGUOUS_RING_RADIUS_M
    for radius_m in radii:
        point = destination_point(base, 90.0, radius_m)
        assert haversine_distance_m(base, point) == pytest.approx(
            radius_m,
            abs=0.01,
        )


def test_fallback_grid_does_not_claim_geodetic_meaning() -> None:
    assert "ГЕОДЕЗ" not in _FALLBACK_GRID_LABEL
    assert "ФОНОВАЯ СЕТКА" in _FALLBACK_GRID_LABEL  # noqa: RUF001


@pytest.mark.ui
def test_measurement_is_cartographic_and_contains_no_coordinates(
    qt_app: QApplication,
) -> None:
    del qt_app
    base = GeoPoint(55.75, 37.62)
    canvas = OfflineMapCanvas()
    canvas.resize(800, 600)
    canvas.set_state(
        map_status=_map_status(),
        location=_location("manual_unverified", base),
        default_zoom=12,
    )
    assert canvas.set_interaction_mode(MapInteractionMode.MEASURE)

    target = destination_point(base, 47.0, 7_500.0)
    result = canvas.measure_point(target)

    assert result is not None
    assert result.distance_m == pytest.approx(7_500.0, abs=1e-6)
    assert result.bearing_deg == pytest.approx(47.0, abs=1e-9)
    assert "не RF-позиция" in result.summary_ru
    assert "55.75" not in result.summary_ru
    assert "37.62" not in result.summary_ru
    assert "55.75" not in repr(result)
    assert "37.62" not in repr(result)
    canvas.close()


@pytest.mark.ui
def test_antipodal_measurement_keeps_distance_without_inventing_bearing(
    qt_app: QApplication,
) -> None:
    del qt_app
    canvas = OfflineMapCanvas()
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", GeoPoint(0.0, 0.0)),
        default_zoom=2,
    )
    assert canvas.set_interaction_mode(MapInteractionMode.MEASURE)

    result = canvas.measure_point(GeoPoint(0.0, 180.0))

    assert result is not None
    assert result.distance_m == pytest.approx(
        math.pi * 6_371_008.8,
        abs=0.01,
    )
    assert result.bearing_deg is None
    assert "азимут не определён" in result.summary_ru
    canvas.close()


@pytest.mark.ui
def test_manual_bearing_never_invents_distance(qt_app: QApplication) -> None:
    del qt_app
    canvas = OfflineMapCanvas()
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", GeoPoint(55.75, 37.62)),
        default_zoom=12,
    )
    assert canvas.set_interaction_mode(MapInteractionMode.MANUAL_BEARING)

    canvas.set_manual_bearing(412.5, assumed_range_km=None)
    assert canvas.manual_bearing_deg == pytest.approx(52.5)
    assert canvas.manual_assumed_range_km is None

    canvas.set_manual_bearing(52.5, assumed_range_km=14.0)
    assert canvas.manual_assumed_range_km == 14.0
    with pytest.raises(ValueError, match="positive"):
        canvas.set_manual_bearing(52.5, assumed_range_km=0.0)
    canvas.close()


@pytest.mark.ui
def test_clear_operator_overlay_removes_manual_ray_and_range(
    qt_app: QApplication,
) -> None:
    del qt_app
    canvas = OfflineMapCanvas()
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", GeoPoint(55.75, 37.62)),
        default_zoom=12,
    )
    assert canvas.set_interaction_mode(MapInteractionMode.MANUAL_BEARING)
    canvas.set_manual_bearing(52.5, assumed_range_km=14.0)
    assert canvas.manual_bearing_active

    canvas.clear_operator_overlay()

    assert not canvas.manual_bearing_active
    assert canvas.manual_bearing_deg == 0.0
    assert canvas.manual_assumed_range_km is None
    canvas.close()


@pytest.mark.ui
def test_canvas_projection_keeps_antimeridian_points_adjacent(
    qt_app: QApplication,
) -> None:
    del qt_app
    base = GeoPoint(0.0, 179.99)
    canvas = OfflineMapCanvas()
    canvas.resize(800, 600)
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", base),
        default_zoom=10,
    )

    point = canvas.point_to_viewport(GeoPoint(0.0, -179.99))

    assert math.isfinite(point.x())
    assert abs(point.x() - canvas.width() / 2) < 20.0
    canvas.close()


@pytest.mark.ui
def test_stale_or_conflicting_base_blocks_new_map_measurements(
    qt_app: QApplication,
) -> None:
    del qt_app
    base = GeoPoint(55.75, 37.62)
    canvas = OfflineMapCanvas()
    for status in ("stale", "conflict", "jump_suspected"):
        canvas.set_state(
            map_status=_map_status(),
            location=_location(status, base),
            default_zoom=12,
        )
        assert canvas.interaction_mode is MapInteractionMode.OVERVIEW
        assert not canvas.set_interaction_mode(MapInteractionMode.MEASURE)
        assert canvas.measure_point(destination_point(base, 90.0, 1000.0)) is None
    canvas.close()


@pytest.mark.ui
def test_map_page_never_styles_unverified_base_as_ready(
    qt_app: QApplication,
) -> None:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            map=SimpleNamespace(default_zoom=12),
            ui=SimpleNamespace(experience_level="guided"),
        )
    )
    page = MapPage(runtime)
    snapshot = SimpleNamespace(
        mode="live",
        map_status=_map_status(),
        location=_location(
            "manual_unverified",
            GeoPoint(55.75, 37.62),
        ),
    )
    page.refresh(snapshot)
    page.show()
    qt_app.processEvents()

    assert page.canvas.location_gate.tone == "warning"
    assert page.location_metric.value_label.styleSheet().find(Colors.WARNING) >= 0
    assert page.header.status.property("statusLevel") == "warning"
    all_text = "\n".join(label.text() for label in page.findChildren(QLabel))
    assert "55.75" not in all_text
    assert "37.62" not in all_text
    assert "не проверена" in all_text.lower()
    page.close()


@pytest.mark.ui
def test_base_outside_map_coverage_blocks_page_tools(
    qt_app: QApplication,
) -> None:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            map=SimpleNamespace(default_zoom=12),
            ui=SimpleNamespace(experience_level="guided"),
        )
    )
    page = MapPage(runtime)
    snapshot = SimpleNamespace(
        mode="live",
        map_status=_map_status(
            available=True,
            base_in_coverage=False,
            name="Локальная карта",
        ),
        location=_location("verified", GeoPoint(55.75, 37.62)),
    )

    page.refresh(snapshot)
    page.show()
    qt_app.processEvents()

    assert not page.canvas.location_gate.tools_allowed
    assert not page.mode_combo.isEnabled()
    assert page.header.status.property("statusLevel") == "critical"
    assert not page.canvas.set_interaction_mode(MapInteractionMode.MEASURE)
    page.close()


@pytest.mark.ui
def test_map_navigation_panel_is_scrollable_at_minimum_window_content_size(
    qt_app: QApplication,
) -> None:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            map=SimpleNamespace(default_zoom=12),
            ui=SimpleNamespace(experience_level="guided"),
        )
    )
    page = MapPage(runtime)
    # MainWindow reserves 112 px for navigation and 92 px for header/footer,
    # leaving this page viewport at 1008x628 in the supported 1120x720 window.
    page.resize(1008, 628)
    page.show()
    qt_app.processEvents()

    scroll_bar = page.side_scroll.verticalScrollBar()
    assert page.width() == 1008
    assert page.height() == 628
    assert page.side_scroll.viewport().width() >= 260
    assert page.side_scroll.horizontalScrollBar().maximum() == 0
    assert scroll_bar.maximum() > 0
    canvas_bottom = page.canvas.mapTo(
        page,
        QPoint(0, page.canvas.height()),
    ).y()
    assert canvas_bottom <= page.height()

    scroll_bar.setValue(scroll_bar.maximum())
    qt_app.processEvents()
    detail_bottom = page.detail.mapTo(
        page.side_scroll.viewport(),
        QPoint(0, page.detail.height()),
    ).y()
    assert detail_bottom <= page.side_scroll.viewport().height()
    page.close()


@pytest.mark.ui
def test_fallback_grid_and_geodesic_overlay_render_without_tiles(
    qt_app: QApplication,
) -> None:
    canvas = OfflineMapCanvas()
    canvas.resize(800, 600)
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", GeoPoint(55.75, 37.62)),
        default_zoom=12,
    )
    canvas.show()
    qt_app.processEvents()

    image = canvas.grab().toImage()

    assert not image.isNull()
    assert image.width() == 800
    assert image.height() == 600
    canvas.close()


@pytest.mark.ui
@pytest.mark.parametrize("latitude", (-90.0, 90.0))
def test_polar_base_renders_with_mercator_scale(
    qt_app: QApplication,
    latitude: float,
) -> None:
    canvas = OfflineMapCanvas()
    canvas.resize(800, 600)
    canvas.set_state(
        map_status=_map_status(),
        location=_location("verified", GeoPoint(latitude, 0.0)),
        default_zoom=12,
    )
    canvas.show()
    qt_app.processEvents()

    image = canvas.grab().toImage()

    assert math.isfinite(canvas._meters_per_pixel())
    assert canvas._meters_per_pixel() > 0.0
    assert not image.isNull()
    canvas.close()


@pytest.mark.ui
def test_high_zoom_manual_range_does_not_overflow_painter(
    qt_app: QApplication,
) -> None:
    canvas = OfflineMapCanvas()
    canvas.resize(800, 600)
    canvas.set_state(
        map_status=_map_status(maximum_zoom=30),
        location=_location("verified", GeoPoint(0.0, 0.0)),
        default_zoom=30,
    )
    assert canvas.set_interaction_mode(MapInteractionMode.MANUAL_BEARING)
    canvas.set_manual_bearing(90.0, assumed_range_km=1000.0)
    canvas.show()
    qt_app.processEvents()

    image = canvas.grab().toImage()

    assert not image.isNull()
    assert image.width() == 800
    assert image.height() == 600
    canvas.close()
