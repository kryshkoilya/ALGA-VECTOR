from __future__ import annotations

import pytest
from pydantic import ValidationError

from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    SpectrumConfig,
)
from alga_vector.devices.capabilities import (
    HACKRF_ONE_PROFILE,
    TinySaModel,
    identify_tinysa_model,
    tinysa_hardware_profile,
)


def test_hackrf_profile_enforces_frequency_window_and_sample_rate() -> None:
    accepted = HACKRF_ONE_PROFILE.validate_tuning(
        center_frequency_hz=2_400_000_000,
        span_hz=10_000_000,
        sample_rate_hz=10_000_000,
    )
    low_edge = HACKRF_ONE_PROFILE.validate_tuning(
        center_frequency_hz=1_000_000,
        span_hz=2_000_000,
        sample_rate_hz=2_000_000,
    )
    low_rate = HACKRF_ONE_PROFILE.validate_tuning(
        center_frequency_hz=100_000_000,
        span_hz=1_000_000,
        sample_rate_hz=1_999_999,
    )

    assert accepted.accepted
    assert not low_edge.accepted
    assert low_edge.code == "SPECTRUM.WINDOW_OUTSIDE_DEVICE_RANGE"
    assert not low_rate.accepted
    assert low_rate.code == "SPECTRUM.SAMPLE_RATE_UNSUPPORTED"


@pytest.mark.parametrize(
    ("model", "normal_maximum_hz", "ultra_maximum_hz"),
    [
        (TinySaModel.ULTRA_ZS405, 800_000_000, 5_300_000_000),
        (TinySaModel.ULTRA_PLUS_ZS406, 900_000_000, 5_400_000_000),
        (TinySaModel.ULTRA_PLUS_ZS407, 900_000_000, 7_300_000_000),
    ],
)
def test_tinysa_profiles_do_not_claim_harmonic_observation_as_working_range(
    model: TinySaModel,
    normal_maximum_hz: int,
    ultra_maximum_hz: int,
) -> None:
    normal = tinysa_hardware_profile(model, ultra_mode_enabled=False)
    ultra = tinysa_hardware_profile(model, ultra_mode_enabled=True)

    assert normal.maximum_frequency_hz == normal_maximum_hz
    assert ultra.maximum_frequency_hz == ultra_maximum_hz
    assert ultra.maximum_frequency_hz < 12_000_000_000
    assert ultra.validate_tuning(
        center_frequency_hz=ultra_maximum_hz - 500_000,
        span_hz=1_000_000,
    ).accepted
    assert not ultra.validate_tuning(
        center_frequency_hz=ultra_maximum_hz,
        span_hz=1_000_000,
    ).accepted


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("tinySA ULTRA test-fw", TinySaModel.ULTRA_ZS405),
        ("tinySA Ultra+ ZS406", TinySaModel.ULTRA_PLUS_ZS406),
        ("tinySA Ultra+ ZS407", TinySaModel.ULTRA_PLUS_ZS407),
        ("unknown tinySA", TinySaModel.BASIC),
    ],
)
def test_tinysa_model_detection_is_conservative(
    identity: str,
    expected: TinySaModel,
) -> None:
    assert identify_tinysa_model(identity) == expected


def test_hackrf_config_accepts_only_explicit_serial_and_valid_hardware_window() -> None:
    adapter = AdapterConfig(
        id="hackrf-01",
        kind="hackrf",
        connection="HACKRF:0000000000000000deadbeefcafebabe",
    )
    config = AppConfig(
        devices=DevicesConfig(adapters=[adapter]),
        spectrum=SpectrumConfig(
            center_frequency_hz=2_400_000_000,
            span_hz=10_000_000,
            sample_rate_hz=10_000_000,
        ),
    )

    assert config.devices.adapters[0].kind == "hackrf"
    with pytest.raises(ValidationError, match="HACKRF:<hexadecimal serial>"):
        AdapterConfig(
            id="hackrf-wildcard",
            kind="hackrf",
            connection="HACKRF:*",
        )
    with pytest.raises(ValidationError, match=r"2000000\.\.20000000"):
        AppConfig(
            devices=DevicesConfig(adapters=[adapter]),
            spectrum=SpectrumConfig(
                center_frequency_hz=100_000_000,
                span_hz=1_000_000,
                sample_rate_hz=1_000_000,
            ),
        )


def test_explicit_tinysa_model_blocks_out_of_range_saved_configuration() -> None:
    with pytest.raises(ValidationError, match="declared receive range"):
        AppConfig(
            devices=DevicesConfig(
                adapters=[
                    AdapterConfig(
                        id="tiny-405",
                        kind="tinysa",
                        connection="COM7",
                        tinysa_model="ultra_zs405",
                        tinysa_ultra_mode=False,
                    )
                ]
            ),
            spectrum=SpectrumConfig(
                center_frequency_hz=1_000_000_000,
                span_hz=2_000_000,
            ),
        )
