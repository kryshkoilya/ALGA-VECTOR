# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from threading import RLock
from typing import Protocol

from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import CapabilityStatus, DeviceSnapshot, SpectrumFrame

from .base import DeviceAdapter

_OPERABLE_STATES = {DeviceState.READY, DeviceState.STREAMING}
_DEGRADED_STATES = {
    DeviceState.DISCOVERED,
    DeviceState.PROBING,
    DeviceState.STARTING,
    DeviceState.DEGRADED,
    DeviceState.RECONNECTING,
}


class DeviceManagerLike(Protocol):
    """Runtime-facing manager contract shared by local and isolated managers."""

    @property
    def closed(self) -> bool: ...

    def refresh(self) -> tuple[DeviceSnapshot, ...]: ...

    def snapshots(self) -> tuple[DeviceSnapshot, ...]: ...

    def resolve_capabilities(
        self,
        capabilities: Iterable[Capability] | None = None,
    ) -> tuple[CapabilityStatus, ...]: ...

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None: ...

    def reconnect(self, device_id: str) -> DeviceSnapshot: ...

    def close(self) -> None: ...


class DeviceManager:
    """Single owner for adapter state, generation, and capability resolution."""

    def __init__(self, adapters: Iterable[DeviceAdapter]) -> None:
        adapter_tuple = tuple(adapters)
        identifiers = [adapter.adapter_id for adapter in adapter_tuple]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("adapter ids must be unique")
        self._adapters = adapter_tuple
        self._snapshots: dict[str, DeviceSnapshot] = {}
        self._generations: dict[str, int] = {}
        self._closed = False
        self._lock = RLock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def adapters(self) -> tuple[DeviceAdapter, ...]:
        return self._adapters

    def refresh(self) -> tuple[DeviceSnapshot, ...]:
        with self._lock:
            self._ensure_open()
            refreshed: dict[str, DeviceSnapshot] = {}
            for adapter in self._adapters:
                try:
                    current = adapter.inspect()
                except AppError as exc:
                    current = _failed_snapshot(adapter, exc.code, exc.message_ru, exc.operator_action_ru)
                except Exception as exc:  # defensive isolation at the adapter boundary
                    current = _failed_snapshot(
                        adapter,
                        "DEVICE.ADAPTER_FAILURE",
                        "Адаптер завершил проверку с ошибкой.",
                        "Откройте диагностику устройства.",
                        technical_error=f"{type(exc).__name__}: {exc}",
                    )
                previous = self._snapshots.get(current.device_id)
                generation = self._generations.get(current.device_id, current.generation)
                if previous is None:
                    generation = max(generation, 1 if current.state != DeviceState.ABSENT else 0)
                elif previous.state != current.state:
                    generation += 1
                self._generations[current.device_id] = generation
                refreshed[current.device_id] = replace(current, generation=generation)
            self._snapshots = refreshed
            return tuple(refreshed[adapter.adapter_id] for adapter in self._adapters)

    def snapshots(self) -> tuple[DeviceSnapshot, ...]:
        with self._lock:
            if not self._snapshots and not self._closed:
                return self.refresh()
            return tuple(
                self._snapshots[adapter.adapter_id]
                for adapter in self._adapters
                if adapter.adapter_id in self._snapshots
            )

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
        with self._lock:
            self._ensure_open()
            snapshots = {snapshot.device_id: snapshot for snapshot in self.snapshots()}
            last_failure: AppError | None = None
            for adapter in self._adapters:
                snapshot = snapshots[adapter.adapter_id]
                if (
                    Capability.SPECTRUM_SWEEP in adapter.capabilities
                    and snapshot.state in _OPERABLE_STATES
                    and snapshot.health != HealthLevel.ERROR
                ):
                    try:
                        frame = adapter.read_spectrum(
                            sequence=sequence,
                            center_frequency_hz=center_frequency_hz,
                            span_hz=span_hz,
                            bins=bins,
                        )
                    except AppError as exc:
                        last_failure = exc
                        self._record_read_failure(
                            adapter,
                            _failed_snapshot(
                                adapter,
                                exc.code,
                                exc.message_ru,
                                exc.operator_action_ru,
                            ),
                        )
                        continue
                    except Exception as exc:
                        last_failure = AppError(
                            code="SPECTRUM.ADAPTER_FAILURE",
                            message_ru="Источник спектра завершил чтение с ошибкой.",
                            operator_action_ru="Проверьте источник в диагностике.",
                            retryable=True,
                            technical_details={
                                "device_id": adapter.adapter_id,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        )
                        self._record_read_failure(
                            adapter,
                            _failed_snapshot(
                                adapter,
                                last_failure.code,
                                last_failure.message_ru,
                                last_failure.operator_action_ru,
                                technical_error=f"{type(exc).__name__}: {exc}",
                            ),
                        )
                        continue
                    if frame is not None:
                        return frame
            if last_failure is not None:
                raise last_failure
            return None

    def _record_read_failure(
        self,
        adapter: DeviceAdapter,
        failed: DeviceSnapshot,
    ) -> None:
        """Store one new read-failure episode without resetting generation."""

        previous = self._snapshots.get(adapter.adapter_id)
        generation = max(
            self._generations.get(adapter.adapter_id, 0),
            previous.generation if previous is not None else 0,
            failed.generation,
        ) + 1
        self._generations[adapter.adapter_id] = generation
        self._snapshots[adapter.adapter_id] = replace(
            failed,
            generation=generation,
        )

    def reconnect(self, device_id: str) -> DeviceSnapshot:
        """Reopen exactly one configured adapter without probing other devices."""

        with self._lock:
            self._ensure_open()
            adapter = next(
                (item for item in self._adapters if item.adapter_id == device_id),
                None,
            )
            if adapter is None:
                raise AppError(
                    code="DEVICE.NOT_CONFIGURED",
                    message_ru="Устройство отсутствует в конфигурации.",
                    operator_action_ru="Обновите список устройств.",
                    retryable=False,
                    technical_details={"device_id": device_id},
                )
            try:
                current = adapter.reconnect()
            except AppError as exc:
                current = _failed_snapshot(
                    adapter,
                    exc.code,
                    exc.message_ru,
                    exc.operator_action_ru,
                )
            except Exception as exc:
                current = _failed_snapshot(
                    adapter,
                    "DEVICE.RECONNECT_FAILED",
                    "Переподключение адаптера завершилось ошибкой.",
                    "Проверьте кабель, драйвер и повторите действие.",
                    technical_error=f"{type(exc).__name__}: {exc}",
                )
            previous = self._snapshots.get(device_id)
            generation = self._generations.get(device_id, 0)
            if previous is None or previous.state != current.state:
                generation += 1
            self._generations[device_id] = generation
            updated = replace(current, generation=generation)
            self._snapshots[device_id] = updated
            return updated

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for adapter in reversed(self._adapters):
                try:
                    adapter.close()
                except Exception:
                    # Shutdown continues so one vendor boundary cannot leak the rest.
                    continue
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise AppError(
                code="DEVICE.MANAGER_CLOSED",
                message_ru="Менеджер устройств остановлен.",
                operator_action_ru="Запустите приложение повторно.",
                retryable=False,
            )

    def __enter__(self) -> DeviceManager:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _resolve_capability(
    capability: Capability,
    snapshots: tuple[DeviceSnapshot, ...],
) -> CapabilityStatus:
    providers = [snapshot for snapshot in snapshots if capability in snapshot.capabilities]
    if not providers:
        return CapabilityStatus(
            capability=capability,
            state=CapabilityState.BLOCKED,
            reason_code="CAPABILITY.NO_PROVIDER",
            explanation_ru="Для функции не настроен провайдер.",
            action_ru="Настройте совместимое устройство или модуль.",
        )
    available = [
        provider
        for provider in providers
        if provider.state in _OPERABLE_STATES and provider.health == HealthLevel.HEALTHY
    ]
    if available:
        return CapabilityStatus(capability=capability, state=CapabilityState.AVAILABLE)
    degraded = [
        provider
        for provider in providers
        if provider.state in _OPERABLE_STATES | _DEGRADED_STATES
        and provider.health in {HealthLevel.HEALTHY, HealthLevel.DEGRADED, HealthLevel.UNKNOWN}
    ]
    if degraded:
        return CapabilityStatus(
            capability=capability,
            state=CapabilityState.DEGRADED,
            reason_code="CAPABILITY.PROVIDER_DEGRADED",
            explanation_ru="Функция доступна с ограничениями.",
            action_ru="Проверьте состояние провайдера в диагностике.",
        )
    first = providers[0]
    return CapabilityStatus(
        capability=capability,
        state=CapabilityState.BLOCKED,
        reason_code=first.reason_code or "CAPABILITY.PROVIDER_UNAVAILABLE",
        explanation_ru=first.reason_ru or "Все провайдеры функции недоступны.",
        action_ru=first.recommended_action_ru or "Проверьте подключение устройства.",
    )


def resolve_snapshot_capabilities(
    snapshots: tuple[DeviceSnapshot, ...],
    capabilities: Iterable[Capability] | None = None,
) -> tuple[CapabilityStatus, ...]:
    """Resolve capabilities from an immutable snapshot set.

    The isolated hardware manager uses the same pure resolver when its worker
    is unavailable, so capability state cannot remain optimistically stale.
    """

    requested = tuple(capabilities) if capabilities is not None else tuple(Capability)
    return tuple(
        _resolve_capability(capability, snapshots)
        for capability in sorted(set(requested), key=lambda item: item.value)
    )


def _failed_snapshot(
    adapter: DeviceAdapter,
    reason_code: str,
    reason_ru: str,
    action_ru: str,
    *,
    technical_error: str | None = None,
) -> DeviceSnapshot:
    metrics: dict[str, float | int | str] = {}
    if technical_error is not None:
        metrics["technical_error"] = technical_error
    return DeviceSnapshot(
        device_id=adapter.adapter_id,
        display_name=adapter.display_name,
        kind=adapter.kind,
        connection="[ошибка проверки]",
        state=DeviceState.FAILED,
        health=HealthLevel.ERROR,
        capabilities=adapter.capabilities,
        reason_code=reason_code,
        reason_ru=reason_ru,
        recommended_action_ru=action_ru,
        metrics=metrics,
    )
