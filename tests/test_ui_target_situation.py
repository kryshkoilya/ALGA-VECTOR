from __future__ import annotations

# ruff: noqa: RUF001
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from alga_vector.ui.pages.simple_situation import SimpleSituationPage

NOW = datetime(2026, 7, 27, 9, 41, 13, tzinfo=UTC)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return (
        app
        if isinstance(app, QApplication)
        else QApplication(["alga-vector-target-situation-test"])
    )


def _event(event_id: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        event_type="LIKELY_VIDEO_LINK",
        operator_label=title,
        operator_explanation="Устойчивый сигнал требует независимой проверки.",
        severity="warning",
        important=True,
        observed_at=datetime(2026, 7, 27, 9, 41, 12, tzinfo=UTC),
    )


@pytest.mark.ui
def test_simple_mode_prefers_future_current_target_and_verbal_stage(
    qt_app: QApplication,
) -> None:
    target = SimpleNamespace(
        target_id="T-024",
        lifecycle="active",
        active=True,
        probable_type="LIKELY_VIDEO_LINK",
        confirmation_stage="probable_source",
        short_operator_summary=(
            "Повторяемый видеоподобный сигнал; физический объект не установлен."
        ),
        sensors_used=("RTL-SDR", "Акустика"),
        last_seen=datetime(2026, 7, 27, 9, 41, 12, tzinfo=UTC),
        direction=SimpleNamespace(
            available=True,
            validated_external=True,
            associated_target_id="T-024",
            bearing_deg=108.0,
            uncertainty_deg=12.0,
            sector_text_ru="Сектор 96–120° · азимут 108°",
            explanation_ru=(
                "Свежий внешний пеленг; дальность по нему не определяется."
            ),
            source_name="KrakenSDR",
            observed_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(seconds=2),
        ),
        recommendation=SimpleNamespace(
            recommended_action_short="Проверьте сектор камерой.",
            recommended_action_detailed=(
                "Не классифицируйте объект только по радиоканалу."
            ),
        ),
        recent_events=(_event("event-1", "Вероятный видеоканал"),),
    )
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        captured_at=NOW,
        operator_situation=None,
        current_target=target,
        sensor_readiness=SimpleNamespace(
            sensors=(
                SimpleNamespace(
                    role="tinysa",
                    level="ready",
                    reason_ru="Триггер активен",
                ),
                SimpleNamespace(
                    role="rtl_sdr",
                    level="ready",
                    reason_ru="Поток стабилен",
                ),
                SimpleNamespace(
                    role="kraken_sdr",
                    level="limited",
                    reason_ru="Калибровка скоро устареет",
                ),
                SimpleNamespace(
                    role="acoustic",
                    level="unavailable",
                    reason_ru="Микрофон не настроен",
                ),
                SimpleNamespace(
                    role="adsb",
                    level="ready",
                    reason_ru="Поток свежий",
                ),
                SimpleNamespace(
                    role="passive_radar",
                    level="unavailable",
                    reason_ru="Источник не подключён",
                ),
                SimpleNamespace(
                    role="fusion",
                    level="limited",
                    reason_ru="Недостаточно независимых сенсоров",
                ),
            )
        ),
    )
    page = SimpleSituationPage()

    page.refresh(snapshot)
    qt_app.processEvents()

    assert page.mode_label.text() == "АКТИВНОСТЬ"
    assert page.target_card.type_label.text() == "Вероятный видеоканал"
    assert page.target_card.stage_badge.text() == "ВЕРОЯТНЫЙ ИСТОЧНИК"
    assert page.target_card.id_label.text() == "ID · T-024"
    assert page.direction_value.text() == "Сектор 96–120° · азимут 108°"
    assert "KrakenSDR" in page.sector_view.toolTip()
    assert page.recommendation_value.text() == "Проверьте сектор камерой."
    assert page.events_list.count() == 1
    assert "Вероятный видеоканал" in page.events_list.item(0).text()
    assert page.sensor_strip.tiles["tinysa"].state_label.text() == "ГОТОВ"
    assert page.sensor_strip.tiles["krakensdr"].state_label.text() == "ОГРАНИЧЕН"
    assert page.sensor_strip.tiles["acoustic"].state_label.text() == "НЕДОСТУПЕН"
    assert page.findChild(QWidget, "currentTargetCard") is page.target_card
    assert page.findChild(QWidget, "compactSectorView") is page.sector_view
    assert page.findChild(QWidget, "sensorReadinessStrip") is page.sensor_strip
    assert (
        page.findChild(QWidget, "sensorStatus_krakensdr")
        is page.sensor_strip.tiles["krakensdr"]
    )

    visible_text = " ".join(
        label.text()
        for label in page.findChildren(QLabel)
        if label.isVisible()
    )
    assert "%" not in visible_text
    page.close()


@pytest.mark.ui
def test_simple_mode_keeps_low_confidence_rf_activity_visible(
    qt_app: QApplication,
) -> None:
    event = SimpleNamespace(
        event_id="rf-low-1",
        event_type="RADIO_ACTIVITY_DETECTED",
        operator_label="Обнаружена RF-активность в наблюдаемом окне",
        operator_explanation=(
            "Энергетический всплеск повторился, но тип физического источника "
            "не подтверждён."
        ),
        severity="info",
        important=False,
        observed_at=NOW - timedelta(seconds=1),
    )
    situation = SimpleNamespace(
        mode="activity",
        headline_ru="Обнаружена RF-активность",
        explanation_ru=(
            "Низкая уверенность: это неподтверждённый RF-источник, а не "
            "идентифицированный БПЛА."
        ),
        primary_event=event,
        recent_events=(event,),
        recommendation=SimpleNamespace(
            recommended_action_short="Продолжайте наблюдение.",
            recommended_action_detailed="Дождитесь повторения или второго сенсора.",
        ),
    )
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        captured_at=NOW,
        operator_situation=situation,
        current_target=None,
        targets=(),
        sensor_readiness=None,
    )
    page = SimpleSituationPage()

    page.refresh(snapshot)
    qt_app.processEvents()

    assert page.mode_label.text() == "АКТИВНОСТЬ"
    assert page.headline.text() == "Обнаружена RF-активность"
    assert "Низкая уверенность" in page.explanation.text()
    assert page.target_card.type_label.text() == "Неподтверждённый RF-источник"
    assert page.target_card.stage_badge.text() == "ПОДОЗРИТЕЛЬНАЯ АКТИВНОСТЬ"
    assert page.events_list.count() == 1
    assert "RF-активность" in page.events_list.item(0).text()
    visible = " ".join(
        label.text()
        for label in page.findChildren(QLabel)
        if label.isVisible()
    ).lower()
    assert "точно бпла" not in visible
    page.close()


@pytest.mark.ui
def test_simple_mode_never_renders_unvalidated_range_or_position(
    qt_app: QApplication,
) -> None:
    target = SimpleNamespace(
        target_id="T-025",
        lifecycle="active",
        probable_type_ru="Неподтверждённый источник",
        confirmation_stage="suspicious_activity",
        short_operator_summary="Есть активность, но направления пока нет.",
        direction=None,
        distance_km=2.4,
        latitude=50.4501,
        longitude=30.5234,
        recommended_action_short="Дождитесь независимого подтверждения.",
        recent_events=(),
    )
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        captured_at=NOW,
        operator_situation=None,
        current_target=None,
        targets=(target,),
        readiness={},
    )
    page = SimpleSituationPage()

    page.refresh(snapshot)
    qt_app.processEvents()

    assert page.target_card.stage_badge.text() == "ПОДОЗРИТЕЛЬНАЯ АКТИВНОСТЬ"
    assert page.direction_value.text() == "Пеленгация недоступна"
    assert "валидного азимута" in page.direction_detail.text()
    assert page.recommendation_value.text() == (
        "Дождитесь независимого подтверждения."
    )

    visible_text = " ".join(
        label.text()
        for label in page.findChildren(QLabel)
        if label.isVisible()
    )
    assert "2.4" not in visible_text
    assert "50.4501" not in visible_text
    assert "30.5234" not in visible_text
    assert "%" not in visible_text
    page.close()


@pytest.mark.ui
def test_simple_mode_hides_an_unvalidated_bearing(
    qt_app: QApplication,
) -> None:
    target = SimpleNamespace(
        target_id="T-026",
        lifecycle="active",
        active=True,
        confirmation_stage="suspicious_activity",
        operator_label="Неподтверждённая активность",
        operator_explanation="Источник ещё не подтверждён.",
        direction=SimpleNamespace(
            available=True,
            validated_external=False,
            associated_target_id="T-026",
            bearing_deg=222.0,
            uncertainty_deg=2.0,
            observed_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(seconds=2),
        ),
        recommended_action_short="Дождитесь валидированного пеленга.",
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=None,
            current_target=target,
        )
    )
    qt_app.processEvents()

    assert page.direction_value.text() == "Пеленгация недоступна"
    assert "222" not in page.direction_detail.text()
    page.close()


@pytest.mark.ui
def test_current_target_drives_the_hero_instead_of_a_lower_level_event(
    qt_app: QApplication,
) -> None:
    target = SimpleNamespace(
        target_id="T-027",
        lifecycle="active",
        active=True,
        confirmation_stage="likely_source",
        operator_label="Согласованная активность нескольких сенсоров",
        operator_explanation=(
            "RF- и акустические наблюдения объединены в одну текущую цель."
        ),
        recommended_action_short="Проверьте источник независимым сенсором.",
    )
    situation = SimpleNamespace(
        mode="activity",
        headline_ru="Обнаружена отдельная RF-активность",
        explanation_ru="Низкоуровневое событие RF.",
        recommendation_ru="Продолжайте наблюдение.",
        recent_events=(),
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=situation,
            current_target=target,
        )
    )
    qt_app.processEvents()

    assert page.headline.text() == target.operator_label
    assert page.explanation.text() == target.operator_explanation
    assert "отдельная RF" not in page.headline.text()
    page.close()


@pytest.mark.ui
def test_sensor_strip_has_all_required_roles_and_actionable_tooltips(
    qt_app: QApplication,
) -> None:
    situation = SimpleNamespace(
        state="background",
        headline_ru="Фон стабилен",
        explanation_ru="Важной активности нет.",
        recommendation_ru="Продолжайте наблюдение.",
        sensors=(
            SimpleNamespace(
                sensor_id="rtl-primary",
                sensor_kind="rf_spectrum",
                availability="available",
                message_ru="Приёмник работает.",
            ),
            SimpleNamespace(
                sensor_id="df-primary",
                sensor_kind="direction_finder",
                availability="unavailable",
                message_ru="Пеленгатор не подключён.",
            ),
        ),
        recent_events=(),
    )
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        operator_situation=situation,
    )
    page = SimpleSituationPage()

    page.refresh(snapshot)
    qt_app.processEvents()

    assert tuple(page.sensor_strip.tiles) == (
        "tinysa",
        "rtlsdr",
        "krakensdr",
        "acoustic",
        "adsb",
        "passive_radar",
        "fusion",
    )
    assert page.sensor_strip.tiles["rtlsdr"].state_label.text() == "ГОТОВ"
    assert (
        page.sensor_strip.tiles["krakensdr"].state_label.text()
        == "НЕДОСТУПЕН"
    )
    assert "Влияние:" in page.sensor_strip.tiles["krakensdr"].toolTip()
    assert "Направление цели" in page.sensor_strip.tiles["krakensdr"].toolTip()
    page.close()


@pytest.mark.ui
@pytest.mark.parametrize("lifecycle", ("holding", "stale"))
def test_simple_mode_never_resurrects_a_noncurrent_target(
    qt_app: QApplication,
    lifecycle: str,
) -> None:
    target = SimpleNamespace(
        target_id=f"target-{lifecycle}",
        lifecycle=lifecycle,
        active=True,
        confirmation_stage="confirmed_target",
        operator_label="Просроченная подтверждённая цель",
        operator_explanation="Свежие признаки отсутствуют.",
        recommended_action_short="Не использовать как текущую цель.",
    )
    situation = SimpleNamespace(
        mode="background",
        headline_ru="Текущая цель не сформирована",
        explanation_ru="Система ожидает свежие наблюдения.",
        recommendation_ru="Продолжайте наблюдение.",
        primary_event=None,
        recent_events=(),
        sensors=(),
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=situation,
            current_target=target,
            targets=(target,),
        )
    )
    qt_app.processEvents()

    assert page.mode_label.text() == "ФОН"
    assert page.target_card.stage_badge.text() == "ФОН"
    assert page.target_card.type_label.text() == "Активная цель не сформирована"
    assert page.target_card.id_label.text() == ""
    assert "Просроченная" not in page.headline.text()
    page.close()


@pytest.mark.ui
def test_simple_direction_requires_a_validated_direction_on_current_target(
    qt_app: QApplication,
) -> None:
    global_direction = SimpleNamespace(
        available=True,
        validated_external=True,
        bearing_deg=108.0,
        uncertainty_deg=12.0,
        sector_text_ru="Сектор 96–120° · азимут 108°",
        explanation_ru="Глобальный пеленг другого эпизода.",
        source_id="kraken-global",
    )
    target = SimpleNamespace(
        target_id="target-without-direction",
        lifecycle="active",
        active=True,
        confirmation_stage="suspicious_activity",
        operator_label="Неподтверждённая RF-активность",
        operator_explanation="Цель не имеет связанного пеленга.",
        direction=None,
        sector_text_ru="Направление не определено",
        recommended_action_short="Дождитесь связанного пеленга.",
    )
    situation = SimpleNamespace(
        mode="activity",
        headline_ru="Получен несвязанный пеленг",
        explanation_ru="Направление относится к другому эпизоду.",
        recommendation_ru="Не связывайте пеленг с целью.",
        direction=global_direction,
        direction_ru=global_direction.sector_text_ru,
        primary_event=None,
        recent_events=(),
        sensors=(),
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=situation,
            current_target=target,
            targets=(target,),
        )
    )
    qt_app.processEvents()

    assert page.direction_value.text() == "Пеленгация недоступна"
    assert page.sector_view.canvas._state.available is False
    assert "108" not in page.direction_value.text()
    assert "другого эпизода" not in page.direction_detail.text()
    page.close()


@pytest.mark.ui
def test_standalone_direction_does_not_create_a_probable_target_card(
    qt_app: QApplication,
) -> None:
    direction = SimpleNamespace(
        available=True,
        validated_external=True,
        bearing_deg=108.0,
        uncertainty_deg=12.0,
        sector_text_ru="Сектор 96–120° · азимут 108°",
    )
    primary = SimpleNamespace(
        event_id="direction-only",
        event_type="DIRECTION_ESTIMATED",
        operator_label="Получен сектор направления",
        operator_explanation="Азимут не устанавливает тип источника.",
        severity="notice",
        important=True,
        observed_at=datetime(2026, 7, 27, 9, 41, 12, tzinfo=UTC),
    )
    situation = SimpleNamespace(
        mode="activity",
        headline_ru="Получен свежий азимут",
        explanation_ru="Азимут является только контекстом.",
        recommendation_ru="Дождитесь связанного события.",
        direction=direction,
        direction_ru=direction.sector_text_ru,
        primary_event=primary,
        recent_events=(primary,),
        sensors=(),
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=situation,
            current_target=None,
            targets=(),
        )
    )
    qt_app.processEvents()

    assert page.mode_label.text() == "ФОН"
    assert page.target_card.stage_badge.text() == "ФОН"
    assert page.target_card.type_label.text() == "Активная цель не сформирована"
    assert page.direction_value.text() == "Пеленгация недоступна"
    page.close()


@pytest.mark.ui
@pytest.mark.parametrize(
    ("case", "validity"),
    (
        ("missing-active-lifecycle", {}),
        (
            "expired",
            {
                "lifecycle": "active",
                "valid_until": NOW - timedelta(milliseconds=1),
            },
        ),
        (
            "malformed",
            {"lifecycle": "active", "valid_until": "not-a-timestamp"},
        ),
        (
            "naive",
            {
                "lifecycle": "active",
                "valid_until": datetime(2026, 7, 27, 10, 0),
            },
        ),
        (
            "future-observation",
            {
                "lifecycle": "active",
                "updated_at": NOW + timedelta(seconds=1),
            },
        ),
    ),
)
def test_simple_mode_rejects_noncurrent_duck_targets_and_old_actions(
    qt_app: QApplication,
    case: str,
    validity: dict[str, object],
) -> None:
    target = SimpleNamespace(
        target_id=f"legacy-{case}",
        active=True,
        confirmation_stage="confirmed_target",
        operator_label="Старая подтверждённая цель",
        operator_explanation="Данные не прошли проверку актуальности.",
        recommended_action_short="ВЫПОЛНИТЬ СТАРОЕ ДЕЙСТВИЕ",
        **validity,
    )
    situation = SimpleNamespace(
        mode="background",
        headline_ru="Текущая цель не сформирована",
        explanation_ru="Система ожидает свежую ACTIVE-цель.",
        recommendation_ru="Продолжайте наблюдение.",
        primary_event=None,
        recent_events=(),
        sensors=(),
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=situation,
            current_target=target,
            targets=(target,),
        )
    )
    qt_app.processEvents()

    assert page.mode_label.text() == "ФОН"
    assert page.target_card.id_label.text() == ""
    assert page.target_card.stage_badge.text() == "ФОН"
    assert page.recommendation_value.text() == "Продолжайте наблюдение."
    assert "СТАРОЕ ДЕЙСТВИЕ" not in page.recommendation_value.text()
    page.close()


@pytest.mark.ui
@pytest.mark.parametrize(
    ("case", "overrides"),
    (
        ("missing-observed", {"observed_at": None}),
        ("malformed-time", {"valid_until": "broken"}),
        (
            "naive-time",
            {"observed_at": datetime(2026, 7, 27, 9, 41, 12)},
        ),
        (
            "future-observation",
            {
                "observed_at": NOW + timedelta(seconds=1),
                "valid_until": NOW + timedelta(seconds=3),
            },
        ),
        (
            "expired",
            {"valid_until": NOW - timedelta(milliseconds=1)},
        ),
        (
            "wrong-target",
            {"associated_target_id": "another-target"},
        ),
    ),
)
def test_simple_mode_hides_unfresh_or_unassociated_direction(
    qt_app: QApplication,
    case: str,
    overrides: dict[str, object],
) -> None:
    direction_values: dict[str, object] = {
        "available": True,
        "validated_external": True,
        "associated_target_id": "direction-target",
        "bearing_deg": 271.0,
        "uncertainty_deg": 4.0,
        "sector_text_ru": "Сектор 267–275° · азимут 271°",
        "observed_at": NOW - timedelta(seconds=1),
        "valid_until": NOW + timedelta(seconds=2),
    }
    direction_values.update(overrides)
    target = SimpleNamespace(
        target_id="direction-target",
        lifecycle="active",
        active=True,
        confirmation_stage="suspicious_activity",
        operator_label=f"Проверка направления: {case}",
        operator_explanation="Направление должно пройти fail-closed проверку.",
        direction=SimpleNamespace(**direction_values),
        recommended_action_short="Дождитесь свежего связанного пеленга.",
    )
    page = SimpleSituationPage()

    page.refresh(
        SimpleNamespace(
            mode="live",
            runtime_mode="live",
            captured_at=NOW,
            operator_situation=None,
            current_target=target,
            targets=(target,),
        )
    )
    qt_app.processEvents()

    assert page.direction_value.text() == "Пеленгация недоступна"
    assert page.sector_view.canvas._state.available is False
    rendered = f"{page.direction_value.text()} {page.direction_detail.text()}"
    assert "271" not in rendered
    page.close()
