"""Application composition that is independent of Qt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_config_path, user_data_path

from alga_vector.application import ApplicationRuntime
from alga_vector.config import (
    AdapterConfig,
    AppConfig,
    ConfigLoadResult,
    ConfigService,
    DevicesConfig,
)
from alga_vector.domain.errors import AppError
from alga_vector.resources import default_config_path

RuntimeMode = Literal["live", "demo", "safe"]


@dataclass(slots=True)
class BootstrapContext:
    """Objects owned by the executable composition root."""

    runtime: ApplicationRuntime
    config: AppConfig
    config_service: ConfigService
    config_source: Path
    warning: AppError | None = None


def application_directories() -> tuple[Path, Path]:
    """Return stable per-user configuration and data directories."""

    config_dir = user_config_path(
        appname="ALGA VECTOR",
        appauthor="Буйвол и Задира",
        roaming=False,
    )
    data_dir = user_data_path(
        appname="ALGA VECTOR",
        appauthor="Буйвол и Задира",
        roaming=False,
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    return config_dir, data_dir


def build_context(
    *,
    mode_override: RuntimeMode | None = None,
    data_dir_override: Path | None = None,
    network_maps_override: bool | None = None,
    debug_logging_override: bool = False,
) -> BootstrapContext:
    """Load validated configuration and construct the application runtime.

    Demo is a process-only training mode.  Omitting a mode is therefore an
    explicit live launch, regardless of a legacy ``mode: demo`` value in the
    persisted profile.
    """

    config_dir, user_data_dir = application_directories()
    process_mode: RuntimeMode = mode_override or "live"
    service = ConfigService(
        default_path=default_config_path(),
        user_path=config_dir / "settings.yaml",
    )
    loaded: ConfigLoadResult = service.load()
    config = _apply_runtime_overrides(
        loaded.config,
        user_data_dir=user_data_dir,
        mode_override=process_mode,
        data_dir_override=data_dir_override,
        network_maps_override=network_maps_override,
        debug_logging_override=debug_logging_override,
    )
    persisted_base = loaded.config

    def save_runtime_config(candidate: AppConfig) -> None:
        nonlocal persisted_base
        persisted = _persistable_config(
            candidate,
            base=persisted_base,
            # Live is the canonical persistent profile. Demo and safe are
            # temporary process modes and must not overwrite its adapters.
            mode_overridden=process_mode != "live",
            data_dir_overridden=data_dir_override is not None,
            network_maps_overridden=network_maps_override is not None,
            debug_logging_overridden=debug_logging_override,
        )
        service.save(persisted)
        persisted_base = persisted

    return BootstrapContext(
        runtime=ApplicationRuntime(
            config,
            config_saver=save_runtime_config,
            startup_warnings=(loaded.warning,) if loaded.warning is not None else (),
            mode_lock=process_mode,
        ),
        config=config,
        config_service=service,
        config_source=loaded.source,
        warning=loaded.warning,
    )


def _apply_runtime_overrides(
    config: AppConfig,
    *,
    user_data_dir: Path,
    mode_override: RuntimeMode | None,
    data_dir_override: Path | None,
    network_maps_override: bool | None = None,
    debug_logging_override: bool = False,
) -> AppConfig:
    requested_data_dir = data_dir_override or config.storage.data_dir
    if requested_data_dir.drive and not requested_data_dir.is_absolute():
        raise ValueError(
            "drive-qualified data directory must be absolute "
            f"(received: {requested_data_dir})"
        )
    resolved_data_dir = (
        requested_data_dir.resolve()
        if requested_data_dir.is_absolute()
        else (user_data_dir / requested_data_dir).resolve()
    )
    storage = config.storage.model_copy(update={"data_dir": resolved_data_dir})
    updates: dict[str, object] = {
        "storage": storage,
        # v0.5 has no operational map or GPS flow. Keep the schema fields so
        # older profiles can be read, but fail closed at the composition root.
        "map": config.map.model_copy(update={"network_enabled": False}),
        "location": config.location.model_copy(
            update={"source": "unset", "gps_port": ""}
        ),
    }
    if network_maps_override is True:
        # Kept solely for internal backwards-compatibility tests. The desktop
        # CLI exposes no switch that can request this legacy path.
        updates["map"] = config.map.model_copy(update={"network_enabled": True})
    if debug_logging_override:
        updates["logging"] = config.logging.model_copy(update={"level": "DEBUG"})
    effective_mode: RuntimeMode = mode_override or "live"
    updates["mode"] = effective_mode
    if effective_mode == "demo":
        updates["devices"] = _demo_devices()
    elif effective_mode == "safe":
        updates["devices"] = config.devices.model_copy(
            update={
                "enable_real_adapters": False,
                "adapters": [
                    adapter.model_copy(update={"enabled": False})
                    for adapter in config.devices.adapters
                    if not adapter.connection.upper().startswith("SIM:")
                ],
            }
        )
        updates["acoustic"] = config.acoustic.model_copy(
            update={"enabled": False, "source": "disabled"}
        )
        updates["airspace"] = config.airspace.model_copy(
            update={"enabled": False}
        )
    else:
        # A normal launch must never inherit synthetic devices from a legacy
        # training profile.  Retain explicitly configured physical receivers,
        # but do not arm them merely because the old profile was a demo.
        updates["devices"] = DevicesConfig(
            enable_real_adapters=(
                config.devices.enable_real_adapters if config.mode != "demo" else False
            ),
            adapters=[
                adapter
                for adapter in config.devices.adapters
                if not adapter.connection.upper().startswith("SIM:")
            ],
        )
    effective = AppConfig.model_validate(
        config.model_copy(update=updates).model_dump(mode="python")
    )
    effective.storage.data_dir.mkdir(parents=True, exist_ok=True)
    return effective


def _demo_devices() -> DevicesConfig:
    """Return the deterministic demonstration topology."""

    return DevicesConfig(
        enable_real_adapters=False,
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
        ],
    )


def _persistable_config(
    candidate: AppConfig,
    *,
    base: AppConfig,
    mode_overridden: bool,
    data_dir_overridden: bool,
    network_maps_overridden: bool = False,
    debug_logging_overridden: bool = False,
) -> AppConfig:
    """Remove process-only CLI overrides before writing user configuration."""

    updates: dict[str, object] = {}
    if mode_overridden:
        updates["mode"] = base.mode
        updates["devices"] = base.devices
    if data_dir_overridden:
        updates["storage"] = candidate.storage.model_copy(
            update={"data_dir": base.storage.data_dir}
        )
    if network_maps_overridden:
        updates["map"] = candidate.map.model_copy(
            update={"network_enabled": base.map.network_enabled}
        )
    if debug_logging_overridden:
        updates["logging"] = candidate.logging.model_copy(
            update={"level": base.logging.level}
        )
    return candidate.model_copy(update=updates)


__all__ = ["BootstrapContext", "RuntimeMode", "application_directories", "build_context"]
