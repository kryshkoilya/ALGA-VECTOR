from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

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

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _source(
    sensor_id: str,
    kind: SensorKind,
    *,
    independent: bool = False,
) -> SourceAttribution:
    return SourceAttribution(
        sensor_id=sensor_id,
        sensor_kind=kind,
        contribution=0.8,
        independent_confirmation=independent,
        explanation_ru="Проверяемый вклад сенсора.",
    )


def _identity(
    *confirmations: str,
) -> ValidatedIdentityEvidence:
    return ValidatedIdentityEvidence(
        classifier_id="validated-classifier",
        model_version="1.2.3",
        validation_dataset_id="civil-validation-2026-01",
        validated_at=NOW - timedelta(days=1),
        class_label="generic_uas_signature",
        independent_confirmation_source_ids=confirmations,
    )


def _event(
    event_type: NormalizedEventType,
    *,
    sources: tuple[SourceAttribution, ...],
    identity: ValidatedIdentityEvidence | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        schema_version="1.0",
        event_id=f"event-{event_type.value}",
        event_type=event_type,
        observed_at=NOW,
        received_at=NOW,
        valid_until=NOW + timedelta(seconds=10),
        severity=EventSeverity.WARNING,
        confidence=ConfidenceScore.heuristic(
            0.8,
            "Эвристическая сила признаков; не вероятность.",
        ),
        summary_ru="Проверяемое событие",
        explanation_ru="Решение сформировано по трассируемым признакам.",
        recommendation_ru="Проверьте независимым средством.",
        sources=sources,
        evidence=(
            EvidenceFact(
                code="RF.FREQUENCY_HZ",
                explanation_ru="Измеренная центральная частота.",
                measured=5_800_000_000.0,
                unit="Hz",
            ),
        ),
        frequency_hz=5_800_000_000.0,
        identity=identity,
    )


def test_frequency_or_rssi_only_cannot_emit_drone_signature() -> None:
    with pytest.raises(EventPolicyViolation, match="independent physical"):
        _event(
            NormalizedEventType.LIKELY_DRONE_SIGNATURE,
            sources=(
                _source("validated-classifier", SensorKind.CLASSIFIER),
                _source(
                    "rtl-sdr",
                    SensorKind.RF_SPECTRUM,
                    independent=True,
                ),
            ),
            identity=_identity("rtl-sdr"),
        )


def test_drone_signature_requires_classifier_and_non_rf_confirmation() -> None:
    event = _event(
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        sources=(
            _source("validated-classifier", SensorKind.CLASSIFIER),
            _source("camera-1", SensorKind.CAMERA, independent=True),
        ),
        identity=_identity("camera-1"),
    )

    assert event.event_type is NormalizedEventType.LIKELY_DRONE_SIGNATURE
    assert event.confidence.is_calibrated_probability is False


def test_target_confirmed_requires_two_independent_physical_sources() -> None:
    with pytest.raises(EventPolicyViolation, match="at least 2"):
        _event(
            NormalizedEventType.TARGET_CONFIRMED,
            sources=(
                _source("validated-classifier", SensorKind.CLASSIFIER),
                _source("camera-1", SensorKind.CAMERA, independent=True),
            ),
            identity=_identity("camera-1"),
        )

    event = _event(
        NormalizedEventType.TARGET_CONFIRMED,
        sources=(
            _source("validated-classifier", SensorKind.CLASSIFIER),
            _source("camera-1", SensorKind.CAMERA, independent=True),
            _source("microphone-1", SensorKind.ACOUSTIC, independent=True),
        ),
        identity=_identity("camera-1", "microphone-1"),
    )
    assert event.event_type is NormalizedEventType.TARGET_CONFIRMED


def test_direction_requires_fresh_validated_external_evidence() -> None:
    with pytest.raises(EventPolicyViolation, match="validated external"):
        DirectionEstimate(
            bearing_deg=110.0,
            uncertainty_deg=8.0,
            source_id="manual",
            observed_at=NOW,
            valid_until=NOW + timedelta(seconds=3),
            confidence=0.7,
            validated_external=False,
            calibration_id="manual",
        )

    direction = DirectionEstimate(
        bearing_deg=110.0,
        uncertainty_deg=8.0,
        source_id="kraken",
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=3),
        confidence=0.7,
        validated_external=True,
        calibration_id="cal-1",
    )
    event = NormalizedEvent(
        schema_version="1.0",
        event_id="direction-1",
        event_type=NormalizedEventType.DIRECTION_ESTIMATED,
        observed_at=NOW,
        received_at=NOW,
        severity=EventSeverity.NOTICE,
        confidence=ConfidenceScore.heuristic(0.7, "Качество пеленга."),
        summary_ru="Получен азимут",
        explanation_ru="Свежий внешний пеленг.",
        recommendation_ru="Осмотрите сектор.",
        sources=(_source("kraken", SensorKind.DIRECTION_FINDER),),
        direction=direction,
        valid_until=NOW + timedelta(seconds=3),
    )
    assert "102" in direction.sector_text_ru
    assert json.loads(event.to_json())["direction"]["calibration_id"] == "cal-1"


def test_event_payload_is_json_serializable_without_probability_claim() -> None:
    event = NormalizedEvent(
        schema_version="1.0",
        event_id="background",
        event_type=NormalizedEventType.NOISE_BACKGROUND,
        observed_at=NOW,
        received_at=NOW,
        severity=EventSeverity.INFO,
        confidence=ConfidenceScore.unavailable(
            "Калиброванная вероятность не рассчитывается."
        ),
        summary_ru="Фон чистый",
        explanation_ru="Свежая оценка не выявила устойчивой активности.",
        recommendation_ru="Продолжайте наблюдение.",
        sources=(_source("rtl-sdr", SensorKind.RF_SPECTRUM),),
    )

    payload = json.loads(event.to_json())
    assert payload["event_type"] == "NOISE_BACKGROUND"
    assert payload["trace_id"] == event.event_id
    assert payload["technical_label"] == "NOISE_BACKGROUND"
    assert payload["operator_label"] == "Фон"
    assert payload["operator_explanation"] == event.explanation_ru
    assert payload["recommended_action_short"] == "Продолжайте наблюдение."
    assert payload["recommended_action_detailed"] == event.recommendation_ru
    assert payload["confidence"]["is_calibrated_probability"] is False
