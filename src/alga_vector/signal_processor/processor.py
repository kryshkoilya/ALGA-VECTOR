"""Facade joining normalization, policy, event delivery and interpretation."""

from __future__ import annotations

from datetime import datetime
from threading import RLock

from alga_vector.domain.models import SystemSnapshot
from alga_vector.targets import (
    FusedTarget,
    SensorReadinessInterpreter,
    SensorReadinessSnapshot,
    TargetAggregator,
)

from .bus import PublishResult, UnifiedEventBus
from .interpretation import HumanReadableInterpreter
from .normalizer import SnapshotEventNormalizer
from .policy import FailClosedEventPolicy
from .recommendations import RecommendationEngine
from .schema import (
    NormalizedEvent,
    NormalizedEventType,
    OperatorSituation,
    SensorState,
)

_TARGET_EVENT_TYPES = frozenset(
    {
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        NormalizedEventType.ACOUSTIC_ANOMALY,
        NormalizedEventType.DIRECTION_ESTIMATED,
        NormalizedEventType.MULTISENSOR_CORRELATED,
        NormalizedEventType.TARGET_CONFIRMED,
    }
)


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
        target_aggregator: TargetAggregator | None = None,
        readiness_interpreter: SensorReadinessInterpreter | None = None,
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
        self._target_aggregator = target_aggregator or TargetAggregator()
        self._readiness_interpreter = (
            readiness_interpreter or SensorReadinessInterpreter()
        )
        self._target_lock = RLock()
        self._last_target_evaluated_at: datetime | None = None
        self._targets: tuple[FusedTarget, ...] = ()
        self._current_target: FusedTarget | None = None
        self._sensor_readiness: SensorReadinessSnapshot | None = None

    @property
    def targets(self) -> tuple[FusedTarget, ...]:
        return self._targets

    @property
    def current_target(self) -> FusedTarget | None:
        return self._current_target

    @property
    def sensor_readiness(self) -> SensorReadinessSnapshot | None:
        return self._sensor_readiness

    def ingest(self, event: NormalizedEvent) -> PublishResult:
        """Accept an already-normalized future adapter/classifier event."""

        enriched = self._recommendations.enrich(event)
        self._policy.require_safe(enriched)
        self._ingest_target_event(enriched, evaluated_at=enriched.received_at)
        publication = self.event_bus.publish(enriched)
        return publication

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
            self._ingest_target_event(
                enriched,
                evaluated_at=snapshot.captured_at,
            )
            publication = self.event_bus.publish(enriched)
            if publication.accepted:
                current.append(enriched)

        self._refresh_targets(snapshot.captured_at)
        self._sensor_readiness = self._readiness_interpreter.interpret(
            snapshot,
            now=snapshot.captured_at,
        )

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

    def _ingest_target_event(
        self,
        event: NormalizedEvent,
        *,
        evaluated_at: datetime,
    ) -> None:
        if event.event_type not in _TARGET_EVENT_TYPES:
            return
        with self._target_lock:
            target_time = self._non_regressing_target_time(
                max(evaluated_at, event.received_at)
            )
            self._target_aggregator.ingest(event, now=target_time)
            self._last_target_evaluated_at = target_time

    def _refresh_targets(self, evaluated_at: datetime) -> None:
        with self._target_lock:
            target_time = self._non_regressing_target_time(evaluated_at)
            targets = self._target_aggregator.targets(
                now=target_time,
                include_stale=True,
            )
            self._last_target_evaluated_at = target_time
            self._targets = targets
            self._current_target = next(
                (target for target in targets if target.active),
                None,
            )

    def _non_regressing_target_time(self, value: datetime) -> datetime:
        previous = self._last_target_evaluated_at
        if previous is None or value >= previous:
            return value
        return previous


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
