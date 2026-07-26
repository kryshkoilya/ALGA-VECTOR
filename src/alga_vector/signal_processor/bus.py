"""A small synchronous event bus with bounded, thread-safe history."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from .schema import EventSeverity, NormalizedEvent, NormalizedEventType

Subscriber = Callable[[NormalizedEvent], None]
MonotonicClock = Callable[[], float]

_SEVERITY_RANK = {
    EventSeverity.INFO: 0,
    EventSeverity.NOTICE: 1,
    EventSeverity.WARNING: 2,
    EventSeverity.ALARM: 3,
    EventSeverity.CRITICAL: 4,
}


@dataclass(frozen=True, slots=True)
class PublishResult:
    accepted: bool
    sequence: int | None
    duplicate: bool
    delivery_failures: int
    retained_in_history: bool


class UnifiedEventBus:
    """Synchronous fan-out with subscriber failure isolation.

    The lock protects sequencing, deduplication and history only.  Callbacks run
    after it is released, preventing a slow UI subscriber from blocking a
    publisher that merely wants a snapshot.
    """

    def __init__(
        self,
        *,
        capacity: int = 256,
        dedup_window_seconds: float = 2.0,
        monotonic_clock: MonotonicClock = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if dedup_window_seconds < 0.0:
            raise ValueError("dedup_window_seconds must be non-negative")
        self._history: deque[tuple[int, NormalizedEvent]] = deque()
        self._capacity = capacity
        self._dedup_capacity = max(16, capacity * 4)
        self._dedup_window_seconds = dedup_window_seconds
        self._monotonic_clock = monotonic_clock
        self._recent_keys: dict[str, float] = {}
        self._subscribers: dict[str, Subscriber] = {}
        self._sequence = 0
        self._lock = RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def subscribe(self, callback: Subscriber) -> str:
        if not callable(callback):
            raise TypeError("callback must be callable")
        token = uuid4().hex
        with self._lock:
            self._subscribers[token] = callback
        return token

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            return self._subscribers.pop(token, None) is not None

    def publish(self, event: NormalizedEvent) -> PublishResult:
        now = self._monotonic_clock()
        key = event.deduplication_key
        with self._lock:
            self._prune_dedup_locked(now)
            previous = self._recent_keys.get(key)
            if (
                self._dedup_window_seconds > 0.0
                and
                previous is not None
                and now - previous <= self._dedup_window_seconds
            ):
                return PublishResult(
                    accepted=False,
                    sequence=None,
                    duplicate=True,
                    delivery_failures=0,
                    retained_in_history=False,
                )
            self._recent_keys[key] = now
            self._trim_dedup_locked()
            self._sequence += 1
            sequence = self._sequence
            retained = self._retain_locked(sequence, event)
            subscribers = tuple(self._subscribers.values())

        failures = 0
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                failures += 1
        return PublishResult(
            accepted=True,
            sequence=sequence,
            duplicate=False,
            delivery_failures=failures,
            retained_in_history=retained,
        )

    def recent(
        self,
        *,
        limit: int = 50,
        event_types: frozenset[NormalizedEventType] | None = None,
        minimum_severity: EventSeverity | None = None,
    ) -> tuple[NormalizedEvent, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return ()
        with self._lock:
            snapshot = tuple(self._history)
        minimum_rank = (
            _SEVERITY_RANK[minimum_severity]
            if minimum_severity is not None
            else -1
        )
        selected = [
            event
            for _, event in reversed(snapshot)
            if (event_types is None or event.event_type in event_types)
            and _SEVERITY_RANK[event.severity] >= minimum_rank
        ]
        return tuple(selected[:limit])

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def dedup_entry_count(self) -> int:
        """Bounded diagnostic count; event keys themselves stay private."""

        with self._lock:
            return len(self._recent_keys)

    def _prune_dedup_locked(self, now: float) -> None:
        if not self._recent_keys:
            return
        cutoff = now - self._dedup_window_seconds
        expired = [
            key for key, published_at in self._recent_keys.items()
            if published_at < cutoff
        ]
        for key in expired:
            self._recent_keys.pop(key, None)

    def _trim_dedup_locked(self) -> None:
        while len(self._recent_keys) > self._dedup_capacity:
            oldest = min(
                self._recent_keys,
                key=self._recent_keys.__getitem__,
            )
            self._recent_keys.pop(oldest, None)

    def _retain_locked(
        self,
        sequence: int,
        event: NormalizedEvent,
    ) -> bool:
        if len(self._history) < self._capacity:
            self._history.append((sequence, event))
            return True
        incoming_rank = _SEVERITY_RANK[event.severity]
        lowest_rank = min(
            _SEVERITY_RANK[item.severity] for _, item in self._history
        )
        if incoming_rank < lowest_rank:
            return False
        evict_index = next(
            index
            for index, (_, item) in enumerate(self._history)
            if _SEVERITY_RANK[item.severity] == lowest_rank
        )
        del self._history[evict_index]
        self._history.append((sequence, event))
        return True


__all__ = ["PublishResult", "Subscriber", "UnifiedEventBus"]
