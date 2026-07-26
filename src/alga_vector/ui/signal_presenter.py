"""Plain-language presentation of conservative signal assessments."""

from __future__ import annotations

from dataclasses import dataclass

# ruff: noqa: RUF001
from .runtime import attr, value_of


@dataclass(frozen=True, slots=True)
class GuidedSignalView:
    """UI-only copy that cannot be mistaken for an emitter classification."""

    state: str
    headline: str
    observation: str
    coverage: str
    reasons: tuple[str, ...]
    trust: str
    attribution_answer: str
    next_action: str
    lifecycle: str = ""
    data_quality: str = ""
    evidence_strength: str = ""
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()
    missing_confirmation: tuple[str, ...] = ()
    sensor_contributions: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RfDecisionView:
    """Stable, non-attributive UI projection of a temporal RF decision."""

    episode_id: str
    source_id: str
    lifecycle: str
    lifecycle_label: str
    family: str
    family_label: str
    summary: str
    peak_frequency_hz: float | None
    occupied_bandwidth_hz: float | None
    data_quality: str
    data_quality_label: str
    evidence_strength: str
    evidence_strength_label: str
    heuristic_score: float
    alertable: bool
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    missing_confirmation: tuple[str, ...]
    sensor_contributions: tuple[str, ...]
    alternatives: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


_STATE_REASON_RU = {
    "no_data": "Измеренный кадр спектра ещё не получен.",
    "learning_background": "Система собирает обычный фон выбранного диапазона.",
    "background_only": "Новых устойчивых изменений относительно изученного фона не найдено.",
    "data_unreliable": "Качество данных пока не позволяет делать устойчивый вывод.",
    "concentrated_rf": "Изменение занимает небольшой участок выбранного диапазона.",
    "wideband_rf": "Изменение затронуло значительную часть выбранного диапазона.",
    "transient_burst": "Изменение появилось резко и наблюдалось недолго.",
    "unclassified_rf": "Изменение есть, но его форма пока неоднозначна.",
}

_FLAG_RU = {
    "absolute_calibration_unverified": "Абсолютный уровень не откалиброван.",
    "insufficient_history": "Нужно накопить больше истории фона.",
    "dropped_frames_reported": "Приёмник сообщил о пропущенных кадрах.",
    "sequence_gap": "В последовательности измерений есть разрыв.",
    "data_stale": "Последний кадр устарел.",
    "clock_regression": "Временные метки источника идут непоследовательно.",
    "spectral_grid_changed": "Диапазон изменился, поэтому фон изучается заново.",
}

_QUALITY_FLAG_PRIORITY = (
    "data_stale",
    "clock_regression",
    "sequence_gap",
    "dropped_frames_reported",
    "spectral_grid_changed",
    "insufficient_history",
    "absolute_calibration_unverified",
)

_TRUST_RU = {
    "low": "Предварительно: данных мало или их качество снижено.",
    "medium": "Достаточно для описания изменения, но не для определения источника.",
    "high": "Достаточно для описания формы сигнала, но не для определения источника.",
}

_DEFAULT_ATTRIBUTION = (
    "Нет. Текущие данные спектра сами по себе не устанавливают физический "
    "источник или класс объекта; они также не измеряют расстояние, направление "
    "или приближение."
)

_UNSAFE_IDENTITY_MARKERS = (
    "дрон",
    "бпла",
    "раци",
    "точно распозн",
    "точно идентифиц",
    "цель обнаружена",
)

_NEUTRALIZED_LEGACY_TEXT = (
    "Категоричная формулировка из сохранённой записи скрыта: "
    "класс объекта по этим RF-данным не устанавливается."
)

_LIFECYCLE_RU = {
    "idle": "Фон: устойчивого RF-эпизода нет",
    "candidate": "Проверяется: подтверждений пока недостаточно",
    "confirmed": "Подтверждён устойчивый RF-эпизод",
    "holding": "Подтверждённый эпизод временно ослаб",
    "resolved": "Эпизод завершён",
    "suppressed": "Короткий эпизод не подтверждён",
    "data_hold": "Решение приостановлено из-за качества данных",
}

_LIFECYCLE_EXPLANATION_RU = {
    "idle": "Новых устойчивых изменений относительно изученного фона нет.",
    "candidate": (
        "Изменение замечено, но система ждёт повторения и достаточной длительности."
    ),
    "confirmed": (
        "Изменение прошло временную проверку, порог входа и защиту от одиночного импульса."
    ),
    "holding": (
        "Ранее подтверждённое изменение сейчас ниже порога удержания; "
        "система ждёт устойчивого завершения."
    ),
    "resolved": "Изменение устойчиво прекратилось, эпизод сохранён в истории.",
    "suppressed": (
        "Изменение не прошло временную проверку или похоже на одиночный шумовой всплеск."
    ),
    "data_hold": (
        "Ненадёжный поток не изменяет решение до появления свежих непрерывных данных."
    ),
}

_FAMILY_RU = {
    "background": "Фон без выраженного изменения",
    "carrier": "Непрерывная несущая или аппаратная линия",
    "narrowband_burst": "Узкополосный RF-эпизод",
    "broadband_burst": "Широкополосный RF-эпизод",
    "packet_like": "Пакетоподобный цифровой обмен",
    "digital_like": "Цифроподобный RF-обмен",
    "voice_like": "Голосоподобный узкополосный канал",
    "periodic_beacon_like": "Периодическая beacon-like форма",
    "interference_noise_like": "Шумоподобная RF-помеха",
    "voice_like_compatible": "Голосоподобный узкополосный канал",
    "continuous_carrier_or_spur": "Непрерывная несущая или аппаратная линия",
    "burst_digital_or_telemetry_like": "Пакетоподобный цифровой обмен",
    "wideband_or_interference": "Широкополосная RF-помеха или передача",
    "impulse_or_local_interference": "Импульс или локальная помеха",
    "unknown": "RF-источник не классифицирован",
}

_FAMILY_EXPLANATION_RU = {
    "background": "Выраженного изменения относительно изученного фона нет.",
    "carrier": (
        "Устойчивая узкополосная линия подтверждена во времени; "
        "она может быть несущей или аппаратным spur."
    ),
    "narrowband_burst": (
        "Ограниченный во времени узкополосный эпизод подтверждён несколькими кадрами."
    ),
    "broadband_burst": (
        "Ограниченный во времени рост энергии наблюдался в широкой части полосы."
    ),
    "packet_like": (
        "Активность повторяется с паузами и совместима с цифровым пакетным "
        "обменом; протокол и тип передатчика не установлены."
    ),
    "digital_like": (
        "Измеренная форма совместима с цифровой передачей; протокол и тип "
        "передатчика не установлены."
    ),
    "voice_like": (
        "Изменяющаяся узкополосная огибающая совместима с голосовой "
        "радиосвязью, в том числе с радиостанцией; это не подтверждает рацию "
        "или тип передатчика."
    ),
    "periodic_beacon_like": (
        "Не менее трёх эпизодов повторились с близким временным интервалом."
    ),
    "interference_noise_like": (
        "Широкая или многокомпонентная шумоподобная форма сохраняется во "
        "времени и больше похожа на помеху; источник помехи не установлен."
    ),
    "voice_like_compatible": (
        "Признаки совместимы с голосовой радиосвязью, в том числе с "
        "радиостанцией; это не подтверждает рацию или тип передатчика."
    ),
    "continuous_carrier_or_spur": (
        "Признаки совместимы с устойчивой несущей или аппаратной спектральной линией."
    ),
    "burst_digital_or_telemetry_like": (
        "Признаки совместимы с пакетным цифровым или телеметрическим обменом; "
        "протокол и тип передатчика не установлены."
    ),
    "wideband_or_interference": (
        "Изменилась широкая часть полосы: возможны передача, помеха "
        "или изменение радиотракта."
    ),
    "impulse_or_local_interference": (
        "Краткий импульс совместим с локальной помехой или одиночным радиопакетом."
    ),
    "unknown": (
        "Изменение подтверждено во времени, но измеренных признаков "
        "недостаточно для устойчивого RF-класса."
    ),
}

_DATA_QUALITY_RU = {
    "low": "низкое: поток ненадёжен или истории недостаточно",
    "medium": "среднее: есть ограничения потока",
    "high": "высокое: поток свежий и непрерывный",
}

_EVIDENCE_STRENGTH_RU = {
    "low": "слабая: нужно больше подтверждений",
    "medium": "средняя: часть признаков подтверждена",
    "high": "высокая: признаки устойчивы во времени",
}


def present_rf_decision(decision: object | None) -> RfDecisionView | None:
    """Project a temporal decision without turning compatibility into identity."""

    if decision is None:
        return None
    lifecycle = value_of(attr(decision, "lifecycle", "")).lower()
    if not lifecycle:
        return None
    family = value_of(attr(decision, "family", "unknown")).lower()
    data_quality = value_of(attr(decision, "data_quality", "low")).lower()
    evidence_strength = value_of(
        attr(decision, "evidence_strength", "low")
    ).lower()
    score = _as_float(attr(decision, "heuristic_score"))
    return RfDecisionView(
        episode_id=str(attr(decision, "episode_id", "") or ""),
        source_id=str(attr(decision, "source_id", "") or ""),
        lifecycle=lifecycle,
        lifecycle_label=_LIFECYCLE_RU.get(
            lifecycle, "Состояние RF-эпизода неизвестно"
        ),
        family=family,
        family_label=_FAMILY_RU.get(family, _FAMILY_RU["unknown"]),
        # UI text is controlled locally.  A legacy journal may contain an old
        # free-form explanation, but it must not become an identity claim.
        summary=_FAMILY_EXPLANATION_RU.get(
            family,
            _FAMILY_EXPLANATION_RU["unknown"],
        ),
        peak_frequency_hz=_as_float(attr(decision, "peak_frequency_hz")),
        occupied_bandwidth_hz=_as_float(
            attr(decision, "occupied_bandwidth_hz")
        ),
        data_quality=data_quality,
        data_quality_label=_DATA_QUALITY_RU.get(
            data_quality, _DATA_QUALITY_RU["low"]
        ),
        evidence_strength=evidence_strength,
        evidence_strength_label=_EVIDENCE_STRENGTH_RU.get(
            evidence_strength, _EVIDENCE_STRENGTH_RU["low"]
        ),
        heuristic_score=score if score is not None else 0.0,
        alertable=bool(attr(decision, "alertable", False)),
        supporting_evidence=_evidence_texts(
            attr(decision, "supporting_evidence", ())
        ),
        contradicting_evidence=_evidence_texts(
            attr(decision, "contradicting_evidence", ())
        ),
        missing_confirmation=_evidence_texts(
            attr(decision, "missing_confirmation", ())
        ),
        sensor_contributions=_sensor_contribution_texts(
            attr(decision, "sensor_contributions", ())
        ),
        alternatives=_alternative_texts(attr(decision, "alternatives", ())),
        limitations=_evidence_texts(attr(decision, "limitations", ())),
    )


def present_signal_assessment(snapshot: object | None) -> GuidedSignalView:
    """Turn a domain assessment into stable novice-facing text.

    The presenter deliberately ignores any future emitter label. Attribution
    requires capabilities that a single power-spectrum receiver does not have.
    """

    decision_view = present_rf_decision(attr(snapshot, "signal_decision"))
    if decision_view is not None:
        return _guided_from_decision(snapshot, decision_view)

    assessment = attr(snapshot, "signal_assessment")
    if assessment is None:
        return GuidedSignalView(
            state="no_data",
            headline="Измеренных данных пока нет",
            observation="Система ждёт первый проверенный кадр от приёмника.",
            coverage=_coverage_from_frame(attr(snapshot, "spectrum")),
            reasons=(_STATE_REASON_RU["no_data"],),
            trust=_TRUST_RU["low"],
            attribution_answer=_DEFAULT_ATTRIBUTION,
            next_action="Подключите приёмник или проверьте его состояние.",
        )

    state = value_of(attr(assessment, "state", "no_data")).lower()
    evidence = attr(assessment, "evidence")
    headline = _safe_visible_text(attr(assessment, "headline_ru", ""))
    observation = _safe_visible_text(attr(assessment, "explanation_ru", ""))
    next_action = _safe_visible_text(attr(assessment, "operator_action_ru", ""))
    reasons: list[str] = [_STATE_REASON_RU.get(state, _STATE_REASON_RU["unclassified_rf"])]
    limitations = _plain_texts(attr(evidence, "limitations", ()))
    reasons.extend(f"Ограничение: {item}" for item in limitations)

    for flag_key in _ordered_quality_flag_keys(
        attr(assessment, "quality_flags", ())
    ):
        translated = _FLAG_RU.get(
            flag_key,
            "Есть дополнительное ограничение качества данных.",
        )
        if translated not in reasons:
            reasons.append(translated)

    baseline_frames = _as_int(attr(evidence, "baseline_frames"))
    baseline_required = _as_int(attr(evidence, "baseline_required_frames"))
    if state == "learning_background" and baseline_required:
        reasons.append(
            f"Фон изучен по {baseline_frames or 0} из {baseline_required} нужных кадров."
        )

    peak_frequency = _as_float(attr(evidence, "peak_frequency_hz"))
    occupied_bandwidth = _as_float(attr(evidence, "occupied_bandwidth_hz"))
    if peak_frequency is not None and state not in {
        "no_data",
        "learning_background",
        "background_only",
    }:
        reasons.append(f"Самое заметное изменение — около {_frequency(peak_frequency)}.")
    if occupied_bandwidth is not None and occupied_bandwidth > 0:
        reasons.append(f"Измеренная занятая полоса — примерно {_frequency(occupied_bandwidth)}.")

    trust_key = value_of(attr(assessment, "trust", "low")).lower()
    return GuidedSignalView(
        state=state,
        headline=headline or _fallback_headline(state),
        observation=observation or _STATE_REASON_RU.get(
            state, _STATE_REASON_RU["unclassified_rf"]
        ),
        coverage=_coverage_from_evidence(evidence)
        or _coverage_from_frame(attr(snapshot, "spectrum")),
        reasons=tuple(reasons),
        trust=_TRUST_RU.get(trust_key, _TRUST_RU["low"]),
        attribution_answer=_DEFAULT_ATTRIBUTION,
        next_action=next_action or _fallback_action(state),
        limitations=limitations,
    )


def _guided_from_decision(
    snapshot: object | None,
    decision: RfDecisionView,
) -> GuidedSignalView:
    reasons = [f"Состояние: {decision.lifecycle_label}."]
    reasons.extend(f"За: {item}" for item in decision.supporting_evidence)
    reasons.extend(f"Против: {item}" for item in decision.contradicting_evidence)
    reasons.extend(
        f"Не хватает: {item}" for item in decision.missing_confirmation
    )
    reasons.extend(
        f"Вклад сенсора: {item}" for item in decision.sensor_contributions
    )
    reasons.extend(f"Альтернатива: {item}" for item in decision.alternatives)
    reasons.extend(f"Ограничение: {item}" for item in decision.limitations)
    if decision.peak_frequency_hz is not None:
        reasons.append(
            "Самое заметное изменение — около "
            f"{_frequency(decision.peak_frequency_hz)}."
        )
    if (
        decision.occupied_bandwidth_hz is not None
        and decision.occupied_bandwidth_hz > 0.0
    ):
        reasons.append(
            "Измеренная занятая полоса — примерно "
            f"{_frequency(decision.occupied_bandwidth_hz)}."
        )

    assessment = attr(snapshot, "signal_assessment")
    coverage = _coverage_from_evidence(attr(assessment, "evidence"))
    lifecycle_explanation = _LIFECYCLE_EXPLANATION_RU.get(
        decision.lifecycle,
        "Система продолжает временную проверку RF-признаков.",
    )
    return GuidedSignalView(
        state=decision.lifecycle,
        headline=_decision_headline(decision),
        observation=f"{lifecycle_explanation} {decision.summary}",
        coverage=coverage or _coverage_from_frame(attr(snapshot, "spectrum")),
        reasons=tuple(reasons),
        trust=(
            f"Качество данных — {decision.data_quality_label}. "
            f"Сила RF-признаков — {decision.evidence_strength_label}. "
            "Это эвристическая оценка, не вероятность идентификации."
        ),
        attribution_answer=_DEFAULT_ATTRIBUTION,
        next_action=_decision_action(decision.lifecycle),
        lifecycle=decision.lifecycle_label,
        data_quality=decision.data_quality_label,
        evidence_strength=decision.evidence_strength_label,
        supporting_evidence=decision.supporting_evidence,
        contradicting_evidence=decision.contradicting_evidence,
        missing_confirmation=decision.missing_confirmation,
        sensor_contributions=decision.sensor_contributions,
        alternatives=decision.alternatives,
        limitations=decision.limitations,
    )


def _decision_headline(decision: RfDecisionView) -> str:
    if decision.lifecycle == "confirmed":
        return f"Устойчивый RF-эпизод: {decision.family_label.lower()}"
    if decision.lifecycle == "holding":
        return "Подтверждённый RF-эпизод временно ослаб"
    if decision.lifecycle == "candidate":
        return "RF-изменение проверяется"
    if decision.lifecycle == "resolved":
        return "RF-эпизод завершён"
    if decision.lifecycle == "suppressed":
        return "Короткий RF-эпизод не подтверждён"
    if decision.lifecycle == "data_hold":
        return "Решение приостановлено: проверьте поток"
    return "Устойчивых RF-изменений нет"


def _decision_action(lifecycle: str) -> str:
    if lifecycle == "confirmed":
        return (
            "Откройте журнал событий и продолжайте наблюдение. Для установления "
            "физического источника требуется независимое подтверждение."
        )
    if lifecycle == "holding":
        return (
            "Продолжайте наблюдение до завершения или повторного усиления эпизода."
        )
    if lifecycle == "candidate":
        return "Оставьте приёмник включённым: система проверяет повторяемость."
    if lifecycle == "data_hold":
        return "Проверьте приёмник и дождитесь свежих непрерывных данных."
    if lifecycle == "suppressed":
        return "Действий не требуется; повторный устойчивый эпизод будет проверен заново."
    if lifecycle == "resolved":
        return "Эпизод сохранён в журнале; продолжайте обычное наблюдение."
    return "Оставьте приёмник включённым для наблюдения за фоном."


def _evidence_texts(evidence_items: object) -> tuple[str, ...]:
    try:
        values: tuple[object, ...] = tuple(evidence_items)  # type: ignore[arg-type]
    except TypeError:
        values = ()
    result: list[str] = []
    for evidence in values:
        explanation = _safe_visible_text(attr(evidence, "explanation_ru", ""))
        if explanation:
            result.append(explanation)
    return tuple(result)


def _alternative_texts(alternatives: object) -> tuple[str, ...]:
    try:
        values: tuple[object, ...] = tuple(alternatives)  # type: ignore[arg-type]
    except TypeError:
        values = ()
    result: list[str] = []
    for alternative in values:
        family = value_of(attr(alternative, "family", "unknown")).lower()
        explanation = _safe_visible_text(
            attr(alternative, "explanation_ru", "")
        )
        label = _FAMILY_RU.get(family, _FAMILY_RU["unknown"])
        if explanation:
            result.append(f"{label}: {explanation}")
    return tuple(result)


def _sensor_contribution_texts(contributions: object) -> tuple[str, ...]:
    try:
        values: tuple[object, ...] = tuple(contributions)  # type: ignore[arg-type]
    except TypeError:
        values = ()
    result: list[str] = []
    for contribution in values:
        source_id = _safe_visible_text(
            attr(contribution, "source_id", "") or "RF-сенсор",
            fallback="RF-сенсор",
        )
        explanation = _safe_visible_text(
            attr(contribution, "explanation_ru", ""),
            fallback="Вклад сенсора учтён.",
        )
        weight = _as_float(attr(contribution, "contribution"))
        quality = value_of(attr(contribution, "data_quality", "low")).lower()
        independent = bool(
            attr(contribution, "independent_confirmation", False)
        )
        weight_text = (
            f"; вклад в эвристику {weight:.2f}" if weight is not None else ""
        )
        confirmation_text = (
            "; независимое подтверждение есть"
            if independent
            else "; независимого подтверждения нет"
        )
        result.append(
            f"{source_id}: {explanation}{weight_text}; качество данных "
            f"{_short_quality(quality)}{confirmation_text}"
        )
    return tuple(result)


def _plain_texts(values: object) -> tuple[str, ...]:
    try:
        raw_values: tuple[object, ...] = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        raw_values = ()
    result = tuple(
        text
        for item in raw_values
        if (text := _safe_visible_text(item))
    )
    return result


def _safe_visible_text(
    value: object,
    *,
    fallback: str = _NEUTRALIZED_LEGACY_TEXT,
) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.casefold()
    if any(marker in normalized for marker in _UNSAFE_IDENTITY_MARKERS):
        return fallback
    return text


def _short_quality(quality: str) -> str:
    return {
        "low": "низкое",
        "medium": "среднее",
        "high": "высокое",
    }.get(quality, "неизвестно")


def translate_quality_flags(flags: object) -> str:
    """Translate expert evidence flags without exposing internal identifiers."""

    translated = [
        _FLAG_RU.get(flag_key, "Неизвестное ограничение качества.")
        for flag_key in _ordered_quality_flag_keys(flags)
    ]
    return "; ".join(translated) or "Ограничений качества не отмечено"


def _ordered_quality_flag_keys(flags: object) -> tuple[str, ...]:
    try:
        raw_values: tuple[object, ...] = tuple(flags)  # type: ignore[arg-type]
    except TypeError:
        raw_values = ()
    keys = {value_of(flag).lower() for flag in raw_values}
    known = [flag for flag in _QUALITY_FLAG_PRIORITY if flag in keys]
    unknown = sorted(keys.difference(_QUALITY_FLAG_PRIORITY))
    return tuple((*known, *unknown))


def event_plain_meaning(classification: str) -> str:
    """Explain an event without implying emitter identity."""

    return {
        "narrowband_activity": (
            "Узкополосный сигнал совместим с голосовой или цифровой связью; "
            "источник не определён."
        ),
        "broadband_activity": (
            "Широкополосная передача или помеха; по одному спектру их не разделить."
        ),
        "transient_burst": (
            "Короткий радиопакет или импульсная помеха; нужно повторение."
        ),
        "impulsive_interference": (
            "Короткий радиопакет или импульсная помеха; нужно повторение."
        ),
        "unknown": "Изменение неоднозначно; данных для вывода об источнике нет.",
    }.get(
        classification,
        "Зафиксировано изменение спектра; источник не определён.",
    )


def _coverage_from_evidence(evidence: object | None) -> str:
    low = _as_float(attr(evidence, "coverage_low_hz"))
    high = _as_float(attr(evidence, "coverage_high_hz"))
    if low is None or high is None or high <= low:
        return ""
    return f"Система слушает {_frequency(low)} — {_frequency(high)}."


def _coverage_from_frame(frame: object | None) -> str:
    center = _as_float(attr(frame, "center_frequency_hz"))
    span = _as_float(attr(frame, "span_hz"))
    if center is None or span is None or span <= 0:
        return "Диапазон прослушивания появится после первого измеренного кадра."
    return (
        f"Система слушает {_frequency(center - span / 2)} — "
        f"{_frequency(center + span / 2)}."
    )


def _frequency(hz: float) -> str:
    if abs(hz) >= 1_000_000:
        return f"{hz / 1_000_000:.3f} МГц"
    if abs(hz) >= 1_000:
        return f"{hz / 1_000:.1f} кГц"
    return f"{hz:.0f} Гц"


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _fallback_headline(state: str) -> str:
    return {
        "no_data": "Измеренных данных пока нет",
        "learning_background": "Система изучает обычный фон",
        "background_only": "Заметных изменений не найдено",
        "data_unreliable": "Данные нужно проверить",
        "concentrated_rf": "Замечено узкое изменение в эфире",
        "wideband_rf": "Изменился широкий участок эфира",
        "transient_burst": "Зафиксирован короткий всплеск",
        "unclassified_rf": "Есть неоднозначное изменение",
    }.get(state, "Система продолжает наблюдение")


def _fallback_action(state: str) -> str:
    if state == "no_data":
        return "Проверьте подключение приёмника."
    if state in {"learning_background", "background_only"}:
        return "Оставьте приёмник включённым: система продолжит сравнение с фоном."
    if state == "data_unreliable":
        return "Проверьте приёмник и дождитесь свежих непрерывных данных."
    return "Наблюдайте: система проверит, сохраняется ли изменение."


__all__ = [
    "GuidedSignalView",
    "RfDecisionView",
    "event_plain_meaning",
    "present_rf_decision",
    "present_signal_assessment",
    "translate_quality_flags",
]
