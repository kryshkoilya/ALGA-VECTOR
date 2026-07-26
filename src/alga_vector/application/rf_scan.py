"""Runtime helpers for capability-gated cyclic RF observation.

The scan session only schedules receiver tuning.  Detection remains based on
measured spectrum evidence and a per-window temporal pipeline; frequency alone
never becomes an emitter or object classification.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock

from alga_vector.devices import (
    CompiledScanPlan,
    ScanPlanCursor,
    ScanWindow,
)
from alga_vector.domain.models import SpectrumFrame
from alga_vector.signal_analysis import (
    AnalysisResult,
    DecisionUpdate,
    DetectorConfig,
    RfDecision,
    RfDecisionEngine,
    RfEventDetector,
    SourceObservationMetadata,
    TemporalDecisionConfig,
)


@dataclass(frozen=True, slots=True)
class ScanRuntimeStatus:
    """Immutable session state consumed structurally by the desktop UI."""

    active: bool
    plan_id: str
    profile_id: str
    source_id: str
    current_window_id: str
    current_window_label_ru: str
    current_ordinal: int
    window_count: int
    start_frequency_hz: int
    stop_frequency_hz: int
    center_frequency_hz: int
    span_hz: int
    successful_frames_in_window: int
    dwell_frames: int
    completed_windows: int
    completed_cycles: int
    failed_windows: int
    estimated_cycle_ms: int
    coverage_fraction: float
    sequential: bool
    limitation_codes: tuple[str, ...]
    limitations_ru: tuple[str, ...]
    observed_window_id: str | None
    observed_window_label_ru: str | None
    observed_ordinal: int | None
    observed_start_frequency_hz: int | None
    observed_stop_frequency_hz: int | None
    transition_pending: bool


class RfScanSession:
    """Thread-safe facade over one compiled cyclic plan."""

    def __init__(
        self,
        plan: CompiledScanPlan,
        *,
        source_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not plan.accepted:
            raise ValueError("scan session requires an accepted plan")
        if not source_id.strip():
            raise ValueError("scan session source_id must not be blank")
        self.plan = plan
        self.source_id = source_id
        self.cursor = ScanPlanCursor(plan, clock=clock)
        self._lock = RLock()
        self._last_observed_window: ScanWindow | None = None
        self._deferred_lookahead_outstanding = False
        self._force_current_request_until_accepted = False
        self._recovery_request_proposed = False
        self._hold_current_until_success = False
        self._trailing_completion_window: ScanWindow | None = None

    def next_window(self) -> ScanWindow:
        with self._lock:
            return self.cursor.next_window()

    def request_window(
        self,
        *,
        anticipate_deferred_completion: bool = False,
    ) -> ScanWindow:
        """Return the tuning for the next hardware request.

        A non-blocking manager returns one completed frame and submits the next
        request in the same poll.  On the last successful dwell frame the
        completed frame still belongs to the current window, while the request
        being submitted must already target the following window.  Local
        synchronous managers must never use this one-frame look-ahead.
        """

        with self._lock:
            current = self.cursor.next_window()
            if not anticipate_deferred_completion:
                self._deferred_lookahead_outstanding = False
                self._force_current_request_until_accepted = False
                self._recovery_request_proposed = False
                return current
            if self._hold_current_until_success:
                self._deferred_lookahead_outstanding = False
                self._recovery_request_proposed = False
                return current
            if self._force_current_request_until_accepted:
                # A speculative next-window request may already be pending.
                # Keep requesting the current window until the deferred
                # manager confirms that one recovery request was actually
                # accepted. A busy worker may need several non-blocking polls.
                self._deferred_lookahead_outstanding = False
                self._recovery_request_proposed = True
                return current
            self._recovery_request_proposed = False
            status = self.cursor.snapshot()
            if status.successful_frames_in_window + 1 < current.dwell_frames:
                return current
            next_ordinal = (status.current_ordinal + 1) % len(
                self.plan.windows
            )
            self._deferred_lookahead_outstanding = True
            return self.plan.windows[next_ordinal]

    def mark_request_accepted(self, accepted: bool) -> None:
        """Acknowledge whether the tuning proposed by ``request_window`` ran.

        This matters only during deferred recovery. Returning the recovery
        tuning from ``request_window`` is not enough: a non-blocking hardware
        worker can still be busy with the speculative request.
        """

        with self._lock:
            if self._recovery_request_proposed and accepted:
                self._force_current_request_until_accepted = False
            self._recovery_request_proposed = False

    def mark_warmup_discarded(self) -> None:
        """Keep requesting the current window until one measured frame passes.

        A deferred poll may already have submitted a speculative look-ahead
        while returning a warm-up frame.  Holding by *result* rather than only
        by request acceptance prevents an A/B retune oscillation.
        """

        with self._lock:
            self._deferred_lookahead_outstanding = False
            self._force_current_request_until_accepted = False
            self._recovery_request_proposed = False
            self._hold_current_until_success = True

    def mark_retune_accepted(self) -> None:
        """Prevent look-ahead until the newly accepted tuning yields data."""

        with self._lock:
            self._hold_current_until_success = True

    def consume_expected_trailing_frame(
        self,
        *,
        center_frequency_hz: int,
        span_hz: int,
    ) -> bool:
        """Consume one deferred completion left by a recovery hold."""

        with self._lock:
            trailing = self._trailing_completion_window
            if trailing is None:
                return False
            matches = (
                trailing.center_frequency_hz == center_frequency_hz
                and trailing.span_hz == span_hz
            )
            self._trailing_completion_window = None
            return matches

    def mark_result(
        self,
        success: bool,
        *,
        detail_code: str | None = None,
    ) -> None:
        with self._lock:
            observed_window = self.cursor.next_window()
            held_until_success = self._hold_current_until_success
            self.cursor.mark_result(success, detail_code=detail_code)
            if success:
                self._last_observed_window = observed_window
                advanced = (
                    self.cursor.next_window().window_id
                    != observed_window.window_id
                )
                if held_until_success and advanced:
                    self._trailing_completion_window = observed_window
                self._deferred_lookahead_outstanding = False
                self._force_current_request_until_accepted = False
                self._recovery_request_proposed = False
                self._hold_current_until_success = False
            elif self._deferred_lookahead_outstanding:
                self._deferred_lookahead_outstanding = False
                self._force_current_request_until_accepted = True

    def status(self) -> ScanRuntimeStatus:
        with self._lock:
            cursor = self.cursor.snapshot()
            window = self.plan.windows[cursor.current_ordinal]
            observed = self._last_observed_window
            return ScanRuntimeStatus(
                active=True,
                plan_id=self.plan.plan_id,
                profile_id=self.plan.profile_id,
                source_id=self.source_id,
                current_window_id=window.window_id,
                current_window_label_ru=window.label_ru,
                current_ordinal=cursor.current_ordinal,
                window_count=len(self.plan.windows),
                start_frequency_hz=window.start_frequency_hz,
                stop_frequency_hz=window.stop_frequency_hz,
                center_frequency_hz=window.center_frequency_hz,
                span_hz=window.span_hz,
                successful_frames_in_window=(
                    cursor.successful_frames_in_window
                ),
                dwell_frames=window.dwell_frames,
                completed_windows=cursor.completed_windows,
                completed_cycles=cursor.completed_cycles,
                failed_windows=cursor.failed_windows,
                estimated_cycle_ms=self.plan.estimated_cycle_ms,
                coverage_fraction=self.plan.coverage_fraction,
                sequential=self.plan.sequential,
                limitation_codes=tuple(
                    limitation.code for limitation in self.plan.limitations
                ),
                limitations_ru=tuple(
                    limitation.message_ru
                    for limitation in self.plan.limitations
                ),
                observed_window_id=(
                    observed.window_id if observed is not None else None
                ),
                observed_window_label_ru=(
                    observed.label_ru if observed is not None else None
                ),
                observed_ordinal=(
                    observed.ordinal if observed is not None else None
                ),
                observed_start_frequency_hz=(
                    observed.start_frequency_hz
                    if observed is not None
                    else None
                ),
                observed_stop_frequency_hz=(
                    observed.stop_frequency_hz
                    if observed is not None
                    else None
                ),
                transition_pending=(
                    observed is not None
                    and observed.window_id != window.window_id
                ),
            )


@dataclass(slots=True)
class _WindowPipeline:
    detector: RfEventDetector
    decision_engine: RfDecisionEngine
    local_sequence: int = 0
    latest_decision: RfDecision | None = None
    last_observed_at: datetime | None = None


class FrequencyScopedRfPipelinePool:
    """Keep independent baseline/temporal state for each tuning window.

    A single adaptive detector cannot safely learn alternating spectral grids.
    Each compiled window therefore receives its own bounded state while the
    original physical source id remains in operator-visible evidence.
    """

    def __init__(
        self,
        *,
        maximum_pipelines: int,
        detector_config: DetectorConfig | None = None,
        decision_config: TemporalDecisionConfig | None = None,
    ) -> None:
        if maximum_pipelines < 1:
            raise ValueError("maximum_pipelines must be positive")
        self._maximum_pipelines = maximum_pipelines
        self._detector_config = detector_config or DetectorConfig()
        self._decision_config = decision_config
        self._maximum_decision_age_ms = (
            self._detector_config.max_data_age_ms
        )
        temporal_config = decision_config or TemporalDecisionConfig()
        self._maximum_decision_gap_ms = min(
            self._maximum_decision_age_ms,
            round(
                temporal_config.maximum_observation_gap_seconds
                * 1_000
            ),
        )
        self._pipelines: dict[tuple[str, str], _WindowPipeline] = {}
        self._last_pipeline_key: tuple[str, str] | None = None

    @property
    def pipeline_count(self) -> int:
        return len(self._pipelines)

    def process(
        self,
        window: ScanWindow,
        frame: SpectrumFrame,
        metadata: SourceObservationMetadata,
    ) -> tuple[AnalysisResult, DecisionUpdate]:
        key = (frame.source_id, window.window_id)
        pipeline = self._pipelines.get(key)
        if pipeline is None:
            if len(self._pipelines) >= self._maximum_pipelines:
                raise RuntimeError("scan pipeline limit exhausted")
            pipeline = _WindowPipeline(
                detector=RfEventDetector(self._detector_config),
                decision_engine=RfDecisionEngine(self._decision_config),
            )
            self._pipelines[key] = pipeline
        if self._last_pipeline_key not in {None, key}:
            # Re-entering a window after observing another spectral grid must
            # require fresh temporal confirmation.  The detector baseline is
            # intentionally retained, but CONFIRMED/HOLDING episode state is
            # not continuous across an unobserved window.
            pipeline.decision_engine.reset(frame.source_id)
            pipeline.latest_decision = None
        if pipeline.last_observed_at is not None:
            revisit_gap_ms = (
                frame.captured_at - pipeline.last_observed_at
            ).total_seconds() * 1_000.0
            if revisit_gap_ms > self._maximum_decision_gap_ms:
                # A stale CONFIRMED/HOLDING FSM must not be revived by one
                # frame after a long sequential-scan absence.  Keep the
                # learned spectral floor, but require fresh temporal support.
                pipeline.decision_engine.reset(frame.source_id)
                pipeline.latest_decision = None
        pipeline.local_sequence += 1
        pipeline.detector.register_source_metadata(frame.source_id, metadata)
        # Global hardware sequences contain gaps while other scan windows are
        # measured.  The per-window sequence records only this window's
        # successful frames, so the quality gate does not mistake planned
        # retuning for dropped device data.
        scoped_frame = replace(frame, sequence=pipeline.local_sequence)
        internal_analysis = pipeline.detector.analyze(scoped_frame)
        update = pipeline.decision_engine.process(internal_analysis)
        # Local per-window sequence numbers exist only to prevent planned
        # retunes from looking like device packet loss.  Operator-visible
        # assessment/event correlation must retain the hardware sequence.
        analysis = replace(
            internal_analysis,
            sequence=frame.sequence,
            event=(
                replace(
                    internal_analysis.event,
                    sequence=frame.sequence,
                )
                if internal_analysis.event is not None
                else None
            ),
            assessment=replace(
                internal_analysis.assessment,
                sequence=frame.sequence,
            ),
        )
        pipeline.latest_decision = update.decision
        pipeline.last_observed_at = frame.captured_at
        self._last_pipeline_key = key
        return analysis, update

    def latest_alertable_decision(
        self,
        *,
        now: datetime,
        maximum_age_ms: int | None = None,
    ) -> RfDecision | None:
        """Return only a still-fresh alertable observation.

        A long sequential cycle must not keep presenting an old decision from
        a window that has not been revisited.  Freshness is bounded by the
        detector's normal data-age limit unless the caller supplies a stricter
        non-negative bound.
        """

        age_limit_ms = (
            self._maximum_decision_age_ms
            if maximum_age_ms is None
            else maximum_age_ms
        )
        if age_limit_ms < 0:
            raise ValueError("maximum_age_ms must be non-negative")
        decisions = tuple(
            pipeline.latest_decision
            for pipeline in self._pipelines.values()
            if pipeline.latest_decision is not None
            and pipeline.latest_decision.alertable
            and 0.0
            <= (
                now - pipeline.latest_decision.observed_at
            ).total_seconds()
            * 1_000.0
            <= age_limit_ms
        )
        return (
            max(decisions, key=lambda decision: decision.observed_at)
            if decisions
            else None
        )

    def reset(self) -> None:
        self._pipelines.clear()
        self._last_pipeline_key = None


__all__ = [
    "FrequencyScopedRfPipelinePool",
    "RfScanSession",
    "ScanRuntimeStatus",
]
