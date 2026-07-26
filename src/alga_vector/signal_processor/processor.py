"""Facade joining normalization, policy, event delivery and interpretation."""

from __future__ import annotations

from datetime import datetime

from alga_vector.domain.models import SystemSnapshot

from .bus import PublishResult, UnifiedEventBus
from .interpretation import HumanReadableInterpreter
from .normalizer import SnapshotEventNormalizer
from .policy import FailClosedEventPolicy
from .recommendations import RecommendationEngine
from .schema import NormalizedEvent, OperatorSituation, SensorState


class UnifiedSignalProcessor:
    """Single integration surface for Simple Mode and future input adapters."""

    def __init__(
        self,
        *,
        event_bus: UnifiedEventBus | None = None,
        normalizer: SnapshotEventNormalizer | None = None,
        interpreter: HumanReadableInterpreter | None = None,
        recommendation_engine: RecommendationEngine | None = None,
        policy: FailClosedEventPolicy | None = None,
        history_limit: int = 64,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        recommendations = recommendation_engine or RecommendationEngine()
        self.event_bus = event_bus or UnifiedEventBus()
        self._normalizer = normalizer or SnapshotEventNormalizer(
            recommendation_engine=recommendations
        )
        self._interpreter = interpreter or HumanReadableInterpreter(
            recent_event_limit=history_limit
        )
        self._recommendations = recommendations
        self._policy = policy or FailClosedEventPolicy()
        self._history_limit = history_limit
        self._last_sensors: tuple[SensorState, ...] = ()

    def ingest(self, event: NormalizedEvent) -> PublishResult:
        """Accept an already-normalized future adapter/classifier event."""

        enriched = self._recommendations.enrich(event)
        self._policy.require_safe(enriched)
        return self.event_bus.publish(enriched)

    def process_snapshot(
        self,
        snapshot: SystemSnapshot,
        *,
        additional_events: tuple[NormalizedEvent, ...] = (),
        important_only: bool = False,
    ) -> OperatorSituation:
        result = self._normalizer.normalize(snapshot)
        self._last_sensors = result.sensors
        current: list[NormalizedEvent] = []
        for event in result.events + additional_events:
            enriched = self._recommendations.enrich(event)
            self._policy.require_safe(enriched)
            publication = self.event_bus.publish(enriched)
            if publication.accepted:
                current.append(enriched)

        history = self.event_bus.recent(limit=self._history_limit)
        merged = _unique_events(tuple(current) + history)
        return self._interpreter.interpret(
            merged,
            result.sensors,
            now=snapshot.captured_at,
            important_only=important_only,
        )

    def current_situation(
        self,
        *,
        now: datetime,
        important_only: bool = False,
    ) -> OperatorSituation:
        return self._interpreter.interpret(
            self.event_bus.recent(limit=self._history_limit),
            self._last_sensors,
            now=now,
            important_only=important_only,
        )


def _unique_events(
    events: tuple[NormalizedEvent, ...],
) -> tuple[NormalizedEvent, ...]:
    seen: set[str] = set()
    unique: list[NormalizedEvent] = []
    for event in events:
        key = event.deduplication_key
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return tuple(unique)


__all__ = ["UnifiedSignalProcessor"]
