"""Safe models for public civil ADS-B/Mode-S broadcast context.

The models intentionally do not contain position, nationality, military
status, friend-or-foe, threat, or cross-sensor identity fields.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

CIVIL_BROADCAST_LIMITATIONS: tuple[str, ...] = (
    "Only locally received public civil ADS-B/Mode-S broadcast facts are represented.",
    "Broadcast presence is context only; it does not correlate or identify another sensor event.",
    "No received broadcast does not prove that the airspace is empty.",
    "No nationality, military status, friend-or-foe, hostile, or threat inference is performed.",
)


class AirspaceFeedState(StrEnum):
    """Fail-closed state of the local civil broadcast feed."""

    NO_DATA = "no_data"
    CURRENT = "current"
    STALE = "stale"
    INVALID = "invalid"
    IO_ERROR = "io_error"


class AirspaceDataQuality(StrEnum):
    """Data quality without turning reception into identity confidence."""

    UNAVAILABLE = "unavailable"
    LIMITED = "limited"
    PARTIAL = "partial"
    GOOD = "good"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_finite_range(
    value: float,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be finite and within {minimum}..{maximum}")


@dataclass(frozen=True, slots=True)
class AirspaceParseIssue:
    """Sanitized parser diagnostic; raw aircraft data is never copied here."""

    code: str
    message: str
    record_index: int | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("code must not be empty")
        if not self.message.strip():
            raise ValueError("message must not be empty")
        if self.record_index is not None and self.record_index < 0:
            raise ValueError("record_index must be non-negative")
        if self.field is not None and not self.field.strip():
            raise ValueError("field must not be empty")


@dataclass(frozen=True, slots=True)
class CivilAircraftContext:
    """A bounded set of facts from one public broadcast record."""

    hex: str
    pseudonymous_id: str
    callsign: str | None
    altitude_ft: float | None
    on_ground: bool | None
    ground_speed_kt: float | None
    track_deg: float | None
    seen_s: float
    observed_at: datetime
    data_quality: AirspaceDataQuality

    def __post_init__(self) -> None:
        if re.fullmatch(r"~?[0-9a-f]{6}", self.hex) is None:
            raise ValueError("hex must be a normalized six-digit broadcast address")
        if re.fullmatch(r"ac-[0-9a-f]{12}", self.pseudonymous_id) is None:
            raise ValueError("pseudonymous_id has an invalid format")
        if self.callsign is not None:
            if not self.callsign or len(self.callsign) > 16 or not self.callsign.isascii():
                raise ValueError("callsign must be 1..16 ASCII characters")
            if any(not (character.isalnum() or character in " ._-") for character in self.callsign):
                raise ValueError("callsign contains unsupported characters")
        if self.altitude_ft is not None:
            _require_finite_range(
                self.altitude_ft,
                "altitude_ft",
                minimum=-2_000.0,
                maximum=100_000.0,
            )
        if self.ground_speed_kt is not None:
            _require_finite_range(
                self.ground_speed_kt,
                "ground_speed_kt",
                minimum=0.0,
                maximum=2_000.0,
            )
        if self.track_deg is not None:
            _require_finite_range(
                self.track_deg,
                "track_deg",
                minimum=0.0,
                maximum=360.0,
            )
            if self.track_deg == 360.0:
                raise ValueError("track_deg must be lower than 360")
        _require_finite_range(
            self.seen_s,
            "seen_s",
            minimum=0.0,
            maximum=604_800.0,
        )
        _require_aware(self.observed_at, "observed_at")
        if self.on_ground is True and self.altitude_ft is not None:
            raise ValueError("ground record must not claim a barometric altitude")
        if self.data_quality is AirspaceDataQuality.UNAVAILABLE:
            raise ValueError("an accepted record cannot have unavailable quality")

    @property
    def altitude(self) -> float | None:
        """Compatibility label for the measured altitude fact."""

        return self.altitude_ft

    @property
    def ground_speed(self) -> float | None:
        return self.ground_speed_kt

    @property
    def track(self) -> float | None:
        return self.track_deg

    @property
    def age_at_payload_s(self) -> float:
        return self.seen_s

    def age_s(self, at: datetime) -> float:
        """Return current age while retaining the original ``seen`` fact."""

        _require_aware(at, "at")
        return max(0.0, (at - self.observed_at).total_seconds())


@dataclass(frozen=True, slots=True)
class ParsedCivilAirspacePayload:
    """Validated root payload with malformed records isolated as issues."""

    generated_at: datetime
    received_at: datetime
    timestamp_from_payload: bool
    aircraft: tuple[CivilAircraftContext, ...]
    issues: tuple[AirspaceParseIssue, ...]
    total_record_count: int

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")
        _require_aware(self.received_at, "received_at")
        if self.total_record_count < len(self.aircraft):
            raise ValueError("total_record_count cannot be lower than accepted records")

    @property
    def rejected_record_count(self) -> int:
        return self.total_record_count - len(self.aircraft)


@dataclass(frozen=True, slots=True)
class CivilAirspaceSummary:
    """Small, fusion-safe summary of civil broadcast context."""

    active_count: int
    state: AirspaceFeedState
    nearby_context_available: bool
    stale: bool
    data_quality: AirspaceDataQuality
    evaluated_at: datetime
    source_generated_at: datetime | None
    source_age_s: float | None
    valid_record_count: int
    rejected_record_count: int
    issue_count: int
    limitations: tuple[str, ...] = CIVIL_BROADCAST_LIMITATIONS

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        if self.source_generated_at is not None:
            _require_aware(self.source_generated_at, "source_generated_at")
        for field_name in (
            "active_count",
            "valid_record_count",
            "rejected_record_count",
            "issue_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.active_count > self.valid_record_count:
            raise ValueError("active_count cannot exceed valid_record_count")
        if self.source_age_s is not None:
            _require_finite_range(
                self.source_age_s,
                "source_age_s",
                minimum=0.0,
                maximum=315_576_000.0,
            )
        expected_context = (
            self.state is AirspaceFeedState.CURRENT and not self.stale and self.active_count > 0
        )
        if self.nearby_context_available is not expected_context:
            raise ValueError(
                "nearby_context_available must mean current broadcast context only"
            )
        if self.stale and self.state is AirspaceFeedState.CURRENT:
            raise ValueError("current state cannot be stale")
        if self.state is not AirspaceFeedState.CURRENT and not self.stale:
            raise ValueError("non-current state must fail closed as stale")
        if self.state is not AirspaceFeedState.CURRENT:
            if self.data_quality is not AirspaceDataQuality.UNAVAILABLE:
                raise ValueError("non-current state must use unavailable quality")
            if self.active_count != 0:
                raise ValueError("non-current state cannot expose active records")
        if not self.limitations:
            raise ValueError("limitations must not be empty")

    @property
    def context_scope(self) -> Literal["public_civil_broadcast_only"]:
        return "public_civil_broadcast_only"

    @property
    def context_only(self) -> Literal[True]:
        return True

    @property
    def supports_identity_correlation(self) -> Literal[False]:
        return False

    @property
    def supports_friend_or_foe(self) -> Literal[False]:
        return False

    @property
    def supports_threat_inference(self) -> Literal[False]:
        return False


@dataclass(frozen=True, slots=True)
class CivilAirspaceSnapshot:
    """Current active records and their fail-closed summary."""

    summary: CivilAirspaceSummary
    aircraft: tuple[CivilAircraftContext, ...]
    issues: tuple[AirspaceParseIssue, ...]

    def __post_init__(self) -> None:
        if len(self.aircraft) != self.summary.active_count:
            raise ValueError("aircraft must contain exactly the active records")
        if len(self.issues) != self.summary.issue_count:
            raise ValueError("issues must match issue_count")


AircraftContext = CivilAircraftContext
AirspaceContextSummary = CivilAirspaceSummary


__all__ = [
    "CIVIL_BROADCAST_LIMITATIONS",
    "AircraftContext",
    "AirspaceContextSummary",
    "AirspaceDataQuality",
    "AirspaceFeedState",
    "AirspaceParseIssue",
    "CivilAircraftContext",
    "CivilAirspaceSnapshot",
    "CivilAirspaceSummary",
    "ParsedCivilAirspacePayload",
]
