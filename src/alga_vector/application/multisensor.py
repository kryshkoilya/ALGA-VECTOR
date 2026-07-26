"""Safe composition of acoustic, RF, direction and civil-broadcast context.

The coordinator is deliberately small: device acquisition remains owned by
the existing adapters, while this module normalizes already-authorized
observations for conservative temporal correlation.  It never infers a
physical identity, intent, nationality, range or position.
"""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np

from alga_vector.acoustics import (
    AcousticAssessment,
    AcousticDataQuality,
    AcousticMonitor,
    AcousticMonitorConfig,
    AcousticProvenance,
    AcousticProvenanceKind,
    PcmWindow,
)
from alga_vector.airspace import (
    AirspaceDataQuality,
    CivilAirspacePolicy,
    CivilAirspaceService,
    CivilAirspaceSnapshot,
    DeterministicCivilAirspaceSource,
)
from alga_vector.config import AppConfig
from alga_vector.direction import DirectionSnapshot, DirectionSource
from alga_vector.domain.models import utc_now
from alga_vector.sensor_fusion import FusionConfig as CoreFusionConfig
from alga_vector.sensor_fusion import (
    FusionDecision,
    FusionObservation,
    FusionTransition,
    SensorFusionEngine,
    SensorModality,
)
from alga_vector.signal_analysis import DataQuality, RfDecision

Clock = Callable[[], datetime]

_DEMO_SAMPLE_RATE_HZ = 16_000
_DEMO_WINDOW_SECONDS = 0.25
_DEMO_STEP = timedelta(milliseconds=250)
_FUSION_STEP = timedelta(milliseconds=1)


class MultiSensorCoordinator:
    """Normalize independent sensor results and maintain one fusion state."""

    def __init__(
        self,
        config: AppConfig,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self.config = config
        self._clock = clock
        initial_now = clock()
        _require_aware(initial_now, "clock")
        self._service_now = initial_now
        self._acoustic_monitor = AcousticMonitor(
            AcousticMonitorConfig(
                minimum_consecutive_windows=(
                    config.fusion.min_consecutive_observations
                )
            )
        )
        self._airspace_service = CivilAirspaceService(
            CivilAirspacePolicy(
                aircraft_ttl_s=config.airspace.stale_after_seconds,
                feed_ttl_s=config.airspace.stale_after_seconds,
            ),
            clock=self._airspace_clock,
        )
        self._fusion_engine = SensorFusionEngine(
            CoreFusionConfig(
                temporal_window_seconds=config.fusion.window_seconds,
                direction_freshness_seconds=min(
                    config.fusion.window_seconds,
                    5.0,
                ),
                civil_adsb_context_seconds=min(
                    config.fusion.window_seconds,
                    config.airspace.stale_after_seconds,
                ),
                minimum_observations=(
                    config.fusion.min_consecutive_observations
                ),
                release_hold_seconds=config.fusion.hold_seconds,
                candidate_timeout_seconds=config.fusion.window_seconds,
                debounce_seconds=max(2.0, config.fusion.hold_seconds),
            )
        )
        initial_update = self._fusion_engine.tick(initial_now)
        self._fusion_decision = initial_update.decision
        self._last_transition: FusionTransition | None = None
        self._acoustic_assessment: AcousticAssessment | None = None
        self._airspace_snapshot = self._airspace_service.snapshot(
            now=initial_now
        )
        self._fusion_time = initial_now
        self._demo_acoustic_at = initial_now - _DEMO_STEP
        self._demo_sequence = 0
        self._last_demo_revision = 0
        self._last_rf_key: tuple[object, ...] | None = None
        self._last_acoustic_key: tuple[object, ...] | None = None
        self._last_direction_key: tuple[object, ...] | None = None
        self._last_airspace_key: tuple[object, ...] | None = None

    @property
    def acoustic_assessment(self) -> AcousticAssessment | None:
        return self._acoustic_assessment

    @property
    def airspace_snapshot(self) -> CivilAirspaceSnapshot:
        return self._airspace_snapshot

    @property
    def fusion_decision(self) -> FusionDecision:
        return self._fusion_decision

    @property
    def last_transition(self) -> FusionTransition | None:
        return self._last_transition

    def ingest_acoustic_window(
        self,
        window: PcmWindow,
    ) -> AcousticAssessment:
        """Process one explicitly supplied PCM window.

        This boundary never opens a microphone.  A live adapter must be
        configured explicitly and must provide matching source metadata.
        """

        if self.config.mode == "safe":
            raise ValueError("acoustic ingestion is disabled in safe mode")
        if self.config.mode != "demo":
            if (
                not self.config.acoustic.enabled
                or self.config.acoustic.source != "external_pcm"
            ):
                raise ValueError(
                    "external PCM ingestion is not enabled in the profile"
                )
            if (
                window.provenance.kind
                is AcousticProvenanceKind.SIMULATED
            ):
                raise ValueError(
                    "simulated acoustic provenance is accepted only in demo mode"
                )
            if window.provenance.source_id != self.config.acoustic.source_id:
                raise ValueError(
                    "PCM source_id does not match the configured source"
                )
            if window.sample_rate_hz != self.config.acoustic.sample_rate_hz:
                raise ValueError(
                    "PCM sample rate does not match the configured source"
                )
        assessment = self._acoustic_monitor.process(window)
        self._acoustic_assessment = assessment
        return assessment

    def refresh_airspace_file(self) -> CivilAirspaceSnapshot:
        """Refresh only the configured local ``aircraft.json`` source."""

        now = self._clock()
        _require_aware(now, "clock")
        self._service_now = now
        if self.config.mode == "safe":
            self._airspace_snapshot = self._airspace_service.snapshot(now=now)
        elif self.config.mode == "demo":
            source = DeterministicCivilAirspaceSource()
            self._airspace_snapshot = self._airspace_service.ingest_payload(
                source.read_payload(now)
            )
        elif (
            self.config.airspace.enabled
            and self.config.airspace.aircraft_json_path is not None
        ):
            self._airspace_snapshot = self._airspace_service.ingest_file(
                self.config.airspace.aircraft_json_path
            )
        else:
            self._airspace_snapshot = self._airspace_service.snapshot(now=now)
        return self._airspace_snapshot

    def advance(
        self,
        *,
        now: datetime,
        revision: int,
        rf_decision: RfDecision | None,
        direction: DirectionSnapshot | None,
    ) -> FusionDecision:
        """Advance freshness and correlation without fabricating live data."""

        _require_aware(now, "now")
        if revision < 0:
            raise ValueError("revision must be non-negative")
        self._last_transition = None
        if self.config.mode == "demo":
            self._advance_demo(now, revision)
        elif self.config.mode == "live":
            self._advance_live(now, rf_decision, direction)
        else:
            self._tick(now)
        return self._fusion_decision

    def _advance_demo(self, now: datetime, revision: int) -> None:
        if revision <= self._last_demo_revision:
            self._tick(now)
            return
        self._last_demo_revision = revision
        acoustic_at = max(now, self._demo_acoustic_at + _DEMO_STEP)
        self._demo_acoustic_at = acoustic_at
        self._service_now = acoustic_at

        window = _demo_acoustic_window(
            sequence=self._demo_sequence,
            captured_at=acoustic_at,
        )
        self._demo_sequence += 1
        assessment = self.ingest_acoustic_window(window)
        self._ingest(
            FusionObservation(
                modality=SensorModality.ACOUSTIC,
                timestamp=self._next_fusion_time(acoustic_at),
                source_id="demo-acoustic-01",
                quality=_acoustic_quality(assessment.data_quality),
                strength=(
                    max(0.70, assessment.heuristic_score)
                    if assessment.alertable
                    else min(0.39, assessment.heuristic_score)
                ),
                summary=(
                    "Демо: устойчивая акустическая форма прошла временное "
                    "подтверждение."
                    if assessment.alertable
                    else "Демо: акустическая форма ещё накапливает подтверждение."
                ),
                evidence_keys=("demo-generic-activity",),
            )
        )
        self._ingest(
            FusionObservation(
                modality=SensorModality.RF,
                timestamp=self._next_fusion_time(acoustic_at),
                source_id="demo-rf-01",
                quality=0.92,
                strength=0.84,
                summary=(
                    "Демо: устойчивый generic RF-эпизод без атрибуции "
                    "физического источника."
                ),
                evidence_keys=("demo-generic-activity",),
            )
        )

        source = DeterministicCivilAirspaceSource()
        self._airspace_snapshot = self._airspace_service.ingest_payload(
            source.read_payload(acoustic_at)
        )
        if self._airspace_snapshot.summary.nearby_context_available:
            self._ingest(
                FusionObservation(
                    modality=SensorModality.CIVIL_ADSB,
                    timestamp=self._next_fusion_time(acoustic_at),
                    source_id="demo-civil-adsb",
                    quality=0.85,
                    strength=0.60,
                    summary=(
                        "Демо: доступен отдельный контекст гражданского "
                        "кооперативного вещания; это не IFF."
                    ),
                    evidence_keys=("demo-civil-broadcast-context",),
                )
            )
        self._tick(self._fusion_time)

    def _advance_live(
        self,
        now: datetime,
        rf_decision: RfDecision | None,
        direction: DirectionSnapshot | None,
    ) -> None:
        self._service_now = now
        if (
            self.config.airspace.enabled
            and self.config.airspace.aircraft_json_path is not None
        ):
            self._airspace_snapshot = self._airspace_service.ingest_file(
                self.config.airspace.aircraft_json_path
            )
        else:
            self._airspace_snapshot = self._airspace_service.snapshot(now=now)

        if rf_decision is not None and rf_decision.alertable:
            rf_key = (
                rf_decision.source_id,
                rf_decision.observed_at,
                rf_decision.episode_id,
                rf_decision.lifecycle,
                rf_decision.heuristic_score,
            )
            if rf_key != self._last_rf_key:
                self._last_rf_key = rf_key
                self._ingest(
                    FusionObservation(
                        modality=SensorModality.RF,
                        timestamp=self._next_fusion_time(now),
                        source_id=rf_decision.source_id,
                        quality=_rf_quality(rf_decision.data_quality),
                        strength=rf_decision.heuristic_score,
                        summary=rf_decision.family_explanation_ru,
                        evidence_keys=(
                            (rf_decision.episode_id,)
                            if rf_decision.episode_id is not None
                            else ()
                        ),
                    )
                )

        acoustic = self._acoustic_assessment
        if acoustic is not None and acoustic.alertable:
            acoustic_key = (
                acoustic.provenance.source_id,
                acoustic.observed_at,
                acoustic.episode_id,
                acoustic.lifecycle,
                acoustic.consecutive_windows,
            )
            if acoustic_key != self._last_acoustic_key:
                self._last_acoustic_key = acoustic_key
                self._ingest(
                    FusionObservation(
                        modality=SensorModality.ACOUSTIC,
                        timestamp=self._next_fusion_time(now),
                        source_id=acoustic.provenance.source_id,
                        quality=_acoustic_quality(acoustic.data_quality),
                        strength=acoustic.heuristic_score,
                        summary=acoustic.explanation_ru,
                        evidence_keys=(
                            (acoustic.episode_id,)
                            if acoustic.episode_id is not None
                            else ()
                        ),
                    )
                )

        if direction is not None and direction.available:
            current = direction.current
            evidence = current.evidence
            direction_key = (
                current.source_id,
                current.captured_at,
                current.bearing_deg,
                current.uncertainty_deg,
            )
            validated = (
                current.source is DirectionSource.EXTERNAL
                and evidence is not None
                and evidence.calibration_valid
            )
            if (
                validated
                and evidence is not None
                and direction_key != self._last_direction_key
            ):
                self._last_direction_key = direction_key
                quality = current.confidence or evidence.quality_score
                self._ingest(
                    FusionObservation(
                        modality=SensorModality.DIRECTION,
                        timestamp=self._next_fusion_time(now),
                        source_id=current.source_id,
                        quality=quality,
                        strength=quality,
                        summary=(
                            "Доступен свежий валидированный угловой контекст; "
                            "он не подтверждает класс физического источника."
                        ),
                        evidence_keys=(evidence.calibration_id,),
                        validated=True,
                    )
                )

        summary = self._airspace_snapshot.summary
        airspace_key = (
            summary.source_generated_at,
            summary.active_count,
            summary.state,
        )
        if (
            summary.nearby_context_available
            and airspace_key != self._last_airspace_key
        ):
            self._last_airspace_key = airspace_key
            self._ingest(
                FusionObservation(
                    modality=SensorModality.CIVIL_ADSB,
                    timestamp=self._next_fusion_time(now),
                    source_id="local-civil-adsb",
                    quality=_airspace_quality(summary.data_quality),
                    strength=0.60,
                    summary=(
                        "Есть свежий контекст гражданского кооперативного "
                        "вещания; он не связан с другой аномалией и не является IFF."
                    ),
                    evidence_keys=("civil-broadcast-context",),
                )
            )
        self._tick(now)

    def _ingest(self, observation: FusionObservation) -> None:
        update = self._fusion_engine.process(observation)
        self._fusion_decision = update.decision
        if update.transition is not None:
            self._last_transition = update.transition

    def _tick(self, now: datetime) -> None:
        tick_at = self._next_fusion_time(now)
        update = self._fusion_engine.tick(tick_at)
        self._fusion_decision = update.decision
        if update.transition is not None:
            self._last_transition = update.transition

    def _next_fusion_time(self, candidate: datetime) -> datetime:
        _require_aware(candidate, "candidate")
        self._fusion_time = max(candidate, self._fusion_time + _FUSION_STEP)
        return self._fusion_time

    def _airspace_clock(self) -> datetime:
        return self._service_now


def _demo_acoustic_window(
    *,
    sequence: int,
    captured_at: datetime,
) -> PcmWindow:
    sample_count = round(_DEMO_SAMPLE_RATE_HZ * _DEMO_WINDOW_SECONDS)
    timebase = np.arange(sample_count, dtype=np.float64) / _DEMO_SAMPLE_RATE_HZ
    samples = (
        0.18 * np.sin(2.0 * np.pi * 120.0 * timebase)
        + 0.05 * np.sin(2.0 * np.pi * 240.0 * timebase)
    )
    return PcmWindow(
        samples=samples,
        sample_rate_hz=_DEMO_SAMPLE_RATE_HZ,
        sequence=sequence,
        captured_at=captured_at,
        received_at=captured_at,
        provenance=AcousticProvenance(
            source_id="demo-acoustic-01",
            device_id="demo-acoustic-adapter",
            session_id="demo-session-01",
            kind=AcousticProvenanceKind.SIMULATED,
            calibration_id="demo-fixture-v1",
        ),
    )


def _rf_quality(value: DataQuality) -> float:
    return {
        DataQuality.LOW: 0.35,
        DataQuality.MEDIUM: 0.72,
        DataQuality.HIGH: 0.94,
    }[value]


def _acoustic_quality(value: AcousticDataQuality) -> float:
    return {
        AcousticDataQuality.LOW: 0.30,
        AcousticDataQuality.MEDIUM: 0.70,
        AcousticDataQuality.HIGH: 0.94,
    }[value]


def _airspace_quality(value: AirspaceDataQuality) -> float:
    return {
        AirspaceDataQuality.UNAVAILABLE: 0.0,
        AirspaceDataQuality.LIMITED: 0.58,
        AirspaceDataQuality.PARTIAL: 0.72,
        AirspaceDataQuality.GOOD: 0.92,
    }[value]


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["MultiSensorCoordinator"]
