"""Health, incident journal, and support actions."""

from __future__ import annotations

# ruff: noqa: RUF001
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
)

from ..multisensor_presenter import MultiSensorView, present_multisensor
from ..runtime import attr, call_runtime, current_snapshot, items, value_of
from ..theme import Colors
from ..widgets import InlineNotice, MetricTile, Panel
from .common import OperatorPage


class DiagnosticsPage(OperatorPage):
    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "Диагностика и журнал",
            "Причины, рекомендуемые действия и технический контекст",
            action_text="Обновить снимок",
        )
        self.header.action.clicked.connect(lambda: self.refresh())
        self._incidents: tuple[object, ...] = ()

        metrics = QHBoxLayout()
        self.healthy = MetricTile("Исправно", "0", accent=Colors.READY)
        self.degraded = MetricTile("Ограничено", "0", accent=Colors.WARNING)
        self.errors = MetricTile("Ошибки", "0", accent=Colors.CRITICAL)
        self.revision = MetricTile("Ревизия снимка", "0")
        metrics.addWidget(self.healthy)
        metrics.addWidget(self.degraded)
        metrics.addWidget(self.errors)
        metrics.addWidget(self.revision)
        self.root_layout.addLayout(metrics)

        self.sensor_panel = Panel(
            "Сенсорные контуры",
            subtitle="БЕЗ АТРИБУЦИИ ФИЗИЧЕСКОГО ИСТОЧНИКА",
            compact=True,
        )
        self.sensor_panel.setObjectName("multiSensorDiagnostics")
        self.sensor_table = QTableWidget(0, 3)
        self.sensor_table.setObjectName("multiSensorDiagnosticsTable")
        self.sensor_table.setHorizontalHeaderLabels(
            ["Контур", "Состояние", "Качество и роль"]
        )
        self.sensor_table.horizontalHeader().setStretchLastSection(True)
        self.sensor_table.verticalHeader().setVisible(False)
        self.sensor_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.sensor_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.sensor_table.setAlternatingRowColors(True)
        self.sensor_table.setMaximumHeight(150)
        self.sensor_panel.content_layout.addWidget(self.sensor_table)
        self.sensor_panel.hide()
        self.root_layout.addWidget(self.sensor_panel)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        incident_panel = Panel("Активные события", subtitle="Новые сверху")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Время", "Серьёзность", "Код", "Событие", "Источник"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._show_selected)
        incident_panel.content_layout.addWidget(self.table)
        splitter.addWidget(incident_panel)

        details = Panel("Разбор события", subtitle="Объяснение → следующее действие")
        self.title = QLabel("Выберите событие")
        self.title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.message = QLabel("Технические детали будут показаны здесь.")
        self.message.setWordWrap(True)
        self.action = InlineNotice(
            "Следующее действие",
            "Выберите событие в журнале.",
            level="info",
        )
        self.technical = QPlainTextEdit()
        self.technical.setReadOnly(True)
        self.technical.setPlaceholderText("Технический контекст отсутствует")
        details.content_layout.addWidget(self.title)
        details.content_layout.addWidget(self.message)
        details.content_layout.addWidget(self.action)
        details.content_layout.addWidget(self.technical, 1)
        button_row = QHBoxLayout()
        self.acknowledge = QPushButton("Подтвердить ознакомление")
        self.acknowledge.clicked.connect(self.acknowledge_selected)
        self.support_bundle = QPushButton("Сформировать support bundle")
        self.support_bundle.clicked.connect(self.export_support_bundle)
        self._can_acknowledge = callable(
            getattr(self.runtime, "acknowledge_incident", None)
        )
        self._can_export_bundle = callable(
            getattr(self.runtime, "export_support_bundle", None)
        )
        if not self._can_acknowledge:
            self.acknowledge.setToolTip(
                "Runtime не предоставляет acknowledge_incident(incident_id)."
            )
        if not self._can_export_bundle:
            self.support_bundle.setEnabled(False)
            self.support_bundle.setToolTip(
                "Runtime не предоставляет export_support_bundle()."
            )
        button_row.addWidget(self.acknowledge)
        button_row.addWidget(self.support_bundle)
        details.content_layout.addLayout(button_row)
        self.result = QLabel("")
        self.result.setProperty("secondary", "true")
        self.result.setWordWrap(True)
        details.content_layout.addWidget(self.result)
        splitter.addWidget(details)
        splitter.setSizes([760, 470])
        self.root_layout.addWidget(splitter, 1)

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        self._refresh_sensor_states(present_multisensor(snapshot))
        devices = items(snapshot, "devices")
        health_values = [value_of(attr(device, "health", "unknown")).lower() for device in devices]
        healthy = health_values.count("healthy")
        degraded = health_values.count("degraded")
        errors = sum(value in {"error", "critical"} for value in health_values)
        self.healthy.set_value(healthy, Colors.READY)
        self.degraded.set_value(degraded, Colors.WARNING)
        self.errors.set_value(errors, Colors.CRITICAL)
        self.revision.set_value(attr(snapshot, "revision", 0))

        self._incidents = items(snapshot, "incidents")
        self.table.setRowCount(len(self._incidents))
        for row, incident in enumerate(self._incidents):
            occurred = attr(incident, "occurred_at", "—")
            if hasattr(occurred, "strftime"):
                occurred = occurred.strftime("%H:%M:%S")
            severity = value_of(attr(incident, "severity", "info")).upper()
            acknowledgement = (
                " · ОЗНАКОМЛЕН"
                if bool(attr(incident, "acknowledged", False))
                else ""
            )
            values = (
                str(occurred),
                f"{severity}{acknowledgement}",
                str(attr(incident, "code", "—")),
                str(attr(incident, "title_ru", "Событие")),
                str(attr(incident, "source", "—")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    color = (
                        Colors.CRITICAL
                        if severity in {"ERROR", "CRITICAL"}
                        else Colors.WARNING
                        if severity == "WARNING"
                        else Colors.TEAL
                    )
                    item.setForeground(QBrush(QColor(color)))
                self.table.setItem(row, column, item)
        active = len(self._incidents)
        unacknowledged = sum(
            not bool(attr(item, "acknowledged", False))
            for item in self._incidents
        )
        critical = sum(
            value_of(attr(item, "severity")).lower() in {"critical", "error"}
            for item in self._incidents
        )
        if critical:
            critical_text = (
                "1 КРИТИЧЕСКОЕ АКТИВНОЕ СОБЫТИЕ"
                if critical == 1
                else f"{critical} КРИТИЧЕСКИХ АКТИВНЫХ СОБЫТИЙ"
            )
            self.header.status.set_status(
                critical_text,
                "critical",
            )
        elif errors:
            self.header.status.set_status("ОШИБКА УСТРОЙСТВА", "critical")
        elif active:
            suffix = (
                f" · {unacknowledged} БЕЗ ОЗНАКОМЛЕНИЯ"
                if unacknowledged
                else " · ОЗНАКОМЛЕНИЕ ПОДТВЕРЖДЕНО"
            )
            self.header.status.set_status(
                f"{active} АКТИВНЫХ СОБЫТИЙ{suffix}",
                "warning",
            )
        elif degraded:
            self.header.status.set_status("РАБОТА С ОГРАНИЧЕНИЯМИ", "warning")
        else:
            self.header.status.set_status("СИСТЕМА СТАБИЛЬНА", "ready")
        if self._incidents and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._show_selected()

    def _refresh_sensor_states(self, view: MultiSensorView) -> None:
        self.sensor_panel.setVisible(view.present)
        if not view.present:
            self.sensor_table.setRowCount(0)
            return
        self.sensor_panel.subtitle_label.setText(view.provenance)
        self.sensor_table.setRowCount(len(view.sensors))
        for row, sensor in enumerate(view.sensors):
            values = (sensor.title, sensor.state, sensor.detail)
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 1:
                    item.setForeground(
                        QBrush(QColor(_sensor_level_color(sensor.level)))
                    )
                self.sensor_table.setItem(row, column, item)
        self.sensor_table.resizeRowsToContents()

    def _selected_incident(self) -> object | None:
        row = self.table.currentRow()
        return self._incidents[row] if 0 <= row < len(self._incidents) else None

    def _show_selected(self) -> None:
        incident = self._selected_incident()
        if incident is None:
            self.title.setText("Выберите событие")
            self.message.setText("Активный технический контекст отсутствует.")
            self.technical.clear()
            self.acknowledge.setEnabled(False)
            self.acknowledge.setText("Подтвердить ознакомление")
            return
        acknowledged = bool(attr(incident, "acknowledged", False))
        acknowledgeable = bool(attr(incident, "acknowledgeable", True))
        self.acknowledge.setEnabled(
            self._can_acknowledge and acknowledgeable and not acknowledged
        )
        if not acknowledgeable:
            self.acknowledge.setText("Служебное событие")
        else:
            self.acknowledge.setText(
                "Ознакомление подтверждено"
                if acknowledged
                else "Подтвердить ознакомление"
            )
        self.title.setText(str(attr(incident, "title_ru", attr(incident, "code", "Событие"))))
        self.message.setText(str(attr(incident, "message_ru", "Описание отсутствует")))
        action_text = str(attr(incident, "action_ru", "Откройте технические детали"))
        labels = self.action.findChildren(QLabel)
        if labels:
            labels[-1].setText(f"<b>Следующее действие</b><br>{action_text}")
        technical = attr(incident, "technical", {}) or {}
        self.technical.setPlainText(
            "\n".join(f"{key}: {value}" for key, value in sorted(technical.items()))
            if isinstance(technical, dict)
            else str(technical)
        )

    def acknowledge_selected(self) -> None:
        incident = self._selected_incident()
        incident_id = attr(incident, "incident_id")
        if incident_id is None:
            return
        ok, result = call_runtime(self.runtime, "acknowledge_incident", str(incident_id))
        self.result.setText("Событие подтверждено" if ok else str(result))
        self.refresh()

    def export_support_bundle(self) -> None:
        ok, result = call_runtime(self.runtime, "export_support_bundle")
        self.result.setText(
            f"Support bundle подготовлен: {result}" if ok else str(result)
        )


def _sensor_level_color(level: str) -> str:
    return {
        "ready": Colors.READY,
        "info": Colors.TEAL,
        "warning": Colors.WARNING,
        "critical": Colors.CRITICAL,
    }.get(level, Colors.MUTED)
