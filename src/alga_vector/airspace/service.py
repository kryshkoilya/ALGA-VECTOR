"""Thread-safe civil broadcast context service with explicit TTL policy."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from .models import (
    CIVIL_BROADCAST_LIMITATIONS,
    AirspaceDataQuality,
    AirspaceFeedState,
    AirspaceParseIssue,
    CivilAircraftContext,
    CivilAirspaceSnapshot,
    CivilAirspaceSummary,
    ParsedCivilAirspacePayload,
)
from .parser import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_RECORDS,
    AirspacePayloadError,
    load_dump1090_aircraft_file,
    parse_dump1090_aircraft_json,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CivilAirspacePolicy:
    """Freshness and resource limits for one local JSON feed."""

    aircraft_ttl_s: float = 15.0
    feed_ttl_s: float = 15.0
    future_tolerance_s: float = 5.0
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    max_records: int = DEFAULT_MAX_RECORDS

    def __post_init__(self) -> None:
        for field_name in ("aircraft_ttl_s", "feed_ttl_s"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{field_name} must be finite and positive")
        if not math.isfinite(self.future_tolerance_s) or self.future_tolerance_s < 0.0:
            raise ValueError("future_tolerance_s must be finite and non-negative")
        if self.max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        if self.max_records < 1:
            raise ValueError("max_records must be positive")


class DeterministicCivilAirspaceSource:
    """Explicitly fake, deterministic local source for tests and demos."""

    source_id = "fake-civil-airspace"
    simulated = True

    def read_payload(self, generated_at: datetime) -> bytes:
        _require_aware(generated_at, "generated_at")
        payload: dict[str, object] = {
            "now": generated_at.timestamp(),
            "aircraft": [
                {
                    "hex": "~f00001",
                    "flight": "TEST01",
                    "alt_baro": 12_000,
                    "gs": 180.0,
                    "track": 45.0,
                    "seen": 0.5,
                },
                {
                    "hex": "~f00002",
                    "flight": "TEST02",
                    "alt_baro": "ground",
                    "gs": 0.0,
                    "track": 0.0,
                    "seen": 1.0,
                },
            ],
            "simulated": True,
            "source_id": self.source_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class CivilAirspaceService:
    """Hold only fresh public broadcast context and fail closed on source errors."""

    def __init__(
        self,
        policy: CivilAirspacePolicy | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._policy = policy or CivilAirspacePolicy()
        self._clock = clock
        self._lock = RLock()
        self._parsed: ParsedCivilAirspacePayload | None = None
        self._snapshot = self._empty_snapshot(self._now())

    @property
    def policy(self) -> CivilAirspacePolicy:
        return self._policy

    def ingest_payload(
        self,
        payload: str | bytes | bytearray | Mapping[str, object],
    ) -> CivilAirspaceSnapshot:
        """Ingest a local payload and isolate root errors as an unavailable state."""

        with self._lock:
            received_at = self._now()
            try:
                parsed = parse_dump1090_aircraft_json(
                    payload,
                    received_at=received_at,
                    max_payload_bytes=self._policy.max_payload_bytes,
                    max_records=self._policy.max_records,
                )
            except (AirspacePayloadError, TypeError, ValueError) as exc:
                return self._fail_from_exception(exc, received_at)
            return self._accept(parsed, received_at)

    def ingest_file(self, path: str | os.PathLike[str]) -> CivilAirspaceSnapshot:
        """Ingest a bounded local file without performing any network request."""

        with self._lock:
            received_at = self._now()
            try:
                parsed = load_dump1090_aircraft_file(
                    path,
                    received_at=received_at,
                    max_payload_bytes=self._policy.max_payload_bytes,
                    max_records=self._policy.max_records,
                )
            except (AirspacePayloadError, TypeError, ValueError) as exc:
                return self._fail_from_exception(exc, received_at)
            return self._accept(parsed, received_at)

    def snapshot(self, *, now: datetime | None = None) -> CivilAirspaceSnapshot:
        """Re-evaluate feed and aircraft TTL without trusting cached active state."""

        with self._lock:
            evaluated_at = now or self._now()
            _require_aware(evaluated_at, "now")
            if self._parsed is None:
                return self._snapshot
            self._snapshot = self._build_snapshot(self._parsed, evaluated_at)
            return self._snapshot

    current_snapshot = snapshot

    def summary(self, *, now: datetime | None = None) -> CivilAirspaceSummary:
        return self.snapshot(now=now).summary

    def _accept(
        self,
        parsed: ParsedCivilAirspacePayload,
        evaluated_at: datetime,
    ) -> CivilAirspaceSnapshot:
        future_s = (parsed.generated_at - evaluated_at).total_seconds()
        if future_s > self._policy.future_tolerance_s:
            self._parsed = None
            self._snapshot = self._failure_snapshot(
                AirspaceFeedState.INVALID,
                evaluated_at,
                AirspaceParseIssue(
                    code="AIRSPACE.SOURCE_TIME_IN_FUTURE",
                    message="Source timestamp is too far in the future.",
                    field="now",
                ),
            )
            return self._snapshot
        self._parsed = parsed
        self._snapshot = self._build_snapshot(parsed, evaluated_at)
        return self._snapshot

    def _build_snapshot(
        self,
        parsed: ParsedCivilAirspacePayload,
        evaluated_at: datetime,
    ) -> CivilAirspaceSnapshot:
        _require_aware(evaluated_at, "evaluated_at")
        source_delta_s = (evaluated_at - parsed.generated_at).total_seconds()
        if source_delta_s < -self._policy.future_tolerance_s:
            return self._failure_snapshot(
                AirspaceFeedState.INVALID,
                evaluated_at,
                AirspaceParseIssue(
                    code="AIRSPACE.SOURCE_TIME_IN_FUTURE",
                    message="Source timestamp is too far in the future.",
                    field="now",
                ),
            )
        source_age_s = max(0.0, source_delta_s)
        if source_age_s > self._policy.feed_ttl_s:
            summary = CivilAirspaceSummary(
                active_count=0,
                state=AirspaceFeedState.STALE,
                nearby_context_available=False,
                stale=True,
                data_quality=AirspaceDataQuality.UNAVAILABLE,
                evaluated_at=evaluated_at,
                source_generated_at=parsed.generated_at,
                source_age_s=source_age_s,
                valid_record_count=len(parsed.aircraft),
                rejected_record_count=parsed.rejected_record_count,
                issue_count=len(parsed.issues),
            )
            return CivilAirspaceSnapshot(
                summary=summary,
                aircraft=(),
                issues=parsed.issues,
            )
        active = tuple(
            aircraft
            for aircraft in parsed.aircraft
            if self._is_active(aircraft, evaluated_at)
        )
        quality = self._quality(parsed, active)
        summary = CivilAirspaceSummary(
            active_count=len(active),
            state=AirspaceFeedState.CURRENT,
            nearby_context_available=bool(active),
            stale=False,
            data_quality=quality,
            evaluated_at=evaluated_at,
            source_generated_at=parsed.generated_at,
            source_age_s=source_age_s,
            valid_record_count=len(parsed.aircraft),
            rejected_record_count=parsed.rejected_record_count,
            issue_count=len(parsed.issues),
        )
        return CivilAirspaceSnapshot(
            summary=summary,
            aircraft=active,
            issues=parsed.issues,
        )

    def _is_active(
        self,
        aircraft: CivilAircraftContext,
        evaluated_at: datetime,
    ) -> bool:
        age_s = (evaluated_at - aircraft.observed_at).total_seconds()
        return (
            -self._policy.future_tolerance_s
            <= age_s
            <= self._policy.aircraft_ttl_s
        )

    @staticmethod
    def _quality(
        parsed: ParsedCivilAirspacePayload,
        active: tuple[CivilAircraftContext, ...],
    ) -> AirspaceDataQuality:
        if parsed.rejected_record_count == parsed.total_record_count and parsed.total_record_count:
            return AirspaceDataQuality.LIMITED
        if not parsed.timestamp_from_payload or parsed.issues:
            return AirspaceDataQuality.PARTIAL
        if any(item.data_quality is AirspaceDataQuality.LIMITED for item in active):
            return AirspaceDataQuality.PARTIAL
        return AirspaceDataQuality.GOOD

    def _fail_from_exception(
        self,
        exception: Exception,
        evaluated_at: datetime,
    ) -> CivilAirspaceSnapshot:
        self._parsed = None
        if isinstance(exception, AirspacePayloadError):
            code = exception.code
            message = str(exception)
        else:
            code = "AIRSPACE.INVALID_PAYLOAD"
            message = "Local aircraft payload failed validation."
        state = (
            AirspaceFeedState.IO_ERROR
            if code in {"AIRSPACE.FILE_IO", "AIRSPACE.FILE_NOT_FOUND"}
            else AirspaceFeedState.INVALID
        )
        self._snapshot = self._failure_snapshot(
            state,
            evaluated_at,
            AirspaceParseIssue(code=code, message=message),
        )
        return self._snapshot

    @staticmethod
    def _failure_snapshot(
        state: AirspaceFeedState,
        evaluated_at: datetime,
        issue: AirspaceParseIssue,
    ) -> CivilAirspaceSnapshot:
        summary = CivilAirspaceSummary(
            active_count=0,
            state=state,
            nearby_context_available=False,
            stale=True,
            data_quality=AirspaceDataQuality.UNAVAILABLE,
            evaluated_at=evaluated_at,
            source_generated_at=None,
            source_age_s=None,
            valid_record_count=0,
            rejected_record_count=0,
            issue_count=1,
        )
        return CivilAirspaceSnapshot(summary=summary, aircraft=(), issues=(issue,))

    @staticmethod
    def _empty_snapshot(evaluated_at: datetime) -> CivilAirspaceSnapshot:
        summary = CivilAirspaceSummary(
            active_count=0,
            state=AirspaceFeedState.NO_DATA,
            nearby_context_available=False,
            stale=True,
            data_quality=AirspaceDataQuality.UNAVAILABLE,
            evaluated_at=evaluated_at,
            source_generated_at=None,
            source_age_s=None,
            valid_record_count=0,
            rejected_record_count=0,
            issue_count=0,
            limitations=CIVIL_BROADCAST_LIMITATIONS,
        )
        return CivilAirspaceSnapshot(summary=summary, aircraft=(), issues=())

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now


CivilAdsbContextService = CivilAirspaceService


__all__ = [
    "CivilAdsbContextService",
    "CivilAirspacePolicy",
    "CivilAirspaceService",
    "DeterministicCivilAirspaceSource",
]
