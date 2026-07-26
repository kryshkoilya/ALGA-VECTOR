from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from alga_vector.domain.enums import Provenance
from alga_vector.domain.models import SpectrumFrame
from alga_vector.signal_analysis import (
    AttributionStatus,
    DataQuality,
    DecisionLifecycle,
    DecisionTransitionKind,
    DetectorConfig,
    RfDecisionEngine,
    RfEventDetector,
    RfFamily,
    SourceObservationMetadata,
    SpectrumAcquisitionMode,
)

START = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
SOURCE = "receiver-a"
BINS = 128


def _detector() -> RfEventDetector:
    return RfEventDetector(
        DetectorConfig(
            history_frames=8,
            min_history_frames=4,
            trend_window=4,
        )
    )


def _frame(
    sequence: int,
    power: np.ndarray[tuple[int, ...], np.dtype[np.float32]],
    *,
    source_id: str = SOURCE,
    elapsed_ms: int | None = None,
    dropped_frames: int = 0,
) -> SpectrumFrame:
    return SpectrumFrame(
        source_id=source_id,
        sequence=sequence,
        center_frequency_hz=100_000_000,
        span_hz=1_000_000,
        power_dbm=power,
        captured_at=START
        + timedelta(milliseconds=elapsed_ms if elapsed_ms is not None else sequence * 100),
        provenance=Provenance.LIVE,
        dropped_frames=dropped_frames,
    )


def _quiet() -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
    return np.full(BINS, -100.0, dtype=np.float32)


def _activity(
    index: int = 62,
    *,
    width: int = 4,
    level: float = -72.0,
) -> np.ndarray[tuple[int, ...], np.dtype[np.float32]]:
    power = _quiet()
    power[index : index + width] = level
    return power


def _warm(
    detector: RfEventDetector,
    engine: RfDecisionEngine,
    *,
    source_id: str = SOURCE,
) -> int:
    update = None
    for sequence in range(1, 6):
        update = engine.process(
            detector.analyze(
                _frame(sequence, _quiet(), source_id=source_id)
            )
        )
    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.IDLE
    return 6


def _confirm(
    detector: RfEventDetector,
    engine: RfDecisionEngine,
    *,
    index: int = 62,
    first_sequence: int = 6,
) -> tuple[int, str]:
    episode_ids: list[str] = []
    update = None
    for sequence in range(first_sequence, first_sequence + 3):
        update = engine.process(
            detector.analyze(_frame(sequence, _activity(index)))
        )
        assert update.decision.episode_id is not None
        episode_ids.append(update.decision.episode_id)
    assert update is not None
    assert len(set(episode_ids)) == 1
    assert update.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert update.transition is not None
    assert update.transition.kind == DecisionTransitionKind.CONFIRMED
    return first_sequence + 3, episode_ids[0]


def test_quiet_background_stays_idle_and_never_alerts() -> None:
    detector = _detector()
    engine = RfDecisionEngine()

    next_sequence = _warm(detector, engine)
    update = engine.process(detector.analyze(_frame(next_sequence, _quiet())))

    assert update.decision.lifecycle == DecisionLifecycle.IDLE
    assert update.decision.family == RfFamily.BACKGROUND
    assert update.decision.episode_id is None
    assert update.decision.alertable is False
    assert update.transition is None


def test_single_bin_is_suppressed_and_does_not_repeat_transition() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    first = engine.process(
        detector.analyze(_frame(sequence, _activity(width=1)))
    )
    second = engine.process(
        detector.analyze(_frame(sequence + 1, _activity(width=1)))
    )

    assert first.decision.lifecycle == DecisionLifecycle.SUPPRESSED
    assert first.decision.alertable is False
    assert first.transition is not None
    assert first.transition.kind == DecisionTransitionKind.SUPPRESSED
    assert second.decision.episode_id == first.decision.episode_id
    assert second.transition is None


def test_three_compatible_observations_confirm_one_stable_episode() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)

    sequence, episode_id = _confirm(detector, engine)
    first_stable = engine.process(
        detector.analyze(_frame(sequence, _activity()))
    )
    current = engine.process(
        detector.analyze(_frame(sequence + 1, _activity()))
    )

    assert first_stable.decision.family == RfFamily.NARROWBAND_BURST
    assert current.decision.episode_id == episode_id
    assert current.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert current.decision.family == RfFamily.CARRIER
    assert current.decision.alertable is True
    assert current.transition is None


def test_varying_narrowband_envelope_is_only_voice_like_compatible() -> None:
    detector = _detector()
    detector.register_source_metadata(
        SOURCE,
        SourceObservationMetadata(
            acquisition_mode=SpectrumAcquisitionMode.SIMULTANEOUS_FFT,
            receiver_model="test-iq-receiver",
        ),
    )
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    update = None
    for offset, level in enumerate((-72.0, -80.0, -70.0, -78.0, -69.0)):
        update = engine.process(
            detector.analyze(
                _frame(
                    sequence + offset,
                    _activity(width=3, level=level),
                )
            )
        )

    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert update.decision.family == RfFamily.VOICE_LIKE
    assert update.decision.identity_established is False
    assert update.decision.calibrated_probability is None
    assert "совместим" in update.decision.family_explanation_ru.lower()


def test_persistent_wideband_change_confirms_as_interference_family() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)
    wideband = np.full(BINS, -76.0, dtype=np.float32)

    first = engine.process(detector.analyze(_frame(sequence, wideband)))
    assert first.decision.lifecycle == DecisionLifecycle.SUPPRESSED

    update = None
    for offset in range(1, 6):
        update = engine.process(
            detector.analyze(_frame(sequence + offset, wideband))
        )

    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert update.decision.family == RfFamily.INTERFERENCE_NOISE_LIKE
    assert update.decision.identity_established is False


def test_one_multibin_impulse_never_confirms_an_episode() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    candidate = engine.process(
        detector.analyze(_frame(sequence, _activity(width=4)))
    )
    quiet = engine.process(
        detector.analyze(_frame(sequence + 1, _quiet()))
    )

    assert candidate.decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert candidate.decision.alertable is False
    assert quiet.decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert quiet.decision.alertable is False
    assert candidate.transition is None
    assert quiet.transition is None


def test_regular_recurrence_can_confirm_only_a_periodic_generic_family() -> None:
    detector = _detector()
    detector.register_source_metadata(
        SOURCE,
        SourceObservationMetadata(
            acquisition_mode=SpectrumAcquisitionMode.SIMULTANEOUS_FFT,
            receiver_model="test-iq-receiver",
        ),
    )
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    update = None
    for offset, power in enumerate(
        (_activity(), _quiet(), _activity(), _quiet(), _activity())
    ):
        update = engine.process(
            detector.analyze(_frame(sequence + offset, power))
        )

    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert update.decision.family == RfFamily.PERIODIC_BEACON_LIKE
    assert update.decision.calibrated_probability is None
    assert any(
        item.code == "RF.HEURISTIC_NOT_PROBABILITY"
        for item in update.decision.limitations
    )
    assert RfFamily.PACKET_LIKE in {
        item.family for item in update.decision.alternatives
    }


def test_irregular_recurrence_is_packet_like_and_unknown_mode_blocks_periodic_claim() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)
    samples = (
        (_activity(), 600),
        (_quiet(), 700),
        (_activity(), 800),
        (_quiet(), 900),
        (_activity(), 1_300),
    )

    update = None
    for offset, (power, elapsed_ms) in enumerate(samples):
        update = engine.process(
            detector.analyze(
                _frame(
                    sequence + offset,
                    power,
                    elapsed_ms=elapsed_ms,
                )
            )
        )

    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert update.decision.family == RfFamily.PACKET_LIKE


def test_engine_emits_only_canonical_safe_active_families() -> None:
    canonical = {
        RfFamily.CARRIER,
        RfFamily.NARROWBAND_BURST,
        RfFamily.BROADBAND_BURST,
        RfFamily.PACKET_LIKE,
        RfFamily.VOICE_LIKE,
        RfFamily.PERIODIC_BEACON_LIKE,
        RfFamily.INTERFERENCE_NOISE_LIKE,
        RfFamily.UNKNOWN,
    }
    assert {item.value for item in canonical} == {
        "carrier",
        "narrowband_burst",
        "broadband_burst",
        "packet_like",
        "voice_like",
        "periodic_beacon_like",
        "interference_noise_like",
        "unknown",
    }
    assert (
        RfFamily("continuous_carrier_or_spur")
        == RfFamily.CONTINUOUS_CARRIER_OR_SPUR
    )
    assert (
        RfFamily("voice_like_compatible")
        == RfFamily.VOICE_LIKE_COMPATIBLE
    )


def test_far_frequency_jumps_create_new_candidates_without_inherited_support() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    updates = [
        engine.process(
            detector.analyze(
                _frame(sequence + offset, _activity(index))
            )
        )
        for offset, index in enumerate((16, 62, 108))
    ]

    assert all(
        item.decision.lifecycle == DecisionLifecycle.CANDIDATE
        for item in updates
    )
    assert all(item.transition is None for item in updates)
    assert len(
        {
            item.decision.episode_id
            for item in updates
            if item.decision.episode_id is not None
        }
    ) == 3


def test_confirmation_requires_real_elapsed_dwell_not_only_frame_count() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)

    close_times = (600, 640, 680)
    update = None
    for offset, elapsed_ms in enumerate(close_times):
        update = engine.process(
            detector.analyze(
                _frame(
                    6 + offset,
                    _activity(),
                    elapsed_ms=elapsed_ms,
                )
            )
        )
    assert update is not None
    assert update.decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert update.transition is None

    confirmed = engine.process(
        detector.analyze(_frame(9, _activity(), elapsed_ms=860))
    )
    assert confirmed.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert confirmed.transition is not None
    assert confirmed.transition.kind == DecisionTransitionKind.CONFIRMED


def test_release_hysteresis_keeps_confirmed_decision_on_weak_jitter() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)
    next_sequence, episode_id = _confirm(detector, engine)

    weak = engine.process(
        detector.analyze(
            _frame(next_sequence, _activity(level=-93.0))
        )
    )

    assert weak.decision.episode_id == episode_id
    assert weak.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert weak.decision.alertable is True
    assert weak.transition is None


def test_short_dropout_holds_then_returns_without_new_transition() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)
    next_sequence, episode_id = _confirm(detector, engine)

    holding = engine.process(
        detector.analyze(_frame(next_sequence, _quiet()))
    )
    returned = engine.process(
        detector.analyze(_frame(next_sequence + 1, _activity()))
    )

    assert holding.decision.episode_id == episode_id
    assert holding.decision.lifecycle == DecisionLifecycle.HOLDING
    assert holding.decision.alertable is True
    assert holding.transition is None
    assert returned.decision.episode_id == episode_id
    assert returned.decision.lifecycle == DecisionLifecycle.CONFIRMED
    assert returned.transition is None


def test_sustained_absence_resolves_exactly_once() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)
    next_sequence, episode_id = _confirm(detector, engine)

    first_absent = engine.process(
        detector.analyze(_frame(next_sequence, _quiet(), elapsed_ms=900))
    )
    resolved = engine.process(
        detector.analyze(
            _frame(next_sequence + 1, _quiet(), elapsed_ms=1_700)
        )
    )
    after = engine.process(
        detector.analyze(
            _frame(next_sequence + 2, _quiet(), elapsed_ms=1_800)
        )
    )

    assert first_absent.decision.lifecycle == DecisionLifecycle.HOLDING
    assert resolved.decision.episode_id == episode_id
    assert resolved.decision.lifecycle == DecisionLifecycle.RESOLVED
    assert resolved.transition is not None
    assert resolved.transition.kind == DecisionTransitionKind.RESOLVED
    assert after.transition is None


def test_unreliable_observation_enters_data_hold_and_cannot_confirm() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    sequence = _warm(detector, engine)

    first = engine.process(
        detector.analyze(_frame(sequence, _activity()))
    )
    unreliable = engine.process(
        detector.analyze(
            _frame(
                sequence + 1,
                _activity(),
                dropped_frames=1,
            )
        )
    )
    resumed = engine.process(
        detector.analyze(_frame(sequence + 2, _activity()))
    )

    assert first.decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert unreliable.decision.lifecycle == DecisionLifecycle.DATA_HOLD
    assert unreliable.decision.data_quality == DataQuality.LOW
    assert unreliable.transition is None
    assert resumed.decision.lifecycle == DecisionLifecycle.CANDIDATE
    assert resumed.transition is None


def test_decision_language_is_generic_and_identity_remains_unavailable() -> None:
    detector = _detector()
    engine = RfDecisionEngine()
    _warm(detector, engine)
    next_sequence, _ = _confirm(detector, engine)
    update = engine.process(
        detector.analyze(_frame(next_sequence, _activity(level=-66.0)))
    )
    decision = update.decision
    rendered = " ".join(
        (
            decision.family.value,
            decision.family_explanation_ru,
            *(item.explanation_ru for item in decision.supporting_evidence),
            *(item.explanation_ru for item in decision.missing_confirmation),
            *(item.explanation_ru for item in decision.alternatives),
            *(item.explanation_ru for item in decision.limitations),
        )
    ).lower()

    assert decision.attribution == AttributionStatus.NOT_AVAILABLE
    assert decision.identity_established is False
    assert decision.calibrated_probability is None
    assert decision.sensor_contributions
    assert all(
        contribution.independent_confirmation is False
        for contribution in decision.sensor_contributions
    )
    assert "drone" not in rendered
    assert "identity established" not in rendered
