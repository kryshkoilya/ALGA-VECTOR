"""Deterministic temporal correlation of normalized sensor observations."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from .models import (
    EvidenceStrength,
    FusionClassification,
    FusionConfig,
    FusionContribution,
    FusionDecision,
    FusionEvidence,
    FusionInputError,
    FusionLifecycle,
    FusionObservation,
    FusionTransition,
    FusionTransitionKind,
    FusionUpdate,
    SensorModality,
    _require_aware,
)

_ACTIVITY_MODALITIES = frozenset(
    {SensorModality.RF, SensorModality.ACOUSTIC}
)
_CONTEXT_MODALITIES = frozenset(
    {SensorModality.DIRECTION, SensorModality.CIVIL_ADSB}
)

_StreamKey = tuple[SensorModality, str]
_ObservationSignature = tuple[
    datetime,
    float,
    float,
    str,
    tuple[str, ...],
    bool,
]


@dataclass(slots=True)
class _Episode:
    episode_id: str
    started_at: datetime
    lifecycle: FusionLifecycle
    last_active_at: datetime
    classification: FusionClassification
    debounced: bool = False


@dataclass(frozen=True, slots=True)
class _Correlation:
    confirmed: bool
    observations: tuple[FusionObservation, ...]
    source_ids: tuple[str, ...]
    latest_support_at: datetime | None
    shared_evidence_keys: tuple[str, ...]


class SensorFusionEngine:
    """Conservative state machine over a single normalized temporal stream.

    Only sustained, independent RF and acoustic observations can create an
    alertable ``multi_sensor_correlated`` result.  Direction and civil ADS-B
    observations are context-only and never advance the confirmation count.

    Input timestamps must be globally non-decreasing.  This keeps lifecycle,
    deduplication, and transition identifiers deterministic.
    """

    def __init__(self, config: FusionConfig | None = None) -> None:
        self.config = config or FusionConfig()
        self._history: deque[FusionObservation] = deque(
            maxlen=self.config.maximum_history
        )
        self._latest: OrderedDict[_StreamKey, FusionObservation] = OrderedDict()
        self._last_timestamp_by_stream: dict[_StreamKey, datetime] = {}
        self._last_signature_by_stream: dict[
            _StreamKey, _ObservationSignature
        ] = {}
        self._episode: _Episode | None = None
        self._last_evaluated_at: datetime | None = None
        self._last_confirmation_at: datetime | None = None
        self._episode_sequence = 0
        self._transition_sequence = 0
        self._lock = RLock()

    @property
    def tracked_stream_count(self) -> int:
        with self._lock:
            return len(self._latest)

    def reset(self) -> None:
        """Discard all temporal state and deterministic sequence counters."""

        with self._lock:
            self._history.clear()
            self._latest.clear()
            self._last_timestamp_by_stream.clear()
            self._last_signature_by_stream.clear()
            self._episode = None
            self._last_evaluated_at = None
            self._last_confirmation_at = None
            self._episode_sequence = 0
            self._transition_sequence = 0

    def process(self, observation: FusionObservation) -> FusionUpdate:
        """Advance the state machine with one normalized observation."""

        if not isinstance(observation, FusionObservation):
            raise FusionInputError("expected FusionObservation")
        with self._lock:
            self._validate_order(observation)
            stream = (observation.modality, observation.source_id)
            signature = _signature(observation)
            previous_timestamp = self._last_timestamp_by_stream.get(stream)
            if previous_timestamp == observation.timestamp:
                if self._last_signature_by_stream.get(stream) == signature:
                    return FusionUpdate(
                        decision=self._build_decision(
                            observation.timestamp,
                            debounced=False,
                        )
                    )
                raise FusionInputError(
                    "one sensor stream cannot publish different observations "
                    "at the same timestamp"
                )

            if (
                self._episode is not None
                and self._episode.lifecycle is FusionLifecycle.RESOLVED
            ):
                self._begin_after_resolution()

            self._last_timestamp_by_stream[stream] = observation.timestamp
            self._last_signature_by_stream[stream] = signature
            self._history.append(observation)
            self._remember_latest(stream, observation)
            self._last_evaluated_at = observation.timestamp
            return self._evaluate(
                observation.timestamp,
                allow_confirmation=(
                    observation.modality in _ACTIVITY_MODALITIES
                    and observation.quality >= self.config.minimum_quality
                    and observation.strength >= self.config.attack_strength
                ),
            )

    def ingest(self, observation: FusionObservation) -> FusionUpdate:
        """Alias used by stream-oriented integrations."""

        return self.process(observation)

    def tick(self, now: datetime) -> FusionUpdate:
        """Advance freshness and release state without adding an observation."""

        _require_aware(now, "now")
        with self._lock:
            if self._last_evaluated_at is not None and now < self._last_evaluated_at:
                raise FusionInputError("evaluation time regressed")
            self._last_evaluated_at = now
            return self._evaluate(now, allow_confirmation=False)

    def _validate_order(self, observation: FusionObservation) -> None:
        if (
            self._last_evaluated_at is not None
            and observation.timestamp < self._last_evaluated_at
        ):
            raise FusionInputError("observation time regressed")

    def _remember_latest(
        self,
        stream: _StreamKey,
        observation: FusionObservation,
    ) -> None:
        self._latest.pop(stream, None)
        self._latest[stream] = observation
        while len(self._latest) > self.config.maximum_streams:
            evicted_stream, _ = self._latest.popitem(last=False)
            self._last_timestamp_by_stream.pop(evicted_stream, None)
            self._last_signature_by_stream.pop(evicted_stream, None)

    def _begin_after_resolution(self) -> None:
        self._episode = None
        self._history.clear()
        self._latest.clear()

    def _evaluate(
        self,
        now: datetime,
        *,
        allow_confirmation: bool,
    ) -> FusionUpdate:
        self._prune(now)
        attack_observations = self._activity_observations(
            now,
            minimum_strength=self.config.attack_strength,
            require_quality=False,
        )
        qualified_attack = tuple(
            item
            for item in attack_observations
            if item.quality >= self.config.minimum_quality
        )
        correlation = self._correlation(qualified_attack)
        attack_current = self._latest_activity(
            now,
            minimum_strength=self.config.attack_strength,
            require_quality=True,
        )
        attack_current_correlation = self._correlation(
            attack_current,
            require_observation_count=False,
            require_dwell=False,
        )
        release_observations = self._latest_activity(
            now,
            minimum_strength=self.config.release_strength,
            require_quality=True,
        )
        release_correlation = self._correlation(
            release_observations,
            require_observation_count=False,
            require_dwell=False,
        )

        if self._episode is None and attack_observations:
            self._episode = self._new_episode(attack_observations)

        episode = self._episode
        transition: FusionTransition | None = None
        debounced = False

        if episode is not None and episode.lifecycle is FusionLifecycle.CANDIDATE:
            if attack_observations:
                episode.last_active_at = max(
                    item.timestamp for item in attack_observations
                )
                episode.classification = _candidate_classification(
                    attack_observations
                )
            if (
                correlation.confirmed
                and attack_current_correlation.confirmed
                and allow_confirmation
            ):
                episode.lifecycle = FusionLifecycle.CONFIRMED
                episode.classification = (
                    FusionClassification.MULTI_SENSOR_CORRELATED
                )
                episode.last_active_at = (
                    correlation.latest_support_at or now
                )
                debounced = self._is_debounced(now)
                episode.debounced = debounced
                self._last_confirmation_at = now
                if not debounced:
                    transition = self._transition(
                        episode,
                        FusionTransitionKind.CONFIRMED,
                        now,
                    )
            elif not attack_observations and (
                now - episode.last_active_at
            ).total_seconds() > self.config.candidate_timeout_seconds:
                episode.lifecycle = FusionLifecycle.RESOLVED
                episode.classification = FusionClassification.BACKGROUND

        elif episode is not None and episode.lifecycle in {
            FusionLifecycle.CONFIRMED,
            FusionLifecycle.HOLDING,
        }:
            if correlation.confirmed and attack_current_correlation.confirmed:
                episode.lifecycle = FusionLifecycle.CONFIRMED
                episode.last_active_at = (
                    correlation.latest_support_at or episode.last_active_at
                )
            elif release_correlation.confirmed:
                episode.lifecycle = FusionLifecycle.HOLDING
                episode.last_active_at = (
                    release_correlation.latest_support_at
                    or episode.last_active_at
                )
            elif (
                now - episode.last_active_at
            ).total_seconds() <= self.config.release_hold_seconds:
                episode.lifecycle = FusionLifecycle.HOLDING
            else:
                episode.lifecycle = FusionLifecycle.RESOLVED
                episode.classification = FusionClassification.BACKGROUND
                transition = self._transition(
                    episode,
                    FusionTransitionKind.RESOLVED,
                    now,
                )

        decision = self._build_decision(now, debounced=debounced)
        return FusionUpdate(decision=decision, transition=transition)

    def _new_episode(
        self,
        observations: tuple[FusionObservation, ...],
    ) -> _Episode:
        self._episode_sequence += 1
        started_at = min(item.timestamp for item in observations)
        return _Episode(
            episode_id=f"fusion-{self._episode_sequence:06d}",
            started_at=started_at,
            lifecycle=FusionLifecycle.CANDIDATE,
            last_active_at=max(item.timestamp for item in observations),
            classification=_candidate_classification(observations),
        )

    def _transition(
        self,
        episode: _Episode,
        kind: FusionTransitionKind,
        now: datetime,
    ) -> FusionTransition:
        self._transition_sequence += 1
        return FusionTransition(
            transition_id=f"fusion-transition-{self._transition_sequence:06d}",
            episode_id=episode.episode_id,
            kind=kind,
            occurred_at=now,
            classification=episode.classification,
            reason_code=(
                "FUSION.MULTI_SENSOR_CONFIRMED"
                if kind is FusionTransitionKind.CONFIRMED
                else "FUSION.CORRELATION_RESOLVED"
            ),
            explanation=(
                "Independent RF and acoustic observations remained correlated."
                if kind is FusionTransitionKind.CONFIRMED
                else "The correlated activity no longer met release criteria."
            ),
        )

    def _is_debounced(self, now: datetime) -> bool:
        previous = self._last_confirmation_at
        if previous is None:
            return False
        return (
            now - previous
        ).total_seconds() < self.config.debounce_seconds

    def _prune(self, now: datetime) -> None:
        retention = max(
            self.config.temporal_window_seconds,
            self.config.direction_freshness_seconds,
            self.config.civil_adsb_context_seconds,
            self.config.candidate_timeout_seconds,
            self.config.release_hold_seconds,
        )
        while (
            self._history
            and (now - self._history[0].timestamp).total_seconds() > retention
        ):
            self._history.popleft()
        expired = [
            stream
            for stream, observation in self._latest.items()
            if (now - observation.timestamp).total_seconds() > retention
        ]
        for stream in expired:
            self._latest.pop(stream, None)

    def _activity_observations(
        self,
        now: datetime,
        *,
        minimum_strength: float,
        require_quality: bool,
    ) -> tuple[FusionObservation, ...]:
        return tuple(
            observation
            for observation in self._history
            if observation.modality in _ACTIVITY_MODALITIES
            and 0.0
            <= (now - observation.timestamp).total_seconds()
            <= self.config.temporal_window_seconds
            and observation.strength >= minimum_strength
            and (
                not require_quality
                or observation.quality >= self.config.minimum_quality
            )
        )

    def _latest_activity(
        self,
        now: datetime,
        *,
        minimum_strength: float,
        require_quality: bool,
    ) -> tuple[FusionObservation, ...]:
        return tuple(
            observation
            for observation in self._latest.values()
            if observation.modality in _ACTIVITY_MODALITIES
            and 0.0
            <= (now - observation.timestamp).total_seconds()
            <= self.config.temporal_window_seconds
            and observation.strength >= minimum_strength
            and (
                not require_quality
                or observation.quality >= self.config.minimum_quality
            )
        )

    def _correlation(
        self,
        observations: tuple[FusionObservation, ...],
        *,
        require_observation_count: bool = True,
        require_dwell: bool = True,
    ) -> _Correlation:
        rf = tuple(
            item for item in observations if item.modality is SensorModality.RF
        )
        acoustic = tuple(
            item
            for item in observations
            if item.modality is SensorModality.ACOUSTIC
        )
        independent_pairs = tuple(
            (rf_item, acoustic_item)
            for rf_item in rf
            for acoustic_item in acoustic
            if rf_item.source_id != acoustic_item.source_id
        )
        count_ok = (
            len(observations) >= self.config.minimum_observations
            if require_observation_count
            else bool(observations)
        )
        if observations:
            earliest = min(item.timestamp for item in observations)
            latest = max(item.timestamp for item in observations)
            dwell = (latest - earliest).total_seconds()
        else:
            latest = None
            dwell = 0.0
        dwell_ok = (
            dwell >= self.config.minimum_correlation_dwell_seconds
            if require_dwell
            else True
        )
        confirmed = bool(independent_pairs) and count_ok and dwell_ok
        source_ids = tuple(
            sorted({item.source_id for item in observations})
        )
        shared_keys: set[str] = set()
        if rf and acoustic:
            rf_keys = {
                key for observation in rf for key in observation.evidence_keys
            }
            acoustic_keys = {
                key
                for observation in acoustic
                for key in observation.evidence_keys
            }
            shared_keys = rf_keys & acoustic_keys
        return _Correlation(
            confirmed=confirmed,
            observations=observations,
            source_ids=source_ids,
            latest_support_at=latest if confirmed else None,
            shared_evidence_keys=tuple(sorted(shared_keys)),
        )

    def _build_decision(
        self,
        now: datetime,
        *,
        debounced: bool,
    ) -> FusionDecision:
        attack = self._activity_observations(
            now,
            minimum_strength=self.config.attack_strength,
            require_quality=False,
        )
        qualified = tuple(
            item
            for item in attack
            if item.quality >= self.config.minimum_quality
        )
        correlation = self._correlation(qualified)
        direction_context = self._direction_context(now)
        adsb_context = self._civil_adsb_context(now)
        episode = self._episode

        if episode is not None:
            lifecycle = episode.lifecycle
            episode_id: str | None = episode.episode_id
            started_at: datetime | None = episode.started_at
            last_active_at: datetime | None = episode.last_active_at
            if lifecycle in {
                FusionLifecycle.CONFIRMED,
                FusionLifecycle.HOLDING,
            }:
                classification = FusionClassification.MULTI_SENSOR_CORRELATED
            elif lifecycle is FusionLifecycle.RESOLVED:
                classification = FusionClassification.BACKGROUND
            else:
                classification = (
                    _candidate_classification(attack)
                    if attack
                    else episode.classification
                )
        elif adsb_context:
            lifecycle = FusionLifecycle.INFORMATIONAL
            classification = (
                FusionClassification.NEARBY_COOPERATIVE_AIRCRAFT_CONTEXT
            )
            episode_id = None
            started_at = None
            last_active_at = None
        else:
            lifecycle = FusionLifecycle.IDLE
            classification = FusionClassification.BACKGROUND
            episode_id = None
            started_at = None
            last_active_at = None

        evidence, contradictions, missing, limitations = self._explain(
            now,
            attack,
            qualified,
            correlation,
            direction_context,
            adsb_context,
            classification,
        )
        strength = _evidence_strength(
            lifecycle,
            attack_count=len(attack),
            qualified_count=len(qualified),
            has_adsb_context=bool(adsb_context),
        )
        active_modalities = tuple(
            sorted(
                {item.modality for item in attack},
                key=lambda item: item.value,
            )
        )
        active_source_ids = tuple(
            sorted({item.source_id for item in attack})
        )
        return FusionDecision(
            evaluated_at=now,
            classification=classification,
            lifecycle=lifecycle,
            evidence_strength=strength,
            alertable=(
                classification
                is FusionClassification.MULTI_SENSOR_CORRELATED
                and lifecycle
                in {FusionLifecycle.CONFIRMED, FusionLifecycle.HOLDING}
            ),
            episode_id=episode_id,
            started_at=started_at,
            last_active_at=last_active_at,
            observation_count=len(attack),
            active_modalities=active_modalities,
            active_source_ids=active_source_ids,
            evidence=evidence,
            contradictions=contradictions,
            missing=missing,
            limitations=limitations,
            contributions=self._contributions(
                now,
                attack,
                direction_context,
                adsb_context,
                correlation,
            ),
            debounced=debounced or bool(episode and episode.debounced),
        )

    def _direction_context(
        self,
        now: datetime,
    ) -> tuple[FusionObservation, ...]:
        return tuple(
            observation
            for observation in self._history
            if observation.modality is SensorModality.DIRECTION
            and observation.validated
            and observation.quality >= self.config.minimum_quality
            and observation.strength >= self.config.release_strength
            and 0.0
            <= (now - observation.timestamp).total_seconds()
            <= self.config.direction_freshness_seconds
        )

    def _civil_adsb_context(
        self,
        now: datetime,
    ) -> tuple[FusionObservation, ...]:
        return tuple(
            observation
            for observation in self._history
            if observation.modality is SensorModality.CIVIL_ADSB
            and observation.quality >= self.config.minimum_quality
            and observation.strength >= self.config.release_strength
            and 0.0
            <= (now - observation.timestamp).total_seconds()
            <= self.config.civil_adsb_context_seconds
        )

    def _explain(
        self,
        now: datetime,
        attack: tuple[FusionObservation, ...],
        qualified: tuple[FusionObservation, ...],
        correlation: _Correlation,
        direction_context: tuple[FusionObservation, ...],
        adsb_context: tuple[FusionObservation, ...],
        classification: FusionClassification,
    ) -> tuple[
        tuple[FusionEvidence, ...],
        tuple[FusionEvidence, ...],
        tuple[FusionEvidence, ...],
        tuple[FusionEvidence, ...],
    ]:
        evidence: list[FusionEvidence] = []
        contradictions: list[FusionEvidence] = []
        missing: list[FusionEvidence] = []
        limitations: list[FusionEvidence] = [
            FusionEvidence(
                code="FUSION.HEURISTIC_EVIDENCE",
                explanation=(
                    "Evidence strength is heuristic and is not a calibrated "
                    "probability."
                ),
            ),
            FusionEvidence(
                code="FUSION.GENERIC_ACTIVITY_ONLY",
                explanation=(
                    "The result describes sensor activity only and does not "
                    "establish physical identity or intent."
                ),
            ),
        ]

        if attack:
            evidence.append(
                FusionEvidence(
                    code="FUSION.ACTIVE_OBSERVATIONS",
                    explanation=(
                        "Recent RF or acoustic observations crossed the entry "
                        "strength threshold."
                    ),
                    modalities=tuple(
                        sorted(
                            {item.modality for item in attack},
                            key=lambda item: item.value,
                        )
                    ),
                    source_ids=tuple(
                        sorted({item.source_id for item in attack})
                    ),
                    evidence_keys=_all_evidence_keys(attack),
                    measured=len(attack),
                    threshold=self.config.minimum_observations,
                )
            )
        low_quality = tuple(
            item
            for item in attack
            if item.quality < self.config.minimum_quality
        )
        if low_quality:
            contradictions.append(
                FusionEvidence(
                    code="FUSION.LOW_QUALITY_ACTIVITY",
                    explanation=(
                        "Some active observations did not pass the quality "
                        "threshold."
                    ),
                    modalities=tuple(
                        sorted(
                            {item.modality for item in low_quality},
                            key=lambda item: item.value,
                        )
                    ),
                    source_ids=tuple(
                        sorted({item.source_id for item in low_quality})
                    ),
                    measured=min(item.quality for item in low_quality),
                    threshold=self.config.minimum_quality,
                )
            )

        if correlation.confirmed:
            evidence.append(
                FusionEvidence(
                    code="FUSION.RF_ACOUSTIC_CORRELATED",
                    explanation=(
                        "Independent RF and acoustic observations met count, "
                        "quality, time-window, and dwell requirements."
                    ),
                    modalities=(
                        SensorModality.RF,
                        SensorModality.ACOUSTIC,
                    ),
                    source_ids=correlation.source_ids,
                    evidence_keys=correlation.shared_evidence_keys,
                    measured=len(correlation.observations),
                    threshold=self.config.minimum_observations,
                    confirming=True,
                )
            )
            if not correlation.shared_evidence_keys:
                limitations.append(
                    FusionEvidence(
                        code="FUSION.TIME_ONLY_CORRELATION",
                        explanation=(
                            "The modalities were correlated by time and "
                            "persistence; no shared evidence key was supplied."
                        ),
                    )
                )
        elif attack:
            if len(qualified) < self.config.minimum_observations:
                missing.append(
                    FusionEvidence(
                        code="FUSION.MORE_OBSERVATIONS_REQUIRED",
                        explanation=(
                            "More quality observations are required before "
                            "multi-sensor confirmation."
                        ),
                        measured=len(qualified),
                        threshold=self.config.minimum_observations,
                    )
                )
            modalities = {item.modality for item in qualified}
            if SensorModality.RF not in modalities:
                missing.append(
                    FusionEvidence(
                        code="FUSION.RF_CONFIRMATION_MISSING",
                        explanation=(
                            "No quality RF observation supports the temporal "
                            "candidate."
                        ),
                    )
                )
            if SensorModality.ACOUSTIC not in modalities:
                missing.append(
                    FusionEvidence(
                        code="FUSION.ACOUSTIC_CONFIRMATION_MISSING",
                        explanation=(
                            "No quality acoustic observation supports the "
                            "temporal candidate."
                        ),
                    )
                )
            if {
                SensorModality.RF,
                SensorModality.ACOUSTIC,
            }.issubset(modalities) and not _has_independent_sources(qualified):
                contradictions.append(
                    FusionEvidence(
                        code="FUSION.INDEPENDENCE_NOT_ESTABLISHED",
                        explanation=(
                            "RF and acoustic observations use the same "
                            "source identifier, so independence is not "
                            "established."
                        ),
                    )
                )

        if direction_context:
            evidence.append(
                FusionEvidence(
                    code="FUSION.DIRECTION_CONTEXT",
                    explanation=(
                        "Fresh validated direction context is available; it "
                        "does not contribute to confirmation."
                    ),
                    modalities=(SensorModality.DIRECTION,),
                    source_ids=tuple(
                        sorted({item.source_id for item in direction_context})
                    ),
                    evidence_keys=_all_evidence_keys(direction_context),
                    confirming=False,
                )
            )
        else:
            direction_observations = tuple(
                item
                for item in self._history
                if item.modality is SensorModality.DIRECTION
            )
            if direction_observations:
                latest_direction = direction_observations[-1]
                age = (now - latest_direction.timestamp).total_seconds()
                if not latest_direction.validated:
                    contradictions.append(
                        FusionEvidence(
                            code="FUSION.DIRECTION_NOT_VALIDATED",
                            explanation=(
                                "Direction context was ignored because it was "
                                "not explicitly validated."
                            ),
                            modalities=(SensorModality.DIRECTION,),
                            source_ids=(latest_direction.source_id,),
                        )
                    )
                elif age > self.config.direction_freshness_seconds:
                    contradictions.append(
                        FusionEvidence(
                            code="FUSION.DIRECTION_STALE",
                            explanation=(
                                "Direction context was ignored because it was "
                                "older than the freshness limit."
                            ),
                            modalities=(SensorModality.DIRECTION,),
                            source_ids=(latest_direction.source_id,),
                            measured=age,
                            threshold=self.config.direction_freshness_seconds,
                        )
                    )

        if adsb_context:
            evidence.append(
                FusionEvidence(
                    code="FUSION.CIVIL_ADSB_CONTEXT",
                    explanation=(
                        "Recent cooperative civil ADS-B context is present; it "
                        "does not contribute to anomaly confirmation."
                    ),
                    modalities=(SensorModality.CIVIL_ADSB,),
                    source_ids=tuple(
                        sorted({item.source_id for item in adsb_context})
                    ),
                    evidence_keys=_all_evidence_keys(adsb_context),
                    confirming=False,
                )
            )
            limitations.append(
                FusionEvidence(
                    code="FUSION.CIVIL_ADSB_NOT_IFF",
                    explanation=(
                        "Civil ADS-B is cooperative context only; it is not "
                        "IFF and cannot confirm an unassociated anomaly."
                    ),
                )
            )

        if (
            classification
            is FusionClassification.NEARBY_COOPERATIVE_AIRCRAFT_CONTEXT
        ):
            limitations.append(
                FusionEvidence(
                    code="FUSION.CONTEXT_NOT_ASSOCIATION",
                    explanation=(
                        "The cooperative context is not associated with any "
                        "other observation by this core."
                    ),
                )
            )

        return (
            tuple(evidence),
            tuple(contradictions),
            tuple(missing),
            tuple(limitations),
        )

    def _contributions(
        self,
        now: datetime,
        attack: tuple[FusionObservation, ...],
        direction_context: tuple[FusionObservation, ...],
        adsb_context: tuple[FusionObservation, ...],
        correlation: _Correlation,
    ) -> tuple[FusionContribution, ...]:
        relevant = (
            attack
            + direction_context
            + adsb_context
        )
        grouped: dict[_StreamKey, list[FusionObservation]] = defaultdict(list)
        for observation in relevant:
            grouped[(observation.modality, observation.source_id)].append(
                observation
            )
        confirming_streams = {
            (observation.modality, observation.source_id)
            for observation in correlation.observations
        }
        contributions = [
            FusionContribution(
                modality=modality,
                source_id=source_id,
                observation_count=len(observations),
                mean_quality=sum(item.quality for item in observations)
                / len(observations),
                mean_strength=sum(item.strength for item in observations)
                / len(observations),
                confirming=(
                    correlation.confirmed
                    and (modality, source_id) in confirming_streams
                ),
                context_only=modality in _CONTEXT_MODALITIES,
            )
            for (modality, source_id), observations in grouped.items()
        ]
        return tuple(
            sorted(
                contributions,
                key=lambda item: (item.modality.value, item.source_id),
            )
        )


def _signature(observation: FusionObservation) -> _ObservationSignature:
    return (
        observation.timestamp,
        observation.quality,
        observation.strength,
        observation.summary,
        observation.evidence_keys,
        observation.validated,
    )


def _candidate_classification(
    observations: tuple[FusionObservation, ...],
) -> FusionClassification:
    modalities = {item.modality for item in observations}
    if modalities == {SensorModality.RF}:
        return FusionClassification.RF_ACTIVITY
    if modalities == {SensorModality.ACOUSTIC}:
        return FusionClassification.ACOUSTIC_ANOMALY
    if modalities & _ACTIVITY_MODALITIES:
        return FusionClassification.UNCONFIRMED_ANOMALY
    return FusionClassification.BACKGROUND


def _has_independent_sources(
    observations: tuple[FusionObservation, ...],
) -> bool:
    rf_sources = {
        item.source_id
        for item in observations
        if item.modality is SensorModality.RF
    }
    acoustic_sources = {
        item.source_id
        for item in observations
        if item.modality is SensorModality.ACOUSTIC
    }
    return any(
        rf_source != acoustic_source
        for rf_source in rf_sources
        for acoustic_source in acoustic_sources
    )


def _all_evidence_keys(
    observations: tuple[FusionObservation, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                key
                for observation in observations
                for key in observation.evidence_keys
            }
        )
    )


def _evidence_strength(
    lifecycle: FusionLifecycle,
    *,
    attack_count: int,
    qualified_count: int,
    has_adsb_context: bool,
) -> EvidenceStrength:
    if lifecycle is FusionLifecycle.CONFIRMED:
        return EvidenceStrength.HIGH
    if lifecycle is FusionLifecycle.HOLDING:
        return EvidenceStrength.MEDIUM
    if lifecycle is FusionLifecycle.CANDIDATE:
        if qualified_count >= 2:
            return EvidenceStrength.MEDIUM
        return EvidenceStrength.LOW
    if has_adsb_context or attack_count:
        return EvidenceStrength.LOW
    return EvidenceStrength.NONE


FusionEngine = SensorFusionEngine


__all__ = ["FusionEngine", "SensorFusionEngine"]
