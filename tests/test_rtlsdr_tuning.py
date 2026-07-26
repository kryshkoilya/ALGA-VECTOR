from __future__ import annotations

import pytest

from alga_vector.devices.tuning import (
    BLOG_V3_DIRECT_Q_PROFILE,
    BLOG_V4_PROFILE,
    FREQUENCY_PRESETS,
    GENERIC_RTLSDR_PROFILE,
    RtlSdrInputMode,
    available_frequency_presets,
    identify_rtlsdr_profile,
    require_rtlsdr_tuning,
    select_rtlsdr_profile,
    validate_rtlsdr_tuning,
)
from alga_vector.domain.errors import AppError


def test_blog_v4_identity_enables_full_documented_tuning_range() -> None:
    profile = identify_rtlsdr_profile(
        "RTLSDRBlog",
        "Blog V4",
        direct_sampling_api=True,
    )

    assert profile == BLOG_V4_PROFILE
    assert profile.minimum_frequency_hz == 500_000
    assert profile.maximum_frequency_hz == 1_766_000_000
    assert profile.input_mode_for(10_000_000) == RtlSdrInputMode.BLOG_V4_UPCONVERTER
    assert profile.input_mode_for(100_000_000) == RtlSdrInputMode.TUNER


def test_unknown_rtlsdr_does_not_claim_an_unverified_hf_input() -> None:
    profile = identify_rtlsdr_profile(
        "Generic",
        "RTL2832U",
        direct_sampling_api=True,
    )

    assert profile == GENERIC_RTLSDR_PROFILE
    with pytest.raises(AppError) as caught:
        require_rtlsdr_tuning(
            profile,
            center_frequency_hz=10_000_000,
            span_hz=2_000_000,
            sample_rate_hz=2_400_000,
        )
    assert caught.value.code == "SPECTRUM.FREQUENCY_OUTSIDE_DEVICE_RANGE"


def test_blog_v3_uses_direct_q_only_when_api_is_available() -> None:
    with_api = identify_rtlsdr_profile(
        "RTLSDRBlog",
        "Blog V3",
        direct_sampling_api=True,
    )
    without_api = identify_rtlsdr_profile(
        "RTLSDRBlog",
        "Blog V3",
        direct_sampling_api=False,
    )

    assert with_api == BLOG_V3_DIRECT_Q_PROFILE
    assert with_api.input_mode_for(10_000_000) == RtlSdrInputMode.DIRECT_Q
    assert without_api == GENERIC_RTLSDR_PROFILE


def test_operator_cannot_force_blog_v4_hf_without_driver_identity() -> None:
    selected = select_rtlsdr_profile(
        GENERIC_RTLSDR_PROFILE,
        override="blog_v4",
        direct_sampling_api=True,
    )

    assert selected == GENERIC_RTLSDR_PROFILE


def test_entire_fft_window_must_remain_inside_hardware_range() -> None:
    rejected = validate_rtlsdr_tuning(
        BLOG_V4_PROFILE,
        center_frequency_hz=1_766_000_000,
        span_hz=2_000_000,
        sample_rate_hz=2_400_000,
    )
    accepted = validate_rtlsdr_tuning(
        BLOG_V4_PROFILE,
        center_frequency_hz=1_000_000,
        span_hz=1_000_000,
        sample_rate_hz=2_400_000,
    )

    assert not rejected.accepted
    assert rejected.code == "SPECTRUM.WINDOW_OUTSIDE_DEVICE_RANGE"
    assert accepted.accepted


@pytest.mark.parametrize("sample_rate_hz", [8_000, 500_000, 3_200_001])
def test_rtlsdr_rejects_driver_sample_rate_gaps(sample_rate_hz: int) -> None:
    result = validate_rtlsdr_tuning(
        BLOG_V4_PROFILE,
        center_frequency_hz=100_000_000,
        span_hz=min(sample_rate_hz, 100_000),
        sample_rate_hz=sample_rate_hz,
    )

    assert not result.accepted
    assert result.code == "SPECTRUM.RTLSDR_SAMPLE_RATE_UNSUPPORTED"


def test_rtlsdr_marks_3_2_msps_as_drop_prone_not_stable() -> None:
    result = validate_rtlsdr_tuning(
        BLOG_V4_PROFILE,
        center_frequency_hz=100_000_000,
        span_hz=3_200_000,
        sample_rate_hz=3_200_000,
    )

    assert result.accepted
    assert result.warning_ru is not None
    assert "пропуски" in result.warning_ru


def test_presets_are_receive_only_civilian_shortcuts_not_source_labels() -> None:
    all_text = " ".join(
        f"{preset.preset_id} {preset.label_ru} {preset.note_ru}"
        for preset in FREQUENCY_PRESETS
    ).casefold()

    assert "дрон" not in all_text
    assert "воен" not in all_text
    assert {preset.preset_id for preset in FREQUENCY_PRESETS} >= {
        "broadcast_am",
        "broadcast_fm",
        "weather_satellite",
        "ism_433",
        "civil_adsb",
    }
    generic_ids = {
        preset.preset_id
        for preset in available_frequency_presets(GENERIC_RTLSDR_PROFILE)
    }
    assert "broadcast_am" not in generic_ids
    assert "broadcast_fm" in generic_ids
