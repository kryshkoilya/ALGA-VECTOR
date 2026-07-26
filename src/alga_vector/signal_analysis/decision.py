"""Conservative temporal decisions over non-attributive RF observations.

The raw detector intentionally remains a frame-level spectrum-shape detector.
This module adds bounded component tracking, time-aware hysteresis, abstention,
and an explainable episode lifecycle without converting RF shape into emitter
identity.  Its numeric score remains an uncalibrated heuristic.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from threading import RLock

from .detector import (
    AnalysisResult,
    AssessmentState,
    AttributionStatus,
    EventClass,
    QualityFlag,
    RfEvent,
    SpectrumAcquisitionMode,
)


class DecisionInputError(ValueError):
    """An analysis result cannot safely advance the temporal state."""


class RfFamily(StrEnum):
    """General observable RF families, never physical emitter identities."""

    BACKGROUND = "background"
    CARRIER = "carrier"
    NARROWBAND_BURST = "narrowband_burst"
    BROADBAND_BURST = "broadband_burst"
    PACKET_LIKE = "packet_like"
    VOICE_LIKE = "voice_like"
    PERIODIC_BEACON_LIKE = "periodic_beacon_like"
    INTERFERENCE_NOISE_LIKE = "interference_noise_like"
    UNKNOWN = "unknown"

    # Deprecated input/API members.  The engine never emits these values, but
    # keeping them allows older journal rows and integrations to deserialize.
    VOICE_LIKE_COMPATIBLE = "voice_like_compatible"
    CONTINUOUS_CARRIER_OR_SPUR = "continuous_carrier_or_spur"
    BURST_DIGITAL_OR_TELEMETRY_LIKE = "burst_digital_or_telemetry_like"
    WIDEBAND_OR_INTERFERENCE = "wideband_or_interference"
    IMPULSE_OR_LOCAL_INTERFERENCE = "impulse_or_local_interference"


class DecisionLifecycle(StrEnum):
    """Lifecycle of one frequency-compatible temporal episode."""

    IDLE = "idle"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    HOLDING = "holding"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    DATA_HOLD = "data_hold"


class DecisionTransitionKind(StrEnum):
    """Externally publishable lifecycle transitions."""

    CONFIRMED = "confirmed"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class DataQuality(StrEnum):
    """Trust in the input stream, separate from RF evidence strength."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceStrength(StrEnum):
    """Strength of measured RF evidence, not a calibrated probability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RF_FAMILY_EXPLANATIONS_RU: dict[RfFamily, str] = {
    RfFamily.BACKGROUND: "Выраженного изменения относительно изученного фона нет.",
    RfFamily.CARRIER: (
        "Устойчивая узкополосная спектральная линия наблюдается в нескольких "
        "согласованных кадрах; она может быть несущей или аппаратным spur."
    ),
    RfFamily.NARROWBAND_BURST: (
        "Подтверждён ограниченный во времени узкополосный RF-эпизод; источник "
        "и протокол по этим данным не устанавливаются."
    ),
    RfFamily.BROADBAND_BURST: (
        "Подтверждён ограниченный во времени рост энергии в широкой части "
        "наблюдаемой полосы; причина не установлена."
    ),
    RfFamily.PACKET_LIKE: (
        "Наблюдается повторяющаяся активность с паузами, совместимая с пакетной "
        "формой; протокол и физический источник не установлены."
    ),
    RfFamily.VOICE_LIKE: (
        "В одновременных спектральных кадрах наблюдается изменяющаяся "
        "узкополосная огибающая, совместимая с voice-like формой; источник "
        "не установлен."
    ),
    RfFamily.PERIODIC_BEACON_LIKE: (
        "Не менее трёх разделённых паузами эпизодов повторяются с близким "
        "интервалом; это только периодическая beacon-like форма."
    ),
    RfFamily.INTERFERENCE_NOISE_LIKE: (
        "Устойчиво изменилась широкая часть полосы либо наблюдается "
        "многокомпонентная шумоподобная форма; причина не установлена."
    ),
    RfFamily.UNKNOWN: (
        "Изменение есть, но измеренных признаков недостаточно для устойчивого RF-класса."
    ),
    # Compatibility descriptions for externally supplied legacy values.
    RfFamily.VOICE_LIKE_COMPATIBLE: (
        "Устаревшая метка voice-like; источник не установлен."
    ),
    RfFamily.CONTINUOUS_CARRIER_OR_SPUR: (
        "Устаревшая метка непрерывной спектральной линии."
    ),
    RfFamily.BURST_DIGITAL_OR_TELEMETRY_LIKE: (
        "Устаревшая метка пакетоподобной RF-формы; источник не установлен."
    ),
    RfFamily.WIDEBAND_OR_INTERFERENCE: (
        "Устаревшая метка широкополосного RF-изменения."
    ),
    RfFamily.IMPULSE_OR_LOCAL_INTERFERENCE: (
        "Устаревшая метка одиночного широкополосного всплеска."
    ),
}


@dataclass(frozen=True, slots=True)
class TemporalDecisionConfig:
    """Validated conservative thresholds for temporal RF decisions."""

    confirmation_window: int = 5
    confirmation_observations: int = 3
    minimum_heuristic_score: float = 0.55
    attack_excess_db: float = 10.0
    release_excess_db: float = 6.0
    minimum_confirm_dwell_seconds: float = 0.15
    release_hold_seconds: float = 0.75
    release_observations: int = 2
    candidate_timeout_seconds: float = 1.50
    maximum_observation_gap_seconds: float = 0.75
    minimum_component_overlap: float = 0.20
    maximum_center_drift_hz: float = 5_000.0
    maximum_center_drift_bandwidths: float = 1.50
    voice_like_maximum_bandwidth_hz: float = 25_000.0
    voice_like_minimum_level_range_db: float = 4.0
    stable_family_minimum_observations: int = 4
    stable_family_minimum_dwell_seconds: float = 0.30
    periodic_minimum_cycles: int = 3
    periodic_maximum_interval_cv: float = 0.20
    recurrence_score_bonus: float = 0.12
    family_switch_observations: int = 2
    maximum_sources: int = 8
    maximum_tracks_per_source: int = 16

    def __post_init__(self) -> None:
        if self.confirmation_window < 3:
            raise ValueError("confirmation_window must be at least 3")
        if not 2 <= self.confirmation_observations <= self.confirmation_window:
            raise ValueError(
                "confirmation_observations must be in 2..confirmation_window"
            )
        if not 0.0 <= self.minimum_heuristic_score <= 1.0:
            raise ValueError("minimum_heuristic_score must be in [0, 1]")
        if self.release_excess_db < 0.0:
            raise ValueError("release_excess_db must be non-negative")
        if self.attack_excess_db <= self.release_excess_db:
            raise ValueError("attack_excess_db must be above release_excess_db")
        for name, value in (
            ("minimum_confirm_dwell_seconds", self.minimum_confirm_dwell_seconds),
            ("release_hold_seconds", self.release_hold_seconds),
            ("candidate_timeout_seconds", self.candidate_timeout_seconds),
            ("maximum_observation_gap_seconds", self.maximum_observation_gap_seconds),
        ):
            if value <= 0.0 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and positive")
        if self.candidate_timeout_seconds < self.minimum_confirm_dwell_seconds:
            raise ValueError(
                "candidate_timeout_seconds must not be below confirmation dwell"
            )
        if not 1 <= self.release_observations <= self.confirmation_window:
            raise ValueError("release_observations must fit confirmation_window")
        if not 0.0 < self.minimum_component_overlap <= 1.0:
            raise ValueError("minimum_component_overlap must be in (0, 1]")
        for name, value in (
            ("maximum_center_drift_hz", self.maximum_center_drift_hz),
            (
                "maximum_center_drift_bandwidths",
                self.maximum_center_drift_bandwidths,
            ),
            (
                "voice_like_maximum_bandwidth_hz",
                self.voice_like_maximum_bandwidth_hz,
            ),
            (
                "voice_like_minimum_level_range_db",
                self.voice_like_minimum_level_range_db,
            ),
        ):
            if value <= 0.0 or not math.isfinite(value):
                raise ValueError(f"{name} must be finite and positive")
        if self.maximum_sources < 1 or self.maximum_tracks_per_source < 1:
            raise ValueError("source and track limits must be positive")
        if self.stable_family_minimum_observations < 3:
            raise ValueError("stable_family_minimum_observations must be at least 3")
        if (
            self.stable_family_minimum_dwell_seconds <= 0.0
            or not math.isfinite(self.stable_family_minimum_dwell_seconds)
        ):
            raise ValueError(
                "stable_family_minimum_dwell_seconds must be finite and positive"
            )
        if self.periodic_minimum_cycles < 3:
            raise ValueError("periodic_minimum_cycles must be at least 3")
        if not 0.0 <= self.periodic_maximum_interval_cv <= 1.0:
            raise ValueError("periodic_maximum_interval_cv must be in [0, 1]")
        if not 0.0 <= self.recurrence_score_bonus <= 0.25:
            raise ValueError("recurrence_score_bonus must be in [0, 0.25]")
        if self.family_switch_observations < 2:
            raise ValueError("family_switch_observations must be at least 2")


EvidenceValue = float | int | str | None


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    """One measured or missing fact in an explainable decision chain."""

    code: str
    explanation_ru: str
    measured: EvidenceValue = None
    threshold: EvidenceValue = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.explanation_ru.strip():
            raise ValueError("decision evidence text must not be blank")
        for value in (self.measured, self.threshold):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric evidence must be finite")


@dataclass(frozen=True, slots=True)
class DecisionAlternative:
    """A plausible generic family that current evidence cannot exclude."""

    family: RfFamily
    explanation_ru: str

    def __post_init__(self) -> None:
        if not self.explanation_ru.strip():
            raise ValueError("alternative explanation must not be blank")
        if self.family == RfFamily.BACKGROUND:
            raise ValueError("background is not an active-family alternative")


@dataclass(frozen=True, slots=True)
class SensorContribution:
    """Traceable contribution from one RF source."""

    source_id: str
    contribution: float
    data_quality: DataQuality
    independent_confirmation: bool
    explanation_ru: str

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.explanation_ru.strip():
            raise ValueError("sensor contribution text must not be blank")
        if not 0.0 <= self.contribution <= 1.0:
            raise ValueError("sensor contribution must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class RfDecision:
    """Current conservative decision for one source or temporal episode."""

    source_id: str
    observed_at: datetime
    lifecycle: DecisionLifecycle
    family: RfFamily
    family_explanation_ru: str
    episode_id: str | None
    started_at: datetime | None
    last_active_at: datetime | None
    peak_frequency_hz: float | None
    occupied_bandwidth_hz: float | None
    heuristic_score: float
    calibrated_probability: None
    evidence_strength: EvidenceStrength
    data_quality: DataQuality
    alertable: bool
    abstained: bool
    supporting_evidence: tuple[DecisionEvidence, ...]
    contradicting_evidence: tuple[DecisionEvidence, ...]
    missing_confirmation: tuple[DecisionEvidence, ...]
    sensor_contributions: tuple[SensorContribution, ...]
    attribution: AttributionStatus = AttributionStatus.NOT_AVAILABLE
    identity_established: bool = False
    alternatives: tuple[DecisionAlternative, ...] = ()
    limitations: tuple[DecisionEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("source_id must not be blank")
        _require_aware(self.observed_at, "observed_at")
        if self.started_at is not None:
            _require_aware(self.started_at, "started_at")
        if self.last_active_at is not None:
            _require_aware(self.last_active_at, "last_active_at")
        if not self.family_explanation_ru.strip():
            raise ValueError("family explanation must not be blank")
        if self.lifecycle == DecisionLifecycle.IDLE and self.episode_id is not None:
            raise ValueError("idle decision must not have an episode_id")
        if (
            self.lifecycle
            not in {DecisionLifecycle.IDLE, DecisionLifecycle.DATA_HOLD}
            and self.episode_id is None
        ):
            raise ValueError("episode lifecycle requires an episode_id")
        if self.episode_id is not None and not self.episode_id.strip():
            raise ValueError("episode_id must not be blank")
        for value, name in (
            (self.peak_frequency_hz, "peak_frequency_hz"),
            (self.occupied_bandwidth_hz, "occupied_bandwidth_hz"),
        ):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0.0 <= self.heuristic_score <= 1.0:
            raise ValueError("heuristic_score must be in [0, 1]")
        if self.calibrated_probability is not None:
            raise ValueError("calibrated_probability is unavailable")
        if self.attribution != AttributionStatus.NOT_AVAILABLE:
            raise ValueError("emitter attribution is unavailable")
        if self.identity_established:
            raise ValueError("emitter identity is not established")
        if self.alertable and self.lifecycle not in {
            DecisionLifecycle.CONFIRMED,
            DecisionLifecycle.HOLDING,
        }:
            raise ValueError("only confirmed or holding decisions can be alertable")
        if self.abstained and self.family != RfFamily.UNKNOWN:
            raise ValueError("only unknown RF family may be marked abstained")
        if any(item.family == self.family for item in self.alternatives):
            raise ValueError("selected family must not be repeated as an alternative")


@dataclass(frozen=True, slots=True)
class DecisionTransition:
    """A deduplicated externally publishable episode transition."""

    transition_id: str
    episode_id: str
    source_id: str
    kind: DecisionTransitionKind
    occurred_at: datetime
    family: RfFamily
    reason_code: str
    explanation_ru: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.transition_id,
                self.episode_id,
                self.source_id,
                self.reason_code,
                self.explanation_ru,
            )
        ):
            raise ValueError("transition identifiers and text must not be blank")
        _require_aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class DecisionUpdate:
    """Current decision plus at most one externally meaningful transition."""

    decision: RfDecision
    transition: DecisionTransition | None = None

    def __post_init__(self) -> None:
        if self.transition is not None:
            if self.decision.episode_id != self.transition.episode_id:
                raise ValueError("transition and decision episode ids must match")
            expected = {
                DecisionTransitionKind.CONFIRMED: DecisionLifecycle.CONFIRMED,
                DecisionTransitionKind.RESOLVED: DecisionLifecycle.RESOLVED,
                DecisionTransitionKind.SUPPRESSED: DecisionLifecycle.SUPPRESSED,
            }[self.transition.kind]
            if self.decision.lifecycle != expected:
                raise ValueError("transition kind does not match decision lifecycle")


@dataclass(frozen=True, slots=True)
class _Component:
    center_hz: float
    low_hz: float
    high_hz: float
    bandwidth_hz: float


@dataclass(frozen=True, slots=True)
class _Observation:
    observed_at: datetime
    temporal_at: datetime
    component: _Component
    peak_excess_db: float
    heuristic_score: float
    present: bool
    strong: bool
    event_class: EventClass | None
    acquisition_mode: SpectrumAcquisitionMode
    sweep_duration_ms: float | None
    limitations: tuple[str, ...]


@dataclass(slots=True)
class _Track:
    episode_id: str
    source_id: str
    started_at: datetime
    temporal_started_at: datetime
    last_observed_at: datetime
    last_active_at: datetime
    temporal_last_active_at: datetime
    component: _Component
    observations: deque[_Observation]
    lifecycle: DecisionLifecycle = DecisionLifecycle.CANDIDATE
    family: RfFamily = RfFamily.UNKNOWN
    pending_family: RfFamily | None = None
    pending_family_count: int = 0
    maximum_score: float = 0.0
    suppression_reason: str | None = None


@dataclass(slots=True)
class _SourceState:
    tracks: deque[_Track]
    last_observed_at: datetime | None = None
    last_reliable_at: datetime | None = None
    data_hold_started_at: datetime | None = None


class RfDecisionEngine:
    """Bounded per-source temporal layer over :class:`AnalysisResult`.

    Confirmation requires frequency-compatible observations, an entry score,
    sufficient real elapsed time, and reliable data.  The engine deliberately
    abstains when those conditions are not met.
    """

    def __init__(self, config: TemporalDecisionConfig | None = None) -> None:
        self.config = config or TemporalDecisionConfig()
        self._sources: OrderedDict[str, _SourceState] = OrderedDict()
        self._lock = RLock()

    @property
    def tracked_source_count(self) -> int:
        with self._lock:
            return len(self._sources)

    def reset(self, source_id: str | None = None) -> None:
        """Discard temporal state for one source or every source."""

        with self._lock:
            if source_id is None:
                self._sources.clear()
            else:
                self._sources.pop(source_id, None)

    def process(self, analysis: AnalysisResult) -> DecisionUpdate:
        """Advance one source with one accepted raw analysis result."""

        if not isinstance(analysis, AnalysisResult):
            raise DecisionInputError("expected AnalysisResult")
        source_id = analysis.source_id.strip()
        if not source_id:
            raise DecisionInputError("analysis source_id must not be blank")
        if analysis.assessment.source_id not in {None, analysis.source_id}:
            raise DecisionInputError("assessment source does not match analysis source")
        observed_at = analysis.assessment.observed_at
        _require_aware(observed_at, "analysis assessment observed_at")

        with self._lock:
            state = self._source_state(source_id)
            if (
                state.last_observed_at is not None
                and observed_at < state.last_observed_at
            ):
                raise DecisionInputError("observation time regressed for source")
            state.last_observed_at = observed_at
            quality = _data_quality(analysis)
            if _must_hold_data(analysis):
                if state.data_hold_started_at is None:
                    state.data_hold_started_at = (
                        state.last_reliable_at or observed_at
                    )
                return DecisionUpdate(
                    self._data_hold_decision(
                        state,
                        source_id=source_id,
                        observed_at=observed_at,
                        quality=quality,
                        analysis=analysis,
                    )
                )
            if state.data_hold_started_at is not None:
                _shift_open_tracks(
                    state,
                    observed_at - state.data_hold_started_at,
                )
                state.data_hold_started_at = None
            state.last_reliable_at = observed_at

            peak_excess = _peak_excess(analysis)
            event = analysis.event
            if event is not None:
                component = _component(event)
                if _is_non_alertable_single_observation(event):
                    return self._suppress_immediately(
                        state,
                        analysis=analysis,
                        component=component,
                        observed_at=observed_at,
                        quality=quality,
                        reason=(
                            "RF.SINGLE_BIN_SUPPRESSED"
                            if _is_single_bin_like(event)
                            else "RF.SINGLE_IMPULSE_SUPPRESSED"
                        ),
                    )
                return self._process_event(
                    state,
                    analysis=analysis,
                    component=component,
                    observed_at=observed_at,
                    peak_excess=peak_excess,
                    quality=quality,
                )
            return self._process_without_event(
                state,
                analysis=analysis,
                observed_at=observed_at,
                peak_excess=peak_excess,
                quality=quality,
            )

    def _source_state(self, source_id: str) -> _SourceState:
        state = self._sources.get(source_id)
        if state is None:
            if len(self._sources) >= self.config.maximum_sources:
                self._sources.popitem(last=False)
            state = _SourceState(
                tracks=deque(maxlen=self.config.maximum_tracks_per_source)
            )
            self._sources[source_id] = state
        else:
            self._sources.move_to_end(source_id)
        return state

    def _data_hold_decision(
        self,
        state: _SourceState,
        *,
        source_id: str,
        observed_at: datetime,
        quality: DataQuality,
        analysis: AnalysisResult,
    ) -> RfDecision:
        track = _latest_track(
            state,
            lifecycles={
                DecisionLifecycle.CANDIDATE,
                DecisionLifecycle.CONFIRMED,
                DecisionLifecycle.HOLDING,
            },
        )
        reason = DecisionEvidence(
            code="RF.DATA_QUALITY_HOLD",
            explanation_ru=(
                "Ненадёжный или ещё не подготовленный поток не изменяет temporal-состояние."
            ),
            measured=analysis.assessment.reason_code,
            threshold="reliable_observation_required",
        )
        return self._make_decision(
            source_id=source_id,
            observed_at=observed_at,
            lifecycle=DecisionLifecycle.DATA_HOLD,
            family=track.family if track is not None else RfFamily.UNKNOWN,
            track=track,
            quality=quality,
            score=track.maximum_score if track is not None else 0.0,
            supporting=(),
            contradicting=(reason,),
            extra_missing=(
                DecisionEvidence(
                    code="RF.NEED_RELIABLE_DATA",
                    explanation_ru=(
                        "Нужен свежий непрерывный поток без разрывов для продолжения решения."
                    ),
                ),
            ),
        )

    def _suppress_immediately(
        self,
        state: _SourceState,
        *,
        analysis: AnalysisResult,
        component: _Component,
        observed_at: datetime,
        quality: DataQuality,
        reason: str,
    ) -> DecisionUpdate:
        existing = self._find_compatible(
            state,
            component,
            include_suppressed=True,
        )
        if (
            existing is not None
            and existing.lifecycle == DecisionLifecycle.SUPPRESSED
            and (observed_at - existing.last_observed_at).total_seconds()
            <= self.config.candidate_timeout_seconds
        ):
            existing.last_observed_at = observed_at
            return DecisionUpdate(
                self._decision_for_track(existing, observed_at, quality)
            )

        event = _require_event(analysis)
        track = self._new_track(
            state,
            analysis=analysis,
            component=component,
            observed_at=observed_at,
        )
        track.lifecycle = DecisionLifecycle.SUPPRESSED
        track.family = (
            RfFamily.BROADBAND_BURST
            if event.classification == EventClass.TRANSIENT_BURST
            else RfFamily.UNKNOWN
        )
        track.suppression_reason = reason
        observation = _observation(
            event,
            component,
            observed_at,
            peak_excess=_peak_excess(analysis),
            present=True,
            strong=False,
        )
        track.observations.append(observation)
        track.maximum_score = event.confidence
        decision = self._decision_for_track(track, observed_at, quality)
        return DecisionUpdate(
            decision,
            self._transition(
                track,
                DecisionTransitionKind.SUPPRESSED,
                observed_at,
                reason,
                (
                    "Одиночное или однобиновое наблюдение сохранено как "
                    "неподтверждённое и не является тревожным событием."
                ),
            ),
        )

    def _process_event(
        self,
        state: _SourceState,
        *,
        analysis: AnalysisResult,
        component: _Component,
        observed_at: datetime,
        peak_excess: float,
        quality: DataQuality,
    ) -> DecisionUpdate:
        event = _require_event(analysis)
        present = peak_excess >= self.config.release_excess_db
        # Entry strength and classifier support are intentionally separate.
        # The raw detector caps its first frame below a trusted score; that
        # frame may start a candidate but confirmation still requires the
        # average score of three compatible observations to pass the threshold.
        strong = peak_excess >= self.config.attack_excess_db
        track = self._find_compatible(state, component)
        if track is None:
            if not strong:
                return DecisionUpdate(
                    self._idle_decision(
                        analysis,
                        observed_at=observed_at,
                        quality=quality,
                        peak_excess=peak_excess,
                        event=event,
                    )
                )
            track = self._new_track(
                state,
                analysis=analysis,
                component=component,
                observed_at=observed_at,
            )

        observation = _observation(
            event,
            component,
            observed_at,
            peak_excess=peak_excess,
            present=present,
            strong=strong,
        )
        track.observations.append(observation)
        track.last_observed_at = observed_at
        if present:
            track.last_active_at = observed_at
            track.temporal_last_active_at = observed_at
        track.maximum_score = max(track.maximum_score, event.confidence)
        track.component = _smoothed_component(track.component, component)
        self._stabilize_family(track, self._family_for(track))

        if track.lifecycle in {
            DecisionLifecycle.CONFIRMED,
            DecisionLifecycle.HOLDING,
        }:
            if present:
                track.lifecycle = DecisionLifecycle.CONFIRMED
            return DecisionUpdate(
                self._decision_for_track(track, observed_at, quality)
            )

        if self._can_confirm(track, quality):
            track.lifecycle = DecisionLifecycle.CONFIRMED
            decision = self._decision_for_track(track, observed_at, quality)
            return DecisionUpdate(
                decision,
                self._transition(
                    track,
                    DecisionTransitionKind.CONFIRMED,
                    observed_at,
                    "RF.EPISODE_CONFIRMED",
                    (
                        "RF-эпизод подтверждён согласованными наблюдениями "
                        "одного спектрального компонента."
                    ),
                ),
            )

        if (
            observed_at - track.temporal_started_at
        ).total_seconds() >= self.config.candidate_timeout_seconds:
            return self._suppress_candidate(
                track,
                observed_at=observed_at,
                quality=quality,
                reason="RF.CANDIDATE_TIMEOUT",
            )
        return DecisionUpdate(
            self._decision_for_track(track, observed_at, quality)
        )

    def _process_without_event(
        self,
        state: _SourceState,
        *,
        analysis: AnalysisResult,
        observed_at: datetime,
        peak_excess: float,
        quality: DataQuality,
    ) -> DecisionUpdate:
        track = _latest_track(
            state,
            lifecycles={
                DecisionLifecycle.CANDIDATE,
                DecisionLifecycle.CONFIRMED,
                DecisionLifecycle.HOLDING,
            },
        )
        if track is None:
            return DecisionUpdate(
                self._idle_decision(
                    analysis,
                    observed_at=observed_at,
                    quality=quality,
                    peak_excess=peak_excess,
                    event=None,
                )
            )

        weak_peak_hz = analysis.assessment.evidence.peak_frequency_hz
        weak_present = (
            peak_excess >= self.config.release_excess_db
            and weak_peak_hz is not None
            and _point_compatible(track.component, weak_peak_hz, self.config)
        )
        track.observations.append(
            _Observation(
                observed_at=observed_at,
                temporal_at=observed_at,
                component=track.component,
                peak_excess_db=peak_excess,
                heuristic_score=0.0,
                present=weak_present,
                strong=False,
                event_class=None,
                acquisition_mode=(
                    track.observations[-1].acquisition_mode
                    if track.observations
                    else SpectrumAcquisitionMode.UNKNOWN
                ),
                sweep_duration_ms=(
                    track.observations[-1].sweep_duration_ms
                    if track.observations
                    else None
                ),
                limitations=(
                    track.observations[-1].limitations
                    if track.observations
                    else ()
                ),
            )
        )
        track.last_observed_at = observed_at
        if weak_present:
            track.last_active_at = observed_at
            track.temporal_last_active_at = observed_at
            if track.lifecycle == DecisionLifecycle.HOLDING:
                track.lifecycle = DecisionLifecycle.CONFIRMED
            return DecisionUpdate(
                self._decision_for_track(track, observed_at, quality)
            )

        if track.lifecycle == DecisionLifecycle.CANDIDATE:
            strong_count = _strong_count(track)
            if (
                len(track.observations) >= self.config.confirmation_window
                and strong_count < self.config.confirmation_observations
            ) or (
                observed_at - track.temporal_started_at
            ).total_seconds() >= self.config.candidate_timeout_seconds:
                return self._suppress_candidate(
                    track,
                    observed_at=observed_at,
                    quality=quality,
                    reason="RF.INSUFFICIENT_TEMPORAL_SUPPORT",
                )
            return DecisionUpdate(
                self._decision_for_track(track, observed_at, quality)
            )

        track.lifecycle = DecisionLifecycle.HOLDING
        absent_count = _trailing_absent_count(track)
        elapsed = (
            observed_at - track.temporal_last_active_at
        ).total_seconds()
        if (
            elapsed >= self.config.release_hold_seconds
            and absent_count >= self.config.release_observations
        ):
            track.lifecycle = DecisionLifecycle.RESOLVED
            decision = self._decision_for_track(track, observed_at, quality)
            return DecisionUpdate(
                decision,
                self._transition(
                    track,
                    DecisionTransitionKind.RESOLVED,
                    observed_at,
                    "RF.EPISODE_RESOLVED",
                    (
                        "После выдержки release-hold согласованный компонент "
                        "больше не наблюдается."
                    ),
                ),
            )
        return DecisionUpdate(
            self._decision_for_track(track, observed_at, quality)
        )

    def _new_track(
        self,
        state: _SourceState,
        *,
        analysis: AnalysisResult,
        component: _Component,
        observed_at: datetime,
    ) -> _Track:
        event = _require_event(analysis)
        episode_id = _episode_id(
            source_id=analysis.source_id,
            component=component,
            observed_at=observed_at,
            raw_event_id=event.event_id,
        )
        track = _Track(
            episode_id=episode_id,
            source_id=analysis.source_id,
            started_at=observed_at,
            temporal_started_at=observed_at,
            last_observed_at=observed_at,
            last_active_at=observed_at,
            temporal_last_active_at=observed_at,
            component=component,
            observations=deque(maxlen=self.config.confirmation_window),
        )
        state.tracks.append(track)
        return track

    def _find_compatible(
        self,
        state: _SourceState,
        component: _Component,
        *,
        include_suppressed: bool = False,
    ) -> _Track | None:
        allowed = {
            DecisionLifecycle.CANDIDATE,
            DecisionLifecycle.CONFIRMED,
            DecisionLifecycle.HOLDING,
        }
        if include_suppressed:
            allowed.add(DecisionLifecycle.SUPPRESSED)
        candidates = [
            track
            for track in state.tracks
            if track.lifecycle in allowed
            and _components_compatible(track.component, component, self.config)
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: abs(item.component.center_hz - component.center_hz),
        )

    def _can_confirm(self, track: _Track, quality: DataQuality) -> bool:
        if quality == DataQuality.LOW or track.family == RfFamily.UNKNOWN:
            return False
        strong = [item for item in track.observations if item.strong]
        if len(strong) < self.config.confirmation_observations:
            return False
        selected = strong[-self.config.confirmation_observations :]
        dwell = (selected[-1].observed_at - selected[0].observed_at).total_seconds()
        if dwell < self.config.minimum_confirm_dwell_seconds:
            return False
        if any(
            (current.temporal_at - previous.temporal_at).total_seconds()
            > self.config.maximum_observation_gap_seconds
            for previous, current in pairwise(selected)
        ):
            return False
        return (
            _track_heuristic_score(track, self.config)
            >= self.config.minimum_heuristic_score
        )

    def _family_for(self, track: _Track) -> RfFamily:
        observations = tuple(track.observations)
        classes = [
            item.event_class
            for item in observations
            if item.present and item.event_class is not None
        ]
        if not classes:
            return track.family
        latest = classes[-1]
        if latest == EventClass.BROADBAND_ACTIVITY:
            strong = [item for item in observations if item.strong]
            if (
                len(strong) >= self.config.stable_family_minimum_observations
                and _strong_dwell(track)
                >= self.config.stable_family_minimum_dwell_seconds
            ):
                return RfFamily.INTERFERENCE_NOISE_LIKE
            return RfFamily.BROADBAND_BURST
        if latest == EventClass.TRANSIENT_BURST:
            return RfFamily.BROADBAND_BURST
        if latest == EventClass.UNKNOWN:
            return RfFamily.UNKNOWN
        if latest == EventClass.MULTICOMPONENT_ACTIVITY:
            return RfFamily.UNKNOWN
        if latest != EventClass.NARROWBAND_ACTIVITY:
            return RfFamily.UNKNOWN

        active = [item for item in observations if item.present]
        strong = [item for item in active if item.strong]
        if len(strong) < self.config.confirmation_observations:
            return RfFamily.UNKNOWN
        periodic_times = _active_run_start_times(observations)
        if (
            len(periodic_times) >= self.config.periodic_minimum_cycles
            and _periodicity_is_supported(
                periodic_times,
                observations,
                self.config,
            )
        ):
            return RfFamily.PERIODIC_BEACON_LIKE
        if any(not item.present for item in observations):
            return RfFamily.PACKET_LIKE
        level_range = max(item.peak_excess_db for item in active) - min(
            item.peak_excess_db for item in active
        )
        average_bandwidth = sum(
            item.component.bandwidth_hz for item in active
        ) / len(active)
        if (
            all(
                item.acquisition_mode
                == SpectrumAcquisitionMode.SIMULTANEOUS_FFT
                for item in active
            )
            and len(strong) >= self.config.stable_family_minimum_observations
            and _strong_dwell(track)
            >= self.config.stable_family_minimum_dwell_seconds
            and average_bandwidth
            <= self.config.voice_like_maximum_bandwidth_hz
            and level_range >= self.config.voice_like_minimum_level_range_db
        ):
            return RfFamily.VOICE_LIKE
        if (
            len(strong) >= self.config.stable_family_minimum_observations
            and _strong_dwell(track)
            >= self.config.stable_family_minimum_dwell_seconds
        ):
            return RfFamily.CARRIER
        return RfFamily.NARROWBAND_BURST

    def _stabilize_family(
        self,
        track: _Track,
        proposed: RfFamily,
    ) -> None:
        if proposed == track.family:
            track.pending_family = None
            track.pending_family_count = 0
            return
        if track.family == RfFamily.UNKNOWN and proposed != RfFamily.UNKNOWN:
            # The first evidence-supported family may unblock confirmation;
            # later family changes are debounced separately.
            track.family = proposed
            track.pending_family = None
            track.pending_family_count = 0
            return
        if proposed == RfFamily.UNKNOWN:
            # Temporary ambiguity must not instantly erase an established
            # generic family.  Release/absence logic still resolves the event.
            track.pending_family = None
            track.pending_family_count = 0
            return
        if track.pending_family == proposed:
            track.pending_family_count += 1
        else:
            track.pending_family = proposed
            track.pending_family_count = 1
        if track.pending_family_count >= self.config.family_switch_observations:
            track.family = proposed
            track.pending_family = None
            track.pending_family_count = 0

    def _suppress_candidate(
        self,
        track: _Track,
        *,
        observed_at: datetime,
        quality: DataQuality,
        reason: str,
    ) -> DecisionUpdate:
        track.lifecycle = DecisionLifecycle.SUPPRESSED
        track.suppression_reason = reason
        decision = self._decision_for_track(track, observed_at, quality)
        return DecisionUpdate(
            decision,
            self._transition(
                track,
                DecisionTransitionKind.SUPPRESSED,
                observed_at,
                reason,
                (
                    "Кандидат не набрал достаточной temporal-поддержки "
                    "и не публикуется как подтверждённый RF-эпизод."
                ),
            ),
        )

    def _idle_decision(
        self,
        analysis: AnalysisResult,
        *,
        observed_at: datetime,
        quality: DataQuality,
        peak_excess: float,
        event: RfEvent | None,
    ) -> RfDecision:
        contradicting: list[DecisionEvidence] = []
        missing: list[DecisionEvidence] = []
        score = event.confidence if event is not None else 0.0
        if peak_excess < self.config.attack_excess_db:
            contradicting.append(
                DecisionEvidence(
                    code="RF.BELOW_ATTACK_THRESHOLD",
                    explanation_ru=(
                        "Пиковое превышение не достигло порога входа в RF-кандидат."
                    ),
                    measured=peak_excess,
                    threshold=self.config.attack_excess_db,
                )
            )
        if event is not None and score < self.config.minimum_heuristic_score:
            contradicting.append(
                DecisionEvidence(
                    code="RF.BELOW_SCORE_THRESHOLD",
                    explanation_ru=(
                        "Эвристическая сила frame-признаков ниже порога кандидата."
                    ),
                    measured=score,
                    threshold=self.config.minimum_heuristic_score,
                )
            )
        if event is not None:
            missing.append(
                DecisionEvidence(
                    code="RF.NEED_ENTRY_EVIDENCE",
                    explanation_ru=(
                        "Нужны одновременно достаточное превышение и frame-score."
                    ),
                )
            )
        return self._make_decision(
            source_id=analysis.source_id,
            observed_at=observed_at,
            lifecycle=DecisionLifecycle.IDLE,
            family=RfFamily.BACKGROUND if event is None else RfFamily.UNKNOWN,
            track=None,
            quality=quality,
            score=score,
            supporting=(),
            contradicting=tuple(contradicting),
            extra_missing=tuple(missing),
        )

    def _decision_for_track(
        self,
        track: _Track,
        observed_at: datetime,
        quality: DataQuality,
    ) -> RfDecision:
        strong_count = _strong_count(track)
        score = _track_heuristic_score(track, self.config)
        dwell = _strong_dwell(track)
        supporting = (
            DecisionEvidence(
                code="RF.PEAK_EXCESS",
                explanation_ru="Пиковое превышение измерено относительно адаптивного фона.",
                measured=(
                    track.observations[-1].peak_excess_db
                    if track.observations
                    else None
                ),
                threshold=self.config.attack_excess_db,
            ),
            DecisionEvidence(
                code="RF.TEMPORAL_SUPPORT",
                explanation_ru=(
                    "Согласованные сильные наблюдения относятся к одному частотному компоненту."
                ),
                measured=strong_count,
                threshold=self.config.confirmation_observations,
            ),
            DecisionEvidence(
                code="RF.CONFIRM_DWELL",
                explanation_ru="Поддержка измеряется реальным прошедшим временем.",
                measured=dwell,
                threshold=self.config.minimum_confirm_dwell_seconds,
            ),
            DecisionEvidence(
                code="RF.HEURISTIC_SCORE",
                explanation_ru=(
                    "Frame-score является эвристической силой признаков, а не вероятностью."
                ),
                measured=score,
                threshold=self.config.minimum_heuristic_score,
            ),
            DecisionEvidence(
                code="RF.ACTIVE_RUN_RECURRENCE",
                explanation_ru=(
                    "Повторные активные серии учитываются только после "
                    "частотного и временного согласования."
                ),
                measured=len(
                    _active_run_start_times(tuple(track.observations))
                ),
                threshold=self.config.periodic_minimum_cycles,
            ),
        )
        contradicting: list[DecisionEvidence] = []
        if track.lifecycle == DecisionLifecycle.HOLDING:
            contradicting.append(
                DecisionEvidence(
                    code="RF.COMPONENT_TEMPORARILY_ABSENT",
                    explanation_ru=(
                        "Компонент временно не наблюдается; действует release-hold."
                    ),
                )
            )
        if track.lifecycle == DecisionLifecycle.SUPPRESSED:
            contradicting.append(
                DecisionEvidence(
                    code=track.suppression_reason or "RF.SUPPRESSED",
                    explanation_ru=(
                        "Наблюдение не прошло требования temporal-подтверждения."
                    ),
                )
            )
        if track.family == RfFamily.UNKNOWN:
            contradicting.append(
                DecisionEvidence(
                    code="RF.FAMILY_ABSTAINED",
                    explanation_ru=(
                        "Система воздержалась от RF-класса из-за неоднозначных признаков."
                    ),
                )
            )
        if quality == DataQuality.LOW:
            contradicting.append(
                DecisionEvidence(
                    code="RF.LOW_DATA_QUALITY",
                    explanation_ru="Качество входного потока ограничивает решение.",
                )
            )
        if track.pending_family is not None:
            contradicting.append(
                DecisionEvidence(
                    code="RF.FAMILY_SWITCH_DEBOUNCE",
                    explanation_ru=(
                        "Новая RF-семья ещё не заменила текущую: требуется "
                        "несколько последовательных согласованных кадров."
                    ),
                    measured=track.pending_family_count,
                    threshold=self.config.family_switch_observations,
                )
            )
        missing: list[DecisionEvidence] = []
        if track.lifecycle == DecisionLifecycle.CANDIDATE:
            if strong_count < self.config.confirmation_observations:
                missing.append(
                    DecisionEvidence(
                        code="RF.NEED_MORE_COMPATIBLE_OBSERVATIONS",
                        explanation_ru=(
                            "Нужны дополнительные согласованные активные наблюдения."
                        ),
                        measured=strong_count,
                        threshold=self.config.confirmation_observations,
                    )
                )
            if dwell < self.config.minimum_confirm_dwell_seconds:
                missing.append(
                    DecisionEvidence(
                        code="RF.NEED_MINIMUM_DWELL",
                        explanation_ru="Минимальная реальная выдержка ещё не достигнута.",
                        measured=dwell,
                        threshold=self.config.minimum_confirm_dwell_seconds,
                    )
                )
            if track.family == RfFamily.UNKNOWN:
                missing.append(
                    DecisionEvidence(
                        code="RF.NEED_STABLE_FAMILY",
                        explanation_ru="Нужен устойчивый общий RF-класс.",
                    )
                )
        return self._make_decision(
            source_id=track.source_id,
            observed_at=observed_at,
            lifecycle=track.lifecycle,
            family=track.family,
            track=track,
            quality=quality,
            score=score,
            supporting=supporting,
            contradicting=tuple(contradicting),
            extra_missing=tuple(missing),
        )

    def _make_decision(
        self,
        *,
        source_id: str,
        observed_at: datetime,
        lifecycle: DecisionLifecycle,
        family: RfFamily,
        track: _Track | None,
        quality: DataQuality,
        score: float,
        supporting: tuple[DecisionEvidence, ...],
        contradicting: tuple[DecisionEvidence, ...],
        extra_missing: tuple[DecisionEvidence, ...],
    ) -> RfDecision:
        score = _clamp01(score)
        missing = (
            *extra_missing,
            DecisionEvidence(
                code="RF.INDEPENDENT_CONFIRMATION_MISSING",
                explanation_ru=(
                    "Независимый сенсор не подтвердил это RF-наблюдение."
                ),
            ),
            DecisionEvidence(
                code="RF.EMITTER_IDENTITY_UNAVAILABLE",
                explanation_ru=(
                    "Тип физического источника по одному RF-наблюдению не установлен."
                ),
            ),
        )
        strength = _evidence_strength(
            lifecycle=lifecycle,
            score=score,
            strong_count=_strong_count(track) if track is not None else 0,
            required=self.config.confirmation_observations,
        )
        contribution = _clamp01(
            score
            * {
                DataQuality.LOW: 0.35,
                DataQuality.MEDIUM: 0.75,
                DataQuality.HIGH: 1.0,
            }[quality]
        )
        return RfDecision(
            source_id=source_id,
            observed_at=observed_at,
            lifecycle=lifecycle,
            family=family,
            family_explanation_ru=RF_FAMILY_EXPLANATIONS_RU[family],
            episode_id=track.episode_id if track is not None else None,
            started_at=track.started_at if track is not None else None,
            last_active_at=track.last_active_at if track is not None else None,
            peak_frequency_hz=(
                track.component.center_hz if track is not None else None
            ),
            occupied_bandwidth_hz=(
                track.component.bandwidth_hz if track is not None else None
            ),
            heuristic_score=score,
            calibrated_probability=None,
            evidence_strength=strength,
            data_quality=quality,
            alertable=lifecycle
            in {DecisionLifecycle.CONFIRMED, DecisionLifecycle.HOLDING},
            abstained=family == RfFamily.UNKNOWN,
            supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            missing_confirmation=missing,
            sensor_contributions=(
                SensorContribution(
                    source_id=source_id,
                    contribution=contribution,
                    data_quality=quality,
                    independent_confirmation=False,
                    explanation_ru=(
                        "Вклад основан на одном RF-источнике и не является "
                        "независимым подтверждением."
                    ),
                ),
            ),
            alternatives=_alternatives_for(family),
            limitations=_decision_limitations(track),
        )

    @staticmethod
    def _transition(
        track: _Track,
        kind: DecisionTransitionKind,
        occurred_at: datetime,
        reason_code: str,
        explanation_ru: str,
    ) -> DecisionTransition:
        transition_id = _stable_digest(
            f"{track.episode_id}|{kind.value}|{occurred_at.isoformat()}"
        )
        return DecisionTransition(
            transition_id=f"rf-transition-{transition_id}",
            episode_id=track.episode_id,
            source_id=track.source_id,
            kind=kind,
            occurred_at=occurred_at,
            family=track.family,
            reason_code=reason_code,
            explanation_ru=explanation_ru,
        )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DecisionInputError(f"{name} must be timezone-aware")


def _must_hold_data(analysis: AnalysisResult) -> bool:
    if analysis.assessment.state in {
        AssessmentState.NO_DATA,
        AssessmentState.LEARNING_BACKGROUND,
        AssessmentState.DATA_UNRELIABLE,
    }:
        return True
    hard_flags = {
        QualityFlag.DROPPED_FRAMES_REPORTED,
        QualityFlag.SEQUENCE_GAP,
        QualityFlag.DATA_STALE,
        QualityFlag.CLOCK_REGRESSION,
        QualityFlag.INSUFFICIENT_HISTORY,
        QualityFlag.SPECTRAL_GRID_CHANGED,
    }
    return bool(analysis.quality_flags & hard_flags)


def _data_quality(analysis: AnalysisResult) -> DataQuality:
    if _must_hold_data(analysis):
        return DataQuality.LOW
    if analysis.quality_flags:
        return DataQuality.MEDIUM
    return DataQuality.HIGH


def _peak_excess(analysis: AnalysisResult) -> float:
    raw = analysis.assessment.evidence.peak_excess_over_floor_db
    if raw is None:
        return 0.0
    if not math.isfinite(raw):
        raise DecisionInputError("peak excess must be finite")
    return float(max(0.0, raw))


def _require_event(analysis: AnalysisResult) -> RfEvent:
    if analysis.event is None:
        raise DecisionInputError("analysis event is required")
    return analysis.event


def _component(event: RfEvent) -> _Component:
    evidence = event.evidence
    values = (
        evidence.peak_frequency_hz,
        evidence.frequency_low_hz,
        evidence.frequency_high_hz,
        evidence.occupied_bandwidth_hz,
    )
    if any(not math.isfinite(value) for value in values):
        raise DecisionInputError("RF component values must be finite")
    low = float(evidence.frequency_low_hz)
    high = float(evidence.frequency_high_hz)
    bandwidth = float(evidence.occupied_bandwidth_hz)
    center = float(evidence.peak_frequency_hz)
    if low > high or bandwidth <= 0.0 or center < 0.0:
        raise DecisionInputError("invalid RF component geometry")
    return _Component(
        center_hz=center,
        low_hz=low,
        high_hz=high,
        bandwidth_hz=bandwidth,
    )


def _is_single_bin_like(event: RfEvent) -> bool:
    evidence = event.evidence
    return evidence.active_bin_count == 1


def _is_non_alertable_single_observation(event: RfEvent) -> bool:
    return (
        event.classification == EventClass.TRANSIENT_BURST
        or _is_single_bin_like(event)
    )


def _observation(
    event: RfEvent,
    component: _Component,
    observed_at: datetime,
    *,
    peak_excess: float,
    present: bool,
    strong: bool,
) -> _Observation:
    return _Observation(
        observed_at=observed_at,
        temporal_at=observed_at,
        component=component,
        peak_excess_db=peak_excess,
        heuristic_score=event.confidence,
        present=present,
        strong=strong,
        event_class=event.classification,
        acquisition_mode=event.evidence.acquisition_mode,
        sweep_duration_ms=event.evidence.sweep_duration_ms,
        limitations=event.evidence.limitations,
    )


def _components_compatible(
    left: _Component,
    right: _Component,
    config: TemporalDecisionConfig,
) -> bool:
    intersection = max(
        0.0,
        min(left.high_hz, right.high_hz) - max(left.low_hz, right.low_hz),
    )
    reference_width = max(
        1.0,
        min(
            max(left.high_hz - left.low_hz, left.bandwidth_hz),
            max(right.high_hz - right.low_hz, right.bandwidth_hz),
        ),
    )
    overlap = intersection / reference_width
    drift_limit = max(
        config.maximum_center_drift_hz,
        config.maximum_center_drift_bandwidths
        * max(left.bandwidth_hz, right.bandwidth_hz),
    )
    center_drift = abs(left.center_hz - right.center_hz)
    return overlap >= config.minimum_component_overlap or center_drift <= drift_limit


def _point_compatible(
    component: _Component,
    frequency_hz: float,
    config: TemporalDecisionConfig,
) -> bool:
    if not math.isfinite(frequency_hz):
        return False
    drift_limit = max(
        config.maximum_center_drift_hz,
        config.maximum_center_drift_bandwidths * component.bandwidth_hz,
    )
    return abs(component.center_hz - frequency_hz) <= drift_limit


def _smoothed_component(previous: _Component, current: _Component) -> _Component:
    alpha = 0.25
    return _Component(
        center_hz=(1.0 - alpha) * previous.center_hz + alpha * current.center_hz,
        low_hz=(1.0 - alpha) * previous.low_hz + alpha * current.low_hz,
        high_hz=(1.0 - alpha) * previous.high_hz + alpha * current.high_hz,
        bandwidth_hz=(1.0 - alpha) * previous.bandwidth_hz
        + alpha * current.bandwidth_hz,
    )


def _latest_track(
    state: _SourceState,
    *,
    lifecycles: set[DecisionLifecycle],
) -> _Track | None:
    return next(
        (
            track
            for track in reversed(state.tracks)
            if track.lifecycle in lifecycles
        ),
        None,
    )


def _shift_open_tracks(state: _SourceState, offset: timedelta) -> None:
    """Freeze episode time while the input is explicitly on data hold."""

    if offset <= timedelta(0):
        return
    open_lifecycles = {
        DecisionLifecycle.CANDIDATE,
        DecisionLifecycle.CONFIRMED,
        DecisionLifecycle.HOLDING,
    }
    for track in state.tracks:
        if track.lifecycle not in open_lifecycles:
            continue
        track.temporal_started_at += offset
        track.temporal_last_active_at += offset
        track.observations = deque(
            (
                _Observation(
                    observed_at=item.observed_at,
                    temporal_at=item.temporal_at + offset,
                    component=item.component,
                    peak_excess_db=item.peak_excess_db,
                    heuristic_score=item.heuristic_score,
                    present=item.present,
                    strong=item.strong,
                    event_class=item.event_class,
                    acquisition_mode=item.acquisition_mode,
                    sweep_duration_ms=item.sweep_duration_ms,
                    limitations=item.limitations,
                )
                for item in track.observations
            ),
            maxlen=track.observations.maxlen,
        )


def _strong_count(track: _Track | None) -> int:
    if track is None:
        return 0
    return sum(item.strong for item in track.observations)


def _strong_dwell(track: _Track) -> float:
    strong = [item for item in track.observations if item.strong]
    if len(strong) < 2:
        return 0.0
    return max(
        0.0,
        (strong[-1].temporal_at - strong[0].temporal_at).total_seconds(),
    )


def _active_run_start_times(
    observations: tuple[_Observation, ...],
) -> tuple[datetime, ...]:
    starts: list[datetime] = []
    previous_present = False
    for item in observations:
        if item.present and item.strong and not previous_present:
            starts.append(item.temporal_at)
        previous_present = item.present
    return tuple(starts)


def _periodicity_is_supported(
    active_run_times: tuple[datetime, ...],
    observations: tuple[_Observation, ...],
    config: TemporalDecisionConfig,
) -> bool:
    if len(active_run_times) < config.periodic_minimum_cycles:
        return False
    intervals = [
        (current - previous).total_seconds()
        for previous, current in pairwise(active_run_times)
    ]
    if not intervals or any(value <= 0.0 for value in intervals):
        return False
    mean_interval = sum(intervals) / len(intervals)
    variance = sum(
        (value - mean_interval) ** 2 for value in intervals
    ) / len(intervals)
    coefficient_of_variation = math.sqrt(variance) / mean_interval
    if coefficient_of_variation > config.periodic_maximum_interval_cv:
        return False

    active = [item for item in observations if item.present and item.strong]
    if not active or any(
        item.acquisition_mode == SpectrumAcquisitionMode.UNKNOWN
        for item in active
    ):
        return False
    swept = [
        item
        for item in active
        if item.acquisition_mode == SpectrumAcquisitionMode.SWEPT_SPECTRUM
    ]
    if not swept:
        return True
    sweep_durations = [
        item.sweep_duration_ms / 1000.0
        for item in swept
        if item.sweep_duration_ms is not None
    ]
    # With unknown sweep duration the apparent repetition may simply be sweep
    # cadence aliasing.  Even with metadata, demand two sweep durations between
    # run starts before describing a periodic family.
    return (
        len(sweep_durations) == len(swept)
        and min(intervals) >= 2.0 * max(sweep_durations)
    )


def _track_heuristic_score(
    track: _Track,
    config: TemporalDecisionConfig,
) -> float:
    active = [item for item in track.observations if item.present]
    if not active:
        return _clamp01(track.maximum_score)
    average = sum(item.heuristic_score for item in active) / len(active)
    recurrence_count = len(
        _active_run_start_times(tuple(track.observations))
    )
    # A single frame remains capped by the raw detector.  Only repeated,
    # frequency-compatible active runs add temporal evidence.
    recurrence_bonus = (
        max(0, recurrence_count - 1) * config.recurrence_score_bonus
    )
    return _clamp01(average + recurrence_bonus)


def _trailing_absent_count(track: _Track) -> int:
    count = 0
    for item in reversed(track.observations):
        if item.present:
            break
        count += 1
    return count


def _episode_id(
    *,
    source_id: str,
    component: _Component,
    observed_at: datetime,
    raw_event_id: str,
) -> str:
    identity = (
        f"{source_id}|{component.center_hz:.3f}|{component.bandwidth_hz:.3f}|"
        f"{observed_at.isoformat()}|{raw_event_id}"
    )
    return f"rf-episode-{_stable_digest(identity)}"


def _alternatives_for(
    family: RfFamily,
) -> tuple[DecisionAlternative, ...]:
    alternatives: dict[RfFamily, tuple[DecisionAlternative, ...]] = {
        RfFamily.BACKGROUND: (),
        RfFamily.CARRIER: (
            DecisionAlternative(
                RfFamily.VOICE_LIKE,
                "Более быстрые одновременные кадры могут выявить изменяющуюся огибающую.",
            ),
            DecisionAlternative(
                RfFamily.INTERFERENCE_NOISE_LIKE,
                "Устойчивая линия также может быть локальным аппаратным spur.",
            ),
        ),
        RfFamily.NARROWBAND_BURST: (
            DecisionAlternative(
                RfFamily.CARRIER,
                "Более длительное непрерывное наблюдение может подтвердить несущую.",
            ),
            DecisionAlternative(
                RfFamily.PACKET_LIKE,
                "Повторение после паузы может подтвердить packet-like форму.",
            ),
        ),
        RfFamily.BROADBAND_BURST: (
            DecisionAlternative(
                RfFamily.INTERFERENCE_NOISE_LIKE,
                "Устойчивое продолжение может указывать на noise-like изменение тракта.",
            ),
            DecisionAlternative(
                RfFamily.UNKNOWN,
                "Короткая длительность не исключает артефакт или локальную помеху.",
            ),
        ),
        RfFamily.PACKET_LIKE: (
            DecisionAlternative(
                RfFamily.PERIODIC_BEACON_LIKE,
                "Нужны дополнительные циклы с устойчивым интервалом.",
            ),
            DecisionAlternative(
                RfFamily.NARROWBAND_BURST,
                "Паузы могут быть пропусками приёма, а не свойством сигнала.",
            ),
        ),
        RfFamily.VOICE_LIKE: (
            DecisionAlternative(
                RfFamily.CARRIER,
                "Изменение уровня может быть федингом устойчивой несущей.",
            ),
            DecisionAlternative(
                RfFamily.NARROWBAND_BURST,
                "Наблюдаемая серия может быть коротким узкополосным эпизодом.",
            ),
        ),
        RfFamily.PERIODIC_BEACON_LIKE: (
            DecisionAlternative(
                RfFamily.PACKET_LIKE,
                "Ограниченное число циклов ещё совместимо с непериодическими пакетами.",
            ),
            DecisionAlternative(
                RfFamily.UNKNOWN,
                "Каденс приёмника может создавать временное алиасирование.",
            ),
        ),
        RfFamily.INTERFERENCE_NOISE_LIKE: (
            DecisionAlternative(
                RfFamily.BROADBAND_BURST,
                "При завершении эпизода форма может оказаться ограниченным burst.",
            ),
            DecisionAlternative(
                RfFamily.UNKNOWN,
                "Изменение усиления или радиотракта нельзя исключить по одному сенсору.",
            ),
        ),
        RfFamily.UNKNOWN: (
            DecisionAlternative(
                RfFamily.NARROWBAND_BURST,
                "Нужна устойчивая узкополосная форма в дополнительных кадрах.",
            ),
            DecisionAlternative(
                RfFamily.BROADBAND_BURST,
                "Нужна подтверждённая одновременная широкополосная форма.",
            ),
            DecisionAlternative(
                RfFamily.INTERFERENCE_NOISE_LIKE,
                "Нужно длительное наблюдение за широкополосной формой.",
            ),
        ),
    }
    return alternatives.get(family, ())


def _decision_limitations(
    track: _Track | None,
) -> tuple[DecisionEvidence, ...]:
    result = [
        DecisionEvidence(
            code="RF.HEURISTIC_NOT_PROBABILITY",
            explanation_ru=(
                "Числовой score — детерминированная эвристика силы признаков, "
                "а не калиброванная вероятность."
            ),
        ),
        DecisionEvidence(
            code="RF.NO_EMITTER_ATTRIBUTION",
            explanation_ru=(
                "Спектральная форма одного RF-приёмника не устанавливает "
                "физический источник, направление или дальность."
            ),
        ),
    ]
    seen = {item.code for item in result}
    if track is not None:
        for observation in track.observations:
            for index, limitation in enumerate(observation.limitations, start=1):
                code = f"RF.ACQUISITION_LIMITATION_{index}"
                if code in seen:
                    continue
                seen.add(code)
                result.append(
                    DecisionEvidence(
                        code=code,
                        explanation_ru=limitation,
                    )
                )
    return tuple(result)


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _evidence_strength(
    *,
    lifecycle: DecisionLifecycle,
    score: float,
    strong_count: int,
    required: int,
) -> EvidenceStrength:
    if lifecycle in {
        DecisionLifecycle.IDLE,
        DecisionLifecycle.SUPPRESSED,
        DecisionLifecycle.DATA_HOLD,
        DecisionLifecycle.RESOLVED,
    }:
        return EvidenceStrength.LOW
    if (
        lifecycle == DecisionLifecycle.CONFIRMED
        and score >= 0.75
        and strong_count > required
    ):
        return EvidenceStrength.HIGH
    if lifecycle in {
        DecisionLifecycle.CONFIRMED,
        DecisionLifecycle.HOLDING,
    } or strong_count >= 2:
        return EvidenceStrength.MEDIUM
    return EvidenceStrength.LOW


def _clamp01(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


__all__ = [
    "RF_FAMILY_EXPLANATIONS_RU",
    "DataQuality",
    "DecisionAlternative",
    "DecisionEvidence",
    "DecisionInputError",
    "DecisionLifecycle",
    "DecisionTransition",
    "DecisionTransitionKind",
    "DecisionUpdate",
    "EvidenceStrength",
    "RfDecision",
    "RfDecisionEngine",
    "RfFamily",
    "SensorContribution",
    "TemporalDecisionConfig",
]
