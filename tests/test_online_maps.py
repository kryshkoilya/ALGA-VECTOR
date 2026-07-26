from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

import pytest

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import AppConfig, MapConfig, StorageConfig
from alga_vector.maps import (
    FetchResponse,
    OnlineMapState,
    OnlineTileProvider,
    OnlineTileService,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeFetcher:
    def __init__(
        self,
        *,
        payload: bytes = _PNG,
        content_type: str = "image/png",
        gate: threading.Event | None = None,
    ) -> None:
        self.payload = payload
        self.content_type = content_type
        self.gate = gate
        self.started = threading.Event()
        self.calls: list[tuple[str, dict[str, str], float, int]] = []
        self._lock = threading.Lock()

    def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        maximum_bytes: int,
    ) -> FetchResponse:
        with self._lock:
            self.calls.append((url, dict(headers), timeout_seconds, maximum_bytes))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=2.0)
        return FetchResponse(
            payload=self.payload,
            content_type=self.content_type,
            final_url=url,
        )


def _wait_idle(service: OnlineTileService, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.snapshot()
        if snapshot.pending_requests == 0 and (
            snapshot.successful_fetches or snapshot.failed_fetches
        ):
            return
        time.sleep(0.01)
    raise AssertionError("online tile service did not become idle")


def _service(
    cache_dir: Path,
    fetcher: FakeFetcher,
    **overrides: object,
) -> OnlineTileService:
    options: dict[str, object] = {
        "fetcher": fetcher,
        "requests_per_second": 10.0,
    }
    options.update(overrides)
    return OnlineTileService(cache_dir, **options)


def test_visible_tile_fetch_is_nonblocking_deduplicated_and_cached_offline(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    fetcher = FakeFetcher(gate=release)
    cache_dir = tmp_path / "online-cache"
    service = _service(cache_dir, fetcher)
    initial = service.snapshot()
    assert initial.state == OnlineMapState.CONFIGURED
    assert not initial.available
    service.set_visible_tiles(((2, 2, 1), (2, 3, 1)))
    assert fetcher.calls == []

    started_at = time.monotonic()
    for _attempt in range(8):
        assert service.get_tile(2, 2, 1) is None
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.2
    assert fetcher.started.wait(timeout=1.0)
    assert service.snapshot().state == OnlineMapState.LOADING
    assert len(fetcher.calls) == 1
    url, headers, timeout_seconds, maximum_bytes = fetcher.calls[0]
    assert url.startswith("https://")
    assert headers["User-Agent"].startswith("ALGA-VECTOR/")
    assert 0.5 <= timeout_seconds <= 15.0
    assert maximum_bytes <= 8 * 1024 * 1024

    release.set()
    _wait_idle(service)
    assert service.snapshot().state == OnlineMapState.READY
    assert service.get_tile(2, 2, 1) == _PNG
    service.close()

    offline_fetcher = FakeFetcher()
    offline = _service(
        cache_dir,
        offline_fetcher,
        network_enabled=False,
    )
    assert not offline.snapshot().available
    assert offline.get_tile(2, 2, 1) == _PNG
    assert offline_fetcher.calls == []
    assert offline.snapshot().available
    offline.close()


def test_rejects_unsafe_provider_and_invalid_content_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OnlineTileProvider(
            url_template="http://example.test/{z}/{x}/{y}.png",
            attribution="Test",
            minimum_zoom=0,
            maximum_zoom=4,
        )

    fetcher = FakeFetcher(content_type="text/html")
    service = _service(tmp_path / "cache", fetcher)
    assert service.get_tile(0, 0, 0) is None
    _wait_idle(service)

    snapshot = service.snapshot()
    assert snapshot.successful_fetches == 0
    assert snapshot.failed_fetches == 1
    assert snapshot.last_error_code == "MAP.ONLINE_CONTENT_TYPE"
    assert snapshot.state == OnlineMapState.ERROR
    assert not snapshot.available
    assert "0/0/0" not in repr(snapshot)
    assert service.force_retry()
    assert service.snapshot().state == OnlineMapState.CONFIGURED
    assert service.snapshot().last_error_code is None
    service.close()


def test_disk_and_memory_caches_remain_bounded(tmp_path: Path) -> None:
    payload = _PNG + b"x" * 600
    fetcher = FakeFetcher(payload=payload)
    service = _service(
        tmp_path / "bounded",
        fetcher,
        maximum_tile_bytes=1024,
        maximum_disk_bytes=1200,
        maximum_memory_bytes=1024,
    )

    for x in range(4):
        assert service.get_tile(2, x, 1) is None
        _wait_idle(service)

    snapshot = service.snapshot()
    assert snapshot.cached_tiles <= 1
    assert snapshot.memory_tiles <= 1
    assert all(path.suffix == ".tile" for path in (tmp_path / "bounded").iterdir())
    assert all(len(path.stem) == 64 for path in (tmp_path / "bounded").glob("*.tile"))
    service.close()


def test_runtime_auto_map_uses_online_cache_and_local_tiles_have_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FakeFetcher()
    online = _service(tmp_path / "cache", fetcher)
    config = AppConfig(
        storage=StorageConfig(data_dir=tmp_path / "runtime"),
        map=MapConfig(network_enabled=True, online_cache_mib=16),
    )
    runtime = ApplicationRuntime(config, online_map_service=online)

    snapshot = runtime.map_snapshot()
    assert not snapshot.available
    assert snapshot.online_state == "configured"
    assert snapshot.source == "online_not_ready"
    assert "OpenStreetMap" in snapshot.name
    assert snapshot.attribution

    monkeypatch.setattr(runtime._map_service, "get_tile", lambda *_args: b"local")
    assert runtime.map_tile(0, 0, 0) == b"local"
    assert fetcher.calls == []

    monkeypatch.setattr(runtime._map_service, "get_tile", lambda *_args: None)
    assert runtime.map_tile(0, 0, 0) is None
    _wait_idle(online)
    assert runtime.map_tile(0, 0, 0) == _PNG
    ready = runtime.map_snapshot()
    assert ready.available
    assert ready.source == "online_visible_cache"
    runtime.shutdown()
