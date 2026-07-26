from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import AppConfig, MapConfig, StorageConfig
from alga_vector.direction import DirectionSource

FIXED_TIME = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FIXED_TIME


def _config(
    tmp_path: Path,
    *,
    mode: Literal["live", "demo", "safe"] = "live",
) -> AppConfig:
    return AppConfig(
        mode=mode,
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / mode),
        map=MapConfig(network_enabled=False),
    )


def test_live_runtime_direction_is_fail_closed_until_explicit_input(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _config(tmp_path),
        clock=_clock,
        background_acquisition=False,
    )
    try:
        initial = runtime.snapshot()
        assert initial.direction is not None
        assert not initial.direction.available
        assert initial.direction.current.source is DirectionSource.UNAVAILABLE

        manual = runtime.set_manual_direction(361.0, 20.0)
        assert manual.available
        assert manual.current.source is DirectionSource.MANUAL
        assert manual.current.bearing_deg == 1.0
        assert manual.current.confidence is None
        assert not manual.current.measured

        current = runtime.snapshot()
        assert current.direction.current.source is DirectionSource.MANUAL

        cleared = runtime.clear_direction()
        assert not cleared.available
        assert cleared.current.source is DirectionSource.UNAVAILABLE
    finally:
        runtime.shutdown()


def test_demo_runtime_publishes_only_marked_simulated_direction(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _config(tmp_path, mode="demo"),
        clock=_clock,
        background_acquisition=False,
    )
    try:
        snapshot = runtime.snapshot()
        assert snapshot.direction.available
        assert snapshot.direction.current.source is DirectionSource.SIMULATED
        assert snapshot.direction.current.reason_code == "DIRECTION.SIMULATED_DEMO"
        assert not snapshot.direction.current.measured
    finally:
        runtime.shutdown()
