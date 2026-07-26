from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
)
from alga_vector.domain.models import CapabilityStatus, DeviceSnapshot, Incident
from alga_vector.observability import HealthAggregator, JsonlRotatingLogger


def test_health_aggregation_reports_partial_capability_degradation() -> None:
    devices = (
        DeviceSnapshot(
            device_id="tiny",
            display_name="TinySA",
            kind="tinysa",
            connection="SIM:TINYSA",
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
        ),
        DeviceSnapshot(
            device_id="kraken",
            display_name="KrakenSDR",
            kind="krakensdr",
            connection="redacted",
            state=DeviceState.ABSENT,
            health=HealthLevel.UNKNOWN,
            capabilities=frozenset({Capability.DF_OBSERVATION}),
            reason_ru="KrakenSDR не обнаружен.",
        ),
    )
    capabilities = (
        CapabilityStatus(Capability.SPECTRUM_SWEEP, CapabilityState.AVAILABLE),
        CapabilityStatus(Capability.IQ_RX, CapabilityState.AVAILABLE),
        CapabilityStatus(
            Capability.DF_OBSERVATION,
            CapabilityState.BLOCKED,
            explanation_ru="Пеленгация недоступна.",
        ),
    )
    incidents = (
        Incident(
            incident_id="i-1",
            code="DEVICE.ABSENT",
            title_ru="Ограничение",
            message_ru="KrakenSDR не обнаружен.",
            action_ru="Проверьте подключение.",
            severity=IncidentSeverity.WARNING,
            source="kraken",
        ),
    )

    result = HealthAggregator(
        {
            Capability.SPECTRUM_SWEEP,
            Capability.IQ_RX,
            Capability.DF_OBSERVATION,
        }
    ).aggregate(devices, capabilities, incidents)

    assert result.level == HealthLevel.DEGRADED
    assert result.readiness_percent == 67
    assert result.healthy_devices == 1
    assert result.unavailable_devices == 1
    assert "Пеленгация недоступна." in result.reasons_ru


def test_jsonl_logger_writes_valid_unicode_and_rotates(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "runtime.jsonl"
    logger = JsonlRotatingLogger(path, max_bytes=420, max_files=3)
    for index in range(20):
        logger.event(
            "test.message",
            f"Проверка события {index}",
            sequence=index,
            captured_at=datetime(2026, 7, 25, 10, index % 60, tzinfo=UTC),
            padding="x" * 80,
        )
    logger.close()

    files = sorted(path.parent.glob("runtime.jsonl*"))
    assert 1 < len(files) <= 3
    parsed = []
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            parsed.append(json.loads(line))
    assert parsed
    assert all(item["event"] == "test.message" for item in parsed)
    assert any("Проверка события" in item["message"] for item in parsed)
