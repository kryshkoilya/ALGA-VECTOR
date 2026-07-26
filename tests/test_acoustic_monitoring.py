from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from alga_vector.acoustics import (
    ACOUSTIC_LIMITATIONS_RU,
    AcousticCoreAdapter,
    AcousticDataQuality,
    AcousticFamily,
    AcousticLifecycle,
    AcousticMonitor,
    AcousticMonitorConfig,
    AcousticProvenance,
    AcousticProvenanceKind,
    AcousticQualityFlag,
    DeterministicAcousticSource,
    PcmWindow,
    extract_acoustic_features,
    normalize_pcm_samples,
)

SAMPLE_RATE = 16_000
SAMPLES = 1_600
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
PROVENANCE = AcousticProvenance(
    source_id="authorized-lab-mic",
    device_id="usb-audio-fixture",
    session_id="session-001",
    kind=AcousticProvenanceKind.SIMULATED,
    calibration_id="fixture-cal-2026",
)


def _timebase() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    return np.arange(SAMPLES, dtype=np.float64) / SAMPLE_RATE


def _rotor_like() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    timebase = _timebase()
    return (
        0.18 * np.sin(2.0 * np.pi * 120.0 * timebase)
        + 0.05 * np.sin(2.0 * np.pi * 240.0 * timebase)
    )


def _engine_like() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    timebase = _timebase()
    return (
        0.16 * np.sin(2.0 * np.pi * 480.0 * timebase)
        + 0.05 * np.sin(2.0 * np.pi * 960.0 * timebase)
    )


def _unknown_like() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    return 0.18 * np.sin(2.0 * np.pi * 2_000.0 * _timebase())


def _broadband() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    return np.random.default_rng(2607).normal(0.0, 0.08, SAMPLES)


def _ambient() -> np.ndarray[tuple[int, ...], np.dtype[np.float64]]:
    return np.zeros(SAMPLES, dtype=np.float64)


def _window(
    sequence: int,
    samples: np.ndarray[tuple[int, ...], np.dtype[np.float64]],
    *,
    sample_rate_hz: int = SAMPLE_RATE,
    captured_at: datetime | None = None,
    received_at: datetime | None = None,
    provenance: AcousticProvenance = PROVENANCE,
    discontinuity: bool = False,
    dropped_samples: int = 0,
) -> PcmWindow:
    captured = captured_at or START + timedelta(milliseconds=sequence * 200)
    return PcmWindow(
        samples=samples,
        sample_rate_hz=sample_rate_hz,
        sequence=sequence,
        captured_at=captured,
        received_at=received_at or captured + timedelta(milliseconds=20),
        provenance=provenance,
        discontinuity=discontinuity,
        dropped_samples=dropped_samples,
    )


def test_numpy_features_measure_required_observables_and_band_energy() -> None:
    features = extract_acoustic_features(_rotor_like(), SAMPLE_RATE)

    assert features.rms == pytest.approx(0.132_098, rel=1e-4)
    assert 1.4 < features.crest_factor < 1.7
    assert 0.0 < features.zero_crossing_rate < 0.03
    assert features.dominant_frequency_hz == pytest.approx(120.0)
    assert 120.0 < features.spectral_centroid_hz < 150.0
    assert features.band_ratio("low") > 0.98
    assert {item.name for item in features.band_energy} == {
        "low",
        "mid",
        "upper_mid",
        "high",
    }
    assert features.duration_seconds == pytest.approx(0.1)


def test_integer_pcm_is_normalized_without_live_audio_dependency() -> None:
    signed = np.asarray((-32_768, 0, 32_767), dtype=np.int16)
    unsigned = np.asarray((0, 128, 255), dtype=np.uint8)

    signed_normalized = normalize_pcm_samples(signed)
    unsigned_normalized = normalize_pcm_samples(unsigned)

    assert signed_normalized.tolist() == pytest.approx((-1.0, 0.0, 32_767 / 32_768))
    assert unsigned_normalized.tolist() == pytest.approx((-1.0, 0.0, 127 / 128))


@pytest.mark.parametrize(
    ("samples", "expected"),
    (
        (_rotor_like(), AcousticFamily.ROTOR_LIKE),
        (_engine_like(), AcousticFamily.ENGINE_LIKE),
        (_broadband(), AcousticFamily.BROADBAND_ANOMALY),
        (_unknown_like(), AcousticFamily.UNKNOWN_AERIAL_LIKE),
        (_ambient(), AcousticFamily.AMBIENT_NOISE),
    ),
)
def test_general_non_attributive_families_are_deterministic(
    samples: np.ndarray[tuple[int, ...], np.dtype[np.float64]],
    expected: AcousticFamily,
) -> None:
    result = AcousticMonitor().process(_window(0, samples))

    assert result.window_family == expected
    assert result.identity_established is False
    assert result.calibrated_probability is None


def test_alert_requires_three_consecutive_matching_windows() -> None:
    monitor = AcousticMonitor()

    first = monitor.process(_window(0, _rotor_like()))
    second = monitor.process(_window(1, _rotor_like()))
    third = monitor.process(_window(2, _rotor_like()))

    assert first.lifecycle == AcousticLifecycle.CANDIDATE
    assert second.lifecycle == AcousticLifecycle.CANDIDATE
    assert first.alertable is second.alertable is False
    assert third.lifecycle == AcousticLifecycle.CONFIRMED
    assert third.alertable is True
    assert third.family == AcousticFamily.ROTOR_LIKE
    assert third.consecutive_windows == 3
    assert third.episode_id is not None


def test_single_anomalous_window_never_publishes_an_alert() -> None:
    monitor = AcousticMonitor()

    anomaly = monitor.process(_window(0, _broadband()))
    quiet = monitor.process(_window(1, _ambient()))

    assert anomaly.lifecycle == AcousticLifecycle.CANDIDATE
    assert anomaly.alertable is False
    assert quiet.lifecycle == AcousticLifecycle.IDLE
    assert quiet.alertable is False


def test_release_hysteresis_holds_then_resolves_confirmed_family() -> None:
    monitor = AcousticMonitor()
    for sequence in range(3):
        confirmed = monitor.process(_window(sequence, _engine_like()))
    assert confirmed.alertable is True

    first_quiet = monitor.process(_window(3, _ambient()))
    second_quiet = monitor.process(_window(4, _ambient()))
    resolved = monitor.process(_window(5, _ambient()))

    assert first_quiet.lifecycle == AcousticLifecycle.HOLDING
    assert second_quiet.lifecycle == AcousticLifecycle.HOLDING
    assert first_quiet.family == AcousticFamily.ENGINE_LIKE
    assert first_quiet.window_family == AcousticFamily.AMBIENT_NOISE
    assert first_quiet.alertable is second_quiet.alertable is True
    assert resolved.lifecycle == AcousticLifecycle.IDLE
    assert resolved.alertable is False
    assert resolved.episode_id is None


def test_bad_sample_rate_fails_closed_and_resets_candidate_history() -> None:
    monitor = AcousticMonitor()
    monitor.process(_window(0, _rotor_like()))
    monitor.process(_window(1, _rotor_like()))

    invalid = monitor.process(
        _window(2, _rotor_like(), sample_rate_hz=1_000)
    )
    restarted = monitor.process(_window(3, _rotor_like()))

    assert invalid.lifecycle == AcousticLifecycle.DATA_HOLD
    assert invalid.alertable is False
    assert invalid.data_quality == AcousticDataQuality.LOW
    assert AcousticQualityFlag.INVALID_SAMPLE_RATE in invalid.quality_flags
    assert restarted.lifecycle == AcousticLifecycle.CANDIDATE
    assert restarted.consecutive_windows == 1


def test_nan_samples_fail_closed_without_advancing_confirmation() -> None:
    monitor = AcousticMonitor()
    monitor.process(_window(0, _engine_like()))
    corrupted = _engine_like()
    corrupted[40] = np.nan

    held = monitor.process(_window(1, corrupted))
    restarted = monitor.process(_window(2, _engine_like()))

    assert held.lifecycle == AcousticLifecycle.DATA_HOLD
    assert AcousticQualityFlag.NON_FINITE_SAMPLES in held.quality_flags
    assert held.features is None
    assert held.alertable is False
    assert restarted.consecutive_windows == 1


def test_stale_window_fails_closed() -> None:
    captured = START
    stale = AcousticMonitor().process(
        _window(
            0,
            _rotor_like(),
            captured_at=captured,
            received_at=captured + timedelta(seconds=3),
        )
    )

    assert stale.lifecycle == AcousticLifecycle.DATA_HOLD
    assert AcousticQualityFlag.DATA_STALE in stale.quality_flags
    assert stale.alertable is False


@pytest.mark.parametrize(
    ("sequence", "discontinuity", "dropped_samples", "expected_flag"),
    (
        (2, True, 0, AcousticQualityFlag.DISCONTINUITY_REPORTED),
        (2, False, 64, AcousticQualityFlag.DROPPED_SAMPLES_REPORTED),
        (4, False, 0, AcousticQualityFlag.SEQUENCE_GAP),
    ),
)
def test_discontinuity_variants_fail_closed_and_clear_debounce(
    sequence: int,
    discontinuity: bool,
    dropped_samples: int,
    expected_flag: AcousticQualityFlag,
) -> None:
    monitor = AcousticMonitor()
    monitor.process(_window(0, _rotor_like()))
    monitor.process(_window(1, _rotor_like()))

    held = monitor.process(
        _window(
            sequence,
            _rotor_like(),
            discontinuity=discontinuity,
            dropped_samples=dropped_samples,
        )
    )
    restarted = monitor.process(_window(sequence + 1, _rotor_like()))

    assert held.lifecycle == AcousticLifecycle.DATA_HOLD
    assert expected_flag in held.quality_flags
    assert held.alertable is False
    assert restarted.lifecycle == AcousticLifecycle.CANDIDATE
    assert restarted.consecutive_windows == 1


def test_clipping_is_visible_and_blocks_temporal_confirmation() -> None:
    clipped = np.sin(2.0 * np.pi * 120.0 * _timebase())
    monitor = AcousticMonitor()

    results = [monitor.process(_window(index, clipped)) for index in range(4)]

    assert all(
        AcousticQualityFlag.CLIPPING_DETECTED in result.quality_flags
        for result in results
    )
    assert all(result.alertable is False for result in results)
    assert all(result.data_quality == AcousticDataQuality.MEDIUM for result in results)


def test_explanation_provenance_and_limitations_do_not_claim_identity() -> None:
    monitor = AcousticMonitor()
    result = None
    for sequence in range(3):
        result = monitor.process(_window(sequence, _rotor_like()))

    assert result is not None
    assert result.provenance == PROVENANCE
    assert result.evidence
    assert {item.code for item in result.evidence} >= {
        "AUDIO.RMS",
        "AUDIO.CREST",
        "AUDIO.ZCR",
        "AUDIO.DOMINANT_FREQUENCY",
        "AUDIO.SPECTRAL_CENTROID",
        "AUDIO.BAND_ENERGY",
        "AUDIO.TEMPORAL_CONFIRMATION",
    }
    assert result.limitations == ACOUSTIC_LIMITATIONS_RU
    rendered = " ".join(
        (
            result.family.value,
            result.explanation_ru,
            *result.limitations,
            *(item.explanation_ru for item in result.evidence),
        )
    ).lower()
    assert "марку" in rendered
    assert "страну" in rendered
    assert "координат" in rendered
    assert result.identity_established is False


def test_fake_source_and_core_adapter_are_finite_and_nonblocking() -> None:
    source = DeterministicAcousticSource(
        _window(sequence, _engine_like()) for sequence in range(3)
    )
    adapter = AcousticCoreAdapter(source)

    first = adapter.poll_once()
    second = adapter.poll_once()
    third = adapter.poll_once()

    assert first is not None and first.alertable is False
    assert second is not None and second.alertable is False
    assert third is not None and third.alertable is True
    assert source.remaining == 0
    assert adapter.poll_once() is None


def test_confirmation_threshold_cannot_be_reduced_below_three() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        AcousticMonitorConfig(minimum_consecutive_windows=2)
