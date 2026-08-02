from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from alga_vector.config.models import (
    AcousticConfig,
    AdapterConfig,
    AirspaceConfig,
    AppConfig,
    DevicesConfig,
    SpectrumConfig,
    TargetTrackingConfig,
)
from alga_vector.config.service import ConfigService


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_default_config_round_trip(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    config = AppConfig()
    _write(default, config.model_dump(mode="json"))

    service = ConfigService(default, user)
    loaded = service.load()
    assert loaded.config.profile_name == "Полевой профиль 01"
    assert loaded.used_fallback is False

    saved = loaded.config.model_copy(update={"first_run_complete": True})
    service.save(saved)
    assert service.load().config.first_run_complete is True


def test_default_spectrum_span_is_safe_for_rtlsdr() -> None:
    assert SpectrumConfig().span_hz == 2_000_000
    assert SpectrumConfig().detection_sensitivity == "high"


def test_detection_sensitivity_is_strict() -> None:
    assert SpectrumConfig(detection_sensitivity="balanced").detection_sensitivity == (
        "balanced"
    )
    with pytest.raises(ValidationError, match="detection_sensitivity"):
        SpectrumConfig(detection_sensitivity="maximum")  # type: ignore[arg-type]


def test_enabled_rtlsdr_rejects_span_above_sample_rate() -> None:
    devices = DevicesConfig(
        adapters=[
            AdapterConfig(
                id="rtl-01",
                kind="rtlsdr",
                enabled=True,
                connection="RTLSDR:0",
            )
        ]
    )

    with pytest.raises(ValidationError, match="spectrum\\.span_hz"):
        AppConfig(
            devices=devices,
            spectrum=SpectrumConfig(
                span_hz=2_500_000,
                sample_rate_hz=2_400_000,
            ),
        )


def test_disabled_rtlsdr_does_not_limit_spectrum_span() -> None:
    config = AppConfig(
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id="rtl-01",
                    kind="rtlsdr",
                    enabled=False,
                    connection="RTLSDR:0",
                )
            ]
        ),
        spectrum=SpectrumConfig(
            span_hz=2_500_000,
            sample_rate_hz=2_400_000,
        ),
    )

    assert config.spectrum.span_hz == 2_500_000


def test_tinysa_does_not_inherit_rtlsdr_sample_rate_limit() -> None:
    config = AppConfig(
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id="tiny-01",
                    kind="tinysa",
                    enabled=True,
                    connection="COM7",
                )
            ]
        ),
        spectrum=SpectrumConfig(
            span_hz=5_000_000,
            sample_rate_hz=2_400_000,
        ),
    )

    assert config.spectrum.span_hz == 5_000_000


def test_rtlsdr_profile_override_is_strict_and_persistable() -> None:
    adapter = AdapterConfig(
        id="rtl-v4",
        kind="rtlsdr",
        enabled=True,
        connection="RTLSDR:0",
        rtlsdr_profile="blog_v4",
    )

    assert adapter.model_dump()["rtlsdr_profile"] == "blog_v4"
    with pytest.raises(ValidationError, match="rtlsdr_profile"):
        AdapterConfig(
            id="tiny-01",
            kind="tinysa",
            enabled=True,
            connection="COM7",
            rtlsdr_profile="blog_v4",
        )


@pytest.mark.parametrize("sample_rate_hz", [8_000, 500_000, 3_200_001])
def test_enabled_rtlsdr_rejects_unsupported_sample_rate(
    sample_rate_hz: int,
) -> None:
    with pytest.raises(ValidationError, match="spectrum\\.sample_rate_hz"):
        AppConfig(
            devices=DevicesConfig(
                adapters=[
                    AdapterConfig(
                        id="rtl-01",
                        kind="rtlsdr",
                        connection="RTLSDR:0",
                    )
                ]
            ),
            spectrum=SpectrumConfig(
                span_hz=min(sample_rate_hz, 100_000),
                sample_rate_hz=sample_rate_hz,
            ),
        )


def test_enabled_rtlsdr_rejects_window_past_widest_hardware_edge() -> None:
    with pytest.raises(ValidationError, match="entire requested spectrum window"):
        AppConfig(
            devices=DevicesConfig(
                adapters=[
                    AdapterConfig(
                        id="rtl-01",
                        kind="rtlsdr",
                        connection="RTLSDR:0",
                        rtlsdr_profile="blog_v4",
                    )
                ]
            ),
            spectrum=SpectrumConfig(
                center_frequency_hz=1_766_000_000,
                span_hz=2_000_000,
                sample_rate_hz=2_400_000,
            ),
        )


def test_invalid_user_config_falls_back(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    _write(default, AppConfig().model_dump(mode="json"))
    user.write_text("schema_version: 99\nunknown: true\n", encoding="utf-8")

    loaded = ConfigService(default, user).load()
    assert loaded.used_fallback is True
    assert loaded.warning is not None
    assert loaded.warning.code == "CONFIG.FALLBACK_USED"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"schema_version": 1, "unknown": True})


def test_duplicate_adapter_ids_trigger_fallback(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    _write(default, AppConfig().model_dump(mode="json"))
    payload = AppConfig().model_dump(mode="json")
    payload["devices"]["adapters"] = [
        {
            "id": "duplicate",
            "kind": "tinysa",
            "enabled": True,
            "connection": "SIM:TINYSA",
        },
        {
            "id": "duplicate",
            "kind": "rtlsdr",
            "enabled": True,
            "connection": "SIM:RTLSDR",
        },
    ]
    _write(user, payload)

    loaded = ConfigService(default, user).load()

    assert loaded.used_fallback is True
    assert loaded.warning is not None


def test_save_never_copies_corrupt_user_bytes_to_last_good(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    service = ConfigService(default, user)
    _write(default, AppConfig().model_dump(mode="json"))
    service.save(AppConfig(profile_name="Известно исправный"))
    user.write_text("schema_version: [broken", encoding="utf-8")

    recovered = service.load().config
    assert recovered.profile_name == "Известно исправный"

    service.save(recovered.model_copy(update={"profile_name": "Сохранённый"}))
    user.write_text("not: [yaml", encoding="utf-8")

    assert service.load().config.profile_name == "Сохранённый"


def test_schema_v1_is_migrated_to_location_and_map_defaults(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 1
    payload.pop("map")
    payload.pop("location")
    payload.pop("ui")
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    loaded = ConfigService(default, user)
    config = loaded.load().config

    assert config.schema_version == 7
    assert config.location.source == "unset"
    assert config.map.package_path is None
    assert config.map.network_enabled is False
    assert config.map.online_cache_mib == 256
    assert config.ui.experience_level == "guided"


def test_schema_v1_clamps_legacy_rtlsdr_span_instead_of_fallback(
    tmp_path: Path,
) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 1
    payload["mode"] = "demo"
    payload["first_run_complete"] = True
    payload["spectrum"]["span_hz"] = 5_000_000
    payload["spectrum"]["sample_rate_hz"] = 2_400_000
    payload["devices"]["adapters"] = [
        {
            "id": "legacy-demo-tiny",
            "kind": "tinysa",
            "enabled": True,
            "connection": "SIM:TINYSA",
        },
        {
            "id": "legacy-demo-rtl",
            "kind": "rtlsdr",
            "enabled": True,
            "connection": "SIM:RTLSDR",
        },
        {
            "id": "legacy-unimplemented",
            "kind": "krakensdr",
            "enabled": True,
            "connection": "192.0.2.10",
        },
    ]
    payload.pop("map")
    payload.pop("location")
    payload.pop("ui")
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    loaded = ConfigService(default, user).load()

    assert loaded.used_fallback is False
    assert loaded.config.schema_version == 7
    assert loaded.config.spectrum.span_hz == 2_400_000
    assert loaded.config.first_run_complete is True
    assert [adapter.id for adapter in loaded.config.devices.adapters] == [
        "legacy-demo-tiny",
        "legacy-demo-rtl",
    ]


def test_schema_v2_removes_unimplemented_and_live_simulated_adapters(
    tmp_path: Path,
) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 2
    payload["spectrum"]["threshold_dbm"] = payload["spectrum"].pop(
        "threshold_level"
    )
    payload["devices"]["adapters"] = [
        {
            "id": "legacy-kraken",
            "kind": "krakensdr",
            "enabled": True,
            "connection": "192.0.2.10",
        },
        {
            "id": "legacy-demo",
            "kind": "tinysa",
            "enabled": True,
            "connection": "SIM:TINYSA",
        },
        {
            "id": "supported",
            "kind": "rtlsdr",
            "enabled": True,
            "connection": "RTLSDR:0",
        },
    ]
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    config = ConfigService(default, user).load().config

    assert config.schema_version == 7
    assert [adapter.id for adapter in config.devices.adapters] == ["supported"]
    assert config.spectrum.threshold_level == -72.4


def test_schema_v3_keeps_legacy_map_network_disabled(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 3
    payload["map"].pop("network_enabled")
    payload["map"].pop("online_cache_mib")
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    config = ConfigService(default, user).load().config

    assert config.schema_version == 7
    assert config.map.network_enabled is False
    assert config.map.online_cache_mib == 256


def test_multisensor_defaults_are_fail_closed() -> None:
    config = AppConfig()

    assert config.schema_version == 7
    assert config.acoustic.enabled is False
    assert config.acoustic.source == "disabled"
    assert config.airspace.enabled is False
    assert config.airspace.aircraft_json_path is None
    assert config.fusion.min_consecutive_observations >= 3
    assert config.target_tracking.maximum_active_targets == 64


def test_schema_v5_adds_fail_closed_target_tracking_defaults(
    tmp_path: Path,
) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 5
    payload.pop("target_tracking")
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    config = ConfigService(default, user).load().config

    assert config.schema_version == 7
    assert config.target_tracking.maximum_active_targets == 64
    assert config.target_tracking.stale_after_seconds == 30.0


def test_schema_v6_adds_explicit_field_sensitivity(tmp_path: Path) -> None:
    default = tmp_path / "default.yaml"
    user = tmp_path / "user.yaml"
    payload = AppConfig().model_dump(mode="json")
    payload["schema_version"] = 6
    payload["spectrum"].pop("detection_sensitivity")
    _write(user, payload)
    _write(default, AppConfig().model_dump(mode="json"))

    config = ConfigService(default, user).load().config

    assert config.schema_version == 7
    assert config.spectrum.detection_sensitivity == "high"


def test_target_tracking_lifecycle_windows_are_ordered() -> None:
    with pytest.raises(ValidationError, match="stale_after_seconds"):
        TargetTrackingConfig(
            correlation_window_seconds=30.0,
            stale_after_seconds=20.0,
        )

    with pytest.raises(ValidationError, match="retire_after_seconds"):
        TargetTrackingConfig(
            stale_after_seconds=30.0,
            retire_after_seconds=30.0,
        )

    with pytest.raises(ValidationError, match="stale_after_seconds"):
        TargetTrackingConfig(
            correlation_window_seconds=30.0,
            stale_after_seconds=30.0,
        )


def test_enabled_acoustic_requires_explicit_external_pcm_source() -> None:
    with pytest.raises(ValidationError, match="explicit PCM source"):
        AcousticConfig(enabled=True)


def test_enabled_civil_airspace_requires_local_json_path() -> None:
    with pytest.raises(ValidationError, match="aircraft_json_path"):
        AirspaceConfig(enabled=True)
