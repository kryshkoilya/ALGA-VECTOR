from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import AppConfig, SpectrumConfig, StorageConfig
from alga_vector.devices import DeviceAdapter, DeviceManager
from alga_vector.domain.enums import (
    Capability,
    DeviceState,
    HealthLevel,
    Provenance,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame
from alga_vector.signal_analysis import (
    AssessmentState,
    AttributionStatus,
    DecisionLifecycle,
)

START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
FrameSpec = tuple[str, np.ndarray[tuple[int, ...], np.dtype[np.float32]], int, int]


class ScriptedSpectrumAdapter(DeviceAdapter):
    def __init__(self, script: list[FrameSpec | Exception | None]) -> None:
        super().__init__(
            adapter_id="scripted-spectrum",
            display_name="Scripted spectrum",
            kind="test",
            connection="TEST:SCRIPTED",
            capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
        )
        self._script = deque(script)

    def inspect(self) -> DeviceSnapshot:
        self._ensure_open()
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
        )

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        del bins
        self._ensure_open()
        item = self._script.popleft() if self._script else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            return None
        source_id, power, center_override_hz, age_ms = item
        return SpectrumFrame(
            source_id=source_id,
            sequence=sequence,
            center_frequency_hz=center_override_hz or center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=START + timedelta(milliseconds=sequence * 100),
            provenance=Provenance.LIVE,
            data_age_ms=age_ms,
        )


def _runtime(
    tmp_path: Path,
    script: list[FrameSpec | Exception | None],
) -> ApplicationRuntime:
    config = AppConfig(
        mode="demo",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / "runtime"),
        spectrum=SpectrumConfig(
            center_frequency_hz=100_000_000,
            span_hz=2_000_000,
        ),
    )
    return ApplicationRuntime(
        config,
        device_manager=DeviceManager((ScriptedSpectrumAdapter(script),)),
        clock=lambda: START,
    )


def _quiet(
    source_id: str = "receiver-a",
    *,
    center_hz: int = 100_000_000,
    age_ms: int = 0,
) -> FrameSpec:
    return (
        source_id,
        np.full(128, -100.0, dtype=np.float32),
        center_hz,
        age_ms,
    )


def _activity(
    source_id: str = "receiver-a",
    *,
    center_hz: int = 100_000_000,
    age_ms: int = 0,
) -> FrameSpec:
    power = np.full(128, -100.0, dtype=np.float32)
    power[62:66] = -72.0
    return (source_id, power, center_hz, age_ms)


def test_runtime_snapshot_always_exposes_no_data_assessment(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [None])

    snapshot = runtime.snapshot()

    assert snapshot.spectrum is None
    assert snapshot.signal_assessment is not None
    assert snapshot.signal_assessment.state == AssessmentState.NO_DATA
    assert snapshot.signal_assessment.attribution == AttributionStatus.NOT_AVAILABLE
    assert snapshot.signal_assessment.identity_established is False
    runtime.shutdown()


def test_runtime_invalidates_previous_assessment_after_read_failure(
    tmp_path: Path,
) -> None:
    failure = AppError(
        code="SPECTRUM.TEST_FAILURE",
        message_ru="Тестовый сбой чтения.",
        operator_action_ru="Повторите чтение.",
    )
    runtime = _runtime(tmp_path, [_quiet()] * 9 + [failure])

    current = None
    for _ in range(9):
        current = runtime.snapshot()
    assert current is not None
    assert current.signal_assessment is not None
    assert current.signal_assessment.state == AssessmentState.BACKGROUND_ONLY

    failed = runtime.snapshot()

    assert failed.signal_assessment is not None
    assert failed.signal_assessment.state == AssessmentState.DATA_UNRELIABLE
    assert failed.signal_assessment.reason_code == "SPECTRUM.READ_FAILED"
    assert failed.signal_assessment.source_id == "receiver-a"
    assert failed.signal_assessment.trust.value == "low"
    runtime.shutdown()


def test_runtime_resets_learning_for_grid_and_source_but_not_ui_changes(
    tmp_path: Path,
) -> None:
    script = (
        [_quiet()] * 9
        + [_quiet(center_hz=101_000_000)]
        + [_quiet(center_hz=101_000_000)]
        + [_quiet("receiver-b", center_hz=101_000_000)]
        + [_quiet("receiver-a", center_hz=101_000_000)]
        + [_quiet("receiver-a", center_hz=101_000_000)]
        + [_quiet("receiver-a", center_hz=101_000_000)]
    )
    runtime = _runtime(tmp_path, script)

    mature = None
    for _ in range(9):
        mature = runtime.snapshot()
    assert mature is not None
    assert mature.signal_assessment is not None
    assert mature.signal_assessment.state == AssessmentState.BACKGROUND_ONLY

    runtime.update_settings(
        {"spectrum": {"center_frequency_hz": 101_000_000}}
    )
    retune_warmup = runtime.snapshot()
    assert retune_warmup.spectrum is None
    assert retune_warmup.signal_assessment is not None
    assert (
        retune_warmup.signal_assessment.reason_code
        == "SIGNAL.SETTINGS_CHANGED"
    )

    grid_changed = runtime.snapshot()
    assert grid_changed.signal_assessment is not None
    assert grid_changed.signal_assessment.state == AssessmentState.LEARNING_BACKGROUND

    source_b = runtime.snapshot()
    assert source_b.signal_assessment is not None
    assert source_b.signal_assessment.source_id == "receiver-b"
    assert source_b.signal_assessment.state == AssessmentState.LEARNING_BACKGROUND

    source_a_again = runtime.snapshot()
    assert source_a_again.signal_assessment is not None
    assert source_a_again.signal_assessment.source_id == "receiver-a"
    assert source_a_again.signal_assessment.state == AssessmentState.LEARNING_BACKGROUND
    assert source_a_again.signal_assessment.evidence.baseline_frames == 1

    runtime.update_settings({"ui": {"experience_level": "expert"}})
    after_settings = runtime.snapshot()
    assert after_settings.signal_assessment is not None
    assert after_settings.signal_assessment.state == AssessmentState.LEARNING_BACKGROUND
    assert after_settings.signal_assessment.evidence.baseline_frames == 2

    runtime.update_settings({"spectrum": {"threshold_level": -80.0}})
    after_display_line = runtime.snapshot()
    assert after_display_line.signal_assessment is not None
    assert after_display_line.signal_assessment.evidence.baseline_frames == 3
    runtime.shutdown()


def test_runtime_publishes_only_temporally_confirmed_rf_episode(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, [_quiet()] * 9 + [_activity()] * 4)

    for _ in range(9):
        warm = runtime.snapshot()
    assert warm.signal_events == ()

    first = runtime.snapshot()
    assert first.signal_decision is not None
    assert first.signal_decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert first.signal_events == ()

    runtime.snapshot()
    confirmed = runtime.snapshot()

    assert confirmed.signal_decision is not None
    assert confirmed.signal_decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert confirmed.signal_decision.alertable is True
    assert len(confirmed.signal_events) == 1
    assert (
        confirmed.signal_events[0].episode_id
        == confirmed.signal_decision.episode_id
    )
    episode_id = confirmed.signal_decision.episode_id
    assert runtime.journal is not None
    assert runtime.journal.list_rf_decisions()[0].episode_id == episode_id
    runtime.shutdown()

    reopened = _runtime(tmp_path, [None])
    restored = reopened.snapshot()
    assert len(restored.signal_events) == 1
    assert restored.signal_events[0].episode_id == episode_id
    reopened.shutdown()


def test_runtime_publishes_stale_accepted_frame_as_unreliable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, [_quiet()] * 8 + [_quiet(age_ms=9_000)])

    current = None
    for _ in range(9):
        current = runtime.snapshot()

    assert current is not None
    assert current.signal_assessment is not None
    assert current.signal_assessment.state == AssessmentState.DATA_UNRELIABLE
    assert current.signal_assessment.reason_code == "SIGNAL.DATA_STALE"
    assert current.signal_assessment.evidence.data_age_ms == 9_000
    runtime.shutdown()


def test_runtime_first_stale_frame_is_unreliable_not_learning(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, [_quiet(age_ms=9_000), _quiet()])

    stale = runtime.snapshot()

    assert stale.signal_assessment is not None
    assert stale.signal_assessment.state == AssessmentState.DATA_UNRELIABLE
    assert stale.signal_assessment.evidence.baseline_frames == 0

    first_clean = runtime.snapshot()
    assert first_clean.signal_assessment is not None
    assert first_clean.signal_assessment.state == AssessmentState.LEARNING_BACKGROUND
    assert first_clean.signal_assessment.evidence.baseline_frames == 1
    runtime.shutdown()
