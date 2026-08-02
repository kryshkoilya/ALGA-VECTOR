from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alga_vector.devices.capabilities import (
    HACKRF_ONE_PROFILE,
    TinySaModel,
    tinysa_hardware_profile,
)
from alga_vector.devices.scan_plan import (
    GENERAL_SCAN_PRESETS,
    ScanPlanCursor,
    ScanPlanLimitationSeverity,
    ScanPlanRequest,
    ScanRange,
    compile_scan_plan,
    full_supported_scan_request,
    scan_request_from_preset,
)
from alga_vector.devices.tuning import BLOG_V4_PROFILE, GENERIC_RTLSDR_PROFILE
from alga_vector.domain.errors import AppError


def _limitation_codes(plan: object) -> set[str]:
    return {item.code for item in plan.limitations}  # type: ignore[attr-defined]


def test_general_presets_are_source_neutral_and_have_stable_ids() -> None:
    all_text = " ".join(
        text
        for preset in GENERAL_SCAN_PRESETS
        for text in (
            preset.preset_id,
            preset.label_ru,
            preset.note_ru,
            *(item.label_ru for item in preset.ranges),
        )
    ).casefold()

    assert "дрон" not in all_text
    assert "воен" not in all_text
    assert "украин" not in all_text
    assert "всу" not in all_text
    assert {item.preset_id for item in GENERAL_SCAN_PRESETS} == {
        "field_priority",
        "general_vhf",
        "general_uhf",
        "general_l_band",
        "general_s_band",
        "general_c_band",
        "general_wide",
    }


def test_field_priority_is_capability_clipped_for_rtlsdr() -> None:
    request = scan_request_from_preset(
        "field_priority",
        window_span_hz=2_400_000,
        overlap_fraction=0.0,
        dwell_time_ms=50,
        maximum_windows=128,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert plan.accepted
    assert len(plan.windows) <= 128
    assert plan.estimated_cycle_ms <= 120_000
    assert all(
        window.stop_frequency_hz <= GENERIC_RTLSDR_PROFILE.maximum_frequency_hz
        for window in plan.windows
    )
    assert {item.requested.range_id for item in plan.excluded_ranges} == {
        "field_2400",
        "field_5800",
    }


def test_field_priority_includes_high_ranges_only_on_compatible_hackrf() -> None:
    request = scan_request_from_preset(
        "field_priority",
        window_span_hz=2_000_000,
        overlap_fraction=0.0,
        dwell_time_ms=50,
        maximum_windows=128,
    )
    plan = compile_scan_plan(
        HACKRF_ONE_PROFILE,
        request,
        sample_rate_hz=2_000_000,
    )

    assert plan.accepted
    assert len(plan.windows) <= 128
    assert plan.estimated_cycle_ms <= 120_000
    assert not plan.excluded_ranges
    covered_ids = {item.range_id for item in plan.covered_ranges}
    assert {"field_2400", "field_5800"}.issubset(covered_ids)
    assert any(window.center_frequency_hz >= 5_725_000_000 for window in plan.windows)


def test_rtlsdr_wide_preset_never_schedules_outside_confirmed_hardware() -> None:
    request = scan_request_from_preset("general_wide")
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert plan.accepted
    assert plan.coverage_fraction < 1.0
    assert all(
        GENERIC_RTLSDR_PROFILE.minimum_frequency_hz
        <= window.start_frequency_hz
        < window.stop_frequency_hz
        <= GENERIC_RTLSDR_PROFILE.maximum_frequency_hz
        for window in plan.windows
    )
    assert "SCAN_PLAN.COVERAGE_CLIPPED_TO_HARDWARE" in _limitation_codes(plan)
    assert "SCAN_PLAN.SEQUENTIAL_SWEEP_MAY_MISS_SHORT_EVENTS" in _limitation_codes(plan)
    assert "SCAN_PLAN.FREQUENCY_IS_NOT_SOURCE_IDENTITY" in _limitation_codes(plan)


def test_hackrf_general_wide_reaches_but_never_crosses_six_ghz() -> None:
    request = scan_request_from_preset(
        "general_wide",
        window_span_hz=20_000_000,
    )
    plan = compile_scan_plan(
        HACKRF_ONE_PROFILE,
        request,
        sample_rate_hz=20_000_000,
    )

    assert plan.accepted
    assert plan.coverage_fraction == pytest.approx(1.0)
    assert max(item.stop_frequency_hz for item in plan.windows) == 6_000_000_000
    assert all(item.start_frequency_hz >= 1_000_000 for item in plan.windows)


def test_tinysa_basic_rejects_unsupported_high_range_instead_of_claiming_it() -> None:
    profile = tinysa_hardware_profile(
        TinySaModel.BASIC,
        ultra_mode_enabled=False,
    )
    request = scan_request_from_preset("general_c_band")
    plan = compile_scan_plan(profile, request)

    assert not plan.accepted
    assert not plan.windows
    assert plan.coverage_fraction == 0.0
    assert plan.excluded_ranges[0].code == "SCAN_PLAN.RANGE_OUTSIDE_DEVICE"
    assert any(
        item.severity == ScanPlanLimitationSeverity.BLOCKING
        for item in plan.limitations
    )


def test_full_supported_request_comes_only_from_declared_profile_range() -> None:
    request = full_supported_scan_request(
        BLOG_V4_PROFILE,
        window_span_hz=2_400_000,
    )
    plan = compile_scan_plan(
        BLOG_V4_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert request.ranges[0].start_frequency_hz == 500_000
    assert request.ranges[0].stop_frequency_hz == 1_766_000_000
    assert plan.accepted
    assert min(item.start_frequency_hz for item in plan.windows) == 500_000
    assert max(item.stop_frequency_hz for item in plan.windows) == 1_766_000_000


def test_iq_plan_requires_sample_rate_before_any_window_is_scheduled() -> None:
    request = scan_request_from_preset("general_vhf")
    plan = compile_scan_plan(GENERIC_RTLSDR_PROFILE, request)

    assert not plan.accepted
    assert plan.windows == ()
    assert "SPECTRUM.SAMPLE_RATE_REQUIRED" in _limitation_codes(plan)


def test_requested_window_is_capped_to_real_instantaneous_bandwidth() -> None:
    request = scan_request_from_preset(
        "general_vhf",
        window_span_hz=10_000_000,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert plan.accepted
    assert max(window.span_hz for window in plan.windows) <= 2_400_000
    assert "SCAN_PLAN.WINDOW_SPAN_CAPPED" in _limitation_codes(plan)


def test_protective_window_limit_blocks_partial_plan_execution() -> None:
    request = scan_request_from_preset(
        "general_vhf",
        window_span_hz=1_000_000,
        maximum_windows=2,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert not plan.accepted
    assert plan.windows == ()
    assert "SCAN_PLAN.TOO_MANY_WINDOWS" in _limitation_codes(plan)


def test_cursor_holds_each_window_for_successful_temporal_dwell() -> None:
    request = ScanPlanRequest(
        plan_id="cursor_test",
        ranges=(ScanRange("test_band", "Тестовый участок", 100_000_000, 102_000_000),),
        window_span_hz=1_000_000,
        overlap_fraction=0.0,
        dwell_frames=3,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )
    fixed_time = datetime(2026, 7, 26, tzinfo=UTC)
    cursor = ScanPlanCursor(plan, clock=lambda: fixed_time)

    first = cursor.next_window()
    assert cursor.next_window() is first
    cursor.mark_result(False, detail_code="SPECTRUM.NO_FRAME")
    assert cursor.next_window().window_id == first.window_id
    cursor.mark_result(True)
    assert cursor.next_window().window_id == first.window_id
    cursor.mark_result(True)
    assert cursor.next_window().window_id == first.window_id
    cursor.mark_result(True)

    second = cursor.next_window()
    status = cursor.snapshot()
    assert second.window_id != first.window_id
    assert status.current_ordinal == 1
    assert status.completed_attempts == 4
    assert status.completed_windows == 1
    assert status.failed_windows == 1
    assert status.successful_frames_in_window == 0
    assert status.last_result is not None
    assert status.last_result.recorded_at == fixed_time


def test_cursor_completes_cycle_only_after_every_window_dwell() -> None:
    request = ScanPlanRequest(
        plan_id="cycle_test",
        ranges=(ScanRange("test_band", "Тестовый участок", 100_000_000, 102_000_000),),
        window_span_hz=1_000_000,
        overlap_fraction=0.0,
        dwell_frames=3,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )
    cursor = ScanPlanCursor(plan)

    for _window in plan.windows:
        for _frame in range(3):
            cursor.next_window()
            cursor.mark_result(True)

    assert cursor.snapshot().completed_cycles == 1
    assert cursor.next_window().window_id == plan.windows[0].window_id


def test_cursor_rejects_result_without_pending_window() -> None:
    request = ScanPlanRequest(
        plan_id="pending_test",
        ranges=(ScanRange("test_band", "Тестовый участок", 100_000_000, 101_000_000),),
        dwell_frames=3,
    )
    plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )
    cursor = ScanPlanCursor(plan)

    with pytest.raises(AppError, match="окно обзора"):
        cursor.mark_result(True)


def test_low_dwell_is_explicitly_warned_and_default_is_temporally_usable() -> None:
    default_request = scan_request_from_preset("general_vhf")
    short_request = scan_request_from_preset("general_vhf", dwell_frames=3)
    default_plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        default_request,
        sample_rate_hz=2_400_000,
    )
    short_plan = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        short_request,
        sample_rate_hz=2_400_000,
    )

    assert default_request.dwell_frames == 12
    assert all(window.dwell_frames == 12 for window in default_plan.windows)
    assert default_plan.dwell_time_ms == default_request.dwell_time_ms
    assert default_plan.retune_settle_ms == default_request.retune_settle_ms
    assert default_plan.estimated_cycle_ms == len(default_plan.windows) * (
        default_request.dwell_frames * default_request.dwell_time_ms
        + default_request.retune_settle_ms
    )
    assert "SCAN_PLAN.DWELL_BELOW_TEMPORAL_RECOMMENDATION" not in _limitation_codes(
        default_plan
    )
    assert "SCAN_PLAN.DWELL_BELOW_TEMPORAL_RECOMMENDATION" in _limitation_codes(
        short_plan
    )


def test_plan_windows_and_ids_are_deterministic() -> None:
    request = scan_request_from_preset(
        "general_uhf",
        window_span_hz=10_000_000,
    )
    first = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )
    second = compile_scan_plan(
        GENERIC_RTLSDR_PROFILE,
        request,
        sample_rate_hz=2_400_000,
    )

    assert first.windows == second.windows
    assert len({item.window_id for item in first.windows}) == len(first.windows)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dwell_frames": 2},
        {"dwell_frames": 1_001},
        {"overlap_fraction": 0.75},
        {"window_span_hz": 999},
    ],
)
def test_request_rejects_unsafe_scheduler_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ScanPlanRequest(
            plan_id="invalid",
            ranges=(
                ScanRange("test_band", "Тестовый участок", 100_000_000, 101_000_000),
            ),
            **kwargs,  # type: ignore[arg-type]
        )
