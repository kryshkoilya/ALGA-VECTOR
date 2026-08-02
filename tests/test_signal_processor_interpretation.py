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
    event_id: str | None = None,
    observed_at: datetime = NOW,
    received_at: datetime | None = None,
    episode_id: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        schema_version="1.0",
        event_id=event_id or event_type.value,
        event_type=event_type,
        observed_at=observed_at,
        received_at=received_at or observed_at,
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
        episode_id=episode_id,
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
                episode_id="rf-episode",
            ),
            _event(
                NormalizedEventType.DIRECTION_ESTIMATED,
                severity=EventSeverity.NOTICE,
                valid_until=NOW + timedelta(seconds=3),
                direction=direction,
                episode_id="rf-episode",
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


def test_standalone_direction_is_context_not_probable_activity() -> None:
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
    event = _event(
        NormalizedEventType.DIRECTION_ESTIMATED,
        severity=EventSeverity.NOTICE,
        valid_until=NOW + timedelta(seconds=3),
        direction=direction,
    )

    situation = HumanReadableInterpreter().interpret(
        (event,),
        (
            _sensor(
                SensorKind.DIRECTION_FINDER,
                SensorAvailability.AVAILABLE,
                "kraken",
            ),
        ),
        now=NOW,
    )

    assert situation.mode is OperatorSituationMode.BACKGROUND
    assert situation.primary_event is event
    assert situation.direction is None


def test_direction_from_another_episode_is_not_attached_to_primary() -> None:
    direction = DirectionEstimate(
        bearing_deg=211.0,
        uncertainty_deg=7.0,
        source_id="kraken",
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=3),
        confidence=0.9,
        validated_external=True,
        calibration_id="cal-cross-episode",
    )
    rf_event = _event(
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        severity=EventSeverity.WARNING,
        valid_until=NOW + timedelta(seconds=5),
        episode_id="rf-episode",
    )
    direction_event = _event(
        NormalizedEventType.DIRECTION_ESTIMATED,
        severity=EventSeverity.NOTICE,
        valid_until=NOW + timedelta(seconds=3),
        direction=direction,
        episode_id="other-episode",
    )

    situation = HumanReadableInterpreter().interpret(
        (rf_event, direction_event),
        (
            _sensor(
                SensorKind.DIRECTION_FINDER,
                SensorAvailability.AVAILABLE,
                "kraken",
            ),
        ),
        now=NOW,
    )

    assert situation.primary_event is rf_event
    assert situation.direction is None
    assert "211" not in situation.direction_ru
    assert direction_event in situation.recent_events


def test_critical_direction_context_cannot_mask_semantic_rf_activity() -> None:
    direction = DirectionEstimate(
        bearing_deg=108.0,
        uncertainty_deg=12.0,
        source_id="kraken",
        observed_at=NOW,
        valid_until=NOW + timedelta(seconds=3),
        confidence=0.8,
        validated_external=True,
        calibration_id="cal-priority",
    )
    rf_event = _event(
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        severity=EventSeverity.WARNING,
        valid_until=NOW + timedelta(seconds=5),
        episode_id="shared-episode",
    )
    direction_event = _event(
        NormalizedEventType.DIRECTION_ESTIMATED,
        severity=EventSeverity.CRITICAL,
        valid_until=NOW + timedelta(seconds=3),
        direction=direction,
        episode_id="shared-episode",
    )

    situation = HumanReadableInterpreter().interpret(
        (rf_event, direction_event),
        (),
        now=NOW,
    )

    assert situation.primary_event is rf_event
    assert situation.mode is OperatorSituationMode.ACTIVITY
    assert situation.direction is direction


def test_newer_expired_timeline_entries_cannot_truncate_active_primary() -> None:
    active = _event(
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        severity=EventSeverity.WARNING,
        valid_until=NOW + timedelta(seconds=60),
        event_id="active-rf",
        episode_id="active-episode",
    )
    expired = tuple(
        _event(
            NormalizedEventType.NOISE_BACKGROUND,
            severity=EventSeverity.INFO,
            observed_at=NOW + timedelta(seconds=index),
            valid_until=NOW + timedelta(seconds=index + 1),
            event_id=f"expired-{index}",
        )
        for index in range(1, 7)
    )

    situation = HumanReadableInterpreter(recent_event_limit=3).interpret(
        (active, *expired),
        (),
        now=NOW + timedelta(seconds=10),
    )

    assert situation.primary_event is active
    assert situation.mode is OperatorSituationMode.ACTIVITY
    assert active not in situation.recent_events
    assert len(situation.recent_events) == 3


def test_important_only_filters_timeline_without_changing_primary() -> None:
    activity = _event(
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        severity=EventSeverity.INFO,
        valid_until=NOW + timedelta(seconds=5),
        episode_id="non-important-activity",
    )

    situation = HumanReadableInterpreter().interpret(
        (activity,),
        (),
        now=NOW,
        important_only=True,
    )

    assert activity.is_important is False
    assert situation.primary_event is activity
    assert situation.mode is OperatorSituationMode.ACTIVITY
    assert situation.recent_events == ()


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
