"""Bounded, deterministic target projection over normalized operator events."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from threading import RLock

from alga_vector.signal_processor.schema import (
    ConfidenceScore,
    DirectionEstimate,
    EvidenceFact,
    NormalizedEvent,
    NormalizedEventType,
    SensorKind,
    SourceAttribution,
)

from .dedup import (
    EventDeduplicationStatus,
    EventDeduplicator,
)
from .models import (
    ConfirmationStage,
    FusedTarget,
    PhenomenologicalType,
    TargetLifecycle,
    TargetSourceAttribution,
    TargetUpdate,
    TargetUpdateStatus,
    ValidatedZone,
)
from .recommendations import TargetRecommendationEngine

_ACTIVITY_TYPES = frozenset(
    {
        NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
        NormalizedEventType.LIKELY_DRONE_SIGNATURE,
        NormalizedEventType.ACOUSTIC_ANOMALY,
        NormalizedEventType.MULTISENSOR_CORRELATED,
        NormalizedEventType.TARGET_CONFIRMED,
    }
)
_CONTEXT_TYPES = frozenset({NormalizedEventType.DIRECTION_ESTIMATED})
_CLASSIFICATION_TYPES = frozenset(
    {
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
    }
)
_STAGE_RANK = {
    ConfirmationStage.BACKGROUND: 0,
    ConfirmationStage.SUSPICIOUS_ACTIVITY: 1,
    ConfirmationStage.LIKELY_SOURCE: 2,
    ConfirmationStage.LIKELY_TARGET: 3,
    ConfirmationStage.CONFIRMED_TARGET: 4,
}
_LIFECYCLE_RANK = {
    TargetLifecycle.ACTIVE: 0,
    TargetLifecycle.HOLDING: 1,
    TargetLifecycle.STALE: 2,
    TargetLifecycle.TOMBSTONED: 3,
}
_TYPE_PRIORITY = {
    NormalizedEventType.TARGET_CONFIRMED: 100,
    NormalizedEventType.LIKELY_DRONE_SIGNATURE: 90,
    NormalizedEventType.MULTISENSOR_CORRELATED: 80,
    NormalizedEventType.LIKELY_VIDEO_LINK: 75,
    NormalizedEventType.LIKELY_HANDHELD_RADIO: 70,
    NormalizedEventType.ACOUSTIC_ANOMALY: 60,
    NormalizedEventType.RADIO_ACTIVITY_DETECTED: 50,
    NormalizedEventType.DIRECTION_ESTIMATED: 10,
}


class TargetInputError(ValueError):
    """An event cannot safely or deterministically advance target state."""


@dataclass(frozen=True, slots=True)
class TargetAggregatorConfig:
    """Validated target projection policy.

    The first six fields intentionally match ``AppConfig.target_tracking``.
    """

    correlation_window_seconds: float = 12.0
    deduplication_window_seconds: float = 4.0
    decay_half_life_seconds: float = 18.0
    stale_after_seconds: float = 30.0
    retire_after_seconds: float = 90.0
    maximum_active_targets: int = 64
    maximum_tombstones: int = 128
    maximum_events_per_target: int = 64
    maximum_sources_per_target: int = 16
    maximum_seen_events: int = 4_096
    tombstone_retention_seconds: float = 300.0
    minimum_association_score: float = 0.65
    frequency_tolerance_hz: float = 50_000.0
    direction_tolerance_deg: float = 15.0
    minimum_direction_confidence: float = 0.4

    def __post_init__(self) -> None:
        positive = (
            ("correlation_window_seconds", self.correlation_window_seconds),
            ("decay_half_life_seconds", self.decay_half_life_seconds),
            ("stale_after_seconds", self.stale_after_seconds),
            ("retire_after_seconds", self.retire_after_seconds),
            ("tombstone_retention_seconds", self.tombstone_retention_seconds),
            ("frequency_tolerance_hz", self.frequency_tolerance_hz),
            ("direction_tolerance_deg", self.direction_tolerance_deg),
        )
        for name, value in positive:
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if (
            not math.isfinite(self.deduplication_window_seconds)
            or self.deduplication_window_seconds < 0.0
        ):
            raise ValueError(
                "deduplication_window_seconds must be finite and non-negative"
            )
        if self.stale_after_seconds <= self.correlation_window_seconds:
            raise ValueError(
                "stale_after_seconds must exceed correlation_window_seconds"
            )
        if self.retire_after_seconds <= self.stale_after_seconds:
            raise ValueError("retire_after_seconds must exceed stale_after_seconds")
        for name, value, minimum in (
            ("maximum_active_targets", self.maximum_active_targets, 1),
            ("maximum_tombstones", self.maximum_tombstones, 1),
            ("maximum_events_per_target", self.maximum_events_per_target, 4),
            ("maximum_sources_per_target", self.maximum_sources_per_target, 2),
            ("maximum_seen_events", self.maximum_seen_events, 16),
        ):
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
        if not 0.0 < self.minimum_association_score <= 1.0:
            raise ValueError("minimum_association_score must be within (0, 1]")
        if (
            isinstance(self.minimum_direction_confidence, bool)
            or not math.isfinite(self.minimum_direction_confidence)
            or not 0.0 < self.minimum_direction_confidence <= 1.0
        ):
            raise ValueError(
                "minimum_direction_confidence must be within (0, 1]"
            )
        if self.direction_tolerance_deg > 180.0:
            raise ValueError("direction_tolerance_deg must not exceed 180")


@dataclass(slots=True)
class _Track:
    target_id: str
    created_at: datetime
    updated_at: datetime
    last_seen: datetime
    lifecycle: TargetLifecycle
    confirmation_stage: ConfirmationStage
    probable_type: PhenomenologicalType
    events: OrderedDict[str, NormalizedEvent] = field(default_factory=OrderedDict)
    merged_from: list[str] = field(default_factory=list)
    zone: ValidatedZone | None = None
    tombstoned_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _Association:
    track: _Track
    score: float


class TargetAggregator:
    """Aggregate policy-safe events into immutable operator targets.

    Correlation is deliberately fail-closed.  Temporal proximity alone never
    merges targets: an explicit episode/observation link, compatible frequency
    from a shared sensor, or an independently-confirmed fusion bridge is
    required.
    """

    def __init__(
        self,
        config: TargetAggregatorConfig | None = None,
        *,
        recommendation_engine: TargetRecommendationEngine | None = None,
    ) -> None:
        self.config = config or TargetAggregatorConfig()
        self._recommendations = recommendation_engine or TargetRecommendationEngine()
        self._deduplicator = EventDeduplicator(
            window_seconds=self.config.deduplication_window_seconds,
            maximum_entries=self.config.maximum_seen_events,
        )
        self._tracks: OrderedDict[str, _Track] = OrderedDict()
        self._last_evaluated_at: datetime | None = None
        self._lock = RLock()

    @property
    def tracked_target_count(self) -> int:
        with self._lock:
            return len(self._tracks)

    @property
    def dedup_entry_count(self) -> int:
        with self._lock:
            return self._deduplicator.exact_entry_count

    def reset(self) -> None:
        with self._lock:
            self._tracks.clear()
            self._deduplicator = EventDeduplicator(
                window_seconds=self.config.deduplication_window_seconds,
                maximum_entries=self.config.maximum_seen_events,
            )
            self._last_evaluated_at = None

    def ingest(
        self,
        event: NormalizedEvent,
        *,
        now: datetime | None = None,
    ) -> TargetUpdate:
        if not isinstance(event, NormalizedEvent):
            raise TargetInputError("expected NormalizedEvent")
        evaluated_at = event.received_at if now is None else now
        _require_aware(evaluated_at, "now")
        if evaluated_at < event.received_at:
            raise TargetInputError("evaluation time cannot precede event receipt")

        with self._lock:
            self._require_non_decreasing(evaluated_at)
            self._advance(evaluated_at)
            dedup = self._deduplicator.check_and_remember(
                event,
                now=evaluated_at,
            )
            if dedup.status is EventDeduplicationStatus.DUPLICATE:
                return TargetUpdate(
                    status=TargetUpdateStatus.DUPLICATE,
                    evaluated_at=evaluated_at,
                    target=None,
                    reason_code=dedup.reason_code,
                )
            if dedup.status is EventDeduplicationStatus.CONFLICT:
                raise TargetInputError(dedup.reason_code)

            if event.event_type in _CONTEXT_TYPES:
                rejection_reason = _direction_rejection_reason(
                    event,
                    evaluated_at,
                    self.config.minimum_direction_confidence,
                )
                if rejection_reason is not None:
                    return TargetUpdate(
                        status=TargetUpdateStatus.IGNORED,
                        evaluated_at=evaluated_at,
                        target=None,
                        reason_code=rejection_reason,
                    )
                if not self._event_is_live(event, evaluated_at):
                    return TargetUpdate(
                        status=TargetUpdateStatus.IGNORED,
                        evaluated_at=evaluated_at,
                        target=None,
                        reason_code="TARGET.EXPIRED_EVENT_IGNORED",
                    )
                track = self._explicit_context_track(event)
                if track is None:
                    return TargetUpdate(
                        status=TargetUpdateStatus.IGNORED,
                        evaluated_at=evaluated_at,
                        target=None,
                        reason_code="TARGET.UNASSOCIATED_DIRECTION_IGNORED",
                    )
                self._add_event(track, event, dedup.semantic_key, evaluated_at)
                target = self._build_target(track, evaluated_at)
                return TargetUpdate(
                    status=TargetUpdateStatus.UPDATED,
                    evaluated_at=evaluated_at,
                    target=target,
                    reason_code="TARGET.DIRECTION_ATTACHED",
                )

            if event.event_type not in _ACTIVITY_TYPES:
                return TargetUpdate(
                    status=TargetUpdateStatus.IGNORED,
                    evaluated_at=evaluated_at,
                    target=None,
                    reason_code="TARGET.NON_TARGET_EVENT_IGNORED",
                )
            if not self._event_is_live(event, evaluated_at):
                return TargetUpdate(
                    status=TargetUpdateStatus.IGNORED,
                    evaluated_at=evaluated_at,
                    target=None,
                    reason_code="TARGET.EXPIRED_EVENT_IGNORED",
                )
            if not event.sources:
                return TargetUpdate(
                    status=TargetUpdateStatus.IGNORED,
                    evaluated_at=evaluated_at,
                    target=None,
                    reason_code="TARGET.SOURCE_ATTRIBUTION_REQUIRED",
                )

            projected_event = event
            if (
                event.direction is not None
                and _direction_rejection_reason(
                    event,
                    evaluated_at,
                    self.config.minimum_direction_confidence,
                )
                is not None
            ):
                projected_event = replace(event, direction=None)

            associations = self._associations(projected_event, evaluated_at)
            merged_ids: tuple[str, ...] = ()
            if self._is_fusion_bridge(projected_event, associations):
                track, merged_ids = self._merge_associations(associations, evaluated_at)
                status = TargetUpdateStatus.UPDATED
                reason = "TARGET.FUSION_BRIDGE_MERGED"
            else:
                track = self._select_unambiguous(associations)
                if track is None:
                    if self._active_count() >= self.config.maximum_active_targets:
                        return TargetUpdate(
                            status=TargetUpdateStatus.CAPACITY_REJECTED,
                            evaluated_at=evaluated_at,
                            target=None,
                            reason_code="TARGET.ACTIVE_CAPACITY_REACHED",
                        )
                    track = self._new_track(
                        projected_event,
                        dedup.semantic_key,
                        evaluated_at,
                    )
                    status = TargetUpdateStatus.CREATED
                    reason = "TARGET.CREATED"
                else:
                    status = TargetUpdateStatus.UPDATED
                    reason = "TARGET.CORRELATED"

            self._add_event(
                track,
                projected_event,
                dedup.semantic_key,
                evaluated_at,
            )
            self._enforce_memory_bound()
            target = self._build_target(track, evaluated_at)
            return TargetUpdate(
                status=status,
                evaluated_at=evaluated_at,
                target=target,
                reason_code=reason,
                merged_target_ids=merged_ids,
            )

    def tick(self, now: datetime) -> tuple[FusedTarget, ...]:
        _require_aware(now, "now")
        with self._lock:
            self._require_non_decreasing(now)
            self._advance(now)
            return self._snapshots(now, include_stale=True, include_tombstones=False)

    def targets(
        self,
        *,
        now: datetime,
        include_stale: bool = True,
        include_tombstones: bool = False,
    ) -> tuple[FusedTarget, ...]:
        _require_aware(now, "now")
        with self._lock:
            self._require_non_decreasing(now)
            self._advance(now)
            return self._snapshots(
                now,
                include_stale=include_stale,
                include_tombstones=include_tombstones,
            )

    def active_targets(self, *, now: datetime) -> tuple[FusedTarget, ...]:
        return tuple(
            target
            for target in self.targets(now=now, include_stale=False)
            if target.active
        )

    def attach_validated_zone(
        self,
        target_id: str,
        zone: ValidatedZone,
        *,
        now: datetime,
    ) -> FusedTarget:
        """Attach an explicit fresh zone; this method never derives one."""

        _require_aware(now, "now")
        if not zone.is_fresh_at(now):
            raise TargetInputError("validated zone is not fresh")
        with self._lock:
            self._require_non_decreasing(now)
            self._advance(now)
            track = self._tracks.get(target_id)
            if track is None or track.lifecycle is TargetLifecycle.TOMBSTONED:
                raise TargetInputError("target is unavailable")
            track.zone = zone
            track.updated_at = now
            return self._build_target(track, now)

    def _require_non_decreasing(self, now: datetime) -> None:
        if self._last_evaluated_at is not None and now < self._last_evaluated_at:
            raise TargetInputError("target evaluation time regressed")
        self._last_evaluated_at = now

    def _advance(self, now: datetime) -> None:
        to_remove: list[str] = []
        for target_id, track in self._tracks.items():
            age = max(0.0, (now - track.last_seen).total_seconds())
            live = any(
                event.event_type in _ACTIVITY_TYPES
                and self._event_is_live(event, now)
                for event in track.events.values()
            )
            if age >= self.config.retire_after_seconds:
                if track.lifecycle is not TargetLifecycle.TOMBSTONED:
                    track.lifecycle = TargetLifecycle.TOMBSTONED
                    track.tombstoned_at = track.last_seen + timedelta(
                        seconds=self.config.retire_after_seconds
                    )
                    track.updated_at = now
            elif age >= self.config.stale_after_seconds:
                if track.lifecycle is not TargetLifecycle.STALE:
                    track.lifecycle = TargetLifecycle.STALE
                    track.tombstoned_at = None
                    track.updated_at = now
            elif live:
                if track.lifecycle is not TargetLifecycle.ACTIVE:
                    track.lifecycle = TargetLifecycle.ACTIVE
                    track.tombstoned_at = None
                    track.updated_at = now
            elif track.lifecycle is not TargetLifecycle.HOLDING:
                track.lifecycle = TargetLifecycle.HOLDING
                track.tombstoned_at = None
                track.updated_at = now

            if (
                track.lifecycle is TargetLifecycle.TOMBSTONED
                and track.tombstoned_at is not None
                and (now - track.tombstoned_at).total_seconds()
                >= self.config.tombstone_retention_seconds
            ):
                to_remove.append(target_id)
        for target_id in to_remove:
            self._tracks.pop(target_id, None)
        self._enforce_memory_bound()

    def _explicit_context_track(self, event: NormalizedEvent) -> _Track | None:
        if event.episode_id is None:
            return None
        candidates = [
            track
            for track in self._tracks.values()
            if track.lifecycle is not TargetLifecycle.TOMBSTONED
            and event.episode_id in _episode_ids(track)
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _associations(
        self,
        event: NormalizedEvent,
        now: datetime,
    ) -> tuple[_Association, ...]:
        associations: list[_Association] = []
        for track in self._tracks.values():
            if track.lifecycle is TargetLifecycle.TOMBSTONED:
                continue
            score = self._association_score(track, event, now)
            if score >= self.config.minimum_association_score:
                associations.append(_Association(track, score))
        return tuple(
            sorted(
                associations,
                key=lambda item: (
                    -item.score,
                    item.track.created_at,
                    item.track.target_id,
                ),
            )
        )

    def _association_score(
        self,
        track: _Track,
        event: NormalizedEvent,
        now: datetime,
    ) -> float:
        del now
        gap = abs((event.observed_at - track.last_seen).total_seconds())
        if gap > self.config.correlation_window_seconds:
            return 0.0
        if not _frequency_compatible(track, event, self.config.frequency_tolerance_hz):
            return 0.0
        if not _direction_compatible(track, event, self.config.direction_tolerance_deg):
            return 0.0

        event_sources = {item.sensor_id for item in event.sources}
        track_sources = _source_ids(track)
        event_observations = {
            item.observation_id for item in event.sources if item.observation_id
        }
        shared_episode = bool(
            event.episode_id is not None and event.episode_id in _episode_ids(track)
        )
        shared_observation = bool(event_observations & _observation_ids(track))
        shared_sources = bool(event_sources & track_sources)
        frequency_match = _has_compatible_frequency(track, event)
        direction_match = _has_compatible_direction(track, event)

        score = 0.05 * (
            1.0 - min(1.0, gap / self.config.correlation_window_seconds)
        )
        if shared_episode:
            score += 0.70
        if shared_observation:
            score += 0.80
        if shared_sources:
            score += 0.35
        if frequency_match:
            score += 0.30
        if direction_match:
            score += 0.20
        if (
            event.event_type is NormalizedEventType.MULTISENSOR_CORRELATED
            and shared_sources
        ):
            score += 0.30
        if any(
            prior.event_type is event.event_type for prior in track.events.values()
        ):
            score += 0.05
        return min(1.0, score)

    @staticmethod
    def _select_unambiguous(
        associations: tuple[_Association, ...],
    ) -> _Track | None:
        if not associations:
            return None
        if len(associations) > 1 and (
            associations[0].score - associations[1].score < 0.05
        ):
            return None
        return associations[0].track

    @staticmethod
    def _is_fusion_bridge(
        event: NormalizedEvent,
        associations: tuple[_Association, ...],
    ) -> bool:
        if (
            event.event_type is not NormalizedEventType.MULTISENSOR_CORRELATED
            or len(associations) < 2
        ):
            return False
        confirming = {
            source.sensor_id
            for source in event.sources
            if source.independent_confirmation
        }
        if len(confirming) < 2:
            return False
        matched = tuple(_source_ids(item.track) & confirming for item in associations)
        if len(associations) != len(confirming) or any(len(item) != 1 for item in matched):
            return False
        # Each independent confirming source may bridge at most one pre-existing
        # target.  This prevents one wideband receiver from collapsing several
        # unrelated frequency tracks into a single target.
        return len(set[str]().union(*matched)) == len(confirming)

    def _merge_associations(
        self,
        associations: tuple[_Association, ...],
        now: datetime,
    ) -> tuple[_Track, tuple[str, ...]]:
        ordered = sorted(
            (item.track for item in associations),
            key=lambda item: (item.created_at, item.target_id),
        )
        primary = ordered[0]
        merged_ids: list[str] = []
        for other in ordered[1:]:
            for key, event in other.events.items():
                primary.events.setdefault(key, event)
            for target_id in (other.target_id, *other.merged_from):
                if target_id not in primary.merged_from:
                    primary.merged_from.append(target_id)
            primary.created_at = min(primary.created_at, other.created_at)
            primary.last_seen = max(primary.last_seen, other.last_seen)
            merged_ids.append(other.target_id)
            self._tracks.pop(other.target_id, None)
        primary.updated_at = now
        self._trim_events(primary)
        return primary, tuple(merged_ids)

    def _new_track(
        self,
        event: NormalizedEvent,
        semantic_key: str,
        now: datetime,
    ) -> _Track:
        target_id = _target_id(semantic_key)
        suffix = 1
        base_id = target_id
        while target_id in self._tracks:
            suffix += 1
            target_id = f"{base_id}-{suffix}"
        stage = _stage_for_event(event)
        probable_type = _type_for_event(event)
        track = _Track(
            target_id=target_id,
            created_at=event.observed_at,
            updated_at=now,
            last_seen=event.observed_at,
            lifecycle=TargetLifecycle.ACTIVE,
            confirmation_stage=stage,
            probable_type=probable_type,
        )
        self._tracks[target_id] = track
        return track

    def _add_event(
        self,
        track: _Track,
        event: NormalizedEvent,
        semantic_key: str,
        now: datetime,
    ) -> None:
        track.events[semantic_key] = event
        track.events.move_to_end(semantic_key)
        if event.event_type in _ACTIVITY_TYPES:
            track.last_seen = max(track.last_seen, event.observed_at)
            if self._event_is_live(event, now):
                track.lifecycle = TargetLifecycle.ACTIVE
                track.tombstoned_at = None
        desired_stage = _stage_for_event(event)
        if _STAGE_RANK[desired_stage] >= _STAGE_RANK[track.confirmation_stage]:
            track.confirmation_stage = desired_stage
            track.probable_type = _type_for_event(event)
        track.updated_at = now
        self._tracks.move_to_end(track.target_id)
        self._trim_events(track)

    def _trim_events(self, track: _Track) -> None:
        while len(track.events) > self.config.maximum_events_per_target:
            track.events.popitem(last=False)

    def _active_count(self) -> int:
        return sum(
            track.lifecycle is TargetLifecycle.ACTIVE
            for track in self._tracks.values()
        )

    def _enforce_memory_bound(self) -> None:
        maximum = (
            self.config.maximum_active_targets + self.config.maximum_tombstones
        )
        if len(self._tracks) <= maximum:
            return
        removable = sorted(
            (
                track
                for track in self._tracks.values()
                if track.lifecycle
                in {TargetLifecycle.TOMBSTONED, TargetLifecycle.STALE}
            ),
            key=lambda item: (
                0 if item.lifecycle is TargetLifecycle.TOMBSTONED else 1,
                item.last_seen,
                item.target_id,
            ),
        )
        for track in removable:
            if len(self._tracks) <= maximum:
                break
            self._tracks.pop(track.target_id, None)

    def _snapshots(
        self,
        now: datetime,
        *,
        include_stale: bool,
        include_tombstones: bool,
    ) -> tuple[FusedTarget, ...]:
        targets = [
            self._build_target(track, now)
            for track in self._tracks.values()
            if (
                include_tombstones
                or track.lifecycle is not TargetLifecycle.TOMBSTONED
            )
            and (include_stale or track.lifecycle is not TargetLifecycle.STALE)
        ]
        return tuple(
            sorted(
                targets,
                key=lambda target: (
                    _LIFECYCLE_RANK[target.lifecycle],
                    -_STAGE_RANK[target.confirmation_stage],
                    -target.last_seen.timestamp(),
                    target.target_id,
                ),
            )
        )

    def _build_target(self, track: _Track, now: datetime) -> FusedTarget:
        ordered_events = tuple(
            sorted(
                track.events.values(),
                key=lambda item: (item.observed_at, item.event_id),
                reverse=True,
            )
        )
        live_events = tuple(
            event for event in ordered_events if self._event_is_live(event, now)
        )
        activity_events = tuple(
            event for event in ordered_events if event.event_type in _ACTIVITY_TYPES
        )
        live_activity_events = tuple(
            event for event in live_events if event.event_type in _ACTIVITY_TYPES
        )
        primary_pool = live_activity_events or activity_events
        primary = max(
            primary_pool,
            key=lambda item: (
                _TYPE_PRIORITY.get(item.event_type, 0),
                item.observed_at,
                item.event_id,
            ),
        )

        classification_conflict = False
        if live_activity_events:
            desired_stage = max(
                (_stage_for_event(event) for event in live_activity_events),
                key=_STAGE_RANK.__getitem__,
            )
            # Stages can de-escalate as stronger evidence expires; generic
            # evidence can never promote itself into an identity stage.
            track.confirmation_stage = desired_stage
            stage_events = tuple(
                event
                for event in live_activity_events
                if _stage_for_event(event) is desired_stage
            )
            type_event, classification_conflict = _select_stage_type(stage_events)
            track.probable_type = (
                PhenomenologicalType.UNKNOWN_ACTIVITY
                if type_event is None
                else _type_for_event(type_event)
            )
        else:
            # Expiry is a hard boundary for an identity claim.  HOLDING keeps
            # the track available for audit/correlation, but must not preserve
            # a confirmed or likely identity after all activity evidence died.
            track.confirmation_stage = ConfirmationStage.SUSPICIOUS_ACTIVITY
            track.probable_type = PhenomenologicalType.UNKNOWN_ACTIVITY

        direction, direction_conflict = _select_direction(
            live_events,
            now,
            self.config.minimum_direction_confidence,
        )
        zone = track.zone if track.zone is not None and track.zone.is_fresh_at(now) else None
        attribution = self._source_attribution(
            live_events,
            now,
            required_direction_source_id=(
                None if direction is None else direction.source_id
            ),
        )
        sensors_used = tuple(
            sorted(
                {item.sensor_kind for item in attribution},
                key=lambda item: item.value,
            )
        )
        evidence_strength = _evidence_strength(
            tuple(
                item
                for item in attribution
                if item.sensor_kind
                not in {SensorKind.DIRECTION_FINDER, SensorKind.ADSB}
            ),
            bool(live_activity_events),
        )
        limitations = _limitations(
            live_events or ordered_events,
            lifecycle=track.lifecycle,
            direction=direction,
            direction_conflict=direction_conflict,
            classification_conflict=classification_conflict,
        )
        operator_label = _operator_label(
            track.confirmation_stage,
            track.probable_type,
        )
        explanation = _operator_explanation(
            track.confirmation_stage,
            track.probable_type,
            attribution,
            direction,
        )
        recommendation = self._recommendations.recommend(
            stage=track.confirmation_stage,
            probable_type=track.probable_type,
            direction_available=direction is not None,
            lifecycle=track.lifecycle,
        )
        tombstoned_at = (
            track.tombstoned_at
            if track.lifecycle is TargetLifecycle.TOMBSTONED
            else None
        )
        return FusedTarget(
            target_id=track.target_id,
            lifecycle=track.lifecycle,
            confirmation_stage=track.confirmation_stage,
            probable_type=track.probable_type,
            technical_label=(
                "CLASSIFICATION_CONFLICT"
                if classification_conflict
                else primary.event_type.value
            ),
            operator_label=operator_label,
            operator_explanation=explanation,
            created_at=track.created_at,
            updated_at=max(track.updated_at, now),
            last_seen=track.last_seen,
            sensors_used=sensors_used,
            source_attribution=attribution,
            direction=direction,
            zone=zone,
            recommendation=recommendation,
            evidence_strength=evidence_strength,
            evidence=_target_evidence(live_events),
            limitations=limitations,
            recent_event_ids=tuple(
                dict.fromkeys(
                    event.event_id
                    for event in ordered_events[
                        : self.config.maximum_events_per_target
                    ]
                )
            ),
            merged_from_target_ids=tuple(track.merged_from),
            tombstoned_at=tombstoned_at,
        )

    def _source_attribution(
        self,
        events: tuple[NormalizedEvent, ...],
        now: datetime,
        *,
        required_direction_source_id: str | None = None,
    ) -> tuple[TargetSourceAttribution, ...]:
        grouped: dict[str, list[tuple[NormalizedEvent, SourceAttribution]]] = {}
        for event in events:
            for source in event.sources:
                grouped.setdefault(source.sensor_id, []).append((event, source))

        items: list[TargetSourceAttribution] = []
        for sensor_id, pairs in grouped.items():
            event_times = tuple(event.observed_at for event, _ in pairs)
            latest_event, latest_source = max(
                pairs,
                key=lambda pair: (pair[0].observed_at, pair[0].event_id),
            )
            contributions = tuple(
                source.contribution * self._decay(event, now)
                for event, source in pairs
            )
            items.append(
                TargetSourceAttribution(
                    sensor_id=sensor_id,
                    sensor_kind=latest_source.sensor_kind,
                    contribution=max(contributions, default=0.0),
                    independent_confirmation=any(
                        source.independent_confirmation
                        and self._event_is_live(event, now)
                        for event, source in pairs
                    ),
                    first_seen=min(event_times),
                    last_seen=max(event_times),
                    observation_count=len(pairs),
                    latest_event_id=latest_event.event_id,
                    explanation_ru=latest_source.explanation_ru,
                    provenance=latest_source.provenance,
                )
            )
        ordered = sorted(
            items,
            key=lambda item: (-item.contribution, item.sensor_id),
        )
        selected = ordered[: self.config.maximum_sources_per_target]
        if (
            required_direction_source_id is not None
            and not any(
                item.sensor_id == required_direction_source_id
                and item.sensor_kind is SensorKind.DIRECTION_FINDER
                for item in selected
            )
        ):
            required = next(
                (
                    item
                    for item in ordered
                    if item.sensor_id == required_direction_source_id
                    and item.sensor_kind is SensorKind.DIRECTION_FINDER
                ),
                None,
            )
            if required is not None:
                selected = [
                    *selected[: self.config.maximum_sources_per_target - 1],
                    required,
                ]
        return tuple(selected)

    def _event_is_live(self, event: NormalizedEvent, now: datetime) -> bool:
        if now < event.observed_at:
            return False
        maximum_age = self.config.stale_after_seconds
        if (now - event.observed_at).total_seconds() >= maximum_age:
            return False
        return event.valid_until is None or now <= event.valid_until

    def _decay(self, event: NormalizedEvent, now: datetime) -> float:
        return time_decay(
            event,
            now=now,
            half_life_seconds=self.config.decay_half_life_seconds,
            maximum_age_seconds=self.config.stale_after_seconds,
        )


def time_decay(
    event: NormalizedEvent,
    *,
    now: datetime,
    half_life_seconds: float,
    maximum_age_seconds: float,
) -> float:
    """Return deterministic heuristic freshness in ``0..1``."""

    _require_aware(now, "now")
    if half_life_seconds <= 0.0 or not math.isfinite(half_life_seconds):
        raise ValueError("half_life_seconds must be finite and positive")
    if maximum_age_seconds <= 0.0 or not math.isfinite(maximum_age_seconds):
        raise ValueError("maximum_age_seconds must be finite and positive")
    age = (now - event.observed_at).total_seconds()
    if age < 0.0 or age >= maximum_age_seconds:
        return 0.0
    if event.valid_until is not None and now > event.valid_until:
        return 0.0
    return max(0.0, min(1.0, math.exp2(-age / half_life_seconds)))


def _stage_for_event(event: NormalizedEvent) -> ConfirmationStage:
    if event.event_type is NormalizedEventType.TARGET_CONFIRMED:
        return ConfirmationStage.CONFIRMED_TARGET
    if event.event_type is NormalizedEventType.LIKELY_DRONE_SIGNATURE:
        return ConfirmationStage.LIKELY_TARGET
    if event.event_type in {
        NormalizedEventType.LIKELY_HANDHELD_RADIO,
        NormalizedEventType.LIKELY_VIDEO_LINK,
        NormalizedEventType.MULTISENSOR_CORRELATED,
    }:
        return ConfirmationStage.LIKELY_SOURCE
    if (
        event.event_type
        in {
            NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            NormalizedEventType.ACOUSTIC_ANOMALY,
        }
        and event.confidence.value is not None
        and event.confidence.value >= 0.75
    ):
        return ConfirmationStage.LIKELY_SOURCE
    return ConfirmationStage.SUSPICIOUS_ACTIVITY


def _type_for_event(event: NormalizedEvent) -> PhenomenologicalType:
    return {
        NormalizedEventType.RADIO_ACTIVITY_DETECTED: PhenomenologicalType.RF_ACTIVITY,
        NormalizedEventType.LIKELY_HANDHELD_RADIO: (
            PhenomenologicalType.HANDHELD_RADIO_LIKE
        ),
        NormalizedEventType.LIKELY_VIDEO_LINK: PhenomenologicalType.VIDEO_LINK_LIKE,
        NormalizedEventType.LIKELY_DRONE_SIGNATURE: (
            PhenomenologicalType.VALIDATED_UAS_LIKE
        ),
        NormalizedEventType.ACOUSTIC_ANOMALY: (
            PhenomenologicalType.ACOUSTIC_ACTIVITY
        ),
        NormalizedEventType.MULTISENSOR_CORRELATED: (
            PhenomenologicalType.MULTISENSOR_ACTIVITY
        ),
        NormalizedEventType.TARGET_CONFIRMED: (
            PhenomenologicalType.VALIDATED_UAS_LIKE
        ),
    }.get(event.event_type, PhenomenologicalType.UNKNOWN_ACTIVITY)


def _operator_label(
    stage: ConfirmationStage,
    probable_type: PhenomenologicalType,
) -> str:
    if stage is ConfirmationStage.CONFIRMED_TARGET:
        return "Цель подтверждена независимыми данными"
    if stage is ConfirmationStage.LIKELY_TARGET:
        return "Вероятная цель"
    return {
        PhenomenologicalType.RF_ACTIVITY: "RF-активность",
        PhenomenologicalType.HANDHELD_RADIO_LIKE: (
            "Источник, совместимый с портативной радиосвязью"
        ),
        PhenomenologicalType.VIDEO_LINK_LIKE: "Вероятный видеоканал",
        PhenomenologicalType.ACOUSTIC_ACTIVITY: "Акустическая аномалия",
        PhenomenologicalType.MULTISENSOR_ACTIVITY: (
            "Согласованная активность нескольких сенсоров"
        ),
        PhenomenologicalType.VALIDATED_UAS_LIKE: (
            "Валидированная БПЛА-подобная сигнатура"
        ),
        PhenomenologicalType.UNKNOWN_ACTIVITY: "Неподтверждённая активность",
    }[probable_type]


def _operator_explanation(
    stage: ConfirmationStage,
    probable_type: PhenomenologicalType,
    attribution: tuple[TargetSourceAttribution, ...],
    direction: DirectionEstimate | None,
) -> str:
    source_count = len(attribution)
    confirmation_count = sum(item.independent_confirmation for item in attribution)
    type_text = {
        PhenomenologicalType.RF_ACTIVITY: "наблюдаемая RF-активность",
        PhenomenologicalType.HANDHELD_RADIO_LIKE: "радиосвязная сигнатура",
        PhenomenologicalType.VIDEO_LINK_LIKE: "сигнатура канала передачи видео",
        PhenomenologicalType.ACOUSTIC_ACTIVITY: "акустическая аномалия",
        PhenomenologicalType.MULTISENSOR_ACTIVITY: "согласованная активность",
        PhenomenologicalType.VALIDATED_UAS_LIKE: (
            "валидированная БПЛА-подобная сигнатура"
        ),
        PhenomenologicalType.UNKNOWN_ACTIVITY: "неопределённая активность",
    }[probable_type]
    direction_text = (
        " Свежий валидированный сектор доступен."
        if direction is not None
        else " Свежего валидированного направления нет."
    )
    return (
        f"Сформирована {type_text}; участвуют сенсоры: {source_count}, "
        f"независимых текущих подтверждений: {confirmation_count}. "
        f"Стадия: {_stage_label(stage)}."
        + direction_text
    )


def _stage_label(stage: ConfirmationStage) -> str:
    return {
        ConfirmationStage.BACKGROUND: "фон",
        ConfirmationStage.SUSPICIOUS_ACTIVITY: "подозрительная активность",
        ConfirmationStage.LIKELY_SOURCE: "вероятный источник",
        ConfirmationStage.LIKELY_TARGET: "вероятная цель",
        ConfirmationStage.CONFIRMED_TARGET: "подтверждённая цель",
    }[stage]


def _evidence_strength(
    attribution: tuple[TargetSourceAttribution, ...],
    live: bool,
) -> ConfidenceScore:
    if not live or not attribution:
        return ConfidenceScore.unavailable(
            "Свежих действующих признаков нет; числовая сила не рассчитывается."
        )
    strengths = sorted(
        (item.contribution for item in attribution),
        reverse=True,
    )
    primary = strengths[0]
    corroboration = (
        sum(strengths[1:3]) / len(strengths[1:3])
        if len(strengths) > 1
        else 0.0
    )
    score = min(1.0, 0.75 * primary + 0.25 * corroboration)
    return ConfidenceScore.heuristic(
        score,
        (
            "Эвристическая сила свежих вкладов независимых потоков; "
            "повторения одного сенсора не суммируются и это не вероятность."
        ),
    )


def _target_evidence(
    events: tuple[NormalizedEvent, ...],
    *,
    maximum: int = 32,
) -> tuple[EvidenceFact, ...]:
    items: list[EvidenceFact] = []
    seen: set[tuple[str, str | None, object, str | None, str]] = set()
    for event in events:
        for fact in event.evidence:
            key = (
                fact.code,
                fact.source_id,
                fact.measured,
                fact.unit,
                fact.explanation_ru,
            )
            if key in seen:
                continue
            seen.add(key)
            items.append(fact)
            if len(items) >= maximum:
                return tuple(items)
    return tuple(items)


def _limitations(
    events: tuple[NormalizedEvent, ...],
    *,
    lifecycle: TargetLifecycle,
    direction: DirectionEstimate | None,
    direction_conflict: bool,
    classification_conflict: bool,
) -> tuple[str, ...]:
    values = [
        limitation
        for event in events
        for limitation in event.limitations
    ]
    if direction_conflict:
        values.append(
            "Свежие пеленги противоречат друг другу; направление скрыто до подтверждения."
        )
    elif direction is None:
        values.append(
            "Свежего валидированного пеленга нет; направление не определяется."
        )
    else:
        values.append("Пеленг определяет сектор, но не дальность до источника.")
    if classification_conflict:
        values.append(
            "Свежие классификации одного уровня противоречат друг другу; "
            "тип источника скрыт до разрешения конфликта."
        )
    if lifecycle is TargetLifecycle.HOLDING:
        values.append(
            "Свежий признак временно отсутствует; цель удерживается гистерезисом."
        )
    elif lifecycle is TargetLifecycle.STALE:
        values.append("Цель устарела и исключена из списка активных.")
    elif lifecycle is TargetLifecycle.TOMBSTONED:
        values.append("Цель завершена и сохранена как ограниченная tombstone-запись.")
    return tuple(dict.fromkeys(values))


def _select_direction(
    events: tuple[NormalizedEvent, ...],
    now: datetime,
    minimum_confidence: float,
) -> tuple[DirectionEstimate | None, bool]:
    candidates = tuple(
        event.direction
        for event in events
        if _direction_rejection_reason(event, now, minimum_confidence) is None
        and event.direction is not None
    )
    if not candidates:
        return None, False
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            tolerance = first.uncertainty_deg + second.uncertainty_deg
            if _circular_distance(first.bearing_deg, second.bearing_deg) > tolerance:
                return None, True
    return max(candidates, key=lambda item: (item.confidence, item.observed_at)), False


def _direction_rejection_reason(
    event: NormalizedEvent,
    now: datetime,
    minimum_confidence: float,
) -> str | None:
    direction = event.direction
    if direction is None:
        return "TARGET.DIRECTION_MEASUREMENT_REQUIRED"
    if not direction.is_fresh_at(now):
        return "TARGET.DIRECTION_NOT_FRESH"
    if direction.confidence < minimum_confidence:
        return "TARGET.DIRECTION_QUALITY_BELOW_THRESHOLD"
    if not any(
        source.sensor_kind is SensorKind.DIRECTION_FINDER
        and source.sensor_id == direction.source_id
        for source in event.sources
    ):
        return "TARGET.DIRECTION_SOURCE_ATTRIBUTION_REQUIRED"
    return None


def _select_stage_type(
    events: tuple[NormalizedEvent, ...],
) -> tuple[NormalizedEvent | None, bool]:
    classified = tuple(
        event for event in events if event.event_type in _CLASSIFICATION_TYPES
    )
    classified_types = {_type_for_event(event) for event in classified}
    if len(classified_types) > 1:
        return None, True
    pool = classified or events
    return (
        max(
            pool,
            key=lambda item: (
                _TYPE_PRIORITY.get(item.event_type, 0),
                item.confidence.value if item.confidence.value is not None else -1.0,
                item.observed_at,
                item.event_id,
            ),
        ),
        False,
    )


def _frequency_compatible(
    track: _Track,
    event: NormalizedEvent,
    tolerance_hz: float,
) -> bool:
    frequencies = tuple(
        prior
        for prior in track.events.values()
        if prior.frequency_hz is not None
    )
    if event.frequency_hz is None or not frequencies:
        return True
    return any(_frequency_pair_compatible(prior, event, tolerance_hz) for prior in frequencies)


def _has_compatible_frequency(track: _Track, event: NormalizedEvent) -> bool:
    if event.frequency_hz is None:
        return False
    return any(
        prior.frequency_hz is not None
        and _frequency_pair_compatible(prior, event, 0.0)
        for prior in track.events.values()
    )


def _frequency_pair_compatible(
    first: NormalizedEvent,
    second: NormalizedEvent,
    tolerance_hz: float,
) -> bool:
    if first.frequency_hz is None or second.frequency_hz is None:
        return False
    first_half = (first.bandwidth_hz or 0.0) / 2.0
    second_half = (second.bandwidth_hz or 0.0) / 2.0
    allowed = max(tolerance_hz, first_half + second_half)
    return abs(first.frequency_hz - second.frequency_hz) <= allowed


def _direction_compatible(
    track: _Track,
    event: NormalizedEvent,
    tolerance_deg: float,
) -> bool:
    if event.direction is None:
        return True
    prior = tuple(
        item.direction
        for item in track.events.values()
        if item.direction is not None
    )
    if not prior:
        return True
    return any(
        _circular_distance(item.bearing_deg, event.direction.bearing_deg)
        <= item.uncertainty_deg + event.direction.uncertainty_deg + tolerance_deg
        for item in prior
    )


def _has_compatible_direction(track: _Track, event: NormalizedEvent) -> bool:
    if event.direction is None:
        return False
    return any(
        item.direction is not None
        and _circular_distance(
            item.direction.bearing_deg,
            event.direction.bearing_deg,
        )
        <= item.direction.uncertainty_deg + event.direction.uncertainty_deg
        for item in track.events.values()
    )


def _circular_distance(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


def _source_ids(track: _Track) -> set[str]:
    return {
        source.sensor_id
        for event in track.events.values()
        for source in event.sources
    }


def _observation_ids(track: _Track) -> set[str]:
    return {
        source.observation_id
        for event in track.events.values()
        for source in event.sources
        if source.observation_id is not None
    }


def _episode_ids(track: _Track) -> set[str]:
    return {
        event.episode_id
        for event in track.events.values()
        if event.episode_id is not None
    }


def _target_id(semantic_key: str) -> str:
    digest = hashlib.sha256(f"target|{semantic_key}".encode()).hexdigest()[:20]
    return f"target-{digest}"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "TargetAggregator",
    "TargetAggregatorConfig",
    "TargetInputError",
    "time_decay",
]
