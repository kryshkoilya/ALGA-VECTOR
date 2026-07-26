"""Honest direction observations with explicit provenance and validity.

This package deliberately models only an angle.  It has no position, range,
coordinate, or RF-strength-to-bearing inference.
"""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DirectionSource(StrEnum):
    """Provenance of a direction observation."""

    UNAVAILABLE = "unavailable"
    MANUAL = "manual"
    EXTERNAL = "external"
    SIMULATED = "simulated"


class DirectionQuality(StrEnum):
    """Operator-facing quality without pretending every source is measured."""

    UNAVAILABLE = "unavailable"
    UNMEASURED = "unmeasured"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    SIMULATED = "simulated"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_probability(value: float, field_name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be finite and within 0..1")


@dataclass(frozen=True, slots=True)
class ExternalDirectionEvidence:
    """Validation evidence supplied by a real external direction sensor."""

    calibration_id: str
    calibrated_at: datetime
    evidence_at: datetime
    sample_count: int
    quality_score: float
    calibration_valid: bool

    def __post_init__(self) -> None:
        if not self.calibration_id.strip():
            raise ValueError("calibration_id must not be empty")
        _require_aware(self.calibrated_at, "calibrated_at")
        _require_aware(self.evidence_at, "evidence_at")
        if self.sample_count < 1:
            raise ValueError("sample_count must be positive")
        _require_probability(self.quality_score, "quality_score")


@dataclass(frozen=True, slots=True)
class DirectionObservation:
    """A single angular observation.

    ``uncertainty_deg`` is the half-width of the displayed cone.  Manual input
    is explicitly unmeasured and therefore has no confidence score.
    """

    source: DirectionSource
    bearing_deg: float | None
    uncertainty_deg: float | None
    confidence: float | None
    quality: DirectionQuality
    captured_at: datetime | None
    source_id: str
    reason_code: str
    message_ru: str
    evidence: ExternalDirectionEvidence | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("reason_code must not be empty")
        if not self.message_ru.strip():
            raise ValueError("message_ru must not be empty")
        if self.source is DirectionSource.UNAVAILABLE:
            if any(
                value is not None
                for value in (
                    self.bearing_deg,
                    self.uncertainty_deg,
                    self.confidence,
                    self.captured_at,
                    self.evidence,
                )
            ):
                raise ValueError("unavailable observation must not carry measurements")
            if self.quality is not DirectionQuality.UNAVAILABLE:
                raise ValueError("unavailable observation must use unavailable quality")
            return

        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")
        if self.bearing_deg is None or not math.isfinite(self.bearing_deg):
            raise ValueError("bearing_deg must be finite")
        if not 0.0 <= self.bearing_deg < 360.0:
            raise ValueError("bearing_deg must be within 0 <= value < 360")
        if self.uncertainty_deg is None or not math.isfinite(self.uncertainty_deg):
            raise ValueError("uncertainty_deg must be finite")
        if not 0.0 <= self.uncertainty_deg <= 180.0:
            raise ValueError("uncertainty_deg must be within 0..180")
        if self.captured_at is None:
            raise ValueError("captured_at is required")
        _require_aware(self.captured_at, "captured_at")

        if self.source is DirectionSource.MANUAL:
            if self.confidence is not None:
                raise ValueError("manual input must not claim measured confidence")
            if self.quality is not DirectionQuality.UNMEASURED:
                raise ValueError("manual input must use unmeasured quality")
            if self.evidence is not None:
                raise ValueError("manual input must not carry sensor evidence")
        elif self.source is DirectionSource.EXTERNAL:
            if self.confidence is None:
                raise ValueError("external observation requires confidence")
            _require_probability(self.confidence, "confidence")
            if self.quality not in {
                DirectionQuality.LOW,
                DirectionQuality.MEDIUM,
                DirectionQuality.HIGH,
            }:
                raise ValueError("external observation requires measured quality")
            if self.evidence is None:
                raise ValueError("external observation requires validation evidence")
        elif self.source is DirectionSource.SIMULATED:
            if self.confidence is None:
                raise ValueError("simulated observation requires a display confidence")
            _require_probability(self.confidence, "confidence")
            if self.quality is not DirectionQuality.SIMULATED:
                raise ValueError("simulated observation must use simulated quality")
            if self.evidence is not None:
                raise ValueError("simulated observation must not carry sensor evidence")

    @property
    def available(self) -> bool:
        return self.source is not DirectionSource.UNAVAILABLE

    @property
    def measured(self) -> bool:
        return self.source is DirectionSource.EXTERNAL

    @property
    def operator_entered(self) -> bool:
        return self.source is DirectionSource.MANUAL

    def is_stale(self, now: datetime, maximum_age_s: float) -> bool:
        """Return whether this observation is older than the supplied policy."""

        _require_aware(now, "now")
        if self.captured_at is None:
            return False
        return (now - self.captured_at).total_seconds() > maximum_age_s

    @classmethod
    def unavailable(
        cls,
        message_ru: str = "Валидный источник направления не подключён.",
        *,
        reason_code: str = "DIRECTION.UNAVAILABLE",
        source_id: str = "none",
    ) -> DirectionObservation:
        return cls(
            source=DirectionSource.UNAVAILABLE,
            bearing_deg=None,
            uncertainty_deg=None,
            confidence=None,
            quality=DirectionQuality.UNAVAILABLE,
            captured_at=None,
            source_id=source_id,
            reason_code=reason_code,
            message_ru=message_ru,
        )


@dataclass(frozen=True, slots=True)
class DirectionTrailPoint:
    """A bounded historical point used only for the angular trail."""

    bearing_deg: float
    uncertainty_deg: float
    confidence: float | None
    captured_at: datetime
    source: DirectionSource
    source_id: str

    @classmethod
    def from_observation(
        cls,
        observation: DirectionObservation,
    ) -> DirectionTrailPoint:
        if (
            not observation.available
            or observation.bearing_deg is None
            or observation.uncertainty_deg is None
            or observation.captured_at is None
        ):
            raise ValueError("only available observations can enter the trail")
        return cls(
            bearing_deg=observation.bearing_deg,
            uncertainty_deg=observation.uncertainty_deg,
            confidence=observation.confidence,
            captured_at=observation.captured_at,
            source=observation.source,
            source_id=observation.source_id,
        )


@dataclass(frozen=True, slots=True)
class DirectionSnapshot:
    """Current fail-closed state plus a bounded angular history."""

    current: DirectionObservation
    trail: tuple[DirectionTrailPoint, ...]
    evaluated_at: datetime
    stale: bool
    age_s: float | None
    last_valid_at: datetime | None

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        if self.age_s is not None and (not math.isfinite(self.age_s) or self.age_s < 0.0):
            raise ValueError("age_s must be finite and non-negative")
        if self.last_valid_at is not None:
            _require_aware(self.last_valid_at, "last_valid_at")
        if self.current.available and self.stale:
            raise ValueError("stale direction must fail closed as unavailable")

    @property
    def available(self) -> bool:
        return self.current.available and not self.stale


SOURCE_LABELS_RU: dict[DirectionSource, str] = {
    DirectionSource.UNAVAILABLE: "НЕТ ИСТОЧНИКА",
    DirectionSource.MANUAL: "РУЧНОЙ ВВОД ОПЕРАТОРА",
    DirectionSource.EXTERNAL: "ВНЕШНИЙ ПЕЛЕНГАТОР",
    DirectionSource.SIMULATED: "ДЕМО · СИМУЛЯЦИЯ",
}

QUALITY_LABELS_RU: dict[DirectionQuality, str] = {
    DirectionQuality.UNAVAILABLE: "НЕДОСТУПНО",
    DirectionQuality.UNMEASURED: "НЕ ИЗМЕРЯЛАСЬ",
    DirectionQuality.LOW: "НИЗКОЕ",
    DirectionQuality.MEDIUM: "СРЕДНЕЕ",
    DirectionQuality.HIGH: "ВЫСОКОЕ",
    DirectionQuality.SIMULATED: "СИМУЛЯЦИЯ",
}


__all__ = [
    "QUALITY_LABELS_RU",
    "SOURCE_LABELS_RU",
    "DirectionObservation",
    "DirectionQuality",
    "DirectionSnapshot",
    "DirectionSource",
    "DirectionTrailPoint",
    "ExternalDirectionEvidence",
]
