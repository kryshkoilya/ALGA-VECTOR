"""Adaptive, non-attributive analysis of power-spectrum frames."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import RLock

import numpy as np
from numpy.typing import NDArray

from alga_vector.domain.models import SpectrumFrame

TREND_LIMITATION = (
    "A rising received-power trend does not estimate distance, identify a target, "
    "or establish that anything is approaching."
)


class EventClass(StrEnum):
    """Generic observable RF activity classes.

    These labels describe spectrum shape and persistence, not an emitter type.
    """

    NARROWBAND_ACTIVITY = "narrowband_activity"
    BROADBAND_ACTIVITY = "broadband_activity"
    MULTICOMPONENT_ACTIVITY = "multicomponent_activity"
    TRANSIENT_BURST = "transient_burst"
    # Source compatibility for integrations that imported the former member.
    # New records always serialize the neutral ``transient_burst`` value.
    IMPULSIVE_INTERFERENCE = "transient_burst"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> EventClass | None:
        # Data compatibility for journals written before the neutral rename.
        if value == "impulsive_interference":
            return cls.TRANSIENT_BURST
        return None


class LevelTrend(StrEnum):
    """Trend of received power only."""

    RISING = "rising_received_power"
    STABLE = "stable_received_power"
    FALLING = "falling_received_power"
    INSUFFICIENT_DATA = "insufficient_data"


class QualityFlag(StrEnum):
    """Conditions that qualify or reduce trust in an analysis result."""

    ABSOLUTE_CALIBRATION_UNVERIFIED = "absolute_calibration_unverified"
    INSUFFICIENT_HISTORY = "insufficient_history"
    DROPPED_FRAMES_REPORTED = "dropped_frames_reported"
    SEQUENCE_GAP = "sequence_gap"
    DATA_STALE = "data_stale"
    CLOCK_REGRESSION = "clock_regression"
    SPECTRAL_GRID_CHANGED = "spectral_grid_changed"


class SpectrumAcquisitionMode(StrEnum):
    """How one spectrum frame was formed.

    A simultaneous FFT observes the whole displayed band during one acquisition
    window.  A swept analyser measures frequency points at different instants,
    so apparent bandwidth and short-event duration have additional ambiguity.
    """

    UNKNOWN = "unknown"
    SIMULTANEOUS_FFT = "simultaneous_fft"
    SWEPT_SPECTRUM = "swept_spectrum"


@dataclass(frozen=True, slots=True)
class SourceObservationMetadata:
    """Validated, caller-supplied acquisition facts for one receiver source."""

    acquisition_mode: SpectrumAcquisitionMode = SpectrumAcquisitionMode.UNKNOWN
    receiver_model: str | None = None
    sweep_duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.receiver_model is not None and not self.receiver_model.strip():
            raise ValueError("receiver_model must not be blank when provided")
        if self.sweep_duration_ms is not None and (
            not math.isfinite(self.sweep_duration_ms)
            or self.sweep_duration_ms <= 0.0
        ):
            raise ValueError("sweep_duration_ms must be finite and positive")
        if (
            self.sweep_duration_ms is not None
            and self.acquisition_mode != SpectrumAcquisitionMode.SWEPT_SPECTRUM
        ):
            raise ValueError("sweep_duration_ms is valid only for swept spectrum")


class AssessmentState(StrEnum):
    """Truthful guided interpretation of the latest receiver data."""

    NO_DATA = "no_data"
    LEARNING_BACKGROUND = "learning_background"
    BACKGROUND_ONLY = "background_only"
    DATA_UNRELIABLE = "data_unreliable"
    CONCENTRATED_RF = "concentrated_rf"
    WIDEBAND_RF = "wideband_rf"
    TRANSIENT_BURST = "transient_burst"
    UNCLASSIFIED_RF = "unclassified_rf"


class AssessmentTrust(StrEnum):
    """Trust in the observation quality, never in emitter identity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AttributionStatus(StrEnum):
    """Whether this receiver configuration can identify an emitter."""

    NOT_AVAILABLE = "not_available"


class FrameValidationError(ValueError):
    """A malformed frame that cannot be analysed safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Validated limits and conservative detector thresholds."""

    history_frames: int = 64
    min_history_frames: int = 8
    max_sources: int = 8
    floor_quantile: float = 0.20
    floor_rise_alpha: float = 0.12
    floor_fall_alpha: float = 0.35
    activity_margin_db: float = 8.0
    narrowband_max_fraction: float = 0.08
    broadband_min_fraction: float = 0.30
    max_narrowband_components: int = 3
    minimum_component_bins_for_strong_shape: int = 2
    impulse_min_fraction: float = 0.30
    impulse_median_excess_db: float = 12.0
    previous_quiet_fraction: float = 0.05
    trend_window: int = 6
    trend_slope_threshold_db_per_frame: float = 0.75
    max_data_age_ms: int = 3_000
    min_bins: int = 16
    max_bins: int = 65_536
    max_single_frame_score: float = 0.49
    max_two_frame_score: float = 0.74

    def __post_init__(self) -> None:
        if self.history_frames < 2:
            raise ValueError("history_frames must be at least 2")
        if not 1 <= self.min_history_frames <= self.history_frames:
            raise ValueError("min_history_frames must be in 1..history_frames")
        if self.max_sources < 1:
            raise ValueError("max_sources must be positive")
        for name, value in (
            ("floor_quantile", self.floor_quantile),
            ("floor_rise_alpha", self.floor_rise_alpha),
            ("floor_fall_alpha", self.floor_fall_alpha),
            ("narrowband_max_fraction", self.narrowband_max_fraction),
            ("broadband_min_fraction", self.broadband_min_fraction),
            ("impulse_min_fraction", self.impulse_min_fraction),
            ("previous_quiet_fraction", self.previous_quiet_fraction),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.narrowband_max_fraction >= self.broadband_min_fraction:
            raise ValueError("narrowband threshold must be below broadband threshold")
        if self.max_narrowband_components < 1:
            raise ValueError("max_narrowband_components must be positive")
        if self.minimum_component_bins_for_strong_shape < 2:
            raise ValueError(
                "minimum_component_bins_for_strong_shape must be at least 2"
            )
        if self.activity_margin_db <= 0.0 or self.impulse_median_excess_db <= 0.0:
            raise ValueError("detector margins must be positive")
        if self.trend_window < 3:
            raise ValueError("trend_window must be at least 3")
        if self.trend_slope_threshold_db_per_frame <= 0.0:
            raise ValueError("trend slope threshold must be positive")
        if self.max_data_age_ms < 0:
            raise ValueError("max_data_age_ms must be non-negative")
        if self.min_bins < 8 or self.max_bins < self.min_bins:
            raise ValueError("invalid bin-count limits")
        if not 0.0 < self.max_single_frame_score < self.max_two_frame_score < 1.0:
            raise ValueError("temporal score caps must satisfy 0 < single < two < 1")


@dataclass(frozen=True, slots=True)
class RfEvidence:
    """Measured evidence supporting a generic event label."""

    power_unit: str
    calibration_id: str | None
    calibration_uncertainty_db: float | None
    reported_noise_floor: float
    reported_peak_level: float
    peak_excess_db: float
    median_active_excess_db: float
    active_bin_fraction: float
    active_bin_count: int
    total_bin_count: int
    contiguous_component_count: int
    largest_contiguous_bins: int
    largest_contiguous_fraction: float
    occupied_bandwidth_hz: float
    frequency_envelope_hz: float
    spectral_fill_ratio: float
    frequency_low_hz: float
    frequency_high_hz: float
    peak_frequency_hz: float
    duration_frames: int
    history_frames: int
    confidence_components: tuple[tuple[str, float], ...]
    acquisition_mode: SpectrumAcquisitionMode = SpectrumAcquisitionMode.UNKNOWN
    receiver_model: str | None = None
    sweep_duration_ms: float | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RfEvent:
    """A non-attributive observation of spectrum activity."""

    event_id: str
    source_id: str
    sequence: int
    observed_at: datetime
    classification: EventClass
    confidence: float
    confidence_kind: str
    evidence: RfEvidence
    quality_flags: frozenset[QualityFlag]
    level_trend: LevelTrend
    received_level_slope_db_per_frame: float | None
    trend_limitation: str = TREND_LIMITATION


@dataclass(frozen=True, slots=True)
class SignalAssessmentEvidence:
    """Measurements exposed to guided and expert presenters.

    Optional activity fields remain ``None`` while a background is still being
    learned, so provisional comparisons cannot be presented as conclusions.
    """

    coverage_low_hz: float | None
    coverage_high_hz: float | None
    peak_frequency_hz: float | None
    occupied_bandwidth_hz: float | None
    peak_excess_over_floor_db: float | None
    active_fraction: float | None
    persistence_frames: int | None
    baseline_frames: int
    baseline_required_frames: int
    data_age_ms: int | None
    power_unit: str | None
    acquisition_mode: SpectrumAcquisitionMode = SpectrumAcquisitionMode.UNKNOWN
    receiver_model: str | None = None
    sweep_duration_ms: float | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        optional_finite = (
            self.coverage_low_hz,
            self.coverage_high_hz,
            self.peak_frequency_hz,
            self.occupied_bandwidth_hz,
            self.peak_excess_over_floor_db,
            self.active_fraction,
        )
        if any(value is not None and not math.isfinite(value) for value in optional_finite):
            raise ValueError("assessment evidence must be finite when present")
        if (
            self.coverage_low_hz is not None
            and self.coverage_high_hz is not None
            and self.coverage_low_hz >= self.coverage_high_hz
        ):
            raise ValueError("coverage_low_hz must be below coverage_high_hz")
        if self.occupied_bandwidth_hz is not None and self.occupied_bandwidth_hz < 0.0:
            raise ValueError("occupied_bandwidth_hz must be non-negative")
        if self.active_fraction is not None and not 0.0 <= self.active_fraction <= 1.0:
            raise ValueError("active_fraction must be in [0, 1]")
        if self.persistence_frames is not None and self.persistence_frames < 0:
            raise ValueError("persistence_frames must be non-negative")
        if self.baseline_frames < 0 or self.baseline_required_frames < 1:
            raise ValueError("baseline frame counts are invalid")
        if self.data_age_ms is not None and self.data_age_ms < 0:
            raise ValueError("data_age_ms must be non-negative")
        if self.power_unit is not None and not self.power_unit.strip():
            raise ValueError("power_unit must not be blank")
        if self.receiver_model is not None and not self.receiver_model.strip():
            raise ValueError("receiver_model must not be blank when provided")
        if self.sweep_duration_ms is not None and (
            not math.isfinite(self.sweep_duration_ms)
            or self.sweep_duration_ms <= 0.0
        ):
            raise ValueError("sweep_duration_ms must be finite and positive")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("assessment limitations must not be blank")


@dataclass(frozen=True, slots=True)
class SignalAssessment:
    """Always-current, novice-readable assessment with strict limitations."""

    state: AssessmentState
    trust: AssessmentTrust
    evidence: SignalAssessmentEvidence
    attribution: AttributionStatus
    identity_established: bool
    reason_code: str
    headline_ru: str
    explanation_ru: str
    operator_action_ru: str
    source_id: str | None
    sequence: int | None
    observed_at: datetime
    quality_flags: frozenset[QualityFlag] = frozenset()

    def __post_init__(self) -> None:
        if self.attribution != AttributionStatus.NOT_AVAILABLE:
            raise ValueError("emitter attribution is unavailable in this system")
        if self.identity_established:
            raise ValueError("emitter identity cannot be established by this system")
        if not self.reason_code.strip():
            raise ValueError("reason_code must not be blank")
        if not self.headline_ru.strip() or not self.explanation_ru.strip():
            raise ValueError("guided assessment text must not be blank")
        if not self.operator_action_ru.strip():
            raise ValueError("operator_action_ru must not be blank")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Result for one frame, including an assessment even when it is quiet."""

    source_id: str
    sequence: int
    event: RfEvent | None
    assessment: SignalAssessment
    power_unit: str
    calibration_id: str | None
    reported_noise_floor: float
    history_frames: int
    quality_flags: frozenset[QualityFlag]
    level_trend: LevelTrend
    received_level_slope_db_per_frame: float | None
    trend_limitation: str = TREND_LIMITATION


@dataclass(slots=True)
class _SourceState:
    grid: tuple[int, int, int]
    noise_history: deque[NDArray[np.float64]]
    recent_levels: deque[float]
    noise_floor: NDArray[np.float64] | None = None
    last_sequence: int | None = None
    last_captured_at: datetime | None = None
    previous_active_fraction: float = 0.0
    active_run_frames: int = 0


class RfEventDetector:
    """Bounded adaptive detector for generic spectrum activity.

    The confidence value is a deterministic heuristic evidence score, not a
    calibrated probability. Absolute levels are considered verified only when
    the frame carries both a calibration record identifier and its uncertainty.
    """

    def __init__(
        self,
        config: DetectorConfig | None = None,
        *,
        source_metadata: Mapping[str, SourceObservationMetadata] | None = None,
    ) -> None:
        self.config = config or DetectorConfig()
        self._sources: OrderedDict[str, _SourceState] = OrderedDict()
        self._source_metadata: OrderedDict[
            str, SourceObservationMetadata
        ] = OrderedDict()
        self._lock = RLock()
        for source_id, metadata in (source_metadata or {}).items():
            self.register_source_metadata(source_id, metadata)

    @property
    def tracked_source_count(self) -> int:
        with self._lock:
            return len(self._sources)

    def history_size(self, source_id: str) -> int:
        with self._lock:
            state = self._sources.get(source_id)
            return len(state.noise_history) if state is not None else 0

    def register_source_metadata(
        self,
        source_id: str,
        metadata: SourceObservationMetadata,
    ) -> None:
        """Register explicit acquisition facts without guessing from an id."""

        normalized = source_id.strip()
        if not normalized:
            raise ValueError("source_id must not be blank")
        if not isinstance(metadata, SourceObservationMetadata):
            raise TypeError("metadata must be SourceObservationMetadata")
        with self._lock:
            if (
                normalized not in self._source_metadata
                and len(self._source_metadata) >= self.config.max_sources
            ):
                self._source_metadata.popitem(last=False)
            self._source_metadata[normalized] = metadata
            self._source_metadata.move_to_end(normalized)

    def source_metadata(self, source_id: str) -> SourceObservationMetadata:
        """Return registered facts or an explicit unknown-mode record."""

        with self._lock:
            metadata = self._source_metadata.get(
                source_id,
                SourceObservationMetadata(),
            )
            if source_id in self._source_metadata:
                self._source_metadata.move_to_end(source_id)
            return metadata

    def clear_source_metadata(self, source_id: str | None = None) -> None:
        """Forget one metadata record or all caller-supplied records."""

        with self._lock:
            if source_id is None:
                self._source_metadata.clear()
            else:
                self._source_metadata.pop(source_id, None)

    def reset(self, source_id: str | None = None) -> None:
        """Drop adaptive state for one source or for all sources."""

        with self._lock:
            if source_id is None:
                self._sources.clear()
            else:
                self._sources.pop(source_id, None)

    def analyze(self, frame: SpectrumFrame) -> AnalysisResult:
        """Validate and analyse one immutable spectrum snapshot."""

        power = self._validate_frame(frame)
        with self._lock:
            metadata = self._source_metadata.get(
                frame.source_id,
                SourceObservationMetadata(),
            )
            if frame.source_id in self._source_metadata:
                self._source_metadata.move_to_end(frame.source_id)
            state, grid_changed = self._state_for(frame, len(power))
            quality = self._quality_flags(frame, state, grid_changed)
            history_before = len(state.noise_history)
            if history_before < self.config.min_history_frames:
                quality.add(QualityFlag.INSUFFICIENT_HISTORY)
            unreliable = bool(_unreliable_quality_flags(quality))

            floor = self._current_floor(state, power)
            excess = power - floor
            active = excess >= self.config.activity_margin_db
            active_count = int(np.count_nonzero(active))
            active_fraction = active_count / len(power)
            run_start, run_end = _largest_true_run(active)
            run_length = max(0, run_end - run_start)
            largest_fraction = run_length / len(power)
            component_count = _count_true_runs(active)

            duration_frames = (
                state.active_run_frames + 1 if active_count else 0
            )

            reported_peak = float(np.max(power))
            levels_for_frame = deque(
                state.recent_levels,
                maxlen=self.config.trend_window,
            )
            levels_for_frame.append(reported_peak)
            trend, slope = self._received_level_trend(levels_for_frame)

            classification = self._classify(
                active_count=active_count,
                active_fraction=active_fraction,
                largest_fraction=largest_fraction,
                component_count=component_count,
                median_active_excess=(
                    float(np.median(excess[active])) if active_count else 0.0
                ),
                previous_active_fraction=state.previous_active_fraction,
                duration_frames=duration_frames,
                history_mature=history_before >= self.config.min_history_frames,
                metadata=metadata,
            )

            event = None
            if classification is not None:
                evidence = self._evidence(
                    frame=frame,
                    power=power,
                    floor=floor,
                    excess=excess,
                    active=active,
                    active_fraction=active_fraction,
                    largest_fraction=largest_fraction,
                    component_count=component_count,
                    run_start=run_start,
                    run_end=run_end,
                    history_frames=history_before,
                    duration_frames=duration_frames,
                    classification=classification,
                    quality=quality,
                    metadata=metadata,
                )
                confidence = dict(evidence.confidence_components)["combined"]
                event = RfEvent(
                    event_id=_event_id(frame, classification),
                    source_id=frame.source_id,
                    sequence=frame.sequence,
                    observed_at=frame.captured_at,
                    classification=classification,
                    confidence=confidence,
                    confidence_kind="heuristic_evidence_score_not_probability",
                    evidence=evidence,
                    quality_flags=frozenset(quality),
                    level_trend=trend,
                    received_level_slope_db_per_frame=slope,
                )

            if not unreliable:
                # Stale, discontinuous or dropped data can still be retained as
                # expert evidence, but it must not teach the adaptive baseline
                # or persistence/trend state used by future guided results.
                self._update_floor(state, power, floor, active)
                state.active_run_frames = duration_frames
                state.previous_active_fraction = active_fraction
                state.recent_levels.append(reported_peak)
            state.last_sequence = frame.sequence
            if QualityFlag.CLOCK_REGRESSION not in quality:
                state.last_captured_at = frame.captured_at
            assessment = self._assessment(
                frame=frame,
                power=power,
                floor=floor,
                active_count=active_count,
                active_fraction=active_fraction,
                classification=classification,
                event=event,
                quality=quality,
                history_mature=history_before >= self.config.min_history_frames,
                history_frames=len(state.noise_history),
                persistence_frames=duration_frames,
                metadata=metadata,
            )
            return AnalysisResult(
                source_id=frame.source_id,
                sequence=frame.sequence,
                event=event,
                assessment=assessment,
                power_unit=frame.unit,
                calibration_id=frame.calibration_id,
                reported_noise_floor=float(np.median(floor)),
                history_frames=len(state.noise_history),
                quality_flags=frozenset(quality),
                level_trend=trend,
                received_level_slope_db_per_frame=slope,
            )

    def _validate_frame(self, frame: SpectrumFrame) -> NDArray[np.float64]:
        if not isinstance(frame, SpectrumFrame):
            raise FrameValidationError("FRAME.TYPE", "expected SpectrumFrame")
        if not frame.source_id.strip():
            raise FrameValidationError("FRAME.SOURCE", "source_id must not be blank")
        if frame.sequence < 0:
            raise FrameValidationError("FRAME.SEQUENCE", "sequence must be non-negative")
        if frame.center_frequency_hz <= 0 or frame.span_hz <= 0:
            raise FrameValidationError("FRAME.FREQUENCY", "frequency and span must be positive")
        if frame.center_frequency_hz - frame.span_hz / 2 <= 0:
            raise FrameValidationError(
                "FRAME.FREQUENCY_RANGE",
                "lower spectral edge must be positive",
            )
        if frame.captured_at.tzinfo is None or frame.captured_at.utcoffset() is None:
            raise FrameValidationError(
                "FRAME.TIMESTAMP",
                "captured_at must be timezone-aware",
            )
        if frame.dropped_frames < 0 or frame.data_age_ms < 0:
            raise FrameValidationError(
                "FRAME.COUNTERS",
                "dropped_frames and data_age_ms must be non-negative",
            )
        if not frame.unit.strip():
            raise FrameValidationError("FRAME.POWER_UNIT", "unit must not be blank")
        if frame.uncertainty_db is not None and (
            not np.isfinite(frame.uncertainty_db) or frame.uncertainty_db < 0.0
        ):
            raise FrameValidationError(
                "FRAME.CALIBRATION_UNCERTAINTY",
                "uncertainty_db must be finite and non-negative",
            )
        power = np.asarray(frame.power_dbm, dtype=np.float64)
        if power.ndim != 1:
            raise FrameValidationError("FRAME.POWER_SHAPE", "power array must be one-dimensional")
        if not self.config.min_bins <= len(power) <= self.config.max_bins:
            raise FrameValidationError(
                "FRAME.BIN_COUNT",
                f"bin count must be in {self.config.min_bins}..{self.config.max_bins}",
            )
        if not bool(np.all(np.isfinite(power))):
            raise FrameValidationError("FRAME.NON_FINITE", "power contains NaN or infinity")
        return np.array(power, dtype=np.float64, copy=True)

    def _state_for(
        self,
        frame: SpectrumFrame,
        bins: int,
    ) -> tuple[_SourceState, bool]:
        state = self._sources.get(frame.source_id)
        grid = (frame.center_frequency_hz, frame.span_hz, bins)
        if state is None:
            if len(self._sources) >= self.config.max_sources:
                self._sources.popitem(last=False)
            state = _SourceState(
                grid=grid,
                noise_history=deque(maxlen=self.config.history_frames),
                recent_levels=deque(maxlen=self.config.trend_window),
            )
            self._sources[frame.source_id] = state
            return state, False

        self._sources.move_to_end(frame.source_id)
        if state.last_sequence is not None and frame.sequence <= state.last_sequence:
            raise FrameValidationError(
                "FRAME.NON_MONOTONIC_SEQUENCE",
                f"sequence {frame.sequence} is not newer than {state.last_sequence}",
            )
        grid_changed = state.grid != grid
        if grid_changed:
            state.grid = grid
            state.noise_history.clear()
            state.recent_levels.clear()
            state.noise_floor = None
            state.previous_active_fraction = 0.0
            state.active_run_frames = 0
        return state, grid_changed

    def _quality_flags(
        self,
        frame: SpectrumFrame,
        state: _SourceState,
        grid_changed: bool,
    ) -> set[QualityFlag]:
        flags: set[QualityFlag] = set()
        if frame.calibration_id is None or frame.uncertainty_db is None:
            flags.add(QualityFlag.ABSOLUTE_CALIBRATION_UNVERIFIED)
        if frame.dropped_frames:
            flags.add(QualityFlag.DROPPED_FRAMES_REPORTED)
        if frame.data_age_ms > self.config.max_data_age_ms:
            flags.add(QualityFlag.DATA_STALE)
        if (
            state.last_sequence is not None
            and frame.sequence > state.last_sequence + 1
        ):
            flags.add(QualityFlag.SEQUENCE_GAP)
        if state.last_captured_at is not None and frame.captured_at < state.last_captured_at:
            flags.add(QualityFlag.CLOCK_REGRESSION)
        if grid_changed:
            flags.add(QualityFlag.SPECTRAL_GRID_CHANGED)
        return flags

    def _current_floor(
        self,
        state: _SourceState,
        power: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        if state.noise_floor is None:
            initial = float(np.quantile(power, self.config.floor_quantile))
            return np.full_like(power, initial)
        return np.array(state.noise_floor, copy=True)

    def _update_floor(
        self,
        state: _SourceState,
        power: NDArray[np.float64],
        floor: NDArray[np.float64],
        active: NDArray[np.bool_],
    ) -> None:
        # Active bins are held at the previous floor, so persistent activity does
        # not quickly teach the detector to regard itself as background.
        candidate = np.where(active, floor, power)
        state.noise_history.append(np.asarray(candidate, dtype=np.float64))
        stacked = np.stack(tuple(state.noise_history), axis=0)
        target = np.asarray(
            np.quantile(stacked, self.config.floor_quantile, axis=0),
            dtype=np.float64,
        )
        delta = target - floor
        alpha = np.where(
            delta >= 0.0,
            self.config.floor_rise_alpha,
            self.config.floor_fall_alpha,
        )
        state.noise_floor = np.asarray(floor + alpha * delta, dtype=np.float64)

    def _classify(
        self,
        *,
        active_count: int,
        active_fraction: float,
        largest_fraction: float,
        component_count: int,
        median_active_excess: float,
        previous_active_fraction: float,
        duration_frames: int,
        history_mature: bool,
        metadata: SourceObservationMetadata,
    ) -> EventClass | None:
        if active_count == 0:
            return None
        transient_shape = (
            history_mature
            and duration_frames == 1
            and active_fraction >= self.config.impulse_min_fraction
            and median_active_excess >= self.config.impulse_median_excess_db
            and previous_active_fraction <= self.config.previous_quiet_fraction
        )
        if (
            transient_shape
            and metadata.acquisition_mode
            != SpectrumAcquisitionMode.SWEPT_SPECTRUM
        ):
            return EventClass.TRANSIENT_BURST
        if (
            transient_shape
            and metadata.acquisition_mode
            == SpectrumAcquisitionMode.SWEPT_SPECTRUM
        ):
            # Sweep points are not simultaneous.  One sweep cannot establish a
            # whole-band impulse or its duration.
            return EventClass.UNKNOWN
        if (
            metadata.acquisition_mode == SpectrumAcquisitionMode.SWEPT_SPECTRUM
            and duration_frames == 1
            and (
                active_fraction >= self.config.broadband_min_fraction
                or largest_fraction >= self.config.broadband_min_fraction
            )
        ):
            return EventClass.UNKNOWN
        if (
            active_fraction >= self.config.broadband_min_fraction
            or largest_fraction >= self.config.broadband_min_fraction
        ):
            return EventClass.BROADBAND_ACTIVITY
        if component_count > self.config.max_narrowband_components:
            return EventClass.MULTICOMPONENT_ACTIVITY
        if (
            active_fraction <= self.config.narrowband_max_fraction
            and largest_fraction <= self.config.narrowband_max_fraction
        ):
            return EventClass.NARROWBAND_ACTIVITY
        return EventClass.UNKNOWN

    def _received_level_trend(
        self,
        levels: deque[float],
    ) -> tuple[LevelTrend, float | None]:
        if len(levels) < 3:
            return LevelTrend.INSUFFICIENT_DATA, None
        values = np.asarray(levels, dtype=np.float64)
        x = np.arange(len(values), dtype=np.float64)
        slope = float(np.polyfit(x, values, 1)[0])
        threshold = self.config.trend_slope_threshold_db_per_frame
        if slope >= threshold:
            return LevelTrend.RISING, slope
        if slope <= -threshold:
            return LevelTrend.FALLING, slope
        return LevelTrend.STABLE, slope

    def _evidence(
        self,
        *,
        frame: SpectrumFrame,
        power: NDArray[np.float64],
        floor: NDArray[np.float64],
        excess: NDArray[np.float64],
        active: NDArray[np.bool_],
        active_fraction: float,
        largest_fraction: float,
        component_count: int,
        run_start: int,
        run_end: int,
        history_frames: int,
        duration_frames: int,
        classification: EventClass,
        quality: set[QualityFlag],
        metadata: SourceObservationMetadata,
    ) -> RfEvidence:
        active_indices = np.flatnonzero(active)
        peak_index = int(np.argmax(power))
        peak_excess = float(np.max(excess))
        median_active_excess = float(np.median(excess[active]))
        bin_width_hz = frame.span_hz / max(1, len(power) - 1)
        low_index = int(active_indices[0])
        high_index = int(active_indices[-1])
        lower_edge_hz = frame.center_frequency_hz - frame.span_hz / 2
        largest_contiguous_bins = max(0, run_end - run_start)
        occupied_bandwidth_hz = float(len(active_indices) * bin_width_hz)
        frequency_envelope_hz = float((high_index - low_index + 1) * bin_width_hz)
        spectral_fill_ratio = _clamp01(
            occupied_bandwidth_hz / max(bin_width_hz, frequency_envelope_hz)
        )

        snr_score = _clamp01(
            (peak_excess - self.config.activity_margin_db) / 20.0 + 0.35
        )
        if classification == EventClass.NARROWBAND_ACTIVITY:
            shape_score = _clamp01(
                1.0 - largest_fraction / self.config.narrowband_max_fraction
            )
            if (
                largest_contiguous_bins
                < self.config.minimum_component_bins_for_strong_shape
            ):
                # One FFT bin is a candidate observation, not strong evidence.
                # It is especially vulnerable to DC leakage, local spurs and
                # max-pooling outliers.
                shape_score = min(shape_score, 0.45)
        elif classification == EventClass.BROADBAND_ACTIVITY:
            shape_score = _clamp01(
                active_fraction / self.config.broadband_min_fraction
            )
        elif classification == EventClass.MULTICOMPONENT_ACTIVITY:
            shape_score = _clamp01(
                component_count / (self.config.max_narrowband_components + 3)
            )
        elif classification == EventClass.TRANSIENT_BURST:
            shape_score = _clamp01(
                0.5 * active_fraction / self.config.impulse_min_fraction
                + 0.5
                * median_active_excess
                / self.config.impulse_median_excess_db
            )
        else:
            shape_score = 0.35
        history_score = _clamp01(history_frames / self.config.min_history_frames)
        if classification == EventClass.TRANSIENT_BURST:
            # A single impulse can be described as an impulse, but it cannot
            # become a trusted operational episode without recurrence.
            persistence_score = 0.0 if duration_frames == 1 else 0.4
        else:
            persistence_score = _clamp01((duration_frames - 1) / 2.0)
        component_score = _clamp01(
            1.0
            - max(0, component_count - 1)
            / max(1, self.config.max_narrowband_components + 2)
        )
        quality_score = _quality_score(quality)
        combined = (
            0.30 * snr_score
            + 0.20 * shape_score
            + 0.15 * history_score
            + 0.25 * persistence_score
            + 0.10 * component_score
        ) * quality_score
        if duration_frames <= 1:
            combined = min(combined, self.config.max_single_frame_score)
        elif duration_frames == 2:
            combined = min(combined, self.config.max_two_frame_score)
        if QualityFlag.INSUFFICIENT_HISTORY in quality:
            combined = min(combined, 0.35)
        if classification in {
            EventClass.UNKNOWN,
            EventClass.MULTICOMPONENT_ACTIVITY,
        }:
            combined = min(combined, 0.49)
        if metadata.acquisition_mode == SpectrumAcquisitionMode.SWEPT_SPECTRUM:
            # Recurrence can later increase temporal support, but one swept
            # shape remains limited by time-smearing and cadence aliasing.
            combined = min(combined, 0.69)
        combined = _clamp01(combined)
        limitations = _acquisition_limitations(metadata)

        return RfEvidence(
            power_unit=frame.unit,
            calibration_id=frame.calibration_id,
            calibration_uncertainty_db=frame.uncertainty_db,
            reported_noise_floor=float(np.median(floor)),
            reported_peak_level=float(power[peak_index]),
            peak_excess_db=peak_excess,
            median_active_excess_db=median_active_excess,
            active_bin_fraction=active_fraction,
            active_bin_count=len(active_indices),
            total_bin_count=len(power),
            contiguous_component_count=component_count,
            largest_contiguous_bins=largest_contiguous_bins,
            largest_contiguous_fraction=largest_fraction,
            occupied_bandwidth_hz=occupied_bandwidth_hz,
            frequency_envelope_hz=frequency_envelope_hz,
            spectral_fill_ratio=spectral_fill_ratio,
            frequency_low_hz=float(lower_edge_hz + low_index * bin_width_hz),
            frequency_high_hz=float(lower_edge_hz + high_index * bin_width_hz),
            peak_frequency_hz=float(lower_edge_hz + peak_index * bin_width_hz),
            duration_frames=duration_frames,
            history_frames=history_frames,
            confidence_components=(
                ("snr", snr_score),
                ("shape", shape_score),
                ("history", history_score),
                ("persistence", persistence_score),
                ("component_consistency", component_score),
                ("quality", quality_score),
                ("combined", combined),
            ),
            acquisition_mode=metadata.acquisition_mode,
            receiver_model=metadata.receiver_model,
            sweep_duration_ms=metadata.sweep_duration_ms,
            limitations=limitations,
        )

    def _assessment(
        self,
        *,
        frame: SpectrumFrame,
        power: NDArray[np.float64],
        floor: NDArray[np.float64],
        active_count: int,
        active_fraction: float,
        classification: EventClass | None,
        event: RfEvent | None,
        quality: set[QualityFlag],
        history_mature: bool,
        history_frames: int,
        persistence_frames: int,
        metadata: SourceObservationMetadata,
    ) -> SignalAssessment:
        coverage_low_hz = frame.center_frequency_hz - frame.span_hz / 2
        coverage_high_hz = frame.center_frequency_hz + frame.span_hz / 2
        peak_index = int(np.argmax(power))
        bin_width_hz = frame.span_hz / max(1, len(power) - 1)
        peak_frequency_hz = coverage_low_hz + peak_index * bin_width_hz
        unreliable = _unreliable_quality_flags(quality)

        if unreliable:
            state = AssessmentState.DATA_UNRELIABLE
            trust = AssessmentTrust.LOW
            reason_code = _unreliable_reason_code(unreliable)
            headline = "Данным сейчас нельзя уверенно доверять"
            explanation = _unreliable_explanation_ru(unreliable)
            action = "Проверьте приёмник и дождитесь нескольких последовательных свежих кадров."
        elif not history_mature:
            state = AssessmentState.LEARNING_BACKGROUND
            trust = AssessmentTrust.LOW
            reason_code = "SIGNAL.BASELINE_LEARNING"
            headline = "Изучаю обычный радиофон"
            explanation = (
                f"Собрано {history_frames} из {self.config.min_history_frames} "
                "кадров фона. До завершения обучения система не делает вывод "
                "о характере изменений."
            )
            action = (
                "Оставьте приёмник включённым и не меняйте частоту, пока обучение не завершится."
            )
        elif active_count == 0:
            state = AssessmentState.BACKGROUND_ONLY
            trust = AssessmentTrust.HIGH
            reason_code = "SIGNAL.BACKGROUND_ONLY"
            headline = "Только изученный радиофон"
            explanation = (
                "Выраженных изменений относительно изученного фона в текущей полосе нет."
            )
            action = "Ничего настраивать не требуется; система продолжает наблюдение."
        elif classification == EventClass.NARROWBAND_ACTIVITY:
            state = AssessmentState.CONCENTRATED_RF
            trust = _event_trust(event)
            reason_code = "SIGNAL.CONCENTRATED_RF"
            headline = "Есть сосредоточенное радиоизменение"
            explanation = (
                "Энергия выше изученного фона сосредоточена в небольшой части текущей полосы."
            )
            action = "Посмотрите частоту и полосу; для определения источника нужны другие данные."
        elif classification == EventClass.BROADBAND_ACTIVITY:
            state = AssessmentState.WIDEBAND_RF
            trust = _event_trust(event)
            reason_code = "SIGNAL.WIDEBAND_RF"
            headline = "Есть широкополосное радиоизменение"
            explanation = (
                "Уровень одновременно вырос в значительной части наблюдаемой полосы."
            )
            action = "Наблюдайте устойчивость изменения; источник по одному спектру не определяется."
        elif classification == EventClass.TRANSIENT_BURST:
            state = AssessmentState.TRANSIENT_BURST
            trust = _event_trust(event)
            reason_code = "SIGNAL.TRANSIENT_BURST"
            headline = "Зафиксирован короткий радиовсплеск"
            explanation = (
                "После спокойного кадра уровень кратковременно вырос в широкой части полосы."
            )
            action = "Дождитесь повторения; одиночный всплеск не позволяет определить источник."
        elif classification == EventClass.MULTICOMPONENT_ACTIVITY:
            state = AssessmentState.UNCLASSIFIED_RF
            trust = AssessmentTrust.LOW
            reason_code = "SIGNAL.MULTICOMPONENT_RF"
            headline = "Есть несколько разнесённых радиоизменений"
            explanation = (
                "Изменение состоит из нескольких раздельных участков спектра; "
                "они не объединяются в один узкополосный источник."
            )
            action = (
                "Продолжайте наблюдение: одиночные помехи и несколько независимых "
                "сигналов должны быть разделены во времени."
            )
        else:
            state = AssessmentState.UNCLASSIFIED_RF
            trust = AssessmentTrust.LOW
            reason_code = "SIGNAL.UNCLASSIFIED_RF"
            headline = "Есть радиоизменение неясной формы"
            explanation = (
                "Часть спектра отличается от изученного фона, но форма не соответствует "
                "устойчивой категории."
            )
            action = "Продолжайте наблюдение; системе нужны дополнительные последовательные кадры."

        limitations = _acquisition_limitations(metadata)
        if limitations:
            explanation = f"{explanation} Ограничение: {limitations[0]}"

        expose_activity = history_mature
        return SignalAssessment(
            state=state,
            trust=trust,
            evidence=SignalAssessmentEvidence(
                coverage_low_hz=float(coverage_low_hz),
                coverage_high_hz=float(coverage_high_hz),
                peak_frequency_hz=float(peak_frequency_hz),
                occupied_bandwidth_hz=(
                    float(active_count * bin_width_hz) if expose_activity else None
                ),
                peak_excess_over_floor_db=(
                    float(np.max(power - floor)) if expose_activity else None
                ),
                active_fraction=active_fraction if expose_activity else None,
                persistence_frames=persistence_frames if expose_activity else None,
                baseline_frames=history_frames,
                baseline_required_frames=self.config.min_history_frames,
                data_age_ms=frame.data_age_ms,
                power_unit=frame.unit,
                acquisition_mode=metadata.acquisition_mode,
                receiver_model=metadata.receiver_model,
                sweep_duration_ms=metadata.sweep_duration_ms,
                limitations=limitations,
            ),
            attribution=AttributionStatus.NOT_AVAILABLE,
            identity_established=False,
            reason_code=reason_code,
            headline_ru=headline,
            explanation_ru=explanation,
            operator_action_ru=action,
            source_id=frame.source_id,
            sequence=frame.sequence,
            observed_at=frame.captured_at,
            quality_flags=frozenset(quality),
        )


def _largest_true_run(mask: NDArray[np.bool_]) -> tuple[int, int]:
    best_start = 0
    best_end = 0
    current_start: int | None = None
    for index, enabled in enumerate(mask):
        if bool(enabled) and current_start is None:
            current_start = index
        elif not bool(enabled) and current_start is not None:
            if index - current_start > best_end - best_start:
                best_start, best_end = current_start, index
            current_start = None
    if current_start is not None and len(mask) - current_start > best_end - best_start:
        best_start, best_end = current_start, len(mask)
    return best_start, best_end


def _count_true_runs(mask: NDArray[np.bool_]) -> int:
    """Count disjoint active spectral components without allocating an index list."""

    components = 0
    inside = False
    for enabled in mask:
        current = bool(enabled)
        if current and not inside:
            components += 1
        inside = current
    return components


def _acquisition_limitations(
    metadata: SourceObservationMetadata,
) -> tuple[str, ...]:
    if metadata.acquisition_mode == SpectrumAcquisitionMode.SWEPT_SPECTRUM:
        duration = (
            f" за {metadata.sweep_duration_ms:.0f} мс"
            if metadata.sweep_duration_ms is not None
            else ""
        )
        return (
            "кадр получен последовательной развёрткой"
            f"{duration}; его точки измерены не одновременно, поэтому полоса "
            "и длительность короткого сигнала имеют дополнительную неопределённость",
            "период развёртки может скрывать или повторно отображать короткие события",
        )
    if metadata.acquisition_mode == SpectrumAcquisitionMode.UNKNOWN:
        return (
            "способ формирования спектрального кадра не указан; выводы о "
            "короткой длительности и одновременной ширине ограничены",
        )
    return ()


def no_data_assessment(
    observed_at: datetime,
    *,
    reason_code: str = "SIGNAL.NO_DATA",
    explanation_ru: str = "Приёмник ещё не передал пригодный кадр спектра.",
    operator_action_ru: str = "Проверьте подключение приёмника и запустите получение данных.",
    baseline_required_frames: int = 8,
) -> SignalAssessment:
    """Create a truthful guided state when no usable frame exists."""

    return SignalAssessment(
        state=AssessmentState.NO_DATA,
        trust=AssessmentTrust.LOW,
        evidence=SignalAssessmentEvidence(
            coverage_low_hz=None,
            coverage_high_hz=None,
            peak_frequency_hz=None,
            occupied_bandwidth_hz=None,
            peak_excess_over_floor_db=None,
            active_fraction=None,
            persistence_frames=None,
            baseline_frames=0,
            baseline_required_frames=baseline_required_frames,
            data_age_ms=None,
            power_unit=None,
        ),
        attribution=AttributionStatus.NOT_AVAILABLE,
        identity_established=False,
        reason_code=reason_code,
        headline_ru="Нет пригодных данных",
        explanation_ru=explanation_ru,
        operator_action_ru=operator_action_ru,
        source_id=None,
        sequence=None,
        observed_at=observed_at,
    )


def data_unreliable_assessment(
    previous: SignalAssessment,
    observed_at: datetime,
    *,
    reason_code: str,
    explanation_ru: str,
    operator_action_ru: str = (
        "Проверьте приёмник и дождитесь нескольких последовательных свежих кадров."
    ),
    data_age_ms: int | None = None,
    quality_flag: QualityFlag | None = None,
) -> SignalAssessment:
    """Invalidate a previous observation without reinterpreting its evidence."""

    flags = set(previous.quality_flags)
    if quality_flag is not None:
        flags.add(quality_flag)
    evidence = previous.evidence
    if data_age_ms is not None:
        evidence = replace(evidence, data_age_ms=max(0, data_age_ms))
    return SignalAssessment(
        state=AssessmentState.DATA_UNRELIABLE,
        trust=AssessmentTrust.LOW,
        evidence=evidence,
        attribution=AttributionStatus.NOT_AVAILABLE,
        identity_established=False,
        reason_code=reason_code,
        headline_ru="Данным сейчас нельзя уверенно доверять",
        explanation_ru=explanation_ru,
        operator_action_ru=operator_action_ru,
        source_id=previous.source_id,
        sequence=previous.sequence,
        observed_at=observed_at,
        quality_flags=frozenset(flags),
    )


def assessment_with_data_age(
    assessment: SignalAssessment,
    data_age_ms: int,
) -> SignalAssessment:
    """Return a detached assessment whose age matches the published frame."""

    return replace(
        assessment,
        evidence=replace(
            assessment.evidence,
            data_age_ms=max(0, data_age_ms),
        ),
    )


def _unreliable_quality_flags(
    quality: set[QualityFlag],
) -> frozenset[QualityFlag]:
    return frozenset(
        quality
        & {
            QualityFlag.DROPPED_FRAMES_REPORTED,
            QualityFlag.SEQUENCE_GAP,
            QualityFlag.DATA_STALE,
            QualityFlag.CLOCK_REGRESSION,
        }
    )


def _unreliable_reason_code(flags: frozenset[QualityFlag]) -> str:
    priorities = (
        (QualityFlag.DATA_STALE, "SIGNAL.DATA_STALE"),
        (QualityFlag.CLOCK_REGRESSION, "SIGNAL.CLOCK_REGRESSION"),
        (QualityFlag.DROPPED_FRAMES_REPORTED, "SIGNAL.FRAMES_DROPPED"),
        (QualityFlag.SEQUENCE_GAP, "SIGNAL.SEQUENCE_GAP"),
    )
    return next(code for flag, code in priorities if flag in flags)


def _unreliable_explanation_ru(flags: frozenset[QualityFlag]) -> str:
    reasons: list[str] = []
    if QualityFlag.DATA_STALE in flags:
        reasons.append("кадр устарел")
    if QualityFlag.CLOCK_REGRESSION in flags:
        reasons.append("время кадров изменилось назад")
    if QualityFlag.DROPPED_FRAMES_REPORTED in flags:
        reasons.append("приёмник сообщил о пропущенных кадрах")
    if QualityFlag.SEQUENCE_GAP in flags:
        reasons.append("в последовательности кадров есть разрыв")
    return "Надёжная интерпретация приостановлена: " + ", ".join(reasons) + "."


def _event_trust(event: RfEvent | None) -> AssessmentTrust:
    if (
        event is None
        or event.evidence.duration_frames <= 1
        or event.confidence < 0.50
    ):
        return AssessmentTrust.LOW
    if event.evidence.duration_frames == 2 or event.confidence < 0.75:
        return AssessmentTrust.MEDIUM
    return AssessmentTrust.HIGH


def _quality_score(flags: set[QualityFlag]) -> float:
    score = 1.0
    penalties = {
        QualityFlag.ABSOLUTE_CALIBRATION_UNVERIFIED: 0.97,
        QualityFlag.INSUFFICIENT_HISTORY: 0.65,
        QualityFlag.DROPPED_FRAMES_REPORTED: 0.78,
        QualityFlag.SEQUENCE_GAP: 0.82,
        QualityFlag.DATA_STALE: 0.72,
        QualityFlag.CLOCK_REGRESSION: 0.80,
        QualityFlag.SPECTRAL_GRID_CHANGED: 0.65,
    }
    for flag in flags:
        score *= penalties[flag]
    return _clamp01(score)


def _clamp01(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


def _event_id(frame: SpectrumFrame, classification: EventClass) -> str:
    identity = (
        f"{frame.source_id}|{frame.sequence}|{frame.captured_at.isoformat()}|"
        f"{classification.value}"
    ).encode()
    return f"rf-{hashlib.sha256(identity).hexdigest()[:16]}"


__all__ = [
    "TREND_LIMITATION",
    "AnalysisResult",
    "AssessmentState",
    "AssessmentTrust",
    "AttributionStatus",
    "DetectorConfig",
    "EventClass",
    "FrameValidationError",
    "LevelTrend",
    "QualityFlag",
    "RfEvent",
    "RfEventDetector",
    "RfEvidence",
    "SignalAssessment",
    "SignalAssessmentEvidence",
    "SourceObservationMetadata",
    "SpectrumAcquisitionMode",
    "assessment_with_data_age",
    "data_unreliable_assessment",
    "no_data_assessment",
]
