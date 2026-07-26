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
        profile_name="Тест RTL-SDR",
        experience_level="guided",
        readiness_percent=0,
    )


def _candidate(
    *,
    index: int = 0,
    description: str = "RTL-SDR Blog V4",
    serial: str = "00000001",
) -> object:
    return SimpleNamespace(
        index=index,
        connection=f"RTLSDR:{index}",
        description=description,
        serial=serial,
        manufacturer="RTLSDRBlog",
    )


def _discovery_result(
    *,
    state: str = "complete",
    devices: tuple[object, ...] = (),
    issues: tuple[object, ...] = (),
) -> object:
    return SimpleNamespace(
        state=state,
        devices=devices,
        reported_count=len(devices),
        scanned_count=len(devices),
        issues=issues,
    )


class DiscoveryRuntime:
    def __init__(
        self,
        *,
        discovery_result: object,
        snapshot: object | None = None,
        add_result: object | None = None,
    ) -> None:
        self.snapshot = snapshot or _snapshot()
        self.discovery_result = discovery_result
        self.add_result = add_result
        self.discovery_calls = 0
        self.add_calls: list[str] = []
        self.discovery_error: Exception | None = None
        self.add_error: Exception | None = None

    def current_snapshot(self) -> object:
        return self.snapshot

    def discover_rtlsdr_devices(self) -> object:
        self.discovery_calls += 1
        if self.discovery_error is not None:
            raise self.discovery_error
        return self.discovery_result

    def add_discovered_rtlsdr_device(self, connection: str) -> object:
        self.add_calls.append(connection)
        if self.add_error is not None:
            raise self.add_error
        if self.add_result is None:
            raise AssertionError("add_result was not configured")
        self.snapshot = self.add_result
        return self.add_result


@pytest.mark.ui
def test_rtlsdr_discovery_shows_product_and_connection_but_never_serial(
    qtbot: object,
) -> None:
    candidate = _candidate()
    runtime = DiscoveryRuntime(discovery_result=_discovery_result(devices=(candidate,)))
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_rtlsdr_button, Qt.MouseButton.LeftButton)

    assert runtime.discovery_calls == 1
    assert page.discovered_rtlsdr_select.count() == 1
    label = page.discovered_rtlsdr_select.currentText()
    assert "RTL-SDR Blog V4" in label
    assert "RTLSDR:0" in label
    assert str(candidate.serial) not in label
    assert page.add_discovered_rtlsdr_button.isEnabled()
    assert "Поток пока не проверен" in page.rtlsdr_discovery_notice.text_label.text()
    assert page.rtlsdr_discovery_notice._level == "info"


@pytest.mark.ui
def test_rtlsdr_discovery_empty_result_is_warning_and_cannot_be_added(
    qtbot: object,
) -> None:
    runtime = DiscoveryRuntime(discovery_result=_discovery_result(state="empty"))
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_rtlsdr_button, Qt.MouseButton.LeftButton)

    assert page.discovered_rtlsdr_select.count() == 0
    assert not page.add_discovered_rtlsdr_button.isEnabled()
    assert "RTL-SDR не найден" in page.rtlsdr_discovery_notice.text_label.text()
    assert page.rtlsdr_discovery_notice._level == "warning"


@pytest.mark.ui
def test_rtlsdr_discovery_error_shows_runtime_reason_without_green_state(
    qtbot: object,
) -> None:
    issue = SimpleNamespace(
        message_ru="Библиотека librtlsdr недоступна.",
        operator_action_ru="Установите аппаратный пакет и повторите поиск.",
    )
    runtime = DiscoveryRuntime(
        discovery_result=_discovery_result(
            state="unavailable",
            issues=(issue,),
        )
    )
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_rtlsdr_button, Qt.MouseButton.LeftButton)

    notice = page.rtlsdr_discovery_notice.text_label.text()
    assert "librtlsdr недоступна" in notice
    assert "аппаратный пакет" in notice
    assert page.rtlsdr_discovery_notice._level == "critical"
    assert not page.add_discovered_rtlsdr_button.isEnabled()


@pytest.mark.ui
def test_already_configured_rtlsdr_is_marked_and_not_added_twice(
    qtbot: object,
) -> None:
    configured = SimpleNamespace(
        device_id="rtl-01",
        display_name="RTL-SDR",
        kind="rtlsdr",
        connection="rtlsdr:0",
        state="failed",
        health="error",
        driver="librtlsdr",
        sample_rate_hz=2_400_000,
        center_frequency_hz=433_920_000,
        metrics={},
        reason_ru="Нет потока.",
        recommended_action_ru="Повторите подключение.",
    )
    runtime = DiscoveryRuntime(
        discovery_result=_discovery_result(devices=(_candidate(),)),
        snapshot=_snapshot(configured),
    )
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())

    qtbot.mouseClick(page.discover_rtlsdr_button, Qt.MouseButton.LeftButton)

    assert "уже в профиле" in page.discovered_rtlsdr_select.currentText()
    assert not page.add_discovered_rtlsdr_button.isEnabled()
    page.add_discovered_rtlsdr_device()
    assert runtime.add_calls == []
    assert "уже находится в профиле" in page.rtlsdr_discovery_notice.text_label.text()


@pytest.mark.ui
def test_add_rtlsdr_refreshes_inventory_and_reports_failed_stream_truthfully(
    qtbot: object,
) -> None:
    failed = SimpleNamespace(
        device_id="rtl-auto-01",
        display_name="RTL-SDR Blog V4",
        kind="rtlsdr",
        connection="RTLSDR:0",
        state="failed",
        health="error",
        driver="librtlsdr",
        sample_rate_hz=2_400_000,
        center_frequency_hz=433_920_000,
        metrics={},
        reason_ru="Не удалось открыть USB-приёмник.",
        recommended_action_ru="Закройте другие SDR-программы и повторите проверку.",
    )
    runtime = DiscoveryRuntime(
        discovery_result=_discovery_result(devices=(_candidate(),)),
        add_result=_snapshot(failed),
    )
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())
    page.discover_rtlsdr_devices()

    qtbot.mouseClick(
        page.add_discovered_rtlsdr_button,
        Qt.MouseButton.LeftButton,
    )

    assert runtime.add_calls == ["RTLSDR:0"]
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "RTL-SDR Blog V4"
    notice = page.rtlsdr_discovery_notice.text_label.text()
    assert "поток не готов" in notice
    assert "Не удалось открыть USB-приёмник" in notice
    assert "Закройте другие SDR-программы" in notice
    assert page.rtlsdr_discovery_notice._level == "warning"
    assert page.header.status.text() == "ЕСТЬ ОГРАНИЧЕНИЯ"


@pytest.mark.ui
def test_ready_receiver_does_not_claim_that_live_stream_is_active(
    qtbot: object,
) -> None:
    ready = SimpleNamespace(
        device_id="rtl-auto-01",
        display_name="RTL-SDR Blog V4",
        kind="rtlsdr",
        connection="RTLSDR:0",
        state="ready",
        health="healthy",
        driver="librtlsdr",
        sample_rate_hz=2_400_000,
        center_frequency_hz=433_920_000,
        metrics={},
    )
    runtime = DiscoveryRuntime(
        discovery_result=_discovery_result(devices=(_candidate(),)),
        add_result=_snapshot(ready),
    )
    page = DevicesPage(runtime)
    qtbot.addWidget(page)
    page.refresh(runtime.current_snapshot())
    page.discover_rtlsdr_devices()

    qtbot.mouseClick(
        page.add_discovered_rtlsdr_button,
        Qt.MouseButton.LeftButton,
    )

    notice = page.rtlsdr_discovery_notice.text_label.text()
    assert "добавлен и готов" in notice
    assert "живых данных подтвердите" in notice
    assert "поток активен" not in notice.lower()
    assert page.rtlsdr_discovery_notice._level == "ready"
