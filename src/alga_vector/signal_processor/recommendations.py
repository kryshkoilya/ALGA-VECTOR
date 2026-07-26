"""Short, deterministic operator recommendations."""

# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import ClassVar

from .schema import NormalizedEvent, NormalizedEventType


@dataclass(frozen=True, slots=True)
class OperatorRecommendation:
    code: str
    action_ru: str


class RecommendationEngine:
    _RECOMMENDATIONS: ClassVar[
        dict[NormalizedEventType, OperatorRecommendation]
    ] = {
        NormalizedEventType.NOISE_BACKGROUND: OperatorRecommendation(
            "OP.CONTINUE_MONITORING",
            "Продолжайте наблюдение; действий не требуется.",
        ),
        NormalizedEventType.RADIO_ACTIVITY_DETECTED: OperatorRecommendation(
            "OP.OBSERVE_AND_CONFIRM",
            "Наблюдайте за эпизодом и дождитесь повторения; не определяйте источник по одной частоте.",
        ),
        NormalizedEventType.LIKELY_HANDHELD_RADIO: OperatorRecommendation(
            "OP.CHECK_LOCAL_RADIOS",
            "Сверьте время с работой разрешённых раций и продолжайте наблюдение.",
        ),
        NormalizedEventType.LIKELY_VIDEO_LINK: OperatorRecommendation(
            "OP.CAMERA_CONFIRMATION",
            "Проверьте указанный сектор камерой или вторым независимым сенсором.",
        ),
        NormalizedEventType.LIKELY_DRONE_SIGNATURE: OperatorRecommendation(
            "OP.DRONE_SAFETY_CHECK",
            "Подтвердите объект камерой и действуйте по утверждённому плану безопасности объекта.",
        ),
        NormalizedEventType.ADSB_CONTACT: OperatorRecommendation(
            "OP.CIVIL_CONTEXT",
            "Учитывайте контакт только как гражданский кооперативный контекст; это не оценка угрозы.",
        ),
        NormalizedEventType.ACOUSTIC_ANOMALY: OperatorRecommendation(
            "OP.ACOUSTIC_CONFIRMATION",
            "Проверьте направление камерой и дождитесь независимого RF- или визуального подтверждения.",
        ),
        NormalizedEventType.DIRECTION_ESTIMATED: OperatorRecommendation(
            "OP.INSPECT_SECTOR",
            "Осмотрите показанный сектор; пеленг указывает направление, но не тип и не дальность источника.",
        ),
        NormalizedEventType.MULTISENSOR_CORRELATED: OperatorRecommendation(
            "OP.MULTISENSOR_CONFIRMATION",
            "Проверьте указанный сектор доступным независимым средством; корреляция подтверждает активность, но не тип объекта.",
        ),
        NormalizedEventType.TARGET_CONFIRMED: OperatorRecommendation(
            "OP.EXECUTE_SITE_PROCEDURE",
            "Выполните утверждённый план безопасности объекта и сохраняйте наблюдение.",
        ),
        NormalizedEventType.SENSOR_UNAVAILABLE: OperatorRecommendation(
            "OP.RESTORE_SENSOR",
            "Проверьте питание, кабель и драйвер устройства; до восстановления учитывайте ограничение покрытия.",
        ),
    }

    def recommend(self, event: NormalizedEvent) -> OperatorRecommendation:
        return self._RECOMMENDATIONS[event.event_type]

    def enrich(self, event: NormalizedEvent) -> NormalizedEvent:
        recommendation = self.recommend(event)
        if event.recommendation_ru == recommendation.action_ru:
            return event
        return replace(event, recommendation_ru=recommendation.action_ru)


__all__ = ["OperatorRecommendation", "RecommendationEngine"]
