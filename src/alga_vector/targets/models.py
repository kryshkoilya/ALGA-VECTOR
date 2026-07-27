"""Immutable contracts for the target-centric operator projection.

Targets in this package are *projections* over already policy-checked
``NormalizedEvent`` instances.  They do not weaken the event schema:

* a frequency or an RSSI-like strength can never establish object identity;
* numeric evidence strength is explicitly heuristic, not a probability;
* direction and zone data are accepted only when a validated external source
  supplied a fresh measurement;
* ``probable_type`` describes observable phenomenology, not intent,
  nationality, ownership, range, or an exact emitter model.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from alga_vector.signal_processor.schema import (
    ConfidenceScore,
    DirectionEstimate,
    EvidenceFact,
    SensorKind,
)


class TargetLifecycle(StrEnum):
    """Freshness lifecycle of one fused operator target."""

    ACTIVE = "active"
    HOLDING = "holding"
    STALE = "stale"
    TOMBSTONED = "tombstoned"


class ConfirmationStage(StrEnum):
    """Human-readable confirmation stage; never a calibrated probability."""

    BACKGROUND = "background"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    LIKELY_SOURCE = "likely_source"
    LIKELY_TARGET = "likely_target"
    CONFIRMED_TARGET = "confirmed_target"


class PhenomenologicalType(StrEnum):
    """General observable class used for ``FusedTarget.probable_type``."""

    UNKNOWN_ACTIVITY = "unknown_activity"
    RF_ACTIVITY = "rf_activity"
    HANDHELD_RADIO_LIKE = "handheld_radio_like"
    VIDEO_LINK_LIKE = "video_link_like"
    ACOUSTIC_ACTIVITY = "acoustic_activity"
    MULTISENSOR_ACTIVITY = "multisensor_activity"
    VALIDATED_UAS_LIKE = "validated_uas_like"


class TargetUpdateStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    CAPACITY_REJECTED = "capacity_rejected"


class SensorRole(StrEnum):
    TINYSA = "tinysa"
    RTL_SDR = "rtl_sdr"
    KRAKEN_SDR = "kraken_sdr"
    ACOUSTIC = "acoustic"
    ADSB = "adsb"
    PASSIVE_RADAR = "passive_radar"
    FUSION = "fusion"


class SensorReadinessLevel(StrEnum):
    READY = "ready"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


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


def _require_unit(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be finite and within 0..1")


@dataclass(frozen=True, slots=True)
class ValidatedZone:
    """A named zone supplied by a separately validated external integration."""

    zone_id: str
    label_ru: str
    source_id: str
    observed_at: datetime
    valid_until: datetime
    calibration_id: str
    confidence: float
    validated_external: bool

    def __post_init__(self) -> None:
        for name in ("zone_id", "label_ru", "source_id", "calibration_id"):
            object.__setattr__(
                self,
                name,
                _clean_text(str(getattr(self, name)), name, 256),
            )
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.observed_at:
            raise ValueError("zone valid_until must follow observed_at")
        _require_unit(self.confidence, "zone confidence")
        if not self.validated_external:
            raise ValueError("zone must come from a validated external source")

    def is_fresh_at(self, now: datetime) -> bool:
        _require_aware(now, "now")
        return self.observed_at <= now <= self.valid_until


@dataclass(frozen=True, slots=True)
class TargetSourceAttribution:
    """Current, bounded contribution of one sensor stream to a target."""

    sensor_id: str
    sensor_kind: SensorKind
    contribution: float
    independent_confirmation: bool
    first_seen: datetime
    last_seen: datetime
    observation_count: int
    latest_event_id: str
    explanation_ru: str
    provenance: str = "live"

    def __post_init__(self) -> None:
        for name, maximum in (
            ("sensor_id", 128),
            ("latest_event_id", 256),
            ("explanation_ru", 1_024),
            ("provenance", 64),
        ):
            object.__setattr__(
                self,
                name,
                _clean_text(str(getattr(self, name)), name, maximum),
            )
        _require_unit(self.contribution, "source contribution")
        _require_aware(self.first_seen, "first_seen")
        _require_aware(self.last_seen, "last_seen")
        if self.last_seen < self.first_seen:
            raise ValueError("source last_seen cannot precede first_seen")
        if self.observation_count < 1:
            raise ValueError("observation_count must be positive")


@dataclass(frozen=True, slots=True)
class TargetRecommendation:
    code: str
    short_ru: str
    detailed_ru: str

    def __post_init__(self) -> None:
        for name, maximum in (
            ("code", 128),
            ("short_ru", 512),
            ("detailed_ru", 2_048),
        ):
            object.__setattr__(
                self,
                name,
                _clean_text(str(getattr(self, name)), name, maximum),
            )


@dataclass(frozen=True, slots=True)
class FusedTarget:
    """One immutable target card produced from normalized, traceable events."""

    target_id: str
    lifecycle: TargetLifecycle
    confirmation_stage: ConfirmationStage
    probable_type: PhenomenologicalType
    technical_label: str
    operator_label: str
    operator_explanation: str
    created_at: datetime
    updated_at: datetime
    last_seen: datetime
    sensors_used: tuple[SensorKind, ...]
    source_attribution: tuple[TargetSourceAttribution, ...]
    direction: DirectionEstimate | None
    zone: ValidatedZone | None
    recommendation: TargetRecommendation
    evidence_strength: ConfidenceScore
    evidence: tuple[EvidenceFact, ...]
    limitations: tuple[str, ...] = field(default_factory=tuple)
    recent_event_ids: tuple[str, ...] = field(default_factory=tuple)
    merged_from_target_ids: tuple[str, ...] = field(default_factory=tuple)
    tombstoned_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("target_id", 128),
            ("technical_label", 256),
            ("operator_label", 512),
            ("operator_explanation", 2_048),
        ):
            object.__setattr__(
                self,
                name,
                _clean_text(str(getattr(self, name)), name, maximum),
            )
        for name in ("created_at", "updated_at", "last_seen"):
            _require_aware(getattr(self, name), name)
        if not self.created_at <= self.last_seen <= self.updated_at:
            raise ValueError("target timestamps must satisfy created <= last_seen <= updated")
        if self.confirmation_stage is ConfirmationStage.BACKGROUND:
            raise ValueError("background is a situation stage, not a target")
        if len(set(self.sensors_used)) != len(self.sensors_used):
            raise ValueError("sensors_used must be unique")
        source_ids = tuple(item.sensor_id for item in self.source_attribution)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source attribution sensor ids must be unique")
        if tuple(dict.fromkeys(self.recent_event_ids)) != self.recent_event_ids:
            raise ValueError("recent_event_ids must be unique and ordered")
        if tuple(dict.fromkeys(self.merged_from_target_ids)) != self.merged_from_target_ids:
            raise ValueError("merged target ids must be unique and ordered")
        if self.target_id in self.merged_from_target_ids:
            raise ValueError("target cannot be merged from itself")
        cleaned_limitations = tuple(
            _clean_text(item, "limitation", 1_024) for item in self.limitations
        )
        object.__setattr__(self, "limitations", cleaned_limitations)
        if self.direction is not None:
            if not self.direction.is_fresh_at(self.updated_at):
                raise ValueError("target cannot expose stale or future direction")
            if not any(
                item.sensor_kind is SensorKind.DIRECTION_FINDER
                and item.sensor_id == self.direction.source_id
                for item in self.source_attribution
            ):
                raise ValueError(
                    "target direction requires matching direction-finder attribution"
                )
        if self.zone is not None and not self.zone.is_fresh_at(self.updated_at):
            raise ValueError("target cannot expose stale or future zone")
        if self.lifecycle is TargetLifecycle.TOMBSTONED:
            if self.tombstoned_at is None:
                raise ValueError("tombstoned target requires tombstoned_at")
            _require_aware(self.tombstoned_at, "tombstoned_at")
        elif self.tombstoned_at is not None:
            raise ValueError("only a tombstoned target may carry tombstoned_at")

    @property
    def recommended_action_short(self) -> str:
        return self.recommendation.short_ru

    @property
    def recommended_action_detailed(self) -> str:
        return self.recommendation.detailed_ru

    @property
    def sector_text_ru(self) -> str:
        if self.direction is None:
            return "Направление не определено"
        low = (self.direction.bearing_deg - self.direction.uncertainty_deg) % 360.0
        high = (self.direction.bearing_deg + self.direction.uncertainty_deg) % 360.0
        return (
            f"Сектор {low:.0f}–{high:.0f}° · "
            f"азимут {self.direction.bearing_deg:.0f}°"
        )

    @property
    def active(self) -> bool:
        return self.lifecycle is TargetLifecycle.ACTIVE


@dataclass(frozen=True, slots=True)
class TargetUpdate:
    status: TargetUpdateStatus
    evaluated_at: datetime
    target: FusedTarget | None
    reason_code: str
    merged_target_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        object.__setattr__(
            self,
            "reason_code",
            _clean_text(self.reason_code, "reason_code", 128),
        )
        if self.status in {
            TargetUpdateStatus.CREATED,
            TargetUpdateStatus.UPDATED,
        } and self.target is None:
            raise ValueError("created/updated result requires a target")
        if tuple(dict.fromkeys(self.merged_target_ids)) != self.merged_target_ids:
            raise ValueError("merged_target_ids must be unique and ordered")


@dataclass(frozen=True, slots=True)
class SensorReadiness:
    role: SensorRole
    display_name: str
    level: SensorReadinessLevel
    reason_code: str
    reason_ru: str
    impact_ru: str
    checked_at: datetime
    sensor_ids: tuple[str, ...] = ()
    last_data_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, maximum in (
            ("display_name", 128),
            ("reason_code", 128),
            ("reason_ru", 1_024),
            ("impact_ru", 1_024),
        ):
            object.__setattr__(
                self,
                name,
                _clean_text(str(getattr(self, name)), name, maximum),
            )
        _require_aware(self.checked_at, "checked_at")
        if self.last_data_at is not None:
            _require_aware(self.last_data_at, "last_data_at")
            if self.last_data_at > self.checked_at:
                raise ValueError("last_data_at cannot be in the future")
        cleaned_ids = tuple(_clean_text(item, "sensor_id", 128) for item in self.sensor_ids)
        if len(set(cleaned_ids)) != len(cleaned_ids):
            raise ValueError("sensor_ids must be unique")
        object.__setattr__(self, "sensor_ids", cleaned_ids)

    @property
    def available(self) -> bool:
        return self.level is not SensorReadinessLevel.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class SensorReadinessSnapshot:
    generated_at: datetime
    sensors: tuple[SensorReadiness, ...]

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")
        roles = tuple(item.role for item in self.sensors)
        if len(set(roles)) != len(roles):
            raise ValueError("sensor readiness roles must be unique")
        if set(roles) != set(SensorRole):
            raise ValueError("readiness snapshot must contain every canonical sensor role")

    def by_role(self, role: SensorRole) -> SensorReadiness:
        return next(item for item in self.sensors if item.role is role)


__all__ = [
    "ConfirmationStage",
    "FusedTarget",
    "PhenomenologicalType",
    "SensorReadiness",
    "SensorReadinessLevel",
    "SensorReadinessSnapshot",
    "SensorRole",
    "TargetLifecycle",
    "TargetRecommendation",
    "TargetSourceAttribution",
    "TargetUpdate",
    "TargetUpdateStatus",
    "ValidatedZone",
]
