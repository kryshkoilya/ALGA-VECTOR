from __future__ import annotations

# ruff: noqa: RUF001
from datetime import UTC, datetime
from types import SimpleNamespace

from alga_vector.direction import DirectionService, ExternalDirectionEvidence
from alga_vector.ui.direction_presenter import (
    RANGE_LIMITATION_RU,
    ReceivedLevelTrendPresenter,
    present_bearing,
)


def _rf_snapshot(
    sequence: int,
    peak_level: float,
    *,
    source_id: str = "receiver-a",
    state: str = "background",
    center_frequency_hz: int = 433_000_000,
    span_hz: int = 2_000_000,
) -> object:
    return SimpleNamespace(
        spectrum=SimpleNamespace(
            source_id=source_id,
            sequence=sequence,
            captured_at=datetime(2026, 7, 26, 12, 0, sequence, tzinfo=UTC),
            center_frequency_hz=center_frequency_hz,
            span_hz=span_hz,
            power_dbm=(-100.0, peak_level, -98.0),
            unit="dBFS",
        ),
        signal_assessment=SimpleNamespace(state=state),
    )


def test_bearing_view_marks_only_validated_external_df_as_measured() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    external = DirectionService(clock=lambda: now).ingest_external(
        127.5,
        uncertainty_deg=6.5,
        confidence=0.88,
        captured_at=now,
        source_id="df-array-lab-01",
        evidence=ExternalDirectionEvidence(
            calibration_id="lab-cal-01",
            calibrated_at=now,
            evidence_at=now,
            sample_count=8,
            quality_score=0.91,
            calibration_valid=True,
        ),
    )
    manual = DirectionService(clock=lambda: now).set_manual(222.2)
    demo = DirectionService(demo_mode=True, clock=lambda: now).set_simulated(
        42.0
    )

    external_view = present_bearing(external)
    manual_view = present_bearing(manual)
    demo_view = present_bearing(demo)

    assert external_view.measured
    assert external_view.value == "127.5°"
    assert "ВНЕШНИЙ DF" in external_view.state
    assert not manual_view.measured
    assert manual_view.value == "222.2° · ВВОД"
    assert "НЕ ИЗМЕРЕНИЕ" in manual_view.state
    assert not demo_view.measured
    assert demo_view.simulated
    assert demo_view.value == "042.0° · ДЕМО"
    assert "СИМУЛЯЦИЯ" in demo_view.state


def test_received_level_trend_is_deduplicated_and_scoped_to_receiver() -> None:
    presenter = ReceivedLevelTrendPresenter()

    first = presenter.present(_rf_snapshot(1, -90.0))
    duplicate = presenter.present(_rf_snapshot(1, -90.0))
    second = presenter.present(_rf_snapshot(2, -85.0))
    rising = presenter.present(_rf_snapshot(3, -80.0))

    assert first.state == "НАКОПЛЕНИЕ"
    assert duplicate.sample_count == 1
    assert second.sample_count == 2
    assert rising.state == "РАСТЁТ"
    assert rising.sample_count == 3
    assert rising.slope_db_per_frame == 5.0
    assert "пространственной интерпретации" in rising.detail
    assert RANGE_LIMITATION_RU in rising.detail
    assert " км" not in rising.detail.lower()
    assert "приближ" not in rising.detail.lower()


def test_received_level_trend_reports_stable_and_falling_without_range() -> None:
    stable_presenter = ReceivedLevelTrendPresenter()
    stable_presenter.present(_rf_snapshot(1, -80.0))
    stable_presenter.present(_rf_snapshot(2, -80.1))
    stable = stable_presenter.present(_rf_snapshot(3, -79.9))

    falling_presenter = ReceivedLevelTrendPresenter()
    falling_presenter.present(_rf_snapshot(1, -70.0))
    falling_presenter.present(_rf_snapshot(2, -75.0))
    falling = falling_presenter.present(_rf_snapshot(3, -80.0))

    assert stable.state == "СТАБИЛЕН"
    assert falling.state == "ПАДАЕТ"
    assert RANGE_LIMITATION_RU in stable.detail
    assert RANGE_LIMITATION_RU in falling.detail


def test_source_change_and_unreliable_frame_fail_closed() -> None:
    presenter = ReceivedLevelTrendPresenter()
    presenter.present(_rf_snapshot(1, -90.0))
    presenter.present(_rf_snapshot(2, -85.0))

    changed = presenter.present(
        _rf_snapshot(1, -70.0, source_id="receiver-b")
    )
    unreliable = presenter.present(
        _rf_snapshot(
            2,
            -65.0,
            source_id="receiver-b",
            state="data_unreliable",
        )
    )

    assert changed.state == "НАКОПЛЕНИЕ"
    assert changed.sample_count == 1
    assert unreliable.state == "НЕТ ДАННЫХ"
    assert unreliable.sample_count == 0
    assert "сброшен" in unreliable.detail
    assert RANGE_LIMITATION_RU in unreliable.detail


def test_missing_frame_clears_old_trend_history() -> None:
    presenter = ReceivedLevelTrendPresenter()
    presenter.present(_rf_snapshot(1, -90.0))
    presenter.present(_rf_snapshot(2, -85.0))
    missing = presenter.present(
        SimpleNamespace(
            spectrum=None,
            signal_assessment=SimpleNamespace(state="no_data"),
        )
    )
    restarted = presenter.present(_rf_snapshot(3, -80.0))

    assert missing.state == "НЕТ ДАННЫХ"
    assert restarted.state == "НАКОПЛЕНИЕ"
    assert restarted.sample_count == 1


def test_scan_retune_resets_received_level_trend() -> None:
    presenter = ReceivedLevelTrendPresenter()
    presenter.present(_rf_snapshot(1, -90.0))
    presenter.present(_rf_snapshot(2, -85.0))

    retuned = presenter.present(
        _rf_snapshot(
            3,
            -60.0,
            center_frequency_hz=915_000_000,
        )
    )

    assert retuned.state == "НАКОПЛЕНИЕ"
    assert retuned.sample_count == 1
    assert retuned.slope_db_per_frame is None
    assert RANGE_LIMITATION_RU in retuned.detail
