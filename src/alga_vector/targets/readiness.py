"""Canonical seven-role sensor-readiness interpretation."""

# ruff: noqa: RUF001

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

from alga_vector.acoustics import AcousticDataQuality, AcousticLifecycle
from alga_vector.airspace import AirspaceFeedState
from alga_vector.direction import DirectionSnapshot, DirectionSource
from alga_vector.domain.enums import Capability, DeviceState, HealthLevel
from alga_vector.domain.models import DeviceSnapshot, SystemSnapshot

from .models import (
    SensorReadiness,
    SensorReadinessLevel,
    SensorReadinessSnapshot,
    SensorRole,
)

_ROLE_ORDER = (
    SensorRole.TINYSA,
    SensorRole.RTL_SDR,
    SensorRole.KRAKEN_SDR,
    SensorRole.ACOUSTIC,
    SensorRole.ADSB,
    SensorRole.PASSIVE_RADAR,
    SensorRole.FUSION,
)
_DISPLAY_NAME = {
    SensorRole.TINYSA: "TinySA",
    SensorRole.RTL_SDR: "RTL-SDR",
    SensorRole.KRAKEN_SDR: "KrakenSDR",
    SensorRole.ACOUSTIC: "Acoustic",
    SensorRole.ADSB: "ADS-B",
    SensorRole.PASSIVE_RADAR: "Passive radar",
    SensorRole.FUSION: "Fusion",
}
_UNAVAILABLE_IMPACT = {
    SensorRole.TINYSA: "Быстрый RF-триггер недоступен.",
    SensorRole.RTL_SDR: "Спектральное RF-наблюдение этим приёмником недоступно.",
    SensorRole.KRAKEN_SDR: "Направление цели не определяется.",
    SensorRole.ACOUSTIC: "Звуковое подтверждение отключено.",
    SensorRole.ADSB: "Гражданский кооперативный контекст недоступен.",
    SensorRole.PASSIVE_RADAR: "Пассивное радиолокационное подтверждение недоступно.",
    SensorRole.FUSION: "Межсенсорное подтверждение недоступно.",
}


class SensorReadinessInterpreter:
    """Map runtime state to stable ready/limited/unavailable operator slots."""

    def __init__(self, *, sensor_stale_after_seconds: float = 5.0) -> None:
        if (
            not math.isfinite(sensor_stale_after_seconds)
            or sensor_stale_after_seconds <= 0.0
        ):
            raise ValueError("sensor_stale_after_seconds must be finite and positive")
        self._stale_after_seconds = sensor_stale_after_seconds

    def interpret(
        self,
        snapshot: SystemSnapshot,
        *,
        now: datetime | None = None,
    ) -> SensorReadinessSnapshot:
        evaluated_at = snapshot.captured_at if now is None else now
        _require_aware(evaluated_at, "now")
        if evaluated_at < snapshot.captured_at:
            raise ValueError("readiness time cannot precede snapshot capture")

        grouped: dict[SensorRole, list[DeviceSnapshot]] = {
            role: [] for role in _ROLE_ORDER
        }
        for device in snapshot.devices:
            for role in _roles_for_device(device):
                grouped[role].append(device)

        items: dict[SensorRole, SensorReadiness] = {}
        for role in _ROLE_ORDER[:-1]:
            items[role] = self._from_devices(
                role,
                grouped[role],
                evaluated_at,
            )

        items[SensorRole.KRAKEN_SDR] = self._direction_override(
            items[SensorRole.KRAKEN_SDR],
            snapshot,
            evaluated_at,
        )
        items[SensorRole.ACOUSTIC] = self._acoustic_override(
            items[SensorRole.ACOUSTIC],
            snapshot,
            evaluated_at,
        )
        items[SensorRole.ADSB] = self._adsb_override(
            items[SensorRole.ADSB],
            snapshot,
            evaluated_at,
        )
        items[SensorRole.FUSION] = self._fusion_readiness(
            snapshot,
            items,
            evaluated_at,
        )
        return SensorReadinessSnapshot(
            generated_at=evaluated_at,
            sensors=tuple(items[role] for role in _ROLE_ORDER),
        )

    def _from_devices(
        self,
        role: SensorRole,
        devices: Iterable[DeviceSnapshot],
        now: datetime,
    ) -> SensorReadiness:
        candidates = tuple(devices)
        if not candidates:
            return _unavailable(
                role,
                now,
                reason_code="SENSOR.NOT_CONFIGURED",
                reason_ru=f"{_DISPLAY_NAME[role]} не настроен или не подключён.",
            )
        ranked = sorted(
            candidates,
            key=lambda device: (
                -_level_rank(self._device_level(device, now)),
                device.device_id,
            ),
        )
        best = ranked[0]
        level = self._device_level(best, now)
        last_data = _latest_data_at(candidates)
        reason_code = best.reason_code or f"SENSOR.{level.value.upper()}"
        if level is SensorReadinessLevel.READY:
            reason = f"{best.display_name}: готов и доступен."
            impact = _ready_impact(role)
        elif level is SensorReadinessLevel.LIMITED:
            reason = best.reason_ru or (
                f"{best.display_name}: работает с ограничениями "
                f"({best.state.value})."
            )
            impact = _limited_impact(role)
        else:
            reason = best.reason_ru or f"{best.display_name}: недоступен."
            impact = _UNAVAILABLE_IMPACT[role]
        return SensorReadiness(
            role=role,
            display_name=_DISPLAY_NAME[role],
            level=level,
            reason_code=reason_code,
            reason_ru=reason,
            impact_ru=impact,
            checked_at=now,
            sensor_ids=tuple(sorted(device.device_id for device in candidates)),
            last_data_at=last_data,
        )

    def _device_level(
        self,
        device: DeviceSnapshot,
        now: datetime,
    ) -> SensorReadinessLevel:
        unavailable = {
            DeviceState.ABSENT,
            DeviceState.FAILED,
            DeviceState.QUARANTINED,
            DeviceState.DISABLED,
        }
        limited = {
            DeviceState.DISCOVERED,
            DeviceState.PROBING,
            DeviceState.STARTING,
            DeviceState.STOPPING,
            DeviceState.DEGRADED,
            DeviceState.RECONNECTING,
        }
        if device.state in unavailable or device.health is HealthLevel.ERROR:
            return SensorReadinessLevel.UNAVAILABLE
        if device.state in limited or device.health in {
            HealthLevel.DEGRADED,
            HealthLevel.UNKNOWN,
        }:
            return SensorReadinessLevel.LIMITED
        if device.last_data_at is not None:
            _require_aware(device.last_data_at, "device.last_data_at")
            age = (now - device.last_data_at).total_seconds()
            if age < 0.0 or age > self._stale_after_seconds:
                return SensorReadinessLevel.LIMITED
        if device.state in {DeviceState.READY, DeviceState.STREAMING}:
            return SensorReadinessLevel.READY
        return SensorReadinessLevel.LIMITED

    @staticmethod
    def _direction_override(
        base: SensorReadiness,
        snapshot: SystemSnapshot,
        now: datetime,
    ) -> SensorReadiness:
        direction = snapshot.direction
        if not isinstance(direction, DirectionSnapshot):
            return base
        source_id = direction.current.source_id or "direction-finder"
        evidence = direction.current.evidence
        validated_external = (
            direction.available
            and direction.current.source is DirectionSource.EXTERNAL
            and evidence is not None
            and evidence.calibration_valid
        )
        if validated_external:
            return SensorReadiness(
                role=SensorRole.KRAKEN_SDR,
                display_name=_DISPLAY_NAME[SensorRole.KRAKEN_SDR],
                level=SensorReadinessLevel.READY,
                reason_code="SENSOR.DIRECTION_CURRENT",
                reason_ru="Внешний пеленгатор передаёт свежий валидированный азимут.",
                impact_ru="Сектор цели может отображаться; дальность не измеряется.",
                checked_at=now,
                sensor_ids=(source_id,),
                last_data_at=direction.current.captured_at,
            )
        if direction.available and direction.current.source in {
            DirectionSource.MANUAL,
            DirectionSource.SIMULATED,
        }:
            return SensorReadiness(
                role=SensorRole.KRAKEN_SDR,
                display_name=_DISPLAY_NAME[SensorRole.KRAKEN_SDR],
                level=(
                    SensorReadinessLevel.LIMITED
                    if base.available
                    else SensorReadinessLevel.UNAVAILABLE
                ),
                reason_code=direction.current.reason_code,
                reason_ru=(
                    "Ручной или демо-азимут не является измерением "
                    "внешнего пеленгатора."
                ),
                impact_ru=_UNAVAILABLE_IMPACT[SensorRole.KRAKEN_SDR],
                checked_at=now,
                sensor_ids=base.sensor_ids,
                last_data_at=None,
            )
        if direction.stale or base.available:
            return SensorReadiness(
                role=SensorRole.KRAKEN_SDR,
                display_name=_DISPLAY_NAME[SensorRole.KRAKEN_SDR],
                level=SensorReadinessLevel.LIMITED,
                reason_code=direction.current.reason_code or "SENSOR.DIRECTION_NO_FIX",
                reason_ru=(
                    direction.current.message_ru
                    or "Пеленгатор доступен, но свежего валидного азимута нет."
                ),
                impact_ru="Направление скрыто до получения свежего валидного пеленга.",
                checked_at=now,
                sensor_ids=base.sensor_ids or (source_id,),
                last_data_at=direction.last_valid_at,
            )
        return base

    @staticmethod
    def _acoustic_override(
        base: SensorReadiness,
        snapshot: SystemSnapshot,
        now: datetime,
    ) -> SensorReadiness:
        assessment = snapshot.acoustic
        if assessment is None:
            return base
        limited = (
            assessment.lifecycle is AcousticLifecycle.DATA_HOLD
            or assessment.data_quality is AcousticDataQuality.LOW
        )
        return SensorReadiness(
            role=SensorRole.ACOUSTIC,
            display_name=_DISPLAY_NAME[SensorRole.ACOUSTIC],
            level=(
                SensorReadinessLevel.LIMITED
                if limited
                else SensorReadinessLevel.READY
            ),
            reason_code=(
                "SENSOR.ACOUSTIC_DATA_HOLD"
                if limited
                else "SENSOR.ACOUSTIC_CURRENT"
            ),
            reason_ru=(
                "Акустические данные временно недостаточно надёжны."
                if limited
                else "Акустический источник передаёт текущие данные."
            ),
            impact_ru=(
                "Звуковое подтверждение не повышает стадию до восстановления качества."
                if limited
                else "Доступно независимое звуковое подтверждение."
            ),
            checked_at=now,
            sensor_ids=(assessment.provenance.source_id,),
            last_data_at=assessment.observed_at,
        )

    @staticmethod
    def _adsb_override(
        base: SensorReadiness,
        snapshot: SystemSnapshot,
        now: datetime,
    ) -> SensorReadiness:
        airspace = snapshot.airspace
        if airspace is None:
            return base
        summary = airspace.summary
        if summary.state is AirspaceFeedState.CURRENT:
            return SensorReadiness(
                role=SensorRole.ADSB,
                display_name=_DISPLAY_NAME[SensorRole.ADSB],
                level=SensorReadinessLevel.READY,
                reason_code="SENSOR.ADSB_CURRENT",
                reason_ru="Локальный гражданский ADS-B поток актуален.",
                impact_ru=(
                    "Доступен только гражданский кооперативный контекст; это не IFF."
                ),
                checked_at=now,
                sensor_ids=("local-civil-adsb",),
                last_data_at=summary.source_generated_at or summary.evaluated_at,
            )
        return SensorReadiness(
            role=SensorRole.ADSB,
            display_name=_DISPLAY_NAME[SensorRole.ADSB],
            level=(
                SensorReadinessLevel.LIMITED
                if summary.state is AirspaceFeedState.STALE
                else SensorReadinessLevel.UNAVAILABLE
            ),
            reason_code=f"SENSOR.ADSB_{summary.state.value.upper()}",
            reason_ru="ADS-B поток устарел или не содержит валидных текущих данных.",
            impact_ru=_UNAVAILABLE_IMPACT[SensorRole.ADSB],
            checked_at=now,
            sensor_ids=("local-civil-adsb",),
            last_data_at=summary.source_generated_at,
        )

    @staticmethod
    def _fusion_readiness(
        snapshot: SystemSnapshot,
        items: dict[SensorRole, SensorReadiness],
        now: datetime,
    ) -> SensorReadiness:
        if snapshot.fusion_decision is None:
            return _unavailable(
                SensorRole.FUSION,
                now,
                reason_code="SENSOR.FUSION_NO_ENGINE_STATE",
                reason_ru="Состояние fusion-движка недоступно.",
            )
        rf_available = any(
            items[role].available for role in (SensorRole.TINYSA, SensorRole.RTL_SDR)
        ) or any(
            _is_other_rf_device(device) and _device_not_failed(device)
            for device in snapshot.devices
        )
        acoustic_available = items[SensorRole.ACOUSTIC].available
        if rf_available and acoustic_available:
            return SensorReadiness(
                role=SensorRole.FUSION,
                display_name=_DISPLAY_NAME[SensorRole.FUSION],
                level=SensorReadinessLevel.READY,
                reason_code="SENSOR.FUSION_MULTI_MODAL_READY",
                reason_ru="Fusion-движок и две независимые модальности доступны.",
                impact_ru="Доступно межсенсорное подтверждение активности.",
                checked_at=now,
                sensor_ids=("sensor-fusion",),
            )
        if rf_available or acoustic_available:
            return SensorReadiness(
                role=SensorRole.FUSION,
                display_name=_DISPLAY_NAME[SensorRole.FUSION],
                level=SensorReadinessLevel.LIMITED,
                reason_code="SENSOR.FUSION_SINGLE_MODALITY",
                reason_ru="Fusion-движок работает, но доступна только одна модальность.",
                impact_ru="Межсенсорное подтверждение не формируется.",
                checked_at=now,
                sensor_ids=("sensor-fusion",),
            )
        return _unavailable(
            SensorRole.FUSION,
            now,
            reason_code="SENSOR.FUSION_NO_INPUTS",
            reason_ru="Fusion-движок не получает доступных RF или акустических данных.",
        )


def _roles_for_device(device: DeviceSnapshot) -> tuple[SensorRole, ...]:
    kind = device.kind.casefold()
    roles: list[SensorRole] = []
    if "tinysa" in kind or Capability.TRIGGER_SOURCE in device.capabilities:
        roles.append(SensorRole.TINYSA)
    if any(marker in kind for marker in ("rtlsdr", "rtl-sdr", "rtl_sdr")):
        roles.append(SensorRole.RTL_SDR)
    if "kraken" in kind or Capability.DF_OBSERVATION in device.capabilities:
        roles.append(SensorRole.KRAKEN_SDR)
    if "acoustic" in kind or "microphone" in kind:
        roles.append(SensorRole.ACOUSTIC)
    if "adsb" in kind or "dump1090" in kind:
        roles.append(SensorRole.ADSB)
    if "passive" in kind and "radar" in kind:
        roles.append(SensorRole.PASSIVE_RADAR)
    return tuple(dict.fromkeys(roles))


def _is_other_rf_device(device: DeviceSnapshot) -> bool:
    return bool(
        device.capabilities
        & {
            Capability.SPECTRUM_SWEEP,
            Capability.IQ_RX,
            Capability.COHERENT_IQ_RX,
        }
    )


def _device_not_failed(device: DeviceSnapshot) -> bool:
    return device.state not in {
        DeviceState.ABSENT,
        DeviceState.FAILED,
        DeviceState.QUARANTINED,
        DeviceState.DISABLED,
    }


def _latest_data_at(devices: Iterable[DeviceSnapshot]) -> datetime | None:
    values = tuple(
        device.last_data_at
        for device in devices
        if device.last_data_at is not None
    )
    return max(values) if values else None


def _level_rank(level: SensorReadinessLevel) -> int:
    return {
        SensorReadinessLevel.UNAVAILABLE: 0,
        SensorReadinessLevel.LIMITED: 1,
        SensorReadinessLevel.READY: 2,
    }[level]


def _unavailable(
    role: SensorRole,
    now: datetime,
    *,
    reason_code: str,
    reason_ru: str,
) -> SensorReadiness:
    return SensorReadiness(
        role=role,
        display_name=_DISPLAY_NAME[role],
        level=SensorReadinessLevel.UNAVAILABLE,
        reason_code=reason_code,
        reason_ru=reason_ru,
        impact_ru=_UNAVAILABLE_IMPACT[role],
        checked_at=now,
    )


def _ready_impact(role: SensorRole) -> str:
    return {
        SensorRole.TINYSA: "Быстрый RF-триггер участвует в наблюдении.",
        SensorRole.RTL_SDR: "Спектральное RF-наблюдение доступно.",
        SensorRole.KRAKEN_SDR: "Сектор может определяться по валидному пеленгу.",
        SensorRole.ACOUSTIC: "Звуковое подтверждение доступно.",
        SensorRole.ADSB: "Гражданский кооперативный контекст доступен.",
        SensorRole.PASSIVE_RADAR: "Пассивное радиолокационное наблюдение доступно.",
        SensorRole.FUSION: "Межсенсорное подтверждение доступно.",
    }[role]


def _limited_impact(role: SensorRole) -> str:
    return {
        SensorRole.TINYSA: "RF-триггер может пропускать или задерживать события.",
        SensorRole.RTL_SDR: "Спектральные выводы ограничены качеством потока.",
        SensorRole.KRAKEN_SDR: "Направление скрыто до свежего валидного пеленга.",
        SensorRole.ACOUSTIC: "Звуковое подтверждение временно не учитывается.",
        SensorRole.ADSB: "Гражданский контекст может быть устаревшим.",
        SensorRole.PASSIVE_RADAR: "Радиолокационное подтверждение ограничено.",
        SensorRole.FUSION: "Доступно только частичное межсенсорное сопоставление.",
    }[role]


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["SensorReadinessInterpreter"]
