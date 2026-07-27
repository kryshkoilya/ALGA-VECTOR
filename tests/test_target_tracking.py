from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from alga_vector.direction import DirectionService
from alga_vector.domain.enums import (
    Capability,
    DeviceState,
    HealthLevel,
    Provenance,
)
from alga_vector.domain.models import DeviceSnapshot, SystemSnapshot
from alga_vector.sensor_fusion import SensorFusionEngine
from alga_vector.signal_processor import (
    ConfidenceScore,
    DirectionEstimate,
    EventPolicyViolation,
    EventSeverity,
    EvidenceFact,
    NormalizedEvent,
    NormalizedEventType,
    SensorKind,
    SourceAttribution,
    ValidatedIdentityEvidence,
)
from alga_vector.targets import (
    ConfirmationStage,
    PhenomenologicalType,
    SensorReadinessInterpreter,
    SensorReadinessLevel,
    SensorRole,
    TargetAggregator,
    TargetAggregatorConfig,
    TargetInputError,
    TargetLifecycle,
    TargetUpdateStatus,
    ValidatedZone,
    time_decay,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _source(
    sensor_id: str,
    kind: SensorKind,
    *,
    contribution: float = 0.8,
    independent: bool = False,
    observation_id: str | None = None,
) -> SourceAttribution:
    return SourceAttribution(
        sensor_id=sensor_id,
        sensor_kind=kind,
        contribution=contribution,
        independent_confirmation=independent,
        explanation_ru="Трассируемый вклад тестового сенсора.",
        observation_id=observation_id,
    )


def _identity(
    classifier_id: str,
    *confirmations: str,
) -> ValidatedIdentityEvidence:
    return ValidatedIdentityEvidence(
        classifier_id=classifier_id,
        model_version="1.0.0",
        validation_dataset_id="civil-validation-2026",
        validated_at=NOW - timedelta(days=1),
        class_label="generic_uas_signature",
        independent_confirmation_source_ids=confirmations,
    )


def _event(
    event_id: str,
    event_type: NormalizedEventType,
    *,
    at: datetime = NOW,
    sources: tuple[SourceAttribution, ...] | None = None,
    episode_id: str | None = None,
    frequency_hz: float | None = None,
    bandwidth_hz: float | None = None,
    confidence: float = 0.65,
    valid_for_seconds: float | None = 20.0,
    direction: DirectionEstimate | None = None,
    identity: ValidatedIdentityEvidence | None = None,
) -> NormalizedEvent:
    supplied_sources = sources or (
        _source("rtl-01", SensorKind.RF_SPECTRUM),
    )
    return NormalizedEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type=event_type,
        observed_at=at,
        received_at=at,
        valid_until=(
            at + timedelta(seconds=valid_for_seconds)
            if valid_for_seconds is not None
            else None
        ),
        severity=(
            EventSeverity.ALARM
            if event_type is NormalizedEventType.TARGET_CONFIRMED
            else EventSeverity.WARNING
        ),
        confidence=ConfidenceScore.heuristic(
            confidence,
            "Эвристическая сила тестовых признаков; не вероятность.",
        ),
        summary_ru="Тестовое нормализованное событие",
        explanation_ru="Событие сформировано из трассируемых тестовых признаков.",
        recommendation_ru="Проверьте событие независимым разрешённым средством.",
        sources=supplied_sources,
        evidence=(
            EvidenceFact(
                code="TEST.OBSERVATION",
                explanation_ru="Трассируемый тестовый факт.",
                source_id=supplied_sources[0].sensor_id,
                measured=1,
                unit="count",
            ),
        ),
        frequency_hz=frequency_hz,
        bandwidth_hz=bandwidth_hz,
        direction=direction,
        episode_id=episode_id,
        identity=identity,
        limitations=("Тестовое событие не определяет дальность или намерение.",),
    )


def _config(**overrides: object) -> TargetAggregatorConfig:
    values: dict[str, object] = {
        "correlation_window_seconds": 3.0,
        "deduplication_window_seconds": 2.0,
        "decay_half_life_seconds": 4.0,
        "stale_after_seconds": 8.0,
        "retire_after_seconds": 20.0,
        "tombstone_retention_seconds": 20.0,
    }
    values.update(overrides)
    return TargetAggregatorConfig(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    devices: tuple[DeviceSnapshot, ...] = (),
    fusion: bool = False,
    captured_at: datetime = NOW,
) -> SystemSnapshot:
    return SystemSnapshot(
        revision=1,
        devices=devices,
        capabilities=(),
        incidents=(),
        spectrum=None,
        mode=Provenance.LIVE,
        profile_name="test",
        readiness_percent=0,
        fusion_decision=(
            SensorFusionEngine().tick(captured_at).decision if fusion else None
        ),
        captured_at=captured_at,
    )


def _device(
    device_id: str,
    kind: str,
    *,
    state: DeviceState = DeviceState.READY,
    health: HealthLevel = HealthLevel.HEALTHY,
    capabilities: frozenset[Capability] = frozenset(),
    last_data_at: datetime | None = NOW,
) -> DeviceSnapshot:
    return DeviceSnapshot(
        device_id=device_id,
        display_name=device_id,
        kind=kind,
        connection="test",
        state=state,
        health=health,
        capabilities=capabilities,
        last_data_at=last_data_at,
    )


def test_config_matches_runtime_schema_names_and_rejects_incoherent_timing() -> None:
    config = TargetAggregatorConfig(
        correlation_window_seconds=12.0,
        deduplication_window_seconds=4.0,
        decay_half_life_seconds=18.0,
        stale_after_seconds=30.0,
        retire_after_seconds=90.0,
        maximum_active_targets=64,
    )

    assert config.maximum_active_targets == 64
    with pytest.raises(ValueError, match="stale_after_seconds"):
        TargetAggregatorConfig(
            correlation_window_seconds=30.0,
            stale_after_seconds=30.0,
        )
    with pytest.raises(ValueError, match="minimum_direction_confidence"):
        TargetAggregatorConfig(minimum_direction_confidence=0.0)


def test_exact_and_semantic_duplicates_are_idempotent_and_conflicts_fail() -> None:
    aggregator = TargetAggregator(_config())
    event = _event(
        "event-1",
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        frequency_hz=433_920_000.0,
        episode_id="rf-episode-1",
    )

    first = aggregator.ingest(event)
    exact_duplicate = aggregator.ingest(event)
    semantic_duplicate = aggregator.ingest(replace(event, event_id="event-2"))
    delayed_envelope = aggregator.ingest(
        replace(
            event,
            event_id="event-3",
            received_at=NOW + timedelta(seconds=1),
        ),
        now=NOW + timedelta(seconds=1),
    )

    assert first.status is TargetUpdateStatus.CREATED
    assert first.target is not None
    assert first.target.evidence[0].code == "TEST.OBSERVATION"
    assert exact_duplicate.status is TargetUpdateStatus.DUPLICATE
    assert semantic_duplicate.status is TargetUpdateStatus.DUPLICATE
    assert delayed_envelope.status is TargetUpdateStatus.DUPLICATE
    assert aggregator.tracked_target_count == 1

    with pytest.raises(TargetInputError, match="EVENT_ID_CONFLICT"):
        aggregator.ingest(
            replace(event, summary_ru="Противоречащая версия того же event_id"),
            now=NOW + timedelta(seconds=1),
        )


def test_same_source_and_compatible_frequency_merge_but_far_frequency_does_not() -> None:
    aggregator = TargetAggregator(_config())
    first = aggregator.ingest(
        _event(
            "rf-a",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            frequency_hz=433_920_000.0,
            bandwidth_hz=100_000.0,
            confidence=0.6,
        )
    )
    second = aggregator.ingest(
        _event(
            "rf-b",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            at=NOW + timedelta(seconds=1),
            frequency_hz=433_950_000.0,
            bandwidth_hz=100_000.0,
            confidence=0.6,
        )
    )
    third = aggregator.ingest(
        _event(
            "rf-c",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            at=NOW + timedelta(seconds=2),
            frequency_hz=5_800_000_000.0,
            bandwidth_hz=20_000_000.0,
            confidence=0.6,
        )
    )

    assert first.target is not None and second.target is not None
    assert second.status is TargetUpdateStatus.UPDATED
    assert second.target.target_id == first.target.target_id
    assert third.status is TargetUpdateStatus.CREATED
    assert aggregator.tracked_target_count == 2


def test_temporal_proximity_alone_never_merges_independent_sources() -> None:
    aggregator = TargetAggregator(_config())
    rf = aggregator.ingest(
        _event(
            "rf",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(_source("rtl", SensorKind.RF_SPECTRUM),),
            confidence=0.6,
        )
    )
    acoustic = aggregator.ingest(
        _event(
            "audio",
            NormalizedEventType.ACOUSTIC_ANOMALY,
            sources=(_source("mic", SensorKind.ACOUSTIC),),
            confidence=0.6,
        )
    )

    assert rf.target is not None and acoustic.target is not None
    assert rf.target.target_id != acoustic.target.target_id
    assert aggregator.tracked_target_count == 2


def test_confirmed_fusion_event_can_bridge_two_explicit_sensor_tracks() -> None:
    aggregator = TargetAggregator(_config())
    rf = aggregator.ingest(
        _event(
            "rf",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(_source("rtl", SensorKind.RF_SPECTRUM),),
            confidence=0.6,
        )
    )
    acoustic = aggregator.ingest(
        _event(
            "audio",
            NormalizedEventType.ACOUSTIC_ANOMALY,
            sources=(_source("mic", SensorKind.ACOUSTIC),),
            confidence=0.6,
        )
    )
    fused = aggregator.ingest(
        _event(
            "fusion",
            NormalizedEventType.MULTISENSOR_CORRELATED,
            sources=(
                _source(
                    "rtl",
                    SensorKind.RF_SPECTRUM,
                    independent=True,
                ),
                _source(
                    "mic",
                    SensorKind.ACOUSTIC,
                    independent=True,
                ),
            ),
            confidence=0.8,
        )
    )

    assert rf.target is not None and acoustic.target is not None
    assert fused.status is TargetUpdateStatus.UPDATED
    assert fused.reason_code == "TARGET.FUSION_BRIDGE_MERGED"
    assert len(fused.merged_target_ids) == 1
    assert fused.target is not None
    assert fused.target.confirmation_stage is ConfirmationStage.LIKELY_SOURCE
    assert aggregator.tracked_target_count == 1


def test_generic_activity_never_promotes_itself_to_likely_or_confirmed_target() -> None:
    aggregator = TargetAggregator(_config())
    result = aggregator.ingest(
        _event(
            "strong-rf",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            confidence=0.99,
            episode_id="rf-episode",
        )
    )

    assert result.target is not None
    assert result.target.confirmation_stage is ConfirmationStage.LIKELY_SOURCE
    assert result.target.probable_type is PhenomenologicalType.RF_ACTIVITY

    with pytest.raises(EventPolicyViolation, match="independent physical"):
        _event(
            "unsafe",
            NormalizedEventType.LIKELY_DRONE_SIGNATURE,
            sources=(
                _source("classifier", SensorKind.CLASSIFIER),
                _source(
                    "rtl",
                    SensorKind.RF_SPECTRUM,
                    independent=True,
                ),
            ),
            identity=_identity("classifier", "rtl"),
        )


def test_identity_backed_events_control_likely_and_confirmed_target_stages() -> None:
    aggregator = TargetAggregator(_config())
    probable = aggregator.ingest(
        _event(
            "probable",
            NormalizedEventType.LIKELY_DRONE_SIGNATURE,
            sources=(
                _source("classifier", SensorKind.CLASSIFIER),
                _source("camera", SensorKind.CAMERA, independent=True),
            ),
            episode_id="target-episode",
            confidence=0.85,
            identity=_identity("classifier", "camera"),
        )
    )
    confirmed = aggregator.ingest(
        _event(
            "confirmed",
            NormalizedEventType.TARGET_CONFIRMED,
            at=NOW + timedelta(seconds=1),
            sources=(
                _source("classifier", SensorKind.CLASSIFIER),
                _source("camera", SensorKind.CAMERA, independent=True),
                _source("mic", SensorKind.ACOUSTIC, independent=True),
            ),
            episode_id="target-episode",
            confidence=0.92,
            identity=_identity("classifier", "camera", "mic"),
        )
    )

    assert probable.target is not None
    assert probable.target.confirmation_stage is ConfirmationStage.LIKELY_TARGET
    assert confirmed.target is not None
    assert confirmed.target.target_id == probable.target.target_id
    assert confirmed.target.confirmation_stage is ConfirmationStage.CONFIRMED_TARGET
    assert confirmed.target.recommended_action_short
    assert "план безопасности" in confirmed.target.recommended_action_short


def test_decay_holding_stale_tombstone_and_bounded_removal_are_deterministic() -> None:
    config = _config(
        correlation_window_seconds=1.0,
        decay_half_life_seconds=2.0,
        stale_after_seconds=5.0,
        retire_after_seconds=10.0,
        tombstone_retention_seconds=10.0,
    )
    aggregator = TargetAggregator(config)
    event = _event(
        "short",
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        confidence=0.8,
        valid_for_seconds=1.0,
    )
    created = aggregator.ingest(event)
    assert created.target is not None

    assert time_decay(
        event,
        now=NOW + timedelta(seconds=2),
        half_life_seconds=2.0,
        maximum_age_seconds=5.0,
    ) == 0.0  # explicit valid_until is fail-closed
    holding = aggregator.tick(NOW + timedelta(seconds=2))
    assert holding[0].lifecycle is TargetLifecycle.HOLDING
    assert holding[0].active is False
    assert (
        holding[0].confirmation_stage
        is ConfirmationStage.SUSPICIOUS_ACTIVITY
    )
    assert holding[0].probable_type is PhenomenologicalType.UNKNOWN_ACTIVITY
    assert holding[0].evidence_strength.value is None
    assert holding[0].evidence == ()
    assert holding[0].source_attribution == ()
    assert (
        holding[0].recommendation.code
        == "TARGET.WAIT_FOR_FRESH_EVIDENCE"
    )
    assert aggregator.active_targets(now=NOW + timedelta(seconds=2)) == ()

    stale = aggregator.tick(NOW + timedelta(seconds=6))
    assert stale[0].lifecycle is TargetLifecycle.STALE
    assert aggregator.active_targets(now=NOW + timedelta(seconds=6)) == ()

    assert aggregator.tick(NOW + timedelta(seconds=11)) == ()
    tombstones = aggregator.targets(
        now=NOW + timedelta(seconds=11),
        include_tombstones=True,
    )
    assert tombstones[0].lifecycle is TargetLifecycle.TOMBSTONED
    assert tombstones[0].tombstoned_at == NOW + timedelta(seconds=10)
    assert "устаревшую" in stale[0].recommended_action_short

    assert (
        aggregator.targets(
            now=NOW + timedelta(seconds=22),
            include_tombstones=True,
        )
        == ()
    )


def test_delayed_expired_ingest_is_ignored_and_never_becomes_active() -> None:
    aggregator = TargetAggregator(_config())
    event = _event(
        "delayed-expired",
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        valid_for_seconds=1.0,
    )
    delayed = replace(
        event,
        received_at=NOW + timedelta(seconds=2),
    )

    result = aggregator.ingest(
        delayed,
        now=NOW + timedelta(seconds=2),
    )

    assert result.status is TargetUpdateStatus.IGNORED
    assert result.reason_code == "TARGET.EXPIRED_EVENT_IGNORED"
    assert result.target is None
    assert aggregator.tracked_target_count == 0
    assert aggregator.active_targets(now=NOW + timedelta(seconds=2)) == ()


def test_exact_stale_boundary_is_fail_closed_everywhere() -> None:
    config = _config(
        correlation_window_seconds=1.0,
        stale_after_seconds=5.0,
        retire_after_seconds=10.0,
    )
    aggregator = TargetAggregator(config)
    event = _event(
        "stale-boundary",
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        valid_for_seconds=20.0,
        confidence=0.8,
    )
    aggregator.ingest(event)

    target = aggregator.tick(NOW + timedelta(seconds=5))[0]

    assert target.lifecycle is TargetLifecycle.STALE
    assert target.active is False
    assert target.confirmation_stage is ConfirmationStage.SUSPICIOUS_ACTIVITY
    assert target.probable_type is PhenomenologicalType.UNKNOWN_ACTIVITY
    assert target.evidence == ()
    assert target.source_attribution == ()
    assert target.recommendation.code == "TARGET.REACQUIRE_STALE"
    assert time_decay(
        event,
        now=NOW + timedelta(seconds=5),
        half_life_seconds=2.0,
        maximum_age_seconds=5.0,
    ) == 0.0


def test_expired_confirmation_cannot_outrank_a_fresh_active_target() -> None:
    aggregator = TargetAggregator(
        _config(
            correlation_window_seconds=1.0,
            stale_after_seconds=6.0,
            retire_after_seconds=12.0,
        )
    )
    confirmed = aggregator.ingest(
        _event(
            "confirmed-expiring",
            NormalizedEventType.TARGET_CONFIRMED,
            sources=(
                _source("classifier", SensorKind.CLASSIFIER),
                _source("camera", SensorKind.CAMERA, independent=True),
                _source("mic", SensorKind.ACOUSTIC, independent=True),
            ),
            episode_id="confirmed-episode",
            valid_for_seconds=1.0,
            identity=_identity("classifier", "camera", "mic"),
        )
    )
    assert confirmed.target is not None

    fresh = aggregator.ingest(
        _event(
            "fresh-rf",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            at=NOW + timedelta(seconds=2),
            sources=(_source("rtl-fresh", SensorKind.RF_SPECTRUM),),
            episode_id="fresh-episode",
        )
    )
    assert fresh.target is not None

    targets = aggregator.targets(now=NOW + timedelta(seconds=2))
    expired = next(
        item for item in targets if item.target_id == confirmed.target.target_id
    )

    assert targets[0].target_id == fresh.target.target_id
    assert targets[0].lifecycle is TargetLifecycle.ACTIVE
    assert expired.lifecycle is TargetLifecycle.HOLDING
    assert expired.active is False
    assert expired.confirmation_stage is ConfirmationStage.SUSPICIOUS_ACTIVITY
    assert expired.recommendation.code == "TARGET.WAIT_FOR_FRESH_EVIDENCE"
    assert expired.evidence == ()
    assert expired.source_attribution == ()


def test_decay_halves_at_half_life_for_unexpired_event() -> None:
    event = _event(
        "decay",
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        valid_for_seconds=30.0,
    )

    assert time_decay(
        event,
        now=NOW,
        half_life_seconds=4.0,
        maximum_age_seconds=20.0,
    ) == pytest.approx(1.0)
    assert time_decay(
        event,
        now=NOW + timedelta(seconds=4),
        half_life_seconds=4.0,
        maximum_age_seconds=20.0,
    ) == pytest.approx(0.5)


def test_repeated_same_sensor_is_one_bounded_attribution_not_additive_evidence() -> None:
    aggregator = TargetAggregator(_config())
    source = _source(
        "rtl",
        SensorKind.RF_SPECTRUM,
        contribution=0.8,
    )
    first = aggregator.ingest(
        _event(
            "one",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(source,),
            episode_id="episode",
            confidence=0.6,
        )
    )
    second = aggregator.ingest(
        _event(
            "two",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            at=NOW + timedelta(seconds=1),
            sources=(source,),
            episode_id="episode",
            confidence=0.6,
        )
    )

    assert first.target is not None and second.target is not None
    assert len(second.target.source_attribution) == 1
    attribution = second.target.source_attribution[0]
    assert attribution.observation_count == 2
    assert attribution.contribution <= 0.8
    assert second.target.evidence_strength.value is not None
    assert second.target.evidence_strength.value <= 0.8


def test_direction_requires_explicit_episode_and_conflicts_fail_closed() -> None:
    aggregator = TargetAggregator(_config())
    created = aggregator.ingest(
        _event(
            "activity",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            episode_id="episode",
            confidence=0.6,
        )
    )
    assert created.target is not None

    first_direction = DirectionEstimate(
        bearing_deg=100.0,
        uncertainty_deg=5.0,
        source_id="kraken-a",
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=8),
        confidence=0.8,
        validated_external=True,
        calibration_id="cal-a",
    )
    ignored = aggregator.ingest(
        _event(
            "unassociated-direction",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=1),
            sources=(_source("kraken-a", SensorKind.DIRECTION_FINDER),),
            direction=first_direction,
            episode_id=None,
            confidence=0.8,
        )
    )
    attached = aggregator.ingest(
        _event(
            "associated-direction",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=1),
            sources=(_source("kraken-a", SensorKind.DIRECTION_FINDER),),
            direction=first_direction,
            episode_id="episode",
            confidence=0.8,
        )
    )

    assert ignored.status is TargetUpdateStatus.IGNORED
    assert attached.target is not None
    assert attached.target.direction == first_direction
    assert "95" in attached.target.sector_text_ru
    with pytest.raises(
        ValueError,
        match="matching direction-finder attribution",
    ):
        replace(
            attached.target,
            source_attribution=tuple(
                item
                for item in attached.target.source_attribution
                if item.sensor_id != first_direction.source_id
            ),
        )

    conflicting_direction = DirectionEstimate(
        bearing_deg=220.0,
        uncertainty_deg=5.0,
        source_id="kraken-b",
        observed_at=NOW + timedelta(seconds=2),
        valid_until=NOW + timedelta(seconds=8),
        confidence=0.8,
        validated_external=True,
        calibration_id="cal-b",
    )
    conflict = aggregator.ingest(
        _event(
            "conflicting-direction",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=2),
            sources=(_source("kraken-b", SensorKind.DIRECTION_FINDER),),
            direction=conflicting_direction,
            episode_id="episode",
            confidence=0.8,
        )
    )

    assert conflict.target is not None
    assert conflict.target.direction is None
    assert any("противоречат" in item for item in conflict.target.limitations)


def test_direction_quality_and_source_provenance_are_fail_closed() -> None:
    aggregator = TargetAggregator(_config(minimum_direction_confidence=0.4))
    created = aggregator.ingest(
        _event(
            "direction-policy-activity",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            episode_id="direction-policy-episode",
        )
    )
    assert created.target is not None

    low_quality = DirectionEstimate(
        bearing_deg=70.0,
        uncertainty_deg=8.0,
        source_id="kraken-low",
        observed_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=8),
        confidence=0.39,
        validated_external=True,
        calibration_id="cal-low",
    )
    low_result = aggregator.ingest(
        _event(
            "low-quality-direction",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=1),
            sources=(_source("kraken-low", SensorKind.DIRECTION_FINDER),),
            direction=low_quality,
            episode_id="direction-policy-episode",
        )
    )

    mismatched = DirectionEstimate(
        bearing_deg=80.0,
        uncertainty_deg=8.0,
        source_id="kraken-measurement",
        observed_at=NOW + timedelta(seconds=2),
        valid_until=NOW + timedelta(seconds=8),
        confidence=0.8,
        validated_external=True,
        calibration_id="cal-mismatch",
    )
    mismatch_result = aggregator.ingest(
        _event(
            "mismatched-direction-source",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=2),
            sources=(
                _source("kraken-envelope", SensorKind.DIRECTION_FINDER),
            ),
            direction=mismatched,
            episode_id="direction-policy-episode",
        )
    )
    wrong_kind_result = aggregator.ingest(
        _event(
            "wrong-kind-direction-source",
            NormalizedEventType.DIRECTION_ESTIMATED,
            at=NOW + timedelta(seconds=3),
            sources=(
                _source("kraken-measurement", SensorKind.RF_SPECTRUM),
            ),
            direction=replace(
                mismatched,
                observed_at=NOW + timedelta(seconds=3),
                valid_until=NOW + timedelta(seconds=9),
            ),
            episode_id="direction-policy-episode",
        )
    )

    assert low_result.status is TargetUpdateStatus.IGNORED
    assert (
        low_result.reason_code
        == "TARGET.DIRECTION_QUALITY_BELOW_THRESHOLD"
    )
    assert mismatch_result.status is TargetUpdateStatus.IGNORED
    assert (
        mismatch_result.reason_code
        == "TARGET.DIRECTION_SOURCE_ATTRIBUTION_REQUIRED"
    )
    assert wrong_kind_result.status is TargetUpdateStatus.IGNORED
    assert (
        wrong_kind_result.reason_code
        == "TARGET.DIRECTION_SOURCE_ATTRIBUTION_REQUIRED"
    )
    target = aggregator.targets(now=NOW + timedelta(seconds=3))[0]
    assert target.direction is None
    assert target.recent_event_ids == ("direction-policy-activity",)


def test_validated_zone_is_explicit_fresh_and_never_inferred() -> None:
    aggregator = TargetAggregator(_config())
    created = aggregator.ingest(
        _event(
            "activity",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            confidence=0.6,
        )
    )
    assert created.target is not None
    assert created.target.zone is None
    zone = ValidatedZone(
        zone_id="north-yard",
        label_ru="Северный сектор объекта",
        source_id="camera-zone-service",
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=5),
        calibration_id="zone-cal-1",
        confidence=0.8,
        validated_external=True,
    )

    attached = aggregator.attach_validated_zone(
        created.target.target_id,
        zone,
        now=NOW,
    )
    assert attached.zone == zone

    with pytest.raises(TargetInputError, match="not fresh"):
        aggregator.attach_validated_zone(
            created.target.target_id,
            zone,
            now=NOW + timedelta(seconds=6),
        )


def test_active_target_capacity_rejects_without_unbounded_growth() -> None:
    aggregator = TargetAggregator(_config(maximum_active_targets=1))
    first = aggregator.ingest(
        _event(
            "first",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(_source("rtl-a", SensorKind.RF_SPECTRUM),),
            frequency_hz=433_000_000.0,
            confidence=0.6,
        )
    )
    rejected = aggregator.ingest(
        _event(
            "second",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(_source("rtl-b", SensorKind.RF_SPECTRUM),),
            frequency_hz=5_800_000_000.0,
            confidence=0.6,
        )
    )

    assert first.status is TargetUpdateStatus.CREATED
    assert rejected.status is TargetUpdateStatus.CAPACITY_REJECTED
    assert aggregator.tracked_target_count == 1


def test_holding_target_does_not_block_confirmed_target_admission() -> None:
    aggregator = TargetAggregator(
        _config(maximum_active_targets=1)
    )
    first = aggregator.ingest(
        _event(
            "holding-first",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            sources=(_source("rtl-holding", SensorKind.RF_SPECTRUM),),
            episode_id="holding-episode",
            valid_for_seconds=1.0,
        )
    )
    assert first.target is not None

    confirmed = aggregator.ingest(
        _event(
            "confirmed-after-holding",
            NormalizedEventType.TARGET_CONFIRMED,
            at=NOW + timedelta(seconds=2),
            sources=(
                _source("classifier-confirmed", SensorKind.CLASSIFIER),
                _source(
                    "camera-confirmed",
                    SensorKind.CAMERA,
                    independent=True,
                ),
                _source(
                    "acoustic-confirmed",
                    SensorKind.ACOUSTIC,
                    independent=True,
                ),
            ),
            episode_id="confirmed-episode",
            confidence=0.95,
            identity=_identity(
                "classifier-confirmed",
                "camera-confirmed",
                "acoustic-confirmed",
            ),
        ),
        now=NOW + timedelta(seconds=2),
    )

    assert confirmed.status is TargetUpdateStatus.CREATED
    assert confirmed.target is not None
    assert confirmed.target.lifecycle is TargetLifecycle.ACTIVE
    assert (
        confirmed.target.confirmation_stage
        is ConfirmationStage.CONFIRMED_TARGET
    )
    targets = aggregator.targets(now=NOW + timedelta(seconds=2))
    holding = next(
        target
        for target in targets
        if target.target_id == first.target.target_id
    )
    assert holding.lifecycle is TargetLifecycle.HOLDING
    assert aggregator.active_targets(now=NOW + timedelta(seconds=2)) == (
        confirmed.target,
    )
    assert aggregator.tracked_target_count == 2


def test_same_stage_conflicting_classifications_resolve_to_unknown() -> None:
    aggregator = TargetAggregator(_config())
    radio = aggregator.ingest(
        _event(
            "radio-classification",
            NormalizedEventType.LIKELY_HANDHELD_RADIO,
            sources=(_source("classifier-radio", SensorKind.CLASSIFIER),),
            episode_id="classification-episode",
            confidence=0.55,
            identity=_identity("classifier-radio"),
        )
    )
    conflict = aggregator.ingest(
        _event(
            "video-classification",
            NormalizedEventType.LIKELY_VIDEO_LINK,
            at=NOW + timedelta(seconds=1),
            sources=(_source("classifier-video", SensorKind.CLASSIFIER),),
            episode_id="classification-episode",
            confidence=0.95,
            identity=_identity("classifier-video"),
        )
    )

    assert radio.target is not None and conflict.target is not None
    assert conflict.target.target_id == radio.target.target_id
    assert conflict.target.confirmation_stage is ConfirmationStage.LIKELY_SOURCE
    assert (
        conflict.target.probable_type
        is PhenomenologicalType.UNKNOWN_ACTIVITY
    )
    assert conflict.target.technical_label == "CLASSIFICATION_CONFLICT"
    assert (
        conflict.target.recommendation.code
        == "TARGET.RESOLVE_CLASSIFICATION_CONFLICT"
    )
    assert any(
        "классификации одного уровня противоречат" in limitation
        for limitation in conflict.target.limitations
    )


def test_ambiguous_fusion_bridge_never_collapses_multiple_tracks_from_one_sensor() -> None:
    aggregator = TargetAggregator(_config())
    for event_id, frequency in (
        ("rf-one", 433_000_000.0),
        ("rf-two", 5_800_000_000.0),
    ):
        aggregator.ingest(
            _event(
                event_id,
                NormalizedEventType.RADIO_ACTIVITY_DETECTED,
                sources=(_source("rtl", SensorKind.RF_SPECTRUM),),
                frequency_hz=frequency,
                confidence=0.6,
            )
        )
    aggregator.ingest(
        _event(
            "audio",
            NormalizedEventType.ACOUSTIC_ANOMALY,
            sources=(_source("mic", SensorKind.ACOUSTIC),),
            confidence=0.6,
        )
    )
    ambiguous = aggregator.ingest(
        _event(
            "ambiguous-fusion",
            NormalizedEventType.MULTISENSOR_CORRELATED,
            sources=(
                _source("rtl", SensorKind.RF_SPECTRUM, independent=True),
                _source("mic", SensorKind.ACOUSTIC, independent=True),
            ),
            confidence=0.8,
        )
    )

    assert ambiguous.merged_target_ids == ()
    assert aggregator.tracked_target_count == 4


def test_dedup_index_is_bounded_under_unique_event_flood() -> None:
    aggregator = TargetAggregator(
        _config(maximum_seen_events=16, maximum_active_targets=64)
    )
    for index in range(30):
        aggregator.ingest(
            _event(
                f"event-{index}",
                NormalizedEventType.RADIO_ACTIVITY_DETECTED,
                sources=(
                    _source(f"rtl-{index}", SensorKind.RF_SPECTRUM),
                ),
                frequency_hz=100_000_000.0 + index * 2_000_000.0,
                confidence=0.6,
            )
        )

    assert aggregator.dedup_entry_count <= 16


def test_readiness_always_returns_seven_canonical_unavailable_slots() -> None:
    readiness = SensorReadinessInterpreter().interpret(_snapshot())

    assert tuple(item.role for item in readiness.sensors) == tuple(SensorRole)
    assert len(readiness.sensors) == 7
    assert all(
        item.level is SensorReadinessLevel.UNAVAILABLE
        for item in readiness.sensors
    )
    assert (
        readiness.by_role(SensorRole.KRAKEN_SDR).impact_ru
        == "Направление цели не определяется."
    )


def test_simulated_direction_never_marks_kraken_ready() -> None:
    service = DirectionService(demo_mode=True, clock=lambda: NOW)
    direction = service.set_simulated(120.0, captured_at=NOW)
    readiness = SensorReadinessInterpreter().interpret(
        replace(_snapshot(), direction=direction)
    )

    kraken = readiness.by_role(SensorRole.KRAKEN_SDR)
    assert kraken.level is SensorReadinessLevel.UNAVAILABLE
    assert "не является измерением" in kraken.reason_ru
    assert kraken.impact_ru == "Направление цели не определяется."


def test_readiness_maps_devices_and_derives_fusion_capability() -> None:
    devices = (
        _device(
            "tinysa",
            "tinysa_ultra",
            capabilities=frozenset({Capability.TRIGGER_SOURCE}),
        ),
        _device(
            "rtl",
            "rtlsdr",
            state=DeviceState.STREAMING,
            capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
        ),
        _device(
            "kraken",
            "krakensdr",
            capabilities=frozenset({Capability.DF_OBSERVATION}),
        ),
        _device("microphone", "acoustic_microphone"),
        _device("dump1090", "adsb_dump1090"),
        _device(
            "passive",
            "passive_radar",
            state=DeviceState.DEGRADED,
            health=HealthLevel.DEGRADED,
        ),
    )
    readiness = SensorReadinessInterpreter().interpret(
        _snapshot(devices=devices, fusion=True)
    )

    assert readiness.by_role(SensorRole.TINYSA).level is SensorReadinessLevel.READY
    assert readiness.by_role(SensorRole.RTL_SDR).level is SensorReadinessLevel.READY
    assert (
        readiness.by_role(SensorRole.PASSIVE_RADAR).level
        is SensorReadinessLevel.LIMITED
    )
    assert readiness.by_role(SensorRole.FUSION).level is SensorReadinessLevel.READY


def test_stale_receiver_and_single_modality_make_readiness_limited() -> None:
    stale_at = NOW - timedelta(seconds=10)
    readiness = SensorReadinessInterpreter(
        sensor_stale_after_seconds=5.0
    ).interpret(
        _snapshot(
            devices=(
                _device(
                    "rtl",
                    "rtlsdr",
                    state=DeviceState.STREAMING,
                    capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
                    last_data_at=stale_at,
                ),
            ),
            fusion=True,
        )
    )

    assert (
        readiness.by_role(SensorRole.RTL_SDR).level
        is SensorReadinessLevel.LIMITED
    )
    assert readiness.by_role(SensorRole.FUSION).level is SensorReadinessLevel.LIMITED
