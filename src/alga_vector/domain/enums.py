from __future__ import annotations

from enum import StrEnum


class DeviceState(StrEnum):
    ABSENT = "absent"
    DISCOVERED = "discovered"
    PROBING = "probing"
    READY = "ready"
    STARTING = "starting"
    STREAMING = "streaming"
    STOPPING = "stopping"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"


class HealthLevel(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    LIVE = "live"
    REPLAYED = "replayed"
    SIMULATED = "simulated"


class Capability(StrEnum):
    SPECTRUM_SWEEP = "spectrum_sweep"
    IQ_RX = "iq_rx"
    COHERENT_IQ_RX = "coherent_iq_rx"
    HARDWARE_TIMESTAMP = "hardware_timestamp"
    TRIGGER_SOURCE = "trigger_source"
    DF_OBSERVATION = "df_observation"
    LOCAL_CAPTURE_STORAGE = "local_capture_storage"
    CLASSIFIER_MODEL = "classifier_model"


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

