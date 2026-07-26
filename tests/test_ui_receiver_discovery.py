from __future__ import annotations

# ruff: noqa: RUF001
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from alga_vector.ui.pages.devices import DevicesPage


def _snapshot(*devices: object) -> object:
    return SimpleNamespace(
        revision=1,
        devices=devices,
        incidents=(),
        spectrum=None,
        runtime_mode="live",
        mode="live",
        profile_name="Тест приёмников",
        experience_level="guided",
        readiness_percent=0,
    )


def _device(kind: str, connection: str) -> object:
    return SimpleNamespace(
        device_id=f"{kind}-01",
        display_name=kind.upper(),
        kind=kind,
        connection=connection,
        state="ready",
        health="healthy",
        metrics={},
        sample_rate_hz=2_400_000,
        center_frequency_hz=433_920_000,
    )


class ReceiverRuntime:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.add_calls: list[tuple[str, str]] = []

    def current_snapshot(self) -> object:
        return self.snapshot

    def discover_hackrf_devices(self) -> object:
        candidate = SimpleNamespace(
            connection="HACKRF:0000000000000001",
            board_name="HackRF One",
            serial="0000000000000001",
        )
        return SimpleNamespace(state="complete", devices=(candidate,), issues=())

    def add_discovered_hackrf_device(self, connection: str) -> object:
        self.add_calls.append(("hackrf", connection))
        self.snapshot = _snapshot(_device("hackrf", connection))
        return self.snapshot

    def discover_tinysa_devices(self) -> object:
        candidate = SimpleNamespace(
            connection="COM7",
            description="tinySA Ultra USB Serial",
            evidence_ru="В системном описании есть tinySA.",
        )
        return SimpleNamespace(
            state="complete",
            candidates=(candidate,),
            issues=(),
        )

    def add_discovered_tinysa_device(self, connection: str) -> object:
        self.add_calls.append(("tinysa", connection))
        self.snapshot = _snapshot(_device("tinysa", connection))
        return self.snapshot


@pytest.mark.ui
def test_hackrf_discovery_requires_explicit_add_and_shows_rx_contract(
    qtbot: object,
) -> None:
    runtime = ReceiverRuntime()
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_hackrf_button, Qt.MouseButton.LeftButton)

    assert page.discovered_hackrf_select.currentData() == "HACKRF:0000000000000001"
    assert page.add_discovered_hackrf_button.isEnabled()
    assert "только приём" in page.hackrf_discovery_notice.text_label.text().lower()
    assert runtime.add_calls == []

    qtbot.mouseClick(
        page.add_discovered_hackrf_button,
        Qt.MouseButton.LeftButton,
    )

    assert runtime.add_calls == [
        ("hackrf", "HACKRF:0000000000000001")
    ]


@pytest.mark.ui
def test_tinysa_search_is_presented_as_metadata_only_confirmation(
    qtbot: object,
) -> None:
    runtime = ReceiverRuntime()
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_tinysa_button, Qt.MouseButton.LeftButton)

    assert page.discovered_tinysa_select.currentData() == "COM7"
    assert page.add_discovered_tinysa_button.isEnabled()
    rendered = page.tinysa_discovery_notice.text_label.text().lower()
    assert "ни один com-порт не открывался" in rendered
    assert runtime.add_calls == []
