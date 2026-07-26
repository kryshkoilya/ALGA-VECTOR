"""Runtime-facing offline map service with bounded tile caching."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock

from alga_vector.location import GeoPoint

from .mbtiles import MBTilesMetadata, MBTilesPackage


class MapAvailability(StrEnum):
    UNSET = "unset"
    READY = "ready"
    ERROR = "error"


@dataclass(slots=True, frozen=True)
class MapSnapshot:
    availability: MapAvailability
    package_path: Path | None = None
    name: str = ""
    minimum_zoom: int | None = None
    maximum_zoom: int | None = None
    bounds: tuple[float, float, float, float] | None = None
    center: tuple[float, float, int] | None = None
    attribution: str = ""
    base_in_coverage: bool | None = None
    message_ru: str = ""
    error_code: str | None = None
    source: str = "offline"
    network_enabled: bool = False
    online_cached_tiles: int = 0
    online_pending_tiles: int = 0
    online_last_error_code: str | None = None
    online_state: str = "disabled"

    @property
    def available(self) -> bool:
        return self.availability is MapAvailability.READY


class OfflineMapService:
    """Own one validated raster MBTiles package and a bounded byte cache."""

    def __init__(
        self,
        package_path: Path | None = None,
        *,
        cache_mib: int = 128,
    ) -> None:
        if not 16 <= cache_mib <= 2048:
            raise ValueError("cache_mib must be in 16..2048")
        self.maximum_cache_bytes = cache_mib * 1024 * 1024
        self._lock = RLock()
        self._package: MBTilesPackage | None = None
        self._cache: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._cache_bytes = 0
        self._snapshot = MapSnapshot(
            MapAvailability.UNSET,
            message_ru="Пакет офлайн-карты не выбран.",
        )
        if package_path is not None:
            self.open(package_path)

    def open(self, path: Path) -> MapSnapshot:
        candidate = MBTilesPackage(Path(path))
        with self._lock:
            previous = self._package
            self._package = candidate
            self._clear_cache()
            if previous is not None:
                previous.close()
            metadata = candidate.metadata
            self._snapshot = _ready_snapshot(candidate.path, metadata)
            return self._snapshot

    def clear(self) -> MapSnapshot:
        with self._lock:
            package = self._package
            self._package = None
            self._clear_cache()
            if package is not None:
                package.close()
            self._snapshot = MapSnapshot(
                MapAvailability.UNSET,
                message_ru="Пакет офлайн-карты не выбран.",
            )
            return self._snapshot

    def snapshot(self) -> MapSnapshot:
        with self._lock:
            return self._snapshot

    def get_tile(self, zoom: int, x: int, y: int) -> bytes | None:
        key = (zoom, x, y)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            package = self._package
            if package is None:
                return None
            payload = package.get_tile(zoom, x, y)
            if payload is not None and len(payload) <= self.maximum_cache_bytes:
                self._cache[key] = payload
                self._cache_bytes += len(payload)
                self._evict()
            return payload

    def contains(self, point: GeoPoint) -> bool | None:
        with self._lock:
            bounds = self._snapshot.bounds
        if bounds is None:
            return None
        west, south, east, north = bounds
        if west <= east:
            longitude_inside = west <= point.longitude_deg <= east
        else:
            longitude_inside = point.longitude_deg >= west or point.longitude_deg <= east
        return longitude_inside and south <= point.latitude_deg <= north

    def close(self) -> None:
        self.clear()

    def _evict(self) -> None:
        while self._cache and self._cache_bytes > self.maximum_cache_bytes:
            _key, payload = self._cache.popitem(last=False)
            self._cache_bytes -= len(payload)

    def _clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0


def _ready_snapshot(path: Path, metadata: MBTilesMetadata) -> MapSnapshot:
    return MapSnapshot(
        MapAvailability.READY,
        package_path=path,
        name=metadata.name,
        minimum_zoom=metadata.minimum_zoom,
        maximum_zoom=metadata.maximum_zoom,
        bounds=metadata.bounds,
        center=metadata.center,
        attribution=metadata.attribution or "Источник и лицензия не указаны",
        message_ru="Локальный пакет карты проверен и открыт только для чтения.",
    )


__all__ = [
    "MapAvailability",
    "MapSnapshot",
    "OfflineMapService",
]
