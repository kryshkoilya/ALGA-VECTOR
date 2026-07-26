from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StorageConfig(StrictModel):
    data_dir: Path = Path("runtime-data")
    minimum_free_gib: float = Field(default=5.0, ge=0.5, le=10_000)
    retention_days: int = Field(default=30, ge=1, le=3650)

    @field_validator("data_dir", mode="before")
    @classmethod
    def parse_data_dir(cls, value: object) -> object:
        # YAML has no native Path type. Convert the one explicitly path-valued field
        # while keeping strict validation for every other configuration value.
        return Path(value) if isinstance(value, str) else value

    @field_validator("data_dir")
    @classmethod
    def reject_drive_relative_path(cls, value: Path) -> Path:
        if value.drive and not value.is_absolute():
            raise ValueError("drive-qualified data_dir must be absolute")
        return value


class AdapterConfig(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["tinysa", "rtlsdr", "hackrf"]
    enabled: bool = True
    connection: str = Field(min_length=1, max_length=512)
    rtlsdr_profile: Literal[
        "auto",
        "generic",
        "blog_v4",
        "blog_v3_direct_q",
    ] = "auto"
    tinysa_model: Literal[
        "auto",
        "basic",
        "ultra_zs405",
        "ultra_plus_zs406",
        "ultra_plus_zs407",
    ] = "auto"
    tinysa_ultra_mode: bool = False

    @model_validator(mode="after")
    def connection_is_explicit_and_supported(self) -> Self:
        connection = self.connection.strip()
        if (
            self.kind == "tinysa"
            and connection.upper() != "SIM:TINYSA"
            and re.fullmatch(
                r"(?i)COM(?:[1-9]|[1-9]\d|[12]\d\d)",
                connection,
            )
            is None
        ):
            raise ValueError("tinySA connection must be one explicit COM port")
        if (
            self.kind == "rtlsdr"
            and connection.upper() != "SIM:RTLSDR"
            and re.fullmatch(r"(?i)RTLSDR:\d{1,3}", connection) is None
        ):
            raise ValueError("RTL-SDR connection must use RTLSDR:<index>")
        if (
            self.kind == "hackrf"
            and re.fullmatch(r"(?i)HACKRF:[0-9a-f]{8,64}", connection) is None
        ):
            raise ValueError(
                "HackRF connection must use HACKRF:<hexadecimal serial>"
            )
        if self.kind != "rtlsdr" and self.rtlsdr_profile != "auto":
            raise ValueError("rtlsdr_profile override is valid only for RTL-SDR")
        if self.kind != "tinysa" and (
            self.tinysa_model != "auto" or self.tinysa_ultra_mode
        ):
            raise ValueError(
                "tinysa_model and tinysa_ultra_mode are valid only for tinySA"
            )
        if self.tinysa_model == "basic" and self.tinysa_ultra_mode:
            raise ValueError("tinySA Basic has no Ultra mode")
        return self


class DevicesConfig(StrictModel):
    enable_real_adapters: bool = False
    adapters: list[AdapterConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def adapter_ids_are_unique(self) -> Self:
        identifiers = [adapter.id for adapter in self.adapters]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("adapter ids must be unique")
        return self


class SpectrumConfig(StrictModel):
    center_frequency_hz: int = Field(default=433_920_000, ge=1)
    span_hz: int = Field(default=2_000_000, ge=1_000)
    sample_rate_hz: int = Field(default=2_400_000, ge=8_000)
    threshold_level: float = Field(default=-72.4, ge=-200, le=30)


class AcousticConfig(StrictModel):
    """Safe acoustic-core settings; live capture remains capability-gated."""

    enabled: bool = False
    source: Literal["disabled", "external_pcm"] = "disabled"
    source_id: str = Field(default="microphone-01", min_length=1, max_length=128)
    sample_rate_hz: int = Field(default=48_000, ge=8_000, le=192_000)
    window_seconds: float = Field(default=1.0, ge=0.1, le=10.0)

    @model_validator(mode="after")
    def enabled_source_is_explicit(self) -> Self:
        if self.enabled and self.source == "disabled":
            raise ValueError(
                "enabled acoustic monitoring requires an explicit PCM source"
            )
        return self


class AirspaceConfig(StrictModel):
    """Local cooperative-civil-aircraft context from dump1090 JSON."""

    enabled: bool = False
    aircraft_json_path: Path | None = None
    stale_after_seconds: float = Field(default=5.0, ge=1.0, le=300.0)

    @field_validator("aircraft_json_path", mode="before")
    @classmethod
    def parse_aircraft_json_path(cls, value: object) -> object:
        if value in {None, ""}:
            return None
        return Path(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def enabled_feed_has_path(self) -> Self:
        if self.enabled and self.aircraft_json_path is None:
            raise ValueError(
                "enabled civilian ADS-B context requires aircraft_json_path"
            )
        return self


class FusionConfig(StrictModel):
    """Temporal multi-sensor correlation policy."""

    window_seconds: float = Field(default=8.0, ge=1.0, le=120.0)
    min_consecutive_observations: int = Field(default=3, ge=3, le=20)
    hold_seconds: float = Field(default=4.0, ge=0.5, le=60.0)


class MapConfig(StrictModel):
    package_path: Path | None = None
    default_zoom: int = Field(default=12, ge=0, le=22)
    tile_cache_mib: int = Field(default=128, ge=16, le=2048)
    # Retained only to read v0.4 profiles. The v0.5 operator flow has no map
    # and always disables the legacy online tile service at composition time.
    network_enabled: bool = False
    online_cache_mib: int = Field(default=256, ge=16, le=2048)

    @field_validator("package_path", mode="before")
    @classmethod
    def parse_package_path(cls, value: object) -> object:
        if value in {None, ""}:
            return None
        return Path(value) if isinstance(value, str) else value


class LocationPolicyConfig(StrictModel):
    source: Literal["unset", "manual", "gps"] = "unset"
    gps_port: str = Field(default="", max_length=128)
    gps_baud: int = Field(default=9_600, ge=1_200, le=921_600)
    maximum_fix_age_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    maximum_hdop: float = Field(default=4.0, ge=0.5, le=99.0)
    verification_radius_m: float = Field(default=50.0, ge=1.0, le=10_000.0)
    maximum_jump_distance_m: float = Field(default=250.0, ge=10.0, le=100_000.0)
    maximum_jump_speed_m_s: float = Field(default=60.0, ge=1.0, le=10_000.0)

    @field_validator("gps_port")
    @classmethod
    def normalize_gps_port(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and re.fullmatch(
            r"(?i)COM(?:[1-9]|[1-9]\d|[12]\d\d)",
            cleaned,
        ) is None:
            raise ValueError("gps_port must be one explicit Windows COM port")
        return cleaned.upper()

    @model_validator(mode="after")
    def gps_source_requires_explicit_port(self) -> Self:
        if self.source == "gps" and not self.gps_port:
            raise ValueError("gps source requires one explicit Windows COM port")
        return self


class UiConfig(StrictModel):
    experience_level: Literal["guided", "expert"] = "guided"
    hide_exact_coordinates: bool = True


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_files: int = Field(default=15, ge=1, le=100)
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1_048_576)


class AppConfig(StrictModel):
    schema_version: Literal[5] = 5
    locale: Literal["ru"] = "ru"
    profile_name: str = Field(default="Полевой профиль 01", min_length=1, max_length=128)
    mode: Literal["live", "demo", "safe"] = "live"
    first_run_complete: bool = False
    storage: StorageConfig = Field(default_factory=StorageConfig)
    devices: DevicesConfig = Field(default_factory=DevicesConfig)
    spectrum: SpectrumConfig = Field(default_factory=SpectrumConfig)
    acoustic: AcousticConfig = Field(default_factory=AcousticConfig)
    airspace: AirspaceConfig = Field(default_factory=AirspaceConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    map: MapConfig = Field(default_factory=MapConfig)
    location: LocationPolicyConfig = Field(default_factory=LocationPolicyConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("profile_name")
    @classmethod
    def profile_name_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("profile_name must not be blank")
        return cleaned

    @model_validator(mode="after")
    def synthetic_sources_are_demo_only(self) -> Self:
        if self.mode != "demo":
            simulated = [
                adapter.id
                for adapter in self.devices.adapters
                if adapter.connection.upper().startswith("SIM:")
            ]
            if simulated:
                raise ValueError(
                    "SIM sources are accepted only when mode is explicitly demo"
                )
        return self

    @model_validator(mode="after")
    def enabled_rtlsdr_span_fits_sample_rate(self) -> Self:
        enabled_rtlsdr = any(
            adapter.enabled and adapter.kind == "rtlsdr"
            for adapter in self.devices.adapters
        )
        if not enabled_rtlsdr:
            return self
        if self.spectrum.span_hz > self.spectrum.sample_rate_hz:
            raise ValueError(
                "for an enabled RTL-SDR, spectrum.span_hz must not exceed "
                "spectrum.sample_rate_hz"
            )
        if not 500_000 <= self.spectrum.center_frequency_hz <= 1_766_000_000:
            raise ValueError(
                "for an enabled RTL-SDR, spectrum.center_frequency_hz must be "
                "within the widest supported hardware range 500000..1766000000"
            )
        window_low_hz = (
            self.spectrum.center_frequency_hz - self.spectrum.span_hz // 2
        )
        window_high_hz = (
            self.spectrum.center_frequency_hz + self.spectrum.span_hz // 2
        )
        if window_low_hz < 500_000 or window_high_hz > 1_766_000_000:
            raise ValueError(
                "for an enabled RTL-SDR, the entire requested spectrum window "
                "must fit within 500000..1766000000"
            )
        sample_rate = self.spectrum.sample_rate_hz
        valid_sample_rate = (
            225_001 <= sample_rate <= 300_000
            or 900_001 <= sample_rate <= 3_200_000
        )
        if not valid_sample_rate:
            raise ValueError(
                "for an enabled RTL-SDR, spectrum.sample_rate_hz must be within "
                "225001..300000 or 900001..3200000"
            )
        return self

    @model_validator(mode="after")
    def enabled_hackrf_settings_fit_hardware(self) -> Self:
        enabled_hackrf = any(
            adapter.enabled and adapter.kind == "hackrf"
            for adapter in self.devices.adapters
        )
        if not enabled_hackrf:
            return self
        if not 2_000_000 <= self.spectrum.sample_rate_hz <= 20_000_000:
            raise ValueError(
                "for an enabled HackRF, spectrum.sample_rate_hz must be within "
                "2000000..20000000"
            )
        if self.spectrum.span_hz > self.spectrum.sample_rate_hz:
            raise ValueError(
                "for an enabled HackRF, spectrum.span_hz must not exceed "
                "spectrum.sample_rate_hz"
            )
        window_low_hz = (
            self.spectrum.center_frequency_hz - self.spectrum.span_hz // 2
        )
        window_high_hz = (
            self.spectrum.center_frequency_hz + self.spectrum.span_hz // 2
        )
        if window_low_hz < 1_000_000 or window_high_hz > 6_000_000_000:
            raise ValueError(
                "for an enabled HackRF, the entire requested spectrum window "
                "must fit within 1000000..6000000000"
            )
        return self

    @model_validator(mode="after")
    def enabled_tinysa_window_fits_declared_model(self) -> Self:
        normal_maxima = {
            "basic": 350_000_000,
            "ultra_zs405": 800_000_000,
            "ultra_plus_zs406": 900_000_000,
            "ultra_plus_zs407": 900_000_000,
        }
        ultra_maxima = {
            "ultra_zs405": 5_300_000_000,
            "ultra_plus_zs406": 5_400_000_000,
            "ultra_plus_zs407": 7_300_000_000,
        }
        window_low_hz = (
            self.spectrum.center_frequency_hz - self.spectrum.span_hz // 2
        )
        window_high_hz = (
            self.spectrum.center_frequency_hz + self.spectrum.span_hz // 2
        )
        for adapter in self.devices.adapters:
            if (
                not adapter.enabled
                or adapter.kind != "tinysa"
                or adapter.tinysa_model == "auto"
            ):
                continue
            maximum_hz = normal_maxima[adapter.tinysa_model]
            if adapter.tinysa_ultra_mode:
                maximum_hz = ultra_maxima[adapter.tinysa_model]
            if window_low_hz < 100_000 or window_high_hz > maximum_hz:
                raise ValueError(
                    "for an enabled tinySA with an explicit model, the entire "
                    "requested spectrum window must fit the declared receive range"
                )
        return self
