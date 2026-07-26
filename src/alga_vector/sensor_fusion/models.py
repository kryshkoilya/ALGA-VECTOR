"""Safe, normalized contracts for generic sensor correlation.

The contracts describe observations and temporal correlation only.  They do
not assign a physical identity or intent to the measured activity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FusionInputError(ValueError):
    """An observation cannot safely advance deterministic fusion state."""


class SensorModality(StrEnum):
    """Normalized input modalities understood by the fusion core."""

    RF = "rf"
    ACOUSTIC = "acoustic"
    DIRECTION = "direction"
    CIVIL_ADSB = "civil_adsb"


class FusionClassification(StrEnum):
    """Generic output classes that make no physical identity claim."""

    BACKGROUND = "background"
    UNCONFIRMED_ANOMALY = "unconfirmed_anomaly"
    RF_ACTIVITY = "rf_activity"
    ACOUSTIC_ANOMALY = "acoustic_anomaly"
    MULTI_SENSOR_CORRELATED = "multi_sensor_correlated"
    NEARBY_COOPERATIVE_AIRCRAFT_CONTEXT = (
        "nearby_cooperative_aircraft_context"
    )


class FusionLifecycle(StrEnum):
    """Temporal state of the current generic correlation episode."""

    IDLE = "idle"
    INFORMATIONAL = "informational"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    HOLDING = "holding"
    RESOLVED = "resolved"


class EvidenceStrength(StrEnum):
    """Heuristic evidence strength, never a calibrated probability."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FusionTransitionKind(StrEnum):
    """Deduplicated externally meaningful lifecycle transitions."""

    CONFIRMED = "confirmed"
    RESOLVED = "resolved"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_unit_interval(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be finite and within 0..1")


def _normalized_text(value: str, field_name: str, maximum_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _normalized_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("evidence keys must be strings")
        key = _normalized_text(raw, "evidence key", 128)
        if key not in seen:
            normalized.append(key)
            seen.add(key)
    if len(normalized) > 32:
        raise ValueError("at most 32 evidence keys are accepted")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class FusionObservation:
    """One normalized observation from a single source.

    ``quality`` and ``strength`` are normalized heuristic values in ``0..1``.
    ``validated`` is consulted only for direction observations; the safe
    default is ``False``.

    ``evidence`` is an initializer alias for integrations that do not use the
    more explicit ``evidence_keys`` name.  Both attributes are normalized to
    the same immutable tuple.
    """

    modality: SensorModality
    timestamp: datetime
    source_id: str
    quality: float
    strength: float
    summary: str
    evidence_keys: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    validated: bool = False

    def __post_init__(self) -> None:
        try:
            modality = SensorModality(self.modality)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported sensor modality") from exc
        object.__setattr__(self, "modality", modality)
        _require_aware(self.timestamp, "timestamp")
        object.__setattr__(
            self,
            "source_id",
            _normalized_text(self.source_id, "source_id", 128),
        )
        _require_unit_interval(self.quality, "quality")
        _require_unit_interval(self.strength, "strength")
        object.__setattr__(
            self,
            "summary",
            _normalized_text(self.summary, "summary", 512),
        )
        if not isinstance(self.validated, bool):
            raise ValueError("validated must be a boolean")
        explicit_keys = _normalized_keys(tuple(self.evidence_keys))
        alias_keys = _normalized_keys(tuple(self.evidence))
        if explicit_keys and alias_keys and explicit_keys != alias_keys:
            raise ValueError("evidence and evidence_keys disagree")
        keys = explicit_keys or alias_keys
        object.__setattr__(self, "evidence_keys", keys)
        object.__setattr__(self, "evidence", keys)

    @property
    def observed_at(self) -> datetime:
        """Compatibility-friendly timestamp alias."""

        return self.timestamp

    @property
    def source(self) -> str:
        """Compatibility-friendly source identifier alias."""

        return self.source_id


@dataclass(frozen=True, slots=True)
class FusionConfig:
    """Validated deterministic thresholds for the temporal fusion core."""

    temporal_window_seconds: float = 3.0
    direction_freshness_seconds: float = 1.0
    civil_adsb_context_seconds: float = 5.0
    minimum_observations: int = 3
    minimum_quality: float = 0.55
    attack_strength: float = 0.60
    release_strength: float = 0.40
    minimum_correlation_dwell_seconds: float = 0.20
    release_hold_seconds: float = 0.80
    candidate_timeout_seconds: float = 3.0
    debounce_seconds: float = 2.0
    maximum_history: int = 256
    maximum_streams: int = 64

    def __post_init__(self) -> None:
        for name, value in (
            ("temporal_window_seconds", self.temporal_window_seconds),
            ("direction_freshness_seconds", self.direction_freshness_seconds),
            ("civil_adsb_context_seconds", self.civil_adsb_context_seconds),
            (
                "minimum_correlation_dwell_seconds",
                self.minimum_correlation_dwell_seconds,
            ),
            ("release_hold_seconds", self.release_hold_seconds),
            ("candidate_timeout_seconds", self.candidate_timeout_seconds),
            ("debounce_seconds", self.debounce_seconds),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.temporal_window_seconds <= 0.0:
            raise ValueError("temporal_window_seconds must be positive")
        if self.direction_freshness_seconds <= 0.0:
            raise ValueError("direction_freshness_seconds must be positive")
        if self.civil_adsb_context_seconds <= 0.0:
            raise ValueError("civil_adsb_context_seconds must be positive")
        if self.release_hold_seconds <= 0.0:
            raise ValueError("release_hold_seconds must be positive")
        if self.candidate_timeout_seconds <= 0.0:
            raise ValueError("candidate_timeout_seconds must be positive")
        if self.minimum_observations < 3:
            raise ValueError("minimum_observations must be at least 3")
        for name, value in (
            ("minimum_quality", self.minimum_quality),
            ("attack_strength", self.attack_strength),
            ("release_strength", self.release_strength),
        ):
            _require_unit_interval(value, name)
        if self.attack_strength <= self.release_strength:
            raise ValueError("attack_strength must be above release_strength")
        if self.maximum_history < self.minimum_observations:
            raise ValueError("maximum_history must fit minimum_observations")
        if self.maximum_streams < 4:
            raise ValueError("maximum_streams must be at least 4")


type EvidenceValue = float | int | str | None


@dataclass(frozen=True, slots=True)
class FusionEvidence:
    """One explainable fact in a fusion decision chain."""

    code: str
    explanation: str
    modalities: tuple[SensorModality, ...] = ()
    source_ids: tuple[str, ...] = ()
    evidence_keys: tuple[str, ...] = ()
    measured: EvidenceValue = None
    threshold: EvidenceValue = None
    confirming: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _normalized_text(self.code, "code", 128))
        object.__setattr__(
            self,
            "explanation",
            _normalized_text(self.explanation, "explanation", 1024),
        )
        if len(set(self.modalities)) != len(self.modalities):
            raise ValueError("evidence modalities must be unique")
        normalized_sources = tuple(
            _normalized_text(value, "source_id", 128) for value in self.source_ids
        )
        if len(set(normalized_sources)) != len(normalized_sources):
            raise ValueError("evidence source_ids must be unique")
        object.__setattr__(self, "source_ids", normalized_sources)
        object.__setattr__(
            self,
            "evidence_keys",
            _normalized_keys(tuple(self.evidence_keys)),
        )
        for value in (self.measured, self.threshold):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric evidence must be finite")


@dataclass(frozen=True, slots=True)
class FusionContribution:
    """Traceable contribution from one normalized sensor stream."""

    modality: SensorModality
    source_id: str
    observation_count: int
    mean_quality: float
    mean_strength: float
    confirming: bool
    context_only: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _normalized_text(self.source_id, "source_id", 128),
        )
        if self.observation_count < 1:
            raise ValueError("observation_count must be positive")
        _require_unit_interval(self.mean_quality, "mean_quality")
        _require_unit_interval(self.mean_strength, "mean_strength")
        if self.confirming and self.context_only:
            raise ValueError("context-only contribution cannot confirm")


@dataclass(frozen=True, slots=True)
class FusionDecision:
    """Current generic result with a complete explainability chain."""

    evaluated_at: datetime
    classification: FusionClassification
    lifecycle: FusionLifecycle
    evidence_strength: EvidenceStrength
    alertable: bool
    episode_id: str | None
    started_at: datetime | None
    last_active_at: datetime | None
    observation_count: int
    active_modalities: tuple[SensorModality, ...]
    active_source_ids: tuple[str, ...]
    evidence: tuple[FusionEvidence, ...]
    contradictions: tuple[FusionEvidence, ...]
    missing: tuple[FusionEvidence, ...]
    limitations: tuple[FusionEvidence, ...]
    contributions: tuple[FusionContribution, ...]
    debounced: bool = False
    calibrated_probability: None = None

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        if self.started_at is not None:
            _require_aware(self.started_at, "started_at")
        if self.last_active_at is not None:
            _require_aware(self.last_active_at, "last_active_at")
        if self.observation_count < 0:
            raise ValueError("observation_count must be non-negative")
        if self.episode_id is not None and not self.episode_id.strip():
            raise ValueError("episode_id must not be blank")
        episode_lifecycles = {
            FusionLifecycle.CANDIDATE,
            FusionLifecycle.CONFIRMED,
            FusionLifecycle.HOLDING,
            FusionLifecycle.RESOLVED,
        }
        if self.lifecycle in episode_lifecycles and self.episode_id is None:
            raise ValueError("episode lifecycle requires episode_id")
        if self.lifecycle not in episode_lifecycles and self.episode_id is not None:
            raise ValueError("non-episode lifecycle must not carry episode_id")
        if self.alertable and (
            self.classification is not FusionClassification.MULTI_SENSOR_CORRELATED
            or self.lifecycle
            not in {FusionLifecycle.CONFIRMED, FusionLifecycle.HOLDING}
        ):
            raise ValueError("only confirmed multi-sensor correlation is alertable")
        if self.calibrated_probability is not None:
            raise ValueError("calibrated_probability is unavailable")
        if len(set(self.active_modalities)) != len(self.active_modalities):
            raise ValueError("active modalities must be unique")
        if len(set(self.active_source_ids)) != len(self.active_source_ids):
            raise ValueError("active source_ids must be unique")

    @property
    def supporting_evidence(self) -> tuple[FusionEvidence, ...]:
        return self.evidence

    @property
    def contradicting_evidence(self) -> tuple[FusionEvidence, ...]:
        return self.contradictions

    @property
    def missing_confirmation(self) -> tuple[FusionEvidence, ...]:
        return self.missing

    @property
    def outcome(self) -> FusionClassification:
        return self.classification


@dataclass(frozen=True, slots=True)
class FusionTransition:
    """One deterministic, deduplicated lifecycle transition."""

    transition_id: str
    episode_id: str
    kind: FusionTransitionKind
    occurred_at: datetime
    classification: FusionClassification
    reason_code: str
    explanation: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("transition_id", self.transition_id),
            ("episode_id", self.episode_id),
            ("reason_code", self.reason_code),
            ("explanation", self.explanation),
        ):
            _normalized_text(value, field_name, 1024)
        _require_aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class FusionUpdate:
    """Current decision plus at most one publishable transition."""

    decision: FusionDecision
    transition: FusionTransition | None = None

    def __post_init__(self) -> None:
        transition = self.transition
        if transition is None:
            return
        if transition.episode_id != self.decision.episode_id:
            raise ValueError("transition and decision episode ids must match")
        expected_lifecycle = {
            FusionTransitionKind.CONFIRMED: FusionLifecycle.CONFIRMED,
            FusionTransitionKind.RESOLVED: FusionLifecycle.RESOLVED,
        }[transition.kind]
        if self.decision.lifecycle is not expected_lifecycle:
            raise ValueError("transition kind does not match decision lifecycle")


FusionOutcome = FusionClassification
Observation = FusionObservation
SensorType = SensorModality


__all__ = [
    "EvidenceStrength",
    "FusionClassification",
    "FusionConfig",
    "FusionContribution",
    "FusionDecision",
    "FusionEvidence",
    "FusionInputError",
    "FusionLifecycle",
    "FusionObservation",
    "FusionOutcome",
    "FusionTransition",
    "FusionTransitionKind",
    "FusionUpdate",
    "Observation",
    "SensorModality",
    "SensorType",
]
