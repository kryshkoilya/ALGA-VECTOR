"""Explainable, non-attributive notifications for observed RF changes."""

# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass

from .multisensor_presenter import present_multisensor
from .runtime import attr, provenance_key, value_of
from .signal_presenter import RfDecisionView, present_rf_decision


@dataclass(frozen=True, slots=True)
class SignalNotification:
    """A compact operator notification derived only from measured evidence."""

    active: bool
    key: str
    level: str
    title: str
    message: str
    details: str
    next_action: str
    target_page: str = "events"


_INACTIVE = SignalNotification(
    active=False,
    key="",
    level="info",
    title="",
    message="",
    details="",
    next_action="",
)

_IDENTITY_LIMIT = (
    "RF-признаки не устанавливают тип физического источника, расстояние "
    "или направление; для этого требуется независимое подтверждение."
)

_VOICE_FAMILIES = frozenset({"voice_like", "voice_like_compatible"})
_PACKET_FAMILIES = frozenset(
    {
        "packet_like",
        "digital_like",
        "burst_digital_or_telemetry_like",
        "periodic_beacon_like",
    }
)
_CARRIER_FAMILIES = frozenset({"carrier", "continuous_carrier_or_spur"})
_NARROW_BURST_FAMILIES = frozenset({"narrowband_burst"})
_BROAD_BURST_FAMILIES = frozenset({"broadband_burst"})
_INTERFERENCE_FAMILIES = frozenset(
    {
        "interference_noise_like",
        "wideband_or_interference",
        "impulse_or_local_interference",
    }
)


def build_signal_notification(snapshot: object | None) -> SignalNotification:
    """Alert only on a temporal decision explicitly marked as alertable."""

    operator_notification = _build_operator_notification(snapshot)
    if operator_notification.active:
        return operator_notification
    if _has_operator_contract(snapshot):
        # A present normalized contract is authoritative, including the
        # deliberate decision to show no banner.  Falling through to legacy
        # fusion/RF fields here could resurrect an expired or policy-rejected
        # event and make the banner disagree with Simple Mode.
        return _INACTIVE

    fusion_notification = _build_fusion_notification(snapshot)
    if fusion_notification.active:
        return fusion_notification

    decision = attr(snapshot, "signal_decision")
    view = present_rf_decision(decision)
    if (
        view is None
        or not bool(attr(decision, "alertable", False))
        or not view.alertable
        or view.lifecycle not in {"confirmed", "holding"}
        or not view.episode_id
    ):
        return _INACTIVE

    title, family_message = _notification_copy(view)
    measured = _measurement_text(view)
    details = _decision_details(view)
    return SignalNotification(
        active=True,
        key=view.episode_id,
        level="info" if view.lifecycle == "holding" else "warning",
        title=title,
        message=" ".join(part for part in (family_message, measured) if part),
        details=details,
        next_action=_next_action(view),
        target_page="events",
    )


def _has_operator_contract(snapshot: object | None) -> bool:
    if snapshot is None:
        return False
    if isinstance(snapshot, dict):
        return "operator_situation" in snapshot
    return hasattr(snapshot, "operator_situation")


def _build_operator_notification(
    snapshot: object | None,
) -> SignalNotification:
    """Build the banner from the same normalized event used by Simple Mode."""

    situation = attr(snapshot, "operator_situation")
    event = attr(situation, "primary_event")
    if event is None:
        return _INACTIVE
    event_type = value_of(attr(event, "event_type")).upper()
    severity = value_of(attr(event, "severity")).lower()
    if event_type not in {
        "RADIO_ACTIVITY_DETECTED",
        "LIKELY_HANDHELD_RADIO",
        "LIKELY_VIDEO_LINK",
        "LIKELY_DRONE_SIGNATURE",
        "ACOUSTIC_ANOMALY",
        "MULTISENSOR_CORRELATED",
        "TARGET_CONFIRMED",
    } or severity not in {"warning", "alarm", "critical"}:
        return _INACTIVE

    event_id = str(attr(event, "event_id", "") or "")
    if not event_id:
        return _INACTIVE
    title = str(
        attr(event, "summary_ru", "Подтверждённое операторское событие")
    )
    if provenance_key(snapshot) in {"demo", "simulated"}:
        title = f"ДЕМО · {title}"
    explanation = str(attr(event, "explanation_ru", "") or "")
    recommendation = str(
        attr(
            event,
            "recommendation_ru",
            "Откройте простую обстановку и проверьте доступные подтверждения.",
        )
    )
    confidence = attr(event, "confidence")
    confidence_basis = str(attr(confidence, "basis_ru", "") or "")
    limitations = tuple(
        str(item)
        for item in attr(event, "limitations", ())
        if str(item).strip()
    )
    sources = tuple(attr(event, "sources", ()) or ())
    source_text = ", ".join(
        str(attr(source, "sensor_id", "") or "")
        for source in sources
        if str(attr(source, "sensor_id", "") or "").strip()
    )
    details = " ".join(
        part
        for part in (
            explanation,
            f"Сила признаков: {confidence_basis}" if confidence_basis else "",
            f"Источники: {source_text}." if source_text else "",
            f"Ограничение: {limitations[0]}" if limitations else "",
            _IDENTITY_LIMIT,
        )
        if part
    )
    return SignalNotification(
        active=True,
        key=f"normalized:{event_id}",
        level="critical" if severity in {"alarm", "critical"} else "warning",
        title=title,
        message=explanation,
        details=details,
        next_action=recommendation,
        target_page="situation",
    )


def _build_fusion_notification(snapshot: object | None) -> SignalNotification:
    fusion = attr(snapshot, "fusion_decision")
    nested = attr(fusion, "decision")
    if nested is not None:
        fusion = nested
    classification = value_of(
        attr(fusion, "classification", "")
    ).lower()
    lifecycle = value_of(attr(fusion, "lifecycle", "")).lower()
    episode_id = str(attr(fusion, "episode_id", "") or "")
    if (
        fusion is None
        or classification != "multi_sensor_correlated"
        or lifecycle not in {"confirmed", "holding"}
        or not bool(attr(fusion, "alertable", False))
        or not episode_id
    ):
        return _INACTIVE

    view = present_multisensor(snapshot)
    simulated = provenance_key(snapshot) in {"demo", "simulated"}
    title = (
        "Согласованное RF+акустическое наблюдение"
        if lifecycle == "confirmed"
        else "Согласованное RF+акустическое наблюдение временно ослабло"
    )
    if simulated:
        title = f"ДЕМО · {title}"
    provenance = (
        "Источник: синтетические данные демо-сценария."
        if simulated
        else "Источник: измеренные сенсорные данные."
    )
    observation_count = attr(fusion, "observation_count")
    count_text = (
        f" Учтено наблюдений: {observation_count}."
        if isinstance(observation_count, int)
        and not isinstance(observation_count, bool)
        and observation_count >= 0
        else ""
    )
    details = " ".join(
        (
            "Почему показано уведомление: независимые RF- и акустические "
            "признаки совпали во временном окне и прошли временную проверку.",
            view.quality,
            f"Не хватает: {view.missing}",
            f"{provenance}{count_text}",
            (
                "Это согласование наблюдений, а не идентификация физического "
                "источника или свойств физического объекта."
            ),
            _IDENTITY_LIMIT,
        )
    )
    return SignalNotification(
        active=True,
        key=f"fusion:{episode_id}",
        level="info" if lifecycle == "holding" or simulated else "warning",
        title=title,
        message=(
            "Независимые RF- и акустические признаки совпали во временном "
            f"окне. {provenance}"
        ),
        details=details,
        next_action=(
            "Откройте обзор, проверьте состояние обоих сенсоров и продолжайте "
            "наблюдение. Физический источник требует отдельного подтверждения."
        ),
        target_page="dashboard",
    )


def _decision_details(view: RfDecisionView) -> str:
    parts = [
        f"Состояние: {view.lifecycle_label}.",
        f"RF-семейство: {view.family_label}.",
        f"Почему показано уведомление: {_temporal_reason(view)}",
        f"Почему выбран этот класс: {view.summary}",
        f"Качество данных: {view.data_quality_label}.",
        f"Сила RF-признаков: {view.evidence_strength_label}.",
    ]
    measured: list[str] = []
    if view.peak_frequency_hz is not None:
        measured.append(f"пик около {_format_frequency(view.peak_frequency_hz)}")
    if (
        view.occupied_bandwidth_hz is not None
        and view.occupied_bandwidth_hz > 0.0
    ):
        measured.append(
            f"занятая полоса ≈ {_format_frequency(view.occupied_bandwidth_hz)}"
        )
    if measured:
        parts.append(f"Измерено: {'; '.join(measured)}.")
    if view.supporting_evidence:
        parts.append(f"За: {view.supporting_evidence[0]}")
    if view.contradicting_evidence:
        parts.append(f"Против: {view.contradicting_evidence[0]}")
    if view.missing_confirmation:
        parts.append(f"Не хватает: {view.missing_confirmation[0]}")
    else:
        parts.append(f"Не хватает: {_default_missing_confirmation(view)}")
    if view.sensor_contributions:
        parts.append(f"Вклад сенсоров: {view.sensor_contributions[0]}")
    if view.alternatives:
        parts.append(f"Альтернатива: {view.alternatives[0]}")
    if view.limitations:
        parts.append(f"Ограничение измерения: {view.limitations[0]}")
    parts.append(
        f"Эвристический балл {view.heuristic_score:.2f}; "
        "это не калиброванная вероятность."
    )
    parts.append(_IDENTITY_LIMIT)
    return " ".join(parts)


def _notification_copy(view: RfDecisionView) -> tuple[str, str]:
    family = view.family
    if family in _VOICE_FAMILIES:
        title = "Устойчивый голосоподобный RF-канал"
        message = (
            "Возможна голосовая радиосвязь или радиостанция, но это не "
            "подтверждает рацию либо тип передатчика."
        )
    elif family in _PACKET_FAMILIES:
        title = "Устойчивый пакетоподобный RF-обмен"
        message = (
            "Форма совместима с цифровыми пакетами или телеметрией; протокол "
            "и тип передатчика не установлены."
        )
    elif family in _CARRIER_FAMILIES:
        title = "Устойчивая RF-несущая"
        message = (
            "Наблюдается непрерывная спектральная линия; это может быть "
            "передача, локальный генератор или аппаратный spur."
        )
    elif family in _NARROW_BURST_FAMILIES:
        title = "Устойчивый узкополосный RF-эпизод"
        message = (
            "Ограниченная во времени узкополосная активность подтверждена; "
            "протокол и физический источник не установлены."
        )
    elif family in _BROAD_BURST_FAMILIES:
        title = "Устойчивый широкополосный RF-эпизод"
        message = (
            "Подтверждён временный рост энергии в широкой части полосы; "
            "причиной может быть передача, помеха или изменение радиотракта."
        )
    elif family in _INTERFERENCE_FAMILIES:
        title = "Устойчивая шумоподобная RF-помеха"
        message = (
            "Шумоподобное изменение сохраняется во времени; источник помехи "
            "не установлен."
        )
    else:
        title = "Устойчивый RF-эпизод не классифицирован"
        message = (
            "Изменение подтверждено во времени, но данных недостаточно для "
            "выбора устойчивого RF-семейства."
        )

    if view.lifecycle == "holding":
        title = (
            "Ранее подтверждённый эпизод временно ослаб: "
            f"{view.family_label.lower()}"
        )
    return title, message


def _measurement_text(view: RfDecisionView) -> str:
    if view.peak_frequency_hz is None:
        return ""
    return f"Пик около {_format_frequency(view.peak_frequency_hz)}."


def _temporal_reason(view: RfDecisionView) -> str:
    if view.lifecycle == "holding":
        return (
            "эпизод ранее прошёл временную проверку, а сейчас временно ниже "
            "порога удержания."
        )
    return (
        "изменение повторилось и прошло временную проверку; одиночный кадр "
        "такого уведомления не создаёт."
    )


def _default_missing_confirmation(view: RfDecisionView) -> str:
    if view.family in _VOICE_FAMILIES:
        return (
            "декодирования или независимого аудио/RF-подтверждения голосовой "
            "связи и типа передатчика."
        )
    if view.family in _PACKET_FAMILIES:
        return (
            "проверенного декодирования протокола или независимого сенсора для "
            "определения типа передатчика."
        )
    if view.family in _CARRIER_FAMILIES:
        return (
            "проверки вторым приёмником или другим трактом, чтобы исключить "
            "локальную аппаратную линию."
        )
    if view.family in _NARROW_BURST_FAMILIES:
        return (
            "декодирования или независимого сенсора для определения протокола "
            "и физического источника узкополосного эпизода."
        )
    if view.family in _BROAD_BURST_FAMILIES:
        return (
            "проверки перегрузки, локальных помех и второго приёмного тракта "
            "для установления причины широкополосного роста."
        )
    if view.family in _INTERFERENCE_FAMILIES:
        return (
            "проверки локального оборудования и независимого измерения для "
            "установления источника помехи."
        )
    return (
        "дополнительных устойчивых признаков и независимого сенсора для "
        "классификации источника."
    )


def _next_action(view: RfDecisionView) -> str:
    if view.family in _INTERFERENCE_FAMILIES:
        return (
            "Откройте журнал и проверьте собственное RF-оборудование, кабели "
            "и перегрузку приёмника. Продолжайте наблюдение до завершения эпизода."
        )
    if view.family in _CARRIER_FAMILIES:
        return (
            "Откройте журнал и, если возможно, сравните сигнал со вторым "
            "приёмным трактом. Продолжайте наблюдение."
        )
    return (
        "Откройте журнал событий и продолжайте наблюдение. Для установления "
        "физического источника требуется независимое подтверждение."
    )


def _format_frequency(hz: float) -> str:
    if abs(hz) >= 1_000_000.0:
        return f"{hz / 1_000_000.0:.3f} МГц"
    if abs(hz) >= 1_000.0:
        return f"{hz / 1_000.0:.1f} кГц"
    return f"{hz:.0f} Гц"


__all__ = ["SignalNotification", "build_signal_notification"]
