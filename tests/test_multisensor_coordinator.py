from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from alga_vector.acoustics import (
    AcousticProvenance,
    AcousticProvenanceKind,
    PcmWindow,
)
from alga_vector.application.multisensor import MultiSensorCoordinator
from alga_vector.application.runtime import ApplicationRuntime
from alga_vector.config import (
    AcousticConfig,
    AdapterConfig,
    AirspaceConfig,
    AppConfig,
    DevicesConfig,
    FusionConfig,
    StorageConfig,
)
from alga_vector.sensor_fusion import (
    FusionClassification,
    FusionLifecycle,
)

FIXED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
SAMPLE_RATE = 16_000


def fixed_clock() -> datetime:
    return FIXED


def _live_window(sequence: int) -> PcmWindow:
    captured_at = FIXED + timedelta(milliseconds=sequence * 250)
    timebase = np.arange(4_000, dtype=np.float64) / SAMPLE_RATE
    samples = (
        0.18 * np.sin(2.0 * np.pi * 120.0 * timebase)
        + 0.05 * np.sin(2.0 * np.pi * 240.0 * timebase)
    )
    return PcmWindow(
        samples=samples,
        sample_rate_hz=SAMPLE_RATE,
        sequence=sequence,
        captured_at=captured_at,
        received_at=captured_at,
        provenance=AcousticProvenance(
            source_id="authorized-pcm-01",
            device_id="usb-audio-01",
            session_id="live-session-01",
            kind=AcousticProvenanceKind.LIVE_MICROPHONE,
            calibration_id="calibration-01",
        ),
    )


def test_demo_builds_explicit_simulated_correlation_after_temporal_gate() -> None:
    coordinator = MultiSensorCoordinator(
        AppConfig(
            mode="demo",
            fusion=FusionConfig(min_consecutive_observations=3),
        ),
        clock=fixed_clock,
    )

    first = coordinator.advance(
        now=FIXED,
        revision=1,
        rf_decision=None,
        direction=None,
    )
    second = coordinator.advance(
        now=FIXED,
        revision=2,
        rf_decision=None,
        direction=None,
    )
    third = coordinator.advance(
        now=FIXED,
        revision=3,
        rf_decision=None,
        direction=None,
    )

    assert not first.alertable
    assert not second.alertable
    assert third.alertable
    assert (
        third.classification
        is FusionClassification.MULTI_SENSOR_CORRELATED
    )
    assert third.lifecycle is FusionLifecycle.CONFIRMED
    assert third.calibrated_probability is None
    assert set(third.active_source_ids) >= {
        "demo-acoustic-01",
        "demo-rf-01",
    }
    assert coordinator.acoustic_assessment is not None
    assert (
        coordinator.acoustic_assessment.provenance.kind
        is AcousticProvenanceKind.SIMULATED
    )
    assert coordinator.airspace_snapshot.summary.nearby_context_available
    assert (
        coordinator.airspace_snapshot.summary.supports_friend_or_foe
        is False
    )


def test_repeated_demo_revision_does_not_fabricate_observations() -> None:
    coordinator = MultiSensorCoordinator(
        AppConfig(mode="demo"),
        clock=fixed_clock,
    )
    coordinator.advance(
        now=FIXED,
        revision=1,
        rf_decision=None,
        direction=None,
    )
    before = coordinator.fusion_decision.observation_count

    repeated = coordinator.advance(
        now=FIXED,
        revision=1,
        rf_decision=None,
        direction=None,
    )

    assert repeated.observation_count == before


def test_single_live_acoustic_modality_never_becomes_fusion_alert() -> None:
    config = AppConfig(
        mode="live",
        acoustic=AcousticConfig(
            enabled=True,
            source="external_pcm",
            source_id="authorized-pcm-01",
            sample_rate_hz=SAMPLE_RATE,
            window_seconds=0.25,
        ),
    )
    coordinator = MultiSensorCoordinator(config, clock=fixed_clock)

    for sequence in range(3):
        window = _live_window(sequence)
        assessment = coordinator.ingest_acoustic_window(window)
        decision = coordinator.advance(
            now=window.received_at,
            revision=sequence + 1,
            rf_decision=None,
            direction=None,
        )

    assert assessment.alertable
    assert not decision.alertable
    assert decision.classification is FusionClassification.ACOUSTIC_ANOMALY
    assert decision.lifecycle is FusionLifecycle.CANDIDATE


def test_safe_mode_rejects_pcm_and_remains_idle() -> None:
    coordinator = MultiSensorCoordinator(
        AppConfig(mode="safe"),
        clock=fixed_clock,
    )

    with pytest.raises(ValueError, match="safe mode"):
        coordinator.ingest_acoustic_window(_live_window(0))
    decision = coordinator.advance(
        now=FIXED,
        revision=1,
        rf_decision=None,
        direction=None,
    )

    assert decision.classification is FusionClassification.BACKGROUND
    assert decision.lifecycle is FusionLifecycle.IDLE
    assert not decision.alertable


def test_local_civil_broadcast_is_context_only_and_not_iff(
    tmp_path: Path,
) -> None:
    aircraft_json = tmp_path / "aircraft.json"
    aircraft_json.write_text(
        json.dumps(
            {
                "now": FIXED.timestamp(),
                "aircraft": [
                    {
                        "hex": "abcdef",
                        "flight": "CIV001",
                        "alt_baro": 8_000,
                        "gs": 140,
                        "track": 90,
                        "seen": 0.2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    coordinator = MultiSensorCoordinator(
        AppConfig(
            mode="live",
            airspace=AirspaceConfig(
                enabled=True,
                aircraft_json_path=aircraft_json,
                stale_after_seconds=5.0,
            ),
        ),
        clock=fixed_clock,
    )

    decision = coordinator.advance(
        now=FIXED,
        revision=1,
        rf_decision=None,
        direction=None,
    )

    assert (
        decision.classification
        is FusionClassification.NEARBY_COOPERATIVE_AIRCRAFT_CONTEXT
    )
    assert decision.lifecycle is FusionLifecycle.INFORMATIONAL
    assert not decision.alertable
    summary = coordinator.airspace_snapshot.summary
    assert summary.active_count == 1
    assert summary.context_only is True
    assert summary.supports_identity_correlation is False
    assert summary.supports_friend_or_foe is False
    assert summary.supports_threat_inference is False


def test_runtime_snapshot_exposes_demo_foundation_and_transition_log(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        AppConfig(
            mode="demo",
            first_run_complete=True,
            storage=StorageConfig(data_dir=tmp_path / "runtime"),
            devices=DevicesConfig(
                adapters=[
                    AdapterConfig(
                        id="fake-rtlsdr-01",
                        kind="rtlsdr",
                        enabled=True,
                        connection="SIM:RTLSDR",
                    )
                ]
            ),
        ),
        clock=fixed_clock,
    )

    snapshots = [runtime.snapshot(bins=32) for _ in range(3)]
    latest = snapshots[-1]

    assert latest.acoustic is not None
    assert latest.airspace is not None
    assert latest.fusion_decision is not None
    assert latest.fusion_decision.alertable
    assert latest.runtime_mode == "demo"
    log_path = runtime.logger_path
    runtime.shutdown()

    assert log_path is not None
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    fusion_transitions = [
        item
        for item in records
        if item.get("event") == "sensor_fusion.transition"
    ]
    assert len(fusion_transitions) == 1
    assert fusion_transitions[0]["context"]["classification"] == (
        "multi_sensor_correlated"
    )
