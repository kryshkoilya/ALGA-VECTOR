from __future__ import annotations

# ruff: noqa: RUF001
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from alga_vector.ui.pages.simple_situation import SimpleSituationPage


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return (
        app
        if isinstance(app, QApplication)
        else QApplication(["alga-vector-simple-situation-test"])
    )


def _snapshot(situation: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        mode="live",
        runtime_mode="live",
        operator_situation=situation,
    )


def _event(
    event_type: str,
    title: str,
    *,
    severity: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        headline_ru=title,
        explanation_ru=f"Объяснение: {title.lower()}.",
        severity=severity,
        observed_at=datetime(2026, 7, 26, 12, 34, 56, tzinfo=UTC),
    )


@pytest.mark.ui
def test_simple_page_renders_interpreted_activity_and_filters_events(
    qt_app: QApplication,
) -> None:
    situation = SimpleNamespace(
        state="activity",
        headline_ru="Обнаружена активность в диапазоне 5.8 ГГц",
        explanation_ru=(
            "Форма сигнала похожа на видеоканал; тип физического источника "
            "ещё не подтверждён."
        ),
        direction=SimpleNamespace(
            available=True,
            sector_text_ru="Сектор 95–120°",
            explanation_ru="Сектор передан валидированным внешним DF-сенсором.",
        ),
        confidence=SimpleNamespace(
            level="high",
            explanation_ru=(
                "Высокая сила наблюдаемых признаков; это не вероятность типа объекта."
            ),
        ),
        recommendation=SimpleNamespace(
            action_ru="Нужно подтверждение по камере.",
            explanation_ru="Не делайте вывод о типе объекта только по RF.",
        ),
        sensor_availability=(
            SimpleNamespace(
                name="KrakenSDR",
                available=True,
            ),
        ),
        recent_events=(
            _event(
                "LIKELY_VIDEO_LINK",
                "Вероятный видеоканал",
                severity="warning",
            ),
            _event(
                "NOISE_BACKGROUND",
                "Фон без заметных изменений",
                severity="info",
            ),
        ),
    )
    page = SimpleSituationPage()

    page.refresh(_snapshot(situation))
    qt_app.processEvents()

    assert page.mode_label.text() == "АКТИВНОСТЬ"
    assert page.headline.text() == "Обнаружена активность в диапазоне 5.8 ГГц"
    assert page.direction_value.text() == "Сектор 95–120°"
    assert page.confidence_value.text() == "Высокая"
    assert page.recommendation_value.text() == "Нужно подтверждение по камере."
    assert page.sensor_notice.isHidden()
    assert page.events_list.count() == 1
    assert "Вероятный видеоканал" in page.events_list.item(0).text()

    page.important_only.setChecked(False)
    qt_app.processEvents()
    assert page.events_list.count() == 2
    assert "Фон без заметных изменений" in page.events_list.item(1).text()
    page.close()


@pytest.mark.ui
def test_simple_page_explains_missing_direction_sensor(
    qt_app: QApplication,
) -> None:
    situation = SimpleNamespace(
        mode="background",
        headline_ru="Фон стабилен",
        explanation_ru="Заметной активности не найдено.",
        direction=SimpleNamespace(available=False),
        evidence_strength="medium",
        recommendation_ru="Продолжайте наблюдение.",
        sensor_availability=(
            SimpleNamespace(
                display_name="KrakenSDR",
                available=False,
                reason_ru="KrakenSDR не подключён",
            ),
        ),
        events=(),
    )
    page = SimpleSituationPage()

    page.refresh(_snapshot(situation))
    qt_app.processEvents()

    assert page.mode_label.text() == "ФОН"
    assert page.direction_value.text() == "Пеленгация недоступна"
    assert "KrakenSDR не подключён" in page.direction_detail.text()
    assert not page.sensor_notice.isHidden()
    assert "KrakenSDR не подключён" in page.sensor_notice.text_label.text()
    assert page.events_list.item(0).text() == "Событий пока нет."
    page.close()


@pytest.mark.ui
def test_simple_page_consumes_production_schema_without_raw_fallback(
    qt_app: QApplication,
) -> None:
    from alga_vector.signal_processor.schema import (
        ConfidenceScore,
        EventSeverity,
        OperatorSituation,
        OperatorSituationMode,
        SensorAvailability,
        SensorKind,
        SensorState,
    )

    now = datetime(2026, 7, 26, 12, 34, 56, tzinfo=UTC)
    situation = OperatorSituation(
        generated_at=now,
        mode=OperatorSituationMode.SILENCE,
        headline_ru="Наблюдение ограничено",
        explanation_ru="RF-приёмник недоступен.",
        severity=EventSeverity.WARNING,
        confidence=ConfidenceScore.unavailable(
            "Нет классифицируемых признаков."
        ),
        direction_ru="Пеленгация недоступна: внешний пеленгатор не подключён.",
        direction=None,
        recommendation_ru="Проверьте страницу устройств.",
        primary_event=None,
        recent_events=(),
        sensors=(
            SensorState(
                sensor_id="df-primary",
                sensor_kind=SensorKind.DIRECTION_FINDER,
                availability=SensorAvailability.UNAVAILABLE,
                message_ru="Внешний пеленгатор не подключён.",
                checked_at=now,
            ),
        ),
    )
    page = SimpleSituationPage()

    page.refresh(_snapshot(situation))
    qt_app.processEvents()

    assert page.mode_label.text() == "ТИШИНА"
    assert page.header.status.property("statusLevel") == "warning"
    assert page.direction_value.text() == "Пеленгация недоступна"
    assert "внешний пеленгатор не подключён" in page.direction_detail.text()
    assert page.confidence_value.text() == "Не рассчитана"
    assert page.confidence_detail.text() == "Нет классифицируемых признаков."
    assert "Внешний пеленгатор не подключён" in page.sensor_notice.text_label.text()
    page.close()


@pytest.mark.ui
def test_simple_page_does_not_reconstruct_situation_from_raw_fields(
    qt_app: QApplication,
) -> None:
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        operator_situation=None,
        signal_decision=SimpleNamespace(
            family="legacy-drone-claim",
            distance_km=3.2,
        ),
        direction=SimpleNamespace(
            available=True,
            bearing_deg=117.0,
        ),
        spectrum=SimpleNamespace(
            peak_frequency_hz=5_800_000_000,
        ),
    )
    page = SimpleSituationPage()

    page.refresh(snapshot)
    qt_app.processEvents()

    rendered = " ".join(
        (
            page.headline.text(),
            page.explanation.text(),
            page.direction_value.text(),
            page.direction_detail.text(),
            page.confidence_value.text(),
            page.recommendation_value.text(),
        )
    ).casefold()
    assert page.mode_label.text() == "НЕТ ДАННЫХ"
    assert "данные обстановки недоступны" in rendered
    assert "117" not in rendered
    assert "3.2" not in rendered
    assert "5.8" not in rendered
    assert "дрон" not in rendered
    page.close()
