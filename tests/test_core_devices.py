from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from alga_vector.config.models import AdapterConfig, DevicesConfig, SpectrumConfig
from alga_vector.devices import (
    DeviceManager,
    FakeRTLSDRAdapter,
    FakeTinySAAdapter,
    InactiveConfiguredAdapter,
    build_adapters,
)
from alga_vector.domain.enums import Capability, CapabilityState, DeviceState
from alga_vector.domain.errors import AppError

FIXED_TIME = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_TIME


def test_fake_adapters_are_deterministic() -> None:
    first = FakeTinySAAdapter(clock=fixed_clock)
    second = FakeTinySAAdapter(clock=fixed_clock)

    frame_a = first.read_spectrum(
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        bins=128,
    )
    frame_b = second.read_spectrum(
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        bins=128,
    )
    later = second.read_spectrum(
        sequence=8,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        bins=128,
    )

    np.testing.assert_array_equal(frame_a.power_dbm, frame_b.power_dbm)
    assert not np.array_equal(frame_a.power_dbm, later.power_dbm)
    assert frame_a.power_dbm.dtype == np.float32
    assert frame_a.captured_at == FIXED_TIME


def test_manager_resolves_supported_capabilities_and_blocks_missing_provider() -> None:
    manager = DeviceManager(
        (
            FakeTinySAAdapter(clock=fixed_clock),
            FakeRTLSDRAdapter(clock=fixed_clock),
        )
    )

    devices = manager.refresh()
    statuses = {
        item.capability: item
        for item in manager.resolve_capabilities(
            (
                Capability.SPECTRUM_SWEEP,
                Capability.IQ_RX,
                Capability.DF_OBSERVATION,
            )
        )
    }

    assert [device.state for device in devices] == [
        DeviceState.READY,
        DeviceState.READY,
    ]
    assert statuses[Capability.SPECTRUM_SWEEP].state == CapabilityState.AVAILABLE
    assert statuses[Capability.IQ_RX].state == CapabilityState.AVAILABLE
    assert statuses[Capability.DF_OBSERVATION].state == CapabilityState.BLOCKED
    assert statuses[Capability.DF_OBSERVATION].reason_code == "CAPABILITY.NO_PROVIDER"

    frame = manager.read_spectrum(
        sequence=1,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        bins=64,
    )
    assert frame is not None
    assert frame.source_id == "fake-tinysa-01"
    streaming = manager.snapshots()[0]
    assert streaming.state == DeviceState.STREAMING
    assert streaming.center_frequency_hz == 433_920_000
    assert streaming.last_data_at == FIXED_TIME
    assert streaming.metrics["capture_confirmed"] == 1
    assert streaming.metrics["capture_active"] == 1
    assert streaming.metrics["capture_success_count"] == 1

    refreshed = manager.refresh()[0]
    assert refreshed.state == DeviceState.STREAMING
    assert refreshed.metrics["capture_success_count"] == 1


def test_builder_never_turns_unknown_connections_into_active_probes() -> None:
    with pytest.raises(ValueError, match="explicit COM port"):
        AdapterConfig(
            id="untrusted-serial",
            kind="tinysa",
            enabled=True,
            connection="COM1;OPEN_EVERYTHING",
        )
    with pytest.raises(ValueError, match=r"tinysa|rtlsdr"):
        AdapterConfig(
            id="unsupported",
            kind="krakensdr",
            enabled=True,
            connection="203.0.113.99",
        )
    devices = DevicesConfig(
        enable_real_adapters=True,
        adapters=[
            AdapterConfig(
                id="known-disabled",
                kind="tinysa",
                enabled=False,
                connection="COM249",
            ),
        ],
    )

    adapters = build_adapters(devices, SpectrumConfig(), clock=fixed_clock)

    assert isinstance(adapters[0], InactiveConfiguredAdapter)
    assert adapters[0].inspect().metrics["probe_attempts"] == 0


def test_manager_shutdown_is_idempotent_and_blocks_new_reads() -> None:
    manager = DeviceManager((FakeTinySAAdapter(clock=fixed_clock),))
    manager.refresh()

    manager.close()
    manager.close()

    assert manager.closed
    with pytest.raises(AppError, match=r"DEVICE\.MANAGER_CLOSED"):
        manager.read_spectrum(
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=5_000_000,
        )


def test_spectrum_failure_is_isolated_and_next_provider_is_used() -> None:
    class BrokenTinySA(FakeTinySAAdapter):
        def read_spectrum(self, **_kwargs: object) -> object:
            raise OSError("simulated USB failure")

    manager = DeviceManager(
        (
            BrokenTinySA("broken", clock=fixed_clock),
            FakeTinySAAdapter("fallback", clock=fixed_clock),
        )
    )
    manager.refresh()

    frame = manager.read_spectrum(
        sequence=3,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        bins=64,
    )

    assert frame is not None
    assert frame.source_id == "fallback"


def test_read_failure_increments_device_generation_for_each_new_episode() -> None:
    class BrokenTinySA(FakeTinySAAdapter):
        def read_spectrum(self, **_kwargs: object) -> object:
            raise OSError("deterministic USB read failure")

    manager = DeviceManager((BrokenTinySA("broken", clock=fixed_clock),))
    initial = manager.refresh()[0]

    with pytest.raises(AppError, match=r"SPECTRUM\.ADAPTER_FAILURE"):
        manager.read_spectrum(
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=5_000_000,
            bins=64,
        )
    first_failure = manager.snapshots()[0]

    recovered = manager.refresh()[0]
    with pytest.raises(AppError, match=r"SPECTRUM\.ADAPTER_FAILURE"):
        manager.read_spectrum(
            sequence=2,
            center_frequency_hz=433_920_000,
            span_hz=5_000_000,
            bins=64,
        )
    second_failure = manager.snapshots()[0]

    assert initial.generation == 1
    assert first_failure.state == DeviceState.FAILED
    assert first_failure.generation == 2
    assert recovered.state == DeviceState.READY
    assert recovered.generation == 3
    assert second_failure.state == DeviceState.FAILED
    assert second_failure.generation == 4
