
from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from alga_vector.ui.app import create_application
from alga_vector.ui.main_window import MainWindow
from alga_vector.ui.runtime import (
    UnavailableRuntimeSnapshot,
    current_snapshot,
    provenance_key,
    provenance_ru,
)
from alga_vector.ui.theme import APP_STYLE, BASE_FONT_PX, NAV_WIDTH
from alga_vector.ui.widgets import ProvenanceBanner


class FakeRuntime:
    def __init__(self) -> None:
        self.rescan_calls = 0
        self.reconnect_calls: list[str] = []
        self.shutdown_calls = 0
        self.tick_calls = 0
        self.snapshot = _snapshot()

    def current_snapshot(self) -> object:
        return self.snapshot

    def rescan(self) -> object:
        self.rescan_calls += 1
        return self.snapshot

    def tick(self) -> object:
        self.tick_calls += 1
        return self.snapshot

    def reconnect(self, device_id: str) -> object:
        self.reconnect_calls.append(device_id)
        return self.snapshot

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class BrokenSnapshotRuntime:
    def current_snapshot(self) -> object:
        raise RuntimeError("snapshot unavailable")


def _snapshot() -> object:
    devices = (
        SimpleNamespace(
            device_id="tinysa-01",
            display_name="Receiver 01",
            kind="tinysa",
            connection="SIM:TINYSA",
            state="ready",
            health="healthy",
            driver="deterministic",
            sample_rate_hz=2_400_000,
            center_frequency_hz=433_920_000,
            metrics={"temperature_c": 36.4, "sync": "Захвачено"},
        ),
        SimpleNamespace(
            device_id="kraken-01",
            display_name="Array DF (KrakenSDR)",
            kind="krakensdr",
            connection="192.168.1.100:8080",
            state="absent",
            health="error",
            driver="k-daq-v2",
            sample_rate_hz=None,
            center_frequency_hz=None,
            metrics={},
            reason_ru="Устройство не отвечает.",
            recommended_action_ru="Проверьте кабель или IP-адрес.",
        ),
    )
    incident = SimpleNamespace(
        incident_id="inc-1",
        code="DEVICE.ABSENT",
        title_ru="KrakenSDR: нет связи",
        message_ru="Пеленгация недоступна.",
        action_ru="Проверьте подключение.",
        severity="warning",
        source="kraken-01",
        occurred_at="14:12:01",
        acknowledged=False,
        technical={"state": "absent"},
    )
    spectrum = SimpleNamespace(
        source_id="tinysa-01",
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=5_000_000,
        power_dbm=[-94.0, -83.0, -47.0, -86.0, -93.0],
        peak_dbm=-47.0,
        dropped_frames=0,
        data_age_ms=12,
    )
    return SimpleNamespace(
        revision=7,
        devices=devices,
        capabilities=(),
        incidents=(incident,),
        spectrum=spectrum,
        mode="simulated",
        profile_name="Тестовый профиль",
        readiness_percent=82,
    )


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return create_application(["alga-vector-ui-test"])


@pytest.mark.ui
def test_main_window_builds_and_navigates_without_owning_runtime(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    window = MainWindow(runtime)
    window.show()
    qt_app.processEvents()

    navigation = window.findChild(QFrame, "navigation")
    assert navigation is not None
    assert navigation.width() == NAV_WIDTH == 112
    for key in (
        "dashboard",
        "devices",
        "spectrum",
        "events",
        "direction",
        "diagnostics",
        "settings",
    ):
        window.navigate(key)
        qt_app.processEvents()
        assert window.current_page_key == key
        assert window.page(key) is not None
    assert window.findChild(QPushButton, "nav_direction") is not None
    assert window.findChild(QPushButton, "nav_map") is None
    window.poll_runtime()
    assert runtime.tick_calls >= 1

    labels = [label.text() for label in window.findChildren(QLabel)]
    assert "Разработал: Буйвол и Задира" in labels
    assert any("ДЕМО" in label for label in labels)

    window.close()
    qt_app.processEvents()
    assert runtime.shutdown_calls == 0


def test_theme_enforces_approved_constraints() -> None:
    normalized = APP_STYLE.lower()
    assert BASE_FONT_PX >= 12
    assert "gradient" not in normalized
    assert "glow" not in normalized
    assert "purple" not in normalized
    assert "violet" not in normalized
    assert "fuchsia" not in normalized
    assert "font-size: 10px" not in normalized
    assert "font-size: 11px" not in normalized


@pytest.mark.ui
def test_missing_or_failed_snapshot_is_never_presented_as_demo(
    qt_app: QApplication,
) -> None:
    assert current_snapshot(None) is None

    snapshot = current_snapshot(BrokenSnapshotRuntime())
    assert isinstance(snapshot, UnavailableRuntimeSnapshot)
    assert snapshot.readiness_percent == 0
    assert snapshot.runtime_error.exception_type == "RuntimeError"
    assert snapshot.signal_assessment.state == "data_unreliable"
    incidents = snapshot.incidents
    assert len(incidents) == 1
    assert incidents[0].title_ru == "Ошибка чтения состояния"
    assert incidents[0].action_ru
    with pytest.raises(FrozenInstanceError):
        snapshot.readiness_percent = 100  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        incidents[0].severity = "info"  # type: ignore[misc]
    assert provenance_key(snapshot) == "unavailable"
    assert provenance_ru(snapshot) == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"

    banner = ProvenanceBanner()
    banner.refresh(snapshot)
    assert banner.label.text() == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    assert "RuntimeError: snapshot unavailable" in banner.toolTip()
    assert "ДЕМО" not in banner.label.text()
    banner.close()
    qt_app.processEvents()


@pytest.mark.ui
def test_snapshot_read_failure_is_visible_in_shell_and_pages(
    qt_app: QApplication,
) -> None:
    window = MainWindow(BrokenSnapshotRuntime())
    window.refresh_timer.stop()
    window.refresh_snapshot()

    assert window.system_status.text() == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    assert window.system_status.property("statusLevel") == "critical"
    assert "runtime.current_snapshot: RuntimeError: snapshot unavailable" in (
        window.system_status.toolTip()
    )
    assert window.global_provenance.label.text() == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    assert "RuntimeError: snapshot unavailable" in window.global_provenance.toolTip()

    dashboard = window.page("dashboard")
    assert dashboard.provenance.label.text() == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    assert dashboard.assessment_headline.text() == "Ошибка чтения состояния"
    assert dashboard.guided_next_button.text() == "Разобрать критический инцидент"

    window.navigate("diagnostics")
    diagnostics = window.page("diagnostics")
    assert diagnostics.provenance.label.text() == "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    assert diagnostics.header.status.text() == "1 КРИТИЧЕСКОЕ АКТИВНОЕ СОБЫТИЕ"
    assert diagnostics.title.text() == "Ошибка чтения состояния"
    assert "RuntimeError: snapshot unavailable" in diagnostics.technical.toPlainText()
    assert diagnostics.acknowledge.text() == "Служебное событие"
    assert not diagnostics.acknowledge.isEnabled()

    window.close()
    qt_app.processEvents()


def test_demo_provenance_requires_an_explicit_snapshot_mode() -> None:
    assert provenance_key(SimpleNamespace(runtime_mode="demo", mode="live")) == "demo"
    assert provenance_key(SimpleNamespace(mode="simulated")) == "simulated"
    assert provenance_key(SimpleNamespace(runtime_mode="live")) == "live"
    assert provenance_key(SimpleNamespace()) == "unknown"


@pytest.mark.ui
def test_global_status_prioritizes_active_failures_over_readiness(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.snapshot.readiness_percent = 100
    runtime.snapshot.signal_assessment = SimpleNamespace(
        state="data_unreliable",
        trust="low",
        source_id="tinysa-01",
        operator_action_ru="Проверьте поток.",
        evidence=SimpleNamespace(
            peak_frequency_hz=None,
            occupied_bandwidth_hz=None,
            persistence_frames=None,
        ),
    )
    runtime.snapshot.incidents = (
        SimpleNamespace(
            severity="critical",
            acknowledged=True,
        ),
    )
    window = MainWindow(runtime)
    window.refresh_snapshot()

    assert window.system_status.text() == "КРИТИЧЕСКИЙ ИНЦИДЕНТ"
    assert window.system_status.property("statusLevel") == "critical"

    runtime.snapshot.incidents = ()
    window.refresh_snapshot()
    assert window.system_status.text() == "КАЧЕСТВО ДАННЫХ СНИЖЕНО"
    assert window.system_status.property("statusLevel") == "critical"

    runtime.snapshot.signal_assessment.state = "background_only"
    window.refresh_snapshot()
    assert window.system_status.text() == "RF-ЯДРО ГОТОВО"
    assert window.system_status.property("statusLevel") == "ready"
    window.close()
    qt_app.processEvents()
