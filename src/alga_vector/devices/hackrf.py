"""Receive-only HackRF One adapter backed by official host tools."""

# ruff: noqa: RUF001

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime

import numpy as np

from alga_vector.domain.enums import Capability, DeviceState, HealthLevel, Provenance
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame, utc_now

from .base import DeviceAdapter
from .capabilities import (
    HACKRF_ONE_PROFILE,
    ReceiverHardwareProfile,
    require_hardware_tuning,
)
from .host_tools import (
    HostToolError,
    HostTools,
    HostToolTimedOut,
    SubprocessHostTools,
)
from .live import _iq_to_dbfs, _welch_sample_count
from .receiver_discovery import HackRfDiscoveryService

Clock = Callable[[], datetime]
_HACKRF_CONNECTION_RE = re.compile(r"(?i)^HACKRF:([0-9a-f]{8,64})$")
_MAX_FFT_EXPONENT = 18
_MIN_CAPTURE_SAMPLES = 32_768
_CAPTURE_ATTEMPTS = 2
_CAPTURE_TIMEOUT_SECONDS = 4.0


def is_explicit_hackrf_connection(value: str) -> bool:
    """Accept only one explicit hexadecimal serial, never an index or wildcard."""

    return _HACKRF_CONNECTION_RE.fullmatch(value.strip()) is not None


class HackRfReceiveAdapter(DeviceAdapter):
    """Capture signed 8-bit IQ through ``hackrf_transfer -r`` only.

    No transmit method, transmit flag, signal-source mode, antenna power, or RF
    amplifier enablement is exposed by this adapter.
    """

    def __init__(
        self,
        *,
        adapter_id: str,
        connection: str,
        sample_rate_hz: int,
        clock: Clock = utc_now,
        host_tools: HostTools | None = None,
    ) -> None:
        match = _HACKRF_CONNECTION_RE.fullmatch(connection.strip())
        if match is None:
            raise ValueError(
                "HackRF connection must use HACKRF:<hexadecimal serial>"
            )
        serial = match.group(1).lower()
        super().__init__(
            adapter_id=adapter_id,
            display_name="HackRF One · RX",
            kind="hackrf",
            connection=f"HACKRF:{serial}",
            capabilities=frozenset(
                {Capability.SPECTRUM_SWEEP, Capability.IQ_RX}
            ),
            clock=clock,
        )
        self.serial = serial
        self.sample_rate_hz = sample_rate_hz
        self._host_tools = host_tools or SubprocessHostTools()
        self._verified = False
        self._board_name = "HackRF One"
        self._firmware = ""

    @property
    def receiver_profile(self) -> ReceiverHardwareProfile:
        return HACKRF_ONE_PROFILE

    def inspect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._ensure_verified()
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=f"{self._board_name} · RX",
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            driver=(
                "Great Scott Gadgets host tools"
                + (f" · {self._firmware}" if self._firmware else "")
            ),
            sample_rate_hz=self.sample_rate_hz,
            generation=1,
            metrics={
                "receive_only": 1,
                "rf_amp_enabled": 0,
                "antenna_port_power_enabled": 0,
                "gain_mode": "fixed_receive_only",
                "lna_gain_db": 16,
                "vga_gain_db": 20,
                "tuning_profile_id": HACKRF_ONE_PROFILE.profile_id,
                "tuning_min_hz": HACKRF_ONE_PROFILE.minimum_frequency_hz,
                "tuning_max_hz": HACKRF_ONE_PROFILE.maximum_frequency_hz,
                "sample_rate_min_hz": (
                    HACKRF_ONE_PROFILE.minimum_sample_rate_hz or 0
                ),
                "sample_rate_max_hz": (
                    HACKRF_ONE_PROFILE.maximum_sample_rate_hz or 0
                ),
                "portapack_requires_hackrf_usb_mode": 1,
                "power_unit": "dBFS",
            },
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
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not 8 <= bins <= 65_536:
            raise ValueError("bins must be in range 8..65536")
        require_hardware_tuning(
            HACKRF_ONE_PROFILE,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            sample_rate_hz=self.sample_rate_hz,
        )
        self._ensure_verified()

        required_samples = max(
            4_096,
            math.ceil(bins * self.sample_rate_hz / span_hz),
        )
        fft_exponent = math.ceil(math.log2(required_samples))
        if fft_exponent > _MAX_FFT_EXPONENT:
            raise AppError(
                code="SPECTRUM.RESOLUTION_UNAVAILABLE",
                message_ru=(
                    "Запрошенная полоса требует слишком длинного IQ-захвата HackRF."
                ),
                operator_action_ru=(
                    "Увеличьте полосу, уменьшите число точек "
                    "или снизьте допустимую частоту дискретизации."
                ),
                retryable=False,
            )
        fft_size = 1 << fft_exponent
        sample_count = max(
            _MIN_CAPTURE_SAMPLES,
            _welch_sample_count(fft_size),
        )
        payload = self._capture_receive_only(
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            sample_count=sample_count,
        )
        samples = _signed_iq_bytes(payload, sample_count=sample_count)
        power = _iq_to_dbfs(
            samples,
            bins,
            span_fraction=span_hz / self.sample_rate_hz,
            segment_size=fft_size,
        )
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=self._clock(),
            provenance=Provenance.LIVE,
            unit="dBFS",
            calibration_id=None,
            uncertainty_db=None,
        )

    def capture_metrics(self) -> dict[str, float | int | str]:
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "gain_mode": "fixed_receive_only",
            "lna_gain_db": 16,
            "vga_gain_db": 20,
            "rf_amp_enabled": 0,
            "antenna_port_power_enabled": 0,
            "tuning_profile_id": HACKRF_ONE_PROFILE.profile_id,
        }

    def reconnect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._verified = False
        return self.inspect()

    def close(self) -> None:
        self._verified = False
        super().close()

    def _ensure_verified(self) -> None:
        if self._verified:
            return
        if self._host_tools.find("hackrf_transfer") is None:
            raise AppError(
                code="DEVICE.HACKRF_TRANSFER_MISSING",
                message_ru="Официальный инструмент hackrf_transfer не найден.",
                operator_action_ru=(
                    "Установите совместимый пакет HackRF host tools "
                    "или добавьте его в hardware-tools приложения."
                ),
                retryable=False,
            )
        discovery = HackRfDiscoveryService(
            host_tools=self._host_tools,
            timeout_seconds=1.5,
            attempts=2,
        ).discover()
        device = next(
            (
                item
                for item in discovery.devices
                if item.serial.casefold() == self.serial.casefold()
            ),
            None,
        )
        if device is None:
            issue = discovery.issues[0] if discovery.issues else None
            raise AppError(
                code=(
                    issue.code
                    if issue is not None
                    else "DEVICE.HACKRF_NOT_FOUND"
                ),
                message_ru=(
                    issue.message_ru
                    if issue is not None
                    else "Настроенный HackRF не обнаружен в USB mode."
                ),
                operator_action_ru=(
                    issue.operator_action_ru
                    if issue is not None
                    else (
                        "Переведите PortaPack в HackRF USB mode, "
                        "проверьте кабель и повторите подключение."
                    )
                ),
                retryable=(issue.retryable if issue is not None else True),
            )
        self._board_name = device.board_name or "HackRF One"
        self._firmware = device.firmware or ""
        self._verified = True

    def _capture_receive_only(
        self,
        *,
        center_frequency_hz: int,
        span_hz: int,
        sample_count: int,
    ) -> bytes:
        tool = self._host_tools.find("hackrf_transfer")
        if tool is None:
            self._verified = False
            raise AppError(
                code="DEVICE.HACKRF_TRANSFER_MISSING",
                message_ru="Официальный инструмент hackrf_transfer не найден.",
                operator_action_ru="Переустановите аппаратный пакет HackRF host tools.",
                retryable=False,
            )
        expected_bytes = sample_count * 2
        filter_hz = max(
            1_750_000,
            min(28_000_000, span_hz),
        )
        command = (
            tool,
            "-d",
            self.serial,
            "-r",
            "-",
            "-f",
            str(center_frequency_hz),
            "-s",
            str(self.sample_rate_hz),
            "-n",
            str(sample_count),
            "-b",
            str(filter_hz),
            "-a",
            "0",
            "-p",
            "0",
            "-l",
            "16",
            "-g",
            "20",
        )

        last_code = "DEVICE.HACKRF_CAPTURE_FAILED"
        last_message = "HackRF не вернул IQ-данные."
        last_details = ""
        for attempt in range(1, _CAPTURE_ATTEMPTS + 1):
            try:
                result = self._host_tools.run(
                    command,
                    timeout_seconds=_CAPTURE_TIMEOUT_SECONDS,
                    maximum_stdout_bytes=expected_bytes,
                )
            except HostToolTimedOut:
                last_code = "DEVICE.HACKRF_CAPTURE_TIMEOUT"
                last_message = "IQ-захват HackRF превысил безопасный тайм-аут."
                last_details = "hackrf_transfer timed out"
            except HostToolError as exc:
                last_code = "DEVICE.HACKRF_CAPTURE_FAILED"
                last_message = "Не удалось выполнить receive-only IQ-захват HackRF."
                last_details = type(exc).__name__
            else:
                if result.returncode == 0 and len(result.stdout) == expected_bytes:
                    return result.stdout
                last_code = "DEVICE.HACKRF_CAPTURE_INCOMPLETE"
                last_message = "HackRF вернул неполный IQ-захват."
                diagnostic = result.stderr.decode(
                    "utf-8", errors="replace"
                ).strip()
                last_details = (
                    f"exit={result.returncode}; bytes={len(result.stdout)}/"
                    f"{expected_bytes}; stderr={diagnostic[:240]}"
                )
            if attempt < _CAPTURE_ATTEMPTS:
                continue

        self._verified = False
        raise AppError(
            code=last_code,
            message_ru=last_message,
            operator_action_ru=(
                "Проверьте HackRF USB mode, драйвер WinUSB, питание USB "
                "и отсутствие другого приложения, удерживающего приёмник."
            ),
            retryable=True,
            technical_details={
                "receive_only": True,
                "attempts": _CAPTURE_ATTEMPTS,
                "details": last_details,
            },
        )


def _signed_iq_bytes(payload: bytes, *, sample_count: int) -> np.ndarray:
    expected_bytes = sample_count * 2
    if len(payload) != expected_bytes:
        raise AppError(
            code="DEVICE.HACKRF_IQ_LENGTH_INVALID",
            message_ru="IQ-буфер HackRF имеет недопустимую длину.",
            operator_action_ru="Повторите подключение приёмника.",
            retryable=True,
        )
    raw = np.frombuffer(payload, dtype=np.int8)
    pairs = raw.reshape(sample_count, 2).astype(np.float32)
    samples = (pairs[:, 0] + 1j * pairs[:, 1]) / 128.0
    return np.asarray(samples, dtype=np.complex64)


__all__ = [
    "HackRfReceiveAdapter",
    "is_explicit_hackrf_connection",
]
