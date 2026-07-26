from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alga_vector.sensor_fusion import (
    EvidenceStrength,
    FusionClassification,
    FusionConfig,
    FusionEvidence,
    FusionInputError,
    FusionLifecycle,
    FusionObservation,
    FusionTransitionKind,
    SensorFusionEngine,
    SensorModality,
)

BASE = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _observation(
    modality: SensorModality,
    seconds: float,
    *,
    source_id: str | None = None,
    quality: float = 0.90,
    strength: float = 0.85,
    evidence: tuple[str, ...] = ("episode-a",),
    validated: bool = False,
) -> FusionObservation:
    return FusionObservation(
        modality=modality,
        timestamp=BASE + timedelta(seconds=seconds),
        source_id=source_id or f"{modality.value}-01",
        quality=quality,
        strength=strength,
        summary=f"normalized {modality.value} observation",
        evidence=evidence,
        validated=validated,
    )


def _codes(items: tuple[FusionEvidence, ...]) -> set[str]:
    return {item.code for item in items}


def _confirm(engine: SensorFusionEngine, offset: float = 0.0):
    engine.process(_observation(SensorModality.RF, offset, source_id="rf-a"))
    engine.process(
        _observation(
            SensorModality.ACOUSTIC,
            offset + 0.10,
            source_id="acoustic-a",
        )
    )
    return engine.process(
        _observation(
            SensorModality.RF,
            offset + 0.25,
            source_id="rf-a",
        )
    )


def test_observation_normalizes_aliases_and_rejects_invalid_input() -> None:
    observation = FusionObservation(
        modality=SensorModality.RF,
        timestamp=BASE,
        source_id=" receiver-1 ",
        quality=0.8,
        strength=0.7,
        summary=" measured activity ",
        evidence=(" shared ", "shared", "trace"),
    )

    assert observation.source_id == "receiver-1"
    assert observation.source == "receiver-1"
    assert observation.observed_at == BASE
    assert observation.evidence == ("shared", "trace")
    assert observation.evidence_keys == observation.evidence

    with pytest.raises(ValueError, match="timezone-aware"):
        FusionObservation(
            modality=SensorModality.RF,
            timestamp=datetime(2026, 7, 26, 12, 0),
            source_id="rf-a",
            quality=0.8,
            strength=0.7,
            summary="activity",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("quality", -0.1), ("quality", 1.1), ("strength", float("nan"))),
)
def test_observation_rejects_invalid_normalized_values(
    field: str,
    value: float,
) -> None:
    kwargs = {"quality": 0.8, "strength": 0.8}
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        FusionObservation(
            modality=SensorModality.RF,
            timestamp=BASE,
            source_id="rf-a",
            quality=kwargs["quality"],
            strength=kwargs["strength"],
            summary="activity",
        )


def test_observation_rejects_disagreeing_evidence_aliases() -> None:
    with pytest.raises(ValueError, match="disagree"):
        FusionObservation(
            modality=SensorModality.RF,
            timestamp=BASE,
            source_id="rf-a",
            quality=0.8,
            strength=0.8,
            summary="activity",
            evidence_keys=("one",),
            evidence=("two",),
        )


def test_empty_engine_is_background() -> None:
    update = SensorFusionEngine().tick(BASE)

    assert update.transition is None
    assert update.decision.classification is FusionClassification.BACKGROUND
    assert update.decision.lifecycle is FusionLifecycle.IDLE
    assert update.decision.evidence_strength is EvidenceStrength.NONE
    assert not update.decision.alertable
    assert update.decision.calibrated_probability is None


def test_single_rf_stream_remains_non_alerting_candidate() -> None:
    engine = SensorFusionEngine()

    engine.process(_observation(SensorModality.RF, 0.0, source_id="rf-a"))
    engine.process(_observation(SensorModality.RF, 0.3, source_id="rf-a"))
    update = engine.process(
        _observation(SensorModality.RF, 0.6, source_id="rf-a")
    )

    assert update.transition is None
    assert update.decision.classification is FusionClassification.RF_ACTIVITY
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert update.decision.observation_count == 3
    assert not update.decision.alertable
    assert "FUSION.ACOUSTIC_CONFIRMATION_MISSING" in _codes(
        update.decision.missing
    )


def test_single_acoustic_stream_remains_non_alerting_candidate() -> None:
    engine = SensorFusionEngine()

    engine.process(
        _observation(SensorModality.ACOUSTIC, 0.0, source_id="acoustic-a")
    )
    update = engine.process(
        _observation(SensorModality.ACOUSTIC, 0.3, source_id="acoustic-a")
    )

    assert (
        update.decision.classification
        is FusionClassification.ACOUSTIC_ANOMALY
    )
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert not update.decision.alertable


def test_independent_rf_acoustic_sequence_confirms_after_three_observations() -> None:
    update = _confirm(SensorFusionEngine())

    assert update.transition is not None
    assert update.transition.kind is FusionTransitionKind.CONFIRMED
    assert update.transition.transition_id == "fusion-transition-000001"
    assert (
        update.decision.classification
        is FusionClassification.MULTI_SENSOR_CORRELATED
    )
    assert update.decision.lifecycle is FusionLifecycle.CONFIRMED
    assert update.decision.evidence_strength is EvidenceStrength.HIGH
    assert update.decision.alertable
    assert update.decision.episode_id == "fusion-000001"
    assert "FUSION.RF_ACOUSTIC_CORRELATED" in _codes(
        update.decision.evidence
    )
    assert all(
        contribution.confirming
        for contribution in update.decision.contributions
        if not contribution.context_only
    )


def test_two_activity_observations_are_not_confirmed() -> None:
    engine = SensorFusionEngine()
    engine.process(_observation(SensorModality.RF, 0.0, source_id="rf-a"))
    update = engine.process(
        _observation(
            SensorModality.ACOUSTIC,
            0.3,
            source_id="acoustic-a",
        )
    )

    assert (
        update.decision.classification
        is FusionClassification.UNCONFIRMED_ANOMALY
    )
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert not update.decision.alertable
    assert "FUSION.MORE_OBSERVATIONS_REQUIRED" in _codes(
        update.decision.missing
    )


def test_same_source_id_does_not_establish_independence() -> None:
    engine = SensorFusionEngine()
    engine.process(_observation(SensorModality.RF, 0.0, source_id="shared"))
    engine.process(
        _observation(SensorModality.ACOUSTIC, 0.2, source_id="shared")
    )
    update = engine.process(
        _observation(SensorModality.RF, 0.4, source_id="shared")
    )

    assert update.transition is None
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert not update.decision.alertable
    assert "FUSION.INDEPENDENCE_NOT_ESTABLISHED" in _codes(
        update.decision.contradictions
    )


def test_civil_adsb_alone_is_informational_context_only() -> None:
    update = SensorFusionEngine().process(
        _observation(
            SensorModality.CIVIL_ADSB,
            0.0,
            source_id="adsb-a",
            evidence=("cooperative-record",),
        )
    )

    assert (
        update.decision.classification
        is FusionClassification.NEARBY_COOPERATIVE_AIRCRAFT_CONTEXT
    )
    assert update.decision.lifecycle is FusionLifecycle.INFORMATIONAL
    assert update.decision.episode_id is None
    assert not update.decision.alertable
    assert "FUSION.CIVIL_ADSB_CONTEXT" in _codes(update.decision.evidence)
    assert "FUSION.CIVIL_ADSB_NOT_IFF" in _codes(
        update.decision.limitations
    )
    adsb_evidence = next(
        item
        for item in update.decision.evidence
        if item.code == "FUSION.CIVIL_ADSB_CONTEXT"
    )
    assert not adsb_evidence.confirming
    assert update.decision.contributions[0].context_only


def test_civil_adsb_never_completes_confirmation_count() -> None:
    engine = SensorFusionEngine()
    engine.process(_observation(SensorModality.RF, 0.0, source_id="rf-a"))
    engine.process(
        _observation(
            SensorModality.ACOUSTIC,
            0.2,
            source_id="acoustic-a",
        )
    )
    update = engine.process(
        _observation(SensorModality.CIVIL_ADSB, 0.3, source_id="adsb-a")
    )

    assert update.transition is None
    assert update.decision.observation_count == 2
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert not update.decision.alertable
    assert "FUSION.CIVIL_ADSB_CONTEXT" in _codes(update.decision.evidence)


def test_direction_is_context_only_when_validated_and_fresh() -> None:
    engine = SensorFusionEngine()
    engine.process(
        _observation(
            SensorModality.DIRECTION,
            0.0,
            source_id="direction-a",
            validated=True,
        )
    )
    update = engine.process(
        _observation(SensorModality.RF, 0.5, source_id="rf-a")
    )

    assert "FUSION.DIRECTION_CONTEXT" in _codes(update.decision.evidence)
    direction_evidence = next(
        item
        for item in update.decision.evidence
        if item.code == "FUSION.DIRECTION_CONTEXT"
    )
    assert not direction_evidence.confirming
    direction_contribution = next(
        item
        for item in update.decision.contributions
        if item.modality is SensorModality.DIRECTION
    )
    assert direction_contribution.context_only
    assert not direction_contribution.confirming


def test_unvalidated_and_stale_direction_are_ignored_with_reason() -> None:
    invalid_engine = SensorFusionEngine()
    invalid_engine.process(
        _observation(
            SensorModality.DIRECTION,
            0.0,
            source_id="direction-a",
            validated=False,
        )
    )
    invalid = invalid_engine.process(
        _observation(SensorModality.RF, 0.2, source_id="rf-a")
    )
    assert "FUSION.DIRECTION_NOT_VALIDATED" in _codes(
        invalid.decision.contradictions
    )
    assert "FUSION.DIRECTION_CONTEXT" not in _codes(
        invalid.decision.evidence
    )

    stale_engine = SensorFusionEngine()
    stale_engine.process(
        _observation(
            SensorModality.DIRECTION,
            0.0,
            source_id="direction-a",
            validated=True,
        )
    )
    stale = stale_engine.process(
        _observation(SensorModality.RF, 1.2, source_id="rf-a")
    )
    assert "FUSION.DIRECTION_STALE" in _codes(
        stale.decision.contradictions
    )
    assert "FUSION.DIRECTION_CONTEXT" not in _codes(stale.decision.evidence)


def test_direction_does_not_complete_confirmation_count() -> None:
    engine = SensorFusionEngine()
    engine.process(_observation(SensorModality.RF, 0.0, source_id="rf-a"))
    engine.process(
        _observation(
            SensorModality.DIRECTION,
            0.1,
            source_id="direction-a",
            validated=True,
        )
    )
    update = engine.process(
        _observation(
            SensorModality.ACOUSTIC,
            0.3,
            source_id="acoustic-a",
        )
    )

    assert update.decision.observation_count == 2
    assert update.decision.lifecycle is FusionLifecycle.CANDIDATE
    assert not update.decision.alertable


def test_release_hysteresis_holds_then_resolves_once() -> None:
    engine = SensorFusionEngine(
        FusionConfig(
            temporal_window_seconds=1.0,
            direction_freshness_seconds=0.5,
            civil_adsb_context_seconds=1.0,
            minimum_correlation_dwell_seconds=0.1,
            release_hold_seconds=0.3,
            candidate_timeout_seconds=1.0,
            debounce_seconds=1.0,
        )
    )
    confirmed = _confirm(engine)
    assert confirmed.decision.lifecycle is FusionLifecycle.CONFIRMED

    holding = engine.process(
        _observation(
            SensorModality.RF,
            0.3,
            source_id="rf-a",
            strength=0.2,
        )
    )
    assert holding.transition is None
    assert holding.decision.lifecycle is FusionLifecycle.HOLDING
    assert holding.decision.alertable

    resolved = engine.tick(BASE + timedelta(seconds=0.61))
    assert resolved.transition is not None
    assert resolved.transition.kind is FusionTransitionKind.RESOLVED
    assert resolved.decision.lifecycle is FusionLifecycle.RESOLVED
    assert resolved.decision.classification is FusionClassification.BACKGROUND
    assert not resolved.decision.alertable

    repeated = engine.tick(BASE + timedelta(seconds=0.8))
    assert repeated.transition is None
    assert repeated.decision.lifecycle is FusionLifecycle.RESOLVED


def test_candidate_expires_without_alert_transition() -> None:
    engine = SensorFusionEngine()
    engine.process(_observation(SensorModality.RF, 0.0, source_id="rf-a"))

    update = engine.tick(BASE + timedelta(seconds=3.1))

    assert update.transition is None
    assert update.decision.lifecycle is FusionLifecycle.RESOLVED
    assert update.decision.classification is FusionClassification.BACKGROUND
    assert not update.decision.alertable


def test_confirmation_transition_is_debounced_across_episodes() -> None:
    engine = SensorFusionEngine(
        FusionConfig(
            temporal_window_seconds=1.0,
            direction_freshness_seconds=0.5,
            civil_adsb_context_seconds=1.0,
            minimum_correlation_dwell_seconds=0.1,
            release_hold_seconds=0.2,
            candidate_timeout_seconds=1.0,
            debounce_seconds=5.0,
        )
    )
    first = _confirm(engine)
    assert first.transition is not None
    engine.tick(BASE + timedelta(seconds=1.3))

    engine.process(_observation(SensorModality.RF, 1.4, source_id="rf-a"))
    engine.process(
        _observation(
            SensorModality.ACOUSTIC,
            1.5,
            source_id="acoustic-a",
        )
    )
    second = engine.process(
        _observation(SensorModality.RF, 1.7, source_id="rf-a")
    )

    assert second.transition is None
    assert second.decision.lifecycle is FusionLifecycle.CONFIRMED
    assert second.decision.alertable
    assert second.decision.debounced
    assert second.decision.episode_id == "fusion-000002"


def test_duplicate_is_idempotent_and_time_regression_is_rejected() -> None:
    engine = SensorFusionEngine()
    observation = _observation(SensorModality.RF, 0.0, source_id="rf-a")
    first = engine.process(observation)
    duplicate = engine.process(observation)

    assert duplicate.transition is None
    assert duplicate.decision == first.decision
    assert duplicate.decision.observation_count == 1

    with pytest.raises(FusionInputError, match="different observations"):
        engine.process(
            _observation(
                SensorModality.RF,
                0.0,
                source_id="rf-a",
                strength=0.9,
            )
        )
    with pytest.raises(FusionInputError, match="regressed"):
        engine.process(
            _observation(
                SensorModality.ACOUSTIC,
                -0.1,
                source_id="acoustic-a",
            )
        )


def test_fixed_sequence_is_deterministic() -> None:
    sequence = (
        _observation(SensorModality.RF, 0.0, source_id="rf-a"),
        _observation(
            SensorModality.CIVIL_ADSB,
            0.05,
            source_id="adsb-a",
        ),
        _observation(
            SensorModality.ACOUSTIC,
            0.10,
            source_id="acoustic-a",
        ),
        _observation(SensorModality.RF, 0.25, source_id="rf-a"),
    )

    first_engine = SensorFusionEngine()
    second_engine = SensorFusionEngine()
    first_updates = tuple(first_engine.process(item) for item in sequence)
    second_updates = tuple(second_engine.process(item) for item in sequence)

    assert first_updates == second_updates


def test_decision_text_uses_only_generic_safe_claims() -> None:
    decision = _confirm(SensorFusionEngine()).decision
    text = " ".join(
        item.explanation
        for group in (
            decision.evidence,
            decision.contradictions,
            decision.missing,
            decision.limitations,
        )
        for item in group
    ).casefold()

    forbidden = (
        "nation" + "ality",
        "d" + "rone",
        "host" + "ility",
        "supp" + "ression",
        "target" + "ing",
        "ran" + "ge",
        "geo" + "location",
    )
    assert not any(word in text for word in forbidden)


def test_config_rejects_unsafe_or_incoherent_thresholds() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        FusionConfig(minimum_observations=2)
    with pytest.raises(ValueError, match="above"):
        FusionConfig(attack_strength=0.4, release_strength=0.5)
    with pytest.raises(ValueError, match="maximum_history"):
        FusionConfig(maximum_history=2)
