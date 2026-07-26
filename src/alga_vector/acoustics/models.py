"""Typed, non-attributive models for passive acoustic monitoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
import numpy.typing as npt

type EvidenceValue = float | int | str | None


class AcousticFamily(StrEnum):
    """Observable sound-shape families, never an object identity."""

    ROTOR_LIKE = "rotor_like"
    ENGINE_LIKE = "engine_like"
    BROADBAND_ANOMALY = "broadband_anomaly"
    UNKNOWN_AERIAL_LIKE = "unknown_aerial_like"
    AMBIENT_NOISE = "ambient_noise"


class AcousticLifecycle(StrEnum):
    """Conservative temporal lifecycle for one microphone source."""

    IDLE = "idle"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    HOLDING = "holding"
    DATA_HOLD = "data_hold"


class AcousticDataQuality(StrEnum):
    """Trust in the PCM window, separate from heuristic evidence."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AcousticProvenanceKind(StrEnum):
    """Origin of PCM samples supplied to the safe core."""

    LIVE_MICROPHONE = "live_microphone"
    REPLAY = "replay"
    SIMULATED = "simulated"


class AcousticQualityFlag(StrEnum):
    """Machine-readable quality and fail-closed reasons."""

    INVALID_SAMPLE_RATE = "invalid_sample_rate"
    INVALID_SAMPLE_SHAPE = "invalid_sample_shape"
    UNSUPPORTED_SAMPLE_DTYPE = "unsupported_sample_dtype"
    NON_FINITE_SAMPLES = "non_finite_samples"
    PCM_OUT_OF_RANGE = "pcm_out_of_range"
    WINDOW_TOO_SHORT = "window_too_short"
    WINDOW_TOO_LARGE = "window_too_large"
    TIMESTAMP_INVALID = "timestamp_invalid"
    DATA_STALE = "data_stale"
    DISCONTINUITY_REPORTED = "discontinuity_reported"
    DROPPED_SAMPLES_REPORTED = "dropped_samples_reported"
    SEQUENCE_GAP = "sequence_gap"
    SAMPLE_RATE_CHANGED = "sample_rate_changed"
    SOURCE_SESSION_CHANGED = "source_session_changed"
    TIMING_DISCONTINUITY = "timing_discontinuity"
    LOW_SIGNAL = "low_signal"
    CLIPPING_DETECTED = "clipping_detected"


@dataclass(frozen=True, slots=True)
class AcousticProvenance:
    """Traceable origin supplied by an operator-controlled capture adapter."""

    source_id: str
    device_id: str
    session_id: str
    kind: AcousticProvenanceKind
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("device_id", self.device_id),
            ("session_id", self.session_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be blank")
        if self.calibration_id is not None and not self.calibration_id.strip():
            raise ValueError("calibration_id must be non-blank when supplied")


@dataclass(frozen=True, slots=True)
class PcmWindow:
    """One already-authorized mono PCM window and its capture metadata."""

    samples: npt.NDArray[np.generic]
    sample_rate_hz: int
    sequence: int
    captured_at: datetime
    received_at: datetime
    provenance: AcousticProvenance
    discontinuity: bool = False
    dropped_samples: int = 0

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.dropped_samples < 0:
            raise ValueError("dropped_samples must be non-negative")


@dataclass(frozen=True, slots=True)
class AcousticBandEnergy:
    """Energy measured inside one fixed audio band."""

    name: str
    low_hz: float
    high_hz: float
    energy: float
    ratio: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("band name must not be blank")
        if not 0.0 <= self.low_hz <= self.high_hz:
            raise ValueError("band limits must be ordered and non-negative")
        if not math.isfinite(self.energy) or self.energy < 0.0:
            raise ValueError("band energy must be finite and non-negative")
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError("band ratio must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class AcousticFeatures:
    """Numpy-derived observables from a single validated PCM window."""

    rms: float
    crest_factor: float
    zero_crossing_rate: float
    dominant_frequency_hz: float
    spectral_centroid_hz: float
    band_energy: tuple[AcousticBandEnergy, ...]
    clipped_fraction: float
    duration_seconds: float

    def __post_init__(self) -> None:
        for name, value in (
            ("rms", self.rms),
            ("crest_factor", self.crest_factor),
            ("zero_crossing_rate", self.zero_crossing_rate),
            ("dominant_frequency_hz", self.dominant_frequency_hz),
            ("spectral_centroid_hz", self.spectral_centroid_hz),
            ("clipped_fraction", self.clipped_fraction),
            ("duration_seconds", self.duration_seconds),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.zero_crossing_rate > 1.0 or self.clipped_fraction > 1.0:
            raise ValueError("fractional acoustic features must be in [0, 1]")
        if not self.band_energy:
            raise ValueError("band_energy must not be empty")

    def band_ratio(self, name: str) -> float:
        """Return a named band ratio, or zero when the band is unavailable."""

        return next((item.ratio for item in self.band_energy if item.name == name), 0.0)


@dataclass(frozen=True, slots=True)
class AcousticEvidence:
    """One explainable measured fact in an acoustic assessment."""

    code: str
    explanation_ru: str
    measured: EvidenceValue = None
    threshold: EvidenceValue = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.explanation_ru.strip():
            raise ValueError("evidence code and explanation must not be blank")
        for value in (self.measured, self.threshold):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("numeric evidence must be finite")


@dataclass(frozen=True, slots=True)
class AcousticAssessment:
    """Fail-closed, non-attributive result for one PCM window."""

    observed_at: datetime
    provenance: AcousticProvenance
    lifecycle: AcousticLifecycle
    family: AcousticFamily
    window_family: AcousticFamily
    heuristic_score: float
    alertable: bool
    data_quality: AcousticDataQuality
    quality_flags: frozenset[AcousticQualityFlag]
    explanation_ru: str
    evidence: tuple[AcousticEvidence, ...]
    limitations: tuple[str, ...]
    alternatives: tuple[AcousticFamily, ...] = ()
    features: AcousticFeatures | None = None
    consecutive_windows: int = 0
    episode_id: str | None = None
    calibrated_probability: None = None
    identity_established: bool = False

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if not 0.0 <= self.heuristic_score <= 1.0:
            raise ValueError("heuristic_score must be in [0, 1]")
        if not self.explanation_ru.strip():
            raise ValueError("explanation_ru must not be blank")
        if not self.limitations:
            raise ValueError("limitations must not be empty")
        if self.consecutive_windows < 0:
            raise ValueError("consecutive_windows must be non-negative")
        if self.calibrated_probability is not None:
            raise ValueError("calibrated probability is unavailable")
        if self.identity_established:
            raise ValueError("acoustic identity is not established")
        if self.alertable and self.lifecycle not in {
            AcousticLifecycle.CONFIRMED,
            AcousticLifecycle.HOLDING,
        }:
            raise ValueError("only confirmed or holding assessments can be alertable")
        if self.alertable and self.family == AcousticFamily.AMBIENT_NOISE:
            raise ValueError("ambient noise cannot be alertable")
        if self.lifecycle == AcousticLifecycle.DATA_HOLD and self.alertable:
            raise ValueError("data hold must fail closed")
        if self.episode_id is not None and not self.episode_id.strip():
            raise ValueError("episode_id must be non-blank when supplied")
        if any(item == self.family for item in self.alternatives):
            raise ValueError("selected family cannot be its own alternative")
