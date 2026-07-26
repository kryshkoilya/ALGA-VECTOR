
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from alga_vector.ui.pages.spectrum import SpectrumPage
from alga_vector.ui.widgets.spectrum_plot import SpectrumPlot, WaterfallPlot


class FakeRuntime:
    def __init__(self) -> None:
        self.recording = False
        self.recording_calls: list[str] = []
        spectrum = SimpleNamespace(
            source_id="demo",
            sequence=3,
            center_frequency_hz=433_920_000,
            span_hz=5_000_000,
            power_dbm=[-94.0, -70.0, -43.0, -82.0],
            peak_dbm=-43.0,
            dropped_frames=0,
            data_age_ms=0,
        )
        self.snapshot = SimpleNamespace(
            revision=3,
            devices=(),
            incidents=(),
            spectrum=spectrum,
            mode="simulated",
            profile_name="Демо",
            readiness_percent=82,
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def recording_status(self) -> object:
        return SimpleNamespace(
            active=self.recording,
            path="D:\\ALGA_DATA\\captures\\capture.jsonl.partial" if self.recording else None,
            completed_path=None,
            elapsed_seconds=2.0 if self.recording else 0.0,
            frames=2 if self.recording else 0,
            bytes_written=2048 if self.recording else 0,
            bytes_per_second=1024.0 if self.recording else 0.0,
        )

    def start_recording(self) -> object:
        self.recording = True
        self.recording_calls.append("start")
        return self.recording_status()

    def stop_recording(self) -> object:
        self.recording = False
        self.recording_calls.append("stop")
        return SimpleNamespace(
            path="D:\\ALGA_DATA\\captures\\capture.jsonl",
            frames=2,
            bytes_written=2048,
        )


class TuningRuntime:
    def __init__(self) -> None:
        self.applied: list[dict[str, object]] = []
        spectrum = SimpleNamespace(
            source_id="rtl-01",
            sequence=7,
            center_frequency_hz=433_920_000,
            span_hz=2_000_000,
            power_dbm=[-98.0, -72.0, -45.0, -81.0],
            peak_dbm=-45.0,
            dropped_frames=0,
            data_age_ms=12,
        )
        self.snapshot = SimpleNamespace(
            revision=7,
            devices=(
                SimpleNamespace(
                    device_id="rtl-01",
                    display_name="RTL-SDR 01",
                    kind="rtlsdr",
                    state="ready",
                    sample_rate_hz=2_400_000,
                    metrics={},
                ),
            ),
            incidents=(),
            spectrum=spectrum,
            mode="live",
            profile_name="Live",
            readiness_percent=100,
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def settings_snapshot(self) -> dict[str, object]:
        return {
            "devices": {
                "adapters": [
                    {
                        "id": "rtl-01",
                        "kind": "rtlsdr",
                        "enabled": True,
                        "connection": "RTLSDR:0",
                    }
                ]
            },
            "spectrum": {"sample_rate_hz": 2_400_000},
        }

    def update_settings(self, payload: dict[str, object]) -> str:
        self.applied.append(payload)
        return "Сохранено"

    def recording_status(self) -> object:
        return SimpleNamespace(
            active=False,
            path=None,
            elapsed_seconds=0.0,
            frames=0,
            bytes_written=0,
            bytes_per_second=0.0,
        )


class ScanRuntime(TuningRuntime):
    def __init__(self, *, experience_level: str = "guided") -> None:
        super().__init__()
        self.snapshot.experience_level = experience_level
        self.snapshot.scan_plan = None
        self.started_scan_presets: list[str] = []
        self.stop_scan_calls = 0

    def start_scan_plan(self, preset_id: str) -> object:
        self.started_scan_presets.append(preset_id)
        status = SimpleNamespace(
            active=True,
            plan_id=(
                preset_id
                if preset_id == "full_supported"
                else f"preset_{preset_id}"
            ),
            profile_id="rtlsdr_generic",
            source_id="rtl-01",
            current_window_id="general_uhf-0002",
            current_window_label_ru="UHF · общий участок",
            current_ordinal=2,
            window_count=48,
            start_frequency_hz=300_000_000,
            stop_frequency_hz=302_400_000,
            center_frequency_hz=301_200_000,
            span_hz=2_400_000,
            successful_frames_in_window=7,
            dwell_frames=12,
            completed_windows=98,
            completed_cycles=2,
            failed_windows=1,
            estimated_cycle_ms=195_000,
            coverage_fraction=0.875,
            sequential=True,
            limitation_codes=("sequential_scan",),
            limitations_ru=("Sequential scan.",),
            observed_window_id="general_uhf-0002",
            observed_window_label_ru="UHF · общий участок",
            observed_ordinal=2,
            observed_start_frequency_hz=300_000_000,
            observed_stop_frequency_hz=302_400_000,
            transition_pending=False,
        )
        self.snapshot.scan_plan = status
        return status

    def stop_scan_plan(self) -> str:
        self.stop_scan_calls += 1
        self.snapshot.scan_plan = None
        return "Автообзор остановлен"


class FailingScanRuntime(ScanRuntime):
    def start_scan_plan(self, preset_id: str) -> object:
        raise RuntimeError(
            f"План {preset_id} превышает аппаратный лимит окон"
        )


class HardwareTuningRuntime(TuningRuntime):
    def __init__(
        self,
        *,
        kind: str,
        connection: str,
        metrics: dict[str, object],
    ) -> None:
        super().__init__()
        device_id = f"{kind}-01"
        self.kind = kind
        self.connection = connection
        self.snapshot.spectrum.source_id = device_id
        self.snapshot.devices = (
            SimpleNamespace(
                device_id=device_id,
                display_name=device_id,
                kind=kind,
                connection=connection,
                state="ready",
                sample_rate_hz=2_400_000 if kind == "hackrf" else None,
                metrics=metrics,
            ),
        )

    def settings_snapshot(self) -> dict[str, object]:
        adapter: dict[str, object] = {
            "id": f"{self.kind}-01",
            "kind": self.kind,
            "enabled": True,
            "connection": self.connection,
        }
        if self.kind == "tinysa":
            adapter.update(
                {
                    "tinysa_model": "auto",
                    "tinysa_ultra_mode": True,
                }
            )
        return {
            "devices": {"adapters": [adapter]},
            "spectrum": {"sample_rate_hz": 2_400_000},
        }


class EmptyLiveRuntime:
    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(
            revision=1,
            devices=(),
            incidents=(),
            spectrum=None,
            mode="live",
            runtime_mode="live",
            profile_name="Live",
            readiness_percent=0,
        )

    def current_snapshot(self) -> object:
        return self.snapshot

    def recording_status(self) -> object:
        return SimpleNamespace(
            active=False,
            path=None,
            elapsed_seconds=0.0,
            frames=0,
            bytes_written=0,
            bytes_per_second=0.0,
        )


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication(["alga-vector-spectrum-test"])


@pytest.mark.ui
def test_demo_spectrum_is_deterministic(qt_app: QApplication) -> None:
    plot = SpectrumPlot()
    plot.set_demo_sequence(41)
    first = plot.power_values
    plot.set_demo_sequence(41)
    assert plot.power_values == first
    plot.set_demo_sequence(42)
    assert plot.power_values != first


@pytest.mark.ui
def test_live_plot_starts_empty_and_never_fabricates_malformed_data(
    qt_app: QApplication,
) -> None:
    plot = SpectrumPlot()
    assert plot.power_values == ()
    plot.set_frame(SimpleNamespace(power_dbm=["invalid"]))
    assert plot.power_values == ()


@pytest.mark.ui
def test_waterfall_is_bounded(qt_app: QApplication) -> None:
    waterfall = WaterfallPlot()
    assert waterfall.row_count == 0
    for sequence in range(100):
        waterfall.append_power([-100.0 + sequence % 60] * 64)
    assert 1 <= waterfall.row_count <= 54


@pytest.mark.ui
def test_spectrum_pause_and_recording_use_runtime_without_fake_metrics(
    qt_app: QApplication,
) -> None:
    runtime = FakeRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    page.toggle_pause()
    assert page.pause_button.text() == "Продолжить график"
    assert page.header.status.text() == "ГРАФИК ЗАМОРОЖЕН"
    assert "Приём, анализ событий и активная запись продолжаются" in (
        page.state_notice.text_label.text()
    )
    page.toggle_pause()
    assert page.pause_button.text() == "Заморозить график"
    page.toggle_recording()
    assert page.record_button.text() == "Остановить запись спектра"
    page.toggle_recording()
    assert page.record_button.text() == "Начать запись спектра"
    assert runtime.recording_calls == ["start", "stop"]
    assert "42,8" not in page.record_rate.text()


@pytest.mark.ui
def test_rtlsdr_tuning_loads_sample_rate_and_blocks_invalid_span(
    qt_app: QApplication,
) -> None:
    runtime = TuningRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.sample_rate.value() == pytest.approx(2.4)

    page.span.setValue(3_000.0)
    page.apply_tuning()

    assert runtime.applied == []
    assert page.header.status.property("statusLevel") == "critical"
    assert "не может превышать" in page.state_notice.text_label.text()

    page.span.setValue(2_000.0)
    page.apply_tuning()

    assert len(runtime.applied) == 1
    spectrum = runtime.applied[0]["spectrum"]
    assert isinstance(spectrum, dict)
    assert spectrum["span_hz"] == 2_000_000
    assert spectrum["sample_rate_hz"] == 2_400_000
    page.close()


@pytest.mark.ui
def test_guided_tuning_explains_retune_vs_instantaneous_bandwidth(
    qt_app: QApplication,
) -> None:
    runtime = TuningRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert not page.guided_tuning.isHidden()
    assert page.guided_center.minimum() == pytest.approx(24.0)
    assert "последовательной перестройкой" in page.guided_tuning_capability.text()
    assert "не одновременно" in page.guided_tuning_capability.text()
    assert page.guided_preset.findData("broadcast_am") == -1
    assert page.guided_preset.findData("broadcast_fm") >= 0
    assert page.guided_manual_tuning.isHidden()
    page.guided_manual_toggle_button.click()
    qt_app.processEvents()
    assert not page.guided_manual_tuning.isHidden()
    page.close()


@pytest.mark.ui
def test_hackrf_tuning_uses_rx_only_limits_and_updates_span_with_sample_rate(
    qt_app: QApplication,
) -> None:
    runtime = HardwareTuningRuntime(
        kind="hackrf",
        connection="HACKRF:0000000000000001",
        metrics={"tuning_profile_id": "hackrf_one_rx"},
    )
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.center.minimum() == pytest.approx(1.0)
    assert page.center.maximum() == pytest.approx(6_000.0)
    assert page.sample_rate.minimum() == pytest.approx(2.0)
    assert page.sample_rate.maximum() == pytest.approx(20.0)
    assert page.sample_rate.isEnabled()
    assert "только приём" in page.tuning_capability.text().lower()

    page.sample_rate.setValue(20.0)
    assert page.span.maximum() == pytest.approx(20_000.0)
    page.center.setValue(6_000.0)
    page.span.setValue(2_000.0)
    assert not page.apply_tuning()
    assert runtime.applied == []
    assert "всё выбранное окно" in page.state_notice.text_label.text().lower()
    page.close()


@pytest.mark.ui
def test_tinysa_ultra_uses_swept_limits_and_disables_sample_rate(
    qt_app: QApplication,
) -> None:
    runtime = HardwareTuningRuntime(
        kind="tinysa",
        connection="COM7",
        metrics={
            "detected_model": "ultra_plus_zs407",
            "ultra_mode_operator_confirmed": 1,
        },
    )
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.center.minimum() == pytest.approx(0.1)
    assert page.center.maximum() == pytest.approx(7_300.0)
    assert not page.sample_rate.isEnabled()
    explanation = page.tuning_capability.text().lower()
    assert "последовательный sweep" in explanation
    assert "зеркал" in explanation

    page.center.setValue(7_300.0)
    page.span.setValue(1.0)
    assert not page.apply_tuning()
    assert runtime.applied == []
    page.close()


@pytest.mark.ui
def test_operator_confirmed_blog_v4_exposes_hf_preset_and_applies_it(
    qt_app: QApplication,
) -> None:
    runtime = TuningRuntime()
    device = runtime.snapshot.devices[0]
    device.metrics.update(
        {
            "tuning_profile_id": "rtlsdr_blog_v4",
            "detected_tuning_profile_id": "rtlsdr_blog_v4",
            "profile_selection": "operator_confirmed",
            "usb_manufacturer": "RTLSDRBlog",
            "usb_product": "Blog V4",
        }
    )
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    index = page.guided_preset.findData("broadcast_am")
    assert index >= 0
    page.guided_preset.setCurrentIndex(index)
    page.apply_guided_tuning()

    assert len(runtime.applied) == 1
    spectrum = runtime.applied[0]["spectrum"]
    assert isinstance(spectrum, dict)
    assert spectrum["center_frequency_hz"] == 1_000_000
    assert spectrum["span_hz"] == 1_000_000
    assert "подтверждён драйвером" in page.guided_tuning_capability.text()
    page.close()


@pytest.mark.ui
def test_pending_guided_and_expert_tuning_survives_runtime_polling(
    qt_app: QApplication,
) -> None:
    runtime = TuningRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    guided_index = page.guided_preset.findData("broadcast_fm")
    assert guided_index >= 0
    page.guided_preset.setCurrentIndex(guided_index)
    guided_center = page.guided_center.value()
    guided_span = page.guided_span.value()
    assert page._guided_tuning_pending

    runtime.snapshot.spectrum.center_frequency_hz = 900_000_000
    runtime.snapshot.spectrum.span_hz = 1_000_000
    for _ in range(3):
        page.refresh(runtime.current_snapshot())
        qt_app.processEvents()

    assert page.guided_center.value() == pytest.approx(guided_center)
    assert page.guided_span.value() == pytest.approx(guided_span)
    page.apply_guided_tuning()
    guided_payload = runtime.applied[-1]["spectrum"]
    assert isinstance(guided_payload, dict)
    assert guided_payload["center_frequency_hz"] == round(
        guided_center * 1_000_000
    )
    assert guided_payload["span_hz"] == round(guided_span * 1_000)
    assert not page._guided_tuning_pending

    expert_index = page.preset_selector.findData("broadcast_fm")
    assert expert_index >= 0
    page.preset_selector.setCurrentIndex(expert_index)
    expert_center = page.center.value()
    expert_span = page.span.value()
    assert page._expert_tuning_pending

    runtime.snapshot.spectrum.center_frequency_hz = 1_200_000_000
    runtime.snapshot.spectrum.span_hz = 500_000
    for _ in range(3):
        page.refresh(runtime.current_snapshot())
        qt_app.processEvents()

    assert page.center.value() == pytest.approx(expert_center)
    assert page.span.value() == pytest.approx(expert_span)
    page.apply_tuning()
    expert_payload = runtime.applied[-1]["spectrum"]
    assert isinstance(expert_payload, dict)
    assert expert_payload["center_frequency_hz"] == round(
        expert_center * 1_000_000
    )
    assert expert_payload["span_hz"] == round(expert_span * 1_000)
    assert not page._expert_tuning_pending
    page.close()


@pytest.mark.ui
def test_live_empty_state_disables_actions_that_require_a_measured_frame(
    qt_app: QApplication,
) -> None:
    runtime = EmptyLiveRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert not page.record_button.isEnabled()
    assert not page.snapshot_button.isEnabled()
    assert "первого измеренного кадра" in page.record_button.toolTip()
    assert "первого измеренного кадра" in page.snapshot_button.toolTip()
    page.close()


@pytest.mark.ui
def test_guided_scan_controls_start_stop_and_explain_limits(
    qt_app: QApplication,
) -> None:
    runtime = ScanRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    preset_ids = {
        str(page.guided_scan_preset.itemData(index))
        for index in range(page.guided_scan_preset.count())
    }
    assert {
        "general_vhf",
        "general_uhf",
        "general_l_band",
        "general_s_band",
        "general_c_band",
        "general_wide",
        "full_supported",
    } <= preset_ids
    index = page.guided_scan_preset.findData("general_uhf")
    assert index >= 0
    page.guided_scan_preset.setCurrentIndex(index)
    page.start_guided_scan()

    assert runtime.started_scan_presets == ["general_uhf"]
    assert page.guided_scan_preset.currentData() == "general_uhf"
    assert page.expert_scan_preset.currentData() == "general_uhf"
    text = page.guided_scan_status.text().lower()
    assert "кадр 3/48" in text
    assert "≥ 00:03:15" in text
    assert "плановый минимум цикла" in page.guided_scan_status.toolTip()
    assert "покрытие 88%" in text
    assert "последовательно, не одновременно" in text
    assert "частота" in text and "не идентифицирует источник" in text
    assert not page.guided_scan_start_button.isEnabled()
    assert page.guided_scan_stop_button.isEnabled()
    assert "остановит активный автообзор" in page.guided_apply_button.toolTip()

    page.stop_scan_plan()
    assert runtime.stop_scan_calls == 1
    assert "автообзор выключен" in page.guided_scan_status.text().lower()
    assert page.guided_scan_start_button.isEnabled()
    assert not page.guided_scan_stop_button.isEnabled()
    page.close()


@pytest.mark.ui
def test_expert_manual_tuning_is_collapsed_but_can_be_opened(
    qt_app: QApplication,
) -> None:
    page = SpectrumPage(ScanRuntime(experience_level="expert"))

    assert page.expert_manual_tuning.isHidden()
    assert "показать" in page.expert_manual_toggle.text().lower()

    page.expert_manual_toggle.click()
    qt_app.processEvents()

    assert not page.expert_manual_tuning.isHidden()
    assert "скрыть" in page.expert_manual_toggle.text().lower()
    page.close()


@pytest.mark.ui
def test_active_scan_refresh_maps_runtime_plan_id_and_keeps_details_compact(
    qt_app: QApplication,
) -> None:
    runtime = ScanRuntime()
    status = runtime.start_scan_plan("general_s_band")
    status.estimated_cycle_ms = 100_000_000
    status.limitations_ru = (
        "Окна измеряются последовательно.",
        "Часть запроса исключена подтверждёнными границами приёмника.",
    )
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    assert page.guided_scan_preset.currentData() == "general_s_band"
    assert page.expert_scan_preset.currentData() == "general_s_band"
    assert "27:46:40" in page.guided_scan_status.text()
    assert "огр. 2" in page.guided_scan_status.text().lower()  # noqa: RUF001
    assert "часть запроса исключена" in (
        page.guided_scan_status.toolTip().lower()
    )
    assert page.guided_scan_status.heightForWidth(900) <= 64
    page.close()


@pytest.mark.ui
def test_expert_scan_status_shows_exact_window_dwell_and_failures(
    qt_app: QApplication,
) -> None:
    runtime = ScanRuntime(experience_level="expert")
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    page.expert_scan_preset.setCurrentIndex(
        page.expert_scan_preset.findData("general_uhf")
    )
    page.start_expert_scan()
    qt_app.processEvents()

    text = page.expert_scan_status.text().lower()
    assert "кадр 3/48" in text
    assert "300.000 мгц" in text
    assert "302.400 мгц" in text
    assert "выдержка 7/12 кадров" in text
    assert "источник: rtl-01" in page.expert_scan_status.toolTip().lower()
    assert "покрытие 88%" in text
    assert "круг 2" in text
    assert "сбои 1" in text
    page.close()


@pytest.mark.ui
def test_scan_status_keeps_last_frame_window_visible_during_retune(
    qt_app: QApplication,
) -> None:
    runtime = ScanRuntime(experience_level="expert")
    status = runtime.start_scan_plan("general_uhf")
    status.current_ordinal = 3
    status.current_window_id = "general_uhf-0003"
    status.current_window_label_ru = "UHF · следующий участок"
    status.start_frequency_hz = 302_160_000
    status.stop_frequency_hz = 304_560_000
    status.successful_frames_in_window = 0
    status.transition_pending = True
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    qt_app.processEvents()

    rendered = page.expert_scan_status.text().lower()
    assert "кадр 3/48" in rendered
    assert "300.000 мгц" in rendered
    assert "302.400 мгц" in rendered
    assert "выдержка завершена" in rendered
    assert "перестройка → окно 4/48" in rendered
    page.close()


@pytest.mark.ui
def test_scan_start_failure_is_visible_and_does_not_claim_activity(
    qt_app: QApplication,
) -> None:
    runtime = FailingScanRuntime()
    page = SpectrumPage(runtime)
    page.refresh(runtime.current_snapshot())
    page.start_guided_scan()
    qt_app.processEvents()

    assert page.header.status.property("statusLevel") == "critical"
    assert "аппаратный лимит окон" in page.state_notice.text_label.text()
    assert "автообзор выключен" in page.guided_scan_status.text().lower()
    assert page.guided_scan_start_button.isEnabled()
    assert not page.guided_scan_stop_button.isEnabled()
    page.close()
