from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from alga_vector.ui.app import create_application
from alga_vector.ui.main_window import MainWindow


class _ModeRuntime:
    def __init__(self) -> None:
        self.update_calls: list[dict[str, object]] = []
        self.snapshot = SimpleNamespace(
            revision=1,
            devices=(),
            capabilities=(),
            incidents=(),
            spectrum=None,
            mode="live",
            runtime_mode="live",
            profile_name="Проверка режимов",
            readiness_percent=100,
            experience_level="guided",
            signal_events=(),
            signal_assessment=SimpleNamespace(state="background_only"),
            signal_decision=None,
            location=None,
            map_status=None,
            direction=None,
            acoustic=None,
            airspace=None,
            fusion_decision=None,
            scan_plan=None,
            operator_situation=None,
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def tick(self) -> object:
        return self.snapshot

    def update_settings(self, payload: dict[str, object]) -> str:
        self.update_calls.append(payload)
        ui = payload.get("ui")
        if isinstance(ui, dict):
            experience = ui.get("experience_level")
            if isinstance(experience, str):
                self.snapshot.experience_level = experience
        return "Настройки применены."


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return create_application(["alga-vector-interface-mode-test"])


@pytest.mark.ui
def test_simple_and_expert_modes_share_pages_and_persist_selection(
    qt_app: QApplication,
) -> None:
    runtime = _ModeRuntime()
    window = MainWindow(runtime)
    window.refresh_timer.stop()
    window.show()
    qt_app.processEvents()

    spectrum_page = window.page("spectrum")
    map_page = window.page("map")
    assert window.current_page_key == "situation"
    assert window.findChild(QPushButton, "nav_situation") is not None
    assert window.findChild(QPushButton, "nav_spectrum") is None
    assert window.findChild(QPushButton, "nav_map") is None
    assert window.simple_mode_button.isChecked()

    window.expert_mode_button.click()
    qt_app.processEvents()

    assert runtime.snapshot.experience_level == "expert"
    assert runtime.update_calls[-1] == {
        "ui": {"experience_level": "expert"}
    }
    assert window.expert_mode_button.isChecked()
    assert window.findChild(QPushButton, "nav_spectrum") is not None
    assert window.findChild(QPushButton, "nav_map") is not None
    assert window.page("spectrum") is spectrum_page
    assert window.page("map") is map_page

    window.navigate("map")
    assert window.current_page_key == "map"
    window.simple_mode_button.click()
    qt_app.processEvents()

    assert runtime.snapshot.experience_level == "guided"
    assert window.current_page_key == "situation"
    assert window.findChild(QPushButton, "nav_spectrum") is None
    assert window.findChild(QPushButton, "nav_map") is None
    assert window.page("spectrum") is spectrum_page
    assert window.page("map") is map_page

    window.close()
    qt_app.processEvents()
