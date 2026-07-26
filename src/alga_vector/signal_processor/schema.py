"""Serializable, fail-closed contracts for the operator event pipeline.

The numeric ``ConfidenceScore`` is deliberately an evidence-strength score.
It is never presented as a calibrated probability.  Identity-like events are
guarded at construction time so a frequency, RSSI value, or one generic RF
receiver cannot create a drone/target assertion.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final


class EventPolicyViolation(ValueError):
    """A normalized event would overstate what its evidence can establish."""


class NormalizedEventType(StrEnum):
    NOISE_BACKGROUND = "NOISE_BACKGROUND"
    RADIO_ACTIVITY_DETECTED = "RADIO_ACTIVITY_DETECTED"
    LIKELY_HANDHELD_RADIO = "LIKELY_HANDHELD_RADIO"
    LIKELY_VIDEO_LINK = "LIKELY_VIDEO_LINK"
    LIKELY_DRONE_SIGNATURE = "LIKELY_DRONE_SIGNATURE"
    ADSB_CONTACT = "ADSB_CONTACT"
    ACOUSTIC_ANOMALY = "ACOUSTIC_ANOMALY"
    DIRECTION_ESTIMATED = "DIRECTION_ESTIMATED"
    MULTISENSOR_CORRELATED = "MULTISENSOR_CORRELATED"
    TARGET_CONFIRMED = "TARGET_CONFIRMED"
    SENSOR_UNAVAILABLE = "SENSOR_UNAVAILABLE"


class EventSeverity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ALARM = "alarm"
    CRITICAL = "critical"


class SensorKind(StrEnum):
    RF_TRIGGER = "rf_trigger"
    RF_SPECTRUM = "rf_spectrum"
    DIRECTION_FINDER = "direction_finder"
    ACOUSTIC = "acoustic"
    ADSB = "adsb"
    PASSIVE_RADAR = "passive_radar"
    CAMERA = "camera"
    CLASSIFIER = "classifier"
    FUSION = "fusion"
    SYSTEM = "system"


class SensorAvailability(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STALE = "stale"


class ConfidenceBand(StrEnum):
    NOT_AVAILABLE = "not_available"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OperatorSituationMode(StrEnum):
    SILENCE = "silence"
    BACKGROUND = "background"
    ACTIVITY = "activity"
    CONFIRMED_TARGET = "confirmed_target"


_IDENTITY_LIKE_EVENTS: Final = frozenset(
    {
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        NormalizedEventType.TARGET_CONFIRMED,
    }
)
_HIGH_CONSEQUENCE_IDENTITY_EVENTS: Final = frozenset(
    {
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        NormalizedEventType.TARGET_CONFIRMED,
    }
)
_RF_ONLY_KINDS: Final = frozenset(
    {SensorKind.RF_TRIGGER, SensorKind.RF_SPECTRUM}
)

type EvidenceValue = str | int | float | bool | None


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _clean_text(value: str, field_name: str, maximum: int = 2_048) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{field_name} is too long")
    return cleaned


def _finite_unit_interval(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be finite and within 0..1")


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """Heuristic evidence strength, explicitly not a probability."""

    value: float | None
    band: ConfidenceBand
    basis_ru: str
    is_calibrated_probability: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "basis_ru",
            _clean_text(self.basis_ru, "basis_ru", 1_024),
        )
        if self.is_calibrated_probability:
            raise EventPolicyViolation(
                "operator confidence must not claim calibrated probability"
            )
        if self.value is None:
            if self.band is not ConfidenceBand.NOT_AVAILABLE:
                raise ValueError("missing confidence must use NOT_AVAILABLE")
            return
        _finite_unit_interval(self.value, "confidence value")
        if self.band is ConfidenceBand.NOT_AVAILABLE:
            raise ValueError("numeric confidence requires a qualitative band")

    @classmethod
    def heuristic(cls, value: float, basis_ru: str) -> ConfidenceScore:
        _finite_unit_interval(value, "confidence value")
        band = (
            ConfidenceBand.LOW
            if value < 0.4
            else ConfidenceBand.MEDIUM
            if value < 0.75
            else ConfidenceBand.HIGH
        )
        return cls(value=value, band=band, basis_ru=basis_ru)

    @classmethod
    def unavailable(cls, basis_ru: str) -> ConfidenceScore:
        return cls(
            value=None,
            band=ConfidenceBand.NOT_AVAILABLE,
            basis_ru=basis_ru,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "band": self.band.value,
            "basis_ru": self.basis_ru,
            "is_calibrated_probability": False,
        }

    @property
    def score(self) -> float | None:
        return self.value

    @property
    def level(self) -> ConfidenceBand:
        return self.band

    @property
    def evidence_strength(self) -> ConfidenceBand:
        return self.band

    @property
    def explanation_ru(self) -> str:
        return self.basis_ru


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    code: str
    explanation_ru: str
    source_id: str | None = None
    measured: EvidenceValue = None
    unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _clean_text(self.code, "code", 128))
        object.__setattr__(
            self,
            "explanation_ru",
            _clean_text(self.explanation_ru, "explanation_ru", 1_024),
        )
        if self.source_id is not None:
            object.__setattr__(
                self,
                "source_id",
                _clean_text(self.source_id, "source_id", 128),
            )
        if self.unit is not None:
            object.__setattr__(self, "unit", _clean_text(self.unit, "unit", 32))
        if isinstance(self.measured, float) and not math.isfinite(self.measured):
            raise ValueError("measured numeric evidence must be finite")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "explanation_ru": self.explanation_ru,
            "source_id": self.source_id,
            "measured": self.measured,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    sensor_id: str
    sensor_kind: SensorKind
    contribution: float
    independent_confirmation: bool
    explanation_ru: str
    observation_id: str | None = None
    provenance: str = "live"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sensor_id",
            _clean_text(self.sensor_id, "sensor_id", 128),
        )
        _finite_unit_interval(self.contribution, "contribution")
        object.__setattr__(
            self,
            "explanation_ru",
            _clean_text(self.explanation_ru, "explanation_ru", 1_024),
        )
        if self.observation_id is not None:
            object.__setattr__(
                self,
                "observation_id",
                _clean_text(self.observation_id, "observation_id", 256),
            )
        object.__setattr__(
            self,
            "provenance",
            _clean_text(self.provenance, "provenance", 64),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "sensor_id": self.sensor_id,
            "sensor_kind": self.sensor_kind.value,
            "contribution": self.contribution,
            "independent_confirmation": self.independent_confirmation,
            "explanation_ru": self.explanation_ru,
            "observation_id": self.observation_id,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class SensorState:
    sensor_id: str
    sensor_kind: SensorKind
    availability: SensorAvailability
    message_ru: str
    checked_at: datetime
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sensor_id",
            _clean_text(self.sensor_id, "sensor_id", 128),
        )
        object.__setattr__(
            self,
            "message_ru",
            _clean_text(self.message_ru, "message_ru", 1_024),
        )
        _require_aware(self.checked_at, "checked_at")
        cleaned = tuple(
            _clean_text(item, "capability", 128) for item in self.capabilities
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("sensor capabilities must be unique")
        object.__setattr__(self, "capabilities", cleaned)

    def to_dict(self) -> dict[str, object]:
        return {
            "sensor_id": self.sensor_id,
            "sensor_kind": self.sensor_kind.value,
            "availability": self.availability.value,
            "message_ru": self.message_ru,
            "checked_at": self.checked_at.isoformat(),
            "capabilities": list(self.capabilities),
        }

    @property
    def available(self) -> bool:
        return self.availability in {
            SensorAvailability.AVAILABLE,
            SensorAvailability.DEGRADED,
        }


@dataclass(frozen=True, slots=True)
class DirectionEstimate:
    bearing_deg: float
    uncertainty_deg: float
    source_id: str
    observed_at: datetime
    valid_until: datetime
    confidence: float
    validated_external: bool
    calibration_id: str

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.bearing_deg)
            or not 0.0 <= self.bearing_deg < 360.0
        ):
            raise ValueError("bearing_deg must be finite and within [0, 360)")
        if (
            not math.isfinite(self.uncertainty_deg)
            or not 0.0 <= self.uncertainty_deg <= 180.0
        ):
            raise ValueError("uncertainty_deg must be finite and within 0..180")
        _finite_unit_interval(self.confidence, "direction confidence")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.observed_at:
            raise ValueError("direction valid_until must follow observed_at")
        object.__setattr__(
            self,
            "source_id",
            _clean_text(self.source_id, "source_id", 128),
        )
        object.__setattr__(
            self,
            "calibration_id",
            _clean_text(self.calibration_id, "calibration_id", 128),
        )
        if not self.validated_external:
            raise EventPolicyViolation(
                "bearing is accepted only from fresh validated external DF"
            )

    def is_fresh_at(self, at: datetime) -> bool:
        _require_aware(at, "at")
        return self.observed_at <= at <= self.valid_until

    def to_dict(self) -> dict[str, object]:
        return {
            "bearing_deg": self.bearing_deg,
            "uncertainty_deg": self.uncertainty_deg,
            "source_id": self.source_id,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "confidence": self.confidence,
            "validated_external": True,
            "calibration_id": self.calibration_id,
        }

    @property
    def available(self) -> bool:
        return True

    @property
    def sector_text_ru(self) -> str:
        low = (self.bearing_deg - self.uncertainty_deg) % 360.0
        high = (self.bearing_deg + self.uncertainty_deg) % 360.0
        return (
            f"Сектор {low:.0f}–{high:.0f}° · "
            f"азимут {self.bearing_deg:.0f}°"
        )

    @property
    def explanation_ru(self) -> str:
        return (
            "Свежий валидный внешний пеленг; тип и дальность источника "
            "по нему не определяются."
        )


@dataclass(frozen=True, slots=True)
class ValidatedIdentityEvidence:
    """Traceable output of a separately validated identity classifier."""

    classifier_id: str
    model_version: str
    validation_dataset_id: str
    validated_at: datetime
    class_label: str
    independent_confirmation_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "classifier_id",
            "model_version",
            "validation_dataset_id",
            "class_label",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_text(str(getattr(self, field_name)), field_name, 256),
            )
        _require_aware(self.validated_at, "validated_at")
        cleaned = tuple(
            _clean_text(item, "confirmation source id", 128)
            for item in self.independent_confirmation_source_ids
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("independent confirmation source ids must be unique")
        object.__setattr__(
            self,
            "independent_confirmation_source_ids",
            cleaned,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "classifier_id": self.classifier_id,
            "model_version": self.model_version,
            "validation_dataset_id": self.validation_dataset_id,
            "validated_at": self.validated_at.isoformat(),
            "class_label": self.class_label,
            "independent_confirmation_source_ids": list(
                self.independent_confirmation_source_ids
            ),
        }


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    schema_version: str
    event_id: str
    event_type: NormalizedEventType
    observed_at: datetime
    received_at: datetime
    severity: EventSeverity
    confidence: ConfidenceScore
    summary_ru: str
    explanation_ru: str
    recommendation_ru: str
    sources: tuple[SourceAttribution, ...]
    evidence: tuple[EvidenceFact, ...] = ()
    limitations: tuple[str, ...] = ()
    frequency_hz: float | None = None
    bandwidth_hz: float | None = None
    direction: DirectionEstimate | None = None
    episode_id: str | None = None
    identity: ValidatedIdentityEvidence | None = None
    tags: tuple[str, ...] = ()
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            _clean_text(self.schema_version, "schema_version", 32),
        )
        object.__setattr__(
            self,
            "event_id",
            _clean_text(self.event_id, "event_id", 256),
        )
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.received_at, "received_at")
        if self.received_at < self.observed_at:
            raise ValueError("received_at cannot precede observed_at")
        for field_name in ("summary_ru", "explanation_ru", "recommendation_ru"):
            object.__setattr__(
                self,
                field_name,
                _clean_text(str(getattr(self, field_name)), field_name, 2_048),
            )
        if self.frequency_hz is not None and (
            not math.isfinite(self.frequency_hz) or self.frequency_hz < 0.0
        ):
            raise ValueError("frequency_hz must be finite and non-negative")
        if self.bandwidth_hz is not None and (
            not math.isfinite(self.bandwidth_hz) or self.bandwidth_hz <= 0.0
        ):
            raise ValueError("bandwidth_hz must be finite and positive")
        if self.episode_id is not None:
            object.__setattr__(
                self,
                "episode_id",
                _clean_text(self.episode_id, "episode_id", 256),
            )
        cleaned_limitations = tuple(
            _clean_text(item, "limitation", 1_024) for item in self.limitations
        )
        object.__setattr__(self, "limitations", cleaned_limitations)
        cleaned_tags = tuple(
            _clean_text(item, "tag", 64).lower() for item in self.tags
        )
        if len(set(cleaned_tags)) != len(cleaned_tags):
            raise ValueError("tags must be unique")
        object.__setattr__(self, "tags", cleaned_tags)
        source_ids = tuple(item.sensor_id for item in self.sources)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("event source sensor ids must be unique")
        if self.valid_until is not None:
            _require_aware(self.valid_until, "valid_until")
            if self.valid_until <= self.observed_at:
                raise ValueError("valid_until must follow observed_at")
        if self.direction is not None and not self.direction.is_fresh_at(
            self.received_at
        ):
            raise EventPolicyViolation(
                "stale or future external direction cannot enter an event"
            )
        if (
            self.event_type is NormalizedEventType.DIRECTION_ESTIMATED
            and self.direction is None
        ):
            raise EventPolicyViolation(
                "DIRECTION_ESTIMATED requires validated external direction"
            )
        self._validate_identity_claim()

    def _validate_identity_claim(self) -> None:
        if self.event_type not in _IDENTITY_LIKE_EVENTS:
            if self.identity is not None:
                raise EventPolicyViolation(
                    "identity evidence is not accepted on a generic event"
                )
            return
        if self.identity is None:
            raise EventPolicyViolation(
                f"{self.event_type.value} requires a validated classifier record"
            )
        classifier_sources = {
            item.sensor_id
            for item in self.sources
            if item.sensor_kind is SensorKind.CLASSIFIER
        }
        if self.identity.classifier_id not in classifier_sources:
            raise EventPolicyViolation(
                "identity classifier must be included in source attribution"
            )
        if self.event_type not in _HIGH_CONSEQUENCE_IDENTITY_EVENTS:
            return
        kinds = {item.sensor_kind for item in self.sources}
        if kinds and kinds <= _RF_ONLY_KINDS:
            raise EventPolicyViolation(
                "frequency-only or RSSI-only evidence cannot assert drone identity"
            )
        confirmations = set(
            self.identity.independent_confirmation_source_ids
        )
        attributed_confirmations = {
            item.sensor_id
            for item in self.sources
            if item.independent_confirmation
        }
        non_rf_confirmations = {
            item.sensor_id
            for item in self.sources
            if item.independent_confirmation
            and item.sensor_kind
            in {
                SensorKind.ACOUSTIC,
                SensorKind.PASSIVE_RADAR,
                SensorKind.CAMERA,
            }
        }
        minimum = (
            2
            if self.event_type is NormalizedEventType.TARGET_CONFIRMED
            else 1
        )
        if len(confirmations) < minimum:
            raise EventPolicyViolation(
                f"{self.event_type.value} needs at least {minimum} independent "
                "confirmation source(s)"
            )
        if not confirmations <= attributed_confirmations:
            raise EventPolicyViolation(
                "every independent confirmation must be explicitly attributed"
            )
        if not confirmations <= non_rf_confirmations:
            raise EventPolicyViolation(
                "frequency/RSSI, direction and classifier output are not "
                "independent physical confirmation"
            )
        if self.valid_until is None:
            raise EventPolicyViolation(
                "high-consequence identity events must expire unless refreshed"
            )

    @property
    def is_important(self) -> bool:
        return self.severity in {
            EventSeverity.WARNING,
            EventSeverity.ALARM,
            EventSeverity.CRITICAL,
        } or self.event_type in {
            NormalizedEventType.LIKELY_DRONE_SIGNATURE,
            NormalizedEventType.TARGET_CONFIRMED,
        }

    @property
    def deduplication_key(self) -> str:
        source_key = ",".join(sorted(item.sensor_id for item in self.sources))
        episode_key = self.episode_id or self.event_id
        return f"{self.event_type.value}|{episode_key}|{source_key}"

    def is_active_at(self, at: datetime) -> bool:
        _require_aware(at, "at")
        return self.observed_at <= at and (
            self.valid_until is None or at <= self.valid_until
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "observed_at": self.observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "valid_until": (
                self.valid_until.isoformat()
                if self.valid_until is not None
                else None
            ),
            "severity": self.severity.value,
            "confidence": self.confidence.to_dict(),
            "summary_ru": self.summary_ru,
            "explanation_ru": self.explanation_ru,
            "recommendation_ru": self.recommendation_ru,
            "sources": [item.to_dict() for item in self.sources],
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
            "frequency_hz": self.frequency_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "direction": (
                self.direction.to_dict() if self.direction is not None else None
            ),
            "episode_id": self.episode_id,
            "identity": (
                self.identity.to_dict() if self.identity is not None else None
            ),
            "tags": list(self.tags),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class OperatorSituation:
    generated_at: datetime
    mode: OperatorSituationMode
    headline_ru: str
    explanation_ru: str
    severity: EventSeverity
    confidence: ConfidenceScore
    direction_ru: str
    direction: DirectionEstimate | None
    recommendation_ru: str
    primary_event: NormalizedEvent | None
    recent_events: tuple[NormalizedEvent, ...]
    sensors: tuple[SensorState, ...]
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")
        for field_name in (
            "headline_ru",
            "explanation_ru",
            "direction_ru",
            "recommendation_ru",
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_text(str(getattr(self, field_name)), field_name, 2_048),
            )
        if self.direction is not None and not self.direction.is_fresh_at(
            self.generated_at
        ):
            raise EventPolicyViolation(
                "operator situation cannot display stale external direction"
            )
        if (
            self.mode is OperatorSituationMode.CONFIRMED_TARGET
            and (
                self.primary_event is None
                or self.primary_event.event_type
                is not NormalizedEventType.TARGET_CONFIRMED
            )
        ):
            raise EventPolicyViolation(
                "confirmed target mode requires TARGET_CONFIRMED event"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "mode": self.mode.value,
            "headline_ru": self.headline_ru,
            "explanation_ru": self.explanation_ru,
            "severity": self.severity.value,
            "confidence": self.confidence.to_dict(),
            "direction_ru": self.direction_ru,
            "direction": (
                self.direction.to_dict() if self.direction is not None else None
            ),
            "recommendation_ru": self.recommendation_ru,
            "primary_event": (
                self.primary_event.to_dict()
                if self.primary_event is not None
                else None
            ),
            "recent_events": [item.to_dict() for item in self.recent_events],
            "sensors": [item.to_dict() for item in self.sensors],
            "limitations": list(self.limitations),
        }

    @property
    def state(self) -> OperatorSituationMode:
        return self.mode

    @property
    def direction_text_ru(self) -> str:
        return self.direction_ru

    @property
    def direction_explanation_ru(self) -> str:
        return self.direction_ru

    @property
    def sensor_availability(self) -> tuple[SensorState, ...]:
        return self.sensors


__all__ = [
    "ConfidenceBand",
    "ConfidenceScore",
    "DirectionEstimate",
    "EventPolicyViolation",
    "EventSeverity",
    "EvidenceFact",
    "NormalizedEvent",
    "NormalizedEventType",
    "OperatorSituation",
    "OperatorSituationMode",
    "SensorAvailability",
    "SensorKind",
    "SensorState",
    "SourceAttribution",
    "ValidatedIdentityEvidence",
]
