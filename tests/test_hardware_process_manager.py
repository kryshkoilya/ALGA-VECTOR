from __future__ import annotations

import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from alga_vector.application import ApplicationRuntime
from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    SpectrumConfig,
    StorageConfig,
)
from alga_vector.devices import (
    DeviceManager,
    HardwareProcessDeviceManager,
    build_device_manager,
    has_enabled_real_hardware,
)
from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot

_PROTOCOL_VERSION = 1


def _slow_spectrum_worker(
    connection: Connection,
    _devices: dict[str, object],
    _spectrum: dict[str, object],
) -> None:
    snapshot = DeviceSnapshot(
        device_id="radio-01",
        display_name="Test radio",
        kind="tinysa",
        connection="COM7",
        state=DeviceState.READY,
        health=HealthLevel.HEALTHY,
        capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
    )
    connection.send({"type": "ready", "protocol": _PROTOCOL_VERSION})
    try:
        while True:
            request = connection.recv()
            command = request["command"]
            if command == "refresh":
                connection.send(
                    {
                        "protocol": _PROTOCOL_VERSION,
                        "id": request["id"],
                        "ok": True,
                        "result": (snapshot,),
                        "snapshots": (snapshot,),
                    }
                )
            elif command == "read_spectrum":
                time.sleep(10.0)
            elif command == "close":
                return
    finally:
        connection.close()


def _crashing_worker(
    connection: Connection,
    _devices: dict[str, object],
    _spectrum: dict[str, object],
) -> None:
    connection.send({"type": "ready", "protocol": _PROTOCOL_VERSION})
    connection.recv()
    connection.close()


def _real_devices() -> DevicesConfig:
    return DevicesConfig(
        enable_real_adapters=True,
        adapters=[
            AdapterConfig(
                id="radio-01",
                kind="tinysa",
                enabled=True,
                connection="COM7",
            )
        ],
    )


def test_safe_empty_process_worker_implements_manager_contract() -> None:
    manager = HardwareProcessDeviceManager(
        DevicesConfig(),
        SpectrumConfig(),
        startup_timeout_seconds=5.0,
        control_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
    )
    try:
        assert manager.refresh() == ()
        assert manager.snapshots() == ()
        statuses = manager.resolve_capabilities((Capability.SPECTRUM_SWEEP,))
        assert statuses[0].state == CapabilityState.BLOCKED
        assert statuses[0].reason_code == "CAPABILITY.NO_PROVIDER"
        assert (
            manager.read_spectrum(
                sequence=1,
                center_frequency_hz=433_920_000,
                span_hz=1_000_000,
                bins=64,
            )
            is None
        )
    finally:
        manager.close()
    assert manager.closed


def test_spawn_worker_round_trips_a_simulated_numpy_spectrum() -> None:
    devices = DevicesConfig(
        adapters=[
            AdapterConfig(
                id="simulated-radio",
                kind="tinysa",
                enabled=True,
                connection="SIM:TINYSA",
            )
        ]
    )
    manager = HardwareProcessDeviceManager(
        devices,
        SpectrumConfig(),
        startup_timeout_seconds=5.0,
        control_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
    )
    try:
        assert manager.refresh()[0].state == DeviceState.READY
        assert (
            manager.read_spectrum(
                sequence=7,
                center_frequency_hz=433_920_000,
                span_hz=1_000_000,
                bins=64,
            )
            is None
        )
        deadline = time.monotonic() + 2.0
        frame = None
        while frame is None and time.monotonic() < deadline:
            time.sleep(0.01)
            frame = manager.read_spectrum(
                sequence=8,
                center_frequency_hz=433_920_000,
                span_hz=1_000_000,
                bins=64,
            )
        assert frame is not None
        assert frame.source_id == "simulated-radio"
        assert frame.sequence == 7
        assert frame.power_dbm.shape == (64,)
    finally:
        manager.close()


def test_spectrum_poll_never_waits_for_slow_native_read_and_times_out_fail_closed() -> None:
    manager = HardwareProcessDeviceManager(
        _real_devices(),
        SpectrumConfig(),
        startup_timeout_seconds=5.0,
        control_timeout_seconds=1.0,
        read_timeout_seconds=0.05,
        worker_target=_slow_spectrum_worker,
    )
    try:
        assert manager.refresh()[0].state == DeviceState.READY
        started = time.monotonic()
        frame = manager.read_spectrum(
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=1_000_000,
            bins=64,
        )
        elapsed = time.monotonic() - started
        assert frame is None
        assert manager.last_read_request_accepted
        assert elapsed < 0.25

        time.sleep(0.08)
        failed = manager.snapshots()[0]
        assert failed.state == DeviceState.FAILED
        assert failed.health == HealthLevel.ERROR
        assert failed.reason_code == "DEVICE.WORKER_TIMEOUT"
        with pytest.raises(AppError, match=r"DEVICE\.WORKER_TIMEOUT"):
            manager.read_spectrum(
                sequence=2,
                center_frequency_hz=433_920_000,
                span_hz=1_000_000,
                bins=64,
            )
    finally:
        manager.close()


def test_crashed_worker_is_fail_closed_and_reconnect_is_bounded() -> None:
    manager = HardwareProcessDeviceManager(
        _real_devices(),
        SpectrumConfig(),
        startup_timeout_seconds=5.0,
        control_timeout_seconds=0.2,
        worker_target=_crashing_worker,
    )
    try:
        failed = manager.refresh()[0]
        assert failed.state == DeviceState.FAILED
        assert failed.reason_code == "DEVICE.WORKER_CRASHED"

        started = time.monotonic()
        reconnected = manager.reconnect("radio-01")
        assert time.monotonic() - started < 2.0
        assert reconnected.state == DeviceState.FAILED
        assert reconnected.reason_code == "DEVICE.WORKER_CRASHED"
    finally:
        manager.close()


def test_factory_isolates_only_enabled_real_hardware_in_live_mode() -> None:
    demo_simulated = AppConfig(
        mode="demo",
        devices=DevicesConfig(
            enable_real_adapters=True,
            adapters=[
                AdapterConfig(
                    id="sim",
                    kind="tinysa",
                    enabled=True,
                    connection="SIM:TINYSA",
                )
            ],
        ),
    )
    live_disabled = AppConfig(
        mode="live",
        devices=DevicesConfig(
            enable_real_adapters=False,
            adapters=_real_devices().adapters,
        ),
    )
    live_real = AppConfig(mode="live", devices=_real_devices())

    assert not has_enabled_real_hardware(demo_simulated)
    assert not has_enabled_real_hardware(live_disabled)
    assert has_enabled_real_hardware(live_real)

    local = build_device_manager(demo_simulated)
    try:
        assert isinstance(local, DeviceManager)
    finally:
        local.close()


def test_runtime_factory_uses_process_proxy_for_live_real_adapter(
    tmp_path: Path,
) -> None:
    runtime = ApplicationRuntime(
        AppConfig(
            mode="live",
            first_run_complete=True,
            storage=StorageConfig(data_dir=tmp_path / "runtime"),
            devices=_real_devices(),
        )
    )
    try:
        assert isinstance(runtime._device_manager, HardwareProcessDeviceManager)
        assert runtime.background_acquisition_enabled
        assert not runtime.acquisition_running
    finally:
        runtime.shutdown()
