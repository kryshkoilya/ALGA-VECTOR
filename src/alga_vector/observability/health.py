from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
)
from alga_vector.domain.models import CapabilityStatus, DeviceSnapshot, Incident

_CAPABILITY_SCORE = {
    CapabilityState.AVAILABLE: 1.0,
    CapabilityState.DEGRADED: 0.5,
    CapabilityState.BLOCKED: 0.0,
}


@dataclass(slots=True, frozen=True)
class HealthSummary:
    level: HealthLevel
    readiness_percent: int
    healthy_devices: int
    degraded_devices: int
    unavailable_devices: int
    available_capabilities: int
    degraded_capabilities: int
    blocked_capabilities: int
    reasons_ru: tuple[str, ...] = ()


class HealthAggregator:
    """Turns independent device/capability state into an honest system status."""

    def __init__(self, required_capabilities: Iterable[Capability] = ()) -> None:
        self.required_capabilities = frozenset(required_capabilities)

    def aggregate(
        self,
        devices: Iterable[DeviceSnapshot],
        capabilities: Iterable[CapabilityStatus],
        incidents: Iterable[Incident] = (),
    ) -> HealthSummary:
        device_tuple = tuple(devices)
        capability_tuple = tuple(capabilities)
        incident_tuple = tuple(incidents)
        capability_by_id = {status.capability: status for status in capability_tuple}
        required = self.required_capabilities or frozenset(capability_by_id)
        required_statuses = [capability_by_id.get(capability) for capability in required]

        if required_statuses:
            score = sum(
                _CAPABILITY_SCORE[status.state] if status is not None else 0.0
                for status in required_statuses
            )
            readiness = round(100 * score / len(required_statuses))
        elif device_tuple:
            readiness = round(
                100
                * sum(
                    1.0
                    if item.health == HealthLevel.HEALTHY
                    else 0.5
                    if item.health == HealthLevel.DEGRADED
                    else 0.0
                    for item in device_tuple
                )
                / len(device_tuple)
            )
        else:
            readiness = 0

        healthy_devices = sum(item.health == HealthLevel.HEALTHY for item in device_tuple)
        degraded_devices = sum(
            item.health == HealthLevel.DEGRADED or item.state in _TRANSITIONAL_STATES
            for item in device_tuple
        )
        unavailable_devices = len(device_tuple) - healthy_devices - degraded_devices
        available_capabilities = sum(
            item.state == CapabilityState.AVAILABLE for item in capability_tuple
        )
        degraded_capabilities = sum(
            item.state == CapabilityState.DEGRADED for item in capability_tuple
        )
        blocked_capabilities = sum(
            item.state == CapabilityState.BLOCKED for item in capability_tuple
        )

        required_blocked = [
            status
            for status in required_statuses
            if status is None or status.state == CapabilityState.BLOCKED
        ]
        all_required_blocked = bool(required_statuses) and len(required_blocked) == len(
            required_statuses
        )
        critical = any(
            incident.severity == IncidentSeverity.CRITICAL for incident in incident_tuple
        )
        errors = any(
            incident.severity == IncidentSeverity.ERROR for incident in incident_tuple
        ) or any(item.health == HealthLevel.ERROR for item in device_tuple)
        warnings = any(
            incident.severity == IncidentSeverity.WARNING for incident in incident_tuple
        )
        degraded = bool(required_blocked) or any(
            status is not None and status.state == CapabilityState.DEGRADED
            for status in required_statuses
        )

        if not device_tuple and not capability_tuple:
            level = HealthLevel.UNKNOWN
        elif critical or all_required_blocked:
            level = HealthLevel.ERROR
        elif errors or warnings or degraded:
            level = HealthLevel.DEGRADED
        else:
            level = HealthLevel.HEALTHY

        reasons: list[str] = []
        for status in required_statuses:
            if (
                status is not None
                and status.state != CapabilityState.AVAILABLE
                and status.explanation_ru
                and status.explanation_ru not in reasons
            ):
                reasons.append(status.explanation_ru)
        for device in device_tuple:
            if (
                device.health != HealthLevel.HEALTHY
                and device.reason_ru
                and device.reason_ru not in reasons
            ):
                reasons.append(device.reason_ru)

        return HealthSummary(
            level=level,
            readiness_percent=max(0, min(100, readiness)),
            healthy_devices=healthy_devices,
            degraded_devices=degraded_devices,
            unavailable_devices=max(0, unavailable_devices),
            available_capabilities=available_capabilities,
            degraded_capabilities=degraded_capabilities,
            blocked_capabilities=blocked_capabilities,
            reasons_ru=tuple(reasons),
        )


_TRANSITIONAL_STATES = {
    DeviceState.DISCOVERED,
    DeviceState.PROBING,
    DeviceState.STARTING,
    DeviceState.STOPPING,
    DeviceState.DEGRADED,
    DeviceState.RECONNECTING,
}
