from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from alga_vector.application import ApplicationRuntime, RfScanSession
from alga_vector.application.rf_scan import FrequencyScopedRfPipelinePool
from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    SpectrumConfig,
    StorageConfig,
)
from alga_vector.devices import (
    GENERIC_RTLSDR_PROFILE,
    CompiledScanPlan,
    DeviceAdapter,
    DeviceManager,
    FakeRTLSDRAdapter,
    FakeTinySAAdapter,
    ScanPlanRequest,
    ScanRange,
    compile_scan_plan,
)
from alga_vector.domain.enums import (
    Capability,
    DeviceState,
    HealthLevel,
    Provenance,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame
from alga_vector.signal_analysis import (
    DetectorConfig,
    QualityFlag,
    SourceObservationMetadata,
    SpectrumAcquisitionMode,
)

START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
_HACKRF_CONNECTION = "HACKRF:0000000000000001"


class RecordingDeviceManager(DeviceManager):
    """Synchronous manager that records every requested tuning."""

    def __init__(self, adapter: DeviceAdapter) -> None:
        super().__init__((adapter,))
        self.requested_centers_hz: list[int] = []
        self.requested_spans_hz: list[int] = []

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        self.requested_centers_hz.append(center_frequency_hz)
        self.requested_spans_hz.append(span_hz)
        return super().read_spectrum(
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            bins=bins,
        )


class DeferredDeviceManager(DeviceManager):
    """Model the isolated manager's poll-complete-and-submit contract."""

    def __init__(
        self,
        adapter: DeviceAdapter,
        *,
        corrupt_completion_number: int | None = None,
        completion_delay_polls: int = 0,
    ) -> None:
        super().__init__((adapter,))
        if completion_delay_polls < 0:
            raise ValueError("completion_delay_polls must be non-negative")
        self._pending_request: tuple[int, int, int, int] | None = None
        self._accepted = False
        self._completed = 0
        self._corrupt_completion_number = corrupt_completion_number
        self._completion_delay_polls = completion_delay_polls
        self._remaining_delay_polls = 0
        self.requested_centers_hz: list[int] = []

    @property
    def last_read_request_accepted(self) -> bool:
        return self._accepted

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        self._accepted = False
        completed: SpectrumFrame | None = None
        if self._pending_request is not None and self._remaining_delay_polls:
            self._remaining_delay_polls -= 1
            return None
        if self._pending_request is not None:
            pending_sequence, pending_center, pending_span, pending_bins = (
                self._pending_request
            )
            completed = super().read_spectrum(
                sequence=pending_sequence,
                center_frequency_hz=pending_center,
                span_hz=pending_span,
                bins=pending_bins,
            )
            self._completed += 1
            if (
                completed is not None
                and self._completed == self._corrupt_completion_number
            ):
                completed = replace(
                    completed,
                    center_frequency_hz=(
                        completed.center_frequency_hz + 1
                    ),
                )
            self._pending_request = None
        if self._pending_request is None:
            self._pending_request = (
                sequence,
                center_frequency_hz,
                span_hz,
                bins,
            )
            self.requested_centers_hz.append(center_frequency_hz)
            self._remaining_delay_polls = self._completion_delay_polls
            self._accepted = True
        return completed


class TestHackRfAdapter(DeviceAdapter):
    """Receive-only deterministic adapter with HackRF capability metadata."""

    __test__ = False

    def __init__(self, *, clock: datetime = START) -> None:
        super().__init__(
            adapter_id="test-hackrf",
            display_name="Test HackRF",
            kind="hackrf",
            connection=_HACKRF_CONNECTION,
            capabilities=frozenset(
                {Capability.SPECTRUM_SWEEP, Capability.IQ_RX}
            ),
            clock=lambda: clock,
        )

    def inspect(self) -> DeviceSnapshot:
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            sample_rate_hz=20_000_000,
            last_data_at=self._clock(),
            generation=1,
        )

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=np.full(bins, -100.0, dtype=np.float32),
            captured_at=self._clock() + timedelta(milliseconds=sequence),
            provenance=Provenance.LIVE,
            unit="dBm",
        )


class ScanSignalTinySaAdapter(DeviceAdapter):
    """Build baseline, then emit a persistent generic narrow RF component."""

    def __init__(self) -> None:
        super().__init__(
            adapter_id="test-tinysa",
            display_name="Scan signal tinySA",
            kind="tinysa",
            connection="SIM:TINYSA",
            capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
            clock=lambda: START,
        )
        self._visits_by_center: dict[int, int] = {}

    def inspect(self) -> DeviceSnapshot:
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            sample_rate_hz=2_400_000,
            last_data_at=self._clock(),
            generation=1,
        )

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        visit = self._visits_by_center.get(center_frequency_hz, 0) + 1
        self._visits_by_center[center_frequency_hz] = visit
        power = np.full(bins, -100.0, dtype=np.float32)
        if visit > 8:
            middle = bins // 2
            power[middle - 2 : middle + 2] = -65.0
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=START + timedelta(milliseconds=sequence * 100),
            provenance=Provenance.SIMULATED,
            unit="dBm",
            calibration_id="test:relative",
        )


class TransientOnRetuneTinySaAdapter(FakeTinySAAdapter):
    """Return one correct-grid transient immediately after every retune."""

    def __init__(self) -> None:
        super().__init__(
            "test-tinysa",
            connection="SIM:TINYSA",
            clock=lambda: START,
        )
        self._last_center_hz: int | None = None
        self.transient_frames = 0

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        power = np.full(bins, -100.0, dtype=np.float32)
        if center_frequency_hz != self._last_center_hz:
            power[bins // 2 - 2 : bins // 2 + 2] = -35.0
            self.transient_frames += 1
        self._last_center_hz = center_frequency_hz
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=START + timedelta(milliseconds=sequence * 100),
            provenance=Provenance.SIMULATED,
            unit="dBm",
            calibration_id="test:retune-transient",
        )


class MislabeledTinySaAdapter(FakeTinySAAdapter):
    """Return a frame from an unexpected fallback source."""

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        frame = super().read_spectrum(
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            bins=bins,
        )
        return replace(frame, source_id="unexpected-fallback")


def _tinysa_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        mode="demo",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / "runtime"),
        spectrum=SpectrumConfig(
            center_frequency_hz=100_000_000,
            span_hz=2_000_000,
            sample_rate_hz=2_400_000,
        ),
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id="test-tinysa",
                    kind="tinysa",
                    enabled=True,
                    connection="SIM:TINYSA",
                    tinysa_model="basic",
                )
            ]
        ),
    )


def _hackrf_config(tmp_path: Path, *, sample_rate_hz: int) -> AppConfig:
    return AppConfig(
        mode="live",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / f"hackrf-{sample_rate_hz}"),
        spectrum=SpectrumConfig(
            center_frequency_hz=100_000_000,
            span_hz=sample_rate_hz,
            sample_rate_hz=sample_rate_hz,
        ),
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="test-hackrf",
                    kind="hackrf",
                    enabled=True,
                    connection=_HACKRF_CONNECTION,
                )
            ],
        ),
    )


def _rtlsdr_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        mode="live",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / "rtlsdr-field"),
        spectrum=SpectrumConfig(
            center_frequency_hz=433_920_000,
            span_hz=2_000_000,
            sample_rate_hz=2_400_000,
        ),
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="test-rtlsdr",
                    kind="rtlsdr",
                    enabled=True,
                    connection="RTLSDR:0",
                    rtlsdr_profile="generic",
                )
            ],
        ),
    )


def _tinysa_adapter() -> FakeTinySAAdapter:
    return FakeTinySAAdapter(
        "test-tinysa",
        connection="SIM:TINYSA",
        clock=lambda: START,
    )


def _two_window_plan() -> CompiledScanPlan:
    request = ScanPlanRequest(
        plan_id="runtime_pool_test",
        ranges=(
            ScanRange(
                "runtime_pool_band",
                "Тестовый участок",
                100_000_000,
                102_000_000,
            ),
        ),
        window_span_hz=1_000_000,
        overlap_fraction=0.0,
        dwell_frames=3,
    )
    return compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )


def _frame(
    *,
    sequence: int,
    center_frequency_hz: int,
    span_hz: int,
    offset_ms: int,
    active: bool = False,
) -> SpectrumFrame:
    power = np.full(64, -100.0, dtype=np.float32)
    if active:
        power[30:34] = -65.0
    return SpectrumFrame(
        source_id="shared-receiver",
        sequence=sequence,
        center_frequency_hz=center_frequency_hz,
        span_hz=span_hz,
        power_dbm=power,
        captured_at=START + timedelta(milliseconds=offset_ms),
        provenance=Provenance.LIVE,
        unit="dBm",
    )


def test_runtime_scan_holds_dwell_then_restores_fixed_tuning(
    tmp_path: Path,
) -> None:
    manager = RecordingDeviceManager(_tinysa_adapter())
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )

    started = runtime.start_scan_plan("general_vhf")
    first_center_hz = started.center_frequency_hz

    assert runtime.background_acquisition_enabled is False
    assert runtime.acquisition_running is False
    assert started.sequential is True
    assert started.source_id == "test-tinysa"
    assert started.window_count > 1
    assert {
        "SCAN_PLAN.FREQUENCY_IS_NOT_SOURCE_IDENTITY",
        "SCAN_PLAN.SEQUENTIAL_SWEEP_MAY_MISS_SHORT_EVENTS",
    }.issubset(started.limitation_codes)

    warmup = runtime.snapshot(bins=64)
    assert warmup.spectrum is None
    assert runtime.scan_plan_status() is not None
    assert runtime.scan_plan_status().successful_frames_in_window == 0

    for successful_frame in range(1, started.dwell_frames + 1):
        snapshot = runtime.snapshot(bins=64)
        assert snapshot.spectrum is not None
        assert snapshot.signal_assessment is not None
        assert snapshot.signal_assessment.identity_established is False
        status = runtime.scan_plan_status()
        assert status is not None
        if successful_frame < started.dwell_frames:
            assert status.current_ordinal == 0
            assert status.successful_frames_in_window == successful_frame
        else:
            assert status.current_ordinal == 1
            assert status.successful_frames_in_window == 0
            assert status.observed_ordinal == 0
            assert status.transition_pending is True
            assert snapshot.spectrum.center_frequency_hz == first_center_hz

    assert manager.requested_centers_hz == [
        first_center_hz
    ] * (started.dwell_frames + 1)

    next_warmup = runtime.snapshot(bins=64)
    assert manager.requested_centers_hz[-1] != first_center_hz
    assert next_warmup.spectrum is None
    next_snapshot = runtime.snapshot(bins=64)
    settled_status = runtime.scan_plan_status()
    assert settled_status is not None
    assert settled_status.current_ordinal == 1
    assert settled_status.observed_ordinal == 1
    assert settled_status.transition_pending is False
    assert next_snapshot.spectrum is not None
    assert (
        next_snapshot.spectrum.center_frequency_hz
        == settled_status.center_frequency_hz
    )
    assert next_snapshot.signal_assessment is not None
    assert (
        next_snapshot.signal_assessment.sequence
        == next_snapshot.spectrum.sequence
    )

    result = runtime.stop_scan_plan()
    fixed_warmup = runtime.snapshot(bins=64)
    fixed_snapshot = runtime.snapshot(bins=64)

    assert "фиксированное окно восстановлено" in result
    assert runtime.scan_plan_status() is None
    assert fixed_warmup.spectrum is None
    assert fixed_snapshot.spectrum is not None
    assert fixed_snapshot.spectrum.center_frequency_hz == 100_000_000
    assert manager.closed is False
    runtime.shutdown()


def test_runtime_scan_rejects_missing_receiver_unknown_preset_and_range(
    tmp_path: Path,
) -> None:
    no_receiver = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=DeviceManager(()),
        clock=lambda: START,
    )

    with pytest.raises(AppError) as missing:
        no_receiver.start_scan_plan("general_vhf")
    assert missing.value.code == "SCAN_PLAN.NO_OPERABLE_RECEIVER"
    assert no_receiver.scan_plan_status() is None
    no_receiver.shutdown()

    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=RecordingDeviceManager(_tinysa_adapter()),
        clock=lambda: START,
    )
    with pytest.raises(AppError) as unknown:
        runtime.start_scan_plan("not_a_preset")
    assert unknown.value.code == "SCAN_PLAN.PRESET_UNKNOWN"

    with pytest.raises(AppError) as unsupported:
        runtime.start_scan_plan("general_c_band")
    assert unsupported.value.code == "SCAN_PLAN.NO_SUPPORTED_COVERAGE"
    assert runtime.scan_plan_status() is None
    runtime.shutdown()


def test_runtime_scan_rejects_fallback_source_instead_of_mixing_profiles(
    tmp_path: Path,
) -> None:
    manager = RecordingDeviceManager(
        MislabeledTinySaAdapter(
            "test-tinysa",
            connection="SIM:TINYSA",
            clock=lambda: START,
        )
    )
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    runtime.start_scan_plan("general_vhf")

    snapshot = runtime.snapshot(bins=64)
    status = runtime.scan_plan_status()

    assert snapshot.spectrum is None
    assert status is not None
    assert status.source_id == "test-tinysa"
    assert status.successful_frames_in_window == 0
    assert status.failed_windows == 1
    assert snapshot.signal_assessment is not None
    assert snapshot.signal_assessment.state.value == "no_data"
    assert snapshot.signal_assessment.reason_code == "SCAN_PLAN.SOURCE_MISMATCH"
    assert any(
        incident.code == "SCAN_PLAN.SOURCE_MISMATCH"
        for incident in snapshot.incidents
    )
    runtime.shutdown()


def test_ui_only_settings_keep_forced_scan_background_thread(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        clock=lambda: START,
    )
    runtime.start_scan_plan("general_vhf")
    acquisition_thread = runtime._acquisition_thread

    assert runtime.background_acquisition_enabled is True
    assert acquisition_thread is not None and acquisition_thread.is_alive()

    runtime.update_settings({"ui": {"experience_level": "expert"}})

    assert runtime.scan_plan_status() is not None
    assert runtime.background_acquisition_enabled is True
    assert runtime._acquisition_thread is acquisition_thread
    assert acquisition_thread.is_alive()
    runtime.stop_scan_plan()
    assert runtime.background_acquisition_enabled is False
    runtime.shutdown()


def test_runtime_resource_limit_allows_s_and_c_but_blocks_wide_at_2_4_mhz(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _hackrf_config(tmp_path, sample_rate_hz=2_400_000),
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        clock=lambda: START,
    )

    s_band = runtime.start_scan_plan("general_s_band")
    assert 512 < s_band.window_count <= 1_024
    runtime.stop_scan_plan()

    c_band = runtime.start_scan_plan("general_c_band")
    assert 512 < c_band.window_count <= 1_024
    runtime.stop_scan_plan()

    for preset_id in ("general_wide", "full_supported"):
        with pytest.raises(AppError) as blocked:
            runtime.start_scan_plan(preset_id)
        assert blocked.value.code == "SCAN_PLAN.TOO_MANY_WINDOWS"
        assert runtime.scan_plan_status() is None
    runtime.shutdown()


def test_runtime_wide_and_full_plans_fit_hackrf_at_20_mhz(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _hackrf_config(tmp_path, sample_rate_hz=20_000_000),
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        clock=lambda: START,
    )

    wide = runtime.start_scan_plan("general_wide")
    assert wide.coverage_fraction == pytest.approx(1.0)
    assert wide.window_count < 1_024
    runtime.stop_scan_plan()

    full = runtime.start_scan_plan("full_supported")
    assert full.coverage_fraction == pytest.approx(1.0)
    assert full.window_count < 1_024
    runtime.shutdown()


def test_first_live_start_uses_capability_clipped_field_priority_on_rtlsdr(
    tmp_path: Path,
) -> None:
    config = _rtlsdr_config(tmp_path)
    adapter = FakeRTLSDRAdapter(
        "test-rtlsdr",
        connection="RTLSDR:0",
        clock=lambda: START,
    )
    runtime = ApplicationRuntime(
        config,
        device_manager=RecordingDeviceManager(adapter),
        background_acquisition=False,
        clock=lambda: START,
    )

    runtime.start()
    status = runtime.scan_plan_status()
    session = runtime._rf_scan_session

    assert status is not None
    assert status.plan_id == "preset_field_priority"
    assert status.window_count <= 128
    assert status.estimated_cycle_ms <= 120_000
    assert session is not None
    assert all(
        window.stop_frequency_hz <= 1_766_000_000
        for window in session.plan.windows
    )
    assert {item.requested.range_id for item in session.plan.excluded_ranges} == {
        "field_2400",
        "field_5800",
    }
    runtime.shutdown()


def test_first_live_start_includes_high_field_ranges_on_hackrf(
    tmp_path: Path,
) -> None:
    config = _hackrf_config(tmp_path, sample_rate_hz=2_000_000)
    runtime = ApplicationRuntime(
        config,
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        background_acquisition=False,
        clock=lambda: START,
    )

    runtime.start()
    status = runtime.scan_plan_status()
    session = runtime._rf_scan_session

    assert status is not None
    assert status.plan_id == "preset_field_priority"
    assert status.window_count <= 128
    assert status.estimated_cycle_ms <= 120_000
    assert session is not None
    assert not session.plan.excluded_ranges
    covered_ids = {item.range_id for item in session.plan.covered_ranges}
    assert {"field_2400", "field_5800"}.issubset(covered_ids)
    assert any(
        window.center_frequency_hz >= 5_725_000_000
        for window in session.plan.windows
    )
    runtime.shutdown()


def test_live_start_resumes_only_last_explicit_bounded_scan_plan(
    tmp_path: Path,
) -> None:
    config = _hackrf_config(tmp_path, sample_rate_hz=20_000_000)
    first = ApplicationRuntime(
        config,
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        background_acquisition=False,
        clock=lambda: START,
    )
    started = first.start_scan_plan("general_vhf")
    resume_path = config.storage.data_dir / "state" / "active-scan-preset.txt"

    assert started.window_count <= 128
    assert resume_path.read_text(encoding="utf-8").strip() == "general_vhf"
    first.shutdown()

    second = ApplicationRuntime(
        config,
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        background_acquisition=False,
        clock=lambda: START,
    )
    second.start()
    resumed = second.scan_plan_status()

    assert resumed is not None
    assert resumed.plan_id == "preset_general_vhf"
    assert resumed.profile_id == "hackrf_one_rx"
    second.stop_scan_plan()
    assert not resume_path.exists()
    second.shutdown()


def test_very_wide_scan_is_never_persisted_for_startup_resume(
    tmp_path: Path,
) -> None:
    config = _hackrf_config(tmp_path, sample_rate_hz=2_400_000)
    runtime = ApplicationRuntime(
        config,
        device_manager=RecordingDeviceManager(TestHackRfAdapter()),
        background_acquisition=False,
        clock=lambda: START,
    )

    started = runtime.start_scan_plan("general_s_band")
    resume_path = config.storage.data_dir / "state" / "active-scan-preset.txt"

    assert started.window_count > 128
    assert not resume_path.exists()
    runtime.shutdown()


def test_deferred_manager_submits_next_window_without_stale_retune(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(_tinysa_adapter())
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    started = runtime.start_scan_plan("general_vhf")
    first_center_hz = started.center_frequency_hz

    # First poll submits a request, the first completion is warm-up, and only
    # the following twelve frames advance the dwell.
    for _ in range(started.dwell_frames + 2):
        runtime.snapshot(bins=64)

    advanced = runtime.scan_plan_status()
    assert advanced is not None
    assert advanced.current_ordinal == 1
    assert advanced.failed_windows == 0
    assert manager.requested_centers_hz[: started.dwell_frames + 1] == [
        first_center_hz
    ] * (started.dwell_frames + 1)
    assert manager.requested_centers_hz[started.dwell_frames + 1] != first_center_hz

    runtime.snapshot(bins=64)
    runtime.snapshot(bins=64)
    settled = runtime.scan_plan_status()
    assert settled is not None
    assert settled.current_ordinal == 1
    assert settled.successful_frames_in_window == 1
    assert settled.failed_windows == 0
    runtime.shutdown()


def test_stop_scan_discards_pending_scan_completion_before_fixed_mode(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(_tinysa_adapter())
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    runtime.start_scan_plan("general_vhf")
    assert runtime.snapshot(bins=64).spectrum is None

    runtime.stop_scan_plan()
    stale = runtime.snapshot(bins=64)
    warmup = runtime.snapshot(bins=64)
    fixed = runtime.snapshot(bins=64)

    assert stale.scan_plan is None
    assert stale.spectrum is None
    assert warmup.spectrum is None
    assert fixed.spectrum is not None
    assert fixed.spectrum.center_frequency_hz == 100_000_000
    runtime.shutdown()


def test_start_scan_discards_pending_fixed_completion_without_failure(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(_tinysa_adapter())
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    assert runtime.snapshot(bins=64).spectrum is None

    runtime.start_scan_plan("general_vhf")
    stale = runtime.snapshot(bins=64)
    after_stale = runtime.scan_plan_status()
    warmup = runtime.snapshot(bins=64)
    measured = runtime.snapshot(bins=64)
    final_status = runtime.scan_plan_status()

    assert stale.spectrum is None
    assert after_stale is not None
    assert after_stale.failed_windows == 0
    assert warmup.spectrum is None
    assert measured.spectrum is not None
    assert final_status is not None
    assert final_status.successful_frames_in_window == 1
    assert final_status.failed_windows == 0
    runtime.shutdown()


def test_only_one_previous_mode_completion_is_silently_discarded(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(
        _tinysa_adapter(),
        corrupt_completion_number=2,
    )
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    runtime.snapshot(bins=64)
    runtime.start_scan_plan("general_vhf")

    runtime.snapshot(bins=64)
    runtime.snapshot(bins=64)
    status = runtime.scan_plan_status()
    snapshot = runtime.current_snapshot()

    assert status is not None
    assert status.failed_windows == 1
    assert any(
        incident.code == "SCAN_PLAN.FRAME_TUNING_MISMATCH"
        for incident in snapshot.incidents
    )
    runtime.shutdown()


def test_first_frame_after_retune_is_warmup_not_temporal_evidence(
    tmp_path: Path,
) -> None:
    adapter = TransientOnRetuneTinySaAdapter()
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=RecordingDeviceManager(adapter),
        clock=lambda: START,
    )
    runtime.start_scan_plan("general_vhf")

    warmup = runtime.snapshot(bins=64)
    warmup_status = runtime.scan_plan_status()
    measured = runtime.snapshot(bins=64)
    measured_status = runtime.scan_plan_status()

    assert adapter.transient_frames == 1
    assert warmup.spectrum is None
    assert warmup.signal_decision is None
    assert warmup_status is not None
    assert warmup_status.successful_frames_in_window == 0
    assert measured.spectrum is not None
    assert measured_status is not None
    assert measured_status.successful_frames_in_window == 1
    runtime.shutdown()


def test_deferred_runtime_recovers_after_boundary_frame_rejection(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(
        _tinysa_adapter(),
        corrupt_completion_number=13,
    )
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    started = runtime.start_scan_plan("general_vhf")

    # Submit one frame, collect eleven valid dwell frames, reject the twelfth,
    # discard one speculative next-window frame, then recover on current.
    for _ in range(started.dwell_frames + 8):
        runtime.snapshot(bins=64)

    recovered = runtime.scan_plan_status()
    assert recovered is not None
    assert recovered.current_ordinal == 1
    assert recovered.completed_windows == 1
    assert recovered.successful_frames_in_window == 1
    assert recovered.failed_windows == 2
    runtime.shutdown()


def test_slow_deferred_runtime_keeps_recovery_tuning_until_accepted(
    tmp_path: Path,
) -> None:
    manager = DeferredDeviceManager(
        _tinysa_adapter(),
        corrupt_completion_number=13,
        completion_delay_polls=2,
    )
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=manager,
        clock=lambda: START,
    )
    runtime.start_scan_plan("general_vhf")

    for _ in range(80):
        runtime.snapshot(bins=64)
        current = runtime.scan_plan_status()
        if (
            current is not None
            and current.current_ordinal == 1
            and current.successful_frames_in_window >= 1
        ):
            break

    recovered = runtime.scan_plan_status()
    assert recovered is not None
    assert recovered.current_ordinal == 1
    assert recovered.completed_windows == 1
    assert recovered.successful_frames_in_window >= 1
    assert recovered.failed_windows == 2
    runtime.shutdown()


def test_runtime_scan_feeds_temporal_detection_without_identity_claim(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        _tinysa_config(tmp_path),
        device_manager=RecordingDeviceManager(
            ScanSignalTinySaAdapter()
        ),
        clock=lambda: START,
    )
    started = runtime.start_scan_plan("general_vhf")

    latest = None
    for _ in range(started.dwell_frames):
        latest = runtime.snapshot(bins=64)

    assert latest is not None
    decision = latest.signal_decision
    assert decision is not None
    assert decision.alertable is True
    assert decision.identity_established is False
    assert decision.attribution.value == "not_available"
    assert decision.calibrated_probability is None
    assert decision.peak_frequency_hz is not None
    assert decision.supporting_evidence
    assert latest.signal_assessment is not None
    assert latest.signal_assessment.identity_established is False
    runtime.shutdown()


def test_frequency_scoped_pipelines_isolate_sequence_and_baseline() -> None:
    plan = _two_window_plan()
    assert plan.accepted
    windows = plan.windows
    pool = FrequencyScopedRfPipelinePool(maximum_pipelines=2)
    metadata = SourceObservationMetadata(
        acquisition_mode=SpectrumAcquisitionMode.SIMULTANEOUS_FFT,
        receiver_model="test receiver",
    )

    first_a, _ = pool.process(
        windows[0],
        _frame(
            sequence=1,
            center_frequency_hz=windows[0].center_frequency_hz,
            span_hz=windows[0].span_hz,
            offset_ms=100,
        ),
        metadata,
    )
    first_b, _ = pool.process(
        windows[1],
        _frame(
            sequence=100,
            center_frequency_hz=windows[1].center_frequency_hz,
            span_hz=windows[1].span_hz,
            offset_ms=200,
        ),
        metadata,
    )
    second_a, _ = pool.process(
        windows[0],
        _frame(
            sequence=200,
            center_frequency_hz=windows[0].center_frequency_hz,
            span_hz=windows[0].span_hz,
            offset_ms=300,
        ),
        metadata,
    )

    assert pool.pipeline_count == 2
    assert first_a.sequence == 1
    assert first_b.sequence == 100
    assert second_a.sequence == 200
    assert first_b.assessment.sequence == 100
    assert second_a.assessment.sequence == 200
    assert first_b.history_frames == 1
    assert second_a.history_frames == 2
    assert QualityFlag.SEQUENCE_GAP not in second_a.quality_flags
    assert QualityFlag.SPECTRAL_GRID_CHANGED not in second_a.quality_flags


def test_scan_session_uses_injected_clock_for_cursor_results() -> None:
    plan = _two_window_plan()
    session = RfScanSession(
        plan,
        source_id="shared-receiver",
        clock=lambda: START,
    )

    session.next_window()
    session.mark_result(False, detail_code="SPECTRUM.TEST_FAILURE")

    last_result = session.cursor.snapshot().last_result
    assert last_result is not None
    assert last_result.recorded_at == START


def test_deferred_lookahead_failure_forces_current_window_recovery() -> None:
    plan = _two_window_plan()
    session = RfScanSession(
        plan,
        source_id="shared-receiver",
        clock=lambda: START,
    )
    first = session.next_window()

    session.mark_result(True)
    session.next_window()
    session.mark_result(True)

    speculative = session.request_window(
        anticipate_deferred_completion=True
    )
    assert speculative.window_id != first.window_id

    # The completed final frame was rejected. The speculative next-window
    # request may already be pending, so one current request is mandatory.
    session.mark_result(False, detail_code="SPECTRUM.FRAME_REJECTED")
    recovery = session.request_window(
        anticipate_deferred_completion=True
    )
    assert recovery.window_id == first.window_id
    session.mark_request_accepted(False)
    still_recovering = session.request_window(
        anticipate_deferred_completion=True
    )
    assert still_recovering.window_id == first.window_id
    session.mark_request_accepted(True)

    # Discard the already-pending speculative frame, then the scheduler may
    # look ahead again while the recovery frame completes.
    session.mark_result(
        False,
        detail_code="SCAN_PLAN.FRAME_TUNING_MISMATCH",
    )
    resumed = session.request_window(
        anticipate_deferred_completion=True
    )
    assert resumed.window_id == speculative.window_id
    session.mark_result(True)

    status = session.status()
    assert status.current_ordinal == 1
    assert status.completed_windows == 1
    assert status.failed_windows == 2


def test_frequency_scoped_pool_does_not_reuse_stale_alert() -> None:
    plan = _two_window_plan()
    windows = plan.windows
    pool = FrequencyScopedRfPipelinePool(
        maximum_pipelines=2,
        detector_config=DetectorConfig(
            history_frames=8,
            min_history_frames=3,
        ),
    )
    metadata = SourceObservationMetadata(
        acquisition_mode=SpectrumAcquisitionMode.SIMULTANEOUS_FFT,
        receiver_model="test receiver",
    )

    offset_ms = 0
    for sequence in range(1, 4):
        pool.process(
            windows[0],
            _frame(
                sequence=sequence,
                center_frequency_hz=windows[0].center_frequency_hz,
                span_hz=windows[0].span_hz,
                offset_ms=offset_ms,
            ),
            metadata,
        )
        offset_ms += 100
    for sequence in range(4, 9):
        pool.process(
            windows[0],
            _frame(
                sequence=sequence,
                center_frequency_hz=windows[0].center_frequency_hz,
                span_hz=windows[0].span_hz,
                offset_ms=offset_ms,
                active=True,
            ),
            metadata,
        )
        offset_ms += 100

    fresh = pool.latest_alertable_decision(
        now=START + timedelta(milliseconds=offset_ms),
    )
    assert fresh is not None
    assert fresh.alertable is True

    pool.process(
        windows[1],
        _frame(
            sequence=100,
            center_frequency_hz=windows[1].center_frequency_hz,
            span_hz=windows[1].span_hz,
            offset_ms=10_000,
        ),
        metadata,
    )

    assert (
        pool.latest_alertable_decision(
            now=START + timedelta(milliseconds=10_000),
        )
        is None
    )

    _, revisited = pool.process(
        windows[0],
        _frame(
            sequence=101,
            center_frequency_hz=windows[0].center_frequency_hz,
            span_hz=windows[0].span_hz,
            offset_ms=10_000,
            active=True,
        ),
        metadata,
    )
    assert revisited.decision.alertable is False
