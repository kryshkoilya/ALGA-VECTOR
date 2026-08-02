"""Crash-isolated process boundary for explicitly configured RF hardware."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import RLock
from typing import Any, cast

from alga_vector.config.models import AppConfig, DevicesConfig, SpectrumConfig
from alga_vector.domain.enums import (
    Capability,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import (
    CapabilityStatus,
    DeviceSnapshot,
    SpectrumFrame,
    utc_now,
)

from .adapters import build_adapters
from .base import Clock
from .manager import DeviceManager, DeviceManagerLike, resolve_snapshot_capabilities

_PROTOCOL_VERSION = 1
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 5.0
_DEFAULT_CONTROL_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 15.0
_DEFAULT_REFRESH_INTERVAL_SECONDS = 2.0
_PROCESS_JOIN_TIMEOUT_SECONDS = 0.5
# Reconnect is an operator-facing synchronous action. Keep only its replacement
# worker handshake short; the initial application startup retains the configured
# (and deliberately more generous) startup timeout.
_RECONNECT_STARTUP_TIMEOUT_SECONDS = 1.0
_RECONNECT_PROCESS_JOIN_TIMEOUT_SECONDS = 0.2
_RECONNECT_TOTAL_TIMEOUT_SECONDS = 1.75
_RECONNECT_CLEANUP_RESERVE_SECONDS = 0.2

WorkerTarget = Callable[
    [Connection, dict[str, object], dict[str, object]],
    None,
]

_CAPABILITIES_BY_KIND: dict[str, frozenset[Capability]] = {
    "tinysa": frozenset({Capability.SPECTRUM_SWEEP}),
    "rtlsdr": frozenset({Capability.SPECTRUM_SWEEP, Capability.IQ_RX}),
    "hackrf": frozenset({Capability.SPECTRUM_SWEEP, Capability.IQ_RX}),
}


class HardwareProcessDeviceManager:
    """Device-manager proxy backed by one disposable ``spawn`` worker.

    The first hardware inspection during runtime startup is bounded and
    synchronous. Periodic refreshes and spectrum reads are subsequently
    submitted and polled without waiting, so a GUI timer never sits inside a
    vendor/serial read. A timed-out or crashed worker is terminated and every
    enabled provider becomes fail-closed until an explicit reconnect.
    """

    def __init__(
        self,
        devices: DevicesConfig,
        spectrum: SpectrumConfig,
        *,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        control_timeout_seconds: float = _DEFAULT_CONTROL_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_INTERVAL_SECONDS,
        mp_context: Any | None = None,
        worker_target: WorkerTarget | None = None,
    ) -> None:
        for value, name in (
            (startup_timeout_seconds, "startup_timeout_seconds"),
            (control_timeout_seconds, "control_timeout_seconds"),
            (read_timeout_seconds, "read_timeout_seconds"),
            (refresh_interval_seconds, "refresh_interval_seconds"),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self._devices = devices
        self._spectrum = spectrum
        self._startup_timeout_seconds = startup_timeout_seconds
        self._control_timeout_seconds = control_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._context = mp_context or multiprocessing.get_context("spawn")
        self._worker_target = worker_target or _hardware_worker
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._closed = False
        self._lock = RLock()
        self._request_id = 0
        self._cached_snapshots: tuple[DeviceSnapshot, ...] = ()
        self._initial_refresh_complete = False
        self._last_refresh_requested = 0.0
        self._last_failure: AppError | None = None
        self._pending: _PendingCommand | None = None
        self._completed_spectrum: SpectrumFrame | None = None
        self._completed_read_error: AppError | None = None
        self._last_read_request_accepted = False
        self._start_worker()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def last_read_request_accepted(self) -> bool:
        """Whether the latest spectrum poll submitted a new worker request.

        ``read_spectrum`` is intentionally a non-blocking poll.  A ``None``
        result therefore cannot tell the caller whether a new capture was
        submitted or an older capture is merely still pending.  This
        backwards-compatible side-channel lets the acquisition coordinator
        allocate source sequence numbers only to accepted requests.
        """

        with self._lock:
            return self._last_read_request_accepted

    def refresh(self) -> tuple[DeviceSnapshot, ...]:
        with self._lock:
            self._ensure_open()
            self._drain_pending()
            if self._last_failure is not None:
                return self._cached_snapshots
            if not self._initial_refresh_complete:
                try:
                    result = self._rpc(
                        "refresh",
                        timeout_seconds=self._control_timeout_seconds,
                    )
                    self._cache_snapshot_result(result)
                except AppError as exc:
                    self._mark_worker_failed(exc)
                else:
                    self._initial_refresh_complete = True
                    self._last_refresh_requested = time.monotonic()
                return self._cached_snapshots

            now = time.monotonic()
            if (
                self._pending is None
                and now - self._last_refresh_requested >= self._refresh_interval_seconds
                and self._send_async(
                    "refresh",
                    timeout_seconds=self._control_timeout_seconds,
                )
            ):
                self._last_refresh_requested = now
            return self._cached_snapshots

    def snapshots(self) -> tuple[DeviceSnapshot, ...]:
        with self._lock:
            self._ensure_open()
            self._drain_pending()
            if not self._initial_refresh_complete and self._last_failure is None:
                return self.refresh()
            return self._cached_snapshots

    def resolve_capabilities(
        self,
        capabilities: Iterable[Capability] | None = None,
    ) -> tuple[CapabilityStatus, ...]:
        with self._lock:
            return resolve_snapshot_capabilities(self.snapshots(), capabilities)

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        """Poll a completed frame and submit the next acquisition.

        No wait occurs here. The worker-side command still has a hard deadline,
        enforced on the next periodic poll.
        """

        with self._lock:
            self._ensure_open()
            self._last_read_request_accepted = False
            self._drain_pending()
            if self._last_failure is not None:
                raise self._last_failure
            if self._completed_read_error is not None:
                error = self._completed_read_error
                self._completed_read_error = None
                raise error
            completed = self._completed_spectrum
            self._completed_spectrum = None
            if self._pending is None:
                self._last_read_request_accepted = self._send_async(
                    "read_spectrum",
                    timeout_seconds=self._read_timeout_seconds,
                    sequence=sequence,
                    center_frequency_hz=center_frequency_hz,
                    span_hz=span_hz,
                    bins=bins,
                )
            return completed

    def reconnect(self, device_id: str) -> DeviceSnapshot:
        """Restart a failed worker, then reconnect exactly one configured device."""

        with self._lock:
            deadline = time.monotonic() + _RECONNECT_TOTAL_TIMEOUT_SECONDS
            self._ensure_open()
            self._drain_pending()
            if self._pending is not None or self._last_failure is not None:
                previous_failure = self._last_failure
                self._restart_worker(
                    startup_timeout_seconds=min(
                        self._startup_timeout_seconds,
                        _RECONNECT_STARTUP_TIMEOUT_SECONDS,
                    ),
                    deadline=deadline,
                )
                if self._last_failure is not None and previous_failure is not None:
                    self._restore_failure_after_recovery(
                        previous_failure,
                        self._last_failure,
                    )
            if self._last_failure is not None:
                return self._snapshot_for(device_id)
            rpc_timeout = min(
                self._control_timeout_seconds,
                max(
                    0.0,
                    deadline - time.monotonic() - _RECONNECT_CLEANUP_RESERVE_SECONDS,
                ),
            )
            if rpc_timeout <= 0.0:
                self._mark_worker_failed(
                    _worker_timeout_error("бюджет переподключения"),
                    transport_deadline=deadline,
                )
                return self._snapshot_for(device_id)
            try:
                result = self._rpc(
                    "reconnect",
                    timeout_seconds=rpc_timeout,
                    device_id=device_id,
                )
            except AppError as exc:
                if exc.code.startswith("DEVICE.WORKER_"):
                    self._mark_worker_failed(
                        exc,
                        transport_deadline=deadline,
                    )
                    return self._snapshot_for(device_id)
                raise
            if not isinstance(result, DeviceSnapshot):
                error = _worker_protocol_error("reconnect result has an invalid type")
                self._mark_worker_failed(
                    error,
                    transport_deadline=deadline,
                )
                return self._snapshot_for(device_id)
            by_id = {item.device_id: item for item in self._cached_snapshots}
            by_id[result.device_id] = result
            self._cached_snapshots = tuple(
                by_id[item.id] for item in self._devices.adapters if item.id in by_id
            )
            self._last_failure = None
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Never wait for an in-flight native read during shutdown.
            if self._pending is None and self._worker_is_alive():
                with suppress(AppError):
                    self._rpc("close", timeout_seconds=min(2.0, self._control_timeout_seconds))
            self._stop_transport()
            self._pending = None
            self._completed_spectrum = None
            self._last_read_request_accepted = False
            self._closed = True

    def __enter__(self) -> HardwareProcessDeviceManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _start_worker(
        self,
        *,
        startup_timeout_seconds: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self._last_failure = None
        self._pending = None
        self._completed_spectrum = None
        self._completed_read_error = None
        self._last_read_request_accepted = False
        self._initial_refresh_complete = False
        parent_connection: Connection | None = None
        child_connection: Connection | None = None
        try:
            raw_parent, raw_child = self._context.Pipe(duplex=True)
            parent_connection = cast(Connection, raw_parent)
            child_connection = cast(Connection, raw_child)
            process = self._context.Process(
                target=self._worker_target,
                args=(
                    child_connection,
                    cast(dict[str, object], self._devices.model_dump(mode="python")),
                    cast(dict[str, object], self._spectrum.model_dump(mode="python")),
                ),
                name="ALGA-VECTOR-hardware",
                daemon=True,
            )
            process.start()
            raw_child.close()
            child_connection = None
            self._process = process
            self._connection = parent_connection
            parent_connection = None
            connection = self._connection
            startup_timeout = (
                self._startup_timeout_seconds
                if startup_timeout_seconds is None
                else startup_timeout_seconds
            )
            if deadline is not None:
                startup_timeout = min(
                    startup_timeout,
                    max(
                        0.0,
                        deadline - time.monotonic() - _RECONNECT_CLEANUP_RESERVE_SECONDS,
                    ),
                )
            if connection is None or not connection.poll(startup_timeout):
                self._mark_worker_failed(
                    _worker_timeout_error("запуск изолированного процесса"),
                    transport_deadline=deadline,
                )
                return
            message = connection.recv()
            if not _is_ready_message(message):
                self._mark_worker_failed(
                    _worker_start_error(message),
                    transport_deadline=deadline,
                )
        except (EOFError, OSError, RuntimeError, ValueError) as exc:
            self._mark_worker_failed(
                AppError(
                    code="DEVICE.WORKER_START_FAILED",
                    message_ru="Изолированный процесс оборудования не запущен.",
                    operator_action_ru="Перезапустите приложение и проверьте аппаратный пакет.",
                    retryable=True,
                    technical_details={"error_type": type(exc).__name__},
                ),
                transport_deadline=deadline,
            )
        finally:
            if parent_connection is not None:
                parent_connection.close()
            if child_connection is not None:
                child_connection.close()

    def _restart_worker(
        self,
        *,
        startup_timeout_seconds: float,
        deadline: float,
    ) -> None:
        self._stop_transport(deadline=deadline)
        if time.monotonic() >= deadline:
            self._mark_worker_failed(
                _worker_timeout_error("остановка предыдущего процесса"),
                transport_deadline=deadline,
            )
            return
        self._start_worker(
            startup_timeout_seconds=startup_timeout_seconds,
            deadline=deadline,
        )

    def _send_async(
        self,
        command: str,
        *,
        timeout_seconds: float,
        **kwargs: object,
    ) -> bool:
        if self._pending is not None:
            return False
        try:
            request_id = self._send(command, kwargs)
        except AppError as exc:
            self._mark_worker_failed(exc)
            return False
        self._pending = _PendingCommand(
            request_id=request_id,
            command=command,
            deadline=time.monotonic() + timeout_seconds,
        )
        return True

    def _drain_pending(self) -> None:
        pending = self._pending
        if pending is None:
            self._detect_worker_crash()
            return
        connection = self._connection
        if connection is None or not self._worker_is_alive():
            self._mark_worker_failed(_worker_crashed_error())
            return
        if time.monotonic() >= pending.deadline:
            self._mark_worker_failed(_worker_timeout_error(f"команда {pending.command}"))
            return
        try:
            if not connection.poll(0.0):
                return
            response = connection.recv()
            result = self._decode_response(response, pending.request_id)
        except (EOFError, OSError, ValueError) as exc:
            self._mark_worker_failed(_worker_crashed_error(error_type=type(exc).__name__))
            return
        except AppError as exc:
            self._pending = None
            if exc.code.startswith("DEVICE.WORKER_"):
                self._mark_worker_failed(exc)
            elif pending.command == "read_spectrum":
                self._completed_read_error = exc
            else:
                self._cached_snapshots = self._failure_snapshots(exc)
            return
        self._pending = None
        if pending.command == "refresh":
            self._cache_snapshot_result(result)
            self._initial_refresh_complete = True
        elif pending.command == "read_spectrum":
            if result is not None and not isinstance(result, SpectrumFrame):
                self._mark_worker_failed(
                    _worker_protocol_error("spectrum result has an invalid type")
                )
            else:
                self._completed_spectrum = result
                if isinstance(result, SpectrumFrame):
                    self._publish_completed_frame(result)

    def _publish_completed_frame(self, frame: SpectrumFrame) -> None:
        """Reflect worker capture proof immediately in the cached snapshot."""

        updated: list[DeviceSnapshot] = []
        for snapshot in self._cached_snapshots:
            if snapshot.device_id != frame.source_id:
                updated.append(snapshot)
                continue
            metrics = dict(snapshot.metrics)
            successes = metrics.get("capture_success_count", 0)
            success_count = (
                int(successes)
                if isinstance(successes, (int, float))
                and not isinstance(successes, bool)
                else 0
            ) + 1
            metrics.update(
                {
                    "capture_success_count": success_count,
                    "capture_confirmed": 1,
                    "capture_active": 1,
                    "last_capture_sequence": frame.sequence,
                    "last_capture_bins": int(frame.power_dbm.size),
                    "last_capture_peak_level": frame.peak_level,
                    "last_capture_unit": frame.unit,
                    "last_capture_provenance": frame.provenance.value,
                    "last_capture_span_hz": frame.span_hz,
                }
            )
            updated.append(
                replace(
                    snapshot,
                    state=DeviceState.STREAMING,
                    health=HealthLevel.HEALTHY,
                    center_frequency_hz=frame.center_frequency_hz,
                    last_data_at=frame.captured_at,
                    metrics=metrics,
                )
            )
        self._cached_snapshots = tuple(updated)

    def _rpc(
        self,
        command: str,
        *,
        timeout_seconds: float,
        **kwargs: object,
    ) -> object:
        if self._pending is not None:
            raise _worker_protocol_error("synchronous command overlaps an async command")
        request_id = self._send(command, kwargs)
        connection = self._connection
        if connection is None:
            raise _worker_crashed_error()
        try:
            if not connection.poll(timeout_seconds):
                raise _worker_timeout_error(f"команда {command}")
            response = connection.recv()
        except (EOFError, OSError, ValueError) as exc:
            raise _worker_crashed_error(error_type=type(exc).__name__) from exc
        return self._decode_response(response, request_id)

    def _send(self, command: str, kwargs: Mapping[str, object]) -> int:
        self._detect_worker_crash()
        if self._last_failure is not None:
            raise self._last_failure
        connection = self._connection
        if connection is None:
            raise _worker_crashed_error()
        self._request_id += 1
        request_id = self._request_id
        try:
            connection.send(
                {
                    "protocol": _PROTOCOL_VERSION,
                    "id": request_id,
                    "command": command,
                    "kwargs": dict(kwargs),
                }
            )
        except (BrokenPipeError, EOFError, OSError, ValueError) as exc:
            raise _worker_crashed_error(error_type=type(exc).__name__) from exc
        return request_id

    def _decode_response(self, response: object, request_id: int) -> object:
        if not isinstance(response, dict):
            raise _worker_protocol_error("worker response is not a mapping")
        payload = cast(dict[str, Any], response)
        if (
            payload.get("protocol") != _PROTOCOL_VERSION
            or payload.get("id") != request_id
            or not isinstance(payload.get("ok"), bool)
        ):
            raise _worker_protocol_error("worker response header is invalid")
        snapshots = payload.get("snapshots")
        if snapshots is not None:
            self._cache_snapshot_result(snapshots)
        if not payload["ok"]:
            raise _app_error_from_payload(payload.get("error"))
        return payload.get("result")

    def _cache_snapshot_result(self, result: object) -> None:
        if not isinstance(result, tuple) or not all(
            isinstance(item, DeviceSnapshot) for item in result
        ):
            raise _worker_protocol_error("snapshot result has an invalid type")
        self._cached_snapshots = cast(tuple[DeviceSnapshot, ...], result)

    def _detect_worker_crash(self) -> None:
        if self._last_failure is None and not self._worker_is_alive():
            self._mark_worker_failed(_worker_crashed_error())

    def _worker_is_alive(self) -> bool:
        process = self._process
        if process is None:
            return False
        try:
            return process.is_alive()
        except (ValueError, OSError):
            return False

    def _mark_worker_failed(
        self,
        error: AppError,
        *,
        transport_deadline: float | None = None,
    ) -> None:
        if self._last_failure is not None and self._process is None:
            return
        self._stop_transport(deadline=transport_deadline)
        self._pending = None
        self._completed_spectrum = None
        self._completed_read_error = None
        self._last_read_request_accepted = False
        self._last_failure = error
        self._cached_snapshots = self._failure_snapshots(error)
        self._initial_refresh_complete = True

    def _restore_failure_after_recovery(
        self,
        previous_failure: AppError,
        recovery_failure: AppError,
    ) -> None:
        self._last_failure = AppError(
            code=previous_failure.code,
            message_ru=previous_failure.message_ru,
            operator_action_ru=previous_failure.operator_action_ru,
            severity=previous_failure.severity,
            retryable=previous_failure.retryable,
            technical_details={
                **previous_failure.technical_details,
                "recovery_error_code": recovery_failure.code,
                "recovery_error_type": recovery_failure.technical_details.get(
                    "error_type",
                    "",
                ),
            },
        )
        self._cached_snapshots = self._failure_snapshots(self._last_failure)

    def _failure_snapshots(self, error: AppError) -> tuple[DeviceSnapshot, ...]:
        previous = {item.device_id: item for item in self._cached_snapshots}
        snapshots: list[DeviceSnapshot] = []
        for configured in self._devices.adapters:
            old = previous.get(configured.id)
            generation = (old.generation if old is not None else 0) + 1
            if configured.enabled:
                state = DeviceState.FAILED
                health = HealthLevel.ERROR
                code = error.code
                reason = error.message_ru
                action = error.operator_action_ru
            else:
                state = DeviceState.DISABLED
                health = HealthLevel.UNKNOWN
                code = "DEVICE.DISABLED_BY_CONFIG"
                reason = "Устройство отключено в конфигурации."
                action = "Включите адаптер в настройках при необходимости."
            snapshots.append(
                DeviceSnapshot(
                    device_id=configured.id,
                    display_name=_display_name(configured.kind),
                    kind=configured.kind,
                    connection="[изолированный worker недоступен]",
                    state=state,
                    health=health,
                    capabilities=_CAPABILITIES_BY_KIND.get(
                        configured.kind,
                        frozenset(),
                    ),
                    reason_code=code,
                    reason_ru=reason,
                    recommended_action_ru=action,
                    generation=generation,
                    metrics={"worker_isolated": 1},
                )
            )
        return tuple(snapshots)

    def _snapshot_for(self, device_id: str) -> DeviceSnapshot:
        snapshot = next(
            (item for item in self._cached_snapshots if item.device_id == device_id),
            None,
        )
        if snapshot is not None:
            return snapshot
        raise AppError(
            code="DEVICE.NOT_CONFIGURED",
            message_ru="Устройство отсутствует в конфигурации.",
            operator_action_ru="Обновите список устройств.",
            retryable=False,
            technical_details={"device_id": device_id},
        )

    def _stop_transport(
        self,
        *,
        deadline: float | None = None,
    ) -> None:
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        if connection is not None:
            with suppress(OSError):
                connection.close()
        if process is None:
            return
        alive = False
        with suppress(OSError, ValueError):
            alive = process.is_alive()
        if alive:
            with suppress(OSError, ValueError):
                process.terminate()
            with suppress(OSError, ValueError):
                process.join(self._process_join_timeout(deadline))
        with suppress(OSError, ValueError):
            alive = process.is_alive()
        if alive:
            with suppress(OSError, ValueError):
                process.kill()
            with suppress(OSError, ValueError):
                process.join(self._process_join_timeout(deadline))
        else:
            with suppress(OSError, ValueError):
                process.join(0)
        with suppress(OSError, ValueError):
            process.close()

    @staticmethod
    def _process_join_timeout(deadline: float | None) -> float:
        if deadline is None:
            return _PROCESS_JOIN_TIMEOUT_SECONDS
        return min(
            _RECONNECT_PROCESS_JOIN_TIMEOUT_SECONDS,
            max(0.0, deadline - time.monotonic()),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise AppError(
                code="DEVICE.MANAGER_CLOSED",
                message_ru="Менеджер устройств остановлен.",
                operator_action_ru="Запустите приложение повторно.",
                retryable=False,
            )


class _PendingCommand:
    __slots__ = ("command", "deadline", "request_id")

    def __init__(self, *, request_id: int, command: str, deadline: float) -> None:
        self.request_id = request_id
        self.command = command
        self.deadline = deadline


def has_enabled_real_hardware(config: AppConfig) -> bool:
    """Return whether live configuration contains an enabled native adapter."""

    if config.mode != "live" or not config.devices.enable_real_adapters:
        return False
    return any(
        adapter.enabled
        and (
            (adapter.kind == "tinysa" and adapter.connection.upper() != "SIM:TINYSA")
            or (adapter.kind == "rtlsdr" and adapter.connection.upper() != "SIM:RTLSDR")
            or adapter.kind == "hackrf"
        )
        for adapter in config.devices.adapters
    )


def build_device_manager(
    config: AppConfig,
    *,
    clock: Clock = utc_now,
) -> DeviceManagerLike:
    """Select the isolated real-hardware path or the local safe/demo path."""

    if has_enabled_real_hardware(config):
        return HardwareProcessDeviceManager(config.devices, config.spectrum)
    return DeviceManager(build_adapters(config.devices, config.spectrum, clock=clock))


def _hardware_worker(
    connection: Connection,
    devices_payload: dict[str, object],
    spectrum_payload: dict[str, object],
) -> None:
    """Worker entry point; all native/COM adapter ownership stays below it."""

    manager: DeviceManager | None = None
    try:
        devices = DevicesConfig.model_validate(devices_payload)
        spectrum = SpectrumConfig.model_validate(spectrum_payload)
        manager = DeviceManager(build_adapters(devices, spectrum))
        connection.send({"type": "ready", "protocol": _PROTOCOL_VERSION})
        while True:
            request = connection.recv()
            if not isinstance(request, dict):
                raise ValueError("request is not a mapping")
            payload = cast(dict[str, Any], request)
            request_id = payload.get("id")
            command = payload.get("command")
            kwargs = payload.get("kwargs")
            if (
                payload.get("protocol") != _PROTOCOL_VERSION
                or not isinstance(request_id, int)
                or not isinstance(command, str)
                or not isinstance(kwargs, dict)
            ):
                raise ValueError("request header is invalid")
            try:
                result = _execute_worker_command(
                    manager,
                    command,
                    cast(dict[str, object], kwargs),
                )
            except AppError as exc:
                connection.send(
                    {
                        "protocol": _PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": False,
                        "error": _app_error_payload(exc),
                        "snapshots": manager.snapshots(),
                    }
                )
                continue
            except Exception as exc:
                error = AppError(
                    code="DEVICE.WORKER_COMMAND_FAILED",
                    message_ru="Команда оборудования завершилась внутренней ошибкой.",
                    operator_action_ru="Откройте диагностику и переподключите устройство.",
                    retryable=True,
                    technical_details={"error_type": type(exc).__name__},
                )
                connection.send(
                    {
                        "protocol": _PROTOCOL_VERSION,
                        "id": request_id,
                        "ok": False,
                        "error": _app_error_payload(error),
                        "snapshots": manager.snapshots(),
                    }
                )
                continue
            connection.send(
                {
                    "protocol": _PROTOCOL_VERSION,
                    "id": request_id,
                    "ok": True,
                    "result": result,
                    "snapshots": manager.snapshots(),
                }
            )
            if command == "close":
                break
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception as exc:
        with suppress(BrokenPipeError, OSError):
            connection.send(
                {
                    "type": "startup_error",
                    "protocol": _PROTOCOL_VERSION,
                    "error": _app_error_payload(
                        AppError(
                            code="DEVICE.WORKER_START_FAILED",
                            message_ru="Изолированный процесс оборудования не инициализирован.",
                            operator_action_ru=("Проверьте конфигурацию и аппаратный пакет."),
                            retryable=True,
                            technical_details={"error_type": type(exc).__name__},
                        )
                    ),
                }
            )
    finally:
        if manager is not None:
            manager.close()
        connection.close()


def _execute_worker_command(
    manager: DeviceManager,
    command: str,
    kwargs: dict[str, object],
) -> object:
    if command == "refresh":
        return manager.refresh()
    if command == "snapshots":
        return manager.snapshots()
    if command == "resolve_capabilities":
        raw = kwargs.get("capabilities")
        capabilities = (
            tuple(Capability(str(item)) for item in cast(Iterable[object], raw))
            if raw is not None
            else None
        )
        return manager.resolve_capabilities(capabilities)
    if command == "read_spectrum":
        return manager.read_spectrum(
            sequence=int(cast(int, kwargs["sequence"])),
            center_frequency_hz=int(cast(int, kwargs["center_frequency_hz"])),
            span_hz=int(cast(int, kwargs["span_hz"])),
            bins=int(cast(int, kwargs["bins"])),
        )
    if command == "reconnect":
        return manager.reconnect(str(kwargs["device_id"]))
    if command == "close":
        manager.close()
        return None
    raise AppError(
        code="DEVICE.WORKER_UNKNOWN_COMMAND",
        message_ru="Изолированный процесс получил неизвестную команду.",
        operator_action_ru="Перезапустите приложение.",
        retryable=False,
        technical_details={"command": command},
    )


def _app_error_payload(error: AppError) -> dict[str, object]:
    return {
        "code": error.code,
        "message_ru": error.message_ru,
        "operator_action_ru": error.operator_action_ru,
        "severity": error.severity.value,
        "retryable": error.retryable,
        "technical_details": error.technical_details,
    }


def _app_error_from_payload(payload: object) -> AppError:
    if not isinstance(payload, dict):
        return _worker_protocol_error("worker error payload is invalid")
    raw = cast(dict[str, Any], payload)
    try:
        severity = IncidentSeverity(str(raw.get("severity", IncidentSeverity.ERROR.value)))
    except ValueError:
        severity = IncidentSeverity.ERROR
    details = raw.get("technical_details")
    return AppError(
        code=str(raw.get("code", "DEVICE.WORKER_COMMAND_FAILED")),
        message_ru=str(
            raw.get(
                "message_ru",
                "Команда оборудования завершилась ошибкой.",
            )
        ),
        operator_action_ru=str(
            raw.get(
                "operator_action_ru",
                "Откройте диагностику и переподключите устройство.",
            )
        ),
        severity=severity,
        retryable=bool(raw.get("retryable", False)),
        technical_details=(cast(dict[str, Any], details) if isinstance(details, dict) else {}),
    )


def _is_ready_message(message: object) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "ready"
        and message.get("protocol") == _PROTOCOL_VERSION
    )


def _worker_start_error(message: object) -> AppError:
    if isinstance(message, dict):
        return _app_error_from_payload(message.get("error"))
    return _worker_protocol_error("worker startup response is invalid")


def _worker_timeout_error(operation: str) -> AppError:
    return AppError(
        code="DEVICE.WORKER_TIMEOUT",
        message_ru="Изолированный процесс оборудования не ответил вовремя.",
        operator_action_ru="Переподключите устройство; при повторе проверьте драйвер и USB.",
        retryable=True,
        technical_details={"operation": operation},
    )


def _worker_crashed_error(*, error_type: str | None = None) -> AppError:
    details: dict[str, object] = {}
    if error_type is not None:
        details["error_type"] = error_type
    return AppError(
        code="DEVICE.WORKER_CRASHED",
        message_ru="Изолированный процесс оборудования аварийно завершился.",
        operator_action_ru="Переподключите устройство; основной интерфейс сохранит работу.",
        retryable=True,
        technical_details=details,
    )


def _worker_protocol_error(details: str) -> AppError:
    return AppError(
        code="DEVICE.WORKER_PROTOCOL_ERROR",
        message_ru="Нарушен протокол изолированного процесса оборудования.",
        operator_action_ru="Перезапустите приложение и проверьте версию аппаратного пакета.",
        retryable=False,
        technical_details={"details": details},
    )


def _display_name(kind: str) -> str:
    return {
        "tinysa": "tinySA",
        "rtlsdr": "RTL-SDR",
        "hackrf": "HackRF One · RX",
    }.get(kind, kind.upper())


__all__ = [
    "HardwareProcessDeviceManager",
    "build_device_manager",
    "has_enabled_real_hardware",
]
