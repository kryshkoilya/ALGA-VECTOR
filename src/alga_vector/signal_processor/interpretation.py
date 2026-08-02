"""Human-readable Russian interpretation for the Simple Mode UI."""

# ruff: noqa: RUF001

from __future__ import annotations

from datetime import datetime

from .schema import (
    ConfidenceScore,
    DirectionEstimate,
    EventSeverity,
    NormalizedEvent,
    NormalizedEventType,
    OperatorSituation,
    OperatorSituationMode,
    SensorAvailability,
    SensorKind,
    SensorState,
)

_SEVERITY_RANK = {
    EventSeverity.INFO: 0,
    EventSeverity.NOTICE: 1,
    EventSeverity.WARNING: 2,
    EventSeverity.ALARM: 3,
    EventSeverity.CRITICAL: 4,
}
_TYPE_RANK = {
    NormalizedEventType.TARGET_CONFIRMED: 100,
    NormalizedEventType.LIKELY_DRONE_SIGNATURE: 90,
    NormalizedEventType.ACOUSTIC_ANOMALY: 70,
    NormalizedEventType.MULTISENSOR_CORRELATED: 75,
    NormalizedEventType.LIKELY_VIDEO_LINK: 65,
    NormalizedEventType.LIKELY_HANDHELD_RADIO: 60,
    NormalizedEventType.RADIO_ACTIVITY_DETECTED: 50,
    NormalizedEventType.DIRECTION_ESTIMATED: 40,
    NormalizedEventType.ADSB_CONTACT: 30,
    NormalizedEventType.SENSOR_UNAVAILABLE: 20,
    NormalizedEventType.NOISE_BACKGROUND: 10,
}
_SEMANTIC_ACTIVITY_TYPES = frozenset(
    {
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        NormalizedEventType.ACOUSTIC_ANOMALY,
        NormalizedEventType.MULTISENSOR_CORRELATED,
        NormalizedEventType.TARGET_CONFIRMED,
    }
)


class HumanReadableInterpreter:
    def __init__(self, *, recent_event_limit: int = 20) -> None:
        if recent_event_limit < 1:
            raise ValueError("recent_event_limit must be positive")
        self._recent_event_limit = recent_event_limit

    def interpret(
        self,
        events: tuple[NormalizedEvent, ...],
        sensors: tuple[SensorState, ...],
        *,
        now: datetime,
        important_only: bool = False,
    ) -> OperatorSituation:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        ordered = tuple(
            sorted(events, key=lambda item: item.received_at, reverse=True)
        )
        active = tuple(item for item in ordered if item.is_active_at(now))
        recent = ordered
        if important_only:
            recent = tuple(item for item in recent if item.is_important)
        recent = recent[: self._recent_event_limit]
        primary = (
            max(active, key=self._priority_key)
            if active
            else None
        )
        if primary is None:
            return self._quiet_or_limited(sensors, recent, now)

        mode = self._mode_for(primary)
        direction = self._best_direction(active, primary, now)
        direction_ru = self._direction_text(direction, sensors)
        limitations = tuple(
            dict.fromkeys(
                limitation
                for item in active
                for limitation in item.limitations
            )
        )
        return OperatorSituation(
            generated_at=now,
            mode=mode,
            headline_ru=primary.summary_ru,
            explanation_ru=primary.explanation_ru,
            severity=primary.severity,
            confidence=primary.confidence,
            direction_ru=direction_ru,
            direction=direction,
            recommendation_ru=primary.recommendation_ru,
            primary_event=primary,
            recent_events=recent,
            sensors=sensors,
            limitations=limitations,
        )

    @staticmethod
    def _priority_key(
        event: NormalizedEvent,
    ) -> tuple[int, int, int, datetime]:
        return (
            1 if event.event_type in _SEMANTIC_ACTIVITY_TYPES else 0,
            _SEVERITY_RANK[event.severity],
            _TYPE_RANK[event.event_type],
            event.received_at,
        )

    @staticmethod
    def _mode_for(event: NormalizedEvent) -> OperatorSituationMode:
        if event.event_type is NormalizedEventType.TARGET_CONFIRMED:
            return OperatorSituationMode.CONFIRMED_TARGET
        if event.event_type in {
            NormalizedEventType.NOISE_BACKGROUND,
            NormalizedEventType.DIRECTION_ESTIMATED,
        }:
            return OperatorSituationMode.BACKGROUND
        if event.event_type is NormalizedEventType.SENSOR_UNAVAILABLE:
            return OperatorSituationMode.SILENCE
        return OperatorSituationMode.ACTIVITY

    @staticmethod
    def _best_direction(
        events: tuple[NormalizedEvent, ...],
        primary: NormalizedEvent,
        now: datetime,
    ) -> DirectionEstimate | None:
        if (
            primary.event_type not in _SEMANTIC_ACTIVITY_TYPES
            or primary.episode_id is None
        ):
            return None
        candidates = tuple(
            item.direction
            for item in events
            if item.episode_id == primary.episode_id
            and item.direction is not None
            and item.direction.is_fresh_at(now)
        )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.confidence, item.observed_at))

    @staticmethod
    def _direction_text(
        direction: DirectionEstimate | None,
        sensors: tuple[SensorState, ...],
    ) -> str:
        if direction is not None:
            low = (direction.bearing_deg - direction.uncertainty_deg) % 360.0
            high = (direction.bearing_deg + direction.uncertainty_deg) % 360.0
            return (
                f"Источник в секторе {low:.0f}–{high:.0f}° "
                f"(азимут {direction.bearing_deg:.0f}°). Дальность не измеряется."
            )
        direction_states = tuple(
            item
            for item in sensors
            if item.sensor_kind is SensorKind.DIRECTION_FINDER
        )
        if not direction_states or all(
            item.availability
            in {SensorAvailability.UNAVAILABLE, SensorAvailability.STALE}
            for item in direction_states
        ):
            return (
                "Пеленгация недоступна: KrakenSDR или другой внешний "
                "пеленгатор не подключён."
            )
        return "Пеленгатор подключён, но свежего валидного азимута пока нет."

    def _quiet_or_limited(
        self,
        sensors: tuple[SensorState, ...],
        recent: tuple[NormalizedEvent, ...],
        now: datetime,
    ) -> OperatorSituation:
        rf_states = tuple(
            item
            for item in sensors
            if item.sensor_kind
            in {SensorKind.RF_TRIGGER, SensorKind.RF_SPECTRUM}
        )
        rf_available = any(
            item.availability is SensorAvailability.AVAILABLE
            for item in rf_states
        )
        if rf_available:
            headline = "Активных событий нет"
            explanation = (
                "Приёмник доступен, но свежего нормализованного решения о "
                "фоне пока нет. Чистый фон не подтверждён."
            )
            recommendation = "Дождитесь свежей оценки RF-фона."
            severity = EventSeverity.NOTICE
            mode = OperatorSituationMode.SILENCE
        else:
            headline = "Наблюдение ограничено"
            explanation = (
                "Нет доступного RF-приёмника, поэтому система не может "
                "подтвердить, что эфир чист."
            )
            recommendation = (
                "Подключите RTL-SDR, TinySA или совместимый приёмник и "
                "проверьте страницу устройств."
            )
            severity = EventSeverity.WARNING
            mode = OperatorSituationMode.SILENCE
        return OperatorSituation(
            generated_at=now,
            mode=mode,
            headline_ru=headline,
            explanation_ru=explanation,
            severity=severity,
            confidence=ConfidenceScore.unavailable(
                "Активных классифицируемых признаков нет."
            ),
            direction_ru=self._direction_text(None, sensors),
            direction=None,
            recommendation_ru=recommendation,
            primary_event=None,
            recent_events=recent,
            sensors=sensors,
            limitations=(),
        )


__all__ = ["HumanReadableInterpreter"]
