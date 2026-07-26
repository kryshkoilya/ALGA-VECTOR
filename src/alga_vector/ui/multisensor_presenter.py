"""Conservative operator presentation for optional multi-sensor snapshots.

The presenter deliberately avoids object identity, intent, coordinates, and
range claims.  It only describes observable sensor state and temporal
correlation.  Runtime models are read structurally so an older snapshot keeps
working while the v0.6 fields are rolled out.
"""

# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from .runtime import attr, provenance_key, value_of


@dataclass(frozen=True, slots=True)
class SensorStatusView:
    """One compact, non-attributive sensor status."""

    key: str
    title: str
    state: str
    detail: str
    level: str


@dataclass(frozen=True, slots=True)
class MultiSensorView:
    """Dashboard/diagnostics projection of the optional v0.6 contract."""

    present: bool
    headline: str
    summary: str
    correlation: str
    quality: str
    missing: str
    level: str
    provenance: str
    sensors: tuple[SensorStatusView, ...]


_MODALITY_RU = {
    "rf": "RF",
    "acoustic": "акустика",
    "direction": "направление",
    "civil_adsb": "гражданский ADS-B",
}

_CLASSIFICATION_RU = {
    "background": (
        "Согласованных изменений не наблюдается",
        "Свежие подтверждающие признаки не сформировали общий эпизод.",
        "ready",
    ),
    "unconfirmed_anomaly": (
        "Изменение пока не подтверждено",
        "Есть отдельные признаки, но их недостаточно для многосенсорного подтверждения.",
        "warning",
    ),
    "rf_activity": (
        "RF-активность требует подтверждения",
        "RF-контур видит изменение; независимого акустического подтверждения пока нет.",
        "warning",
    ),
    "acoustic_anomaly": (
        "Акустическое изменение требует подтверждения",
        "Акустический контур видит изменение; независимого RF-подтверждения пока нет.",
        "warning",
    ),
    "multi_sensor_correlated": (
        "Согласованное многосенсорное наблюдение",
        "Независимые RF- и акустические признаки совпали во временном окне.",
        "warning",
    ),
    "nearby_cooperative_aircraft_context": (
        "Доступен гражданский эфирный контекст",
        "Получены свежие публичные ADS-B/Mode-S сообщения. Это только контекст.",
        "info",
    ),
}

_LIFECYCLE_RU = {
    "idle": "эпизод не активен",
    "informational": "информационный контекст",
    "candidate": "идёт проверка во времени",
    "confirmed": "временная корреляция подтверждена",
    "holding": "подтверждение удерживается гистерезисом",
    "resolved": "эпизод завершён",
}

_STRENGTH_RU = {
    "none": "признаков для оценки нет",
    "low": "сила признаков низкая",
    "medium": "сила признаков средняя",
    "high": "сила признаков высокая",
}

_MISSING_RU = {
    "FUSION.MORE_OBSERVATIONS_REQUIRED": (
        "Нужно больше последовательных качественных наблюдений."
    ),
    "FUSION.RF_CONFIRMATION_MISSING": "Не хватает свежего RF-подтверждения.",
    "FUSION.ACOUSTIC_CONFIRMATION_MISSING": (
        "Не хватает свежего акустического подтверждения."
    ),
}


def present_multisensor(snapshot: object | None) -> MultiSensorView:
    """Build a safe Russian view and gracefully handle partial snapshots."""

    present = _has_any_field(
        snapshot,
        ("acoustic", "airspace", "fusion_decision"),
    )
    acoustic = _unwrap(attr(snapshot, "acoustic"), "assessment")
    airspace = _unwrap(attr(snapshot, "airspace"), "summary")
    fusion = _fusion_decision(snapshot)
    direction = attr(snapshot, "direction")

    sensors = (
        _rf_status(snapshot),
        _acoustic_status(acoustic),
        _direction_status(direction),
        _airspace_status(airspace),
    )

    simulated = provenance_key(snapshot) in {"demo", "simulated"}
    provenance = (
        "СИНТЕТИЧЕСКИЕ ДАННЫЕ · ДЕМО"
        if simulated
        else "ИЗМЕРЕННЫЕ И ЛОКАЛЬНЫЕ ДАННЫЕ"
    )
    if fusion is None:
        return MultiSensorView(
            present=present,
            headline="Корреляция пока не рассчитана",
            summary=(
                "Сенсорные контуры показаны отдельно; общий вывод появится "
                "только после безопасной временной корреляции."
            ),
            correlation="Свежего решения ядра корреляции нет.",
            quality="Качество общего решения пока недоступно.",
            missing="Нужны свежие данные минимум от RF- и акустического контуров.",
            level="neutral",
            provenance=provenance,
            sensors=sensors,
        )

    classification = value_of(attr(fusion, "classification", "background")).lower()
    headline, summary, level = _CLASSIFICATION_RU.get(
        classification,
        (
            "Нейтральное сенсорное наблюдение",
            "Ядро вернуло общий класс без утверждения о физическом источнике.",
            "info",
        ),
    )
    lifecycle = value_of(attr(fusion, "lifecycle", "idle")).lower()
    modalities = _modalities(attr(fusion, "active_modalities", ()))
    correlation = _correlation_text(classification, lifecycle, modalities)
    strength = value_of(attr(fusion, "evidence_strength", "none")).lower()
    quality = (
        f"{_STRENGTH_RU.get(strength, 'сила признаков не определена')}; "
        f"{_LIFECYCLE_RU.get(lifecycle, 'состояние эпизода не определено')}."
    )
    missing = _missing_text(attr(fusion, "missing", ()), classification)

    if simulated:
        summary = f"{summary} Источник этого сценария — демо-симуляция."

    return MultiSensorView(
        present=present,
        headline=headline,
        summary=summary,
        correlation=correlation,
        quality=quality,
        missing=missing,
        level=level,
        provenance=provenance,
        sensors=sensors,
    )


def _has_any_field(snapshot: object | None, names: tuple[str, ...]) -> bool:
    if snapshot is None:
        return False
    if isinstance(snapshot, dict):
        return any(snapshot.get(name) is not None for name in names)
    return any(attr(snapshot, name) is not None for name in names)


def _fusion_decision(snapshot: object | None) -> object | None:
    raw = attr(snapshot, "fusion_decision")
    if raw is None:
        raw = attr(snapshot, "fusion")
    return _unwrap(raw, "decision")


def _unwrap(value: object | None, field: str) -> object | None:
    nested = attr(value, field)
    return nested if nested is not None else value


def _as_tuple(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, dict)) or value is None:
        return ()
    try:
        return tuple(cast(Any, value))
    except TypeError:
        return ()


def _modalities(value: object) -> tuple[str, ...]:
    ordered: list[str] = []
    for item in _as_tuple(value):
        key = value_of(item).lower()
        label = _MODALITY_RU.get(key)
        if label is not None and label not in ordered:
            ordered.append(label)
    return tuple(ordered)


def _correlation_text(
    classification: str,
    lifecycle: str,
    modalities: tuple[str, ...],
) -> str:
    if classification == "multi_sensor_correlated":
        return (
            "RF и акустика дали независимые качественные признаки в одном "
            "временном окне."
        )
    if modalities:
        listed = ", ".join(modalities)
        return (
            f"Сейчас участвуют: {listed}. Независимая многосенсорная "
            "корреляция ещё не сформирована."
        )
    if lifecycle == "resolved":
        return "Ранее наблюдавшийся эпизод завершён; свежего подтверждения нет."
    return "Свежих подтверждающих наблюдений для корреляции нет."


def _missing_text(value: object, classification: str) -> str:
    messages: list[str] = []
    for item in _as_tuple(value):
        code = str(attr(item, "code", "")).upper()
        message = _MISSING_RU.get(code)
        if message is not None and message not in messages:
            messages.append(message)
    if messages:
        return " ".join(messages[:2])
    if classification == "multi_sensor_correlated":
        return "Критичных недостающих подтверждений для текущего вывода нет."
    if classification == "background":
        return "Подтверждение не требуется, пока значимых изменений нет."
    return "Нужно независимое подтверждение и устойчивость признаков во времени."


def _rf_status(snapshot: object | None) -> SensorStatusView:
    frame = attr(snapshot, "spectrum")
    assessment = attr(snapshot, "signal_assessment")
    state = value_of(attr(assessment, "state", "no_data")).lower()
    if state == "data_unreliable":
        return SensorStatusView(
            "rf",
            "RF",
            "ДАННЫЕ НЕНАДЁЖНЫ",
            "Кадр отклонён проверками качества; подтверждение не формируется.",
            "critical",
        )
    if frame is not None:
        return SensorStatusView(
            "rf",
            "RF",
            "ДАННЫЕ ПОСТУПАЮТ",
            "Спектральный кадр принят и проходит объяснимую интерпретацию.",
            "ready",
        )
    devices = _as_tuple(attr(snapshot, "devices", ()))
    if devices:
        return SensorStatusView(
            "rf",
            "RF",
            "ОЖИДАНИЕ КАДРА",
            "Приёмник известен, но свежего спектрального кадра пока нет.",
            "warning",
        )
    return SensorStatusView(
        "rf",
        "RF",
        "НЕ НАСТРОЕН",
        "Приёмник не подключён.",
        "neutral",
    )


def _acoustic_status(acoustic: object | None) -> SensorStatusView:
    if acoustic is None:
        return SensorStatusView(
            "acoustic",
            "Акустика",
            "НЕ НАСТРОЕНА",
            "Свежая оценка PCM не поступала.",
            "neutral",
        )
    lifecycle = value_of(attr(acoustic, "lifecycle", "idle")).lower()
    quality = value_of(attr(acoustic, "data_quality", "low")).lower()
    alertable = bool(attr(acoustic, "alertable", False))
    if lifecycle == "data_hold":
        return SensorStatusView(
            "acoustic",
            "Акустика",
            "ДАННЫЕ УДЕРЖАНЫ",
            "Окно отклонено или поток прерван; накопленное подтверждение сброшено.",
            "critical",
        )
    if alertable:
        return SensorStatusView(
            "acoustic",
            "Акустика",
            "ИЗМЕНЕНИЕ ПОДТВЕРЖДЕНО",
            f"Временной фильтр пройден; качество данных: {_quality_ru(quality)}.",
            "warning",
        )
    if lifecycle == "candidate":
        return SensorStatusView(
            "acoustic",
            "Акустика",
            "ИДЁТ ПРОВЕРКА",
            f"Нужно продолжение во времени; качество данных: {_quality_ru(quality)}.",
            "info",
        )
    return SensorStatusView(
        "acoustic",
        "Акустика",
        "ФОН",
        f"Подтверждённого изменения нет; качество данных: {_quality_ru(quality)}.",
        "ready",
    )


def _direction_status(direction: object | None) -> SensorStatusView:
    if direction is None:
        return SensorStatusView(
            "direction",
            "Направление",
            "НЕТ ИСТОЧНИКА",
            "Угловой контекст не участвует в подтверждении.",
            "neutral",
        )
    current = _unwrap(direction, "current")
    available = bool(attr(direction, "available", attr(current, "available", False)))
    stale = bool(attr(direction, "stale", False))
    source = value_of(attr(current, "source", "unavailable")).lower()
    if stale:
        return SensorStatusView(
            "direction",
            "Направление",
            "ДАННЫЕ УСТАРЕЛИ",
            "Старое угловое наблюдение не используется.",
            "warning",
        )
    if source == "simulated":
        return SensorStatusView(
            "direction",
            "Направление",
            "СИМУЛЯЦИЯ",
            "Демонстрационный угловой контекст не считается измерением.",
            "warning",
        )
    if source == "manual":
        return SensorStatusView(
            "direction",
            "Направление",
            "РУЧНОЙ КОНТЕКСТ",
            "Отметка оператора показана отдельно и не подтверждает эпизод.",
            "info",
        )
    if available and source == "external":
        return SensorStatusView(
            "direction",
            "Направление",
            "ВАЛИДИРОВАНО",
            "Свежий внешний угловой контекст доступен, но не подтверждает эпизод.",
            "ready",
        )
    return SensorStatusView(
        "direction",
        "Направление",
        "НЕТ ИСТОЧНИКА",
        "Валидированное угловое наблюдение отсутствует.",
        "neutral",
    )


def _airspace_status(airspace: object | None) -> SensorStatusView:
    if airspace is None:
        return SensorStatusView(
            "civil_adsb",
            "Гражданский ADS-B",
            "НЕ НАСТРОЕН",
            "Локальный публичный эфирный контекст не подключён.",
            "neutral",
        )
    state = value_of(attr(airspace, "state", "no_data")).lower()
    quality = value_of(attr(airspace, "data_quality", "unavailable")).lower()
    count = max(0, _as_int(attr(airspace, "active_count", 0)))
    if state == "current":
        noun = _record_noun(count)
        return SensorStatusView(
            "civil_adsb",
            "Гражданский ADS-B",
            "КОНТЕКСТ АКТУАЛЕН",
            (
                f"Свежих публичных сообщений: {count} {noun}; "
                f"качество: {_quality_ru(quality)}. Это не идентификация."
            ),
            "info",
        )
    if state in {"stale", "invalid", "io_error"}:
        return SensorStatusView(
            "civil_adsb",
            "Гражданский ADS-B",
            "НЕТ АКТУАЛЬНЫХ ДАННЫХ",
            "Источник устарел или недоступен; прежний контекст не используется.",
            "warning",
        )
    return SensorStatusView(
        "civil_adsb",
        "Гражданский ADS-B",
        "ОЖИДАНИЕ ДАННЫХ",
        "Свежих публичных сообщений пока нет; это не доказывает пустое воздушное пространство.",
        "neutral",
    )


def _quality_ru(value: str) -> str:
    return {
        "unavailable": "недоступно",
        "low": "низкое",
        "limited": "ограниченное",
        "medium": "среднее",
        "partial": "частичное",
        "high": "высокое",
        "good": "хорошее",
        "simulated": "симуляция",
    }.get(value, "не определено")


def _as_int(value: object) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _record_noun(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "запись"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "записи"
    return "записей"
