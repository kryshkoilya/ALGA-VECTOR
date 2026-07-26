from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alga_vector.signal_processor import (
    ConfidenceScore,
    DirectionEstimate,
    EventSeverity,
    HumanReadableInterpreter,
    NormalizedEvent,
    NormalizedEventType,
    OperatorSituationMode,
    SensorAvailability,
    SensorKind,
    SensorState,
    SourceAttribution,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _sensor(
    kind: SensorKind,
    availability: SensorAvailability,
    sensor_id: str,
) -> SensorState:
    return SensorState(
        sensor_id=sensor_id,
        sensor_kind=kind,
        availability=availability,
        message_ru="Проверяемое состояние.",
        checked_at=NOW,
    )


def _event(
    event_type: NormalizedEventType,
    *,
    severity: EventSeverity,
    valid_until: datetime | None = None,
    direction: DirectionEstimate | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        schema_version="1.0",
        event_id=event_type.value,
        event_type=event_type,
        observed_at=NOW,
        received_at=NOW,
        valid_until=valid_until,
        severity=severity,
        confidence=ConfidenceScore.heuristic(
            0.65,
            "Эвристическая сила признаков; не вероятность.",
        ),
        summary_ru={
            NormalizedEventType.NOISE_BACKGROUND: "Фон чистый",
            NormalizedEventType.RADIO_ACTIVITY_DETECTED: "Обнаружена активность",
            NormalizedEventType.DIRECTION_ESTIMATED: "Получен азимут",
        }.get(event_type, "Событие"),
        explanation_ru="Краткое объяснение решения.",
        recommendation_ru="Проверьте ситуацию независимым средством.",
        sources=(
            SourceAttribution(
                sensor_id="source",
                sensor_kind=(
                    SensorKind.DIRECTION_FINDER
                    if direction is not None
                    else SensorKind.RF_SPECTRUM
                ),
                contribution=0.65,
                independent_confirmation=False,
                explanation_ru="Трассируемый вклад.",
            ),
        ),
        direction=direction,
    )


def test_no_event_never_claims_clean_background() -> None:
    situation = HumanReadableInterpreter().interpret(
        (),
        (
            _sensor(
                SensorKind.RF_SPECTRUM,
                SensorAvailability.AVAILABLE,
                "rtl",
            ),
        ),
        now=NOW,
    )

    assert situation.mode is OperatorSituationMode.SILENCE
    assert situation.headline_ru == "Активных событий нет"
    assert "не подтверждён" in situation.explanation_ru


def test_clean_background_requires_explicit_fresh_background_event() -> None:
    situation = HumanReadableInterpreter().interpret(
        (
            _event(
                NormalizedEventType.NOISE_BACKGROUND,
                severity=EventSeverity.INFO,
                valid_until=NOW + timedelta(seconds=5),
            ),
        ),
        (
            _sensor(
                SensorKind.RF_SPECTRUM,
                SensorAvailability.AVAILABLE,
                "rtl",
            ),
        ),
        now=NOW,
    )

    assert situation.mode is OperatorSituationMode.BACKGROUND
    assert situation.headline_ru == "Фон чистый"


def test_activity_uses_fresh_external_direction_and_explains_no_range() -> None:
    direction = DirectionEstimate(
        bearing_deg=108.0,
        uncertainty_deg=12.0,
        source_id="kraken",
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=3),
        confidence=0.8,
        validated_external=True,
        calibration_id="cal-1",
    )
    situation = HumanReadableInterpreter().interpret(
        (
            _event(
                NormalizedEventType.RADIO_ACTIVITY_DETECTED,
                severity=EventSeverity.WARNING,
                valid_until=NOW + timedelta(seconds=5),
            ),
            _event(
                NormalizedEventType.DIRECTION_ESTIMATED,
                severity=EventSeverity.NOTICE,
                valid_until=NOW + timedelta(seconds=3),
                direction=direction,
            ),
        ),
        (
            _sensor(
                SensorKind.DIRECTION_FINDER,
                SensorAvailability.AVAILABLE,
                "kraken",
            ),
        ),
        now=NOW,
    )

    assert situation.mode is OperatorSituationMode.ACTIVITY
    assert "96" in situation.direction_ru
    assert "Дальность не измеряется" in situation.direction_ru


def test_missing_direction_has_explicit_fallback_message() -> None:
    situation = HumanReadableInterpreter().interpret(
        (
            _event(
                NormalizedEventType.RADIO_ACTIVITY_DETECTED,
                severity=EventSeverity.WARNING,
                valid_until=NOW + timedelta(seconds=5),
            ),
        ),
        (
            _sensor(
                SensorKind.DIRECTION_FINDER,
                SensorAvailability.UNAVAILABLE,
                "kraken",
            ),
        ),
        now=NOW,
    )

    assert "Пеленгация недоступна" in situation.direction_ru
    assert "KrakenSDR" in situation.direction_ru


def test_expired_high_priority_event_does_not_remain_primary() -> None:
    expired = _event(
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        severity=EventSeverity.ALARM,
        valid_until=NOW + timedelta(seconds=1),
    )
    situation = HumanReadableInterpreter().interpret(
        (expired,),
        (
            _sensor(
                SensorKind.RF_SPECTRUM,
                SensorAvailability.AVAILABLE,
                "rtl",
            ),
        ),
        now=NOW + timedelta(seconds=2),
    )

    assert situation.primary_event is None
    assert expired in situation.recent_events
    assert situation.headline_ru == "Активных событий нет"
