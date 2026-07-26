"""Numpy-only PCM normalization and acoustic feature extraction."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from .models import AcousticBandEnergy, AcousticFeatures, AcousticQualityFlag


class AcousticFeatureError(ValueError):
    """A PCM window cannot be analyzed safely."""

    def __init__(self, code: str, flag: AcousticQualityFlag, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.flag = flag


@dataclass(frozen=True, slots=True)
class FeatureExtractionConfig:
    """Bounds for deterministic PCM feature extraction."""

    minimum_sample_rate_hz: int = 8_000
    maximum_sample_rate_hz: int = 192_000
    minimum_samples: int = 256
    maximum_samples: int = 1_048_576

    def __post_init__(self) -> None:
        if self.minimum_sample_rate_hz < 1:
            raise ValueError("minimum_sample_rate_hz must be positive")
        if self.maximum_sample_rate_hz <= self.minimum_sample_rate_hz:
            raise ValueError("maximum sample rate must exceed minimum sample rate")
        if self.minimum_samples < 32:
            raise ValueError("minimum_samples must be at least 32")
        if self.maximum_samples < self.minimum_samples:
            raise ValueError("maximum_samples must not be below minimum_samples")


def normalize_pcm_samples(
    samples: npt.NDArray[np.generic],
) -> npt.NDArray[np.float64]:
    """Normalize mono integer/float PCM to finite float64 in [-1, 1]."""

    if samples.ndim != 1:
        raise AcousticFeatureError(
            "AUDIO.PCM_SHAPE",
            AcousticQualityFlag.INVALID_SAMPLE_SHAPE,
            "mono PCM must be a one-dimensional array",
        )
    if np.issubdtype(samples.dtype, np.bool_) or not (
        np.issubdtype(samples.dtype, np.integer)
        or np.issubdtype(samples.dtype, np.floating)
    ):
        raise AcousticFeatureError(
            "AUDIO.PCM_DTYPE",
            AcousticQualityFlag.UNSUPPORTED_SAMPLE_DTYPE,
            "only real integer or floating-point PCM is supported",
        )

    if np.issubdtype(samples.dtype, np.floating):
        normalized = samples.astype(np.float64, copy=True)
        if not np.all(np.isfinite(normalized)):
            raise AcousticFeatureError(
                "AUDIO.PCM_NON_FINITE",
                AcousticQualityFlag.NON_FINITE_SAMPLES,
                "PCM contains NaN or infinity",
            )
        if normalized.size and float(np.max(np.abs(normalized))) > 1.000_001:
            raise AcousticFeatureError(
                "AUDIO.PCM_RANGE",
                AcousticQualityFlag.PCM_OUT_OF_RANGE,
                "floating-point PCM must be normalized to [-1, 1]",
            )
        return np.clip(normalized, -1.0, 1.0)

    integer_pcm = samples.astype(np.float64, copy=True)
    bits = samples.dtype.itemsize * 8
    if samples.dtype.kind == "i":
        scale = float(2 ** (bits - 1))
        return integer_pcm / scale

    midpoint = float(2 ** (bits - 1))
    return (integer_pcm - midpoint) / midpoint


def extract_acoustic_features(
    samples: npt.NDArray[np.generic],
    sample_rate_hz: int,
    *,
    config: FeatureExtractionConfig | None = None,
) -> AcousticFeatures:
    """Extract bounded scalar and spectral observables from mono PCM."""

    resolved = config or FeatureExtractionConfig()
    if (
        isinstance(sample_rate_hz, bool)
        or sample_rate_hz < resolved.minimum_sample_rate_hz
        or sample_rate_hz > resolved.maximum_sample_rate_hz
    ):
        raise AcousticFeatureError(
            "AUDIO.SAMPLE_RATE",
            AcousticQualityFlag.INVALID_SAMPLE_RATE,
            "sample rate is outside the validated processing range",
        )
    if samples.size < resolved.minimum_samples:
        raise AcousticFeatureError(
            "AUDIO.WINDOW_SHORT",
            AcousticQualityFlag.WINDOW_TOO_SHORT,
            "PCM window is too short for stable spectral features",
        )
    if samples.size > resolved.maximum_samples:
        raise AcousticFeatureError(
            "AUDIO.WINDOW_LARGE",
            AcousticQualityFlag.WINDOW_TOO_LARGE,
            "PCM window exceeds the bounded processing limit",
        )

    normalized = normalize_pcm_samples(samples)
    centered = normalized - float(np.mean(normalized))
    rms = float(np.sqrt(np.mean(np.square(centered))))
    peak = float(np.max(np.abs(centered)))
    crest_factor = peak / rms if rms > np.finfo(np.float64).eps else 0.0
    clipped_fraction = float(np.mean(np.abs(normalized) >= 0.999))

    if centered.size > 1:
        signs = np.signbit(centered)
        zero_crossing_rate = float(np.mean(signs[1:] != signs[:-1]))
    else:
        zero_crossing_rate = 0.0

    windowed = centered * np.hanning(centered.size)
    spectrum = np.fft.rfft(windowed)
    power = np.square(np.abs(spectrum))
    frequencies = np.fft.rfftfreq(centered.size, d=1.0 / sample_rate_hz)
    if power.size:
        power[0] = 0.0
    total_power = float(np.sum(power))

    if total_power > np.finfo(np.float64).eps:
        dominant_frequency_hz = float(frequencies[int(np.argmax(power))])
        spectral_centroid_hz = float(np.sum(frequencies * power) / total_power)
    else:
        dominant_frequency_hz = 0.0
        spectral_centroid_hz = 0.0

    nyquist_hz = sample_rate_hz / 2.0
    definitions = (
        ("low", 0.0, min(250.0, nyquist_hz)),
        ("mid", min(250.0, nyquist_hz), min(1_000.0, nyquist_hz)),
        ("upper_mid", min(1_000.0, nyquist_hz), min(4_000.0, nyquist_hz)),
        ("high", min(4_000.0, nyquist_hz), nyquist_hz),
    )
    scale = float(centered.size * centered.size)
    bands: list[AcousticBandEnergy] = []
    for index, (name, low_hz, high_hz) in enumerate(definitions):
        if high_hz <= low_hz:
            band_power = 0.0
        else:
            upper_inclusive = index == len(definitions) - 1
            mask = (frequencies >= low_hz) & (
                frequencies <= high_hz
                if upper_inclusive
                else frequencies < high_hz
            )
            band_power = float(np.sum(power[mask]))
        bands.append(
            AcousticBandEnergy(
                name=name,
                low_hz=low_hz,
                high_hz=high_hz,
                energy=band_power / scale,
                ratio=band_power / total_power if total_power > 0.0 else 0.0,
            )
        )

    return AcousticFeatures(
        rms=rms,
        crest_factor=crest_factor,
        zero_crossing_rate=zero_crossing_rate,
        dominant_frequency_hz=dominant_frequency_hz,
        spectral_centroid_hz=spectral_centroid_hz,
        band_energy=tuple(bands),
        clipped_fraction=clipped_fraction,
        duration_seconds=centered.size / float(sample_rate_hz),
    )
