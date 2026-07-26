"""Operational overview page."""

from __future__ import annotations

# ruff: noqa: RUF001
from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)

from ..multisensor_presenter import MultiSensorView, present_multisensor
from ..runtime import attr, items, provenance_key, value_of
from ..signal_presenter import present_signal_assessment
from ..theme import Colors
from ..widgets import MetricTile, Panel, SpectrumPlot
from .common import OperatorPage, device_level, device_state_ru, format_frequency


class DashboardPage(OperatorPage):
    open_page = Signal(str)

    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "Обзор раннего предупреждения",
            "Сенсорные контуры, качество данных и объяснимая корреляция",
            action_text="Обновить",
        )
        self.header.action.clicked.connect(lambda: self.refresh())
        self._guided_action_page = "devices"

        self.fusion_panel = Panel(
            "Гражданское раннее предупреждение",
            subtitle="ОЖИДАНИЕ МУЛЬТИСЕНСОРНЫХ ДАННЫХ",
        )
        self.fusion_panel.setObjectName("multiSensorEarlyWarningCard")
        self.fusion_headline = QLabel("Корреляция пока не рассчитана")
        self.fusion_headline.setObjectName("fusionHeadline")
        self.fusion_headline.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.fusion_headline.setWordWrap(True)
        self.fusion_summary = QLabel(
            "Общий вывод появится только после безопасной временной корреляции."
        )
        self.fusion_summary.setObjectName("fusionSummary")
        self.fusion_summary.setWordWrap(True)
        self.fusion_panel.content_layout.addWidget(self.fusion_headline)
        self.fusion_panel.content_layout.addWidget(self.fusion_summary)

        fusion_explanation = QGridLayout()
        fusion_explanation.setHorizontalSpacing(16)
        fusion_explanation.setVerticalSpacing(3)
        for column, caption in enumerate(
            ("КОРРЕЛЯЦИЯ", "КАЧЕСТВО ДАННЫХ", "НЕ ХВАТАЕТ")
        ):
            label = QLabel(caption)
            label.setProperty("muted", "true")
            fusion_explanation.addWidget(label, 0, column)
            fusion_explanation.setColumnStretch(column, 1)
        self.fusion_correlation = QLabel("Свежего решения ядра корреляции нет.")
        self.fusion_correlation.setObjectName("fusionCorrelation")
        self.fusion_quality = QLabel("Качество общего решения пока недоступно.")
        self.fusion_quality.setObjectName("fusionQuality")
        self.fusion_missing = QLabel(
            "Нужны свежие данные минимум от RF- и акустического контуров."
        )
        self.fusion_missing.setObjectName("fusionMissingConfirmation")
        for column, label in enumerate(
            (self.fusion_correlation, self.fusion_quality, self.fusion_missing)
        ):
            label.setWordWrap(True)
            fusion_explanation.addWidget(label, 1, column)
        self.fusion_panel.content_layout.addLayout(fusion_explanation)

        sensor_caption = QLabel("СЕНСОРНЫЕ КОНТУРЫ")
        sensor_caption.setProperty("muted", "true")
        self.fusion_panel.content_layout.addWidget(sensor_caption)
        sensor_row = QHBoxLayout()
        sensor_row.setSpacing(8)
        self.sensor_status_tiles: dict[str, MetricTile] = {}
        for key, title in (
            ("rf", "RF"),
            ("acoustic", "Акустика"),
            ("direction", "Направление"),
            ("civil_adsb", "Гражданский ADS-B"),
        ):
            tile = MetricTile(title, "НЕ НАСТРОЕН")
            tile.setObjectName(f"sensorStatus_{key}")
            tile.value_label.setWordWrap(True)
            tile.value_label.setMinimumWidth(0)
            self.sensor_status_tiles[key] = tile
            sensor_row.addWidget(tile, 1)
        self.fusion_panel.content_layout.addLayout(sensor_row)
        self.fusion_safety = QLabel(
            "Вывод описывает только активность сенсоров и не устанавливает "
            "физический источник или намерение."
        )
        self.fusion_safety.setObjectName("fusionSafetyLimit")
        self.fusion_safety.setProperty("secondary", "true")
        self.fusion_safety.setWordWrap(True)
        self.fusion_panel.content_layout.addWidget(self.fusion_safety)
        self.fusion_panel.hide()
        self.root_layout.addWidget(self.fusion_panel)

        self.guided_panel = Panel(
            "Что система видит сейчас",
            subtitle="Простое объяснение измерений",
        )
        self.guided_panel.setObjectName("guidedSignalCard")
        self.assessment_headline = QLabel("Измеренных данных пока нет")
        self.assessment_headline.setObjectName("assessmentHeadline")
        self.assessment_headline.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.assessment_headline.setWordWrap(True)
        self.assessment_observation = QLabel("Система ждёт данные от приёмника.")
        self.assessment_observation.setObjectName("assessmentObservation")
        self.assessment_observation.setWordWrap(True)
        self.assessment_coverage = QLabel(
            "Диапазон прослушивания появится после первого измеренного кадра."
        )
        self.assessment_coverage.setObjectName("assessmentCoverage")
        self.assessment_coverage.setProperty("secondary", "true")
        self.assessment_coverage.setWordWrap(True)
        self.guided_panel.content_layout.addWidget(self.assessment_headline)
        self.guided_panel.content_layout.addWidget(self.assessment_observation)
        self.guided_panel.content_layout.addWidget(self.assessment_coverage)

        assessment_details = QGridLayout()
        assessment_details.setSpacing(10)
        why_caption = QLabel("Почему система так пишет")
        why_caption.setProperty("muted", "true")
        self.assessment_reasons = QLabel("• Измеренный кадр ещё не получен.")
        self.assessment_reasons.setObjectName("assessmentReasons")
        self.assessment_reasons.setWordWrap(True)
        trust_caption = QLabel("Насколько можно доверять данным")
        trust_caption.setProperty("muted", "true")
        self.assessment_trust = QLabel("Предварительно: данных пока недостаточно.")
        self.assessment_trust.setObjectName("assessmentTrust")
        self.assessment_trust.setWordWrap(True)
        attribution_caption = QLabel("Можно ли определить физический источник?")
        attribution_caption.setProperty("muted", "true")
        self.assessment_attribution = QLabel(
            "Нет. Одного приёмника и спектра для этого недостаточно."
        )
        self.assessment_attribution.setObjectName("assessmentAttribution")
        self.assessment_attribution.setWordWrap(True)
        assessment_details.addWidget(why_caption, 0, 0)
        assessment_details.addWidget(self.assessment_reasons, 1, 0)
        assessment_details.addWidget(trust_caption, 0, 1)
        assessment_details.addWidget(self.assessment_trust, 1, 1)
        assessment_details.addWidget(attribution_caption, 2, 0)
        assessment_details.addWidget(self.assessment_attribution, 3, 0, 1, 2)
        assessment_details.setColumnStretch(0, 1)
        assessment_details.setColumnStretch(1, 1)
        self.guided_panel.content_layout.addLayout(assessment_details)

        readiness_caption = QLabel("Быстрый запуск")
        readiness_caption.setProperty("muted", "true")
        self.guided_checklist = QLabel(
            "1  Приёмник → 2  Измеренный кадр → 3  Интерпретация и события"
        )
        self.guided_checklist.setObjectName("guidedReadinessChecklist")
        self.guided_checklist.setWordWrap(True)
        guided_action_row = QHBoxLayout()
        self.assessment_action = QLabel("Проверьте подключение приёмника.")
        self.assessment_action.setObjectName("assessmentNextAction")
        self.assessment_action.setWordWrap(True)
        self.guided_next_button = QPushButton("Найти RTL-SDR")
        self.guided_next_button.setObjectName("guidedNextButton")
        self.guided_next_button.setProperty("primary", "true")
        self.guided_next_button.clicked.connect(self._open_guided_action)
        guided_action_row.addWidget(self.assessment_action, 1)
        guided_action_row.addWidget(self.guided_next_button)
        self.guided_panel.content_layout.addWidget(readiness_caption)
        self.guided_panel.content_layout.addWidget(self.guided_checklist)
        self.guided_panel.content_layout.addLayout(guided_action_row)
        self.root_layout.addWidget(self.guided_panel)

        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        readiness_panel = Panel("Готовность системы", compact=True)
        readiness_row = QHBoxLayout()
        self.readiness_value = QLabel("0%")
        self.readiness_value.setStyleSheet("font-size: 28px; font-weight: 600;")
        self.readiness_reason = QLabel("Ожидание снимка")
        self.readiness_reason.setProperty("secondary", "true")
        readiness_row.addWidget(self.readiness_value)
        readiness_row.addWidget(self.readiness_reason, 1)
        self.readiness_bar = QProgressBar()
        self.readiness_bar.setRange(0, 100)
        readiness_panel.content_layout.addLayout(readiness_row)
        readiness_panel.content_layout.addWidget(self.readiness_bar)
        metrics.addWidget(readiness_panel, 2)

        self.device_metric = MetricTile("Устройства", "0 / 0")
        self.incident_metric = MetricTile("Активные инциденты", "0")
        self.profile_metric = MetricTile("Профиль", "—", accent=Colors.TEAL)
        self.next_action_metric = MetricTile("Следующий шаг", "НАСТРОЙКА")
        metrics.addWidget(self.device_metric, 1)
        metrics.addWidget(self.incident_metric, 1)
        metrics.addWidget(self.profile_metric, 1)
        metrics.addWidget(self.next_action_metric, 2)
        self.root_layout.addLayout(metrics)

        content_grid = QGridLayout()
        content_grid.setSpacing(10)
        self.spectrum_panel = Panel(
            "Спектр в реальном времени",
            subtitle="Ожидание измеренного кадра",
        )
        self.spectrum_plot = SpectrumPlot()
        self.spectrum_panel.content_layout.addWidget(self.spectrum_plot)
        spectrum_actions = QHBoxLayout()
        spectrum_actions.addStretch(1)
        self.open_spectrum_button = QPushButton("Открыть спектр")
        self.open_spectrum_button.clicked.connect(lambda: self.open_page.emit("spectrum"))
        spectrum_actions.addWidget(self.open_spectrum_button)
        self.spectrum_panel.content_layout.addLayout(spectrum_actions)
        content_grid.addWidget(self.spectrum_panel, 0, 0, 2, 2)

        self.devices_panel = Panel("Устройства", subtitle="Активные источники")
        self.devices_table = QTableWidget(0, 2)
        self.devices_table.setHorizontalHeaderLabels(["Узел", "Состояние"])
        self.devices_table.horizontalHeader().setStretchLastSection(True)
        self.devices_table.verticalHeader().setVisible(False)
        self.devices_table.setAlternatingRowColors(True)
        self.devices_panel.content_layout.addWidget(self.devices_table)
        self.open_devices_button = QPushButton("Управление устройствами")
        self.open_devices_button.clicked.connect(lambda: self.open_page.emit("devices"))
        self.devices_panel.content_layout.addWidget(self.open_devices_button)
        content_grid.addWidget(self.devices_panel, 0, 2)

        self.incidents_panel = Panel(
            "Инциденты и рекомендации",
            subtitle="Причина → действие",
        )
        self.incidents = QListWidget()
        self.incidents_panel.content_layout.addWidget(self.incidents)
        self.open_diagnostics_button = QPushButton("Открыть диагностику")
        self.open_diagnostics_button.clicked.connect(
            lambda: self.open_page.emit("diagnostics")
        )
        self.incidents_panel.content_layout.addWidget(self.open_diagnostics_button)
        content_grid.addWidget(self.incidents_panel, 1, 2)
        content_grid.setColumnStretch(0, 4)
        content_grid.setColumnStretch(1, 4)
        content_grid.setColumnStretch(2, 4)
        self.root_layout.addLayout(content_grid, 1)

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            from ..runtime import current_snapshot

            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        multi_sensor = present_multisensor(snapshot)
        self._refresh_multisensor(multi_sensor)
        guided = str(attr(snapshot, "experience_level", "guided")).lower() != "expert"
        self.guided_panel.setVisible(guided and not multi_sensor.present)
        self.open_spectrum_button.setVisible(not guided)
        self.open_devices_button.setVisible(not guided)
        self.open_diagnostics_button.setVisible(not guided)
        # The guided dashboard is intentionally a short decision surface.
        # Dense plots and tables remain available on their dedicated pages and
        # would otherwise squeeze the explanation/trust/limitations out of a
        # 1120 x 720 production window.
        self.spectrum_panel.setVisible(not guided)
        self.devices_panel.setVisible(not guided)
        self.incidents_panel.setVisible(not guided)
        self.profile_metric.setVisible(not guided)
        self.next_action_metric.setVisible(not guided)
        if guided:
            self._refresh_guided_assessment(snapshot)
        incidents = items(snapshot, "incidents")
        critical_incident = next(
            (
                incident
                for incident in incidents
                if value_of(attr(incident, "severity")).lower()
                in {"critical", "error"}
            ),
            None,
        )
        assessment_state = value_of(
            attr(attr(snapshot, "signal_assessment"), "state", "no_data")
        ).lower()
        readiness = max(0, min(100, int(attr(snapshot, "readiness_percent", 0))))
        self.readiness_value.setText(f"{readiness}%")
        self.readiness_bar.setValue(readiness)
        if critical_incident is not None:
            level = "critical"
            status_text = "КРИТИЧЕСКИЙ ИНЦИДЕНТ"
        elif assessment_state == "data_unreliable":
            level = "critical"
            status_text = "КАЧЕСТВО ДАННЫХ СНИЖЕНО"
        else:
            level = "ready" if readiness >= 90 else "warning"
            status_text = (
                ("СИСТЕМА ГОТОВА" if readiness >= 90 else "НУЖНА НАСТРОЙКА")
                if guided
                else (
                    "RF-ЯДРО ГОТОВО"
                    if readiness >= 90
                    else "РАБОТА С ОГРАНИЧЕНИЯМИ"
                )
            )
        self.header.status.set_status(status_text, level)
        devices = items(snapshot, "devices")
        active_states = {"ready", "streaming", "starting"}
        active = sum(
            value_of(attr(device, "state")).lower() in active_states for device in devices
        )
        self.device_metric.set_value(f"{active} / {len(devices)}")
        active_incidents = list(incidents)
        incident_color = (
            Colors.CRITICAL
            if critical_incident is not None
            else Colors.WARNING
            if active_incidents
            else Colors.READY
        )
        self.incident_metric.set_value(len(active_incidents), incident_color)
        self.profile_metric.set_value(attr(snapshot, "profile_name", "Профиль не выбран"))
        frame = attr(snapshot, "spectrum")
        if critical_incident is not None:
            next_action = "Диагностика → разберите критический инцидент"
            readiness_reason = str(
                attr(
                    critical_incident,
                    "title_ru",
                    "Активный критический инцидент требует действия",
                )
            )
            next_color = Colors.CRITICAL
        elif not devices:
            next_action = "Устройства → Найти RTL-SDR"
            readiness_reason = "Подключите приёмник одним действием"
            next_color = Colors.WARNING
        elif frame is None:
            next_action = "Устройства → устраните ошибку"
            readiness_reason = "Приёмник ещё не выдаёт проверенный спектр"
            next_color = (
                Colors.CRITICAL
                if any(device_level(device) == "critical" for device in devices)
                else Colors.WARNING
            )
        else:
            assessment = attr(snapshot, "signal_assessment")
            assessment_state = value_of(attr(assessment, "state", "no_data")).lower()
            interpretation_ready = assessment is not None and assessment_state not in {
                "",
                "no_data",
            }
            if interpretation_ready:
                next_action = "События → наблюдайте интерпретацию и качество"
                readiness_reason = "Основной RF-мониторинг готов"
                next_color = Colors.READY
            else:
                next_action = "Спектр → дождитесь интерпретации кадра"
                readiness_reason = "Кадр получен; интерпретация ещё формируется"
                next_color = Colors.WARNING
        self.readiness_reason.setText(readiness_reason)
        self.next_action_metric.set_value(next_action, next_color)

        self.devices_table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            name = str(attr(device, "display_name", attr(device, "device_id", "Устройство")))
            state_item = QTableWidgetItem(device_state_ru(device))
            state_item.setForeground(
                QBrush(
                    QColor(
                        {
                            "ready": Colors.READY,
                            "warning": Colors.WARNING,
                            "critical": Colors.CRITICAL,
                            "neutral": Colors.MUTED,
                        }[device_level(device)]
                    )
                )
            )
            self.devices_table.setItem(row, 0, QTableWidgetItem(name))
            self.devices_table.setItem(row, 1, state_item)

        self.incidents.clear()
        if not active_incidents:
            item = QListWidgetItem("Активных инцидентов нет")
            item.setForeground(QBrush(QColor(Colors.READY)))
            self.incidents.addItem(item)
        for incident in active_incidents[:5]:
            title = str(attr(incident, "title_ru", attr(incident, "code", "Инцидент")))
            message = str(attr(incident, "message_ru", ""))
            action = str(attr(incident, "action_ru", "Откройте диагностику"))
            acknowledgement = (
                "\nОзнакомление подтверждено; причина остаётся активной."
                if bool(attr(incident, "acknowledged", False))
                else ""
            )
            entry = QListWidgetItem(
                f"{title}\n{message}\nДействие: {action}{acknowledgement}"
            )
            severity = value_of(attr(incident, "severity", "warning")).lower()
            entry.setForeground(
                QBrush(
                    QColor(
                        Colors.CRITICAL
                        if severity in {"error", "critical"}
                        else Colors.WARNING
                    )
                )
            )
            self.incidents.addItem(entry)

        if frame is not None:
            self.spectrum_panel.subtitle_label.setText(
                format_frequency(attr(frame, "center_frequency_hz"))
            )
            self.spectrum_plot.set_frame(frame)
        elif provenance_key(snapshot) in {"simulated", "demo"}:
            self.spectrum_panel.subtitle_label.setText("СИНТЕТИЧЕСКИЙ КАДР")
            revision = int(attr(snapshot, "revision", 0))
            self.spectrum_plot.set_demo_sequence(revision)
        else:
            self.spectrum_panel.subtitle_label.setText("ОЖИДАНИЕ ИЗМЕРЕННОГО КАДРА")
            self.spectrum_plot.clear()

    def _refresh_multisensor(self, view: MultiSensorView) -> None:
        self.fusion_panel.setVisible(view.present)
        if not view.present:
            return
        self.fusion_panel.subtitle_label.setText(view.provenance)
        self.fusion_headline.setText(view.headline)
        self.fusion_summary.setText(view.summary)
        self.fusion_correlation.setText(view.correlation)
        self.fusion_quality.setText(view.quality)
        self.fusion_missing.setText(view.missing)
        self.fusion_headline.setStyleSheet(
            "font-size: 22px; font-weight: 600; "
            f"color: {_level_color(view.level)};"
        )
        for sensor in view.sensors:
            tile = self.sensor_status_tiles.get(sensor.key)
            if tile is None:
                continue
            tile.set_value(sensor.state, _level_color(sensor.level))
            tile.setToolTip(sensor.detail)
            tile.value_label.setToolTip(sensor.detail)

    def _refresh_guided_assessment(self, snapshot: object | None) -> None:
        view = present_signal_assessment(snapshot)
        self.assessment_headline.setText(view.headline)
        self.assessment_observation.setText(view.observation)
        self.assessment_coverage.setText(view.coverage)
        self.assessment_reasons.setText(
            "\n".join(f"• {reason}" for reason in _guided_reason_summary(view.reasons))
        )
        self.assessment_trust.setText(view.trust)
        self.assessment_attribution.setText(view.attribution_answer)
        self.assessment_action.setText(view.next_action)

        devices = items(snapshot, "devices")
        frame = attr(snapshot, "spectrum")
        assessment = attr(snapshot, "signal_assessment")
        active_states = {"ready", "streaming", "starting"}
        receiver_ready = any(
            value_of(attr(device, "state")).lower() in active_states
            for device in devices
        )
        measured_ready = frame is not None
        assessment_state = value_of(attr(assessment, "state", "no_data")).lower()
        interpretation_ready = assessment is not None and assessment_state not in {
            "",
            "no_data",
        }
        self.guided_checklist.setText(
            "  →  ".join(
                (
                    _check_step(1, "Приёмник", receiver_ready),
                    _check_step(2, "Измеренный кадр", measured_ready),
                    _check_step(
                        3,
                        "Интерпретация и события",
                        interpretation_ready,
                    ),
                )
            )
        )

        if not devices:
            self._guided_action_page = "devices"
            self.guided_next_button.setText("Найти RTL-SDR")
        elif not receiver_ready or not measured_ready:
            self._guided_action_page = "devices"
            self.guided_next_button.setText("Проверить приёмник")
        elif not interpretation_ready:
            self._guided_action_page = "spectrum"
            self.guided_next_button.setText("Проверить интерпретацию")
        else:
            self._guided_action_page = "events"
            self.guided_next_button.setText("Открыть события")

        critical_incident = next(
            (
                incident
                for incident in items(snapshot, "incidents")
                if value_of(attr(incident, "severity")).lower()
                in {"critical", "error"}
            ),
            None,
        )
        if critical_incident is not None:
            self._guided_action_page = "diagnostics"
            self.guided_next_button.setText("Разобрать критический инцидент")
            action = str(
                attr(
                    critical_incident,
                    "action_ru",
                    "Откройте диагностику и устраните активную причину.",
                )
            )
            self.assessment_action.setText(f"Сначала: {action}")

    def _open_guided_action(self) -> None:
        self.open_page.emit(self._guided_action_page)


def _check_step(number: int, label: str, ready: bool) -> str:
    state = "готово" if ready else "нужно настроить"
    return f"{number}. {label}: {state}"


def _guided_reason_summary(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Keep the novice overview short; the complete chain stays in Events."""

    if not reasons:
        return ("Подробная цепочка решения доступна в разделе «События».",)

    selected: list[str] = [reasons[0]]
    for prefix in ("За:", "Против:", "Не хватает:", "Ограничение:"):
        match = next(
            (reason for reason in reasons[1:] if reason.startswith(prefix)),
            None,
        )
        if match is not None and match not in selected:
            selected.append(match)
        if len(selected) == 3:
            break
    for reason in reasons[1:]:
        if len(selected) == 3:
            break
        if reason not in selected:
            selected.append(reason)
    selected.append("Подробная цепочка решения доступна в разделе «События».")
    return tuple(selected)


def _level_color(level: str) -> str:
    return {
        "ready": Colors.READY,
        "info": Colors.TEAL,
        "warning": Colors.WARNING,
        "critical": Colors.CRITICAL,
    }.get(level, Colors.MUTED)
