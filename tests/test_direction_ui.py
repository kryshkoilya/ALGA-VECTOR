from __future__ import annotations

# ruff: noqa: RUF001
import os
from datetime import UTC, datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from alga_vector.direction import (
    DirectionService,
    DirectionSnapshot,
    ExternalDirectionEvidence,
)
from alga_vector.ui.direction_presenter import RANGE_LIMITATION_RU
from alga_vector.ui.pages.direction import DirectionPage
from alga_vector.ui.widgets.direction_plot import DirectionPlot


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _external_snapshot(now: datetime) -> DirectionSnapshot:
    service = DirectionService(clock=lambda: now)
    return service.ingest_external(
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


@pytest.mark.ui
def test_direction_plot_renders_honest_empty_state(
    qt_app: QApplication,
) -> None:
    plot = DirectionPlot()
    plot.resize(720, 560)
    plot.clear("Внешний датчик не подключён.")
    plot.show()
    qt_app.processEvents()

    image = plot.grab().toImage()

    assert not image.isNull()
    assert image.width() == 720
    assert image.height() == 560
    assert plot.current_observation is None
    assert plot.empty_reason == "Внешний датчик не подключён."
    plot.close()


@pytest.mark.ui
def test_direction_plot_renders_external_ray_cone_and_trail(
    qt_app: QApplication,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    snapshot = _external_snapshot(now)
    plot = DirectionPlot()
    plot.resize(720, 560)
    plot.set_snapshot(snapshot)
    plot.show()
    qt_app.processEvents()

    image = plot.grab().toImage()

    assert not image.isNull()
    assert plot.current_observation is snapshot.current
    assert plot.current_observation.bearing_deg == pytest.approx(127.5)
    assert plot.current_observation.uncertainty_deg == pytest.approx(6.5)
    plot.close()


class _DirectionRuntime:
    def __init__(self, now: datetime) -> None:
        self.service = DirectionService(clock=lambda: now)
        self.snapshot = SimpleNamespace(
            direction=self.service.snapshot(),
            runtime_mode="live",
            mode="live",
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def set_manual_direction(
        self,
        bearing_deg: float,
        uncertainty_deg: float,
    ) -> DirectionSnapshot:
        result = self.service.set_manual(
            bearing_deg,
            uncertainty_deg=uncertainty_deg,
        )
        self.snapshot.direction = result
        return result

    def clear_direction(self) -> DirectionSnapshot:
        result = self.service.clear()
        self.snapshot.direction = result
        return result


@pytest.mark.ui
def test_direction_page_manual_workflow_never_claims_measurement(
    qt_app: QApplication,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    runtime = _DirectionRuntime(now)
    page = DirectionPage(runtime)
    page.resize(1100, 700)
    page.refresh()
    page.bearing_input.setValue(222.2)
    page.uncertainty_input.setValue(18.0)
    page.apply_manual_button.click()
    page.show()
    qt_app.processEvents()

    current = page.plot.current_observation
    assert current is not None
    assert current.operator_entered
    assert not current.measured
    assert current.confidence is None
    assert page.header.status.text() == "ВВОД ОПЕРАТОРА · НЕ ИЗМЕРЕНИЕ"
    assert "не измерено" in page.source_notice.text_label.text().lower()
    assert "Уверенность: не измерялась" in page.detail.text()
    assert page.bearing_metric.value_label.text() == "222.2° · ВВОД"

    visible_text = " ".join(label.text().lower() for label in page.findChildren(QLabel))
    assert " км" not in visible_text
    assert "широта" not in visible_text
    assert "долгота" not in visible_text
    page.close()


@pytest.mark.ui
def test_direction_page_clears_active_ray(
    qt_app: QApplication,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    runtime = _DirectionRuntime(now)
    runtime.set_manual_direction(30.0, 10.0)
    page = DirectionPage(runtime)
    page.refresh()
    assert page.plot.current_observation is not None
    assert page.plot.current_observation.available

    page.clear_button.click()
    qt_app.processEvents()

    assert page.plot.current_observation is not None
    assert not page.plot.current_observation.available
    assert page.bearing_metric.value_label.text() == "—"
    assert page.header.status.text() == "НАПРАВЛЕНИЕ НЕДОСТУПНО"
    page.close()


@pytest.mark.ui
def test_direction_page_shows_measured_rf_trend_without_range_claim(
    qt_app: QApplication,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    runtime = _DirectionRuntime(now)
    page = DirectionPage(runtime)

    for sequence, peak in enumerate((-90.0, -85.0, -80.0), start=1):
        runtime.snapshot.spectrum = SimpleNamespace(
            source_id="receiver-a",
            sequence=sequence,
            captured_at=datetime(
                2026,
                7,
                26,
                10,
                0,
                sequence,
                tzinfo=UTC,
            ),
            center_frequency_hz=433_920_000,
            span_hz=2_000_000,
            power_dbm=(-100.0, peak, -98.0),
            unit="dBFS",
        )
        runtime.snapshot.signal_assessment = SimpleNamespace(
            state="background"
        )
        page.refresh(runtime.snapshot)

    qt_app.processEvents()

    assert page.rf_trend_metric.value_label.text() == "РАСТЁТ"
    assert "измеренный тренд" in page.rf_trend_detail.text()
    assert RANGE_LIMITATION_RU in page.rf_trend_detail.text()
    assert RANGE_LIMITATION_RU in page.range_limitation_notice.text_label.text()
    assert " км" not in page.rf_trend_detail.text().lower()
    assert "приближ" not in page.rf_trend_detail.text().lower()
    page.close()


@pytest.mark.ui
def test_direction_page_marks_demo_bearing_as_simulated(
    qt_app: QApplication,
) -> None:
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    runtime = _DirectionRuntime(now)
    demo_service = DirectionService(demo_mode=True, clock=lambda: now)
    runtime.snapshot.direction = demo_service.set_simulated(42.0)
    runtime.snapshot.mode = "demo"
    runtime.snapshot.runtime_mode = "demo"
    page = DirectionPage(runtime)
    page.refresh(runtime.snapshot)
    qt_app.processEvents()

    assert page.bearing_metric.value_label.text() == "042.0° · ДЕМО"
    assert page.header.status.text() == "СИМУЛЯЦИЯ · НЕ ИЗМЕРЕНИЕ"
    assert "Demo" in page.source_notice.text_label.text()
    assert page.plot.current_observation is not None
    assert not page.plot.current_observation.measured
    page.close()
