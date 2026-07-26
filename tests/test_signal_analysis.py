from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from alga_vector.domain.enums import Provenance
from alga_vector.domain.models import SpectrumFrame
from alga_vector.signal_analysis import (
    TREND_LIMITATION,
    DetectorConfig,
    EventClass,
    FrameValidationError,
    LevelTrend,
    QualityFlag,
    RfEventDetector,
    SourceObservationMetadata,
    SpectrumAcquisitionMode,
)

START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
SOURCE = "civilian-lab-receiver"


def _frame(
    sequence: int,
    power: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    *,
    source_id: str = SOURCE,
    center_frequency_hz: int = 100_000_000,
    span_hz: int = 1_000_000,
    dropped_frames: int = 0,
    data_age_ms: int = 0,
    captured_at: datetime | None = None,
    unit: str = "dBFS",
    calibration_id: str | None = None,
    uncertainty_db: float | None = None,
) -> SpectrumFrame:
    return SpectrumFrame(
        source_id=source_id,
        sequence=sequence,
        center_frequency_hz=center_frequency_hz,
        span_hz=span_hz,
        power_dbm=power,
        captured_at=captured_at or START + timedelta(milliseconds=sequence * 100),
        provenance=Provenance.LIVE,
        unit=unit,
        calibration_id=calibration_id,
        uncertainty_db=uncertainty_db,
        dropped_frames=dropped_frames,
        data_age_ms=data_age_ms,
    )


def _config(**overrides: object) -> DetectorConfig:
    values: dict[str, object] = {
        "history_frames": 8,
        "min_history_frames": 4,
        "trend_window": 4,
    }
    values.update(overrides)
    return DetectorConfig(**values)


def _warm(
    detector: RfEventDetector,
    *,
    bins: int = 128,
    frames: int = 4,
    source_id: str = SOURCE,
) -> int:
    for sequence in range(1, frames + 1):
        result = detector.analyze(
            _frame(
                sequence,
                np.full(bins, -100.0, dtype=np.float32),
                source_id=source_id,
            )
        )
        assert result.event is None
    return frames + 1


def test_detects_narrowband_activity_with_observable_evidence() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -100.0, dtype=np.float32)
    power[61:65] = -72.0

    result = detector.analyze(_frame(sequence, power))

    assert result.event is not None
    assert result.event.classification == EventClass.NARROWBAND_ACTIVITY
    assert result.event.confidence_kind == "heuristic_evidence_score_not_probability"
    assert result.event.evidence.active_bin_fraction == pytest.approx(4 / 128)
    assert result.event.evidence.peak_excess_db == pytest.approx(28.0)
    assert result.event.evidence.occupied_bandwidth_hz > 0
    assert result.event.evidence.power_unit == "dBFS"
    assert result.event.evidence.reported_peak_level == pytest.approx(-72.0)
    assert QualityFlag.ABSOLUTE_CALIBRATION_UNVERIFIED in result.event.quality_flags


def test_single_bin_is_only_low_trust_candidate_evidence() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -100.0, dtype=np.float32)
    power[64] = -55.0

    result = detector.analyze(_frame(sequence, power))

    assert result.event is not None
    assert result.event.classification == EventClass.NARROWBAND_ACTIVITY
    assert result.event.evidence.active_bin_count == 1
    assert result.event.evidence.largest_contiguous_bins == 1
    assert result.event.confidence <= detector.config.max_single_frame_score
    assert result.assessment.trust.value == "low"


def test_sparse_spurs_are_not_collapsed_into_one_narrowband_signal() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -100.0, dtype=np.float32)
    power[np.asarray((2, 13, 24, 35, 46, 57, 68, 79, 90, 101, 125))] = -55.0

    result = detector.analyze(_frame(sequence, power))

    assert result.event is not None
    assert result.event.classification == EventClass.MULTICOMPONENT_ACTIVITY
    assert result.event.evidence.contiguous_component_count == 11
    assert result.event.evidence.frequency_envelope_hz > 900_000
    assert result.event.evidence.occupied_bandwidth_hz < 100_000
    assert result.event.evidence.spectral_fill_ratio < 0.10
    assert result.event.confidence <= 0.49


def test_distinguishes_persistent_broadband_from_sudden_impulse() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -78.0, dtype=np.float32)

    impulse = detector.analyze(_frame(sequence, power))
    persistent = detector.analyze(_frame(sequence + 1, power))

    assert impulse.event is not None
    assert impulse.event.classification == EventClass.IMPULSIVE_INTERFERENCE
    assert persistent.event is not None
    assert persistent.event.classification == EventClass.BROADBAND_ACTIVITY


def test_swept_frame_does_not_claim_simultaneous_broadband_impulse() -> None:
    detector = RfEventDetector(_config())
    detector.register_source_metadata(
        SOURCE,
        SourceObservationMetadata(
            acquisition_mode=SpectrumAcquisitionMode.SWEPT_SPECTRUM,
            receiver_model="tinySA Ultra",
            sweep_duration_ms=420.0,
        ),
    )
    sequence = _warm(detector)
    power = np.full(128, -78.0, dtype=np.float32)

    first = detector.analyze(_frame(sequence, power))
    second = detector.analyze(_frame(sequence + 1, power))

    assert first.event is not None
    assert first.event.classification == EventClass.UNKNOWN
    assert first.event.evidence.acquisition_mode == (
        SpectrumAcquisitionMode.SWEPT_SPECTRUM
    )
    assert first.event.evidence.sweep_duration_ms == pytest.approx(420.0)
    assert first.event.evidence.limitations
    assert "не одновременно" in first.event.evidence.limitations[0]
    assert first.event.confidence <= 0.49
    assert first.assessment.evidence.limitations
    assert second.event is not None
    assert second.event.classification == EventClass.BROADBAND_ACTIVITY


def test_ambiguous_activity_is_reported_as_unknown() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -100.0, dtype=np.float32)
    power[40:60] = -86.0

    result = detector.analyze(_frame(sequence, power))

    assert result.event is not None
    assert result.event.classification == EventClass.UNKNOWN
    assert result.event.confidence <= 0.49


def test_history_and_source_tracking_are_bounded_and_floor_adapts() -> None:
    detector = RfEventDetector(
        _config(
            history_frames=5,
            min_history_frames=3,
            max_sources=2,
            floor_rise_alpha=0.5,
        )
    )
    for sequence in range(1, 4):
        detector.analyze(
            _frame(sequence, np.full(64, -100.0, dtype=np.float32), source_id="a")
        )
    result = None
    for sequence in range(4, 14):
        result = detector.analyze(
            _frame(sequence, np.full(64, -96.0, dtype=np.float32), source_id="a")
        )
    assert result is not None
    assert result.power_unit == "dBFS"
    assert -100.0 < result.reported_noise_floor <= -96.0
    assert detector.history_size("a") == 5

    detector.analyze(_frame(1, np.full(64, -100.0, dtype=np.float32), source_id="b"))
    detector.analyze(_frame(1, np.full(64, -100.0, dtype=np.float32), source_id="c"))

    assert detector.tracked_source_count == 2
    assert detector.history_size("a") == 0


def test_quality_flags_expose_gaps_staleness_and_reduce_confidence() -> None:
    degraded = RfEventDetector(_config())
    clean = RfEventDetector(_config())
    sequence = _warm(degraded)
    _warm(clean)
    power = np.full(128, -100.0, dtype=np.float32)
    power[60:64] = -70.0

    degraded_result = degraded.analyze(
        _frame(sequence + 2, power, dropped_frames=2, data_age_ms=9_000)
    )
    clean_result = clean.analyze(_frame(sequence, power))

    assert degraded_result.event is not None
    assert clean_result.event is not None
    assert {
        QualityFlag.DROPPED_FRAMES_REPORTED,
        QualityFlag.SEQUENCE_GAP,
        QualityFlag.DATA_STALE,
    } <= degraded_result.event.quality_flags
    assert degraded_result.event.confidence < clean_result.event.confidence


def test_grid_change_resets_baseline_and_is_visible() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector, bins=64)

    result = detector.analyze(
        _frame(
            sequence,
            np.full(128, -100.0, dtype=np.float32),
            span_hz=2_000_000,
        )
    )

    assert QualityFlag.SPECTRAL_GRID_CHANGED in result.quality_flags
    assert QualityFlag.INSUFFICIENT_HISTORY in result.quality_flags
    assert result.history_frames == 1


def test_rising_level_is_scoped_to_received_power_not_approach() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    result = None
    for offset, level in enumerate((-84.0, -80.0, -76.0), start=0):
        power = np.full(128, -100.0, dtype=np.float32)
        power[62:65] = level
        result = detector.analyze(_frame(sequence + offset, power))

    assert result is not None and result.event is not None
    assert result.event.level_trend == LevelTrend.RISING
    assert result.event.received_level_slope_db_per_frame is not None
    assert result.event.received_level_slope_db_per_frame > 0
    assert result.event.trend_limitation == TREND_LIMITATION
    assert "does not estimate distance" in result.event.trend_limitation
    assert "approaching" in result.event.trend_limitation


def test_calibration_flag_depends_on_explicit_record_and_uncertainty() -> None:
    detector = RfEventDetector(_config())
    sequence = _warm(detector)
    power = np.full(128, -100.0, dtype=np.float32)
    power[61:65] = -72.0

    result = detector.analyze(
        _frame(
            sequence,
            power,
            unit="dBm",
            calibration_id="lab-cal-2026-07",
            uncertainty_db=1.2,
        )
    )

    assert result.event is not None
    assert (
        QualityFlag.ABSOLUTE_CALIBRATION_UNVERIFIED
        not in result.event.quality_flags
    )
    assert result.event.evidence.power_unit == "dBm"
    assert result.event.evidence.calibration_id == "lab-cal-2026-07"
    assert result.event.evidence.calibration_uncertainty_db == pytest.approx(1.2)


def test_rejects_malformed_and_non_monotonic_frames() -> None:
    detector = RfEventDetector(_config())
    valid = np.full(64, -100.0, dtype=np.float32)
    detector.analyze(_frame(1, valid))

    with pytest.raises(FrameValidationError, match=r"FRAME\.NON_MONOTONIC_SEQUENCE"):
        detector.analyze(_frame(1, valid))
    with pytest.raises(FrameValidationError, match=r"FRAME\.NON_FINITE"):
        invalid = valid.copy()
        invalid[3] = np.nan
        detector.analyze(_frame(2, invalid))
    with pytest.raises(FrameValidationError, match=r"FRAME\.POWER_SHAPE"):
        detector.analyze(_frame(2, np.full((8, 8), -100.0, dtype=np.float32)))
    with pytest.raises(FrameValidationError, match=r"FRAME\.TIMESTAMP"):
        detector.analyze(
            _frame(
                2,
                valid,
                captured_at=datetime(2026, 7, 25, 12, 0),
            )
        )
