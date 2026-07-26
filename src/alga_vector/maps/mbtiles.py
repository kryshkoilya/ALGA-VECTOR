from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from alga_vector.location.geometry import xyz_to_tms_y

_SQLITE_HEADER = b"SQLite format 3\x00"
_SUPPORTED_RASTER_FORMATS = frozenset({"png", "jpg", "jpeg", "webp"})


class MBTilesError(RuntimeError):
    pass


class MBTilesValidationError(MBTilesError):
    pass


@dataclass(slots=True, frozen=True)
class MBTilesMetadata:
    name: str
    format: str
    minimum_zoom: int
    maximum_zoom: int
    bounds: tuple[float, float, float, float] | None = None
    center: tuple[float, float, int] | None = None
    attribution: str | None = None
    description: str | None = None
    version: str | None = None


class MBTilesPackage:
    """A bounded, query-only reader for raster MBTiles 1.3 packages."""

    def __init__(
        self,
        path: Path,
        *,
        maximum_package_bytes: int = 64 * 1024 * 1024 * 1024,
        maximum_tile_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.path = Path(path).resolve()
        self.maximum_package_bytes = _positive_int(
            "maximum_package_bytes",
            maximum_package_bytes,
        )
        self.maximum_tile_bytes = _positive_int(
            "maximum_tile_bytes",
            maximum_tile_bytes,
        )
        self._connection: sqlite3.Connection | None = None
        self._metadata: MBTilesMetadata | None = None
        self._open()

    @property
    def metadata(self) -> MBTilesMetadata:
        if self._metadata is None:
            raise MBTilesError("MBTiles package is closed")
        return self._metadata

    @property
    def closed(self) -> bool:
        return self._connection is None

    def get_tile(self, zoom: int, x: int, y: int) -> bytes | None:
        """Return one tile addressed with normal XYZ coordinates."""

        _validate_tile_coordinate(zoom, x, y)
        metadata = self.metadata
        if not metadata.minimum_zoom <= zoom <= metadata.maximum_zoom:
            return None
        connection = self._require_connection()
        tms_y = xyz_to_tms_y(zoom, y)
        try:
            row = connection.execute(
                "SELECT tile_data FROM tiles "
                "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? "
                "LIMIT 1",
                (zoom, x, tms_y),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MBTilesError("tile query failed") from exc
        if row is None:
            return None
        raw = row[0]
        if isinstance(raw, memoryview):
            payload = raw.tobytes()
        elif isinstance(raw, bytes):
            payload = raw
        else:
            raise MBTilesValidationError("tile_data is not a byte payload")
        if not payload or len(payload) > self.maximum_tile_bytes:
            raise MBTilesValidationError("tile payload is empty or oversized")
        _validate_raster_signature(payload, metadata.format)
        return payload

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._metadata = None
        if connection is not None:
            connection.close()

    def __enter__(self) -> MBTilesPackage:
        if self.closed:
            raise MBTilesError("MBTiles package is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def _open(self) -> None:
        if not self.path.is_file():
            raise MBTilesValidationError("MBTiles path is not a regular file")
        size = self.path.stat().st_size
        if size <= len(_SQLITE_HEADER) or size > self.maximum_package_bytes:
            raise MBTilesValidationError("MBTiles package size is invalid")
        try:
            with self.path.open("rb") as handle:
                if handle.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                    raise MBTilesValidationError("MBTiles package is not SQLite")
        except OSError as exc:
            raise MBTilesValidationError("MBTiles package cannot be read") from exc

        uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=1.0,
                check_same_thread=False,
            )
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA busy_timeout = 1000")
            self._connection = connection
            self._validate_schema()
            self._metadata = self._load_metadata()
        except MBTilesError:
            self.close()
            raise
        except sqlite3.Error as exc:
            self.close()
            raise MBTilesValidationError("MBTiles SQLite validation failed") from exc

    def _validate_schema(self) -> None:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT name, type FROM sqlite_schema "
            "WHERE name IN ('metadata', 'tiles')"
        ).fetchall()
        objects = {
            str(row[0]): str(row[1])
            for row in rows
            if row[1] in {"table", "view"}
        }
        if set(objects) != {"metadata", "tiles"}:
            raise MBTilesValidationError("metadata and tiles objects are required")
        metadata_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(metadata)")
        }
        tile_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(tiles)")
        }
        if not {"name", "value"} <= metadata_columns:
            raise MBTilesValidationError("metadata columns are invalid")
        if not {
            "zoom_level",
            "tile_column",
            "tile_row",
            "tile_data",
        } <= tile_columns:
            raise MBTilesValidationError("tiles columns are invalid")

    def _load_metadata(self) -> MBTilesMetadata:
        connection = self._require_connection()
        values: dict[str, str] = {}
        for raw_name, raw_value in connection.execute(
            "SELECT name, value FROM metadata"
        ):
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise MBTilesValidationError("metadata names and values must be text")
            name = raw_name.strip().lower()
            if not name or len(name) > 128 or len(raw_value) > 16_384:
                raise MBTilesValidationError("metadata field is empty or oversized")
            if name in values:
                raise MBTilesValidationError(f"duplicate metadata field: {name}")
            values[name] = raw_value.strip()

        name = values.get("name", "").strip()
        format_name = values.get("format", "").lower()
        if not name:
            raise MBTilesValidationError("metadata.name is required")
        if format_name not in _SUPPORTED_RASTER_FORMATS:
            raise MBTilesValidationError(
                "only PNG, JPEG and WebP raster MBTiles are supported"
            )
        minimum_zoom, maximum_zoom = self._zoom_range(values)
        bounds = _parse_bounds(values["bounds"]) if "bounds" in values else None
        center = _parse_center(values["center"]) if "center" in values else None
        if center is not None and not minimum_zoom <= center[2] <= maximum_zoom:
            raise MBTilesValidationError("metadata.center zoom is outside package range")
        return MBTilesMetadata(
            name=name,
            format=format_name,
            minimum_zoom=minimum_zoom,
            maximum_zoom=maximum_zoom,
            bounds=bounds,
            center=center,
            attribution=values.get("attribution") or None,
            description=values.get("description") or None,
            version=values.get("version") or None,
        )

    def _zoom_range(self, values: dict[str, str]) -> tuple[int, int]:
        if "minzoom" in values and "maxzoom" in values:
            minimum = _zoom_value(values["minzoom"], "minzoom")
            maximum = _zoom_value(values["maxzoom"], "maxzoom")
        else:
            row = self._require_connection().execute(
                "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
            ).fetchone()
            if row is None or row[0] is None or row[1] is None:
                raise MBTilesValidationError("MBTiles package contains no tiles")
            minimum = _zoom_value(row[0], "minimum tile zoom")
            maximum = _zoom_value(row[1], "maximum tile zoom")
        if minimum > maximum:
            raise MBTilesValidationError("minimum zoom exceeds maximum zoom")
        return minimum, maximum

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MBTilesError("MBTiles package is closed")
        return self._connection


def _parse_bounds(value: str) -> tuple[float, float, float, float]:
    parts = _comma_floats(value, expected=4, label="bounds")
    west, south, east, north = parts
    if not -180.0 <= west <= 180.0 or not -180.0 <= east <= 180.0:
        raise MBTilesValidationError("metadata.bounds longitude is invalid")
    if not -90.0 <= south <= 90.0 or not -90.0 <= north <= 90.0:
        raise MBTilesValidationError("metadata.bounds latitude is invalid")
    if south > north:
        raise MBTilesValidationError("metadata.bounds south exceeds north")
    return west, south, east, north


def _parse_center(value: str) -> tuple[float, float, int]:
    parts = value.split(",")
    if len(parts) != 3:
        raise MBTilesValidationError("metadata.center must contain lon,lat,zoom")
    floats = _comma_floats(",".join(parts[:2]), expected=2, label="center")
    try:
        zoom = int(parts[2])
    except ValueError as exc:
        raise MBTilesValidationError("metadata.center zoom is invalid") from exc
    longitude, latitude = floats
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        raise MBTilesValidationError("metadata.center is outside WGS84 bounds")
    _zoom_value(zoom, "center zoom")
    return longitude, latitude, zoom


def _comma_floats(value: str, *, expected: int, label: str) -> tuple[float, ...]:
    parts = value.split(",")
    if len(parts) != expected:
        raise MBTilesValidationError(
            f"metadata.{label} must contain {expected} numbers"
        )
    try:
        converted = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise MBTilesValidationError(f"metadata.{label} is not numeric") from exc
    if any(number != number or number in {float("inf"), float("-inf")} for number in converted):
        raise MBTilesValidationError(f"metadata.{label} must be finite")
    return converted


def _zoom_value(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise MBTilesValidationError(f"{label} is invalid")
    if isinstance(value, int):
        converted = value
    elif isinstance(value, str):
        try:
            converted = int(value)
        except ValueError as exc:
            raise MBTilesValidationError(f"{label} is invalid") from exc
    else:
        raise MBTilesValidationError(f"{label} is invalid")
    if not 0 <= converted <= 30:
        raise MBTilesValidationError(f"{label} is outside 0..30")
    return converted


def _validate_tile_coordinate(zoom: int, x: int, y: int) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (zoom, x, y)):
        raise TypeError("tile coordinates must be integers")
    if not 0 <= zoom <= 30:
        raise ValueError("zoom is outside 0..30")
    tile_count = 1 << zoom
    if not 0 <= x < tile_count or not 0 <= y < tile_count:
        raise ValueError("tile coordinate is outside the selected zoom")


def _validate_raster_signature(payload: bytes, format_name: str) -> None:
    valid = False
    if format_name == "png":
        valid = payload.startswith(b"\x89PNG\r\n\x1a\n")
    elif format_name in {"jpg", "jpeg"}:
        valid = payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")
    elif format_name == "webp":
        valid = (
            len(payload) >= 12
            and payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
        )
    if not valid:
        raise MBTilesValidationError("tile payload does not match metadata.format")


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


__all__ = [
    "MBTilesError",
    "MBTilesMetadata",
    "MBTilesPackage",
    "MBTilesValidationError",
]
