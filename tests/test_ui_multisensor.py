from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from alga_vector.ui.multisensor_presenter import present_multisensor
from alga_vector.ui.pages.dashboard import DashboardPage
from alga_vector.ui.runtime import unavailable_snapshot


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return (
        app
        if isinstance(app, QApplication)
        else QApplication(["alga-vector-multisensor-test"])
    )


def _demo_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        mode="demo",
        runtime_mode="demo",
        devices=(SimpleNamespace(state="streaming"),),
        spectrum=SimpleNamespace(),
        signal_assessment=SimpleNamespace(state="concentrated_rf"),
        acoustic=SimpleNamespace(
            assessment=SimpleNamespace(
                lifecycle="confirmed",
                data_quality="simulated",
                alertable=True,
            )
        ),
        airspace=SimpleNamespace(
            summary=SimpleNamespace(
                state="current",
                data_quality="simulated",
                active_count=1,
            )
        ),
        direction=SimpleNamespace(
            available=True,
            stale=False,
            current=SimpleNamespace(source="simulated"),
        ),
        fusion_decision=SimpleNamespace(
            classification="multi_sensor_correlated",
            lifecycle="confirmed",
            active_modalities=("rf", "acoustic"),
            evidence_strength="high",
            missing=(),
            alertable=True,
        ),
    )


def test_dashboard_renders_demo_correlation_and_four_sensor_states(
    qt_app: QApplication,
) -> None:
    view = present_multisensor(_demo_snapshot())
    page = DashboardPage()

    page._refresh_multisensor(view)
    qt_app.processEvents()

    assert view.present
    assert not page.fusion_panel.isHidden()
    assert "Согласованное многосенсорное наблюдение" in page.fusion_headline.text()
    assert "СИНТЕТИЧЕСКИЕ ДАННЫЕ" in page.fusion_panel.subtitle_label.text()
    assert set(page.sensor_status_tiles) == {
        "rf",
        "acoustic",
        "direction",
        "civil_adsb",
    }
    rendered = " ".join(
        (
            page.fusion_headline.text(),
            page.fusion_summary.text(),
            page.fusion_correlation.text(),
            page.fusion_safety.text(),
        )
    ).lower()
    assert "дрон обнаружен" not in rendered
    assert "свой-чужой" not in rendered
    assert "координат" not in rendered


def test_unavailable_runtime_keeps_fail_closed_guidance_visible(
    qt_app: QApplication,
) -> None:
    page = DashboardPage()

    page.refresh(unavailable_snapshot(RuntimeError("runtime offline")))
    qt_app.processEvents()

    assert page.fusion_panel.isHidden()
    assert not page.guided_panel.isHidden()
    assert page.assessment_headline.text() == "Ошибка чтения состояния"
