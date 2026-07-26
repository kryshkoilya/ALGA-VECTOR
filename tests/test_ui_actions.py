
from __future__ import annotations

# ruff: noqa: RUF001
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from alga_vector.ui.onboarding import OnboardingDialog
from alga_vector.ui.pages.devices import DevicesPage
from alga_vector.ui.pages.diagnostics import DiagnosticsPage
from alga_vector.ui.pages.settings import SettingsPage


class FakeRuntime:
    def __init__(self) -> None:
        self.rescan_calls = 0
        self.reconnect_calls: list[str] = []
        self.snapshot = SimpleNamespace(
            revision=1,
            devices=(
                SimpleNamespace(
                    device_id="receiver-01",
                    display_name="Receiver 01",
                    kind="tinysa",
                    connection="SIM:TINYSA",
                    state="ready",
                    health="healthy",
                    driver="deterministic",
                    sample_rate_hz=2_400_000,
                    center_frequency_hz=433_920_000,
                    metrics={},
                ),
                SimpleNamespace(
                    device_id="kraken-01",
                    display_name="Array DF (KrakenSDR)",
                    kind="krakensdr",
                    connection="192.168.1.100",
                    state="absent",
                    health="error",
                    driver="k-daq-v2",
                    sample_rate_hz=None,
                    center_frequency_hz=None,
                    metrics={},
                    reason_ru="Нет связи",
                    recommended_action_ru="Проверьте подключение",
                ),
            ),
            incidents=(),
            spectrum=None,
            mode="simulated",
            profile_name="Тест",
            readiness_percent=50,
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def rescan(self) -> object:
        self.rescan_calls += 1
        return self.snapshot

    def reconnect(self, device_id: str) -> object:
        self.reconnect_calls.append(device_id)
        return self.snapshot


class StagedOnboardingRuntime(FakeRuntime):
    def __init__(self, *, fail_stage: str = "") -> None:
        super().__init__()
        self.fail_stage = fail_stage
        self.calls: list[str] = []
        self.settings_payload: dict[str, object] = {}
        self.final_storage = ""
        self.completed = False
        self.completion_argument: object = "not-called"

    def update_settings(self, payload: dict[str, object]) -> str:
        self.calls.append("update")
        self.settings_payload = payload
        storage = payload.get("storage")
        if isinstance(storage, dict):
            self.final_storage = str(storage.get("data_dir", ""))
        if self.fail_stage == "update":
            raise RuntimeError("update failed")
        return "Настройки сохранены"

    def import_map_package(self, path: str) -> str:
        del path
        assert self.final_storage
        self.calls.append("map")
        if self.fail_stage == "map":
            raise RuntimeError("map failed")
        return "Карта импортирована"

    def set_manual_base(self, latitude: float, longitude: float) -> str:
        del latitude, longitude
        self.calls.append("base")
        if self.fail_stage == "base":
            raise RuntimeError("base failed")
        return "База сохранена"

    def start_gps(self, port: str) -> str:
        del port
        self.calls.append("gps")
        if self.fail_stage == "gps":
            raise RuntimeError("gps failed")
        return "GPS запущен"

    def complete_onboarding(self, data_dir: object = None) -> str:
        self.calls.append("complete")
        self.completion_argument = data_dir
        if self.fail_stage == "complete":
            raise RuntimeError("complete failed")
        self.completed = True
        return "Завершено"


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication(["alga-vector-actions-test"])


@pytest.mark.ui
def test_device_reconnect_calls_only_runtime_protocol(qt_app: QApplication) -> None:
    runtime = FakeRuntime()
    page = DevicesPage(runtime)
    page.show()
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()
    target_row = -1
    for row in range(page.table.rowCount()):
        item = page.table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == "kraken-01":
            target_row = row
            break
    assert target_row >= 0
    page.table.selectRow(target_row)
    page.reconnect_selected()
    assert runtime.reconnect_calls == ["kraken-01"]
    assert "не восстановлен" in page.action_result.text()
    page.close()


@pytest.mark.ui
def test_disabled_device_cannot_be_reconnected_from_inventory(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.snapshot.devices = (
        SimpleNamespace(
            device_id="disabled-01",
            display_name="Disabled receiver",
            kind="rtlsdr",
            connection="RTLSDR:0",
            state="disabled",
            health="unknown",
            driver="N/A",
            sample_rate_hz=2_400_000,
            center_frequency_hz=None,
            metrics={},
            reason_ru="Устройство отключено в конфигурации.",
            recommended_action_ru="Включите адаптер в настройках.",
        ),
    )
    page = DevicesPage(runtime)
    page.refresh(runtime.current_snapshot())
    page.table.selectRow(0)
    qt_app.processEvents()

    assert not page.reconnect_button.isEnabled()
    assert "настройках" in page.reconnect_button.toolTip()
    page.close()


@pytest.mark.ui
def test_device_scan_is_not_green_when_every_receiver_failed(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.snapshot.devices = (
        SimpleNamespace(
            device_id="failed-01",
            display_name="Failed receiver",
            kind="tinysa",
            connection="COM7",
            state="failed",
            health="error",
            driver="USB CDC",
            sample_rate_hz=None,
            center_frequency_hz=None,
            metrics={},
            reason_ru="COM-порт недоступен.",
            recommended_action_ru="Проверьте кабель.",
        ),
    )
    page = DevicesPage(runtime)
    page.rescan()
    qt_app.processEvents()

    assert "доступных приёмников нет" in page.action_result.text()
    assert "COM-порт недоступен" in page.action_result.text()
    assert page.header.status.text() == "ЕСТЬ ОГРАНИЧЕНИЯ"
    page.close()


@pytest.mark.ui
def test_onboarding_scans_only_after_explicit_action(qt_app: QApplication) -> None:
    runtime = FakeRuntime()
    dialog = OnboardingDialog(runtime)
    assert runtime.rescan_calls == 0
    dialog.scan_devices()
    qt_app.processEvents()
    assert runtime.rescan_calls == 1
    assert dialog.device_list.count() == 2
    dialog.close()


@pytest.mark.ui
def test_onboarding_scan_warns_when_no_receiver_is_usable(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.snapshot.devices = (
        SimpleNamespace(
            device_id="failed-01",
            display_name="Failed receiver",
            state="failed",
        ),
        SimpleNamespace(
            device_id="disabled-01",
            display_name="Disabled receiver",
            state="disabled",
        ),
    )
    dialog = OnboardingDialog(runtime)
    dialog.scan_devices()
    qt_app.processEvents()

    assert "ДОСТУПНЫХ НЕТ" in dialog.device_status.text()
    assert dialog.device_status.property("statusLevel") == "warning"
    dialog.close()


@pytest.mark.ui
def test_onboarding_has_six_receive_only_steps_without_map_or_gps(
    qt_app: QApplication,
) -> None:
    dialog = OnboardingDialog(FakeRuntime())
    assert dialog.stack.count() == 6
    headings: list[str] = []
    visible_copy: list[str] = []
    for index in range(dialog.stack.count()):
        page = dialog.stack.widget(index)
        assert page is not None
        labels = page.findChildren(QLabel)
        headings.extend(
            label.text()
            for label in labels
            if label.property("heading") == "true"
        )
        visible_copy.extend(label.text() for label in labels)

    assert headings == [
        "Добро пожаловать в ALGA VECTOR",
        "Как показывать информацию",
        "Локальное хранилище",
        "Приёмник",
        "Интерпретация и ограничения",
        "Готово к безопасному запуску",
    ]
    rendered = " ".join(visible_copy).lower()
    assert "карта" not in rendered
    assert "gps" not in rendered
    assert any(
        label.text() == "Разработал: Буйвол и Задира"
        for label in dialog.findChildren(QLabel)
    )
    dialog.close()


@pytest.mark.ui
def test_settings_load_all_values_and_do_not_clobber_dirty_form(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.applied = []
    runtime.settings_snapshot = lambda: {
        "profile_name": "Нестандартный профиль",
        "mode": "safe",
        "runtime_override": None,
        "storage": {
            "data_dir": "D:\\ALGA_DATA",
            "retention_days": 91,
            "minimum_free_gib": 12.5,
        },
        "devices": {
            "enable_real_adapters": True,
            "adapters": [
                {
                    "id": "tiny-01",
                    "kind": "tinysa",
                    "enabled": True,
                    "connection": "COM7",
                },
                {
                    "id": "rtl-01",
                    "kind": "rtlsdr",
                    "enabled": True,
                    "connection": "RTLSDR:0",
                },
            ],
        },
        "spectrum": {
            "center_frequency_hz": 915_000_000,
            "span_hz": 2_500_000,
            "sample_rate_hz": 3_200_000,
            "threshold_level": -61.5,
        },
        "location": {"source": "manual", "gps_port": ""},
    }
    runtime.manual_base_calls = []
    runtime.set_manual_base = (
        lambda latitude, longitude: runtime.manual_base_calls.append(
            (latitude, longitude)
        )
        or "Сохранено"
    )

    def update_settings(payload: dict[str, object]) -> str:
        runtime.applied.append(payload)
        return "Сохранено"

    runtime.update_settings = update_settings
    page = SettingsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.profile_name.text() == "Нестандартный профиль"
    assert page.data_dir.text() == "D:\\ALGA_DATA"
    assert page.retention.value() == 91
    assert page.minimum_free.value() == 12.5
    assert page.center_frequency.value() == 915.0
    assert page.span.value() == 2500.0
    assert page.sample_rate.value() == 3.2
    assert page.threshold.value() == -61.5
    assert page.real_adapters.isChecked()

    page.hardware_table.selectRow(1)
    qt_app.processEvents()
    profile_index = page.hardware_rtlsdr_profile.findData("blog_v4")
    page.hardware_rtlsdr_profile.setCurrentIndex(profile_index)
    page.commit_hardware_editor()

    page.profile_name.setText("Редактируется оператором")
    page.profile_name.textEdited.emit(page.profile_name.text())
    page.refresh(runtime.current_snapshot())
    assert page.profile_name.text() == "Редактируется оператором"
    page.apply_settings()
    assert runtime.applied[0]["profile_name"] == "Редактируется оператором"
    assert [
        adapter["id"] for adapter in runtime.applied[0]["devices"]["adapters"]
    ] == ["tiny-01", "rtl-01"]
    assert (
        runtime.applied[0]["devices"]["adapters"][1]["rtlsdr_profile"]
        == "blog_v4"
    )
    assert "map" not in runtime.applied[0]
    assert "location" not in runtime.applied[0]
    assert page._legacy_location_panel.isHidden()
    assert not page.direction_panel.isHidden()
    assert "не измеряет азимут" in page.direction_limitations.text_label.text()
    assert runtime.manual_base_calls == []
    page.close()


@pytest.mark.ui
def test_settings_block_invalid_rtlsdr_span_but_preserve_tinysa_range(
    qt_app: QApplication,
) -> None:
    def settings_snapshot(kind: str) -> dict[str, object]:
        return {
            "profile_name": "Приёмный профиль",
            "mode": "live",
            "runtime_override": None,
            "storage": {
                "data_dir": "runtime-data",
                "retention_days": 30,
                "minimum_free_gib": 5.0,
            },
            "devices": {
                "enable_real_adapters": True,
                "adapters": [
                    {
                        "id": "receiver-01",
                        "kind": kind,
                        "enabled": True,
                        "connection": "RTLSDR:0" if kind == "rtlsdr" else "COM7",
                    }
                ],
            },
            "spectrum": {
                "center_frequency_hz": 433_920_000,
                "span_hz": 2_000_000,
                "sample_rate_hz": 2_400_000,
                "threshold_level": -72.4,
            },
            "location": {"source": "unset", "gps_port": ""},
            "ui": {"experience_level": "guided"},
            "map": {"package_path": None},
        }

    rtl_runtime = FakeRuntime()
    rtl_runtime.applied = []
    rtl_runtime.settings_snapshot = lambda: settings_snapshot("rtlsdr")
    rtl_runtime.update_settings = (
        lambda payload: rtl_runtime.applied.append(payload) or "Сохранено"
    )
    rtl_page = SettingsPage(rtl_runtime)
    rtl_page.refresh(rtl_runtime.current_snapshot())
    rtl_page.span.setValue(3_000.0)
    rtl_page.sample_rate.setValue(2.4)
    rtl_page.apply_settings()

    assert rtl_runtime.applied == []
    assert rtl_page.header.status.property("statusLevel") == "critical"
    assert "не может превышать" in rtl_page.result.text()
    rtl_page.close()

    tiny_runtime = FakeRuntime()
    tiny_runtime.applied = []
    tiny_runtime.settings_snapshot = lambda: settings_snapshot("tinysa")
    tiny_runtime.update_settings = (
        lambda payload: tiny_runtime.applied.append(payload) or "Сохранено"
    )
    tiny_page = SettingsPage(tiny_runtime)
    tiny_page.refresh(tiny_runtime.current_snapshot())
    tiny_page.span.setValue(3_000.0)
    tiny_page.sample_rate.setValue(2.4)
    tiny_page.apply_settings()

    assert len(tiny_runtime.applied) == 1
    spectrum = tiny_runtime.applied[0]["spectrum"]
    assert isinstance(spectrum, dict)
    assert spectrum["span_hz"] == 3_000_000
    tiny_page.close()


@pytest.mark.ui
def test_settings_uses_hackrf_profile_limits_and_validates_the_whole_window(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.applied = []
    runtime.settings_snapshot = lambda: {
        "profile_name": "HackRF RX",
        "mode": "live",
        "runtime_override": None,
        "storage": {
            "data_dir": "runtime-data",
            "retention_days": 30,
            "minimum_free_gib": 5.0,
        },
        "devices": {
            "enable_real_adapters": True,
            "adapters": [
                {
                    "id": "hackrf-01",
                    "kind": "hackrf",
                    "enabled": True,
                    "connection": "HACKRF:0000000000000001",
                }
            ],
        },
        "spectrum": {
            "center_frequency_hz": 433_920_000,
            "span_hz": 2_000_000,
            "sample_rate_hz": 2_400_000,
            "threshold_level": -72.4,
        },
        "ui": {"experience_level": "guided"},
    }
    runtime.update_settings = (
        lambda payload: runtime.applied.append(payload) or "Сохранено"
    )
    page = SettingsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.hardware_kind.findData("hackrf") >= 0
    assert page.center_frequency.minimum() == pytest.approx(1.0)
    assert page.center_frequency.maximum() == pytest.approx(6_000.0)
    assert page.sample_rate.minimum() == pytest.approx(2.0)
    assert page.sample_rate.maximum() == pytest.approx(20.0)
    assert page.span.maximum() == pytest.approx(20_000.0)
    assert "HackRF One" in page.receiver_capability_note.text()

    # A centre exactly on the hardware edge still leaves half the requested
    # span outside the supported receive range and must be rejected.
    page.center_frequency.setValue(6_000.0)
    page.span.setValue(2_000.0)
    page.sample_rate.setValue(20.0)
    page.apply_settings()

    assert runtime.applied == []
    assert page.header.status.property("statusLevel") == "critical"
    assert "всё выбранное окно" in page.result.text().lower()
    page.close()


@pytest.mark.ui
def test_settings_requires_explicit_tinysa_model_before_ultra_mode(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.applied = []
    runtime.settings_snapshot = lambda: {
        "profile_name": "tinySA laboratory",
        "mode": "live",
        "runtime_override": None,
        "storage": {
            "data_dir": "runtime-data",
            "retention_days": 30,
            "minimum_free_gib": 5.0,
        },
        "devices": {
            "enable_real_adapters": True,
            "adapters": [
                {
                    "id": "tinysa-01",
                    "kind": "tinysa",
                    "enabled": True,
                    "connection": "COM7",
                    "tinysa_model": "auto",
                    "tinysa_ultra_mode": False,
                }
            ],
        },
        "spectrum": {
            "center_frequency_hz": 433_920_000,
            "span_hz": 2_000_000,
            "sample_rate_hz": 2_400_000,
            "threshold_level": -72.4,
        },
        "ui": {"experience_level": "guided"},
    }
    runtime.update_settings = (
        lambda payload: runtime.applied.append(payload) or "Сохранено"
    )

    page = SettingsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.hardware_tinysa_model.isEnabled()
    assert not page.hardware_tinysa_ultra_mode.isEnabled()
    model_index = page.hardware_tinysa_model.findData(
        "ultra_plus_zs407"
    )
    page.hardware_tinysa_model.setCurrentIndex(model_index)
    assert page.hardware_tinysa_ultra_mode.isEnabled()
    page.hardware_tinysa_ultra_mode.setChecked(True)
    page.commit_hardware_editor()

    assert page.center_frequency.maximum() == pytest.approx(7_300.0)
    assert "Ultra mode" in page.hardware_table.item(0, 3).text()
    page.center_frequency.setValue(7_000.0)
    page.span.setValue(100_000.0)
    page.apply_settings()

    assert len(runtime.applied) == 1
    devices = runtime.applied[0]["devices"]
    assert isinstance(devices, dict)
    adapters = devices["adapters"]
    assert isinstance(adapters, list)
    assert adapters[0]["tinysa_model"] == "ultra_plus_zs407"
    assert adapters[0]["tinysa_ultra_mode"] is True
    assert "зеркал" in page.result.text().lower()
    page.close()


@pytest.mark.ui
def test_production_settings_cannot_persist_demo_mode(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.applied = []
    runtime.settings_snapshot = lambda: {
        "profile_name": "Старый демо-профиль",
        "mode": "demo",
        "runtime_override": None,
        "storage": {
            "data_dir": "runtime-data",
            "retention_days": 30,
            "minimum_free_gib": 5.0,
        },
        "devices": {
            "enable_real_adapters": False,
            "adapters": [],
        },
        "spectrum": {
            "center_frequency_hz": 433_920_000,
            "span_hz": 5_000_000,
            "sample_rate_hz": 2_400_000,
            "threshold_level": -72.4,
        },
        "location": {"source": "unset", "gps_port": ""},
        "ui": {"experience_level": "guided"},
        "map": {"package_path": None},
    }

    def update_settings(payload: dict[str, object]) -> str:
        runtime.applied.append(payload)
        return "Сохранено"

    runtime.update_settings = update_settings
    page = SettingsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.mode.findData("demo") == -1
    assert page.mode.currentData() == "live"
    page.apply_settings()
    assert runtime.applied[0]["mode"] == "live"
    page.close()


@pytest.mark.ui
def test_explicit_demo_is_visible_but_process_locked(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.settings_snapshot = lambda: {
        "profile_name": "Учебный сеанс",
        "mode": "demo",
        "runtime_override": "demo",
        "storage": {
            "data_dir": "runtime-data",
            "retention_days": 30,
            "minimum_free_gib": 5.0,
        },
        "devices": {
            "enable_real_adapters": False,
            "adapters": [],
        },
        "spectrum": {
            "center_frequency_hz": 433_920_000,
            "span_hz": 5_000_000,
            "sample_rate_hz": 2_400_000,
            "threshold_level": -72.4,
        },
        "location": {"source": "unset", "gps_port": ""},
        "ui": {"experience_level": "guided"},
        "map": {"package_path": None},
    }

    page = SettingsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.mode.currentData() == "demo"
    assert not page.mode.isEnabled()
    page.close()


@pytest.mark.ui
def test_onboarding_persists_completion_before_accept(qt_app: QApplication) -> None:
    runtime = FakeRuntime()
    completed: list[str] = []
    runtime.complete_onboarding = lambda path: completed.append(path) or "Сохранено"
    dialog = OnboardingDialog(runtime)
    dialog.storage_path.setText("D:\\ALGA_CAPTURE")
    dialog.stack.setCurrentIndex(dialog.stack.count() - 1)
    dialog.next()
    qt_app.processEvents()

    assert completed == ["D:\\ALGA_CAPTURE"]
    assert dialog.result() == dialog.DialogCode.Accepted


@pytest.mark.ui
def test_onboarding_commits_receive_only_setup_in_recoverable_stage_order(
    qt_app: QApplication,
) -> None:
    runtime = StagedOnboardingRuntime()
    dialog = OnboardingDialog(runtime)
    dialog.storage_path.setText("D:\\ALGA_CAPTURE")
    dialog.stack.setCurrentIndex(dialog.stack.count() - 1)

    dialog.next()
    qt_app.processEvents()

    assert runtime.calls == ["update", "complete"]
    assert runtime.settings_payload["storage"] == {
        "data_dir": "D:\\ALGA_CAPTURE"
    }
    assert "map" not in runtime.settings_payload
    assert "location" not in runtime.settings_payload
    assert runtime.completion_argument is None
    assert runtime.completed
    assert dialog.result() == dialog.DialogCode.Accepted


@pytest.mark.ui
@pytest.mark.parametrize("fail_stage", ("update", "complete"))
def test_onboarding_never_marks_complete_after_a_later_stage_failure(
    qt_app: QApplication,
    fail_stage: str,
) -> None:
    runtime = StagedOnboardingRuntime(fail_stage=fail_stage)
    dialog = OnboardingDialog(runtime)
    dialog.storage_path.setText("D:\\ALGA_CAPTURE")
    dialog.stack.setCurrentIndex(dialog.stack.count() - 1)

    dialog.next()
    qt_app.processEvents()

    assert not runtime.completed
    assert dialog.result() != dialog.DialogCode.Accepted
    assert dialog.completion_error.text()
    if fail_stage != "complete":
        assert "complete" not in runtime.calls
    dialog.close()


@pytest.mark.ui
def test_onboarding_prevalidation_runs_before_any_runtime_mutation(
    qt_app: QApplication,
) -> None:
    runtime = StagedOnboardingRuntime()
    dialog = OnboardingDialog(runtime)
    dialog.hardware_kind.setCurrentIndex(dialog.hardware_kind.findData("rtlsdr"))
    dialog.hardware_id.setText("rtl-01")
    dialog.hardware_connection.setText("USB auto")
    dialog.stack.setCurrentIndex(dialog.stack.count() - 1)

    dialog.next()
    qt_app.processEvents()

    assert runtime.calls == []
    assert "RTLSDR:<индекс>" in dialog.completion_error.text()
    assert dialog.result() != dialog.DialogCode.Accepted
    dialog.close()


@pytest.mark.ui
def test_optional_runtime_actions_are_not_presented_as_working(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    diagnostics = DiagnosticsPage(runtime)
    diagnostics.refresh(runtime.current_snapshot())
    settings = SettingsPage(runtime)
    settings.refresh(runtime.current_snapshot())
    qt_app.processEvents()
    assert not diagnostics.support_bundle.isEnabled()
    assert not diagnostics.acknowledge.isEnabled()
    assert settings.apply_button.text() == "Проверить значения"
    settings.apply_settings()
    assert settings.header.status.text() == "ЗНАЧЕНИЯ ПРОВЕРЕНЫ"


@pytest.mark.ui
def test_acknowledged_incident_is_still_active_in_diagnostics(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.snapshot.devices = ()
    runtime.snapshot.incidents = (
        SimpleNamespace(
            incident_id="capture-failure",
            severity="error",
            acknowledged=True,
            occurred_at="12:00:00",
            code="CAPTURE.WRITE_FAILED",
            title_ru="Запись остановлена",
            message_ru="Локальный диск недоступен.",
            action_ru="Проверьте диск.",
            source="capture",
            technical={},
        ),
    )
    page = DiagnosticsPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.table.rowCount() == 1
    assert "ОЗНАКОМЛЕН" in page.table.item(0, 1).text()
    assert page.header.status.text() == "1 КРИТИЧЕСКОЕ АКТИВНОЕ СОБЫТИЕ"
    assert page.header.status.property("statusLevel") == "critical"
    assert page.acknowledge.text() == "Ознакомление подтверждено"
    assert not page.acknowledge.isEnabled()
    assert page.header.status.text() != "СИСТЕМА СТАБИЛЬНА"
    page.close()


@pytest.mark.ui
def test_gps_candidate_search_requires_explicit_selection(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    runtime.discover_gps_ports = lambda: (
        SimpleNamespace(
            port="COM12",
            display_name="COM12 · u-blox GNSS",
            confidence="likely",
            reason_ru="В системном описании есть признак GPS/GNSS.",
        ),
        SimpleNamespace(
            port="COM3",
            display_name="COM3 · USB Serial Port",
            confidence="possible",
            reason_ru="Назначение нужно подтвердить.",
        ),
    )
    settings = SettingsPage(runtime)
    settings.refresh(runtime.current_snapshot())
    settings.location_source.setCurrentIndex(
        settings.location_source.findData("gps")
    )

    settings.discover_gps_ports()
    qt_app.processEvents()

    assert settings.gps_candidates.currentData() == ""
    assert settings.gps_port.text() == ""
    settings.gps_candidates.setCurrentIndex(1)
    assert settings.gps_port.text() == "COM12"
    assert "Ни один порт не был открыт" in settings.gps_status_notice.text_label.text()
    settings.close()
