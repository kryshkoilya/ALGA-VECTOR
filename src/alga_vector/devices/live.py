"""Explicit real-device adapters for the hardware paths we can support directly."""

from __future__ import annotations

# ruff: noqa: RUF001
import importlib
import math
import re
from collections.abc import Callable
from contextlib import suppress
from ctypes import c_ubyte
from datetime import datetime
from types import ModuleType
from typing import Any, cast

import numpy as np

from alga_vector.domain.enums import Capability, DeviceState, HealthLevel, Provenance
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame, utc_now

from .base import DeviceAdapter
from .capabilities import (
    ReceiverHardwareProfile,
    TinySaModel,
    identify_tinysa_model,
    require_hardware_tuning,
    tinysa_hardware_profile,
)
from .tuning import (
    BLOG_V4_PROFILE,
    GENERIC_RTLSDR_PROFILE,
    RTLSDR_MAX_SAMPLE_RATE_HZ,
    RTLSDR_STABLE_SAMPLE_RATE_HZ,
    RtlSdrInputMode,
    identify_rtlsdr_profile,
    require_rtlsdr_tuning,
    select_rtlsdr_profile,
)

Clock = Callable[[], datetime]
_COM_PORT_RE = re.compile(r"(?i)^COM(?:[1-9]|[1-9]\d|[12]\d\d)$")
_WELCH_SEGMENT_COUNT = 5
_WELCH_OVERLAP_DIVISOR = 2
_RTLSDR_MIN_SETTLING_SAMPLES = 4_096
_RTLSDR_MAX_SETTLING_SAMPLES = 65_536


def is_explicit_windows_com_port(value: str) -> bool:
    """Accept only a single explicit COM port, never an enumeration expression."""

    return _COM_PORT_RE.fullmatch(value.strip()) is not None


class TinySASerialAdapter(DeviceAdapter):
    """Read calibrated sweeps from one explicitly configured tinySA USB serial port."""

    def __init__(
        self,
        *,
        adapter_id: str,
        connection: str,
        model_override: str = "auto",
        ultra_mode_enabled: bool = False,
        clock: Clock = utc_now,
        serial_module: ModuleType | None = None,
    ) -> None:
        if not is_explicit_windows_com_port(connection):
            raise ValueError("tinySA connection must be one explicit COM port")
        if model_override != "auto":
            try:
                TinySaModel(model_override)
            except ValueError as exc:
                raise ValueError("unknown tinySA model override") from exc
        super().__init__(
            adapter_id=adapter_id,
            display_name="tinySA",
            kind="tinysa",
            connection=connection.upper(),
            capabilities=frozenset({Capability.SPECTRUM_SWEEP}),
            clock=clock,
        )
        self._serial_module = serial_module
        self._port: Any | None = None
        self._version = ""
        self.model_override = model_override
        self.ultra_mode_enabled = ultra_mode_enabled
        self._detected_model = TinySaModel.BASIC
        self._model = TinySaModel.BASIC
        self._receiver_profile = tinysa_hardware_profile(
            self._model,
            ultra_mode_enabled=False,
        )

    @property
    def receiver_profile(self) -> ReceiverHardwareProfile:
        return self._receiver_profile

    def inspect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._open_port()
        profile = self._receiver_profile
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=profile.display_name_ru,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            driver=f"USB CDC · {self._version or 'firmware version unavailable'}",
            last_data_at=self._clock(),
            generation=1,
            metrics={
                "transport": "serial",
                "port_allowlisted": 1,
                "metadata_discovery_does_not_open_port": 1,
                "tuning_profile_id": profile.profile_id,
                "detected_model": self._detected_model.value,
                "configured_model": self.model_override,
                "ultra_mode_operator_confirmed": int(self.ultra_mode_enabled),
                "tuning_min_hz": profile.minimum_frequency_hz,
                "tuning_max_hz": profile.maximum_frequency_hz,
                "maximum_sweep_points": profile.maximum_sweep_points or 0,
                "level_calibrated_max_hz": (
                    profile.level_calibrated_maximum_hz or 0
                ),
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
        self._open_port()
        require_hardware_tuning(
            self._receiver_profile,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
        )
        points = min(
            self._receiver_profile.maximum_sweep_points or 450,
            max(51, bins),
        )
        start_hz = center_frequency_hz - span_hz // 2
        stop_hz = center_frequency_hz + span_hz // 2
        try:
            payload = self._command(
                f"scan {start_hz} {stop_hz} {points} 2",
                timeout=12.0,
            )
            values = _parse_tinysa_scan(payload, points)
        except AppError:
            # A partial/malformed sweep leaves serial framing unknown.  Do not
            # let a later inspect report READY from the same poisoned handle.
            self._close_port()
            raise
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=values,
            captured_at=self._clock(),
            provenance=Provenance.LIVE,
            unit="dBm",
            # Firmware identity is not a calibration certificate.  Until an
            # operator-supplied calibration record is implemented, absolute
            # calibration remains explicitly unverified.
            calibration_id=None,
            uncertainty_db=None,
        )

    def close(self) -> None:
        self._close_port()
        super().close()

    def reconnect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._close_port()
        return self.inspect()

    def _close_port(self) -> None:
        port = self._port
        self._port = None
        if port is not None:
            with suppress(Exception):
                port.close()

    def _open_port(self) -> None:
        if self._port is not None and bool(getattr(self._port, "is_open", True)):
            return
        module = self._serial_module
        if module is None:
            try:
                module = importlib.import_module("serial")
            except ImportError as exc:
                raise AppError(
                    code="DEVICE.PYSERIAL_MISSING",
                    message_ru="Модуль pyserial не установлен.",
                    operator_action_ru="Установите аппаратный пакет ALGA VECTOR.",
                    retryable=False,
                ) from exc
        try:
            self._port = module.Serial(
                port=self.connection,
                baudrate=115_200,
                timeout=1.0,
                write_timeout=1.0,
            )
            self._version = self._command("version", timeout=2.0).decode(
                "utf-8", errors="replace"
            ).strip()
            self._detected_model = identify_tinysa_model(self._version)
            self._model = (
                self._detected_model
                if self.model_override == "auto"
                else TinySaModel(self.model_override)
            )
            self._receiver_profile = tinysa_hardware_profile(
                self._model,
                ultra_mode_enabled=self.ultra_mode_enabled,
            )
        except AppError:
            self._close_port()
            raise
        except Exception as exc:
            self._close_port()
            raise AppError(
                code="DEVICE.TINYSA_OPEN_FAILED",
                message_ru="Не удалось открыть настроенный tinySA.",
                operator_action_ru="Проверьте выбранный COM-порт, кабель и доступ к устройству.",
                retryable=True,
                technical_details={"port": self.connection, "error": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def _command(self, command: str, *, timeout: float) -> bytes:
        port = self._port
        if port is None:
            raise RuntimeError("serial port is not open")
        original_timeout = getattr(port, "timeout", 1.0)
        try:
            port.timeout = timeout
            reset_input = getattr(port, "reset_input_buffer", None)
            if callable(reset_input):
                reset_input()
            port.write((command + "\r").encode("ascii"))
            flush = getattr(port, "flush", None)
            if callable(flush):
                flush()
            data = bytes(port.read_until(b"ch>", 1_048_576))
        except Exception as exc:
            self._close_port()
            raise AppError(
                code="DEVICE.TINYSA_IO_FAILED",
                message_ru="tinySA не ответил на команду измерения.",
                operator_action_ru="Переподключите устройство и повторите измерение.",
                retryable=True,
                technical_details={"command": command.split()[0], "error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        finally:
            with suppress(Exception):
                port.timeout = original_timeout
        if b"ch>" not in data:
            self._close_port()
            raise AppError(
                code="DEVICE.TINYSA_TIMEOUT",
                message_ru="Ответ tinySA не завершён до истечения тайм-аута.",
                operator_action_ru="Уменьшите полосу/число точек или проверьте USB-соединение.",
                retryable=True,
            )
        body = data.split(b"\r\n", 1)[-1]
        return body.rsplit(b"ch>", 1)[0].strip()


class RtlSdrAdapter(DeviceAdapter):
    """Acquire IQ from one explicit RTL-SDR index and compute a truthful dBFS spectrum."""

    def __init__(
        self,
        *,
        adapter_id: str,
        connection: str,
        sample_rate_hz: int,
        profile_override: str = "auto",
        clock: Clock = utc_now,
        rtlsdr_module: ModuleType | None = None,
    ) -> None:
        index = _parse_rtlsdr_index(connection)
        super().__init__(
            adapter_id=adapter_id,
            display_name=f"RTL-SDR #{index}",
            kind="rtlsdr",
            connection=f"RTLSDR:{index}",
            capabilities=frozenset({Capability.SPECTRUM_SWEEP, Capability.IQ_RX}),
            clock=clock,
        )
        self.index = index
        self.sample_rate_hz = sample_rate_hz
        self.profile_override = profile_override
        self._rtlsdr_module = rtlsdr_module
        self._receiver: Any | None = None
        self._tuning_profile = GENERIC_RTLSDR_PROFILE
        self._detected_profile = GENERIC_RTLSDR_PROFILE
        self._usb_manufacturer = ""
        self._usb_product = ""
        self._input_mode: RtlSdrInputMode | None = None
        self._applied_sample_rate_hz: int | None = None
        self._applied_center_frequency_hz: int | None = None
        self._applied_gain: str | float | None = None

    def inspect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._open_receiver()
        profile = self._tuning_profile
        return DeviceSnapshot(
            device_id=self.adapter_id,
            display_name=profile.display_name_ru,
            kind=self.kind,
            connection=self.connection,
            state=DeviceState.READY,
            health=HealthLevel.HEALTHY,
            capabilities=self.capabilities,
            driver="pyrtlsdr/librtlsdr",
            sample_rate_hz=self.sample_rate_hz,
            last_data_at=self._clock(),
            generation=1,
            metrics={
                "device_index": self.index,
                "power_unit": "dBFS",
                "tuning_profile_id": profile.profile_id,
                "detected_tuning_profile_id": self._detected_profile.profile_id,
                "profile_selection": (
                    "automatic"
                    if self.profile_override == "auto"
                    else (
                        "operator_unconfirmed_fallback"
                        if self.profile_override == "blog_v4"
                        and profile != BLOG_V4_PROFILE
                        else "operator_confirmed"
                    )
                ),
                "profile_warning_ru": (
                    "Blog V4 указан оператором, но драйвер не подтвердил "
                    "точные EEPROM-строки RTLSDRBlog / Blog V4. HF отключён; "
                    "используется безопасный диапазон 24–1766 МГц."
                    if self.profile_override == "blog_v4"
                    and profile != BLOG_V4_PROFILE
                    else ""
                ),
                "usb_manufacturer": self._usb_manufacturer or "unavailable",
                "usb_product": self._usb_product or "unavailable",
                "tuning_min_hz": profile.minimum_frequency_hz,
                "tuning_max_hz": profile.maximum_frequency_hz,
                "stable_sample_rate_max_hz": RTLSDR_STABLE_SAMPLE_RATE_HZ,
                "sample_rate_max_hz": RTLSDR_MAX_SAMPLE_RATE_HZ,
                "hf_input_mode": (
                    profile.hf_mode.value
                    if profile.hf_mode is not None
                    else "unavailable"
                ),
                "active_input_mode": (
                    self._input_mode.value
                    if self._input_mode is not None
                    else "not_tuned"
                ),
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
        self._open_receiver()
        validation = require_rtlsdr_tuning(
            self._tuning_profile,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            sample_rate_hz=self.sample_rate_hz,
        )
        input_mode = validation.input_mode or RtlSdrInputMode.TUNER
        required_samples = max(
            4_096,
            math.ceil(max(8, bins) * self.sample_rate_hz / span_hz),
        )
        fft_exponent = math.ceil(math.log2(required_samples))
        if fft_exponent > 20:
            raise AppError(
                code="SPECTRUM.RESOLUTION_UNAVAILABLE",
                message_ru="Запрошенная полоса требует слишком длинного FFT для этого режима.",
                operator_action_ru="Увеличьте полосу или уменьшите число отображаемых точек.",
                retryable=False,
            )
        fft_size = 1 << fft_exponent
        receiver = cast(Any, self._receiver)
        try:
            tuning_changed = self._apply_tuning(
                receiver,
                center_frequency_hz=center_frequency_hz,
                input_mode=input_mode,
            )
        except AppError:
            self._close_receiver()
            raise
        except Exception as exc:
            self._close_receiver()
            raise AppError(
                code="SPECTRUM.RTLSDR_TUNE_FAILED",
                message_ru="RTL-SDR не принял выбранную частоту или режим входа.",
                operator_action_ru=(
                    "Выберите частоту внутри аппаратного диапазона, "
                    "установите 2,4 MSPS и повторите."
                ),
                retryable=True,
                technical_details={
                    "device_index": self.index,
                    "profile": self._tuning_profile.profile_id,
                    "input_mode": input_mode.value,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            ) from exc
        if tuning_changed:
            try:
                receiver.read_samples(
                    _rtlsdr_settling_sample_count(self.sample_rate_hz)
                )
            except Exception as exc:
                self._close_receiver()
                raise AppError(
                    code="DEVICE.RTLSDR_READ_FAILED",
                    message_ru="Не удалось стабилизировать IQ-поток RTL-SDR после настройки.",
                    operator_action_ru=(
                        "Проверьте драйвер WinUSB, питание и USB-соединение."
                    ),
                    retryable=True,
                    technical_details={
                        "device_index": self.index,
                        "phase": "settling",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                ) from exc
        capture_sample_count = _welch_sample_count(fft_size)
        try:
            samples = np.asarray(
                receiver.read_samples(capture_sample_count),
                dtype=np.complex64,
            )
        except Exception as exc:
            self._close_receiver()
            raise AppError(
                code="DEVICE.RTLSDR_READ_FAILED",
                message_ru="Не удалось получить IQ-данные RTL-SDR.",
                operator_action_ru="Проверьте драйвер WinUSB, питание и USB-соединение.",
                retryable=True,
                technical_details={"device_index": self.index, "error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        try:
            power = _iq_to_dbfs(
                samples,
                bins,
                span_fraction=span_hz / self.sample_rate_hz,
                segment_size=fft_size,
            )
        except AppError:
            self._close_receiver()
            raise
        return SpectrumFrame(
            source_id=self.adapter_id,
            sequence=sequence,
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=power,
            captured_at=self._clock(),
            provenance=Provenance.LIVE,
            unit="dBFS",
        )

    def close(self) -> None:
        self._close_receiver()
        super().close()

    def reconnect(self) -> DeviceSnapshot:
        self._ensure_open()
        self._close_receiver()
        return self.inspect()

    def _close_receiver(self) -> None:
        receiver = self._receiver
        self._receiver = None
        self._input_mode = None
        self._applied_sample_rate_hz = None
        self._applied_center_frequency_hz = None
        self._applied_gain = None
        if receiver is not None:
            with suppress(Exception):
                receiver.close()

    def _apply_tuning(
        self,
        receiver: Any,
        *,
        center_frequency_hz: int,
        input_mode: RtlSdrInputMode,
    ) -> bool:
        """Apply only changed hardware properties and report real retuning."""

        sample_rate_changed = self._applied_sample_rate_hz != self.sample_rate_hz
        input_mode_changed = self._input_mode != input_mode
        center_changed = (
            self._applied_center_frequency_hz != center_frequency_hz
        )
        gain: str | float = "auto"
        gain_changed = self._applied_gain != gain

        if sample_rate_changed:
            receiver.sample_rate = self.sample_rate_hz
        if input_mode_changed:
            self._select_input_mode(receiver, input_mode)
        if center_changed:
            receiver.center_freq = center_frequency_hz
        if gain_changed:
            receiver.gain = gain

        self._applied_sample_rate_hz = self.sample_rate_hz
        self._applied_center_frequency_hz = center_frequency_hz
        self._applied_gain = gain
        return (
            sample_rate_changed
            or input_mode_changed
            or center_changed
            or gain_changed
        )

    def _open_receiver(self) -> None:
        if self._receiver is not None:
            return
        module = self._rtlsdr_module
        if module is None:
            try:
                module = importlib.import_module("rtlsdr")
            except ImportError as exc:
                raise AppError(
                    code="DEVICE.PYRTLSDR_MISSING",
                    message_ru="Аппаратный модуль pyrtlsdr не установлен.",
                    operator_action_ru="Установите аппаратный пакет и драйвер RTL-SDR.",
                    retryable=False,
                ) from exc
        try:
            receiver_class = cast(Any, module).RtlSdr
            self._receiver = receiver_class(device_index=self.index)
            manufacturer, product = _read_rtlsdr_usb_identity(module, self.index)
            receiver = cast(Any, self._receiver)
            direct_sampling_api = callable(
                getattr(receiver, "set_direct_sampling", None)
            )
            self._usb_manufacturer = manufacturer
            self._usb_product = product
            self._detected_profile = identify_rtlsdr_profile(
                manufacturer,
                product,
                direct_sampling_api=direct_sampling_api,
            )
            self._tuning_profile = select_rtlsdr_profile(
                self._detected_profile,
                override=self.profile_override,
                direct_sampling_api=direct_sampling_api,
            )
        except AppError:
            receiver = self._receiver
            self._receiver = None
            if receiver is not None:
                with suppress(Exception):
                    receiver.close()
            raise
        except Exception as exc:
            receiver = self._receiver
            self._receiver = None
            if receiver is not None:
                with suppress(Exception):
                    receiver.close()
            raise AppError(
                code="DEVICE.RTLSDR_OPEN_FAILED",
                message_ru="Не удалось открыть выбранный RTL-SDR.",
                operator_action_ru="Проверьте индекс устройства и драйвер WinUSB.",
                retryable=True,
                technical_details={"device_index": self.index, "error": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def _select_input_mode(
        self,
        receiver: Any,
        mode: RtlSdrInputMode,
    ) -> None:
        if mode == self._input_mode:
            return
        direct_value = 2 if mode == RtlSdrInputMode.DIRECT_Q else 0
        setter = getattr(receiver, "set_direct_sampling", None)
        if mode == RtlSdrInputMode.DIRECT_Q and not callable(setter):
            raise AppError(
                code="SPECTRUM.RTLSDR_DIRECT_SAMPLING_UNAVAILABLE",
                message_ru="Этот RTL-SDR не подтвердил управление Q-ветвью для HF.",
                operator_action_ru=(
                    "Используйте частоту от 24 МГц либо совместимый приёмник "
                    "с подтверждённым HF-входом."
                ),
                retryable=False,
            )
        if callable(setter):
            setter(direct_value)
        self._input_mode = mode


def _read_rtlsdr_usb_identity(module: ModuleType, index: int) -> tuple[str, str]:
    """Read public USB labels without serialising or exposing a serial number."""

    injected = getattr(module, "usb_identity", None)
    if (
        isinstance(injected, tuple)
        and len(injected) >= 2
        and all(isinstance(value, str) for value in injected[:2])
    ):
        return str(injected[0]), str(injected[1])

    library = getattr(module, "librtlsdr", None)
    reader = getattr(library, "rtlsdr_get_device_usb_strings", None)
    if not callable(reader):
        return "", ""
    manufacturer_buffer = (c_ubyte * 256)()
    product_buffer = (c_ubyte * 256)()
    serial_buffer = (c_ubyte * 256)()
    try:
        result = int(
            reader(
                index,
                manufacturer_buffer,
                product_buffer,
                serial_buffer,
            )
        )
    except Exception:
        return "", ""
    if result != 0:
        return "", ""
    return (
        _decode_usb_label(manufacturer_buffer),
        _decode_usb_label(product_buffer),
    )


def _decode_usb_label(buffer: Any) -> str:
    try:
        payload = bytes(buffer)
    except (TypeError, ValueError):
        return ""
    return payload.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def _parse_rtlsdr_index(connection: str) -> int:
    match = re.fullmatch(r"(?i)RTLSDR:(\d{1,3})", connection.strip())
    if match is None:
        raise ValueError("RTL-SDR connection must use RTLSDR:<index>")
    return int(match.group(1))


def _parse_tinysa_scan(payload: bytes, points: int) -> np.ndarray:
    values: list[float] = []
    for raw_line in payload.replace(b"\r", b"").split(b"\n"):
        line = raw_line.strip()
        if not line or line.lower().startswith(b"scan"):
            continue
        try:
            value = float(line.split()[0])
        except (ValueError, IndexError):
            continue
        if math.isfinite(value) and -250.0 <= value <= 100.0:
            values.append(value)
    if len(values) != points:
        raise AppError(
            code="DEVICE.TINYSA_MALFORMED_SCAN",
            message_ru="tinySA вернул неполный или повреждённый спектр.",
            operator_action_ru="Повторите измерение и проверьте версию прошивки.",
            retryable=True,
            technical_details={"expected_points": points, "received_points": len(values)},
        )
    return np.asarray(values, dtype=np.float32)


def _iq_to_dbfs(
    samples: np.ndarray,
    bins: int,
    *,
    span_fraction: float = 1.0,
    segment_size: int | None = None,
) -> np.ndarray:
    if not 0.0 < span_fraction <= 1.0:
        raise ValueError("span_fraction must be in (0, 1]")
    if samples.ndim != 1:
        raise AppError(
            code="SPECTRUM.INVALID_IQ",
            message_ru="Источник вернул IQ-данные некорректной формы.",
            operator_action_ru="Проверьте поток и частоту дискретизации.",
            retryable=True,
        )
    if segment_size is None:
        approximate_segment = samples.size // (
            1 + (_WELCH_SEGMENT_COUNT - 1) // _WELCH_OVERLAP_DIVISOR
        )
        if approximate_segment < 64:
            segment_size = 0
        else:
            segment_size = 1 << math.floor(math.log2(approximate_segment))
    if (
        segment_size < 64
        or segment_size & (segment_size - 1)
        or samples.size < _welch_sample_count(segment_size)
    ):
        raise AppError(
            code="SPECTRUM.INVALID_IQ",
            message_ru="Источник вернул недостаточно IQ-данных для устойчивого спектра.",
            operator_action_ru="Проверьте поток и частоту дискретизации.",
            retryable=True,
            technical_details={
                "received_samples": int(samples.size),
                "required_samples": (
                    _welch_sample_count(segment_size)
                    if segment_size >= 64
                    else 0
                ),
            },
        )
    if not np.all(np.isfinite(samples)):
        raise AppError(
            code="SPECTRUM.NONFINITE_IQ",
            message_ru="IQ-поток содержит недопустимые значения.",
            operator_action_ru="Перезапустите приёмник и проверьте драйвер.",
            retryable=True,
        )

    hop = segment_size // _WELCH_OVERLAP_DIVISOR
    window = np.hanning(segment_size).astype(np.float32)
    coherent_power_scale = max(
        float(np.sum(window, dtype=np.float64)) ** 2,
        np.finfo(np.float64).eps,
    )
    periodograms = np.empty(
        (_WELCH_SEGMENT_COUNT, segment_size),
        dtype=np.float64,
    )
    for index in range(_WELCH_SEGMENT_COUNT):
        start = index * hop
        segment = samples[start : start + segment_size]
        centered = segment - np.mean(segment)
        spectrum = np.fft.fft(centered * window)
        periodograms[index] = (
            np.square(np.abs(spectrum), dtype=np.float64)
            / coherent_power_scale
        )

    # A median across five 50%-overlapped periodograms rejects a one-off
    # sample impulse, which can contaminate at most two neighbouring segments.
    robust_power = np.fft.fftshift(np.median(periodograms, axis=0))
    cropped_size = max(
        8,
        min(robust_power.size, round(robust_power.size * span_fraction)),
    )
    crop_start = (robust_power.size - cropped_size) // 2
    cropped_power = robust_power[crop_start : crop_start + cropped_size]
    target = max(8, min(int(bins), cropped_power.size))
    edges = np.linspace(0, cropped_power.size, target + 1, dtype=np.int64)
    reduced_power = np.asarray(
        [
            np.mean(cropped_power[edges[index] : edges[index + 1]])
            for index in range(target)
        ],
        dtype=np.float64,
    )
    reduced_dbfs = 10.0 * np.log10(np.maximum(reduced_power, 1e-24))
    return cast(np.ndarray, np.asarray(reduced_dbfs, dtype=np.float32))


def _welch_sample_count(segment_size: int) -> int:
    hop = segment_size // _WELCH_OVERLAP_DIVISOR
    return segment_size + (_WELCH_SEGMENT_COUNT - 1) * hop


def _rtlsdr_settling_sample_count(sample_rate_hz: int) -> int:
    return max(
        _RTLSDR_MIN_SETTLING_SAMPLES,
        min(_RTLSDR_MAX_SETTLING_SAMPLES, sample_rate_hz // 100),
    )
