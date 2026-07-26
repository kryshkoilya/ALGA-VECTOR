from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import numpy as np

from alga_vector.config.models import DevicesConfig, SpectrumConfig
from alga_vector.domain.enums import Capability, DeviceState, HealthLevel, Provenance
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame, utc_now

from .base import DeviceAdapter
from .hackrf import HackRfReceiveAdapter, is_explicit_hackrf_connection
from .live import (
    RtlSdrAdapter,
    TinySASerialAdapter,
    is_explicit_windows_com_port,
)

Clock = Callable[[], datetime]

_CAPABILITIES_BY_KIND: dict[str, frozenset[Capability]] = {
    "tinysa": frozenset({Capability.SPECTRUM_SWEEP}),
    "rtlsdr": frozenset({Capability.SPECTRUM_SWEEP, Capability.IQ_RX}),
    "hackrf": frozenset({Capability.SPECTRUM_SWEEP, Capability.IQ_RX}),
}


class FakeTinySAAdapter(DeviceAdapter):
    """Deterministic TinySA simulator suitable for demos and tests."""

    def __init__(
        self,
        adapter_id: str = "fake-tinysa-01",
        connection: str = "SIM:TINYSA",
        *,
        sample_rate_hz: int = 2_400_000,
        clock: Clock = utc_now,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id,
            display_name="TinySA Ultra",
            kind="tinysa",
            connection=connection,
            capabilities=_CAPABILITIES_BY_KIND["tinysa"],
            clock=clock,
        )
        self.sample_rate_hz = sample_rate_hz

    def inspect(self) -> DeviceSnapshot:
        if self.closed:
            return _closed_snapshot(self)
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            driver="ALGA deterministic simulator",
            sample_rate_hz=self.sample_rate_hz,
            last_data_at=self._clock(),
            generation=1,
            metrics={"simulation_seed": 11, "sweep_latency_ms": 42},
        )

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        self._ensure_open()
        _validate_spectrum_request(sequence, center_frequency_hz, span_hz, bins)
        axis = np.linspace(-1.0, 1.0, bins, dtype=np.float64)
        baseline = -108.0 + 1.7 * np.sin(axis * 19.0 + sequence * 0.13)
        primary = 43.0 * np.exp(-0.5 * ((axis - 0.08) / 0.035) ** 2)
        secondary = 18.0 * np.exp(-0.5 * ((axis + 0.37) / 0.055) ** 2)
        ripple = 0.8 * np.cos((axis + sequence * 0.001) * 61.0)
        power = np.asarray(baseline + primary + secondary + ripple, dtype=np.float32)
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=self._clock(),
            provenance=Provenance.SIMULATED,
            unit="dBm",
            calibration_id="simulation:tinysa",
        )


class FakeRTLSDRAdapter(DeviceAdapter):
    """Deterministic software-FFT representation of an RTL-SDR receiver."""

    def __init__(
        self,
        adapter_id: str = "fake-rtlsdr-01",
        connection: str = "SIM:RTLSDR",
        *,
        sample_rate_hz: int = 2_400_000,
        clock: Clock = utc_now,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id,
            display_name="RTL-SDR V3",
            kind="rtlsdr",
            connection=connection,
            capabilities=_CAPABILITIES_BY_KIND["rtlsdr"],
            clock=clock,
        )
        self.sample_rate_hz = sample_rate_hz

    def inspect(self) -> DeviceSnapshot:
        if self.closed:
            return _closed_snapshot(self)
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            driver="ALGA deterministic simulator",
            sample_rate_hz=self.sample_rate_hz,
            last_data_at=self._clock(),
            generation=1,
            metrics={"simulation_seed": 23, "iq_buffer_fill_percent": 18},
        )

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame:
        self._ensure_open()
        _validate_spectrum_request(sequence, center_frequency_hz, span_hz, bins)
        axis = np.linspace(-1.0, 1.0, bins, dtype=np.float64)
        baseline = -112.0 + 1.2 * np.cos(axis * 27.0 + sequence * 0.09)
        primary = 37.0 * np.exp(-0.5 * ((axis + 0.02) / 0.028) ** 2)
        sideband = 12.0 * np.exp(-0.5 * ((axis - 0.43) / 0.08) ** 2)
        power = np.asarray(baseline + primary + sideband, dtype=np.float32)
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=self._clock(),
            provenance=Provenance.SIMULATED,
            unit="dBFS",
            calibration_id="simulation:rtlsdr",
        )


class InactiveConfiguredAdapter(DeviceAdapter):
    """Concrete state for a supported receiver that policy keeps closed."""

    def __init__(
        self,
        *,
        adapter_id: str,
        kind: str,
        connection: str,
        enabled: bool,
        real_adapters_allowed: bool,
        clock: Clock = utc_now,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id,
            display_name={
                "tinysa": "tinySA",
                "rtlsdr": "RTL-SDR",
                "hackrf": "HackRF One · RX",
            }[kind],
            kind=kind,
            connection=connection,
            capabilities=_CAPABILITIES_BY_KIND[kind],
            clock=clock,
        )
        self.enabled = enabled
        self.real_adapters_allowed = real_adapters_allowed

    def inspect(self) -> DeviceSnapshot:
        if self.closed:
            return _closed_snapshot(self)
        if not self.enabled:
            code = "DEVICE.DISABLED_BY_CONFIG"
            reason = "Устройство отключено в конфигурации."
            action = "Включите адаптер в настройках при необходимости."
        elif not self.real_adapters_allowed:
            code = "DEVICE.REAL_ADAPTERS_DISABLED"
            reason = "Реальные адаптеры отключены безопасной политикой."
            action = "Явно разрешите реальные адаптеры после проверки драйвера."
        else:
            code = "DEVICE.CONFIGURATION_INACTIVE"
            reason = "Приёмник не активирован текущим профилем."
            action = "Проверьте состояние приёмника в настройках."
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=self.display_name,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.DISABLED,
            health=HealthLevel.UNKNOWN,
            capabilities=self.capabilities,
            driver="N/A",
            reason_code=code,
            reason_ru=reason,
            recommended_action_ru=action,
            generation=0,
            metrics={"probe_attempts": 0},
        )


def build_adapters(
    devices: DevicesConfig,
    spectrum: SpectrumConfig,
    *,
    clock: Clock = utc_now,
) -> tuple[DeviceAdapter, ...]:
    """Build explicit simulators or allowlisted real adapters from configuration."""

    result: list[DeviceAdapter] = []
    for configured in devices.adapters:
        connection_upper = configured.connection.upper()
        if configured.enabled and configured.kind == "tinysa" and connection_upper == "SIM:TINYSA":
            result.append(
                FakeTinySAAdapter(
                    configured.id,
                    configured.connection,
                    sample_rate_hz=spectrum.sample_rate_hz,
                    clock=clock,
                )
            )
        elif (
            configured.enabled
            and configured.kind == "rtlsdr"
            and connection_upper == "SIM:RTLSDR"
        ):
            result.append(
                FakeRTLSDRAdapter(
                    configured.id,
                    configured.connection,
                    sample_rate_hz=spectrum.sample_rate_hz,
                    clock=clock,
                )
            )
        elif (
            configured.enabled
            and configured.kind == "tinysa"
            and devices.enable_real_adapters
            and is_explicit_windows_com_port(configured.connection)
        ):
            result.append(
                TinySASerialAdapter(
                    adapter_id=configured.id,
                    connection=configured.connection,
                    model_override=configured.tinysa_model,
                    ultra_mode_enabled=configured.tinysa_ultra_mode,
                    clock=clock,
                )
            )
        elif (
            configured.enabled
            and configured.kind == "rtlsdr"
            and devices.enable_real_adapters
            and configured.connection.upper().startswith("RTLSDR:")
        ):
            result.append(
                RtlSdrAdapter(
                    adapter_id=configured.id,
                    connection=configured.connection,
                    sample_rate_hz=spectrum.sample_rate_hz,
                    profile_override=configured.rtlsdr_profile,
                    clock=clock,
                )
            )
        elif (
            configured.enabled
            and configured.kind == "hackrf"
            and devices.enable_real_adapters
            and is_explicit_hackrf_connection(configured.connection)
        ):
            result.append(
                HackRfReceiveAdapter(
                    adapter_id=configured.id,
                    connection=configured.connection,
                    sample_rate_hz=spectrum.sample_rate_hz,
                    clock=clock,
                )
            )
        else:
            result.append(
                InactiveConfiguredAdapter(
                    adapter_id=configured.id,
                    kind=configured.kind,
                    connection=configured.connection,
                    enabled=configured.enabled,
                    real_adapters_allowed=devices.enable_real_adapters,
                    clock=clock,
                )
            )
    return tuple(result)


def _closed_snapshot(adapter: DeviceAdapter) -> DeviceSnapshot:
    return DeviceSnapshot(
        device_id=adapter.adapter_id,
        display_name=adapter.display_name,
        kind=adapter.kind,
        connection="[закрыто]",
        state=DeviceState.DISABLED,
        health=HealthLevel.UNKNOWN,
        capabilities=adapter.capabilities,
        reason_code="DEVICE.ADAPTER_CLOSED",
        reason_ru="Адаптер остановлен.",
        recommended_action_ru="Запустите новый сеанс мониторинга.",
    )


def _validate_spectrum_request(
    sequence: int,
    center_frequency_hz: int,
    span_hz: int,
    bins: int,
) -> None:
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if center_frequency_hz <= 0 or span_hz <= 0:
        raise ValueError("frequency and span must be positive")
    if bins < 8 or bins > 65_536:
        raise ValueError("bins must be in range 8..65536")
