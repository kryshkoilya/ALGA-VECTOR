from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

from .enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
    Provenance,
)

if TYPE_CHECKING:
    from alga_vector.acoustics import AcousticAssessment
    from alga_vector.airspace import CivilAirspaceSnapshot
    from alga_vector.application.rf_scan import ScanRuntimeStatus
    from alga_vector.sensor_fusion import FusionDecision
    from alga_vector.signal_analysis import RfDecision, SignalAssessment
    from alga_vector.signal_processor import NormalizedEvent, OperatorSituation


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, frozen=True)
class DeviceSnapshot:
    device_id: str
    display_name: str
    kind: str
    connection: str
    state: DeviceState
    health: HealthLevel
    capabilities: frozenset[Capability] = frozenset()
    driver: str = "N/A"
    sample_rate_hz: int | None = None
    center_frequency_hz: int | None = None
    last_data_at: datetime | None = None
    reason_code: str | None = None
    reason_ru: str | None = None
    recommended_action_ru: str | None = None
    generation: int = 0
    metrics: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CapabilityStatus:
    capability: Capability
    state: CapabilityState
    reason_code: str | None = None
    explanation_ru: str | None = None
    action_ru: str | None = None


@dataclass(slots=True, frozen=True)
class SpectrumFrame:
    source_id: str
    sequence: int
    center_frequency_hz: int
    span_hz: int
    power_dbm: NDArray[np.float32]
    captured_at: datetime
    provenance: Provenance
    unit: str = "dBFS"
    calibration_id: str | None = None
    uncertainty_db: float | None = None
    dropped_frames: int = 0
    data_age_ms: int = 0

    @property
    def peak_level(self) -> float:
        return float(np.max(self.power_dbm))

    @property
    def peak_dbm(self) -> float:
        """Compatibility alias; interpret the value using :attr:`unit`."""

        return self.peak_level


@dataclass(slots=True, frozen=True)
class Incident:
    incident_id: str
    code: str
    title_ru: str
    message_ru: str
    action_ru: str
    severity: IncidentSeverity
    source: str
    occurred_at: datetime = field(default_factory=utc_now)
    acknowledged: bool = False
    technical: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SystemSnapshot:
    revision: int
    devices: tuple[DeviceSnapshot, ...]
    capabilities: tuple[CapabilityStatus, ...]
    incidents: tuple[Incident, ...]
    spectrum: SpectrumFrame | None
    mode: Provenance
    profile_name: str
    readiness_percent: int
    runtime_mode: str = "live"
    experience_level: str = "guided"
    location: object | None = None
    map_status: object | None = None
    direction: object | None = None
    acoustic: AcousticAssessment | None = None
    airspace: CivilAirspaceSnapshot | None = None
    fusion_decision: FusionDecision | None = None
    scan_plan: ScanRuntimeStatus | None = None
    signal_events: tuple[RfDecision, ...] = ()
    signal_assessment: SignalAssessment | None = None
    signal_decision: RfDecision | None = None
    operator_situation: OperatorSituation | None = None
    normalized_events: tuple[NormalizedEvent, ...] = ()
    captured_at: datetime = field(default_factory=utc_now)
