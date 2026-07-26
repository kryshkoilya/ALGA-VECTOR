from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alga_vector.direction import (
    DirectionPolicy,
    DirectionQuality,
    DirectionService,
    DirectionSource,
    ExternalDirectionEvidence,
)


def _evidence(
    now: datetime,
    *,
    valid: bool = True,
    samples: int = 5,
    quality: float = 0.90,
    evidence_age_s: float = 0.0,
    calibration_age_s: float = 30.0,
) -> ExternalDirectionEvidence:
    return ExternalDirectionEvidence(
        calibration_id="cal-2026-07-26-a",
        calibrated_at=now - timedelta(seconds=calibration_age_s),
        evidence_at=now - timedelta(seconds=evidence_age_s),
        sample_count=samples,
        quality_score=quality,
        calibration_valid=valid,
    )


def test_manual_direction_is_explicitly_unmeasured_and_normalized() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    service = DirectionService(clock=lambda: now)

    snapshot = service.set_manual(
        725.0,
        uncertainty_deg=20.0,
        source_id="operator:main",
    )

    assert snapshot.available
    assert snapshot.current.source is DirectionSource.MANUAL
    assert snapshot.current.bearing_deg == pytest.approx(5.0)
    assert snapshot.current.uncertainty_deg == pytest.approx(20.0)
    assert snapshot.current.confidence is None
    assert snapshot.current.quality is DirectionQuality.UNMEASURED
    assert snapshot.current.operator_entered
    assert not snapshot.current.measured
    assert "оператором" in snapshot.current.message_ru
    assert len(snapshot.trail) == 1


def test_external_direction_requires_fresh_calibration_and_evidence() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    service = DirectionService(clock=lambda: now)

    accepted = service.ingest_external(
        91.5,
        uncertainty_deg=7.0,
        confidence=0.86,
        captured_at=now,
        source_id="df-array-01",
        evidence=_evidence(now),
    )

    assert accepted.available
    assert accepted.current.source is DirectionSource.EXTERNAL
    assert accepted.current.measured
    assert accepted.current.confidence == pytest.approx(0.86)
    assert accepted.current.quality is DirectionQuality.HIGH
    assert accepted.current.evidence is not None
    assert accepted.current.evidence.calibration_id == "cal-2026-07-26-a"

    rejected = service.ingest_external(
        93.0,
        uncertainty_deg=8.0,
        confidence=0.90,
        captured_at=now,
        source_id="df-array-01",
        evidence=_evidence(now, valid=False),
    )
    assert not rejected.available
    assert rejected.current.reason_code == "DIRECTION.CALIBRATION_INVALID"
    assert rejected.current.bearing_deg is None
    assert len(rejected.trail) == 1


@pytest.mark.parametrize(
    ("evidence", "expected_code"),
    [
        (
            lambda now: _evidence(now, samples=1),
            "DIRECTION.EVIDENCE_INSUFFICIENT",
        ),
        (
            lambda now: _evidence(now, quality=0.20),
            "DIRECTION.EVIDENCE_LOW_QUALITY",
        ),
        (
            lambda now: _evidence(now, evidence_age_s=10.0),
            "DIRECTION.EVIDENCE_STALE",
        ),
        (
            lambda now: _evidence(now, calibration_age_s=1_000.0),
            "DIRECTION.CALIBRATION_STALE",
        ),
    ],
)
def test_external_direction_rejects_weak_or_stale_evidence(
    evidence: object,
    expected_code: str,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    service = DirectionService(clock=lambda: now)
    build_evidence = evidence
    assert callable(build_evidence)

    snapshot = service.ingest_external(
        180.0,
        uncertainty_deg=10.0,
        confidence=0.90,
        captured_at=now,
        source_id="df-array-01",
        evidence=build_evidence(now),
    )

    assert not snapshot.available
    assert snapshot.current.reason_code == expected_code
    assert snapshot.current.bearing_deg is None


def test_simulation_is_blocked_outside_demo_and_labelled_inside_demo() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

    blocked = DirectionService(clock=lambda: now).set_simulated(45.0)
    assert not blocked.available
    assert blocked.current.reason_code == "DIRECTION.SIMULATION_BLOCKED"

    demo = DirectionService(demo_mode=True, clock=lambda: now).set_simulated(45.0)
    assert demo.available
    assert demo.current.source is DirectionSource.SIMULATED
    assert demo.current.quality is DirectionQuality.SIMULATED
    assert not demo.current.measured
    assert "демо-режиме" in demo.current.message_ru


def test_stale_external_sample_hides_active_ray_but_keeps_bounded_trail() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    policy = DirectionPolicy(external_max_age_s=2.0, history_limit=2)
    service = DirectionService(policy, clock=lambda: now)
    service.ingest_external(
        10.0,
        uncertainty_deg=5.0,
        confidence=0.80,
        captured_at=now,
        source_id="df-array-01",
        evidence=_evidence(now),
    )

    stale = service.snapshot(now=now + timedelta(seconds=3))

    assert not stale.available
    assert stale.stale
    assert stale.current.reason_code == "DIRECTION.STALE"
    assert stale.current.bearing_deg is None
    assert stale.last_valid_at == now
    assert stale.age_s == pytest.approx(3.0)
    assert [item.bearing_deg for item in stale.trail] == [10.0]
    later = service.snapshot(now=now + timedelta(seconds=7))
    assert later.stale
    assert later.age_s == pytest.approx(7.0)


def test_history_is_bounded_and_contains_only_accepted_observations() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    service = DirectionService(
        DirectionPolicy(history_limit=3),
        clock=lambda: now,
    )

    for bearing in (5.0, 15.0, 25.0, 35.0):
        snapshot = service.set_manual(bearing)

    assert [item.bearing_deg for item in snapshot.trail] == [15.0, 25.0, 35.0]


def test_direction_models_reject_naive_time_and_invalid_uncertainty() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    service = DirectionService(clock=lambda: now)

    with pytest.raises(ValueError, match="timezone-aware"):
        service.set_manual(
            10.0,
            captured_at=datetime(2026, 7, 26, 10, 0),
        )
    with pytest.raises(ValueError, match="uncertainty_deg"):
        service.set_manual(10.0, uncertainty_deg=181.0)
    with pytest.raises(ValueError, match="bearing_deg"):
        service.set_manual(float("nan"))


def test_old_manual_input_and_old_demo_frame_fail_closed() -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    policy = DirectionPolicy(manual_max_age_s=20.0, simulated_max_age_s=2.0)

    manual = DirectionService(policy, clock=lambda: now).set_manual(
        10.0,
        captured_at=now - timedelta(seconds=21),
    )
    assert not manual.available
    assert manual.current.reason_code == "DIRECTION.MANUAL_STALE"

    demo = DirectionService(
        policy,
        demo_mode=True,
        clock=lambda: now,
    ).set_simulated(
        10.0,
        captured_at=now - timedelta(seconds=3),
    )
    assert not demo.available
    assert demo.current.reason_code == "DIRECTION.SIMULATION_STALE"
