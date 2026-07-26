from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from alga_vector.domain.enums import Provenance
from alga_vector.domain.models import SpectrumFrame
from alga_vector.signal_analysis import (
    AssessmentState,
    AssessmentTrust,
    AttributionStatus,
    DetectorConfig,
    EventClass,
    QualityFlag,
    RfEventDetector,
    no_data_assessment,
)

START = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def _frame(
    sequence: int,
    power: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    *,
    source_id: str = "receiver-a",
    center_frequency_hz: int = 100_000_000,
    span_hz: int = 1_000_000,
    data_age_ms: int = 0,
    dropped_frames: int = 0,
) -> SpectrumFrame:
    return SpectrumFrame(
        source_id=source_id,
        sequence=sequence,
        center_frequency_hz=center_frequency_hz,
        span_hz=span_hz,
        power_dbm=power,
        captured_at=START + timedelta(milliseconds=sequence * 100),
        provenance=Provenance.LIVE,
        data_age_ms=data_age_ms,
        dropped_frames=dropped_frames,
    )


def _detector() -> RfEventDetector:
    return RfEventDetector(
        DetectorConfig(
            history_frames=8,
            min_history_frames=4,
            trend_window=4,
        )
    )


def _warm(detector: RfEventDetector, *, frames: int = 4) -> int:
    quiet = np.full(128, -100.0, dtype=np.float32)
    for sequence in range(1, frames + 1):
        detector.analyze(_frame(sequence, quiet))
    return frames + 1


def _activity(kind: str) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
    power = np.full(128, -100.0, dtype=np.float32)
    if kind == "concentrated":
        power[61:65] = -72.0
    elif kind == "ambiguous":
        power[40:60] = -82.0
    elif kind == "wide":
        power[:] = -76.0
    else:
        raise AssertionError(f"unknown test activity: {kind}")
    return power


def test_every_accepted_frame_has_guided_assessment_during_learning_and_quiet() -> None:
    detector = _detector()

    learning = detector.analyze(_frame(1, _activity("concentrated")))

    assert learning.event is not None  # expert evidence is preserved
    assert learning.assessment.state == AssessmentState.LEARNING_BACKGROUND
    assert learning.assessment.trust == AssessmentTrust.LOW
    assert learning.assessment.evidence.baseline_frames == 1
    assert learning.assessment.evidence.baseline_required_frames == 4
    assert learning.assessment.evidence.active_fraction is None
    assert learning.assessment.evidence.peak_excess_over_floor_db is None

    detector.reset()
    sequence = _warm(detector)
    quiet = detector.analyze(
        _frame(sequence, np.full(128, -100.0, dtype=np.float32))
    )

    assert quiet.event is None
    assert quiet.assessment.state == AssessmentState.BACKGROUND_ONLY
    assert quiet.assessment.trust == AssessmentTrust.HIGH
    assert quiet.assessment.evidence.active_fraction == 0.0
    assert quiet.assessment.evidence.occupied_bandwidth_hz == 0.0


def test_guided_states_cover_shape_burst_and_unclassified_activity() -> None:
    concentrated_detector = _detector()
    sequence = _warm(concentrated_detector)
    concentrated = concentrated_detector.analyze(
        _frame(sequence, _activity("concentrated"))
    )
    assert concentrated.assessment.state == AssessmentState.CONCENTRATED_RF

    wide_detector = _detector()
    sequence = _warm(wide_detector)
    burst = wide_detector.analyze(_frame(sequence, _activity("wide")))
    persistent = wide_detector.analyze(_frame(sequence + 1, _activity("wide")))
    assert burst.assessment.state == AssessmentState.TRANSIENT_BURST
    assert persistent.assessment.state == AssessmentState.WIDEBAND_RF

    unknown_detector = _detector()
    sequence = _warm(unknown_detector)
    unknown = unknown_detector.analyze(_frame(sequence, _activity("ambiguous")))
    assert unknown.assessment.state == AssessmentState.UNCLASSIFIED_RF
    assert unknown.assessment.trust == AssessmentTrust.LOW


def test_assessment_evidence_reports_coverage_peak_bandwidth_and_persistence() -> None:
    detector = _detector()
    sequence = _warm(detector)

    first = detector.analyze(_frame(sequence, _activity("concentrated")))
    second = detector.analyze(_frame(sequence + 1, _activity("concentrated")))
    evidence = second.assessment.evidence

    assert evidence.coverage_low_hz == pytest.approx(99_500_000.0)
    assert evidence.coverage_high_hz == pytest.approx(100_500_000.0)
    assert 99_500_000.0 <= (evidence.peak_frequency_hz or 0.0) <= 100_500_000.0
    assert evidence.occupied_bandwidth_hz is not None
    assert evidence.occupied_bandwidth_hz > 0.0
    assert evidence.peak_excess_over_floor_db == pytest.approx(28.0)
    assert evidence.active_fraction == pytest.approx(4 / 128)
    assert first.assessment.evidence.persistence_frames == 1
    assert evidence.persistence_frames == 2


def test_stale_or_discontinuous_frame_is_not_given_a_guided_classification() -> None:
    detector = _detector()
    sequence = _warm(detector)

    stale = detector.analyze(
        _frame(
            sequence + 2,
            _activity("concentrated"),
            data_age_ms=9_000,
            dropped_frames=2,
        )
    )

    assert stale.event is not None  # expert event remains available
    assert stale.assessment.state == AssessmentState.DATA_UNRELIABLE
    assert stale.assessment.trust == AssessmentTrust.LOW
    assert stale.assessment.reason_code == "SIGNAL.DATA_STALE"
    assert {
        QualityFlag.DATA_STALE,
        QualityFlag.DROPPED_FRAMES_REPORTED,
        QualityFlag.SEQUENCE_GAP,
    } <= stale.assessment.quality_flags


@pytest.mark.parametrize(
    ("frame_counters", "expected_flag", "expected_reason"),
    [
        (
            {"data_age_ms": 9_000},
            QualityFlag.DATA_STALE,
            "SIGNAL.DATA_STALE",
        ),
        (
            {"dropped_frames": 2},
            QualityFlag.DROPPED_FRAMES_REPORTED,
            "SIGNAL.FRAMES_DROPPED",
        ),
    ],
)
def test_first_unreliable_frame_preempts_learning_and_cannot_teach_baseline(
    frame_counters: dict[str, int],
    expected_flag: QualityFlag,
    expected_reason: str,
) -> None:
    detector = _detector()

    first = detector.analyze(
        _frame(1, _activity("concentrated"), **frame_counters)
    )

    assert first.event is not None  # expert evidence remains inspectable
    assert first.assessment.state == AssessmentState.DATA_UNRELIABLE
    assert first.assessment.trust == AssessmentTrust.LOW
    assert first.assessment.reason_code == expected_reason
    assert expected_flag in first.assessment.quality_flags
    assert first.history_frames == 0
    assert first.assessment.evidence.baseline_frames == 0
    assert detector.history_size("receiver-a") == 0

    clean = detector.analyze(
        _frame(2, np.full(128, -100.0, dtype=np.float32))
    )

    assert clean.assessment.state == AssessmentState.LEARNING_BACKGROUND
    assert clean.assessment.evidence.baseline_frames == 1
    assert detector.history_size("receiver-a") == 1


def test_attribution_is_structurally_unavailable_for_every_assessment() -> None:
    detector = _detector()
    assessment = detector.analyze(
        _frame(1, _activity("concentrated"))
    ).assessment
    no_data = no_data_assessment(START)

    for current in (assessment, no_data):
        assert current.attribution == AttributionStatus.NOT_AVAILABLE
        assert current.identity_established is False

    with pytest.raises(ValueError, match="identity cannot be established"):
        replace(assessment, identity_established=True)


def test_transient_label_is_neutral_while_legacy_enum_input_remains_accepted() -> None:
    assert EventClass.IMPULSIVE_INTERFERENCE is EventClass.TRANSIENT_BURST
    assert EventClass("impulsive_interference") is EventClass.TRANSIENT_BURST
    assert EventClass.TRANSIENT_BURST.value == "transient_burst"


def test_guided_lexicon_never_claims_identity_distance_direction_or_approach() -> None:
    detector = _detector()
    sequence = _warm(detector)
    assessments = [
        no_data_assessment(START),
        detector.analyze(_frame(sequence, _activity("concentrated"))).assessment,
        detector.analyze(_frame(sequence + 1, _activity("wide"))).assessment,
        detector.analyze(_frame(sequence + 2, _activity("wide"))).assessment,
        detector.analyze(_frame(sequence + 3, _activity("ambiguous"))).assessment,
    ]
    forbidden = (
        "дрон",
        "бпла",
        "азимут",
        "километр",
        "дистанц",
        "приближ",
        "удаля",
        "идентифицирован",
    )

    for assessment in assessments:
        user_text = " ".join(
            (
                assessment.headline_ru,
                assessment.explanation_ru,
                assessment.operator_action_ru,
            )
        ).casefold()
        assert all(word not in user_text for word in forbidden)
        assert assessment.identity_established is False
