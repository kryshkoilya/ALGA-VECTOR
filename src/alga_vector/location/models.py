from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def _finite_number(name: str, value: float | int) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f"{name} must be a real number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _aware_utc(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


class LocationSource(StrEnum):
    MANUAL = "manual"
    NMEA_GGA = "nmea_gga"
    NMEA_RMC = "nmea_rmc"


class LocationStatus(StrEnum):
    UNSET = "unset"
    COLLECTING = "collecting"
    MANUAL_UNVERIFIED = "manual_unverified"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    STALE = "stale"
    JUMP_SUSPECTED = "jump_suspected"


class GpsFixDimension(StrEnum):
    UNKNOWN = "unknown"
    NONE = "none"
    TWO_D = "2d"
    THREE_D = "3d"


class GpsFixState(StrEnum):
    DISCONNECTED = "disconnected"
    SEARCHING = "searching"
    NO_FIX = "no_fix"
    FIX = "fix"
    FIX_2D = "fix_2d"
    FIX_3D = "fix_3d"
    STALE = "stale"
    JUMP_SUSPECTED = "jump_suspected"


@dataclass(slots=True, frozen=True)
class GeoPoint:
    """A validated WGS84 point whose exact coordinates never appear in repr."""

    latitude_deg: float = field(repr=False)
    longitude_deg: float = field(repr=False)
    altitude_m: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        latitude = _finite_number("latitude_deg", self.latitude_deg)
        longitude = _finite_number("longitude_deg", self.longitude_deg)
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude_deg must be between -90 and 90")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("longitude_deg must be between -180 and 180")
        altitude = self.altitude_m
        if altitude is not None:
            altitude = _finite_number("altitude_m", altitude)
        object.__setattr__(self, "latitude_deg", latitude)
        object.__setattr__(self, "longitude_deg", longitude)
        object.__setattr__(self, "altitude_m", altitude)

    def __repr__(self) -> str:
        return "GeoPoint(<redacted>)"


@dataclass(slots=True, frozen=True)
class LocationFix:
    point: GeoPoint = field(repr=False)
    captured_at: datetime
    source: LocationSource
    horizontal_accuracy_m: float | None = None
    accuracy_is_estimate: bool = False
    hdop: float | None = None
    satellites: int | None = None
    speed_m_s: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.point, GeoPoint):
            raise TypeError("point must be a GeoPoint")
        object.__setattr__(self, "captured_at", _aware_utc("captured_at", self.captured_at))
        if not isinstance(self.source, LocationSource):
            raise TypeError("source must be a LocationSource")
        for name in ("horizontal_accuracy_m", "hdop", "speed_m_s"):
            value = getattr(self, name)
            if value is None:
                continue
            converted = _finite_number(name, value)
            if converted < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, converted)
        if self.satellites is not None:
            if isinstance(self.satellites, bool) or not isinstance(self.satellites, int):
                raise TypeError("satellites must be an integer")
            if self.satellites < 0:
                raise ValueError("satellites cannot be negative")


@dataclass(slots=True, frozen=True)
class LocationSnapshot:
    status: LocationStatus
    base: GeoPoint | None = field(default=None, repr=False)
    source: LocationSource | None = None
    captured_at: datetime | None = None
    horizontal_accuracy_m: float | None = None
    accuracy_is_estimate: bool = False
    sample_count: int = 0
    gps_fix_state: GpsFixState = GpsFixState.DISCONNECTED
    fix_dimension: GpsFixDimension = GpsFixDimension.UNKNOWN
    last_receiver_at: datetime | None = None
    message_ru: str = ""
    warnings_ru: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, LocationStatus):
            raise TypeError("status must be a LocationStatus")
        if self.base is not None and not isinstance(self.base, GeoPoint):
            raise TypeError("base must be a GeoPoint or None")
        if self.source is not None and not isinstance(self.source, LocationSource):
            raise TypeError("source must be a LocationSource or None")
        if self.captured_at is not None:
            object.__setattr__(
                self,
                "captured_at",
                _aware_utc("captured_at", self.captured_at),
            )
        if not isinstance(self.gps_fix_state, GpsFixState):
            raise TypeError("gps_fix_state must be a GpsFixState")
        if not isinstance(self.fix_dimension, GpsFixDimension):
            raise TypeError("fix_dimension must be a GpsFixDimension")
        if self.last_receiver_at is not None:
            object.__setattr__(
                self,
                "last_receiver_at",
                _aware_utc("last_receiver_at", self.last_receiver_at),
            )
        if self.horizontal_accuracy_m is not None:
            accuracy = _finite_number(
                "horizontal_accuracy_m",
                self.horizontal_accuracy_m,
            )
            if accuracy < 0:
                raise ValueError("horizontal_accuracy_m cannot be negative")
            object.__setattr__(self, "horizontal_accuracy_m", accuracy)
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
            raise TypeError("sample_count must be an integer")
        if self.sample_count < 0:
            raise ValueError("sample_count cannot be negative")
        if self.status is LocationStatus.VERIFIED and self.base is None:
            raise ValueError("verified location requires a base point")

    @property
    def absolute_position_allowed(self) -> bool:
        return self.status is LocationStatus.VERIFIED and self.base is not None

    def __repr__(self) -> str:
        base_state = "present" if self.base is not None else "none"
        return (
            "LocationSnapshot("
            f"status={self.status.value!r}, base={base_state!r}, "
            f"source={self.source.value if self.source else None!r}, "
            f"sample_count={self.sample_count}, "
            f"gps_fix_state={self.gps_fix_state.value!r})"
        )


@dataclass(slots=True, frozen=True)
class LocationPolicy:
    minimum_samples: int = 5
    sample_window_s: float = 60.0
    maximum_fix_age_s: float = 10.0
    maximum_future_skew_s: float = 5.0
    maximum_hdop: float = 3.0
    minimum_satellites: int = 4
    maximum_stationary_speed_m_s: float = 0.75
    maximum_stationary_radius_m: float = 25.0
    manual_conflict_distance_m: float = 100.0
    maximum_jump_distance_m: float = 250.0
    maximum_jump_speed_m_s: float = 60.0

    def __post_init__(self) -> None:
        if isinstance(self.minimum_samples, bool) or not isinstance(self.minimum_samples, int):
            raise TypeError("minimum_samples must be an integer")
        if self.minimum_samples < 1:
            raise ValueError("minimum_samples must be at least 1")
        if isinstance(self.minimum_satellites, bool) or not isinstance(
            self.minimum_satellites,
            int,
        ):
            raise TypeError("minimum_satellites must be an integer")
        if self.minimum_satellites < 0:
            raise ValueError("minimum_satellites cannot be negative")
        for name in (
            "sample_window_s",
            "maximum_fix_age_s",
            "maximum_future_skew_s",
            "maximum_hdop",
            "maximum_stationary_speed_m_s",
            "maximum_stationary_radius_m",
            "manual_conflict_distance_m",
            "maximum_jump_distance_m",
            "maximum_jump_speed_m_s",
        ):
            converted = _finite_number(name, getattr(self, name))
            if converted <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, converted)


__all__ = [
    "GeoPoint",
    "GpsFixDimension",
    "GpsFixState",
    "LocationFix",
    "LocationPolicy",
    "LocationSnapshot",
    "LocationSource",
    "LocationStatus",
]
