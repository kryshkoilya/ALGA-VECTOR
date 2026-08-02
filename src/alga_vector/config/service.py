# ruff: noqa: RUF001

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from alga_vector.domain.enums import IncidentSeverity
from alga_vector.domain.errors import AppError

from .models import AppConfig


@dataclass(slots=True, frozen=True)
class ConfigLoadResult:
    config: AppConfig
    source: Path
    used_fallback: bool = False
    warning: AppError | None = None


class ConfigService:
    def __init__(self, default_path: Path, user_path: Path) -> None:
        self.default_path = default_path
        self.user_path = user_path
        self.last_good_path = user_path.with_suffix(".last-good.yaml")

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, Any]:
        if path.stat().st_size > 1_048_576:
            raise ValueError("configuration exceeds 1 MiB")
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise ValueError("configuration root must be a mapping")
        return data

    def _validate(self, path: Path) -> AppConfig:
        return AppConfig.model_validate(_migrate_mapping(self._read_mapping(path)))

    def load(self) -> ConfigLoadResult:
        candidates = (self.user_path, self.last_good_path, self.default_path)
        failures: list[str] = []
        for path in candidates:
            if not path.exists():
                continue
            try:
                config = self._validate(path)
                return ConfigLoadResult(
                    config=config,
                    source=path,
                    # A missing user file is the normal first-run path.  We only
                    # report fallback when an earlier candidate actually failed.
                    used_fallback=bool(failures),
                    warning=(
                        AppError(
                            code="CONFIG.FALLBACK_USED",
                            message_ru="Рабочая конфигурация повреждена. Загружена последняя исправная версия.",
                            operator_action_ru="Проверьте отмеченные параметры в настройках.",
                            severity=IncidentSeverity.WARNING,
                            technical_details={"failures": failures},
                        )
                        if failures
                        else None
                    ),
                )
            except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
                failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
        raise AppError(
            code="CONFIG.NO_VALID_CONFIG",
            message_ru="Не удалось загрузить конфигурацию.",
            operator_action_ru="Запустите безопасный режим и восстановите настройки.",
            severity=IncidentSeverity.CRITICAL,
            technical_details={"failures": failures},
        )

    def save(self, config: AppConfig) -> None:
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = yaml.safe_dump(
            config.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )
        temporaries: list[Path] = []
        try:
            user_temp = self._write_temp(self.user_path, serialized)
            temporaries.append(user_temp)
            last_good_temp = self._write_temp(self.last_good_path, serialized)
            temporaries.append(last_good_temp)
            # Refresh the recoverable copy first. The user file is the final
            # commit point, so failure before it leaves the active config intact.
            os.replace(last_good_temp, self.last_good_path)
            os.replace(user_temp, self.user_path)
        finally:
            for temporary in temporaries:
                if temporary.exists():
                    temporary.unlink()

    @staticmethod
    def _write_temp(path: Path, serialized: str) -> Path:
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path


def _migrate_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Upgrade supported configuration mappings before strict validation."""

    migrated = dict(payload)
    version = migrated.get("schema_version", 1)
    if version == 1:
        migrated["schema_version"] = 2
        migrated.setdefault("map", {})
        migrated.setdefault("location", {})
        migrated.setdefault("ui", {})
        version = 2
    if version == 2:
        # Version 3 removes advertised-but-unimplemented receiver kinds and
        # guarantees that a live/safe profile cannot inherit demo sources.
        raw_devices = migrated.get("devices")
        devices = dict(raw_devices) if isinstance(raw_devices, dict) else {}
        raw_adapters = devices.get("adapters")
        adapters = raw_adapters if isinstance(raw_adapters, list) else []
        supported = [
            item
            for item in adapters
            if isinstance(item, dict)
            and item.get("kind") in {"tinysa", "rtlsdr"}
            and (
                migrated.get("mode") == "demo"
                or not str(item.get("connection", "")).upper().startswith("SIM:")
            )
        ]
        devices["adapters"] = supported
        migrated["devices"] = devices
        raw_spectrum = migrated.get("spectrum")
        if isinstance(raw_spectrum, dict):
            spectrum = dict(raw_spectrum)
            if "threshold_level" not in spectrum and "threshold_dbm" in spectrum:
                spectrum["threshold_level"] = spectrum.pop("threshold_dbm")
            migrated["spectrum"] = spectrum
        migrated["schema_version"] = 3
        version = 3
    if version == 3:
        raw_map = migrated.get("map")
        map_config = dict(raw_map) if isinstance(raw_map, dict) else {}
        # v0.5 retains the field only for backwards-compatible profile reads;
        # an old profile must not silently re-enable the retired map network.
        map_config.setdefault("network_enabled", False)
        map_config.setdefault("online_cache_mib", 256)
        migrated["map"] = map_config
        # v4 added a cross-field invariant required by the real RTL-SDR
        # adapter. Preserve an otherwise valid legacy profile by narrowing its
        # sweep to the configured sample rate instead of discarding the whole
        # user configuration and silently returning to defaults.
        raw_devices = migrated.get("devices")
        devices = dict(raw_devices) if isinstance(raw_devices, dict) else {}
        raw_adapters = devices.get("adapters")
        adapters = raw_adapters if isinstance(raw_adapters, list) else []
        enabled_rtlsdr = any(
            isinstance(item, dict)
            and item.get("kind") == "rtlsdr"
            and bool(item.get("enabled", True))
            for item in adapters
        )
        raw_spectrum = migrated.get("spectrum")
        if enabled_rtlsdr and isinstance(raw_spectrum, dict):
            spectrum = dict(raw_spectrum)
            span_hz = spectrum.get("span_hz")
            sample_rate_hz = spectrum.get("sample_rate_hz")
            if (
                isinstance(span_hz, int)
                and not isinstance(span_hz, bool)
                and isinstance(sample_rate_hz, int)
                and not isinstance(sample_rate_hz, bool)
                and span_hz > sample_rate_hz
            ):
                spectrum["span_hz"] = sample_rate_hz
                migrated["spectrum"] = spectrum
        migrated["schema_version"] = 4
        version = 4
    if version == 4:
        # v5 introduces capability-gated acoustic, civilian ADS-B and fusion
        # policies. Defaults are deliberately disabled/fail-closed so old
        # profiles never start a new input automatically after migration.
        migrated.setdefault("acoustic", {})
        migrated.setdefault("airspace", {})
        migrated.setdefault("fusion", {})
        migrated["schema_version"] = 5
        version = 5
    if version == 5:
        # v6 adds bounded target aggregation. Defaults only group already
        # accepted normalized events and do not enable a new sensor or weaken
        # the fail-closed event policy.
        migrated.setdefault("target_tracking", {})
        migrated["schema_version"] = 6
        version = 6
    if version == 6:
        # v7 makes field sensitivity explicit. Existing profiles retain the
        # field-oriented default so an upgrade cannot silently return to the
        # former conservative threshold policy.
        raw_spectrum = migrated.get("spectrum")
        spectrum = dict(raw_spectrum) if isinstance(raw_spectrum, dict) else {}
        spectrum.setdefault("detection_sensitivity", "high")
        migrated["spectrum"] = spectrum
        migrated["schema_version"] = 7
        version = 7
    if version != 7:
        raise ValueError(f"unsupported schema_version: {version!r}")
    return migrated
