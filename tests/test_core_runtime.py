from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alga_vector.application import ApplicationRuntime, RuntimeState
from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    LocationPolicyConfig,
    LoggingConfig,
    StorageConfig,
)
from alga_vector.domain.enums import Capability, CapabilityState, DeviceState, Provenance
from alga_vector.domain.errors import AppError
from alga_vector.support import verify_support_bundle

FIXED_TIME = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
# Deliberately synthetic test-only coordinate; it does not represent a deployed site.
SYNTHETIC_BASE = (12.3456, 65.4321)


def fixed_clock() -> datetime:
    return FIXED_TIME


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        mode="demo",
        first_run_complete=True,
        storage=StorageConfig(data_dir=tmp_path / "runtime"),
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id="fake-tinysa-01",
                    kind="tinysa",
                    enabled=True,
                    connection="SIM:TINYSA",
                ),
                AdapterConfig(
                    id="fake-rtlsdr-01",
                    kind="rtlsdr",
                    enabled=True,
                    connection="SIM:RTLSDR",
                ),
                AdapterConfig(
                    id="disabled-tinysa-02",
                    kind="tinysa",
                    enabled=False,
                    connection="COM249",
                ),
            ]
        ),
        logging=LoggingConfig(max_bytes=1_048_576, max_files=3),
    )


def test_runtime_produces_truthful_snapshots_and_shuts_down_cleanly(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)

    first = runtime.snapshot(bins=64)
    assert runtime.current_snapshot() is first
    second = runtime.rescan()
    third = runtime.reconnect("disabled-tinysa-02")

    assert runtime.state == RuntimeState.RUNNING
    assert first.revision == 1
    assert second.revision == 2
    assert third.revision == 3
    assert first.mode == Provenance.SIMULATED
    # Product readiness is based on the primary RF-monitoring capability.
    # Optional IQ/DF/map/location features degrade independently.
    assert first.readiness_percent == 100
    assert first.spectrum is not None
    assert first.spectrum.source_id == "fake-tinysa-01"
    assert len(first.spectrum.power_dbm) == 64
    assert [item.state for item in first.devices] == [
        DeviceState.STREAMING,
        DeviceState.READY,
        DeviceState.DISABLED,
    ]
    capability = {item.capability: item.state for item in first.capabilities}
    assert capability[Capability.SPECTRUM_SWEEP] == CapabilityState.AVAILABLE
    assert capability[Capability.IQ_RX] == CapabilityState.AVAILABLE
    assert capability[Capability.DF_OBSERVATION] == CapabilityState.BLOCKED
    assert capability[Capability.LOCAL_CAPTURE_STORAGE] == CapabilityState.AVAILABLE
    assert [item.code for item in first.incidents] == ["DEVICE.DISABLED_BY_CONFIG"]
    assert first.incidents[0].occurred_at == second.incidents[0].occurred_at
    assert runtime.journal is not None
    assert runtime.journal.summary().total == 1

    log_path = runtime.logger_path
    runtime.shutdown()
    runtime.shutdown()

    assert runtime.closed
    assert runtime.state == RuntimeState.CLOSED
    assert runtime.journal is not None and runtime.journal.closed
    assert log_path is not None and log_path.exists()
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {item["event"] for item in records} >= {
        "runtime.started",
        "runtime.snapshot",
        "runtime.shutdown_complete",
    }
    with pytest.raises(AppError, match=r"RUNTIME\.CLOSED"):
        runtime.snapshot()


def test_runtime_records_real_spectrum_frames_and_finalizes_capture(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)
    runtime.snapshot(bins=32)

    started = runtime.start_recording()
    runtime.snapshot(bins=32)
    completed = runtime.stop_recording()

    assert started.active
    assert completed.frames == 1
    assert completed.path.is_file()
    assert completed.dropped_frames == 0
    assert runtime.recording_status().active is False
    runtime.shutdown()


def test_runtime_static_simulator_does_not_create_false_rf_episode(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)

    snapshots = [runtime.snapshot(bins=64) for _ in range(12)]
    latest = snapshots[-1]

    assert latest.signal_events == ()
    assert latest.signal_decision is not None
    assert latest.signal_decision.alertable is False
    runtime.shutdown()


def test_runtime_starts_configured_gps_without_waiting_for_settings_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receivers: list[object] = []

    class FakeGpsReceiver:
        def __init__(
            self,
            _service: object,
            port: str,
            *,
            baudrate: int,
        ) -> None:
            self.port = port
            self.baudrate = baudrate
            self.running = False
            self.stopped = False
            receivers.append(self)

        def start(self) -> None:
            self.running = True

        @property
        def status(self) -> dict[str, object]:
            return {
                "port": self.port,
                "running": self.running,
                "last_error": "serial read failed" if not self.running else "",
            }

        def stop(self) -> None:
            self.running = False
            self.stopped = True

    monkeypatch.setattr(
        "alga_vector.application.runtime.NmeaSerialReceiver",
        FakeGpsReceiver,
    )
    config = _config(tmp_path).model_copy(
        update={
            "location": LocationPolicyConfig(
                source="gps",
                gps_port="COM8",
            )
        }
    )
    runtime = ApplicationRuntime(config, clock=fixed_clock)

    runtime.start()

    assert len(receivers) == 1
    assert receivers[0].port == "COM8"
    assert receivers[0].running
    assert runtime.start_gps() == "GPS уже работает на выбранном порту"
    receivers[0].running = False
    snapshot = runtime.snapshot()
    assert any(
        incident.code == "LOCATION.GPS_READER_STOPPED"
        for incident in snapshot.incidents
    )
    runtime.shutdown()
    assert receivers[0].stopped


def test_tuning_reuses_open_device_manager(tmp_path: Path) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)
    runtime.start()
    manager = runtime._device_manager

    runtime.update_settings(
        {"spectrum": {"center_frequency_hz": 434_100_000}}
    )

    assert runtime._device_manager is manager
    assert runtime.config.spectrum.center_frequency_hz == 434_100_000
    runtime.shutdown()


def test_storage_change_rebinds_journal_capture_and_protected_base(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)
    runtime.start()
    runtime.set_manual_base(*SYNTHETIC_BASE)
    previous_journal = runtime.journal
    new_directory = tmp_path / "relocated"

    message = runtime.update_settings(
        {"storage": {"data_dir": str(new_directory)}}
    )

    assert "перенесены" in message
    assert runtime.config.storage.data_dir == new_directory
    assert previous_journal is not None and previous_journal.closed
    assert runtime.journal is not None
    assert runtime.journal.path == new_directory / "state" / "events.sqlite3"
    assert runtime.logger_path == new_directory / "logs" / "alga-vector.jsonl"
    assert (new_directory / "state" / "base-location.dpapi").is_file()
    runtime.snapshot()
    started = runtime.start_recording()
    assert started.path is not None
    assert started.path.parent == new_directory / "captures"
    runtime.stop_recording()
    relocated_config = runtime.config
    runtime.shutdown()

    reopened = ApplicationRuntime(relocated_config, clock=fixed_clock)
    location = reopened.snapshot().location
    assert location.status.value == "manual_unverified"
    reopened.shutdown()


def test_acknowledgement_survives_restart_and_support_bundle_is_valid(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runtime = ApplicationRuntime(config, clock=fixed_clock)
    incident = runtime.snapshot().incidents[0]

    assert runtime.acknowledge_incident(incident.incident_id)
    bundle_path = runtime.export_support_bundle()
    assert bundle_path.is_file()
    assert verify_support_bundle(bundle_path)
    runtime.shutdown()

    reopened = ApplicationRuntime(config, clock=fixed_clock)
    current = reopened.snapshot()
    assert current.incidents[0].incident_id == incident.incident_id
    assert current.incidents[0].acknowledged is True
    reopened.shutdown()


def test_corrupt_sqlite_degrades_without_locking_or_crashing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    journal_path = config.storage.data_dir / "state" / "events.sqlite3"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_bytes(b"not-a-sqlite-database")

    runtime = ApplicationRuntime(config, clock=fixed_clock)
    snapshot = runtime.snapshot()

    storage = {
        item.capability: item.state for item in snapshot.capabilities
    }[Capability.LOCAL_CAPTURE_STORAGE]
    assert storage == CapabilityState.BLOCKED
    assert any(item.code == "STORAGE.JOURNAL_UNAVAILABLE" for item in snapshot.incidents)
    runtime.shutdown()
    journal_path.unlink()


def test_repeated_spectrum_failure_is_one_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ApplicationRuntime(_config(tmp_path), clock=fixed_clock)
    runtime.start()
    manager = runtime._device_manager
    original = manager.read_spectrum

    def fail(**_kwargs: object) -> object:
        raise AppError(
            code="SPECTRUM.TEST_FAILURE",
            message_ru="Тестовый сбой.",
            operator_action_ru="Повторите тест.",
        )

    monkeypatch.setattr(manager, "read_spectrum", fail)
    snapshots = [runtime.snapshot() for _ in range(3)]
    failure_ids = [
        item.incident_id
        for snapshot in snapshots
        for item in snapshot.incidents
        if item.code == "SPECTRUM.READ_FAILED"
    ]
    assert len(set(failure_ids)) == 1
    assert runtime.journal is not None
    assert runtime.journal.summary().total == 2

    monkeypatch.setattr(manager, "read_spectrum", original)
    runtime.snapshot()
    monkeypatch.setattr(manager, "read_spectrum", fail)
    second_episode = runtime.snapshot()
    second_id = next(
        item.incident_id
        for item in second_episode.incidents
        if item.code == "SPECTRUM.READ_FAILED"
    )
    assert second_id != failure_ids[0]
    runtime.shutdown()
