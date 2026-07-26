"""Visible-view online raster tiles with bounded offline caching.

Only explicitly requested tiles are fetched.  The service has no region,
route, or bulk-prefetch API by design.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

OSM_ATTRIBUTION = "© OpenStreetMap contributors · ODbL"
OSM_USER_AGENT = "ALGA-VECTOR/0.5.0 (retired compatibility map client)"
_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


@dataclass(frozen=True, slots=True)
class OnlineTileProvider:
    """Code-owned tile provider definition; it is not loaded from user input."""

    url_template: str
    attribution: str
    minimum_zoom: int
    maximum_zoom: int

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url_template)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("online tile provider must use HTTPS")
        if not {"{z}", "{x}", "{y}"} <= {
            part
            for part in ("{z}", "{x}", "{y}")
            if part in self.url_template
        }:
            raise ValueError("tile URL template must contain z/x/y placeholders")
        if not self.attribution.strip():
            raise ValueError("online tile attribution is required")
        if not 0 <= self.minimum_zoom <= self.maximum_zoom <= 22:
            raise ValueError("online provider zoom range is invalid")

    def tile_url(self, zoom: int, x: int, y: int) -> str:
        return self.url_template.format(z=zoom, x=x, y=y)


OSM_PROVIDER = OnlineTileProvider(
    url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution=OSM_ATTRIBUTION,
    minimum_zoom=0,
    maximum_zoom=19,
)


@dataclass(frozen=True, slots=True)
class FetchResponse:
    payload: bytes
    content_type: str
    final_url: str
    status: int = 200


class TileFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> FetchResponse: ...


class TileFetchError(RuntimeError):
    """Sanitized fetch failure suitable for privacy-preserving diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OnlineMapState(StrEnum):
    DISABLED = "disabled"
    CONFIGURED = "configured"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


class UrlLibTileFetcher:
    """Small HTTPS fetcher with bounded reads and redirect validation."""

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> FetchResponse:
        if urlsplit(url).scheme != "https":
            raise TileFetchError("MAP.ONLINE_HTTPS_REQUIRED")
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                final_url = str(response.geturl())
                if urlsplit(final_url).scheme != "https":
                    raise TileFetchError("MAP.ONLINE_REDIRECT_REJECTED")
                status = int(response.getcode() or 0)
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise TileFetchError("MAP.ONLINE_LENGTH_INVALID") from exc
                    if declared_size < 1 or declared_size > maximum_bytes:
                        raise TileFetchError("MAP.ONLINE_TILE_OVERSIZED")
                payload = response.read(maximum_bytes + 1)
                content_type = str(response.headers.get("Content-Type", ""))
        except TileFetchError:
            raise
        except TimeoutError as exc:
            raise TileFetchError("MAP.ONLINE_TIMEOUT") from exc
        except urllib.error.HTTPError as exc:
            raise TileFetchError(f"MAP.ONLINE_HTTP_{exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TileFetchError("MAP.ONLINE_UNREACHABLE") from exc
        return FetchResponse(
            payload=payload,
            content_type=content_type,
            final_url=final_url,
            status=status,
        )


@dataclass(frozen=True, slots=True)
class OnlineMapSnapshot:
    state: OnlineMapState
    network_enabled: bool
    cached_tiles: int
    memory_tiles: int
    pending_requests: int
    generation: int
    successful_fetches: int
    failed_fetches: int
    last_error_code: str | None
    attribution: str
    minimum_zoom: int
    maximum_zoom: int
    message_ru: str

    @property
    def available(self) -> bool:
        return self.state is OnlineMapState.READY


@dataclass(frozen=True, slots=True)
class _MemoryEntry:
    payload: bytes
    stored_at: float


class OnlineTileService:
    """Nonblocking visible-tile loader with bounded memory and disk caches."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        network_enabled: bool = True,
        cache_mib: int = 256,
        memory_cache_mib: int = 16,
        cache_ttl_days: int = 30,
        timeout_seconds: float = 5.0,
        maximum_tile_bytes: int = 2 * 1024 * 1024,
        requests_per_second: float = 2.0,
        maximum_pending: int = 48,
        provider: OnlineTileProvider = OSM_PROVIDER,
        fetcher: TileFetcher | None = None,
        user_agent: str = OSM_USER_AGENT,
        maximum_disk_bytes: int | None = None,
        maximum_memory_bytes: int | None = None,
    ) -> None:
        if not 16 <= cache_mib <= 2048:
            raise ValueError("cache_mib must be in 16..2048")
        if not 1 <= memory_cache_mib <= 256:
            raise ValueError("memory_cache_mib must be in 1..256")
        if cache_ttl_days < 7:
            raise ValueError("online tiles must be cached for at least seven days")
        if not 0.5 <= timeout_seconds <= 15.0:
            raise ValueError("timeout_seconds must be in 0.5..15")
        if not 1024 <= maximum_tile_bytes <= 8 * 1024 * 1024:
            raise ValueError("maximum_tile_bytes is outside safe bounds")
        if not 0.1 <= requests_per_second <= 10.0:
            raise ValueError("requests_per_second is outside safe bounds")
        if not 1 <= maximum_pending <= 256:
            raise ValueError("maximum_pending is outside safe bounds")
        if not user_agent.strip():
            raise ValueError("an explicit User-Agent is required")

        disk_limit = maximum_disk_bytes or cache_mib * 1024 * 1024
        memory_limit = maximum_memory_bytes or memory_cache_mib * 1024 * 1024
        if disk_limit < maximum_tile_bytes or memory_limit < maximum_tile_bytes:
            raise ValueError("cache limits must fit at least one maximum-size tile")

        self.cache_dir = Path(cache_dir)
        self.network_enabled = network_enabled
        self.maximum_disk_bytes = disk_limit
        self.maximum_memory_bytes = memory_limit
        self.cache_ttl_seconds = cache_ttl_days * 24 * 60 * 60
        self.timeout_seconds = timeout_seconds
        self.maximum_tile_bytes = maximum_tile_bytes
        self.requests_per_second = requests_per_second
        self.maximum_pending = maximum_pending
        self.provider = provider
        self.fetcher = fetcher or UrlLibTileFetcher()
        self.user_agent = user_agent

        self._lock = RLock()
        self._memory: OrderedDict[tuple[int, int, int], _MemoryEntry] = OrderedDict()
        self._memory_bytes = 0
        self._pending: set[tuple[int, int, int]] = set()
        self._visible_keys: set[tuple[int, int, int]] = set()
        self._visible_ready: set[tuple[int, int, int]] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        self._generation = 0
        self._successful_fetches = 0
        self._failed_fetches = 0
        self._last_error_code: str | None = None
        self._consecutive_failures = 0
        self._retry_not_before = 0.0
        self._disk_index: dict[Path, tuple[int, float]] = {}
        self._disk_bytes = 0
        self._disk_available = True
        self._tokens = 1.0
        self._last_refill = time.monotonic()
        self._prepare_disk_cache()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def snapshot(self) -> OnlineMapSnapshot:
        with self._lock:
            cached_tiles = len(self._disk_index)
            has_visible_cache = bool(self._visible_ready & self._visible_keys)
            has_visible_pending = bool(self._pending & self._visible_keys)
            if has_visible_cache:
                state = OnlineMapState.READY
            elif has_visible_pending:
                state = OnlineMapState.LOADING
            elif self._last_error_code is not None:
                state = OnlineMapState.ERROR
            elif self.network_enabled:
                state = OnlineMapState.CONFIGURED
            else:
                state = OnlineMapState.DISABLED
            if state is OnlineMapState.READY and self.network_enabled:
                message = (
                    "Карта готова: видимые тайлы загружаются по HTTPS, "
                    "локальный кэш используется офлайн."
                )
            elif state is OnlineMapState.READY:
                message = "Сеть для карт отключена; используются только ранее сохранённые тайлы."
            elif state is OnlineMapState.LOADING:
                message = "Загружаются первые видимые тайлы карты."
            elif state is OnlineMapState.CONFIGURED:
                message = "Сетевая карта настроена и ожидает запроса видимой области."
            elif state is OnlineMapState.ERROR:
                message = "Карта ещё не готова: сетевой запрос завершился ошибкой."
            else:
                message = (
                    "Сеть для карт отключена; для текущей области нет сохранённых тайлов."
                    if cached_tiles
                    else "Сеть для карт отключена; локальный кэш пуст."
                )
            if cached_tiles and not has_visible_cache:
                message += " В кэше есть тайлы других ранее просмотренных областей."
            if self._last_error_code is not None:
                message += f" Последняя ошибка: {self._last_error_code}."
            return OnlineMapSnapshot(
                state=state,
                network_enabled=self.network_enabled,
                cached_tiles=cached_tiles,
                memory_tiles=len(self._memory),
                pending_requests=len(self._pending),
                generation=self._generation,
                successful_fetches=self._successful_fetches,
                failed_fetches=self._failed_fetches,
                last_error_code=self._last_error_code,
                attribution=self.provider.attribution,
                minimum_zoom=self.provider.minimum_zoom,
                maximum_zoom=self.provider.maximum_zoom,
                message_ru=message,
            )

    def set_visible_tiles(
        self,
        keys: tuple[tuple[int, int, int], ...],
    ) -> None:
        """Declare the current viewport without scheduling or prefetching it."""

        if len(keys) > 64:
            raise ValueError("visible viewport exceeds 64 tiles")
        validated: set[tuple[int, int, int]] = set()
        for zoom, x, y in keys:
            _validate_tile_coordinate(zoom, x, y, self.provider)
            validated.add((zoom, x, y))
        with self._lock:
            if validated == self._visible_keys:
                return
            self._visible_keys = validated
            self._visible_ready.intersection_update(validated)
            self._generation += 1

    def force_retry(self) -> bool:
        """Clear bounded retry backoff; the caller must repaint the visible view."""

        with self._lock:
            if self._closed or not self.network_enabled:
                return False
            self._last_error_code = None
            self._consecutive_failures = 0
            self._retry_not_before = 0.0
            self._generation += 1
            return True

    def get_tile(self, zoom: int, x: int, y: int) -> bytes | None:
        """Return a cache hit or enqueue exactly this tile and return immediately."""

        _validate_tile_coordinate(zoom, x, y, self.provider)
        key = (zoom, x, y)
        now = time.time()
        with self._lock:
            if key not in self._visible_keys:
                self._visible_keys.add(key)
            entry = self._memory.get(key)
            if entry is not None:
                self._memory.move_to_end(key)
                self._mark_visible_ready_locked(key)
                if self.network_enabled and now - entry.stored_at > self.cache_ttl_seconds:
                    self._schedule_locked(key)
                return entry.payload

        disk_payload, stored_at = self._read_disk(key)
        if disk_payload is not None:
            with self._lock:
                self._put_memory_locked(key, disk_payload, stored_at)
                self._mark_visible_ready_locked(key)
                if self.network_enabled and now - stored_at > self.cache_ttl_seconds:
                    self._schedule_locked(key)
            return disk_payload

        with self._lock:
            if self.network_enabled:
                self._schedule_locked(key)
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def _schedule_locked(self, key: tuple[int, int, int]) -> None:
        if self._closed or key in self._pending:
            return
        if time.monotonic() < self._retry_not_before:
            return
        if len(self._pending) >= self.maximum_pending:
            self._failed_fetches += 1
            self._last_error_code = "MAP.ONLINE_QUEUE_FULL"
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="alga-map",
            )
        self._pending.add(key)
        self._executor.submit(self._fetch_one, key)

    def _fetch_one(self, key: tuple[int, int, int]) -> None:
        try:
            self._wait_for_rate_limit()
            zoom, x, y = key
            url = self.provider.tile_url(zoom, x, y)
            response = self.fetcher.fetch(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "image/png,image/jpeg,image/webp",
                },
                timeout_seconds=self.timeout_seconds,
                maximum_bytes=self.maximum_tile_bytes,
            )
            payload = self._validated_response(response)
            stored_at = time.time()
            self._write_disk(key, payload, stored_at)
            with self._lock:
                if self._closed:
                    return
                self._put_memory_locked(key, payload, stored_at)
                self._mark_visible_ready_locked(key)
                self._successful_fetches += 1
                self._last_error_code = None
                self._consecutive_failures = 0
                self._retry_not_before = 0.0
                self._generation += 1
        except TileFetchError as exc:
            self._record_failure(exc.code)
        except Exception:
            self._record_failure("MAP.ONLINE_FETCH_FAILED")
        finally:
            with self._lock:
                self._pending.discard(key)

    def _validated_response(self, response: FetchResponse) -> bytes:
        if response.status != 200:
            raise TileFetchError(f"MAP.ONLINE_HTTP_{response.status}")
        initial_host = urlsplit(self.provider.url_template).hostname
        final = urlsplit(response.final_url)
        if final.scheme != "https" or final.hostname != initial_host:
            raise TileFetchError("MAP.ONLINE_REDIRECT_REJECTED")
        content_type = response.content_type.split(";", 1)[0].strip().lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise TileFetchError("MAP.ONLINE_CONTENT_TYPE")
        payload = bytes(response.payload)
        if not payload or len(payload) > self.maximum_tile_bytes:
            raise TileFetchError("MAP.ONLINE_TILE_OVERSIZED")
        if not _valid_raster_payload(payload, content_type):
            raise TileFetchError("MAP.ONLINE_RASTER_INVALID")
        return payload

    def _wait_for_rate_limit(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    raise TileFetchError("MAP.ONLINE_CLOSED")
                now = time.monotonic()
                elapsed = max(0.0, now - self._last_refill)
                self._tokens = min(
                    2.0,
                    self._tokens + elapsed * self.requests_per_second,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / self.requests_per_second
            time.sleep(min(wait_seconds, 0.25))

    def _record_failure(self, code: str) -> None:
        with self._lock:
            if self._closed and code == "MAP.ONLINE_CLOSED":
                return
            self._failed_fetches += 1
            self._last_error_code = code
            self._consecutive_failures += 1
            backoff_seconds = min(60.0, float(2 ** min(self._consecutive_failures, 5)))
            self._retry_not_before = time.monotonic() + backoff_seconds
            self._generation += 1

    def _prepare_disk_cache(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            for path in self.cache_dir.glob("*.tile"):
                if not path.is_file():
                    continue
                stat = path.stat()
                if 0 < stat.st_size <= self.maximum_tile_bytes:
                    self._disk_index[path] = (stat.st_size, stat.st_mtime)
                    self._disk_bytes += stat.st_size
            self._evict_disk()
        except OSError:
            self._disk_available = False
            self._disk_index.clear()
            self._disk_bytes = 0
            self._last_error_code = "MAP.ONLINE_CACHE_UNAVAILABLE"

    def _read_disk(
        self,
        key: tuple[int, int, int],
    ) -> tuple[bytes | None, float]:
        if not self._disk_available:
            return None, 0.0
        path = self._disk_path(key)
        try:
            payload = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError:
            return None, 0.0
        except OSError:
            self._record_failure("MAP.ONLINE_CACHE_READ_FAILED")
            return None, 0.0
        if (
            not payload
            or len(payload) > self.maximum_tile_bytes
            or not _valid_raster_payload(payload)
        ):
            with suppress(OSError):
                path.unlink()
            with self._lock:
                previous = self._disk_index.pop(path, None)
                if previous is not None:
                    self._disk_bytes -= previous[0]
            self._record_failure("MAP.ONLINE_CACHE_CORRUPT")
            return None, 0.0
        return payload, stat.st_mtime

    def _write_disk(
        self,
        key: tuple[int, int, int],
        payload: bytes,
        stored_at: float,
    ) -> None:
        if not self._disk_available:
            return
        path = self._disk_path(key)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.utime(path, (stored_at, stored_at))
            with self._lock:
                previous = self._disk_index.get(path)
                if previous is not None:
                    self._disk_bytes -= previous[0]
                self._disk_index[path] = (len(payload), stored_at)
                self._disk_bytes += len(payload)
                self._evict_disk()
        except OSError:
            with suppress(OSError):
                temporary.unlink()
            self._record_failure("MAP.ONLINE_CACHE_WRITE_FAILED")

    def _disk_path(self, key: tuple[int, int, int]) -> Path:
        # Hashed names avoid exposing tile coordinates in filesystem listings or
        # diagnostics while retaining deterministic offline lookup.
        identity = f"osm-v1|{key[0]}|{key[1]}|{key[2]}".encode()
        return self.cache_dir / f"{hashlib.sha256(identity).hexdigest()}.tile"

    def _evict_disk(self) -> None:
        while self._disk_index and self._disk_bytes > self.maximum_disk_bytes:
            oldest = min(
                self._disk_index,
                key=lambda path: self._disk_index[path][1],
            )
            size, _mtime = self._disk_index.pop(oldest)
            try:
                oldest.unlink()
            except OSError:
                self._disk_index[oldest] = (size, _mtime)
                self._disk_available = False
                self._last_error_code = "MAP.ONLINE_CACHE_EVICTION_FAILED"
                return
            self._disk_bytes -= size

    def _put_memory_locked(
        self,
        key: tuple[int, int, int],
        payload: bytes,
        stored_at: float,
    ) -> None:
        previous = self._memory.pop(key, None)
        if previous is not None:
            self._memory_bytes -= len(previous.payload)
        self._memory[key] = _MemoryEntry(payload=payload, stored_at=stored_at)
        self._memory_bytes += len(payload)
        while self._memory and self._memory_bytes > self.maximum_memory_bytes:
            _old_key, old = self._memory.popitem(last=False)
            self._memory_bytes -= len(old.payload)

    def _mark_visible_ready_locked(self, key: tuple[int, int, int]) -> None:
        if key in self._visible_keys and key not in self._visible_ready:
            self._visible_ready.add(key)
            self._generation += 1


def _validate_tile_coordinate(
    zoom: int,
    x: int,
    y: int,
    provider: OnlineTileProvider,
) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (zoom, x, y)):
        raise TypeError("tile coordinates must be integers")
    if not provider.minimum_zoom <= zoom <= provider.maximum_zoom:
        raise ValueError("zoom is outside online provider bounds")
    tile_count = 1 << zoom
    if not 0 <= x < tile_count or not 0 <= y < tile_count:
        raise ValueError("tile coordinate is outside the selected zoom")


def _valid_raster_payload(payload: bytes, content_type: str | None = None) -> bool:
    png = payload.startswith(b"\x89PNG\r\n\x1a\n")
    jpeg = payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")
    webp = (
        len(payload) >= 12
        and payload.startswith(b"RIFF")
        and payload[8:12] == b"WEBP"
    )
    if content_type == "image/png":
        return png
    if content_type == "image/jpeg":
        return jpeg
    if content_type == "image/webp":
        return webp
    return png or jpeg or webp


__all__ = [
    "OSM_ATTRIBUTION",
    "OSM_PROVIDER",
    "OSM_USER_AGENT",
    "FetchResponse",
    "OnlineMapSnapshot",
    "OnlineMapState",
    "OnlineTileProvider",
    "OnlineTileService",
    "TileFetchError",
    "TileFetcher",
    "UrlLibTileFetcher",
]
