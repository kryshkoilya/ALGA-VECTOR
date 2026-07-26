from __future__ import annotations

# ruff: noqa: RUF001
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from alga_vector.ui.pages.dashboard import DashboardPage
from alga_vector.ui.pages.events import SignalEventsPage
from alga_vector.ui.pages.spectrum import SpectrumPage
from alga_vector.ui.signal_presenter import present_signal_assessment


def _assessment(
    state: str = "concentrated_rf",
    *,
    trust: str = "high",
) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        trust=trust,
        attribution="not_available",
        identity_established=False,
        reason_code=f"ASSESSMENT.{state.upper()}",
        headline_ru={
            "no_data": "Измеренных данных пока нет",
            "learning_background": "Система изучает обычный фон",
            "background_only": "Заметных изменений не найдено",
            "data_unreliable": "Данные нужно проверить",
            "concentrated_rf": "Замечено узкое изменение в эфире",
            "wideband_rf": "Изменился широкий участок эфира",
            "transient_burst": "Зафиксирован короткий всплеск",
            "unclassified_rf": "Есть неоднозначное изменение",
        }[state],
        explanation_ru="Форма принятого сигнала изменилась относительно фона.",
        operator_action_ru="Наблюдайте: система проверит, сохраняется ли изменение.",
        source_id="rtl-01",
        sequence=7,
        observed_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        quality_flags=frozenset({"absolute_calibration_unverified"}),
        evidence=SimpleNamespace(
            coverage_low_hz=432_920_000.0,
            coverage_high_hz=434_920_000.0,
            peak_frequency_hz=433_921_000.0,
            occupied_bandwidth_hz=31_250.0,
            peak_excess_over_floor_db=18.0,
            active_fraction=0.03,
            persistence_frames=3,
            baseline_frames=8,
            baseline_required_frames=8,
            data_age_ms=12,
            power_unit="dBFS",
        ),
    )


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        observed_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        classification="narrowband_activity",
        level_trend="rising_received_power",
        confidence=0.83,
        quality_flags=frozenset(
            {"absolute_calibration_unverified", "insufficient_history"}
        ),
        evidence=SimpleNamespace(
            peak_frequency_hz=433_921_000.0,
            occupied_bandwidth_hz=31_250.0,
        ),
    )


def _decision(
    lifecycle: str = "confirmed",
    *,
    episode_id: str | None = "rf-episode-0001",
    alertable: bool | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        source_id="rtl-01",
        observed_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        lifecycle=lifecycle,
        family="voice_like_compatible",
        family_explanation_ru=(
            "Признаки совместимы с узкополосным каналом с изменяющейся "
            "огибающей; тип физического источника не подтверждён."
        ),
        episode_id=episode_id,
        peak_frequency_hz=433_921_000.0,
        occupied_bandwidth_hz=31_250.0,
        heuristic_score=0.83,
        calibrated_probability=None,
        evidence_strength="high",
        data_quality="medium",
        alertable=(
            lifecycle in {"confirmed", "holding"}
            if alertable is None
            else alertable
        ),
        supporting_evidence=(
            SimpleNamespace(
                explanation_ru="Изменение повторилось в трёх из пяти наблюдений."
            ),
        ),
        contradicting_evidence=(
            SimpleNamespace(
                explanation_ru="Второго независимого RF-сенсора нет."
            ),
        ),
        missing_confirmation=(
            SimpleNamespace(
                explanation_ru="Не хватает независимого подтверждения источника."
            ),
        ),
        sensor_contributions=(
            SimpleNamespace(
                source_id="rtl-01",
                contribution=0.83,
                data_quality="medium",
                independent_confirmation=False,
                explanation_ru="Основной RF-поток прошёл проверку непрерывности.",
            ),
        ),
        alternatives=(
            SimpleNamespace(
                family="carrier",
                explanation_ru="Изменение уровня может быть федингом несущей.",
            ),
        ),
        limitations=(
            SimpleNamespace(
                explanation_ru=(
                    "Спектральная форма не устанавливает класс объекта."
                )
            ),
        ),
    )


def _snapshot(
    *,
    experience: str = "guided",
    devices: tuple[object, ...] = (),
    spectrum: object | None = None,
    assessment: object | None = None,
    decision: object | None = None,
    events: tuple[object, ...] = (),
    incidents: tuple[object, ...] = (),
) -> object:
    return SimpleNamespace(
        revision=7,
        devices=devices,
        capabilities=(),
        incidents=incidents,
        spectrum=spectrum,
        mode="live",
        runtime_mode="live",
        profile_name="Рабочий профиль",
        readiness_percent=80 if spectrum is not None else 0,
        experience_level=experience,
        location=SimpleNamespace(absolute_position_allowed=False),
        map_status=SimpleNamespace(
            available=False,
            network_enabled=True,
            online_state="idle",
        ),
        signal_events=events,
        signal_assessment=assessment,
        signal_decision=decision,
    )


class _Runtime:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot

    def current_snapshot(self) -> object:
        return self.snapshot

    def recording_status(self) -> object:
        return SimpleNamespace(
            active=False,
            path=None,
            elapsed_seconds=0.0,
            frames=0,
            bytes_written=0,
            bytes_per_second=0.0,
        )


def _cell_text(page: SignalEventsPage, row: int, column: int) -> str:
    cell = page.table.item(row, column)
    assert cell is not None
    return cell.text()


def _header_text(page: SignalEventsPage, column: int) -> str:
    cell = page.table.horizontalHeaderItem(column)
    assert cell is not None
    return cell.text()


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication(["alga-vector-guided-test"])


def test_presenter_explains_measurement_without_attribution() -> None:
    snapshot = _snapshot(assessment=_assessment())

    view = present_signal_assessment(snapshot)
    rendered = " ".join(
        (
            view.headline,
            view.observation,
            view.coverage,
            *view.reasons,
            view.trust,
            view.attribution_answer,
            view.next_action,
        )
    ).lower()

    assert "432.920 мгц" in rendered
    assert "434.920 мгц" in rendered
    assert "нет. текущие данные спектра" in rendered
    assert "дрон обнаружен" not in rendered
    assert "цель обнаружена" not in rendered
    assert "объект приближается" not in rendered
    assert "вероятность дрона" not in rendered


def test_presenter_exposes_temporal_decision_chain_without_identity_claim() -> None:
    snapshot = _snapshot(
        assessment=_assessment(),
        decision=_decision("candidate", alertable=False),
    )

    view = present_signal_assessment(snapshot)
    rendered = " ".join(
        (
            view.headline,
            view.observation,
            *view.reasons,
            view.trust,
            view.attribution_answer,
            view.next_action,
        )
    ).lower()

    assert view.state == "candidate"
    assert "проверяется" in view.lifecycle.lower()
    assert view.data_quality.startswith("среднее")
    assert view.evidence_strength.startswith("высокая")
    assert view.supporting_evidence
    assert view.contradicting_evidence
    assert view.missing_confirmation
    assert view.sensor_contributions
    assert view.alternatives
    assert view.limitations
    assert "за:" in rendered
    assert "против:" in rendered
    assert "не хватает:" in rendered
    assert "вклад сенсора:" in rendered
    assert "альтернатива:" in rendered
    assert "ограничение:" in rendered
    assert "качество данных — среднее" in rendered
    assert "сила rf-признаков — высокая" in rendered
    assert "не вероятность идентификации" in rendered
    assert "это дрон" not in rendered
    assert "это рация" not in rendered


def test_presenter_keeps_all_quality_failures_before_optional_measurements() -> None:
    assessment = _assessment("data_unreliable", trust="low")
    assessment.quality_flags = frozenset(
        {
            "absolute_calibration_unverified",
            "dropped_frames_reported",
            "sequence_gap",
            "data_stale",
            "clock_regression",
        }
    )
    snapshot = _snapshot(assessment=assessment)

    first = present_signal_assessment(snapshot).reasons
    second = present_signal_assessment(snapshot).reasons

    assert first == second
    assert "Последний кадр устарел." in first
    assert "В последовательности измерений есть разрыв." in first
    assert "Приёмник сообщил о пропущенных кадрах." in first
    assert "Временные метки источника идут непоследовательно." in first
    peak_index = next(
        index for index, reason in enumerate(first) if "Самое заметное изменение" in reason
    )
    assert first.index("Последний кадр устарел.") < peak_index
    assert first.index("Приёмник сообщил о пропущенных кадрах.") < peak_index


@pytest.mark.ui
def test_guided_dashboard_has_one_clear_setup_path(
    qt_app: QApplication,
) -> None:
    snapshot = _snapshot(assessment=_assessment("no_data", trust="low"))
    page = DashboardPage(_Runtime(snapshot))
    opened: list[str] = []
    page.open_page.connect(opened.append)
    page.refresh(snapshot)
    qt_app.processEvents()

    assert not page.guided_panel.isHidden()
    assert page.guided_next_button.text() == "Найти RTL-SDR"
    assert page.next_action_metric.value_label.text() == "Устройства → Найти RTL-SDR"
    assert "Приёмник: нужно настроить" in page.guided_checklist.text()
    assert page.open_spectrum_button.isHidden()
    assert page.open_devices_button.isHidden()
    assert page.open_diagnostics_button.isHidden()

    page.guided_next_button.click()
    assert opened == ["devices"]
    page.close()


@pytest.mark.ui
def test_acknowledged_critical_incident_remains_active_and_overrides_guided_action(
    qt_app: QApplication,
) -> None:
    incident = SimpleNamespace(
        incident_id="storage-critical",
        severity="critical",
        acknowledged=True,
        title_ru="Хранилище недоступно",
        message_ru="Запись данных остановлена.",
        action_ru="Проверьте локальный диск.",
    )
    snapshot = _snapshot(
        assessment=_assessment("background_only"),
        incidents=(incident,),
    )
    page = DashboardPage(_Runtime(snapshot))
    opened: list[str] = []
    page.open_page.connect(opened.append)
    page.refresh(snapshot)
    qt_app.processEvents()

    assert page.incident_metric.value_label.text() == "1"
    assert page.header.status.text() == "КРИТИЧЕСКИЙ ИНЦИДЕНТ"
    assert page.header.status.property("statusLevel") == "critical"
    assert page.guided_next_button.text() == "Разобрать критический инцидент"
    assert "причина остаётся активной" in page.incidents.item(0).text()
    page.guided_next_button.click()
    assert opened == ["diagnostics"]
    page.close()


@pytest.mark.ui
def test_guided_dashboard_renders_reason_trust_and_honest_answer(
    qt_app: QApplication,
) -> None:
    device = SimpleNamespace(
        device_id="rtl-01",
        display_name="RTL-SDR",
        kind="rtlsdr",
        state="streaming",
        health="healthy",
    )
    frame = SimpleNamespace(
        source_id="rtl-01",
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=2_000_000,
        power_dbm=[-90.0, -68.0, -88.0],
        dropped_frames=0,
        data_age_ms=12,
    )
    snapshot = _snapshot(
        devices=(device,),
        spectrum=frame,
        assessment=_assessment(),
    )
    page = DashboardPage(_Runtime(snapshot))
    page.refresh(snapshot)
    qt_app.processEvents()

    assert page.assessment_headline.text() == "Замечено узкое изменение в эфире"
    assert "Почему" in " ".join(
        label.text() for label in page.guided_panel.findChildren(QLabel)
    )
    assert "не устанавливают физический источник" in page.assessment_attribution.text()
    assert "класс объекта" in page.assessment_attribution.text()
    assert "%" not in page.assessment_trust.text()
    reason_lines = page.assessment_reasons.text().splitlines()
    assert len(reason_lines) <= 4
    assert "Подробная цепочка решения" in reason_lines[-1]
    assert "Измеренный кадр: готово" in page.guided_checklist.text()
    assert "Интерпретация и события: готово" in page.guided_checklist.text()
    assert page.guided_next_button.text() == "Открыть события"
    assert "карта" not in page.guided_checklist.text().lower()
    assert "gps" not in page.guided_checklist.text().lower()
    page.close()


@pytest.mark.ui
def test_guided_dashboard_keeps_explanation_visible_at_minimum_window_content(
    qt_app: QApplication,
) -> None:
    device = SimpleNamespace(
        device_id="rtl-01",
        display_name="RTL-SDR",
        kind="rtlsdr",
        state="streaming",
        health="healthy",
    )
    frame = SimpleNamespace(
        source_id="rtl-01",
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=2_000_000,
        power_dbm=[-90.0, -68.0, -88.0],
        dropped_frames=0,
        data_age_ms=12,
    )
    snapshot = _snapshot(
        devices=(device,),
        spectrum=frame,
        assessment=_assessment(),
    )
    page = DashboardPage(_Runtime(snapshot))
    # MainWindow minimum is 1120x720; navigation/header/footer leave roughly
    # this much space for the active page.
    page.resize(1008, 628)
    page.refresh(snapshot)
    page.show()
    qt_app.processEvents()

    assert page.spectrum_panel.isHidden()
    assert page.devices_panel.isHidden()
    assert page.incidents_panel.isHidden()
    assert page.profile_metric.isHidden()
    assert page.next_action_metric.isHidden()
    for label in (
        page.assessment_reasons,
        page.assessment_trust,
        page.assessment_attribution,
        page.assessment_action,
    ):
        assert label.isVisible()
        assert label.height() >= label.fontMetrics().height()
        bottom_in_card = label.mapTo(page.guided_panel, label.rect().bottomLeft()).y()
        assert bottom_in_card < page.guided_panel.height()
    page.close()


@pytest.mark.ui
def test_events_show_temporal_chain_and_separate_quality_from_evidence(
    qt_app: QApplication,
) -> None:
    guided_snapshot = _snapshot(
        assessment=_assessment(),
        decision=_decision(
            "candidate",
            episode_id="rf-episode-candidate",
            alertable=False,
        ),
        events=(_decision("suppressed", episode_id="rf-episode-suppressed"),),
    )
    runtime = _Runtime(guided_snapshot)
    page = SignalEventsPage(runtime)
    page.refresh(guided_snapshot)
    qt_app.processEvents()

    assert page.table.rowCount() == 2
    assert _header_text(page, 1) == "Статус"
    assert _header_text(page, 4) == "Качество данных"
    assert _header_text(page, 5) == "Сила признаков"
    assert page.table.isColumnHidden(3)
    assert not page.table.isColumnHidden(4)
    assert not page.table.isColumnHidden(5)
    assert "проверяется" in _cell_text(page, 0, 1).lower()
    assert _cell_text(page, 0, 4) == "Среднее"
    assert _cell_text(page, 0, 5) == "Высокая"
    assert "без оповещения" in _cell_text(page, 0, 6).lower()
    guided_cells = " ".join(
        _cell_text(page, 0, column)
        for column in range(page.table.columnCount())
    )
    assert "%" not in guided_cells
    assert "это дрон" not in guided_cells.lower()
    assert "это рация" not in guided_cells.lower()
    guided_detail = page.detail.text().lower()
    assert "за:" in guided_detail
    assert "против:" in guided_detail
    assert "не хватает:" in guided_detail
    assert "вклад сенсоров:" in guided_detail
    assert "альтернативные объяснения:" in guided_detail
    assert "федингом несущей" in guided_detail
    assert "ограничения:" in guided_detail
    assert "не устанавливает класс объекта" in guided_detail
    assert "качество данных:" in guided_detail
    assert "сила rf-признаков:" in guided_detail

    expert_snapshot = _snapshot(
        experience="expert",
        assessment=_assessment(),
        events=(_decision(),),
    )
    page.refresh(expert_snapshot)
    qt_app.processEvents()

    assert _header_text(page, 5) == "Сила признаков / эвристика"
    assert not page.table.isColumnHidden(3)
    assert "эвристика 0.83" in _cell_text(page, 0, 5).lower()
    assert "не вероятность" in _cell_text(page, 0, 5).lower()
    assert "%" not in _cell_text(page, 0, 5)
    expert_detail = page.detail.text().lower()
    assert "эпизод: rf-episode-0001" in expert_detail
    assert "калиброванная вероятность недоступна" in expert_detail
    assert "основной rf-поток" in expert_detail
    page.close()


@pytest.mark.ui
def test_spectrum_guided_hides_expert_controls_and_explains_listening_window(
    qt_app: QApplication,
) -> None:
    frame = SimpleNamespace(
        source_id="rtl-01",
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=2_000_000,
        power_dbm=[-90.0, -68.0, -88.0],
        peak_dbm=-68.0,
        dropped_frames=0,
        data_age_ms=12,
        unit="dBFS",
    )
    guided_snapshot = _snapshot(
        spectrum=frame,
        assessment=_assessment(),
    )
    runtime = _Runtime(guided_snapshot)
    page = SpectrumPage(runtime)
    page.refresh(guided_snapshot)
    qt_app.processEvents()

    assert not page.guided_summary.isHidden()
    assert page.controls.isHidden()
    assert page.inspector.isHidden()
    assert page.technical_status.isHidden()
    assert "432.920 МГц" in page.guided_coverage.text()
    assert (
        "Можно ли установить физический источник? Нет."
        in page.guided_attribution.text()
    )
    assert "класс объекта" in page.guided_attribution.text()
    assert "дрон" not in page.guided_attribution.text().lower()
    assert "раци" not in page.guided_attribution.text().lower()

    expert_snapshot = _snapshot(
        experience="expert",
        spectrum=frame,
        assessment=_assessment(),
    )
    page.refresh(expert_snapshot)
    qt_app.processEvents()

    assert page.guided_summary.isHidden()
    assert not page.controls.isHidden()
    assert not page.inspector.isHidden()
    assert not page.technical_status.isHidden()
    page.close()


@pytest.mark.ui
def test_active_spectrum_and_events_neutralize_legacy_identity_claims(
    qt_app: QApplication,
) -> None:
    decision = _decision()
    decision.family_explanation_ru = "Это дрон, точно распознано."
    decision.supporting_evidence = (
        SimpleNamespace(explanation_ru="Это рация."),
    )
    decision.alternatives = (
        SimpleNamespace(
            family="carrier",
            explanation_ru="БПЛА точно идентифицирован.",
        ),
    )
    decision.limitations = (
        SimpleNamespace(explanation_ru="Дрон обнаружен."),
    )
    snapshot = _snapshot(
        assessment=_assessment(),
        decision=decision,
        events=(decision,),
    )
    runtime = _Runtime(snapshot)

    spectrum = SpectrumPage(runtime)
    spectrum.refresh(snapshot)
    events = SignalEventsPage(runtime)
    events.refresh(snapshot)
    qt_app.processEvents()

    spectrum_text = " ".join(
        (
            spectrum.guided_headline.text(),
            spectrum.guided_explanation.text(),
            spectrum.guided_trust.text(),
            spectrum.guided_context.text(),
            spectrum.guided_attribution.text(),
            spectrum.guided_action.text(),
        )
    ).casefold()
    event_text = " ".join(
        (
            *(
                _cell_text(events, 0, column)
                for column in range(events.table.columnCount())
            ),
            events.detail.text(),
        )
    ).casefold()

    for rendered in (spectrum_text, event_text):
        for forbidden in (
            "это дрон",
            "это рация",
            "дрон обнаружен",
            "бпла",
            "точно распозн",
            "точно идентифиц",
        ):
            assert forbidden not in rendered
        assert "класс объекта" in rendered
        assert "не устанавливается" in rendered

    assert "альтернатива:" in spectrum_text
    assert "ограничение:" in spectrum_text
    assert "альтернативные объяснения:" in event_text
    assert "ограничения:" in event_text
    spectrum.close()
    events.close()
