"""Deterministic, civilian-safety recommendations for fused targets."""

# ruff: noqa: RUF001

from __future__ import annotations

from .models import (
    ConfirmationStage,
    PhenomenologicalType,
    TargetLifecycle,
    TargetRecommendation,
)


class TargetRecommendationEngine:
    """Build concise operator guidance without inventing missing capability."""

    def recommend(
        self,
        *,
        stage: ConfirmationStage,
        probable_type: PhenomenologicalType,
        direction_available: bool,
        lifecycle: TargetLifecycle,
    ) -> TargetRecommendation:
        if lifecycle is TargetLifecycle.TOMBSTONED:
            return TargetRecommendation(
                code="TARGET.OBSERVATION_CLOSED",
                short_ru="Наблюдение завершено",
                detailed_ru=(
                    "Запись сохранена для аудита и не является текущей целью. "
                    "Не принимайте оперативное решение по этой tombstone-записи."
                ),
            )
        if lifecycle is TargetLifecycle.STALE:
            return TargetRecommendation(
                code="TARGET.REACQUIRE_STALE",
                short_ru="Не используйте устаревшую цель как текущую",
                detailed_ru=(
                    "Свежие подтверждающие данные отсутствуют. Дождитесь нового "
                    "события или повторно получите независимое подтверждение."
                ),
            )
        if lifecycle is TargetLifecycle.HOLDING:
            return TargetRecommendation(
                code="TARGET.WAIT_FOR_FRESH_EVIDENCE",
                short_ru="Дождитесь свежего подтверждающего наблюдения",
                detailed_ru=(
                    "Срок действия подтверждающих признаков истёк. Цель "
                    "удерживается только для непрерывности журнала и не является "
                    "текущей подтверждённой целью."
                ),
            )
        if stage is ConfirmationStage.BACKGROUND:
            return TargetRecommendation(
                code="TARGET.CONTINUE_MONITORING",
                short_ru="Продолжайте наблюдение",
                detailed_ru=(
                    "Активная цель не сформирована. Сохраняйте наблюдение и "
                    "учитывайте текущие ограничения доступности сенсоров."
                ),
            )
        if stage is ConfirmationStage.SUSPICIOUS_ACTIVITY:
            return TargetRecommendation(
                code="TARGET.WAIT_FOR_CORROBORATION",
                short_ru="Дождитесь повторения и независимого подтверждения",
                detailed_ru=(
                    "Не делайте вывод о типе объекта по одному эпизоду. "
                    + _direction_clause(direction_available)
                ),
            )
        if stage is ConfirmationStage.LIKELY_SOURCE:
            if probable_type is PhenomenologicalType.UNKNOWN_ACTIVITY:
                return TargetRecommendation(
                    code="TARGET.RESOLVE_CLASSIFICATION_CONFLICT",
                    short_ru="Дождитесь разрешения конфликта классификации",
                    detailed_ru=(
                        "Свежие классификации источника противоречат друг другу. "
                        "Не выбирайте тип по одному из конфликтующих событий; "
                        "дождитесь повторного или независимого наблюдения. "
                        + _direction_clause(direction_available)
                    ),
                )
            if probable_type is PhenomenologicalType.HANDHELD_RADIO_LIKE:
                return TargetRecommendation(
                    code="TARGET.CHECK_AUTHORIZED_RADIOS",
                    short_ru="Сверьте работу разрешённых радиосредств",
                    detailed_ru=(
                        "Сопоставьте время события с журналом разрешённой "
                        "радиосвязи и продолжайте наблюдение. "
                        + _direction_clause(direction_available)
                    ),
                )
            return TargetRecommendation(
                code="TARGET.CHECK_INDEPENDENT_SENSOR",
                short_ru="Проверьте источник независимым сенсором",
                detailed_ru=(
                    "Активность устойчива, но физический тип источника ещё не "
                    "установлен. "
                    + _direction_clause(direction_available)
                ),
            )
        if stage is ConfirmationStage.LIKELY_TARGET:
            return TargetRecommendation(
                code="TARGET.VISUAL_CONFIRMATION",
                short_ru="Получите визуальное или иное независимое подтверждение",
                detailed_ru=(
                    "Есть валидированный признак вероятной цели, но данных ещё "
                    "недостаточно для окончательного вывода. "
                    + _direction_clause(direction_available)
                ),
            )
        return TargetRecommendation(
            code="TARGET.EXECUTE_SITE_SAFETY_PLAN",
            short_ru="Выполните утверждённый план безопасности объекта",
            detailed_ru=(
                "Цель подтверждена требуемыми независимыми данными. Следуйте "
                "утверждённым гражданским процедурам оповещения и безопасности; "
                "сохраняйте наблюдение и журнал событий."
            ),
        )


def _direction_clause(direction_available: bool) -> str:
    if direction_available:
        return (
            "Осмотрите указанный сектор доступным разрешённым средством; "
            "пеленг не определяет дальность."
        )
    return (
        "Направление не определено: нужен свежий валидированный внешний "
        "пеленг или другой независимый источник."
    )


__all__ = ["TargetRecommendationEngine"]
