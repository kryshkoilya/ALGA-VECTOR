"""Adapters from the existing runtime snapshot to normalized operator events."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from alga_vector.acoustics import (
    AcousticAssessment,
    AcousticFamily,
    AcousticLifecycle,
)
from alga_vector.airspace import AirspaceFeedState, CivilAirspaceSnapshot
from alga_vector.direction import DirectionSnapshot, DirectionSource
from alga_vector.domain.enums import DeviceState
from alga_vector.domain.models import DeviceSnapshot, SystemSnapshot
from alga_vector.sensor_fusion import (
    FusionClassification,
    FusionDecision,
    FusionLifecycle,
    SensorModality,
)
from alga_vector.signal_analysis import (
    DecisionLifecycle,
    RfDecision,
    RfFamily,
)

from .recommendations import RecommendationEngine
from .schema import (
    ConfidenceScore,
    DirectionEstimate,
    EventSeverity,
    EvidenceFact,
    NormalizedEvent,
    NormalizedEventType,
    SensorAvailability,
    SensorKind,
    SensorState,
    SourceAttribution,
)


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    events: tuple[NormalizedEvent, ...]
    sensors: tuple[SensorState, ...]


class SnapshotEventNormalizer:
    """Convert current subsystems without inventing emitter identity."""

    def __init__(
        self,
        *,
        recommendation_engine: RecommendationEngine | None = None,
    ) -> None:
        self._recommendations = (
            recommendation_engine or RecommendationEngine()
        )

    def normalize(self, snapshot: SystemSnapshot) -> NormalizationResult:
        now = snapshot.captured_at
        sensors = self._sensor_states(snapshot, now)
        events: list[NormalizedEvent] = []

        rf_event = (
            self.normalize_rf_decision(snapshot.signal_decision, now=now)
            if snapshot.signal_decision is not None
            else None
        )
        if rf_event is not None:
            events.append(rf_event)

        acoustic_event = self._acoustic_event(snapshot.acoustic, now)
        if acoustic_event is not None:
            events.append(acoustic_event)

        adsb_event = self._adsb_event(snapshot.airspace, now)
        if adsb_event is not None:
            events.append(adsb_event)

        direction_event = self._direction_event(snapshot.direction, now)
        if direction_event is not None:
            events.append(direction_event)

        fusion_event = self._fusion_event(snapshot.fusion_decision, now)
        if fusion_event is not None:
            events.append(fusion_event)

        unavailable = self._unavailable_events(
            snapshot.devices,
            sensors,
            now,
            activity_present=any(
                item.event_type
                in {
                    NormalizedEventType.RADIO_ACTIVITY_DETECTED,
                    NormalizedEventType.ACOUSTIC_ANOMALY,
                    NormalizedEventType.MULTISENSOR_CORRELATED,
                }
                for item in events
            ),
        )
        events.extend(unavailable)
        return NormalizationResult(events=tuple(events), sensors=sensors)

    def normalize_rf_decision(
        self,
        decision: RfDecision,
        *,
        now: datetime,
    ) -> NormalizedEvent:
        """Normalize one RF decision without waiting for a UI snapshot.

        Acquisition uses this narrow entry point so a short energy-gate event
        cannot disappear between hardware frames before the next UI refresh.
        Identity policy is unchanged: RF-only input remains generic activity.
        """

        event = self._rf_event(decision, now)
        if event is None:  # defensive: a concrete decision must always map
            raise RuntimeError("RF decision did not produce a normalized event")
        return event

    def _sensor_states(
        self,
        snapshot: SystemSnapshot,
        now: datetime,
    ) -> tuple[SensorState, ...]:
        states: list[SensorState] = [
            self._device_sensor_state(device, now)
            for device in snapshot.devices
        ]
        kinds = {item.sensor_kind for item in states}
        if not kinds.intersection(
            {SensorKind.RF_TRIGGER, SensorKind.RF_SPECTRUM}
        ):
            states.append(
                SensorState(
                    sensor_id="rf-receiver",
                    sensor_kind=SensorKind.RF_SPECTRUM,
                    availability=SensorAvailability.UNAVAILABLE,
                    message_ru="Совместимый RF-приёмник не подключён.",
                    checked_at=now,
                )
            )
        if SensorKind.DIRECTION_FINDER not in kinds:
            direction_state = self._direction_sensor_state(
                snapshot.direction,
                now,
            )
            states.append(direction_state)
        if SensorKind.ACOUSTIC not in kinds:
            states.append(
                SensorState(
                    sensor_id="acoustic",
                    sensor_kind=SensorKind.ACOUSTIC,
                    availability=(
                        SensorAvailability.AVAILABLE
                        if snapshot.acoustic is not None
                        else SensorAvailability.UNAVAILABLE
                    ),
                    message_ru=(
                        "Акустический источник передаёт данные."
                        if snapshot.acoustic is not None
                        else "Акустический источник не настроен."
                    ),
                    checked_at=now,
                )
            )
        if SensorKind.ADSB not in kinds:
            states.append(
                SensorState(
                    sensor_id="adsb",
                    sensor_kind=SensorKind.ADSB,
                    availability=self._adsb_availability(snapshot.airspace),
                    message_ru=(
                        "Локальный гражданский ADS-B контекст доступен."
                        if self._adsb_availability(snapshot.airspace)
                        is SensorAvailability.AVAILABLE
                        else "Локальный гражданский ADS-B источник недоступен."
                    ),
                    checked_at=now,
                )
            )
        return tuple(states)

    @staticmethod
    def _device_sensor_state(
        device: DeviceSnapshot,
        now: datetime,
    ) -> SensorState:
        availability = _device_availability(device.state)
        message = (
            device.reason_ru
            or {
                SensorAvailability.AVAILABLE: "Устройство готово.",
                SensorAvailability.DEGRADED: "Устройство работает с ограничениями.",
                SensorAvailability.UNAVAILABLE: "Устройство недоступно.",
                SensorAvailability.STALE: "Данные устройства устарели.",
            }[availability]
        )
        return SensorState(
            sensor_id=device.device_id,
            sensor_kind=_sensor_kind(device),
            availability=availability,
            message_ru=message,
            checked_at=now,
            capabilities=tuple(sorted(item.value for item in device.capabilities)),
        )

    @staticmethod
    def _direction_sensor_state(
        raw_direction: object | None,
        now: datetime,
    ) -> SensorState:
        direction = (
            raw_direction
            if isinstance(raw_direction, DirectionSnapshot)
            else None
        )
        if direction is not None and direction.available:
            if direction.current.source is DirectionSource.EXTERNAL:
                availability = SensorAvailability.AVAILABLE
                message = "Внешний пеленгатор передаёт свежий валидный азимут."
            else:
                availability = SensorAvailability.UNAVAILABLE
                message = (
                    "Демо- или ручной азимут не является свежим измерением "
                    "внешнего пеленгатора."
                )
            source_id = direction.current.source_id
        elif direction is not None and direction.stale:
            availability = SensorAvailability.STALE
            message = "Последний азимут устарел и не используется."
            source_id = direction.current.source_id or "direction-finder"
        else:
            availability = SensorAvailability.UNAVAILABLE
            message = "KrakenSDR или другой внешний пеленгатор не подключён."
            source_id = "direction-finder"
        return SensorState(
            sensor_id=source_id,
            sensor_kind=SensorKind.DIRECTION_FINDER,
            availability=availability,
            message_ru=message,
            checked_at=now,
        )

    @staticmethod
    def _adsb_availability(
        airspace: CivilAirspaceSnapshot | None,
    ) -> SensorAvailability:
        if airspace is None:
            return SensorAvailability.UNAVAILABLE
        if airspace.summary.state is AirspaceFeedState.CURRENT:
            return SensorAvailability.AVAILABLE
        if airspace.summary.state is AirspaceFeedState.STALE:
            return SensorAvailability.STALE
        return SensorAvailability.UNAVAILABLE

    def _rf_event(
        self,
        decision: RfDecision | None,
        now: datetime,
    ) -> NormalizedEvent | None:
        if decision is None:
            return None
        source = self._rf_sources(decision)
        decision_evidence = (
            *decision.supporting_evidence,
            *decision.contradicting_evidence,
            *decision.missing_confirmation,
        )
        evidence = tuple(
            EvidenceFact(
                code=item.code,
                explanation_ru=item.explanation_ru,
                source_id=decision.source_id,
                measured=item.measured,
            )
            for item in decision_evidence
        )
        limitations = tuple(
            dict.fromkeys(
                item.explanation_ru
                for item in decision.limitations
            )
        )
        received_at = max(now, decision.observed_at)
        valid_until = decision.observed_at + timedelta(seconds=10)
        raw_activity_observed = any(
            item.code == "RF.RAW_ACTIVITY_OBSERVED"
            for item in decision.supporting_evidence
        )
        if (
            decision.lifecycle is DecisionLifecycle.DATA_HOLD
            and not raw_activity_observed
        ):
            return self._make_event(
                event_id=_stable_id(
                    "rf-data-unavailable",
                    decision.source_id,
                    decision.observed_at.isoformat(),
                ),
                event_type=NormalizedEventType.SENSOR_UNAVAILABLE,
                observed_at=decision.observed_at,
                received_at=received_at,
                valid_until=valid_until,
                severity=EventSeverity.WARNING,
                confidence=ConfidenceScore.unavailable(
                    "Качество RF-данных недостаточно для вывода."
                ),
                summary_ru="RF-наблюдение временно ограничено",
                explanation_ru=(
                    "Пайплайн удерживает решение из-за качества, "
                    "разрыва или устаревания данных."
                ),
                sources=source,
                evidence=evidence,
                limitations=limitations,
                frequency_hz=decision.peak_frequency_hz,
                bandwidth_hz=decision.occupied_bandwidth_hz,
                # DATA_HOLD/SUPPRESSED describes the health of the RF
                # observation path, not the transient signal episode that
                # happened to be current when quality was lost.  Keeping a
                # stable semantic episode prevents the operator timeline from
                # filling with the same availability warning on every frame.
                episode_id=f"rf-data-unavailable:{decision.source_id}",
                tags=("rf", "data-quality"),
            )
        if (
            decision.family is RfFamily.BACKGROUND
            and decision.lifecycle is DecisionLifecycle.IDLE
        ):
            return self._make_event(
                event_id=_stable_id(
                    "rf-background",
                    decision.source_id,
                    decision.observed_at.isoformat(),
                ),
                event_type=NormalizedEventType.NOISE_BACKGROUND,
                observed_at=decision.observed_at,
                received_at=received_at,
                valid_until=valid_until,
                severity=EventSeverity.INFO,
                confidence=ConfidenceScore.heuristic(
                    max(0.0, min(1.0, decision.heuristic_score)),
                    "Сила общих RF-признаков; не вероятность чистого эфира.",
                ),
                summary_ru="Фон чистый",
                explanation_ru=(
                    "Устойчивого изменения относительно изученного RF-фона "
                    "в свежих данных нет."
                ),
                sources=source,
                evidence=evidence,
                limitations=limitations,
                episode_id=f"rf-background:{decision.source_id}",
                tags=("rf", "background"),
            )
        severity = (
            EventSeverity.WARNING
            if decision.alertable
            else EventSeverity.NOTICE
        )
        family_ru = _rf_family_ru(decision.family)
        lifecycle_ru = _rf_lifecycle_ru(decision.lifecycle)
        return self._make_event(
            event_id=_stable_id(
                "rf-activity",
                decision.episode_id or decision.source_id,
                decision.observed_at.isoformat(),
            ),
            event_type=NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            observed_at=decision.observed_at,
            received_at=received_at,
            valid_until=valid_until,
            severity=severity,
            confidence=ConfidenceScore.heuristic(
                decision.heuristic_score,
                "Эвристическая сила повторяемых RF-признаков; не вероятность и не идентификация.",
            ),
            summary_ru=_rf_summary(decision.peak_frequency_hz),
            explanation_ru=(
                f"Форма активности: {family_ru}. "
                f"Статус обработки: {lifecycle_ru}. "
                "Физический источник по частоте и спектральной форме не установлен."
            ),
            sources=source,
            evidence=evidence,
            limitations=(
                *limitations,
                "Частота и уровень сигнала не определяют тип, направление или дальность источника.",
            ),
            frequency_hz=decision.peak_frequency_hz,
            bandwidth_hz=decision.occupied_bandwidth_hz,
            episode_id=(
                decision.episode_id
                or f"rf-unconfirmed:{decision.source_id}"
            ),
            tags=(
                "rf",
                "generic-activity",
                f"lifecycle-{decision.lifecycle.value}",
            ),
        )

    @staticmethod
    def _rf_sources(
        decision: RfDecision,
    ) -> tuple[SourceAttribution, ...]:
        if decision.sensor_contributions:
            return tuple(
                SourceAttribution(
                    sensor_id=item.source_id,
                    sensor_kind=SensorKind.RF_SPECTRUM,
                    contribution=item.contribution,
                    independent_confirmation=False,
                    explanation_ru=item.explanation_ru,
                    observation_id=decision.episode_id,
                )
                for item in decision.sensor_contributions
            )
        return (
            SourceAttribution(
                sensor_id=decision.source_id,
                sensor_kind=SensorKind.RF_SPECTRUM,
                contribution=max(
                    0.0,
                    min(1.0, decision.heuristic_score),
                ),
                independent_confirmation=False,
                explanation_ru="Основной RF-источник наблюдения.",
                observation_id=decision.episode_id,
            ),
        )

    def _acoustic_event(
        self,
        assessment: AcousticAssessment | None,
        now: datetime,
    ) -> NormalizedEvent | None:
        if assessment is None or assessment.family is AcousticFamily.AMBIENT_NOISE:
            return None
        if assessment.lifecycle in {
            AcousticLifecycle.IDLE,
            AcousticLifecycle.DATA_HOLD,
        }:
            return None
        observed_at = assessment.observed_at
        return self._make_event(
            event_id=_stable_id(
                "acoustic",
                assessment.episode_id or assessment.provenance.source_id,
                observed_at.isoformat(),
            ),
            event_type=NormalizedEventType.ACOUSTIC_ANOMALY,
            observed_at=observed_at,
            received_at=max(now, observed_at),
            valid_until=observed_at + timedelta(seconds=10),
            severity=(
                EventSeverity.WARNING
                if assessment.alertable
                else EventSeverity.NOTICE
            ),
            confidence=ConfidenceScore.heuristic(
                assessment.heuristic_score,
                "Эвристическая сила акустических признаков; не вероятность и не идентификация.",
            ),
            summary_ru="Обнаружена акустическая аномалия",
            explanation_ru=(
                f"{_acoustic_family_ru(assessment.family)}. "
                "По одному микрофону тип объекта не устанавливается."
            ),
            sources=(
                SourceAttribution(
                    sensor_id=assessment.provenance.source_id,
                    sensor_kind=SensorKind.ACOUSTIC,
                    contribution=assessment.heuristic_score,
                    independent_confirmation=False,
                    explanation_ru=assessment.explanation_ru,
                    observation_id=assessment.episode_id,
                    provenance=assessment.provenance.kind.value,
                ),
            ),
            evidence=tuple(
                EvidenceFact(
                    code=item.code,
                    explanation_ru=item.explanation_ru,
                    source_id=assessment.provenance.source_id,
                    measured=item.measured,
                )
                for item in assessment.evidence
            ),
            limitations=assessment.limitations,
            episode_id=assessment.episode_id,
            tags=("acoustic", "generic-anomaly"),
        )

    def _adsb_event(
        self,
        airspace: CivilAirspaceSnapshot | None,
        now: datetime,
    ) -> NormalizedEvent | None:
        if (
            airspace is None
            or airspace.summary.state is not AirspaceFeedState.CURRENT
            or airspace.summary.active_count < 1
        ):
            return None
        summary = airspace.summary
        return self._make_event(
            event_id=_stable_id(
                "adsb",
                str(summary.active_count),
                summary.evaluated_at.isoformat(),
            ),
            event_type=NormalizedEventType.ADSB_CONTACT,
            observed_at=summary.evaluated_at,
            received_at=max(now, summary.evaluated_at),
            valid_until=summary.evaluated_at + timedelta(seconds=15),
            severity=EventSeverity.INFO,
            confidence=ConfidenceScore.unavailable(
                "ADS-B — принятый кооперативный контекст, а не классификатор угроз."
            ),
            summary_ru=(
                f"Гражданские ADS-B контакты: {summary.active_count}"
            ),
            explanation_ru=(
                "Получены локальные публичные кооперативные сообщения. "
                "Они не подтверждают и не опровергают другой объект."
            ),
            sources=(
                SourceAttribution(
                    sensor_id="adsb",
                    sensor_kind=SensorKind.ADSB,
                    contribution=0.0,
                    independent_confirmation=False,
                    explanation_ru="Только гражданский контекст.",
                ),
            ),
            evidence=(
                EvidenceFact(
                    code="ADSB.ACTIVE_COUNT",
                    explanation_ru="Число свежих принятых записей.",
                    source_id="adsb",
                    measured=summary.active_count,
                ),
            ),
            limitations=summary.limitations,
            tags=("adsb", "context-only"),
        )

    def _direction_event(
        self,
        raw_direction: object | None,
        now: datetime,
    ) -> NormalizedEvent | None:
        if not isinstance(raw_direction, DirectionSnapshot):
            return None
        snapshot = raw_direction
        current = snapshot.current
        if (
            not snapshot.available
            or snapshot.stale
            or current.source is not DirectionSource.EXTERNAL
            or current.evidence is None
            or not current.evidence.calibration_valid
            or current.bearing_deg is None
            or current.uncertainty_deg is None
            or current.confidence is None
            or current.captured_at is None
        ):
            return None
        valid_until = current.captured_at + timedelta(seconds=3)
        if now > valid_until:
            return None
        direction = DirectionEstimate(
            bearing_deg=current.bearing_deg,
            uncertainty_deg=current.uncertainty_deg,
            source_id=current.source_id,
            observed_at=current.captured_at,
            valid_until=valid_until,
            confidence=current.confidence,
            validated_external=True,
            calibration_id=current.evidence.calibration_id,
        )
        return self._make_event(
            event_id=_stable_id(
                "direction",
                current.source_id,
                current.captured_at.isoformat(),
            ),
            event_type=NormalizedEventType.DIRECTION_ESTIMATED,
            observed_at=current.captured_at,
            received_at=max(now, current.captured_at),
            valid_until=valid_until,
            severity=EventSeverity.NOTICE,
            confidence=ConfidenceScore.heuristic(
                current.confidence,
                "Качество валидного внешнего пеленга; не вероятность типа источника.",
            ),
            summary_ru="Получен свежий азимут",
            explanation_ru=(
                "Внешний калиброванный пеленгатор дал направление. "
                "Азимут не определяет тип или дальность источника."
            ),
            sources=(
                SourceAttribution(
                    sensor_id=current.source_id,
                    sensor_kind=SensorKind.DIRECTION_FINDER,
                    contribution=current.confidence,
                    independent_confirmation=False,
                    explanation_ru="Свежий внешний измеренный азимут.",
                    observation_id=current.reason_code,
                ),
            ),
            evidence=(
                EvidenceFact(
                    code="DIRECTION.BEARING",
                    explanation_ru="Измеренный внешний азимут.",
                    source_id=current.source_id,
                    measured=current.bearing_deg,
                    unit="deg",
                ),
            ),
            limitations=(
                "Азимут не содержит оценки дальности.",
                "Азимут не устанавливает физический тип источника.",
            ),
            direction=direction,
            tags=("direction", "external-validated"),
        )

    def _fusion_event(
        self,
        decision: FusionDecision | None,
        now: datetime,
    ) -> NormalizedEvent | None:
        if (
            decision is None
            or decision.classification
            is not FusionClassification.MULTI_SENSOR_CORRELATED
            or decision.lifecycle
            not in {FusionLifecycle.CONFIRMED, FusionLifecycle.HOLDING}
        ):
            return None
        score = {
            "none": 0.0,
            "low": 0.3,
            "medium": 0.6,
            "high": 0.8,
        }[decision.evidence_strength.value]
        sources = tuple(
            SourceAttribution(
                sensor_id=item.source_id,
                sensor_kind=_modality_kind(item.modality),
                contribution=max(0.0, min(1.0, item.mean_strength)),
                independent_confirmation=item.confirming,
                explanation_ru=(
                    "Независимый вклад в общую временную корреляцию."
                    if item.confirming
                    else "Контекстный вклад в общую временную корреляцию."
                ),
                observation_id=decision.episode_id,
            )
            for item in decision.contributions
        )
        observed_at = decision.evaluated_at
        return self._make_event(
            event_id=_stable_id(
                "fusion",
                decision.episode_id or "none",
                observed_at.isoformat(),
            ),
            event_type=NormalizedEventType.MULTISENSOR_CORRELATED,
            observed_at=observed_at,
            received_at=max(now, observed_at),
            valid_until=observed_at + timedelta(seconds=10),
            severity=EventSeverity.WARNING,
            confidence=ConfidenceScore.heuristic(
                score,
                "Сила временной корреляции независимых сенсоров; не вероятность идентичности.",
            ),
            summary_ru="Несколько сенсоров видят согласованную активность",
            explanation_ru=(
                "Наблюдения совпали по времени согласно правилам fusion. "
                "Это подтверждает активность, но не тип и не намерение объекта."
            ),
            sources=sources
            or (
                SourceAttribution(
                    sensor_id="sensor-fusion",
                    sensor_kind=SensorKind.FUSION,
                    contribution=score,
                    independent_confirmation=False,
                    explanation_ru="Обобщённое решение sensor fusion.",
                    observation_id=decision.episode_id,
                ),
            ),
            evidence=tuple(
                EvidenceFact(
                    code=item.code,
                    explanation_ru=item.explanation,
                    source_id=(
                        item.source_ids[0] if item.source_ids else None
                    ),
                    measured=item.measured,
                )
                for item in decision.evidence
            ),
            limitations=(
                *(item.explanation for item in decision.limitations),
                "Корреляция нескольких общих аномалий не является идентификацией БПЛА.",
            ),
            episode_id=decision.episode_id,
            tags=("fusion", "generic-activity"),
        )

    def _unavailable_events(
        self,
        devices: tuple[DeviceSnapshot, ...],
        sensors: tuple[SensorState, ...],
        now: datetime,
        *,
        activity_present: bool,
    ) -> tuple[NormalizedEvent, ...]:
        unavailable: list[NormalizedEvent] = []
        for device in devices:
            if device.state not in {
                DeviceState.FAILED,
                DeviceState.QUARANTINED,
                DeviceState.RECONNECTING,
            }:
                continue
            kind = _sensor_kind(device)
            unavailable.append(
                self._make_event(
                    event_id=_stable_id(
                        "sensor-unavailable",
                        device.device_id,
                        device.state.value,
                        now.isoformat(),
                    ),
                    event_type=NormalizedEventType.SENSOR_UNAVAILABLE,
                    observed_at=now,
                    received_at=now,
                    valid_until=now + timedelta(seconds=15),
                    severity=EventSeverity.WARNING,
                    confidence=ConfidenceScore.unavailable(
                        "Состояние доступности устройства."
                    ),
                    summary_ru=f"Сенсор недоступен: {device.display_name}",
                    explanation_ru=(
                        device.reason_ru
                        or "Устройство не передаёт надёжные данные."
                    ),
                    sources=(
                        SourceAttribution(
                            sensor_id=device.device_id,
                            sensor_kind=kind,
                            contribution=0.0,
                            independent_confirmation=False,
                            explanation_ru="Источник сообщения о состоянии.",
                        ),
                    ),
                    episode_id=(
                        f"sensor-unavailable:{device.device_id}:"
                        f"{device.state.value}"
                    ),
                    tags=("sensor", "unavailable"),
                )
            )
        has_rf = any(
            item.sensor_kind
            in {SensorKind.RF_TRIGGER, SensorKind.RF_SPECTRUM}
            and item.availability
            in {SensorAvailability.AVAILABLE, SensorAvailability.DEGRADED}
            for item in sensors
        )
        if not has_rf:
            unavailable.append(
                self._make_event(
                    event_id=_stable_id(
                        "sensor-unavailable",
                        "rf-receiver",
                        now.isoformat(),
                    ),
                    event_type=NormalizedEventType.SENSOR_UNAVAILABLE,
                    observed_at=now,
                    received_at=now,
                    valid_until=now + timedelta(seconds=15),
                    severity=EventSeverity.WARNING,
                    confidence=ConfidenceScore.unavailable(
                        "RF-наблюдение отсутствует."
                    ),
                    summary_ru="RF-приёмник не подключён",
                    explanation_ru=(
                        "Без совместимого приёмника система не может "
                        "оценивать активность в эфире."
                    ),
                    sources=(
                        SourceAttribution(
                            sensor_id="rf-receiver",
                            sensor_kind=SensorKind.RF_SPECTRUM,
                            contribution=0.0,
                            independent_confirmation=False,
                            explanation_ru="Синтетическое состояние RF-слоя.",
                        ),
                    ),
                    episode_id="sensor-unavailable:rf-receiver",
                    tags=("sensor", "unavailable", "rf"),
                )
            )
        direction_available = any(
            item.sensor_kind is SensorKind.DIRECTION_FINDER
            and item.availability is SensorAvailability.AVAILABLE
            for item in sensors
        )
        if activity_present and not direction_available:
            unavailable.append(
                self._make_event(
                    event_id=_stable_id(
                        "sensor-unavailable",
                        "direction-finder",
                        now.isoformat(),
                    ),
                    event_type=NormalizedEventType.SENSOR_UNAVAILABLE,
                    observed_at=now,
                    received_at=now,
                    valid_until=now + timedelta(seconds=15),
                    severity=EventSeverity.NOTICE,
                    confidence=ConfidenceScore.unavailable(
                        "Пеленгация не выполняется."
                    ),
                    summary_ru="Пеленгация недоступна",
                    explanation_ru=(
                        "KrakenSDR или другой валидный внешний пеленгатор "
                        "не подключён."
                    ),
                    sources=(
                        SourceAttribution(
                            sensor_id="direction-finder",
                            sensor_kind=SensorKind.DIRECTION_FINDER,
                            contribution=0.0,
                            independent_confirmation=False,
                            explanation_ru="Состояние пеленгационного слоя.",
                        ),
                    ),
                    episode_id="sensor-unavailable:direction-finder",
                    tags=("sensor", "unavailable", "direction"),
                )
            )
        return tuple(unavailable)

    def _make_event(
        self,
        *,
        event_id: str,
        event_type: NormalizedEventType,
        observed_at: datetime,
        received_at: datetime,
        severity: EventSeverity,
        confidence: ConfidenceScore,
        summary_ru: str,
        explanation_ru: str,
        sources: tuple[SourceAttribution, ...],
        evidence: tuple[EvidenceFact, ...] = (),
        limitations: tuple[str, ...] = (),
        frequency_hz: float | None = None,
        bandwidth_hz: float | None = None,
        direction: DirectionEstimate | None = None,
        episode_id: str | None = None,
        tags: tuple[str, ...] = (),
        valid_until: datetime | None = None,
    ) -> NormalizedEvent:
        event = NormalizedEvent(
            schema_version="1.0",
            event_id=event_id,
            event_type=event_type,
            observed_at=observed_at,
            received_at=received_at,
            severity=severity,
            confidence=confidence,
            summary_ru=summary_ru,
            explanation_ru=explanation_ru,
            recommendation_ru="Продолжайте наблюдение.",
            sources=sources,
            evidence=evidence,
            limitations=limitations,
            frequency_hz=frequency_hz,
            bandwidth_hz=bandwidth_hz,
            direction=direction,
            episode_id=episode_id,
            tags=tags,
            valid_until=valid_until,
        )
        return self._recommendations.enrich(event)


def _device_availability(state: DeviceState) -> SensorAvailability:
    if state in {DeviceState.READY, DeviceState.STREAMING}:
        return SensorAvailability.AVAILABLE
    if state in {
        DeviceState.DISCOVERED,
        DeviceState.PROBING,
        DeviceState.STARTING,
        DeviceState.STOPPING,
        DeviceState.DEGRADED,
        DeviceState.RECONNECTING,
    }:
        return SensorAvailability.DEGRADED
    return SensorAvailability.UNAVAILABLE


def _sensor_kind(device: DeviceSnapshot) -> SensorKind:
    kind = device.kind.strip().lower()
    if "kraken" in kind or any(
        item.value == "df_observation" for item in device.capabilities
    ):
        return SensorKind.DIRECTION_FINDER
    if "tinysa" in kind or "trigger" in kind:
        return SensorKind.RF_TRIGGER
    if any(
        marker in kind
        for marker in ("rtlsdr", "rtl-sdr", "hackrf", "sdr", "spectrum")
    ) or any(
        item.value in {"spectrum_sweep", "iq_rx", "coherent_iq_rx"}
        for item in device.capabilities
    ):
        return SensorKind.RF_SPECTRUM
    if "acoustic" in kind or "microphone" in kind:
        return SensorKind.ACOUSTIC
    if "adsb" in kind or "dump1090" in kind:
        return SensorKind.ADSB
    if "radar" in kind:
        return SensorKind.PASSIVE_RADAR
    if "camera" in kind:
        return SensorKind.CAMERA
    return SensorKind.SYSTEM


def _modality_kind(modality: SensorModality) -> SensorKind:
    return {
        SensorModality.RF: SensorKind.RF_SPECTRUM,
        SensorModality.ACOUSTIC: SensorKind.ACOUSTIC,
        SensorModality.DIRECTION: SensorKind.DIRECTION_FINDER,
        SensorModality.CIVIL_ADSB: SensorKind.ADSB,
    }[modality]


def _stable_id(*parts: str) -> str:
    value = "|".join(parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _rf_summary(frequency_hz: float | None) -> str:
    if frequency_hz is None:
        return "Обнаружена RF-активность"
    if frequency_hz >= 1_000_000_000.0:
        return (
            "Обнаружена активность в диапазоне "
            f"{frequency_hz / 1_000_000_000.0:.3f} ГГц"
        )
    return (
        "Обнаружена активность на "
        f"{frequency_hz / 1_000_000.0:.3f} МГц"
    )


def _rf_family_ru(family: RfFamily) -> str:
    return {
        RfFamily.BACKGROUND: "фон",
        RfFamily.CARRIER: "устойчивая несущая или аппаратный spur",
        RfFamily.NARROWBAND_BURST: "узкополосный импульсный эпизод",
        RfFamily.BROADBAND_BURST: "широкополосный импульсный эпизод",
        RfFamily.PACKET_LIKE: "пакетоподобная активность",
        RfFamily.VOICE_LIKE: "голосоподобная спектральная форма",
        RfFamily.PERIODIC_BEACON_LIKE: "периодическая маякоподобная форма",
        RfFamily.INTERFERENCE_NOISE_LIKE: "помехо- или шумоподобная форма",
        RfFamily.UNKNOWN: "неопределённая RF-форма",
        RfFamily.VOICE_LIKE_COMPATIBLE: "голосоподобная форма",
        RfFamily.CONTINUOUS_CARRIER_OR_SPUR: "несущая или аппаратный spur",
        RfFamily.BURST_DIGITAL_OR_TELEMETRY_LIKE: "импульсная цифровая форма",
        RfFamily.WIDEBAND_OR_INTERFERENCE: "широкополосная форма или помеха",
        RfFamily.IMPULSE_OR_LOCAL_INTERFERENCE: "импульс или локальная помеха",
    }[family]


def _rf_lifecycle_ru(lifecycle: DecisionLifecycle) -> str:
    return {
        DecisionLifecycle.IDLE: (
            "изменение замечено, но ниже порога temporal-кандидата"
        ),
        DecisionLifecycle.CANDIDATE: "неподтверждённый temporal-кандидат",
        DecisionLifecycle.CONFIRMED: "устойчивый RF-эпизод",
        DecisionLifecycle.HOLDING: "RF-эпизод на выдержке release-hold",
        DecisionLifecycle.RESOLVED: "RF-эпизод завершён",
        DecisionLifecycle.SUPPRESSED: (
            "одиночное или шумоподобное наблюдение без подтверждения"
        ),
        DecisionLifecycle.DATA_HOLD: (
            "активность замечена при ограниченном качестве данных"
        ),
    }[lifecycle]


def _acoustic_family_ru(family: AcousticFamily) -> str:
    return {
        AcousticFamily.ROTOR_LIKE: "Наблюдается ротороподобная звуковая форма",
        AcousticFamily.ENGINE_LIKE: "Наблюдается двигателеподобная звуковая форма",
        AcousticFamily.BROADBAND_ANOMALY: "Наблюдается широкополосная звуковая аномалия",
        AcousticFamily.UNKNOWN_AERIAL_LIKE: "Наблюдается неопределённая воздушноподобная форма",
        AcousticFamily.AMBIENT_NOISE: "Фоновый звук",
    }[family]


__all__ = ["NormalizationResult", "SnapshotEventNormalizer"]
