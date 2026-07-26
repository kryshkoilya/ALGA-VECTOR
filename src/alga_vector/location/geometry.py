from __future__ import annotations

import math

from .models import GeoPoint

WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
EARTH_MEAN_RADIUS_M = 6_371_008.8


def clamp_mercator_latitude(latitude_deg: float) -> float:
    if not math.isfinite(latitude_deg):
        raise ValueError("latitude must be finite")
    return max(-WEB_MERCATOR_MAX_LATITUDE, min(WEB_MERCATOR_MAX_LATITUDE, latitude_deg))


def latlon_to_world(
    point: GeoPoint,
    zoom: int,
    *,
    tile_size: int = 256,
) -> tuple[float, float]:
    """Convert WGS84 to XYZ world-pixel coordinates at a zoom level."""

    _validate_zoom_and_tile_size(zoom, tile_size)
    latitude = math.radians(clamp_mercator_latitude(point.latitude_deg))
    scale = float(tile_size * (1 << zoom))
    longitude = min(point.longitude_deg, math.nextafter(180.0, -math.inf))
    x = (longitude + 180.0) / 360.0 * scale
    y = (
        1.0
        - math.asinh(math.tan(latitude)) / math.pi
    ) / 2.0 * scale
    return x, y


def world_to_latlon(
    x: float,
    y: float,
    zoom: int,
    *,
    tile_size: int = 256,
) -> GeoPoint:
    _validate_zoom_and_tile_size(zoom, tile_size)
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("world coordinates must be finite")
    scale = float(tile_size * (1 << zoom))
    if not 0.0 <= x <= scale or not 0.0 <= y <= scale:
        raise ValueError("world coordinates are outside the selected zoom")
    longitude = x / scale * 360.0 - 180.0
    mercator_y = math.pi * (1.0 - 2.0 * y / scale)
    latitude = math.degrees(math.atan(math.sinh(mercator_y)))
    return GeoPoint(latitude, longitude)


def latlon_to_tile(point: GeoPoint, zoom: int) -> tuple[int, int]:
    x, y = latlon_to_world(point, zoom)
    tile_count = 1 << zoom
    tile_x = min(tile_count - 1, max(0, int(x // 256)))
    tile_y = min(tile_count - 1, max(0, int(y // 256)))
    return tile_x, tile_y


def xyz_to_tms_y(zoom: int, xyz_y: int) -> int:
    _validate_tile_index(zoom, xyz_y)
    return (1 << zoom) - 1 - xyz_y


def tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return west, south, east, north bounds for one XYZ tile."""

    _validate_tile_index(zoom, x)
    _validate_tile_index(zoom, y)
    north_west = world_to_latlon(float(x * 256), float(y * 256), zoom)
    south_east = world_to_latlon(float((x + 1) * 256), float((y + 1) * 256), zoom)
    return (
        north_west.longitude_deg,
        south_east.latitude_deg,
        south_east.longitude_deg,
        north_west.latitude_deg,
    )


def haversine_distance_m(first: GeoPoint, second: GeoPoint) -> float:
    lat1 = math.radians(first.latitude_deg)
    lat2 = math.radians(second.latitude_deg)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(
        ((second.longitude_deg - first.longitude_deg + 180.0) % 360.0) - 180.0
    )
    a = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_MEAN_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(first: GeoPoint, second: GeoPoint) -> float:
    # Altitude is deliberately ignored: a vertical-only displacement has no
    # cartographic bearing. Comparing the dataclasses directly used to return
    # a false north bearing when latitude/longitude matched but altitude did not.
    delta_lon_deg = (
        (second.longitude_deg - first.longitude_deg + 180.0) % 360.0
    ) - 180.0
    if (
        (
            first.latitude_deg == second.latitude_deg
            and delta_lon_deg == 0.0
        )
        or (
            abs(first.latitude_deg) == 90.0
            and first.latitude_deg == second.latitude_deg
        )
    ):
        raise ValueError("bearing is undefined for identical points")
    lat1 = math.radians(first.latitude_deg)
    lat2 = math.radians(second.latitude_deg)
    delta_lon = math.radians(delta_lon_deg)
    central_cosine = (
        math.sin(lat1) * math.sin(lat2)
        + math.cos(lat1) * math.cos(lat2) * math.cos(delta_lon)
    )
    if math.isclose(central_cosine, -1.0, rel_tol=0.0, abs_tol=1e-15):
        raise ValueError("bearing is undefined for antipodal points")
    y = math.sin(delta_lon) * math.cos(lat2)
    x = (
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(
    origin: GeoPoint,
    bearing_deg: float,
    distance_m: float,
) -> GeoPoint:
    """Return the spherical-geodesic destination from ``origin``.

    The map overlay uses this function instead of drawing screen-space circles:
    every ring vertex and spoke endpoint is derived from a true bearing and
    surface distance. Altitude is intentionally not propagated because the
    result describes a cartographic point on the WGS84 surface.
    """

    bearing = _finite_number("bearing_deg", bearing_deg)
    distance = _finite_number("distance_m", distance_m)
    if distance < 0.0:
        raise ValueError("distance_m cannot be negative")
    if distance == 0.0:
        return GeoPoint(origin.latitude_deg, origin.longitude_deg)

    angular_distance = distance / EARTH_MEAN_RADIUS_M
    latitude = math.radians(origin.latitude_deg)
    longitude = math.radians(origin.longitude_deg)
    bearing_rad = math.radians(bearing % 360.0)
    sin_latitude = math.sin(latitude)
    cos_latitude = math.cos(latitude)
    sin_distance = math.sin(angular_distance)
    cos_distance = math.cos(angular_distance)

    destination_latitude = math.asin(
        max(
            -1.0,
            min(
                1.0,
                sin_latitude * cos_distance
                + cos_latitude * sin_distance * math.cos(bearing_rad),
            ),
        )
    )
    destination_longitude = longitude + math.atan2(
        math.sin(bearing_rad) * sin_distance * cos_latitude,
        cos_distance - sin_latitude * math.sin(destination_latitude),
    )
    normalized_longitude = (
        math.degrees(destination_longitude) + 180.0
    ) % 360.0 - 180.0
    return GeoPoint(math.degrees(destination_latitude), normalized_longitude)


def geodesic_ring(
    center: GeoPoint,
    radius_m: float,
    *,
    vertex_count: int = 96,
) -> tuple[GeoPoint, ...]:
    """Return a closed base-centred ring made of geodesic vertices."""

    radius = _finite_number("radius_m", radius_m)
    if radius <= 0.0:
        raise ValueError("radius_m must be positive")
    if (
        isinstance(vertex_count, bool)
        or not isinstance(vertex_count, int)
        or vertex_count < 12
    ):
        raise ValueError("vertex_count must be an integer of at least 12")
    return tuple(
        destination_point(center, index * 360.0 / vertex_count, radius)
        for index in range(vertex_count + 1)
    )


def wrapped_world_x_delta(
    point_x: float,
    center_x: float,
    world_width: float,
) -> float:
    """Return the shortest signed world-X displacement across the antimeridian."""

    point = _finite_number("point_x", point_x)
    center = _finite_number("center_x", center_x)
    width = _finite_number("world_width", world_width)
    if width <= 0.0:
        raise ValueError("world_width must be positive")
    return ((point - center + width / 2.0) % width) - width / 2.0


def _finite_number(name: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f"{name} must be a real number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _validate_zoom_and_tile_size(zoom: int, tile_size: int) -> None:
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 0 <= zoom <= 30:
        raise ValueError("zoom must be an integer between 0 and 30")
    if isinstance(tile_size, bool) or not isinstance(tile_size, int) or tile_size <= 0:
        raise ValueError("tile_size must be a positive integer")


def _validate_tile_index(zoom: int, index: int) -> None:
    _validate_zoom_and_tile_size(zoom, 256)
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("tile index must be an integer")
    if not 0 <= index < (1 << zoom):
        raise ValueError("tile index is outside the selected zoom")


__all__ = [
    "EARTH_MEAN_RADIUS_M",
    "WEB_MERCATOR_MAX_LATITUDE",
    "clamp_mercator_latitude",
    "destination_point",
    "geodesic_ring",
    "haversine_distance_m",
    "initial_bearing_deg",
    "latlon_to_tile",
    "latlon_to_world",
    "tile_bounds",
    "world_to_latlon",
    "wrapped_world_x_delta",
    "xyz_to_tms_y",
]
