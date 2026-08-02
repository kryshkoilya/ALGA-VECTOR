from __future__ import annotations

# ruff: noqa: RUF001
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from alga_vector.ui.app import create_application
from alga_vector.ui.pages.targets import ExpertTargetsPage

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return create_application(["alga-vector-targets-page-test"])


def _snapshot(
    *,
    current_target: object | None = None,
    targets: tuple[object, ...] = (),
    experience_level: str = "expert",
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_mode="live",
        mode="live",
        captured_at=NOW,
        experience_level=experience_level,
        current_target=current_target,
        targets=targets,
    )


def _target(
    *,
    target_id: str = "target-001",
    direction: object | None = None,
    valid_until: datetime | None = None,
) -> SimpleNamespace:
    confidence = SimpleNamespace(
        value=0.837,
        band="high",
        basis_ru="Сильная повторяемая корреляция; это не вероятность.",
    )
    sources = (
        SimpleNamespace(
            sensor_id="rf-spectrum-01",
            sensor_kind="rf_spectrum",
            contribution=0.72,
            independent_confirmation=False,
            explanation_ru="Устойчивое спектральное наблюдение.",
        ),
        SimpleNamespace(
            sensor_id="acoustic-01",
            sensor_kind="acoustic",
            contribution=0.91,
            independent_confirmation=True,
            explanation_ru="Независимое акустическое подтверждение.",
        ),
    )
    evidence = (
        SimpleNamespace(
            code="RF.TEMPORAL_STABILITY",
            source_id="rf-spectrum-01",
            measured=7,
            unit="окон",
            explanation_ru="Признак повторился в последовательных окнах.",
        ),
    )
    return SimpleNamespace(
        target_id=target_id,
        lifecycle="active",
        confirmation_stage="probable_target",
        probable_type_label_ru="Вероятная воздушная активность",
        short_operator_summary="Несколько наблюдений объединены в одну цель.",
        confidence=confidence,
        sources=sources,
        evidence=evidence,
        direction=direction,
        direction_target_id=target_id if direction is not None else None,
        first_seen=NOW - timedelta(seconds=18),
        last_seen=NOW - timedelta(seconds=1),
        valid_until=valid_until or NOW + timedelta(seconds=12),
        recommended_action_short="Проверьте сектор независимым средством.",
        recommended_action_detailed=(
            "Сопоставьте направление с доступной камерой; "
            "не делайте вывод о дальности."
        ),
        limitations=(
            "Физический тип источника не установлен.",
            "Дальность не измеряется.",
        ),
    )


@pytest.mark.ui
def test_targets_page_has_explicit_empty_state(qt_app: QApplication) -> None:
    page = ExpertTargetsPage()

    page.refresh(_snapshot())
    qt_app.processEvents()

    assert page.objectName() == "expertTargetsPage"
    assert page.target_table.rowCount() == 0
    assert page.empty_state.isVisibleTo(page)
    assert not page._details.isVisibleTo(page)
    assert page.header.status.text() == "ЦЕЛЕЙ НЕТ"
    page.close()


@pytest.mark.ui
def test_expert_target_breakdown_renders_backend_evidence_and_validated_direction(
    qt_app: QApplication,
) -> None:
    direction = SimpleNamespace(
        validated_external=True,
        available=True,
        bearing_deg=113.5,
        uncertainty_deg=8.0,
        source_id="kraken-01",
        calibration_id="kraken-cal-2026-07",
        observed_at=NOW - timedelta(seconds=1),
        valid_until=NOW + timedelta(seconds=2),
    )
    target = _target(direction=direction)
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target, targets=(target,)))
    qt_app.processEvents()

    assert page.target_table.rowCount() == 1
    assert page.target_id.text() == "target-001"
    assert page.lifecycle.text() == "АКТИВНА"
    assert page.confirmation.text() == "ВЕРОЯТНАЯ ЦЕЛЬ"
    assert page.hypothesis.text() == "Вероятная воздушная активность"
    assert "0.837" in page.confidence.text()
    assert "не вероятность" in page.confidence_basis.text()
    assert "113.5" in page.direction_value.text()
    assert "kraken-01" in page.direction_detail.text()
    assert "Дальность и координаты не вычисляются" in page.direction_detail.text()
    assert page.source_table.rowCount() == 2
    independent_item = page.source_table.item(1, 3)
    assert independent_item is not None
    assert independent_item.text() == "Независимое подтверждение"
    assert page.evidence_table.rowCount() == 1
    evidence_code_item = page.evidence_table.item(0, 0)
    assert evidence_code_item is not None
    assert evidence_code_item.text() == "RF.TEMPORAL_STABILITY"
    assert page.limitations.count() == 2
    assert (
        page.recommendation_short.text()
        == "Проверьте сектор независимым средством."
    )
    page.close()


@pytest.mark.ui
def test_guided_snapshot_hides_raw_confidence_and_unvalidated_direction(
    qt_app: QApplication,
) -> None:
    direction = SimpleNamespace(
        validated_external=False,
        available=True,
        bearing_deg=147.25,
        uncertainty_deg=3.0,
        source_id="unvalidated-manual-bearing",
    )
    target = _target(direction=direction)
    target.range_km = 12.4
    target.latitude = 48.123456
    target.longitude = 37.654321
    page = ExpertTargetsPage()
    page.show()

    page.refresh(
        _snapshot(
            current_target=target,
            experience_level="guided",
        )
    )
    qt_app.processEvents()

    assert page.confidence.text() == "Высокая"
    assert "0.837" not in page.confidence.text()
    assert page.direction_value.text() == "Направление недоступно"
    assert "147.25" not in page.direction_detail.text()
    rendered = " ".join(
        (
            page.summary.text(),
            page.direction_value.text(),
            page.direction_detail.text(),
            page.recommendation_short.text(),
            page.recommendation_detailed.text(),
        )
    )
    assert "12.4" not in rendered
    assert "48.123456" not in rendered
    assert "37.654321" not in rendered
    page.close()


@pytest.mark.ui
def test_expired_target_and_direction_are_marked_stale(
    qt_app: QApplication,
) -> None:
    direction = SimpleNamespace(
        validated_external=True,
        available=True,
        bearing_deg=89.0,
        uncertainty_deg=10.0,
        source_id="kraken-01",
        calibration_id="cal-1",
        observed_at=NOW - timedelta(seconds=10),
        valid_until=NOW - timedelta(seconds=5),
    )
    target = _target(
        target_id="target-stale",
        direction=direction,
        valid_until=NOW - timedelta(seconds=1),
    )
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target))
    qt_app.processEvents()

    assert page.header.status.text() == "ЦЕЛЬ НЕАКТУАЛЬНА"
    assert page.stale_notice.isVisibleTo(page)
    assert page.direction_value.text() == "Направление устарело"
    assert "89" not in page.direction_value.text()
    page.close()


@pytest.mark.ui
def test_holding_target_is_not_presented_as_current_or_actionable(
    qt_app: QApplication,
) -> None:
    target = _target(target_id="target-holding")
    target.lifecycle = "holding"
    target.confirmation_stage = "confirmed_target"
    target.recommended_action_short = "Выполните старое оперативное действие."
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target))
    qt_app.processEvents()

    assert page.header.status.text() == "ЦЕЛЬ НЕАКТУАЛЬНА"
    assert page.stale_notice.isVisibleTo(page)
    assert page.lifecycle.text() == "ИСТОРИЧЕСКАЯ · УДЕРЖАНИЕ"
    assert page.confirmation.text() == "ИСТОРИЧЕСКАЯ ЗАПИСЬ"
    assert page.recommendation_short.text() == (
        "Не используйте историческую запись как текущую цель."
    )
    assert "старое оперативное" not in page.recommendation_short.text()
    page.close()


@pytest.mark.ui
def test_selecting_another_target_updates_the_breakdown(
    qt_app: QApplication,
) -> None:
    first = _target(target_id="target-first")
    second = _target(target_id="target-second")
    second.lifecycle = "resolved"
    second.confirmation_stage = "probable_source"
    second.short_operator_summary = "Вторая цель завершена."
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=first, targets=(first, second)))
    page.target_table.selectRow(1)
    qt_app.processEvents()

    assert page.target_id.text() == "target-second"
    assert page.lifecycle.text() == "ИСТОРИЧЕСКАЯ · ЗАВЕРШЕНА"
    assert page.confirmation.text() == "ИСТОРИЧЕСКАЯ ЗАПИСЬ"
    assert page.summary.text() == "Вторая цель завершена."
    assert page.stale_notice.isVisibleTo(page)
    page.close()


@pytest.mark.ui
def test_page_accepts_fused_target_contract_field_names(
    qt_app: QApplication,
) -> None:
    target = SimpleNamespace(
        target_id="fused-contract-target",
        lifecycle="tombstoned",
        confirmation_stage="likely_source",
        probable_type="rf_activity",
        operator_label="RF-активность",
        operator_explanation="Backend объединил связанные нормализованные события.",
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
        last_seen=NOW - timedelta(seconds=4),
        sensors_used=("rf_spectrum",),
        source_attribution=(
            SimpleNamespace(
                sensor_id="rtl-01",
                sensor_kind="rf_spectrum",
                contribution=0.625,
                independent_confirmation=False,
                explanation_ru="Контекстное RF-наблюдение.",
            ),
        ),
        direction=None,
        recommendation=SimpleNamespace(
            code="OP.CONTINUE_OBSERVATION",
            short_ru="Продолжайте наблюдение.",
            detailed_ru="Для изменения стадии требуется независимое подтверждение.",
        ),
        evidence_strength=SimpleNamespace(
            value=0.625,
            band="medium",
            basis_ru="Эвристическая сила признаков, не вероятность.",
        ),
        evidence=(
            SimpleNamespace(
                code="RF.RECURRENCE",
                source_id="rtl-01",
                measured=4,
                unit="окон",
                explanation_ru="Признак повторился в нескольких окнах.",
            ),
        ),
        limitations=("Физическая идентичность не установлена.",),
        recent_event_ids=("event-1", "event-2"),
    )
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target))
    qt_app.processEvents()

    assert page.lifecycle.text() == "ИСТОРИЧЕСКАЯ · АРХИВИРОВАНА"
    assert page.summary.text() == target.operator_explanation
    assert page.confidence.text() == "Средняя · 0.625"
    assert page.confirmation.text() == "ИСТОРИЧЕСКАЯ ЗАПИСЬ"
    assert page.recommendation_short.text() == (
        "Не используйте историческую запись как текущую цель."
    )
    assert "Продолжайте наблюдение" not in page.recommendation_short.text()
    assert page.source_table.rowCount() == 1
    assert page.evidence_table.rowCount() == 1
    evidence_code_item = page.evidence_table.item(0, 0)
    evidence_detail_item = page.evidence_table.item(0, 3)
    assert evidence_code_item is not None
    assert evidence_detail_item is not None
    assert evidence_code_item.text() == "RF.RECURRENCE"
    assert "event-1" not in evidence_detail_item.text()
    assert page.stale_notice.isVisibleTo(page)
    page.close()


@pytest.mark.ui
@pytest.mark.parametrize(
    ("case", "overrides", "expected_label"),
    (
        ("missing-observed", {"observed_at": None}, "Направление недоступно"),
        ("missing-expiry", {"valid_until": None}, "Направление недоступно"),
        ("malformed", {"valid_until": "broken"}, "Направление недоступно"),
        (
            "naive",
            {"observed_at": datetime(2026, 7, 27, 11, 59, 59)},
            "Направление недоступно",
        ),
        (
            "future",
            {
                "observed_at": NOW + timedelta(seconds=1),
                "valid_until": NOW + timedelta(seconds=3),
            },
            "Направление недоступно",
        ),
        (
            "expired",
            {"valid_until": NOW - timedelta(milliseconds=1)},
            "Направление устарело",
        ),
        (
            "wrong-target",
            {"associated_target_id": "different-target"},
            "Направление недоступно",
        ),
    ),
)
def test_expert_direction_fails_closed_for_invalid_time_or_association(
    qt_app: QApplication,
    case: str,
    overrides: dict[str, object],
    expected_label: str,
) -> None:
    values: dict[str, object] = {
        "validated_external": True,
        "available": True,
        "bearing_deg": 271.0,
        "uncertainty_deg": 4.0,
        "source_id": f"df-{case}",
        "calibration_id": "cal-1",
        "observed_at": NOW - timedelta(seconds=1),
        "valid_until": NOW + timedelta(seconds=2),
    }
    values.update(overrides)
    direction = SimpleNamespace(**values)
    target = _target(target_id="direction-target", direction=direction)
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target))
    qt_app.processEvents()

    assert page.direction_value.text() == expected_label
    rendered = f"{page.direction_value.text()} {page.direction_detail.text()}"
    assert "271" not in rendered
    page.close()


@pytest.mark.ui
def test_expert_missing_active_lifecycle_is_historical_and_nonactionable(
    qt_app: QApplication,
) -> None:
    target = _target(target_id="legacy-without-lifecycle")
    del target.lifecycle
    target.confirmation_stage = "confirmed_target"
    target.recommended_action_short = "Выполните старое оперативное действие."
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target))
    qt_app.processEvents()

    assert page.header.status.text() == "ЦЕЛЬ НЕАКТУАЛЬНА"
    assert page.confirmation.text() == "ИСТОРИЧЕСКАЯ ЗАПИСЬ"
    assert page.recommendation_short.text() == (
        "Не используйте историческую запись как текущую цель."
    )
    assert "старое оперативное" not in page.recommendation_short.text()
    page.close()


@pytest.mark.ui
@pytest.mark.parametrize(
    ("lifecycle", "table_state", "header_state", "historical"),
    (
        ("active", "Активна", "ЦЕЛЬ АКТИВНА", False),
        (
            "holding",
            "Историческая · Удержание",
            "ЦЕЛЬ НЕАКТУАЛЬНА",
            True,
        ),
    ),
)
def test_table_and_selected_details_share_one_freshness_verdict(
    qt_app: QApplication,
    lifecycle: str,
    table_state: str,
    header_state: str,
    historical: bool,
) -> None:
    target = _target(target_id=f"consistent-{lifecycle}")
    target.lifecycle = lifecycle
    target.confirmation_stage = "confirmed_target"
    target.recommended_action_short = "Старая оперативная рекомендация."
    page = ExpertTargetsPage()
    page.show()

    page.refresh(_snapshot(current_target=target, targets=(target,)))
    qt_app.processEvents()

    table_lifecycle = page.target_table.item(0, 1)
    table_confirmation = page.target_table.item(0, 2)
    assert table_lifecycle is not None
    assert table_confirmation is not None
    assert table_lifecycle.text() == table_state
    assert page.header.status.text() == header_state
    assert page.stale_notice.isVisibleTo(page) is historical
    if historical:
        assert table_confirmation.text() == "Историческая запись"
        assert page.lifecycle.text().startswith("ИСТОРИЧЕСКАЯ")
        assert page.confirmation.text() == "ИСТОРИЧЕСКАЯ ЗАПИСЬ"
        assert page.recommendation_short.text().startswith("Не используйте")
        assert "Старая оперативная" not in page.recommendation_short.text()
    else:
        assert table_confirmation.text() == "Подтверждённая цель"
        assert page.lifecycle.text() == "АКТИВНА"
        assert page.confirmation.text() == "ПОДТВЕРЖДЁННАЯ ЦЕЛЬ"
        assert page.recommendation_short.text() == (
            "Старая оперативная рекомендация."
        )
    page.close()


@pytest.mark.ui
def test_production_target_allows_only_bounded_internal_snapshot_skew(
    qt_app: QApplication,
) -> None:
    from alga_vector.signal_processor.schema import ConfidenceScore
    from alga_vector.targets.models import (
        ConfirmationStage,
        FusedTarget,
        PhenomenologicalType,
        TargetLifecycle,
        TargetRecommendation,
    )

    pipeline_time = NOW + timedelta(milliseconds=3)
    target = FusedTarget(
        target_id="production-skew",
        lifecycle=TargetLifecycle.ACTIVE,
        confirmation_stage=ConfirmationStage.LIKELY_SOURCE,
        probable_type=PhenomenologicalType.MULTISENSOR_ACTIVITY,
        technical_label="MULTISENSOR_CORRELATED",
        operator_label="Согласованная активность",
        operator_explanation="Production target обновлён внутри того же snapshot pipeline.",
        created_at=NOW - timedelta(seconds=1),
        updated_at=pipeline_time,
        last_seen=pipeline_time,
        sensors_used=(),
        source_attribution=(),
        direction=None,
        zone=None,
        recommendation=TargetRecommendation(
            code="TARGET.CHECK",
            short_ru="Продолжайте проверку.",
            detailed_ru="Используйте свежие данные.",
        ),
        evidence_strength=ConfidenceScore.heuristic(
            0.7,
            "Эвристическая сила признаков.",
        ),
        evidence=(),
    )
    snapshot = SimpleNamespace(
        runtime_mode="live",
        mode="live",
        captured_at=NOW,
        experience_level="expert",
        current_target=target,
        targets=(target,),
    )
    page = ExpertTargetsPage()
    page.show()

    page.refresh(snapshot)
    qt_app.processEvents()

    table_lifecycle = page.target_table.item(0, 1)
    assert table_lifecycle is not None
    assert table_lifecycle.text() == "Активна"
    assert page.header.status.text() == "ЦЕЛЬ АКТИВНА"
    assert page.lifecycle.text() == "АКТИВНА"
    assert page.stale_notice.isHidden()
    page.close()


@pytest.mark.ui
def test_historical_banner_does_not_clip_current_target_at_1440x900(
    qt_app: QApplication,
) -> None:
    target = _target(target_id="layout-historical")
    target.lifecycle = "holding"
    target.confirmation_stage = "confirmed_target"
    target.recommended_action_short = "Старая оперативная рекомендация."
    page = ExpertTargetsPage()
    page.resize(1440, 900)
    page.show()

    page.refresh(_snapshot(current_target=target, targets=(target,)))
    qt_app.processEvents()

    assert page.size().width() == 1440
    assert page.size().height() == 900
    assert page.stale_notice.isVisibleTo(page)
    assert page.left_scroll.viewport().height() > 0
    assert page.overview_panel.height() >= page.overview_panel.minimumSizeHint().height()
    for widget in (
        page.target_id,
        page.lifecycle,
        page.confirmation,
        page.hypothesis,
        page.confidence,
    ):
        visible = widget.visibleRegion().boundingRect()
        assert widget.isVisibleTo(page)
        assert visible.height() == widget.height()
    page.close()
