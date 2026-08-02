"""Deterministic, bounded idempotency for normalized target events."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock

from alga_vector.signal_processor.schema import NormalizedEvent


class EventDeduplicationStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class EventDeduplicationDecision:
    status: EventDeduplicationStatus
    semantic_key: str
    reason_code: str

    @property
    def accepted(self) -> bool:
        return self.status is EventDeduplicationStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class _ExactRecord:
    content_fingerprint: str
    semantic_key: str


@dataclass(frozen=True, slots=True)
class _SemanticRecord:
    content_fingerprint: str
    remembered_at: datetime


class EventDeduplicator:
    """Exact-id and short-window semantic deduplication.

    Exact event identifiers remain idempotent while retained in the bounded
    index.  The semantic index catches the same immutable observation wrapped
    in a new event id, but deliberately includes ``observed_at`` so a refreshed
    observation in the same upstream episode is not suppressed.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        maximum_entries: int = 4_096,
    ) -> None:
        if window_seconds < 0.0:
            raise ValueError("window_seconds must be non-negative")
        if maximum_entries < 16:
            raise ValueError("maximum_entries must be at least 16")
        self._window_seconds = window_seconds
        self._maximum_entries = maximum_entries
        self._exact: OrderedDict[str, _ExactRecord] = OrderedDict()
        self._semantic: OrderedDict[str, _SemanticRecord] = OrderedDict()
        self._lock = RLock()

    @property
    def exact_entry_count(self) -> int:
        with self._lock:
            return len(self._exact)

    @property
    def semantic_entry_count(self) -> int:
        with self._lock:
            return len(self._semantic)

    def check_and_remember(
        self,
        event: NormalizedEvent,
        *,
        now: datetime,
    ) -> EventDeduplicationDecision:
        _require_aware(now, "now")
        if now < event.received_at:
            raise ValueError("deduplication time cannot precede event receipt")
        with self._lock:
            return self._check_and_remember_locked(event, now=now)

    def _check_and_remember_locked(
        self,
        event: NormalizedEvent,
        *,
        now: datetime,
    ) -> EventDeduplicationDecision:
        self._prune_semantic(now)

        semantic_key = event_semantic_key(event)
        content = _content_fingerprint(event)
        exact = self._exact.get(event.event_id)
        if exact is not None:
            self._exact.move_to_end(event.event_id)
            if exact.content_fingerprint == content:
                return EventDeduplicationDecision(
                    EventDeduplicationStatus.DUPLICATE,
                    semantic_key,
                    "TARGET.EVENT_ID_DUPLICATE",
                )
            return EventDeduplicationDecision(
                EventDeduplicationStatus.CONFLICT,
                semantic_key,
                "TARGET.EVENT_ID_CONFLICT",
            )

        semantic = self._semantic.get(semantic_key)
        if semantic is not None:
            self._semantic.move_to_end(semantic_key)
            if semantic.content_fingerprint == content:
                self._remember_exact(event.event_id, content, semantic_key)
                return EventDeduplicationDecision(
                    EventDeduplicationStatus.DUPLICATE,
                    semantic_key,
                    "TARGET.SEMANTIC_DUPLICATE",
                )
            return EventDeduplicationDecision(
                EventDeduplicationStatus.CONFLICT,
                semantic_key,
                "TARGET.SEMANTIC_CONFLICT",
            )

        self._remember_exact(event.event_id, content, semantic_key)
        self._semantic[semantic_key] = _SemanticRecord(content, now)
        self._trim(self._semantic)
        return EventDeduplicationDecision(
            EventDeduplicationStatus.ACCEPTED,
            semantic_key,
            "TARGET.EVENT_ACCEPTED",
        )

    def _remember_exact(
        self,
        event_id: str,
        content: str,
        semantic_key: str,
    ) -> None:
        self._exact[event_id] = _ExactRecord(content, semantic_key)
        self._trim(self._exact)

    def _prune_semantic(self, now: datetime) -> None:
        if self._window_seconds == 0.0:
            self._semantic.clear()
            return
        while self._semantic:
            key, record = next(iter(self._semantic.items()))
            age = (now - record.remembered_at).total_seconds()
            if age <= self._window_seconds:
                break
            self._semantic.pop(key)

    def _trim[T](self, index: OrderedDict[str, T]) -> None:
        while len(index) > self._maximum_entries:
            index.popitem(last=False)


def event_semantic_key(event: NormalizedEvent) -> str:
    """Return a stable key for one immutable sensor observation."""

    source_parts = tuple(
        sorted(
            (
                source.sensor_id,
                source.sensor_kind.value,
                source.observation_id or "",
            )
            for source in event.sources
        )
    )
    direction = (
        None
        if event.direction is None
        else (
            event.direction.source_id,
            round(event.direction.bearing_deg, 6),
            round(event.direction.uncertainty_deg, 6),
            event.direction.observed_at.isoformat(),
        )
    )
    identity = (
        None
        if event.identity is None
        else (
            event.identity.classifier_id,
            event.identity.model_version,
            event.identity.class_label,
            event.identity.validated_at.isoformat(),
        )
    )
    payload = (
        event.schema_version,
        event.event_type.value,
        event.observed_at.isoformat(),
        event.episode_id,
        source_parts,
        _rounded(event.frequency_hz),
        _rounded(event.bandwidth_hz),
        direction,
        identity,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_fingerprint(event: NormalizedEvent) -> str:
    payload = event.to_dict()
    # A producer may wrap the same immutable sensor observation in a new
    # delivery envelope. Event id and receipt time are transport metadata;
    # measured/derived observation content must remain semantically equal.
    payload.pop("event_id", None)
    payload.pop("trace_id", None)
    payload.pop("received_at", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "EventDeduplicationDecision",
    "EventDeduplicationStatus",
    "EventDeduplicator",
    "event_semantic_key",
]
