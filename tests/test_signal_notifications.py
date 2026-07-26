from __future__ import annotations

# ruff: noqa: RUF001
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from alga_vector.ui.main_window import MainWindow
from alga_vector.ui.signal_notifications import build_signal_notification
from alga_vector.ui.widgets import SignalAlertBanner


def _decision(
    lifecycle: str = "confirmed",
    *,
    episode_id: str | None = "rf-episode-0001",
    alertable: bool | None = None,
    family: str = "voice_like_compatible",
    bandwidth_hz: float = 12_500.0,
) -> object:
    return SimpleNamespace(
        lifecycle=lifecycle,
        episode_id=episode_id,
        alertable=(
            lifecycle in {"confirmed", "holding"}
            if alertable is None
            else alertable
        ),
        family=family,
        family_explanation_ru=(
            "Признаки совместимы с узкополосным каналом с изменяющейся "
            "огибающей; тип физического источника не подтверждён."
        ),
        source_id="rtl-01",
        peak_frequency_hz=145_500_000.0,
        occupied_bandwidth_hz=bandwidth_hz,
        heuristic_score=0.82,
        calibrated_probability=None,
        evidence_strength="high",
        data_quality="medium",
        supporting_evidence=(
            SimpleNamespace(explanation_ru="Изменение повторилось в трёх кадрах."),
        ),
        contradicting_evidence=(
            SimpleNamespace(explanation_ru="Независимого второго сенсора нет."),
        ),
        missing_confirmation=(
            SimpleNamespace(
                explanation_ru="Нужно независимое подтверждение источника."
            ),
        ),
        sensor_contributions=(
            SimpleNamespace(
                source_id="rtl-01",
                contribution=0.82,
                data_quality="medium",
                independent_confirmation=False,
                explanation_ru="Основной RF-поток.",
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


def _snapshot(decision: object | None) -> object:
    return SimpleNamespace(
        signal_decision=decision,
        signal_assessment=SimpleNamespace(state="concentrated_rf"),
    )


def _fusion_snapshot(*, mode: str = "live", lifecycle: str = "confirmed") -> object:
    return SimpleNamespace(
        mode=mode,
        runtime_mode=mode,
        signal_decision=_decision(),
        signal_assessment=SimpleNamespace(state="concentrated_rf"),
        fusion_decision=SimpleNamespace(
            classification="multi_sensor_correlated",
            lifecycle=lifecycle,
            episode_id="fusion-0001",
            alertable=True,
            observation_count=6,
            evidence_strength="high",
            active_modalities=("rf", "acoustic"),
            missing=(),
        ),
        acoustic=SimpleNamespace(),
        airspace=None,
    )


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication(["signal-alert-test"])


def test_only_alertable_temporal_decisions_raise_an_alert() -> None:
    assert build_signal_notification(_snapshot(None)).active is False
    for lifecycle in (
        "idle",
        "candidate",
        "suppressed",
        "data_hold",
        "resolved",
    ):
        notice = build_signal_notification(_snapshot(_decision(lifecycle)))
        assert notice.active is False

    malformed_candidate = _decision("candidate", alertable=True)
    assert build_signal_notification(_snapshot(malformed_candidate)).active is False


def test_confirmed_episode_uses_stable_id_and_compatibility_wording() -> None:
    notice = build_signal_notification(_snapshot(_decision()))
    rendered = " ".join(
        (notice.title, notice.message, notice.details, notice.next_action)
    ).lower()

    assert notice.active is True
    assert notice.key == "rf-episode-0001"
    assert notice.level == "warning"
    assert "совместимы с голосовой радиосвязью" in rendered
    assert "не подтвержд" in rendered
    assert "это рация" not in rendered
    assert "это дрон" not in rendered
    assert "дрон обнаружен" not in rendered
    assert "145.500 мгц" in rendered
    assert "12.5 кгц" in rendered
    assert "качество данных:" in rendered
    assert "сила rf-признаков:" in rendered
    assert "за:" in rendered
    assert "против:" in rendered
    assert "не хватает:" in rendered
    assert "вклад сенсоров:" in rendered
    assert "альтернатива:" in rendered
    assert "ограничение измерения:" in rendered
    assert "федингом несущей" in rendered
    assert "не устанавливает класс объекта" in rendered
    assert "не калиброванная вероятность" in rendered


@pytest.mark.parametrize(
    ("family", "expected_title", "expected_message"),
    (
        ("voice_like", "голосоподобный", "голосовая радиосвязь"),
        ("voice_like_compatible", "голосоподобный", "радиостанция"),
        ("packet_like", "пакетоподобный", "цифровыми пакетами"),
        ("digital_like", "пакетоподобный", "телеметрией"),
        ("carrier", "несущая", "аппаратный spur"),
        ("narrowband_burst", "узкополосный", "ограниченная во времени"),
        ("broadband_burst", "широкополосный", "рост энергии"),
        ("interference_noise_like", "помеха", "источник помехи"),
        ("unknown", "не классифицирован", "данных недостаточно"),
    ),
)
def test_confirmed_notification_distinguishes_generic_rf_families(
    family: str,
    expected_title: str,
    expected_message: str,
) -> None:
    notice = build_signal_notification(
        _snapshot(_decision(family=family))
    )
    rendered = " ".join(
        (notice.title, notice.message, notice.details, notice.next_action)
    ).casefold()

    assert notice.active is True
    assert notice.target_page == "events"
    assert expected_title in notice.title.casefold()
    assert expected_message in rendered
    assert "почему показано уведомление:" in rendered
    assert "одиночный кадр такого уведомления не создаёт" in rendered
    assert "не хватает:" in rendered
    assert "дрон" not in rendered
    assert "приближ" not in rendered
    assert "уровня угрозы" not in rendered


def test_missing_confirmation_is_explained_even_if_engine_list_is_empty() -> None:
    decision = _decision(family="carrier")
    decision.missing_confirmation = ()

    notice = build_signal_notification(_snapshot(decision))

    assert "Не хватает:" in notice.details
    assert "вторым приёмником" in notice.details
    assert "локальную аппаратную линию" in notice.details


def test_notification_neutralizes_legacy_identity_claims() -> None:
    decision = _decision()
    decision.family_explanation_ru = "Это дрон, точно распознано."
    decision.supporting_evidence = (
        SimpleNamespace(explanation_ru="Это рация."),
    )
    decision.alternatives = (
        SimpleNamespace(
            family="unknown",
            explanation_ru="БПЛА точно идентифицирован.",
        ),
    )
    decision.limitations = (
        SimpleNamespace(explanation_ru="Дрон обнаружен."),
    )

    notice = build_signal_notification(_snapshot(decision))
    rendered = " ".join(
        (notice.title, notice.message, notice.details, notice.next_action)
    ).casefold()

    assert notice.active is True
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


def test_holding_reuses_episode_key_without_new_frequency_bucket() -> None:
    confirmed = build_signal_notification(_snapshot(_decision("confirmed")))
    holding = build_signal_notification(
        _snapshot(
            _decision(
                "holding",
                bandwidth_hz=18_000.0,
            )
        )
    )

    assert holding.active is True
    assert holding.level == "info"
    assert holding.key == confirmed.key == "rf-episode-0001"
    assert "временно ослаб" in holding.title.lower()


def test_alertable_fusion_has_priority_and_explicit_live_provenance() -> None:
    notice = build_signal_notification(_fusion_snapshot())
    rendered = " ".join(
        (notice.title, notice.message, notice.details, notice.next_action)
    ).casefold()

    assert notice.active is True
    assert notice.key == "fusion:fusion-0001"
    assert notice.target_page == "dashboard"
    assert notice.level == "warning"
    assert "согласованное rf+акустическое наблюдение" in notice.title.casefold()
    assert "измеренные сенсорные данные" in rendered
    assert "независимые rf- и акустические признаки" in rendered
    assert "не идентификация" in rendered
    assert "дрон" not in rendered
    assert "угроз" not in rendered
    assert "приближ" not in rendered


def test_demo_fusion_is_prominently_marked_and_not_a_live_warning() -> None:
    notice = build_signal_notification(_fusion_snapshot(mode="demo"))
    rendered = " ".join(
        (notice.title, notice.message, notice.details)
    ).casefold()

    assert notice.active is True
    assert notice.level == "info"
    assert notice.title.startswith("ДЕМО ·")
    assert "синтетические данные демо-сценария" in rendered
    assert notice.target_page == "dashboard"


def test_non_alertable_fusion_does_not_hide_confirmed_rf_notification() -> None:
    snapshot = _fusion_snapshot(lifecycle="candidate")
    snapshot.fusion_decision.alertable = False

    notice = build_signal_notification(snapshot)

    assert notice.active is True
    assert notice.key == "rf-episode-0001"
    assert notice.target_page == "events"


def test_normalized_operator_event_drives_the_global_banner() -> None:
    event = SimpleNamespace(
        event_id="normalized-001",
        event_type="MULTISENSOR_CORRELATED",
        severity="warning",
        summary_ru="Несколько сенсоров видят согласованную активность",
        explanation_ru=(
            "RF- и акустические наблюдения совпали по времени; тип "
            "физического источника не установлен."
        ),
        recommendation_ru="Откройте простую обстановку и проверьте сенсоры.",
        confidence=SimpleNamespace(
            basis_ru="Сила временной корреляции; не вероятность объекта."
        ),
        sources=(
            SimpleNamespace(sensor_id="rtl-01"),
            SimpleNamespace(sensor_id="acoustic-01"),
        ),
        limitations=(
            "Корреляция общих аномалий не идентифицирует БПЛА.",
        ),
    )
    snapshot = SimpleNamespace(
        mode="live",
        runtime_mode="live",
        operator_situation=SimpleNamespace(primary_event=event),
        signal_decision=None,
        fusion_decision=None,
    )

    notice = build_signal_notification(snapshot)
    rendered = " ".join(
        (notice.title, notice.message, notice.details, notice.next_action)
    ).casefold()

    assert notice.active
    assert notice.key == "normalized:normalized-001"
    assert notice.target_page == "situation"
    assert notice.level == "warning"
    assert "rtl-01" in rendered
    assert "не вероятность" in rendered
    assert "не установлен" in rendered
    assert "расстояние" in rendered


def test_normalized_contract_prevents_legacy_banner_resurrection() -> None:
    snapshot = _fusion_snapshot()
    snapshot.operator_situation = SimpleNamespace(primary_event=None)

    notice = build_signal_notification(snapshot)

    assert notice.active is False


@pytest.mark.ui
def test_signal_alert_banner_is_compact_and_opens_events(
    qt_app: QApplication,
) -> None:
    banner = SignalAlertBanner()
    opened: list[bool] = []
    banner.open_requested.connect(lambda: opened.append(True))
    banner.set_alert(
        "Возможный узкополосный канал связи",
        "Тип передатчика не подтверждён.",
        level="info",
        details="Измеренные признаки и ограничение.",
    )
    banner.show()
    qt_app.processEvents()

    assert banner.height() == 46
    assert "узкополосный" in banner.title_label.text().lower()
    assert "не подтверждён" in banner.message_label.text().lower()
    assert "ограничение" in banner.toolTip().lower()
    banner.open_button.click()
    assert opened == [True]
    banner.close()


@pytest.mark.ui
def test_main_window_routes_notification_to_its_evidence_page(
    qt_app: QApplication,
) -> None:
    window = MainWindow()
    window._render_snapshot(_fusion_snapshot())
    window.navigate("events")
    window._render_snapshot(_fusion_snapshot())
    window.signal_alert.open_button.click()
    assert window.current_page_key == "dashboard"

    window._render_snapshot(_snapshot(_decision()))
    window.navigate("dashboard")
    window._render_snapshot(_snapshot(_decision()))
    window.signal_alert.open_button.click()
    assert window.current_page_key == "events"
    window.close()
