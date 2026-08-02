from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    LoggingConfig,
    StorageConfig,
)
from alga_vector.devices import DeviceManager, FakeTinySAAdapter
from alga_vector.domain.enums import Capability, CapabilityState
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import (
    CapabilityStatus,
    DeviceSnapshot,
    SpectrumFrame,
)
from alga_vector.signal_analysis import AssessmentState, QualityFlag
from alga_vector.signal_processor import NormalizedEventType


class CountingSpectrumManager:
    def __init__(self, *, emit_frames: bool = True) -> None:
        self._inner = DeviceManager((FakeTinySAAdapter("background-radio"),))
        self._condition = threading.Condition()
        self._emit_frames = emit_frames
        self.read_count = 0
        self.refresh_count = 0
        self.read_threads: list[str] = []
        self.centers_hz: list[int] = []
        self.closed = False

    def refresh(self) -> tuple[DeviceSnapshot, ...]:
        with self._condition:
            self.refresh_count += 1
        return self._inner.refresh()

    def snapshots(self) -> tuple[DeviceSnapshot, ...]:
        return self._inner.snapshots()

    def resolve_capabilities(
        self,
        capabilities: Iterable[Capability] | None = None,
    ) -> tuple[CapabilityStatus, ...]:
        return self._inner.resolve_capabilities(capabilities)

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        with self._condition:
            self.read_count += 1
            self.read_threads.append(threading.current_thread().name)
            self.centers_hz.append(center_frequency_hz)
            emit = self._emit_frames
            self._condition.notify_all()
        if not emit:
            return None
        return self._inner.read_spectrum(
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            bins=bins,
        )

    def reconnect(self, device_id: str) -> DeviceSnapshot:
        return self._inner.reconnect(device_id)

    def close(self) -> None:
        self._inner.close()
        self.closed = True

    def set_emit_frames(self, enabled: bool) -> None:
        with self._condition:
            self._emit_frames = enabled

    def wait_for_reads(self, minimum: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self.read_count < minimum:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class DeferredSpectrumManager(CountingSpectrumManager):
    """Deterministic stand-in for the non-blocking hardware-process proxy."""

    def __init__(self, *, polls_per_frame: int = 4) -> None:
        super().__init__()
        self.polls_per_frame = polls_per_frame
        self.last_read_request_accepted = False
        self._pending_sequence: int | None = None
        self._remaining_polls = 0
        self.completed_sequences: list[int] = []

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        with self._condition:
            self.read_count += 1
            self.read_threads.append(threading.current_thread().name)
            self.centers_hz.append(center_frequency_hz)
            self.last_read_request_accepted = False
            if self._pending_sequence is None:
                self._pending_sequence = sequence
                self._remaining_polls = self.polls_per_frame
                self.last_read_request_accepted = True
                self._condition.notify_all()
                return None
            if self._remaining_polls > 1:
                self._remaining_polls -= 1
                self._condition.notify_all()
                return None

            completed_sequence = self._pending_sequence
            frame = self._inner.read_spectrum(
                sequence=completed_sequence,
                center_frequency_hz=center_frequency_hz,
                span_hz=span_hz,
                bins=bins,
            )
            self.completed_sequences.append(completed_sequence)
            # Match HardwareProcessDeviceManager: the poll that consumes one
            # completed frame also submits the caller's next sequence.
            self._pending_sequence = sequence
            self._remaining_polls = self.polls_per_frame
            self.last_read_request_accepted = True
            self._condition.notify_all()
            return frame

    def wait_for_completed(self, minimum: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.completed_sequences) < minimum:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class UnexpectedFailureManager(CountingSpectrumManager):
    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        del sequence, center_frequency_hz, span_hz, bins
        with self._condition:
            self.read_count += 1
            self._condition.notify_all()
        raise TypeError("deterministic unexpected acquisition failure")


class OneShotRfActivityManager(CountingSpectrumManager):
    """Return one strong frame after a learned quiet floor, then go quiet."""

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        frame = super().read_spectrum(
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            bins=bins,
        )
        if frame is None:
            return None
        power = np.full(frame.power_dbm.shape, -100.0, dtype=np.float32)
        if self.read_count == 10:
            middle = power.size // 2
            power[middle - 3 : middle + 4] = -70.0
        return replace(frame, power_dbm=power)


def _demo_config(tmp_path: Path, *, adapter_id: str = "configured-sim") -> AppConfig:
    return AppConfig(
        mode="demo",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / "runtime"),
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id=adapter_id,
                    kind="tinysa",
                    enabled=True,
                    connection="SIM:TINYSA",
                )
            ]
        ),
    )


def _wait_for_recorded_frames(
    runtime: ApplicationRuntime,
    minimum: int,
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.recording_status().frames >= minimum:
            return True
        time.sleep(0.01)
    return False


def test_background_acquisition_runs_without_snapshot_and_records_unique_frames(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.01,
        acquisition_refresh_seconds=0.03,
    )

    runtime.start()
    assert manager.wait_for_reads(4)
    assert runtime.latest_snapshot is None
    count_without_ui = manager.read_count
    assert manager.wait_for_reads(count_without_ui + 3)
    assert manager.refresh_count >= 2

    snapshot = runtime.snapshot()
    assert snapshot.spectrum is not None
    assert snapshot.signal_assessment is not None
    assert snapshot.signal_decision is not None
    assert snapshot.signal_events == ()
    assert set(manager.read_threads) == {"ALGA-VECTOR-acquisition"}

    runtime.start_recording()
    assert _wait_for_recorded_frames(runtime, 3)
    for _ in range(8):
        runtime.snapshot()
    completed = runtime.stop_recording()

    records = [
        json.loads(line)
        for line in completed.path.read_text(encoding="utf-8").splitlines()
    ]
    sequences = [
        int(record["sequence"])
        for record in records
        if record.get("type") == "frame"
    ]
    assert completed.frames >= 3
    assert len(sequences) == len(set(sequences))
    assert set(manager.read_threads) == {"ALGA-VECTOR-acquisition"}

    started = time.monotonic()
    runtime.shutdown()
    assert time.monotonic() - started < 1.0
    assert not runtime.acquisition_running
    assert manager.closed


def test_async_polling_allocates_sequences_only_to_accepted_capture_requests(
    tmp_path: Path,
) -> None:
    manager = DeferredSpectrumManager(polls_per_frame=5)
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.002,
        acquisition_refresh_seconds=10.0,
    )

    runtime.start()
    assert manager.wait_for_completed(6)
    snapshot = runtime.snapshot()

    assert manager.completed_sequences[:6] == [1, 2, 3, 4, 5, 6]
    assert snapshot.signal_assessment is not None
    assert QualityFlag.SEQUENCE_GAP not in snapshot.signal_assessment.quality_flags
    assert manager.read_count > len(manager.completed_sequences)
    runtime.shutdown()


def test_debug_log_proves_capture_request_and_consumed_live_frame(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    config = _demo_config(tmp_path).model_copy(
        update={"logging": LoggingConfig(level="DEBUG")}
    )
    runtime = ApplicationRuntime(
        config,
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.005,
    )

    runtime.start()
    assert manager.wait_for_reads(5)
    logger_path = runtime.logger_path
    runtime.shutdown()

    assert logger_path is not None
    records = [
        json.loads(line)
        for line in logger_path.read_text(encoding="utf-8").splitlines()
    ]
    requested = next(
        record
        for record in records
        if record.get("event") == "acquisition.capture_requested"
    )
    captured = next(
        record
        for record in records
        if record.get("event") == "acquisition.frame_captured"
    )
    request_context = requested["context"]
    capture_context = captured["context"]
    assert request_context["center_frequency_hz"] == 433_920_000
    assert request_context["configured_sample_rate_hz"] == 2_400_000
    assert request_context["submitted_requests"] >= 1
    assert capture_context["source_id"] == "background-radio"
    assert capture_context["provenance"] == "simulated"
    assert capture_context["received_frames"] >= 1
    assert capture_context["consumed_frames"] >= 1
    assert capture_context["bins"] == 512


def test_short_rf_activity_reaches_event_bus_without_ui_snapshot(
    tmp_path: Path,
) -> None:
    manager = OneShotRfActivityManager()
    config = _demo_config(tmp_path).model_copy(
        update={"logging": LoggingConfig(level="DEBUG")}
    )
    runtime = ApplicationRuntime(
        config,
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.005,
    )

    runtime.start()
    assert manager.wait_for_reads(14)
    assert runtime.latest_snapshot is None

    events = runtime.operator_event_bus.recent(limit=64)
    activity = next(
        event
        for event in events
        if event.event_type is NormalizedEventType.RADIO_ACTIVITY_DETECTED
    )
    assert activity.identity is None
    assert "generic-activity" in activity.tags

    logger_path = runtime.logger_path
    runtime.shutdown()

    assert logger_path is not None
    records = [
        json.loads(line)
        for line in logger_path.read_text(encoding="utf-8").splitlines()
    ]
    names = {record.get("event") for record in records}
    assert "signal_analysis.activity_observed" in names
    assert "signal_processor.event_published" in names


def test_unexpected_acquisition_exception_is_visible_and_logged(
    tmp_path: Path,
) -> None:
    manager = UnexpectedFailureManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.002,
    )

    runtime.start()
    assert manager.wait_for_reads(1)
    deadline = time.monotonic() + 1.0
    while runtime.acquisition_running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not runtime.acquisition_running

    snapshot = runtime.snapshot()
    assert any(
        incident.code == "SPECTRUM.ACQUISITION_INTERNAL_ERROR"
        for incident in snapshot.incidents
    )
    assert snapshot.signal_assessment is not None
    assert (
        snapshot.signal_assessment.reason_code
        == "SPECTRUM.ACQUISITION_INTERNAL_ERROR"
    )
    logger_path = runtime.logger_path
    runtime.shutdown()

    assert logger_path is not None
    records = [
        json.loads(line)
        for line in logger_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record.get("event") == "acquisition.unhandled_exception"
        and record.get("context", {}).get("error_type") == "TypeError"
        for record in records
    )


def test_custom_manager_and_demo_remain_synchronous_without_explicit_enable(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
    )

    runtime.start()
    time.sleep(0.08)
    assert not runtime.background_acquisition_enabled
    assert not runtime.acquisition_running
    assert manager.read_count == 0

    snapshot = runtime.snapshot()
    assert snapshot.spectrum is not None
    assert manager.read_count == 1
    assert manager.read_threads == ["MainThread"]
    runtime.shutdown()


def test_stale_live_frame_blocks_readiness_but_preserves_provenance_and_age(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.01,
        acquisition_stale_seconds=0.05,
    )
    runtime.start()
    assert manager.wait_for_reads(3)
    fresh = runtime.snapshot()
    assert fresh.spectrum is not None
    assert fresh.readiness_percent == 100

    manager.set_emit_frames(False)
    last_sequence = fresh.spectrum.sequence
    last_provenance = fresh.spectrum.provenance
    time.sleep(0.08)
    stale = runtime.snapshot()
    spectrum_state = {
        status.capability: status.state for status in stale.capabilities
    }[Capability.SPECTRUM_SWEEP]

    assert stale.spectrum is not None
    assert stale.spectrum.sequence == last_sequence
    assert stale.spectrum.provenance == last_provenance
    assert stale.spectrum.data_age_ms >= 50
    assert stale.signal_assessment is not None
    assert stale.signal_assessment.state == AssessmentState.DATA_UNRELIABLE
    assert stale.signal_assessment.evidence.data_age_ms >= 50
    assert QualityFlag.DATA_STALE in stale.signal_assessment.quality_flags
    assert spectrum_state == CapabilityState.BLOCKED
    assert stale.readiness_percent == 0
    assert any(
        incident.code == "SPECTRUM.STALE_FRAME"
        for incident in stale.incidents
    )
    with pytest.raises(AppError, match=r"CAPTURE\.NO_SPECTRUM"):
        runtime.start_recording()
    runtime.shutdown()


def test_spectrum_settings_pause_and_restart_background_loop(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.01,
    )
    runtime.start()
    assert manager.wait_for_reads(3)

    runtime.update_settings(
        {"spectrum": {"center_frequency_hz": 434_100_000}}
    )
    reads_after_commit = manager.read_count
    assert runtime.acquisition_running
    assert manager.wait_for_reads(reads_after_commit + 3)
    assert set(manager.centers_hz[reads_after_commit:]) == {434_100_000}
    runtime.shutdown()


def test_device_replacement_stops_old_loop_and_uses_new_manager(
    tmp_path: Path,
) -> None:
    manager = CountingSpectrumManager()
    runtime = ApplicationRuntime(
        _demo_config(tmp_path),
        device_manager=manager,
        background_acquisition=True,
        acquisition_period_seconds=0.01,
    )
    runtime.start()
    assert manager.wait_for_reads(3)

    runtime.update_settings(
        {
            "devices": {
                "adapters": [
                    {
                        "id": "replacement-sim",
                        "kind": "rtlsdr",
                        "enabled": True,
                        "connection": "SIM:RTLSDR",
                    }
                ]
            }
        }
    )
    old_count = manager.read_count
    assert manager.closed
    assert runtime.acquisition_running

    deadline = time.monotonic() + 1.0
    snapshot = runtime.snapshot()
    while (
        (snapshot.spectrum is None or snapshot.spectrum.source_id != "replacement-sim")
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
        snapshot = runtime.snapshot()
    time.sleep(0.04)

    assert snapshot.spectrum is not None
    assert snapshot.spectrum.source_id == "replacement-sim"
    assert manager.read_count == old_count
    runtime.shutdown()
