from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import AppConfig, StorageConfig
from alga_vector.devices import DeviceManager
from alga_vector.domain.models import SystemSnapshot
from alga_vector.signal_processor import (
    ConfidenceScore,
    EventSeverity,
    NormalizedEvent,
    NormalizedEventType,
    OperatorSituation,
    SensorKind,
    SourceAttribution,
    UnifiedSignalProcessor,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _runtime(
    data_dir: Path,
    *,
    processor: UnifiedSignalProcessor | None = None,
) -> ApplicationRuntime:
    return ApplicationRuntime(
        AppConfig(
            mode="live",
            first_run_complete=True,
            storage=StorageConfig(data_dir=data_dir),
        ),
        device_manager=DeviceManager(()),
        signal_processor=processor,
        clock=lambda: NOW,
        background_acquisition=False,
    )


def test_runtime_snapshot_exposes_one_interpreted_operator_contract(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "normal")
    try:
        first = runtime.snapshot()

        assert first.operator_situation is not None
        assert first.operator_situation.mode.value == "silence"
        assert "RF-приёмник" in first.operator_situation.headline_ru
        assert first.normalized_events
        assert (
            first.normalized_events[0].event_type
            is NormalizedEventType.SENSOR_UNAVAILABLE
        )
        assert runtime.operator_event_bus.recent(limit=1) == (
            first.normalized_events[0],
        )

        external = NormalizedEvent(
            schema_version="1.0",
            event_id="external-rf-observation-1",
            event_type=NormalizedEventType.RADIO_ACTIVITY_DETECTED,
            observed_at=NOW,
            received_at=NOW,
            valid_until=NOW + timedelta(seconds=20),
            severity=EventSeverity.WARNING,
            confidence=ConfidenceScore.heuristic(
                0.78,
                "Сила повторяемых признаков; не вероятность типа объекта.",
            ),
            summary_ru="Обнаружена RF-активность на 433,920 МГц",
            explanation_ru=(
                "Внешний адаптер передал нормализованное наблюдение; "
                "физический источник не установлен."
            ),
            recommendation_ru="Продолжайте наблюдение.",
            sources=(
                SourceAttribution(
                    sensor_id="external-rf-01",
                    sensor_kind=SensorKind.RF_SPECTRUM,
                    contribution=0.78,
                    independent_confirmation=False,
                    explanation_ru="Внешний нормализованный RF-источник.",
                ),
            ),
            frequency_hz=433_920_000.0,
            tags=("rf", "external"),
        )
        result = runtime.ingest_normalized_event(external)
        second = runtime.snapshot()

        assert result.accepted
        assert second.operator_situation is not None
        assert second.operator_situation.mode.value == "activity"
        assert second.operator_situation.primary_event is not None
        assert second.operator_situation.primary_event.event_id == external.event_id
        assert second.current_target is not None
        assert second.current_target in second.targets
        assert second.sensor_readiness is not None
        assert len(second.sensor_readiness.sensors) == 7
        assert all(
            item.event_type
            not in {
                NormalizedEventType.LIKELY_DRONE_SIGNATURE,
                NormalizedEventType.TARGET_CONFIRMED,
            }
            for item in second.normalized_events
        )
    finally:
        runtime.shutdown()


def test_interface_mode_switch_keeps_the_same_measurement_backend(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "mode-switch")
    bus = runtime.operator_event_bus
    try:
        result = runtime.set_experience_level("expert")
        snapshot = runtime.snapshot()
    finally:
        runtime.shutdown()

    assert result == "EXPERT MODE включён."
    assert snapshot.experience_level == "expert"
    assert runtime.operator_event_bus is bus
    assert snapshot.operator_situation is not None


class _BrokenProcessor(UnifiedSignalProcessor):
    def process_snapshot(
        self,
        snapshot: SystemSnapshot,
        *,
        additional_events: tuple[NormalizedEvent, ...] = (),
        important_only: bool = False,
    ) -> OperatorSituation:
        del snapshot, additional_events, important_only
        raise RuntimeError("processor failed for test")


def test_runtime_makes_processor_failure_visible_without_raw_fallback(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "broken",
        processor=_BrokenProcessor(),
    )
    try:
        snapshot = runtime.snapshot()
    finally:
        runtime.shutdown()

    assert snapshot.operator_situation is None
    assert snapshot.normalized_events == ()
    assert snapshot.readiness_percent <= 75
    incident = next(
        item
        for item in snapshot.incidents
        if item.code == "SIGNAL_PROCESSOR.FAILED"
    )
    assert incident.severity.value == "error"
    assert incident.technical["exception_type"] == "RuntimeError"
    assert "processor failed for test" in str(incident.technical["message"])
