from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alga_vector.domain.enums import (
    Capability,
    DeviceState,
    HealthLevel,
    Provenance,
)
from alga_vector.domain.models import DeviceSnapshot, SystemSnapshot
from alga_vector.signal_analysis import (
    AttributionStatus,
    DataQuality,
    DecisionEvidence,
    DecisionLifecycle,
    EvidenceStrength,
    RfDecision,
    RfFamily,
    SensorContribution,
)
from alga_vector.signal_processor import (
    NormalizedEventType,
    OperatorSituationMode,
    SnapshotEventNormalizer,
    UnifiedSignalProcessor,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _device() -> DeviceSnapshot:
    return DeviceSnapshot(
        device_id="rtl-1",
        display_name="RTL-SDR",
        kind="rtlsdr",
        connection="RTLSDR:0",
        state=DeviceState.STREAMING,
        health=HealthLevel.HEALTHY,
        capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
        last_data_at=NOW,
    )


def _decision(
    *,
    family: RfFamily = RfFamily.PACKET_LIKE,
    lifecycle: DecisionLifecycle = DecisionLifecycle.CONFIRMED,
) -> RfDecision:
    episode_id = None if lifecycle is DecisionLifecycle.IDLE else "rf-episode"
    return RfDecision(
        source_id="rtl-1",
        observed_at=NOW,
        lifecycle=lifecycle,
        family=family,
        family_explanation_ru="Общая наблюдаемая RF-форма.",
        episode_id=episode_id,
        started_at=NOW - timedelta(seconds=2) if episode_id else None,
        last_active_at=NOW if episode_id else None,
        peak_frequency_hz=5_800_000_000.0 if episode_id else None,
        occupied_bandwidth_hz=20_000_000.0 if episode_id else None,
        heuristic_score=0.8 if episode_id else 0.1,
        calibrated_probability=None,
        evidence_strength=(
            EvidenceStrength.HIGH
            if episode_id
            else EvidenceStrength.LOW
        ),
        data_quality=DataQuality.HIGH,
        alertable=lifecycle in {
            DecisionLifecycle.CONFIRMED,
            DecisionLifecycle.HOLDING,
        },
        abstained=False,
        supporting_evidence=(
            DecisionEvidence(
                code="RF.RECURRENCE",
                explanation_ru="Эпизод повторился в согласованных кадрах.",
                measured=4,
            ),
        ),
        contradicting_evidence=(),
        missing_confirmation=(),
        sensor_contributions=(
            SensorContribution(
                source_id="rtl-1",
                contribution=0.8,
                data_quality=DataQuality.HIGH,
                independent_confirmation=False,
                explanation_ru="Один RF-приёмник.",
            ),
        ),
        attribution=AttributionStatus.NOT_AVAILABLE,
        identity_established=False,
        limitations=(
            DecisionEvidence(
                code="RF.NO_IDENTITY",
                explanation_ru="Физический источник не установлен.",
            ),
        ),
    )


def _snapshot(decision: RfDecision | None) -> SystemSnapshot:
    return SystemSnapshot(
        revision=1,
        devices=(_device(),),
        capabilities=(),
        incidents=(),
        spectrum=None,
        mode=Provenance.LIVE,
        profile_name="test",
        readiness_percent=100,
        signal_decision=decision,
        captured_at=NOW,
    )


def test_generic_rf_decision_never_becomes_identity_event() -> None:
    result = SnapshotEventNormalizer().normalize(_snapshot(_decision()))
    event_types = {item.event_type for item in result.events}

    assert NormalizedEventType.RADIO_ACTIVITY_DETECTED in event_types
    assert NormalizedEventType.LIKELY_DRONE_SIGNATURE not in event_types
    assert NormalizedEventType.TARGET_CONFIRMED not in event_types
    rf_event = next(
        item
        for item in result.events
        if item.event_type is NormalizedEventType.RADIO_ACTIVITY_DETECTED
    )
    assert "не установлен" in rf_event.explanation_ru
    assert "радиоактивность" not in rf_event.summary_ru


def test_activity_without_df_emits_explicit_direction_fallback() -> None:
    result = SnapshotEventNormalizer().normalize(_snapshot(_decision()))

    fallback = tuple(
        item
        for item in result.events
        if item.event_type is NormalizedEventType.SENSOR_UNAVAILABLE
        and "Пеленгация" in item.summary_ru
    )
    assert len(fallback) == 1


def test_background_has_semantic_deduplication_and_does_not_flood_history() -> None:
    background = _decision(
        family=RfFamily.BACKGROUND,
        lifecycle=DecisionLifecycle.IDLE,
    )
    processor = UnifiedSignalProcessor()
    first = processor.process_snapshot(_snapshot(background))
    second = processor.process_snapshot(_snapshot(background))

    assert first.mode is OperatorSituationMode.BACKGROUND
    assert second.mode is OperatorSituationMode.BACKGROUND
    background_events = tuple(
        item
        for item in processor.event_bus.recent(limit=20)
        if item.event_type is NormalizedEventType.NOISE_BACKGROUND
    )
    assert len(background_events) == 1


def test_absent_rf_device_does_not_claim_clean_background() -> None:
    snapshot = SystemSnapshot(
        revision=1,
        devices=(),
        capabilities=(),
        incidents=(),
        spectrum=None,
        mode=Provenance.LIVE,
        profile_name="test",
        readiness_percent=0,
        captured_at=NOW,
    )
    situation = UnifiedSignalProcessor().process_snapshot(snapshot)

    assert situation.mode is OperatorSituationMode.SILENCE
    assert situation.headline_ru != "Фон чистый"
    assert any(
        item.event_type is NormalizedEventType.SENSOR_UNAVAILABLE
        for item in situation.recent_events
    )


def test_rf_data_hold_uses_one_stable_operator_episode() -> None:
    result = SnapshotEventNormalizer().normalize(
        _snapshot(_decision(lifecycle=DecisionLifecycle.DATA_HOLD))
    )

    rf_quality_event = next(
        item
        for item in result.events
        if item.event_type is NormalizedEventType.SENSOR_UNAVAILABLE
        and item.frequency_hz is not None
    )

    assert rf_quality_event.episode_id == "rf-data-unavailable:rtl-1"
