"""Spectrum and waterfall operator page."""

from __future__ import annotations

from typing import cast

# ruff: noqa: RUF001
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from alga_vector.devices import (
    GENERAL_SCAN_PRESETS,
    HACKRF_ONE_PROFILE,
    CaptureTopology,
    HardwareTuningValidation,
    ReceiverHardwareProfile,
    TinySaModel,
    tinysa_hardware_profile,
)
from alga_vector.devices.tuning import (
    FREQUENCY_PRESETS,
    GENERIC_RTLSDR_PROFILE,
    FrequencyPreset,
    RtlSdrTuningProfile,
    TuningValidation,
    available_frequency_presets,
    rtlsdr_profile_by_id,
    validate_rtlsdr_tuning,
)

from ..runtime import (
    attr,
    call_runtime,
    current_snapshot,
    items,
    provenance_key,
    value_of,
)
from ..signal_presenter import present_signal_assessment
from ..theme import Colors
from ..widgets import InlineNotice, MetricTile, Panel, SpectrumDisplay
from .common import OperatorPage, format_frequency


def _spin(
    minimum: float,
    maximum: float,
    value: float,
    suffix: str,
    *,
    decimals: int = 3,
) -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    return widget


class SpectrumPage(OperatorPage):
    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "Спектр и водопад",
            "Инструментальный контроль RF-потока",
        )
        self._paused = False
        self._recording = False
        self._demo_sequence = 0
        self._rtl_profile: RtlSdrTuningProfile | None = None
        self._hardware_profile: ReceiverHardwareProfile | None = None
        self._preset_profile_key = ""
        self._syncing_tuning_controls = False
        self._guided_tuning_pending = False
        self._expert_tuning_pending = False
        self._scan_active = False
        self._experience_expert = False
        self._scan_preset_notes: dict[str, str] = {}

        self.guided_summary = Panel(
            "Что происходит в выбранном диапазоне",
            subtitle="Объяснение без технических терминов",
            compact=True,
        )
        self.guided_summary.setObjectName("guidedSpectrumSummary")
        self.guided_headline = QLabel("Измеренных данных пока нет")
        self.guided_headline.setObjectName("guidedSpectrumHeadline")
        self.guided_headline.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.guided_headline.setWordWrap(True)
        self.guided_coverage = QLabel(
            "Диапазон прослушивания появится после первого измеренного кадра."
        )
        self.guided_coverage.setObjectName("guidedListeningWindow")
        self.guided_coverage.setProperty("secondary", "true")
        self.guided_coverage.setWordWrap(True)
        self.guided_explanation = QLabel("Система ждёт данные от приёмника.")
        self.guided_explanation.setObjectName("guidedSpectrumExplanation")
        self.guided_explanation.setWordWrap(True)
        self.guided_trust = QLabel("Предварительно: данных пока недостаточно.")
        self.guided_trust.setObjectName("guidedSpectrumTrust")
        self.guided_trust.setWordWrap(True)
        self.guided_context = QLabel("")
        self.guided_context.setObjectName("guidedSpectrumContext")
        self.guided_context.setProperty("secondary", "true")
        self.guided_context.setWordWrap(True)
        self.guided_context.setVisible(False)
        self.guided_attribution = QLabel(
            "Можно ли установить физический источник? Нет — класс объекта "
            "по одному спектру не устанавливается."
        )
        self.guided_attribution.setObjectName("guidedSpectrumAttribution")
        self.guided_attribution.setProperty("secondary", "true")
        self.guided_attribution.setWordWrap(True)
        self.guided_action = QLabel("Проверьте подключение приёмника.")
        self.guided_action.setObjectName("guidedSpectrumAction")
        self.guided_action.setWordWrap(True)
        for summary_label in (
            self.guided_headline,
            self.guided_coverage,
            self.guided_explanation,
            self.guided_trust,
            self.guided_context,
            self.guided_attribution,
            self.guided_action,
        ):
            self.guided_summary.content_layout.addWidget(summary_label)
        self.root_layout.addWidget(self.guided_summary)

        self.guided_tuning = Panel(
            "Какой участок эфира смотреть",
            subtitle=(
                "Приёмник перестраивается по широкому диапазону, "
                "но измеряет только одно узкое окно за раз"
            ),
            compact=True,
        )
        guided_tuning_row = QHBoxLayout()
        guided_tuning_row.setContentsMargins(0, 0, 0, 0)
        guided_tuning_row.setSpacing(8)
        self.guided_preset = QComboBox()
        self.guided_center = _spin(0.500, 1_766.0, 433.920, " МГц")
        self.guided_span = _spin(1.0, 2_400.0, 2_000.0, " кГц", decimals=1)
        for label, widget in (
            ("Готовый участок", self.guided_preset),
            ("Центральная частота", self.guided_center),
            ("Одновременно видно", self.guided_span),
        ):
            block = QVBoxLayout()
            caption = QLabel(label)
            caption.setProperty("muted", "true")
            block.addWidget(caption)
            block.addWidget(widget)
            guided_tuning_row.addLayout(block)
        self.guided_apply_button = QPushButton("Перестроить приёмник")
        self.guided_apply_button.setProperty("primary", "true")
        self.guided_apply_button.clicked.connect(self.apply_guided_tuning)
        guided_tuning_row.addWidget(
            self.guided_apply_button,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        self.guided_manual_tuning = QWidget()
        self.guided_manual_tuning.setObjectName("guidedManualTuning")
        self.guided_manual_tuning.setLayout(guided_tuning_row)
        self.guided_manual_tuning.setVisible(False)
        self.guided_tuning.content_layout.addWidget(self.guided_manual_tuning)
        self.guided_tuning_capability = QLabel("")
        self.guided_tuning_capability.setWordWrap(True)
        self.guided_tuning_capability.setProperty("secondary", "true")
        self.guided_tuning.content_layout.addWidget(self.guided_tuning_capability)
        self.guided_preset_note = QLabel("")
        self.guided_preset_note.setWordWrap(True)
        self.guided_preset_note.setProperty("secondary", "true")
        self.guided_preset_note.setVisible(False)
        self.guided_tuning.content_layout.addWidget(self.guided_preset_note)
        guided_scan_row = QHBoxLayout()
        guided_scan_row.setSpacing(8)
        guided_scan_label = QLabel("Автообзор")
        guided_scan_label.setProperty("muted", "true")
        self.guided_scan_preset = QComboBox()
        self.guided_scan_preset.setMinimumWidth(270)
        self.guided_scan_start_button = QPushButton("Запустить обзор")
        self.guided_scan_start_button.clicked.connect(
            self.start_guided_scan
        )
        self.guided_scan_stop_button = QPushButton("Стоп")
        self.guided_scan_stop_button.clicked.connect(self.stop_scan_plan)
        self.guided_manual_toggle_button = QPushButton("Ручное окно")
        self.guided_manual_toggle_button.setCheckable(True)
        self.guided_manual_toggle_button.setToolTip(
            "Показать настройку одного фиксированного окна."
        )
        self.guided_manual_toggle_button.toggled.connect(
            self._toggle_guided_manual_tuning
        )
        guided_scan_row.addWidget(guided_scan_label)
        guided_scan_row.addWidget(self.guided_scan_preset, 1)
        guided_scan_row.addWidget(self.guided_scan_start_button)
        guided_scan_row.addWidget(self.guided_scan_stop_button)
        guided_scan_row.addWidget(self.guided_manual_toggle_button)
        self.guided_tuning.content_layout.addLayout(guided_scan_row)
        self.guided_scan_status = QLabel()
        self.guided_scan_status.setObjectName("guidedScanPlanStatus")
        self.guided_scan_status.setWordWrap(True)
        self.guided_scan_status.setProperty("secondary", "true")
        self.guided_tuning.content_layout.addWidget(self.guided_scan_status)
        self.guided_preset.currentIndexChanged.connect(
            self._guided_preset_changed
        )
        self.guided_center.valueChanged.connect(
            self._mark_guided_tuning_pending
        )
        self.guided_span.valueChanged.connect(
            self._mark_guided_tuning_pending
        )
        self.root_layout.addWidget(self.guided_tuning)

        self.controls = Panel("Управление потоком", compact=True)
        self.expert_manual_toggle = QPushButton(
            "Ручная настройка диапазона — показать"
        )
        self.expert_manual_toggle.setCheckable(True)
        self.expert_manual_toggle.setToolTip(
            "Показывает ручной центр, полосу, дискретизацию и команды записи."
        )
        self.expert_manual_toggle.toggled.connect(
            self._toggle_expert_manual_tuning
        )
        self.controls.content_layout.addWidget(
            self.expert_manual_toggle
        )
        self.expert_manual_tuning = QWidget()
        expert_manual_layout = QVBoxLayout(self.expert_manual_tuning)
        expert_manual_layout.setContentsMargins(0, 0, 0, 0)
        expert_manual_layout.setSpacing(8)
        self.expert_manual_tuning.setVisible(False)
        self.controls.content_layout.addWidget(
            self.expert_manual_tuning
        )
        preset_row = QHBoxLayout()
        preset_label = QLabel("Готовый гражданский участок")
        preset_label.setProperty("muted", "true")
        self.preset_selector = QComboBox()
        self.preset_selector.setMinimumWidth(330)
        self.preset_selector.currentIndexChanged.connect(
            self._expert_preset_changed
        )
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.preset_selector, 1)
        expert_manual_layout.addLayout(preset_row)
        control_grid = QGridLayout()
        control_grid.setHorizontalSpacing(8)
        control_grid.setVerticalSpacing(6)
        self.center = _spin(0.500, 1_766.0, 433.920, " МГц")
        self.span = _spin(1.0, 100_000.0, 2_000.0, " кГц", decimals=1)
        self.sample_rate = _spin(0.225001, 3.200, 2.400, " MSPS", decimals=6)
        self.input_selector = QComboBox()
        self.input_selector.addItem("Источник не настроен")
        self.input_selector.setEnabled(False)
        self.input_selector.setToolTip(
            "Активный источник выбирается ядром из проверенных настроенных адаптеров."
        )
        for column, (label, expert_widget) in enumerate((
            ("Центр", self.center),
            ("Полоса", self.span),
            ("Дискретизация", self.sample_rate),
            ("Активный источник", self.input_selector),
        )):
            caption = QLabel(label)
            caption.setProperty("muted", "true")
            control_grid.addWidget(caption, 0, column)
            control_grid.addWidget(expert_widget, 1, column)
            control_grid.setColumnStretch(column, 1)
        self.apply_tuning_button = QPushButton("Применить диапазон")
        self.apply_tuning_button.clicked.connect(self.apply_tuning)
        self.center.valueChanged.connect(self._mark_expert_tuning_pending)
        self.span.valueChanged.connect(self._mark_expert_tuning_pending)
        self.sample_rate.valueChanged.connect(self._sample_rate_changed)
        self.pause_button = QPushButton("Заморозить график")
        self.pause_button.setToolTip(
            "Замораживает только график и водопад; приём, анализ и запись продолжаются."
        )
        self.pause_button.clicked.connect(self.toggle_pause)
        self.record_button = QPushButton("Начать запись спектра")
        self.record_button.setProperty("primary", "true")
        self.record_button.clicked.connect(self.toggle_recording)
        self.snapshot_button = QPushButton("Снимок в буфер")
        self.snapshot_button.clicked.connect(self.copy_snapshot)
        for column, button in enumerate((
            self.apply_tuning_button,
            self.pause_button,
            self.record_button,
            self.snapshot_button,
        )):
            control_grid.addWidget(button, 2, column)
        expert_manual_layout.addLayout(control_grid)
        self.tuning_capability = QLabel("")
        self.tuning_capability.setWordWrap(True)
        self.tuning_capability.setProperty("secondary", "true")
        expert_manual_layout.addWidget(self.tuning_capability)
        expert_scan_row = QHBoxLayout()
        expert_scan_row.setSpacing(8)
        expert_scan_label = QLabel("Автообзор")
        expert_scan_label.setProperty("muted", "true")
        self.expert_scan_preset = QComboBox()
        self.expert_scan_preset.setMinimumWidth(330)
        self.expert_scan_start_button = QPushButton("Запустить обзор")
        self.expert_scan_start_button.clicked.connect(
            self.start_expert_scan
        )
        self.expert_scan_stop_button = QPushButton("Стоп")
        self.expert_scan_stop_button.clicked.connect(self.stop_scan_plan)
        expert_scan_row.addWidget(expert_scan_label)
        expert_scan_row.addWidget(self.expert_scan_preset, 1)
        expert_scan_row.addWidget(self.expert_scan_start_button)
        expert_scan_row.addWidget(self.expert_scan_stop_button)
        self.controls.content_layout.addLayout(expert_scan_row)
        self.expert_scan_status = QLabel()
        self.expert_scan_status.setObjectName("expertScanPlanStatus")
        self.expert_scan_status.setWordWrap(True)
        self.expert_scan_status.setProperty("secondary", "true")
        self.controls.content_layout.addWidget(self.expert_scan_status)
        self.root_layout.addWidget(self.controls)
        self._populate_scan_plan_controls()
        self._render_scan_plan_status(None)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        display_panel = Panel(
            "Рабочая область",
            subtitle="Текущий спектр · история водопада",
        )
        self.display = SpectrumDisplay()
        display_panel.content_layout.addWidget(self.display)
        self.state_notice = InlineNotice(
            "Ожидание данных",
            "Выберите приёмник или проверьте подключение устройства.",
            level="warning",
        )
        display_panel.content_layout.addWidget(self.state_notice)
        splitter.addWidget(display_panel)

        self.inspector = Panel("Инспектор", subtitle="Параметры обработки")
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_display_tab(), "Отображение")
        self.tabs.addTab(self._build_markers_tab(), "Маркеры")
        self.tabs.addTab(self._build_recording_tab(), "Запись")
        self.inspector.content_layout.addWidget(self.tabs)
        splitter.addWidget(self.inspector)
        splitter.setSizes([900, 310])
        self.root_layout.addWidget(splitter, 1)

        self.technical_status = QWidget()
        self.technical_status.setObjectName("technicalSpectrumMetrics")
        status_row = QHBoxLayout(self.technical_status)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.source_metric = MetricTile("Источник", "—", accent=Colors.TEAL)
        self.age_metric = MetricTile("Возраст данных", "—")
        self.drop_metric = MetricTile("Пропуски кадров", "0")
        self.adc_metric = MetricTile("Нагрузка АЦП", "—")
        status_row.addWidget(self.source_metric)
        status_row.addWidget(self.age_metric)
        status_row.addWidget(self.drop_metric)
        status_row.addWidget(self.adc_metric)
        self.root_layout.addWidget(self.technical_status)

    def _build_display_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.noise_threshold = _spin(-140.0, 0.0, -72.4, " dBFS", decimals=1)
        self.noise_threshold.valueChanged.connect(self.display.set_threshold)
        threshold_note = QLabel(
            "Порог влияет на линию индикации и сохраняется вместе с настройками диапазона."
        )
        threshold_note.setWordWrap(True)
        threshold_note.setProperty("secondary", "true")
        form.addRow("Порог индикации", self.noise_threshold)
        form.addRow("", threshold_note)
        return page

    def _build_markers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.marker_table = QTableWidget(3, 3)
        self.marker_table.setHorizontalHeaderLabels(["Маркер", "Частота", "Уровень"])
        self.marker_table.verticalHeader().setVisible(False)
        self.marker_table.horizontalHeader().setStretchLastSection(True)
        for row, marker in enumerate(("M1", "M2", "M3")):
            self.marker_table.setItem(row, 0, QTableWidgetItem(marker))
            self.marker_table.setItem(row, 1, QTableWidgetItem("—"))
            self.marker_table.setItem(row, 2, QTableWidgetItem("—"))
        reset = QPushButton("Сбросить маркеры")
        reset.clicked.connect(self.reset_markers)
        layout.addWidget(self.marker_table)
        layout.addWidget(reset)
        return page

    def _build_recording_tab(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        self.record_format = QLabel("ALGA Spectrum JSONL v1 · не RAW IQ")
        self.record_duration = QLabel("00:00:00")
        self.record_rate = QLabel("Запись не запущена")
        self.record_path = QLabel("Каталог данных / captures")
        self.record_path.setWordWrap(True)
        layout.addRow("Формат", self.record_format)
        layout.addRow("Длительность", self.record_duration)
        layout.addRow("Скорость", self.record_rate)
        layout.addRow("Каталог", self.record_path)
        return page

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        expert = str(attr(snapshot, "experience_level", "guided")).lower() == "expert"
        self._experience_expert = expert
        frame = attr(snapshot, "spectrum")
        assessment_state = value_of(
            attr(attr(snapshot, "signal_assessment"), "state", "no_data")
        ).lower()
        self.guided_summary.setVisible(not expert)
        self.guided_tuning.setVisible(not expert)
        self.controls.setVisible(expert)
        self.inspector.setVisible(expert)
        self.technical_status.setVisible(expert)
        self._render_scan_plan_status(attr(snapshot, "scan_plan"))
        self._syncing_tuning_controls = True
        try:
            self._refresh_tuning_capabilities(snapshot, frame)
        finally:
            self._syncing_tuning_controls = False
        if not expert:
            self.header.title.setText("Что приёмник слышит сейчас")
            self.header.subtitle.setText(
                "Измеренный эфир и простое объяснение текущего состояния"
            )
            view = present_signal_assessment(snapshot)
            self.guided_headline.setText(view.headline)
            self.guided_coverage.setText(view.coverage)
            self.guided_explanation.setText(view.observation)
            self.guided_trust.setText(f"Качество данных: {view.trust}")
            context: list[str] = []
            if view.alternatives:
                context.append(f"Альтернатива: {view.alternatives[0]}")
            if view.limitations:
                context.append(f"Ограничение: {view.limitations[0]}")
            self.guided_context.setText("\n".join(context))
            self.guided_context.setVisible(bool(context))
            self.guided_attribution.setText(
                f"Можно ли установить физический источник? {view.attribution_answer}"
            )
            self.guided_action.setText(f"Что делать: {view.next_action}")
        else:
            self.header.title.setText("Спектр и водопад")
            self.header.subtitle.setText("Инструментальный контроль RF-потока")
        self.tabs.setTabVisible(0, expert)
        self.tabs.setTabVisible(1, expert)
        self.tabs.setTabVisible(2, True)
        self._refresh_source_selector(snapshot, frame)
        simulated = provenance_key(snapshot) in {"simulated", "demo"}
        if not self._paused and frame is not None:
            self.display.set_frame(frame)
        elif not self._paused and simulated:
            self._demo_sequence += 1
            self.display.set_demo_sequence(self._demo_sequence)
        elif not self._paused:
            self.display.clear()

        if self._paused:
            self.header.status.set_status("ГРАФИК ЗАМОРОЖЕН", "warning")
            self.state_notice.set_notice(
                "График и водопад заморожены",
                "Приём, анализ событий и активная запись продолжаются.",
                level="warning",
            )
            self.state_notice.setVisible(True)
        elif frame is not None:
            frame_unit = str(attr(frame, "unit", "dBFS"))
            self.noise_threshold.setSuffix(f" {frame_unit}")
            dropped = int(attr(frame, "dropped_frames", 0))
            age_ms = int(attr(frame, "data_age_ms", 0))
            if assessment_state == "data_unreliable":
                self.header.status.set_status("КАЧЕСТВО ДАННЫХ СНИЖЕНО", "critical")
                self.state_notice.set_notice(
                    "Данным пока нельзя доверять",
                    str(
                        attr(
                            attr(snapshot, "signal_assessment"),
                            "operator_action_ru",
                            "Проверьте приёмник и дождитесь свежих непрерывных данных.",
                        )
                    ),
                    level="critical",
                )
                self.state_notice.setVisible(True)
            elif dropped > 0:
                self.header.status.set_status("ПОТОК С ПРОПУСКАМИ", "warning")
                self.state_notice.setVisible(False)
            else:
                self.header.status.set_status("ПОТОК АКТИВЕН", "ready")
                self.state_notice.setVisible(False)
            self.source_metric.set_value(attr(frame, "source_id", "Источник"))
            self.age_metric.set_value(f"{age_ms} мс")
            self.drop_metric.set_value(
                dropped, Colors.WARNING if dropped else Colors.READY
            )
            sample_rate_hz = _sample_rate_for_frame(self.runtime, snapshot, frame)
            frame_center_mhz = (
                float(attr(frame, "center_frequency_hz", 433_920_000))
                / 1_000_000
            )
            frame_span_khz = (
                float(attr(frame, "span_hz", 2_000_000)) / 1_000
            )
            self._syncing_tuning_controls = True
            try:
                if not self._expert_tuning_pending:
                    self.center.setValue(frame_center_mhz)
                    self.span.setValue(frame_span_khz)
                    if sample_rate_hz is not None:
                        self.sample_rate.setValue(sample_rate_hz / 1_000_000)
                if not self._guided_tuning_pending:
                    self.guided_center.setValue(frame_center_mhz)
                    self.guided_span.setValue(frame_span_khz)
            finally:
                self._syncing_tuning_controls = False
            peak = attr(frame, "peak_level", attr(frame, "peak_dbm"))
            if not isinstance(peak, (int, float)):
                power = attr(frame, "power_dbm")
                if hasattr(power, "tolist"):
                    power = power.tolist()
                if isinstance(power, (tuple, list)) and power:
                    peak = max(float(value) for value in power)
            if isinstance(peak, (int, float)):
                self._update_marker(
                    0,
                    attr(frame, "center_frequency_hz", 433_920_000),
                    float(peak),
                    frame_unit,
                )
        elif simulated:
            self.header.status.set_status("ДЕМО-ПОТОК", "warning")
            self.state_notice.setVisible(False)
            self.source_metric.set_value("Детерминированный генератор", Colors.WARNING)
            self.age_metric.set_value("0 мс")
            self.drop_metric.set_value("0", Colors.READY)
            self._update_marker(0, int(self.center.value() * 1_000_000), -48.0, "dBFS")
        else:
            self.header.status.set_status("ОЖИДАНИЕ ДАННЫХ", "warning")
            self.state_notice.setVisible(True)
            self.source_metric.set_value("Источник отключён", Colors.WARNING)
            self.age_metric.set_value("—")
            self.drop_metric.set_value("—")
        self.snapshot_button.setEnabled(frame is not None or simulated)
        self.snapshot_button.setToolTip(
            "Скопировать текущую измеренную визуализацию."
            if frame is not None or simulated
            else "Снимок станет доступен после первого измеренного кадра."
        )
        adc: object = "—"
        source_id = str(attr(frame, "source_id", ""))
        for device in items(snapshot, "devices"):
            if str(attr(device, "device_id", "")) != source_id:
                continue
            metrics = attr(device, "metrics", {}) or {}
            adc = attr(metrics, "adc_load_percent", "—")
            break
        if isinstance(adc, (int, float)):
            color = Colors.CRITICAL if adc >= 95 else Colors.WARNING if adc >= 85 else Colors.READY
            self.adc_metric.set_value(f"{adc:.0f}%", color)
        else:
            self.adc_metric.set_value(adc)
        self._refresh_recording_status(frame_available=frame is not None)

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_button.setText(
            "Продолжить график"
            if self._paused
            else "Заморозить график"
        )
        self.refresh()

    def _mark_guided_tuning_pending(self, *_args: object) -> None:
        if self._syncing_tuning_controls:
            return
        self._guided_tuning_pending = True
        self.guided_apply_button.setText("Применить выбранный участок")

    def _mark_expert_tuning_pending(self, *_args: object) -> None:
        if self._syncing_tuning_controls:
            return
        self._expert_tuning_pending = True
        self.apply_tuning_button.setText("Применить изменения")

    def _sample_rate_changed(self, *_args: object) -> None:
        self._mark_expert_tuning_pending()
        if self._syncing_tuning_controls:
            return
        if self._hardware_profile is not None:
            self._apply_hardware_tuning_capabilities(
                self._hardware_profile
            )
            return
        if self._rtl_profile is not None:
            maximum_span_khz = min(
                3_200.0,
                max(1.0, self.sample_rate.value() * 1_000),
            )
            self.span.setMaximum(maximum_span_khz)
            self.guided_span.setMaximum(maximum_span_khz)

    def _toggle_guided_manual_tuning(self, visible: bool) -> None:
        self.guided_manual_tuning.setVisible(visible)
        self.guided_manual_toggle_button.setText(
            "Скрыть ручное окно" if visible else "Ручное окно"
        )
        self.guided_manual_toggle_button.setToolTip(
            "Скрыть настройку одного фиксированного окна."
            if visible
            else "Показать настройку одного фиксированного окна."
        )

    def _toggle_expert_manual_tuning(self, visible: bool) -> None:
        self.expert_manual_tuning.setVisible(visible)
        self.expert_manual_toggle.setText(
            "Ручная настройка диапазона — скрыть"
            if visible
            else "Ручная настройка диапазона — показать"
        )

    def _clear_guided_tuning_pending(self) -> None:
        self._guided_tuning_pending = False
        self.guided_apply_button.setText("Перестроить приёмник")

    def _clear_expert_tuning_pending(self) -> None:
        self._expert_tuning_pending = False
        self.apply_tuning_button.setText("Применить диапазон")

    def _populate_scan_plan_controls(self) -> None:
        presets: tuple[object, ...] = GENERAL_SCAN_PRESETS
        try:
            runtime_presets = getattr(self.runtime, "scan_plan_presets", presets)
            if callable(runtime_presets):
                runtime_presets = runtime_presets()
            candidate = tuple(runtime_presets)
            if candidate:
                presets = candidate
        except (AttributeError, RuntimeError, TypeError, ValueError):
            presets = GENERAL_SCAN_PRESETS

        selected_ids = (
            str(self.guided_scan_preset.currentData() or ""),
            str(self.expert_scan_preset.currentData() or ""),
        )
        self._scan_preset_notes.clear()
        for combo, selected_id in zip(
            (self.guided_scan_preset, self.expert_scan_preset),
            selected_ids,
            strict=True,
        ):
            combo.blockSignals(True)
            combo.clear()
            for preset in presets:
                preset_id = str(attr(preset, "preset_id", "")).strip()
                label = str(attr(preset, "label_ru", preset_id)).strip()
                note = str(attr(preset, "note_ru", "")).strip()
                if not preset_id or not label:
                    continue
                combo.addItem(label, preset_id)
                self._scan_preset_notes[preset_id] = note
            combo.addItem(
                "Весь подтверждённый диапазон приёмника",
                "full_supported",
            )
            self._scan_preset_notes["full_supported"] = (
                "План строится только по подтверждённым возможностям приёмника. "
                "Слишком широкий или медленный план может быть отклонён."
            )
            selected_index = combo.findData(selected_id)
            combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
            combo.blockSignals(False)
        self.guided_scan_preset.currentIndexChanged.connect(
            self._scan_selection_changed
        )
        self.expert_scan_preset.currentIndexChanged.connect(
            self._scan_selection_changed
        )
        self._scan_selection_changed()

    def _scan_selection_changed(self, _index: int = -1) -> None:
        for combo in (self.guided_scan_preset, self.expert_scan_preset):
            preset_id = str(combo.currentData() or "")
            combo.setToolTip(self._scan_preset_notes.get(preset_id, ""))
        if not self._scan_active:
            self._render_scan_plan_status(None)

    def start_guided_scan(self) -> None:
        self._start_scan_plan(self.guided_scan_preset)

    def start_expert_scan(self) -> None:
        self._start_scan_plan(self.expert_scan_preset)

    def _start_scan_plan(self, combo: QComboBox) -> None:
        preset_id = str(combo.currentData() or "").strip()
        if not preset_id:
            self._show_scan_action_failure("План автообзора не выбран.")
            return
        ok, result = call_runtime(
            self.runtime,
            "start_scan_plan",
            preset_id,
        )
        if not ok:
            self._show_scan_action_failure(str(result))
            return
        self._sync_scan_plan_selection(preset_id)
        self._render_scan_plan_status(result)
        self.header.status.set_status("АВТООБЗОР ЗАПУЩЕН", "ready")

    def stop_scan_plan(self) -> None:
        ok, result = call_runtime(self.runtime, "stop_scan_plan")
        if not ok:
            self._show_scan_action_failure(str(result), stopping=True)
            return
        self._render_scan_plan_status(None)
        self.header.status.set_status("АВТООБЗОР ОСТАНОВЛЕН", "info")

    def _show_scan_action_failure(
        self,
        message: str,
        *,
        stopping: bool = False,
    ) -> None:
        action = "остановлен" if stopping else "запущен"
        self.header.status.set_status(
            f"АВТООБЗОР НЕ {action.upper()}",
            "critical",
        )
        self.state_notice.set_notice(
            f"Автообзор не {action}",
            message,
            level="critical",
        )
        self.state_notice.setVisible(True)

    def _sync_scan_plan_selection(self, preset_id: str) -> None:
        for combo in (self.guided_scan_preset, self.expert_scan_preset):
            index = combo.findData(preset_id)
            if index < 0 or index == combo.currentIndex():
                continue
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _render_scan_plan_status(self, status: object | None) -> None:
        active = bool(status is not None and attr(status, "active", True))
        self._scan_active = active
        action_available = callable(
            getattr(self.runtime, "start_scan_plan", None)
        ) and callable(getattr(self.runtime, "stop_scan_plan", None))
        for combo in (self.guided_scan_preset, self.expert_scan_preset):
            combo.setEnabled(action_available and not active)
        for button in (
            self.guided_scan_start_button,
            self.expert_scan_start_button,
        ):
            button.setEnabled(action_available and not active)
        for button in (
            self.guided_scan_stop_button,
            self.expert_scan_stop_button,
        ):
            button.setEnabled(action_available and active)

        manual_tooltip = (
            "Применение фиксированного окна остановит активный автообзор."
            if active
            else "Перестроить приёмник на одно фиксированное окно."
        )
        self.guided_apply_button.setToolTip(manual_tooltip)
        self.apply_tuning_button.setToolTip(manual_tooltip)

        disclaimer = (
            "Последовательно, не одновременно: короткие сигналы могут быть "
            "пропущены. Частота не идентифицирует источник."
        )
        if not active:
            if action_available:
                lead = (
                    "<b>Автообзор выключен.</b> "
                    "Выберите общий участок и запустите проверку."
                )
            else:
                lead = (
                    "<b>Автообзор недоступен.</b> "
                    "Текущий runtime не поддерживает управление планом."
                )
            text = f"{lead}<br>{disclaimer}"
            self.guided_scan_status.setText(text)
            self.expert_scan_status.setText(text)
            self.guided_scan_status.setToolTip(disclaimer)
            self.expert_scan_status.setToolTip(disclaimer)
            return

        plan_id = str(attr(status, "plan_id", ""))
        preset_id = (
            plan_id.removeprefix("preset_")
            if plan_id.startswith("preset_")
            else plan_id
        )
        self._sync_scan_plan_selection(preset_id)
        scheduled_ordinal = (
            max(0, int(attr(status, "current_ordinal", 0))) + 1
        )
        window_count = max(1, int(attr(status, "window_count", 1)))
        coverage = max(
            0.0,
            min(1.0, float(attr(status, "coverage_fraction", 0.0))),
        )
        cycle = _format_duration(
            float(attr(status, "estimated_cycle_ms", 0)) / 1_000
        )
        observed_ordinal_raw = attr(status, "observed_ordinal")
        observed_ordinal = (
            max(0, int(observed_ordinal_raw)) + 1
            if observed_ordinal_raw is not None
            else scheduled_ordinal
        )
        observed_label = attr(status, "observed_window_label_ru")
        label = str(
            observed_label
            or attr(status, "current_window_label_ru", "текущее окно")
        )
        transition_pending = bool(
            attr(status, "transition_pending", False)
        )
        transition_text = (
            f" · перестройка → окно {scheduled_ordinal}/{window_count}"
            if transition_pending
            else ""
        )
        waiting_text = (
            " · ожидание первого кадра"
            if attr(status, "observed_window_id") is None
            else ""
        )
        cycles = max(0, int(attr(status, "completed_cycles", 0)))
        limitations = tuple(
            str(message).strip()
            for message in attr(status, "limitations_ru", ())
            if str(message).strip()
        )
        limitation_count = len(limitations)
        limitation_suffix = (
            f" · огр. {limitation_count}"
            if limitation_count
            else ""
        )
        guided_text = (
            f"<b>Автообзор:</b> кадр {observed_ordinal}/{window_count} · "
            f"{label}{transition_text}{waiting_text} · ≥ {cycle} · "
            f"покрытие {coverage:.0%} · круг {cycles}"
            f"{limitation_suffix}.<br>"
            f"{disclaimer}"
        )
        start_hz = (
            attr(status, "observed_start_frequency_hz")
            or attr(status, "start_frequency_hz")
        )
        stop_hz = (
            attr(status, "observed_stop_frequency_hz")
            or attr(status, "stop_frequency_hz")
        )
        successful = max(
            0,
            int(attr(status, "successful_frames_in_window", 0)),
        )
        dwell = max(1, int(attr(status, "dwell_frames", 1)))
        failed = max(0, int(attr(status, "failed_windows", 0)))
        source_id = str(attr(status, "source_id", "") or "—")
        dwell_text = (
            "выдержка завершена"
            if transition_pending
            else f"выдержка {successful}/{dwell} кадров"
        )
        expert_text = (
            f"<b>Автообзор:</b> кадр {observed_ordinal}/{window_count} · "
            f"{format_frequency(start_hz)}–{format_frequency(stop_hz)} · "
            f"{dwell_text}{transition_text}{waiting_text} · ≥ {cycle} · "
            f"покрытие {coverage:.0%} · круг {cycles} · сбои {failed}"
            f"{limitation_suffix}.<br>{disclaimer}"
        )
        self.guided_scan_status.setText(guided_text)
        self.expert_scan_status.setText(expert_text)
        limitation_tooltip = "\n".join(
            (
                f"Источник: {source_id}",
                f"≥ {cycle} — плановый минимум цикла; фактический цикл "
                "может быть дольше.",
                disclaimer,
                *(
                    f"{index}. {message}"
                    for index, message in enumerate(limitations, start=1)
                ),
            )
        )
        self.guided_scan_status.setToolTip(limitation_tooltip)
        self.expert_scan_status.setToolTip(limitation_tooltip)

    def toggle_recording(self) -> None:
        target = not self._recording
        method = "start_recording" if target else "stop_recording"
        ok, result = call_runtime(self.runtime, method)
        if not ok:
            self.header.status.set_status("ОШИБКА ЗАПИСИ", "critical")
            self.record_rate.setText(str(result))
            return
        self._recording = target
        self.record_button.setText(
            "Остановить запись спектра" if target else "Начать запись спектра"
        )
        self.record_button.setProperty("danger", "true" if target else "false")
        self.record_button.setProperty("primary", "false" if target else "true")
        style = self.record_button.style()
        style.unpolish(self.record_button)
        style.polish(self.record_button)
        path = attr(result, "path", attr(result, "completed_path"))
        if path is not None:
            self.record_path.setText(str(path))
        if target:
            self.record_rate.setText("Ожидание первого измеренного кадра")
        else:
            frames = int(attr(result, "frames", 0))
            size = int(attr(result, "bytes_written", 0))
            self.record_rate.setText(
                f"Завершено · {frames} кадров · {_format_bytes(size)}"
            )
            self.record_duration.setText("00:00:00")

    def _refresh_tuning_capabilities(
        self,
        snapshot: object | None,
        frame: object | None,
    ) -> None:
        hardware_profile = _hardware_profile_for_snapshot(
            self.runtime,
            snapshot,
            frame,
        )
        self._hardware_profile = hardware_profile
        if hardware_profile is not None:
            self._rtl_profile = None
            self._apply_hardware_tuning_capabilities(hardware_profile)
            return
        profile = _rtlsdr_profile_for_snapshot(self.runtime, snapshot, frame)
        self._rtl_profile = profile
        if profile is None:
            minimum_mhz = 0.001
            maximum_mhz = 6_000.0
            self.center.setRange(minimum_mhz, maximum_mhz)
            self.guided_center.setRange(minimum_mhz, maximum_mhz)
            self.span.setMaximum(100_000.0)
            self.guided_span.setMaximum(100_000.0)
            self.sample_rate.setRange(0.008, 64.0)
            note = (
                "Диапазон и мгновенная полоса зависят от активного приёмника. "
                "Готовые пункты только перестраивают приёмник и не определяют "
                "тип наблюдаемого источника."
            )
            guided_note = note
            profile_key = "no-rtlsdr"
        else:
            minimum_mhz = profile.minimum_frequency_hz / 1_000_000
            maximum_mhz = profile.maximum_frequency_hz / 1_000_000
            self.center.setRange(minimum_mhz, maximum_mhz)
            self.guided_center.setRange(minimum_mhz, maximum_mhz)
            self.span.setMaximum(3_200.0)
            current_sample_khz = max(1.0, self.sample_rate.value() * 1_000)
            self.guided_span.setMaximum(min(3_200.0, current_sample_khz))
            self.sample_rate.setRange(0.225001, 3.200)
            hf_note = (
                "HF у Blog V4 принимается встроенным upconverter; "
                "Q-direct не включается."
                if profile.profile_id == "rtlsdr_blog_v4"
                else (
                    "HF использует подтверждённую Q-ветвь direct sampling."
                    if profile.profile_id == "rtlsdr_blog_v3_direct_q"
                    else (
                        "HF ниже 24 МГц заблокирован, пока совместимый "
                        "аппаратный вход не подтверждён."
                    )
                )
            )
            note = (
                "Общий диапазон перестройки: "
                f"{minimum_mhz:g}–{maximum_mhz:g} МГц. "
                f"Одновременно видно до {self.sample_rate.value():g} МГц "
                f"при {self.sample_rate.value():g} MSPS. Полный диапазон "
                "просматривается последовательной перестройкой, а не одновременно. "
                f"{hf_note}"
            )
            evidence = _rtlsdr_profile_evidence(snapshot, frame)
            if evidence:
                note = f"{note} {evidence}"
            guided_evidence = _rtlsdr_guided_evidence(snapshot, frame)
            guided_note = (
                f"Перестройка: {minimum_mhz:g}–{maximum_mhz:g} МГц. "
                f"За один кадр: до {self.sample_rate.value():g} МГц; "
                "полный диапазон — последовательной перестройкой, не одновременно."
            )
            if guided_evidence:
                guided_note = f"{guided_note} {guided_evidence}"
            profile_key = profile.profile_id
        self.guided_tuning_capability.setText(guided_note)
        self.tuning_capability.setText(note)
        if profile_key != self._preset_profile_key:
            presets = available_frequency_presets(profile)
            self._populate_preset_combo(self.guided_preset, presets)
            self._populate_preset_combo(self.preset_selector, presets)
            self._preset_profile_key = profile_key

    def _apply_hardware_tuning_capabilities(
        self,
        profile: ReceiverHardwareProfile,
    ) -> None:
        minimum_mhz = profile.minimum_frequency_hz / 1_000_000
        maximum_mhz = profile.maximum_frequency_hz / 1_000_000
        self.center.setRange(minimum_mhz, maximum_mhz)
        self.guided_center.setRange(minimum_mhz, maximum_mhz)
        if profile.capture_topology is CaptureTopology.IQ:
            minimum_sample_rate = profile.minimum_sample_rate_hz or 1
            maximum_sample_rate = profile.maximum_sample_rate_hz or minimum_sample_rate
            self.sample_rate.setEnabled(True)
            self.sample_rate.setRange(
                minimum_sample_rate / 1_000_000,
                maximum_sample_rate / 1_000_000,
            )
            instantaneous_hz = min(
                profile.maximum_instantaneous_span_hz or maximum_sample_rate,
                round(self.sample_rate.value() * 1_000_000),
            )
            maximum_span_khz = max(1.0, instantaneous_hz / 1_000)
            topology_note = (
                f"Одновременно видно до {maximum_span_khz / 1_000:g} МГц "
                f"при {self.sample_rate.value():g} MSPS."
            )
        else:
            self.sample_rate.setEnabled(False)
            maximum_span_khz = max(
                1.0,
                (
                    profile.maximum_frequency_hz
                    - profile.minimum_frequency_hz
                )
                / 1_000,
            )
            topology_note = (
                "Это последовательный sweep: точки полосы измеряются не "
                "одновременно, поэтому короткие и широкополосные эпизоды "
                "могут быть пропущены или искажены."
            )
        self.span.setMaximum(maximum_span_khz)
        self.guided_span.setMaximum(maximum_span_khz)
        caveats = tuple(
            band.caveat_ru
            for band in profile.tuning_bands
            if band.caveat_ru
        )
        note = (
            f"{profile.display_name_ru}. Подтверждённая перестройка: "
            f"{minimum_mhz:g}–{maximum_mhz:g} МГц. {topology_note}"
        )
        if caveats:
            note = f"{note} {caveats[0]}"
        self.tuning_capability.setText(note)
        self.guided_tuning_capability.setText(note)
        profile_key = profile.profile_id
        if profile_key != self._preset_profile_key:
            presets = _hardware_frequency_presets(profile)
            self._populate_preset_combo(self.guided_preset, presets)
            self._populate_preset_combo(self.preset_selector, presets)
            self._preset_profile_key = profile_key

    @staticmethod
    def _populate_preset_combo(
        combo: QComboBox,
        presets: tuple[FrequencyPreset, ...],
    ) -> None:
        selected_id = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Ручная частота", "")
        for preset in presets:
            combo.addItem(preset.label_ru, preset.preset_id)
        selected_index = combo.findData(selected_id)
        combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        combo.blockSignals(False)

    def _guided_preset_changed(self, _index: int = -1) -> None:
        preset = _preset_by_id(self.guided_preset.currentData())
        if preset is None:
            self.guided_preset_note.setText(
                "Введите центральную частоту вручную в пределах диапазона выше."
            )
            self.guided_preset_note.setVisible(False)
            return
        self.guided_center.setValue(preset.center_frequency_hz / 1_000_000)
        self.guided_span.setValue(preset.span_hz / 1_000)
        self.guided_preset_note.setText(
            f"{preset.note_ru} Это выбор участка, а не распознавание источника."
        )
        self.guided_preset_note.setVisible(True)
        self._mark_guided_tuning_pending()

    def _expert_preset_changed(self, _index: int = -1) -> None:
        preset = _preset_by_id(self.preset_selector.currentData())
        if preset is None:
            return
        self.center.setValue(preset.center_frequency_hz / 1_000_000)
        self.span.setValue(preset.span_hz / 1_000)
        self._mark_expert_tuning_pending()

    def apply_guided_tuning(self) -> None:
        self._syncing_tuning_controls = True
        try:
            self.center.setValue(self.guided_center.value())
            self.span.setValue(self.guided_span.value())
        finally:
            self._syncing_tuning_controls = False
        if self.apply_tuning():
            self._clear_guided_tuning_pending()

    def apply_tuning(self) -> bool:
        requested_center_hz = round(self.center.value() * 1_000_000)
        requested_span_hz = round(self.span.value() * 1_000)
        requested_sample_rate_hz = round(self.sample_rate.value() * 1_000_000)
        validation: HardwareTuningValidation | TuningValidation | None = None
        if self._hardware_profile is not None:
            validation = self._hardware_profile.validate_tuning(
                center_frequency_hz=requested_center_hz,
                span_hz=requested_span_hz,
                sample_rate_hz=(
                    requested_sample_rate_hz
                    if self._hardware_profile.capture_topology
                    is CaptureTopology.IQ
                    else None
                ),
            )
        elif _runtime_has_enabled_rtlsdr(self.runtime):
            validation = validate_rtlsdr_tuning(
                self._rtl_profile or GENERIC_RTLSDR_PROFILE,
                center_frequency_hz=requested_center_hz,
                span_hz=requested_span_hz,
                sample_rate_hz=requested_sample_rate_hz,
            )
        if validation is not None and not validation.accepted:
            self.header.status.set_status(
                "ДИАПАЗОН ВНЕ ВОЗМОЖНОСТЕЙ ПРИЁМНИКА",
                "critical",
            )
            self.state_notice.set_notice(
                "Диапазон не применён",
                " ".join(
                    part
                    for part in (
                        validation.message_ru,
                        validation.operator_action_ru,
                    )
                    if part
                ),
                level="critical",
            )
            self.state_notice.setVisible(True)
            return False
        payload = {
            "spectrum": {
                "center_frequency_hz": requested_center_hz,
                "span_hz": requested_span_hz,
                "sample_rate_hz": requested_sample_rate_hz,
                "threshold_level": float(self.noise_threshold.value()),
            }
        }
        ok, result = call_runtime(self.runtime, "update_settings", payload)
        self.header.status.set_status(
            "ДИАПАЗОН ПРИМЕНЁН" if ok else "ОШИБКА НАСТРОЙКИ",
            "ready" if ok else "critical",
        )
        if not ok:
            self.state_notice.set_notice(
                "Диапазон не применён",
                str(result),
                level="critical",
            )
            self.state_notice.setVisible(True)
            return False
        elif validation is not None and validation.warning_ru:
            self.state_notice.set_notice(
                "Диапазон применён с ограничением",
                validation.warning_ru,
                level="warning",
            )
            self.state_notice.setVisible(True)
        if self._scan_active:
            self._render_scan_plan_status(None)
        self._clear_expert_tuning_pending()
        return True

    def _refresh_source_selector(
        self,
        snapshot: object | None,
        frame: object | None,
    ) -> None:
        active_id = str(attr(frame, "source_id", ""))
        configured = items(snapshot, "devices")
        labels: list[tuple[str, str]] = []
        for device in configured:
            device_id = str(attr(device, "device_id", ""))
            display = str(attr(device, "display_name", device_id or "Устройство"))
            labels.append((device_id, display))
        existing = [
            (str(self.input_selector.itemData(index) or ""), self.input_selector.itemText(index))
            for index in range(self.input_selector.count())
        ]
        if labels != existing:
            self.input_selector.clear()
            if not labels:
                self.input_selector.addItem("Источник не настроен", "")
            else:
                for device_id, display in labels:
                    self.input_selector.addItem(display, device_id)
        if active_id:
            for index in range(self.input_selector.count()):
                if str(self.input_selector.itemData(index) or "") == active_id:
                    self.input_selector.setCurrentIndex(index)
                    break

    def _refresh_recording_status(self, *, frame_available: bool) -> None:
        ok, status = call_runtime(self.runtime, "recording_status")
        if not ok:
            self._recording = False
            self.record_button.setEnabled(False)
            self.record_button.setToolTip(str(status))
            return
        active = bool(attr(status, "active", False))
        self._recording = active
        self.record_button.setEnabled(active or frame_available)
        self.record_button.setToolTip(
            "Записываются обработанные кадры спектра; это не запись сырого IQ."
            if active or frame_available
            else "Запись станет доступна после первого измеренного кадра."
        )
        self.record_button.setText(
            "Остановить запись спектра" if active else "Начать запись спектра"
        )
        self.record_duration.setText(_format_duration(float(attr(status, "elapsed_seconds", 0))))
        if active:
            frames = int(attr(status, "frames", 0))
            size = int(attr(status, "bytes_written", 0))
            rate = float(attr(status, "bytes_per_second", 0))
            self.record_rate.setText(
                f"{frames} кадров · {_format_bytes(size)} · {_format_bytes(rate)}/с"
            )
            path = attr(status, "path")
            if path is not None:
                self.record_path.setText(str(path))

    def copy_snapshot(self) -> None:
        QApplication.clipboard().setPixmap(self.display.grab())
        self.header.status.set_status("СНИМОК СКОПИРОВАН", "info")

    def reset_markers(self) -> None:
        for row in range(self.marker_table.rowCount()):
            self.marker_table.setItem(row, 1, QTableWidgetItem("—"))
            self.marker_table.setItem(row, 2, QTableWidgetItem("—"))

    def _update_marker(
        self,
        row: int,
        frequency_hz: object,
        level: float,
        unit: str = "dBFS",
    ) -> None:
        self.marker_table.setItem(row, 1, QTableWidgetItem(format_frequency(frequency_hz)))
        self.marker_table.setItem(row, 2, QTableWidgetItem(f"{level:.1f} {unit}"))


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


def _sample_rate_for_frame(
    runtime: object | None,
    snapshot: object | None,
    frame: object,
) -> int | None:
    direct = attr(frame, "sample_rate_hz")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool) and direct > 0:
        return int(direct)
    source_id = str(attr(frame, "source_id", ""))
    for device in items(snapshot, "devices"):
        if str(attr(device, "device_id", "")) != source_id:
            continue
        sample_rate = attr(device, "sample_rate_hz")
        if (
            isinstance(sample_rate, (int, float))
            and not isinstance(sample_rate, bool)
            and sample_rate > 0
        ):
            return int(sample_rate)
    settings = _runtime_settings(runtime)
    configured = attr(attr(settings, "spectrum"), "sample_rate_hz")
    if (
        isinstance(configured, (int, float))
        and not isinstance(configured, bool)
        and configured > 0
    ):
        return int(configured)
    return None


def _hardware_profile_for_snapshot(
    runtime: object | None,
    snapshot: object | None,
    frame: object | None,
) -> ReceiverHardwareProfile | None:
    source_id = str(attr(frame, "source_id", "")).strip()
    devices = items(snapshot, "devices")
    selected = next(
        (
            device
            for device in devices
            if source_id
            and str(attr(device, "device_id", "")).strip() == source_id
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                device
                for device in devices
                if str(attr(device, "kind", "")).lower()
                in {"hackrf", "tinysa"}
                and not str(attr(device, "connection", "")).upper().startswith(
                    "SIM:"
                )
            ),
            None,
        )
    if selected is not None:
        kind = str(attr(selected, "kind", "")).lower()
        if kind == "hackrf":
            return HACKRF_ONE_PROFILE
        if kind == "tinysa":
            metrics = attr(selected, "metrics", {}) or {}
            detected = value_of(attr(metrics, "detected_model", "")).lower()
            if detected:
                try:
                    model = TinySaModel(detected)
                except ValueError:
                    model = None
                if model is not None:
                    return tinysa_hardware_profile(
                        model,
                        ultra_mode_enabled=bool(
                            attr(
                                metrics,
                                "ultra_mode_operator_confirmed",
                                False,
                            )
                        ),
                    )

    settings = _runtime_settings(runtime)
    adapters = attr(attr(settings, "devices"), "adapters", ())
    try:
        configured = tuple(adapters)
    except TypeError:
        configured = ()
    for adapter in configured:
        if not bool(attr(adapter, "enabled", False)):
            continue
        connection = str(attr(adapter, "connection", "")).upper()
        if connection.startswith("SIM:"):
            continue
        kind = str(attr(adapter, "kind", "")).lower()
        if kind == "hackrf":
            return HACKRF_ONE_PROFILE
        if kind != "tinysa":
            continue
        configured_model = value_of(
            attr(adapter, "tinysa_model", "auto")
        ).lower()
        if configured_model == "auto":
            continue
        try:
            model = TinySaModel(configured_model)
        except ValueError:
            continue
        return tinysa_hardware_profile(
            model,
            ultra_mode_enabled=bool(
                attr(adapter, "tinysa_ultra_mode", False)
            ),
        )
    return None


def _hardware_frequency_presets(
    profile: ReceiverHardwareProfile,
) -> tuple[FrequencyPreset, ...]:
    return tuple(
        preset
        for preset in FREQUENCY_PRESETS
        if any(
            band.supports_window(
                preset.center_frequency_hz,
                preset.span_hz,
            )
            for band in profile.tuning_bands
        )
    )


def _runtime_has_enabled_rtlsdr(runtime: object | None) -> bool:
    settings = _runtime_settings(runtime)
    for adapter in items(attr(settings, "devices"), "adapters"):
        if (
            str(attr(adapter, "kind", "")).lower() == "rtlsdr"
            and bool(attr(adapter, "enabled", True))
        ):
            return True
    snapshot = current_snapshot(runtime)
    return any(
        str(attr(device, "kind", "")).lower() == "rtlsdr"
        and str(attr(device, "state", "")).lower() not in {"disabled", "absent"}
        for device in items(snapshot, "devices")
    )


def _rtlsdr_profile_for_snapshot(
    runtime: object | None,
    snapshot: object | None,
    frame: object | None,
) -> RtlSdrTuningProfile | None:
    source_id = str(attr(frame, "source_id", ""))
    rtl_devices = [
        device
        for device in items(snapshot, "devices")
        if str(attr(device, "kind", "")).lower() == "rtlsdr"
        and str(attr(device, "state", "")).lower() not in {"disabled", "absent"}
    ]
    ordered = sorted(
        rtl_devices,
        key=lambda device: str(attr(device, "device_id", "")) != source_id,
    )
    for device in ordered:
        metrics = attr(device, "metrics", {}) or {}
        profile_id = attr(metrics, "tuning_profile_id")
        if profile_id:
            return rtlsdr_profile_by_id(profile_id)
    if rtl_devices or _runtime_has_enabled_rtlsdr(runtime):
        return GENERIC_RTLSDR_PROFILE
    return None


def _rtlsdr_profile_evidence(
    snapshot: object | None,
    frame: object | None,
) -> str:
    source_id = str(attr(frame, "source_id", ""))
    rtl_devices = [
        device
        for device in items(snapshot, "devices")
        if str(attr(device, "kind", "")).lower() == "rtlsdr"
    ]
    device = next(
        (
            item
            for item in rtl_devices
            if str(attr(item, "device_id", "")) == source_id
        ),
        rtl_devices[0] if rtl_devices else None,
    )
    if device is None:
        return ""
    metrics = attr(device, "metrics", {}) or {}
    selection = str(attr(metrics, "profile_selection", "automatic"))
    manufacturer = str(attr(metrics, "usb_manufacturer", "не прочитано"))
    product = str(attr(metrics, "usb_product", "не прочитано"))
    detected = rtlsdr_profile_by_id(
        attr(metrics, "detected_tuning_profile_id")
    ).display_name_ru
    if selection == "operator_confirmed":
        return (
            f"USB сообщает: {manufacturer} / {product}; драйвер определил: {detected}."
        )
    if selection == "operator_unconfirmed_fallback":
        return (
            f"USB сообщает: {manufacturer} / {product}. Драйвер не подтвердил "
            "Blog V4: HF отключён, используется безопасный тюнерный диапазон."
        )
    return f"USB сообщает: {manufacturer} / {product}; профиль выбран автоматически."


def _rtlsdr_guided_evidence(
    snapshot: object | None,
    frame: object | None,
) -> str:
    source_id = str(attr(frame, "source_id", ""))
    rtl_devices = [
        device
        for device in items(snapshot, "devices")
        if str(attr(device, "kind", "")).lower() == "rtlsdr"
    ]
    device = next(
        (
            item
            for item in rtl_devices
            if str(attr(item, "device_id", "")) == source_id
        ),
        rtl_devices[0] if rtl_devices else None,
    )
    if device is None:
        return ""
    metrics = attr(device, "metrics", {}) or {}
    product = str(attr(metrics, "usb_product", "описание не прочитано"))
    selection = str(attr(metrics, "profile_selection", "automatic"))
    source = {
        "operator_confirmed": "выбран оператором и подтверждён драйвером",
        "operator_unconfirmed_fallback": "Blog V4 не подтверждён; HF отключён",
    }.get(selection, "авто")
    return f"USB: {product}; профиль: {source}."


def _preset_by_id(preset_id: object) -> FrequencyPreset | None:
    selected = str(preset_id or "")
    return next(
        (preset for preset in FREQUENCY_PRESETS if preset.preset_id == selected),
        None,
    )


def _runtime_settings(runtime: object | None) -> object | None:
    getter = getattr(runtime, "settings_snapshot", None)
    if callable(getter):
        try:
            return cast(object | None, getter())
        except Exception:
            return cast(object | None, attr(runtime, "config"))
    return cast(object | None, attr(runtime, "config"))


def _format_bytes(value: float | int) -> str:
    size = max(0.0, float(value))
    for suffix in ("Б", "КиБ", "МиБ", "ГиБ"):
        if size < 1024.0 or suffix == "ГиБ":
            return f"{size:.1f} {suffix}"
        size /= 1024.0
    return f"{size:.1f} ГиБ"
