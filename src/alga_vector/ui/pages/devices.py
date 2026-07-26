"""SDR inventory and recovery actions."""

from __future__ import annotations

# ruff: noqa: RUF001
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..runtime import attr, call_runtime, current_snapshot, items, value_of
from ..theme import Colors
from ..widgets import InlineNotice, Panel, StatusBadge
from .common import OperatorPage, device_level, device_state_ru, format_frequency

_RECONNECT_RECOVERED_STATES = frozenset({"ready", "streaming"})
_RECONNECT_BLOCKED_STATES = frozenset({"disabled", "stopping"})
_DISCOVERY_FAILURE_STATES = frozenset({"unavailable", "failed", "timed_out"})


def _device_from_result(result: object, device_id: str) -> object | None:
    candidates = items(result, "devices")
    if not candidates and str(attr(result, "device_id", "")) == device_id:
        candidates = (result,)
    return next(
        (device for device in candidates if str(attr(device, "device_id", "")) == device_id),
        None,
    )


def _device_failure_text(device: object | None) -> str:
    if device is None:
        return "Runtime не вернул состояние выбранного устройства."
    state = device_state_ru(device)
    reason = str(attr(device, "reason_ru", "")).strip()
    action = str(attr(device, "recommended_action_ru", "")).strip()
    parts = [f"Состояние после действия: {state}."]
    if reason:
        parts.append(reason)
    if action:
        parts.append(f"Действие: {action}")
    return " ".join(parts)


def _discovery_issue_text(result: object) -> str:
    issues = items(result, "issues")
    if not issues:
        return ""
    issue = issues[0]
    message = str(attr(issue, "message_ru", "")).strip()
    action = str(attr(issue, "operator_action_ru", "")).strip()
    if action:
        return f"{message} Действие: {action}".strip()
    return message


class DevicesPage(OperatorPage):
    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "Устройства",
            "Поиск, подключение и проверка RF-приёмников",
            action_text="Проверить настроенные",
        )
        self.header.action.clicked.connect(self.rescan)
        self._devices: tuple[object, ...] = ()
        self._discovered_rtlsdr: tuple[object, ...] = ()
        self._discovered_hackrf: tuple[object, ...] = ()
        self._discovered_tinysa: tuple[object, ...] = ()

        summary = QHBoxLayout()
        self.total = StatusBadge("0 НАСТРОЕНО", "neutral")
        self.active = StatusBadge("0 АКТИВНО", "ready")
        self.problem = StatusBadge("0 ТРЕБУЮТ ДЕЙСТВИЯ", "warning")
        summary.addWidget(self.total)
        summary.addWidget(self.active)
        summary.addWidget(self.problem)
        summary.addStretch(1)
        self.root_layout.addLayout(summary)

        discovery = Panel(
            "Автообнаружение приёмников",
            subtitle="Без скрытого открытия неизвестных устройств",
            compact=True,
        )
        discovery.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.discovery_tabs = QTabWidget()
        rtl_tab = QWidget()
        rtl_layout = QVBoxLayout(rtl_tab)
        rtl_layout.setContentsMargins(8, 8, 8, 8)
        rtl_layout.setSpacing(8)
        discovery_controls = QHBoxLayout()
        discovery_controls.setSpacing(8)
        self.discover_rtlsdr_button = QPushButton("Найти RTL-SDR")
        self.discover_rtlsdr_button.setProperty("primary", "true")
        self.discover_rtlsdr_button.clicked.connect(self.discover_rtlsdr_devices)
        self.discovered_rtlsdr_select = QComboBox()
        self.discovered_rtlsdr_select.setMinimumWidth(280)
        self.discovered_rtlsdr_select.setPlaceholderText("Сначала выполните поиск")
        self.discovered_rtlsdr_select.currentIndexChanged.connect(
            self._update_discovered_rtlsdr_action
        )
        self.add_discovered_rtlsdr_button = QPushButton("Добавить и включить")
        self.add_discovered_rtlsdr_button.clicked.connect(self.add_discovered_rtlsdr_device)
        self._can_discover_rtlsdr = callable(getattr(self.runtime, "discover_rtlsdr_devices", None))
        self._can_add_rtlsdr = callable(getattr(self.runtime, "add_discovered_rtlsdr_device", None))
        self.discover_rtlsdr_button.setEnabled(self._can_discover_rtlsdr)
        self.add_discovered_rtlsdr_button.setEnabled(False)
        if not self._can_discover_rtlsdr:
            self.discover_rtlsdr_button.setToolTip(
                "Runtime не предоставляет безопасный поиск RTL-SDR."
            )
        if not self._can_add_rtlsdr:
            self.add_discovered_rtlsdr_button.setToolTip(
                "Runtime не предоставляет добавление найденного RTL-SDR."
            )
        discovery_controls.addWidget(self.discover_rtlsdr_button)
        discovery_controls.addWidget(self.discovered_rtlsdr_select, 1)
        discovery_controls.addWidget(self.add_discovered_rtlsdr_button)
        rtl_layout.addLayout(discovery_controls)
        self.rtlsdr_discovery_notice = InlineNotice(
            "RTL-SDR ещё не искали",
            (
                "Подключите приёмник и нажмите «Найти RTL-SDR». "
                "Поиск проверяет только USB-обнаружение и не означает, "
                "что поток уже запущен."
            ),
            level="info",
        )
        rtl_layout.addWidget(self.rtlsdr_discovery_notice)
        self.discovery_tabs.addTab(rtl_tab, "RTL-SDR")

        hackrf_tab = QWidget()
        hackrf_layout = QVBoxLayout(hackrf_tab)
        hackrf_layout.setContentsMargins(8, 8, 8, 8)
        hackrf_layout.setSpacing(8)
        hackrf_controls = QHBoxLayout()
        self.discover_hackrf_button = QPushButton("Найти HackRF")
        self.discover_hackrf_button.setProperty("primary", "true")
        self.discover_hackrf_button.clicked.connect(self.discover_hackrf_devices)
        self.discovered_hackrf_select = QComboBox()
        self.discovered_hackrf_select.setMinimumWidth(280)
        self.discovered_hackrf_select.setPlaceholderText(
            "PortaPack должен быть в HackRF USB mode"
        )
        self.discovered_hackrf_select.currentIndexChanged.connect(
            self._update_discovered_hackrf_action
        )
        self.add_discovered_hackrf_button = QPushButton("Добавить и включить")
        self.add_discovered_hackrf_button.clicked.connect(
            self.add_discovered_hackrf_device
        )
        self._can_discover_hackrf = callable(
            getattr(self.runtime, "discover_hackrf_devices", None)
        )
        self._can_add_hackrf = callable(
            getattr(self.runtime, "add_discovered_hackrf_device", None)
        )
        self.discover_hackrf_button.setEnabled(self._can_discover_hackrf)
        self.add_discovered_hackrf_button.setEnabled(False)
        hackrf_controls.addWidget(self.discover_hackrf_button)
        hackrf_controls.addWidget(self.discovered_hackrf_select, 1)
        hackrf_controls.addWidget(self.add_discovered_hackrf_button)
        hackrf_layout.addLayout(hackrf_controls)
        self.hackrf_discovery_notice = InlineNotice(
            "HackRF ещё не искали",
            (
                "Поиск вызывает официальный hackrf_info с тайм-аутом. "
                "Приложение поддерживает только приём; TX-команды отсутствуют."
            ),
            level="info",
        )
        hackrf_layout.addWidget(self.hackrf_discovery_notice)
        self.discovery_tabs.addTab(hackrf_tab, "HackRF / PortaPack")

        tinysa_tab = QWidget()
        tinysa_layout = QVBoxLayout(tinysa_tab)
        tinysa_layout.setContentsMargins(8, 8, 8, 8)
        tinysa_layout.setSpacing(8)
        tinysa_controls = QHBoxLayout()
        self.discover_tinysa_button = QPushButton("Найти tinySA")
        self.discover_tinysa_button.setProperty("primary", "true")
        self.discover_tinysa_button.clicked.connect(self.discover_tinysa_devices)
        self.discovered_tinysa_select = QComboBox()
        self.discovered_tinysa_select.setMinimumWidth(280)
        self.discovered_tinysa_select.setPlaceholderText(
            "Читаются только системные описания COM"
        )
        self.discovered_tinysa_select.currentIndexChanged.connect(
            self._update_discovered_tinysa_action
        )
        self.add_discovered_tinysa_button = QPushButton("Подтвердить и включить")
        self.add_discovered_tinysa_button.clicked.connect(
            self.add_discovered_tinysa_device
        )
        self._can_discover_tinysa = callable(
            getattr(self.runtime, "discover_tinysa_devices", None)
        )
        self._can_add_tinysa = callable(
            getattr(self.runtime, "add_discovered_tinysa_device", None)
        )
        self.discover_tinysa_button.setEnabled(self._can_discover_tinysa)
        self.add_discovered_tinysa_button.setEnabled(False)
        tinysa_controls.addWidget(self.discover_tinysa_button)
        tinysa_controls.addWidget(self.discovered_tinysa_select, 1)
        tinysa_controls.addWidget(self.add_discovered_tinysa_button)
        tinysa_layout.addLayout(tinysa_controls)
        self.tinysa_discovery_notice = InlineNotice(
            "tinySA ещё не искали",
            (
                "Поиск не открывает COM-порты. Неоднозначный USB Serial "
                "показывается как кандидат и требует явного подтверждения."
            ),
            level="info",
        )
        tinysa_layout.addWidget(self.tinysa_discovery_notice)
        self.discovery_tabs.addTab(tinysa_tab, "tinySA")

        discovery.content_layout.addWidget(self.discovery_tabs)
        self.root_layout.addWidget(discovery)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        inventory = Panel("Список SDR-узлов", subtitle="Только явно заданные подключения")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Устройство", "Подключение", "Поток", "Температура", "Состояние"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._update_inspector)
        inventory.content_layout.addWidget(self.table)
        self.degradation_notice = InlineNotice(
            "Работа с ограничениями",
            "Недоступность одного приёмника не блокирует остальные функции.",
            level="warning",
        )
        inventory.content_layout.addWidget(self.degradation_notice)
        splitter.addWidget(inventory)

        inspector = Panel("Инспектор устройства", subtitle="Выберите строку слева")
        self.inspector_title = QLabel("Устройство не выбрано")
        self.inspector_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.inspector_status = StatusBadge("ОЖИДАНИЕ", "neutral")
        title_row = QHBoxLayout()
        title_row.addWidget(self.inspector_title, 1)
        title_row.addWidget(self.inspector_status)
        inspector.content_layout.addLayout(title_row)
        details_container = QWidget()
        self.details = QFormLayout(details_container)
        self.details.setContentsMargins(0, 0, 4, 0)
        self.details.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.detail_values: dict[str, QLabel] = {}
        for key, caption in (
            ("id", "Стабильный ID"),
            ("kind", "Тип"),
            ("connection", "Подключение"),
            ("driver", "Драйвер"),
            ("usb_identity", "USB-описание"),
            ("tuning_profile", "RF-профиль"),
            ("tuning_range", "Диапазон перестройки"),
            ("sample_rate", "Частота дискретизации"),
            ("frequency", "Центральная частота"),
            ("sync", "Синхронизация"),
            ("temperature", "Температура"),
            ("reason", "Причина"),
            ("action", "Рекомендация"),
        ):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.details.addRow(caption, value)
            self.detail_values[key] = value
        details_scroll = QScrollArea()
        details_scroll.setObjectName("deviceDetailsScroll")
        details_scroll.setWidgetResizable(True)
        details_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        details_scroll.setWidget(details_container)
        details_scroll.setStyleSheet(
            "QScrollArea#deviceDetailsScroll { border: 0; background: transparent; }"
        )
        inspector.content_layout.addWidget(details_scroll, 1)
        action_row = QHBoxLayout()
        self.reconnect_button = QPushButton("Переподключить")
        self.reconnect_button.setProperty("primary", "true")
        self.reconnect_button.clicked.connect(self.reconnect_selected)
        self.calibrate_button = QPushButton("Калибровать")
        self.calibrate_button.clicked.connect(self.calibrate_selected)
        self.disconnect_button = QPushButton("Отключить")
        self.disconnect_button.setProperty("danger", "true")
        self.disconnect_button.clicked.connect(self.disconnect_selected)
        self._can_reconnect = callable(getattr(self.runtime, "reconnect", None))
        self._can_calibrate = callable(getattr(self.runtime, "calibrate", None))
        self._can_disconnect = callable(getattr(self.runtime, "disconnect", None))
        if not self._can_reconnect:
            self.reconnect_button.setToolTip("Runtime не предоставляет reconnect(device_id).")
        if not self._can_calibrate:
            self.calibrate_button.setToolTip("Runtime не предоставляет calibrate(device_id).")
        if not self._can_disconnect:
            self.disconnect_button.setToolTip("Runtime не предоставляет disconnect(device_id).")
        self.calibrate_button.setVisible(self._can_calibrate)
        self.disconnect_button.setVisible(self._can_disconnect)
        action_row.addWidget(self.reconnect_button)
        action_row.addWidget(self.calibrate_button)
        action_row.addWidget(self.disconnect_button)
        inspector.content_layout.addLayout(action_row)
        self.action_result = QLabel("")
        self.action_result.setProperty("secondary", "true")
        self.action_result.setWordWrap(True)
        inspector.content_layout.addWidget(self.action_result)
        splitter.addWidget(inspector)
        splitter.setSizes([760, 430])
        self.root_layout.addWidget(splitter, 1)

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        expert = str(attr(snapshot, "experience_level", "guided")).lower() == "expert"
        for column in (1, 2, 3):
            self.table.setColumnHidden(column, not expert)
        self._devices = items(snapshot, "devices")
        previous_id = self._selected_device_id()
        self.table.setRowCount(len(self._devices))
        active_count = 0
        problem_count = 0
        for row, device in enumerate(self._devices):
            level = device_level(device)
            if level == "ready":
                active_count += 1
            elif level in {"warning", "critical"}:
                problem_count += 1
            metrics = attr(device, "metrics", {}) or {}
            temperature = attr(metrics, "temperature_c", attr(metrics, "temperature", "—"))
            sample_rate = attr(device, "sample_rate_hz")
            values = (
                str(attr(device, "display_name", attr(device, "device_id", "Устройство"))),
                str(attr(device, "connection", "—")),
                (
                    f"{float(sample_rate) / 1_000_000:.2f} MSPS"
                    if isinstance(sample_rate, (int, float))
                    else "—"
                ),
                f"{temperature} °C" if temperature != "—" else "—",
                device_state_ru(device),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, str(attr(device, "device_id", row)))
                if column == 4:
                    cell.setForeground(
                        QBrush(
                            QColor(
                                {
                                    "ready": Colors.READY,
                                    "warning": Colors.WARNING,
                                    "critical": Colors.CRITICAL,
                                    "neutral": Colors.MUTED,
                                }[level]
                            )
                        )
                    )
                self.table.setItem(row, column, cell)
            if previous_id and str(attr(device, "device_id", "")) == previous_id:
                self.table.selectRow(row)
            elif not previous_id and row == 0:
                self.table.selectRow(0)
        self.total.set_status(f"{len(self._devices)} НАСТРОЕНО", "info")
        self.active.set_status(f"{active_count} АКТИВНО", "ready")
        self.problem.set_status(f"{problem_count} ТРЕБУЮТ ДЕЙСТВИЯ", "warning")
        if not self._devices:
            self.header.status.set_status("УСТРОЙСТВА НЕ НАСТРОЕНЫ", "neutral")
        elif problem_count:
            self.header.status.set_status("ЕСТЬ ОГРАНИЧЕНИЯ", "warning")
        else:
            self.header.status.set_status("ВСЕ УЗЛЫ ГОТОВЫ", "ready")
        self.degradation_notice.setVisible(problem_count > 0)
        self._render_discovered_rtlsdr()
        self._render_discovered_hackrf()
        self._render_discovered_tinysa()
        self._update_inspector()

    def _configured_connections(self) -> frozenset[str]:
        return frozenset(
            str(attr(device, "connection", "")).strip().upper()
            for device in self._devices
            if str(attr(device, "connection", "")).strip()
        )

    def _render_discovered_rtlsdr(self) -> None:
        if not self._discovered_rtlsdr:
            self.add_discovered_rtlsdr_button.setEnabled(False)
            return
        previous_connection = str(self.discovered_rtlsdr_select.currentData() or "")
        configured = self._configured_connections()
        self.discovered_rtlsdr_select.blockSignals(True)
        self.discovered_rtlsdr_select.clear()
        preferred_index = 0
        for row, candidate in enumerate(self._discovered_rtlsdr):
            connection = str(attr(candidate, "connection", "")).strip()
            description = str(attr(candidate, "description", "RTL-SDR")).strip() or "RTL-SDR"
            already_configured = connection.upper() in configured
            suffix = " · уже в профиле" if already_configured else ""
            self.discovered_rtlsdr_select.addItem(
                f"{description} — {connection}{suffix}",
                connection,
            )
            self.discovered_rtlsdr_select.setItemData(
                row,
                already_configured,
                Qt.ItemDataRole.UserRole + 1,
            )
            if connection == previous_connection:
                preferred_index = row
            elif not already_configured and self.discovered_rtlsdr_select.count() > 1:
                current_preferred = bool(
                    self.discovered_rtlsdr_select.itemData(
                        preferred_index,
                        Qt.ItemDataRole.UserRole + 1,
                    )
                )
                if current_preferred:
                    preferred_index = row
        self.discovered_rtlsdr_select.setCurrentIndex(preferred_index)
        self.discovered_rtlsdr_select.blockSignals(False)
        self._update_discovered_rtlsdr_action()

    def _update_discovered_rtlsdr_action(self) -> None:
        index = self.discovered_rtlsdr_select.currentIndex()
        already_configured = (
            bool(
                self.discovered_rtlsdr_select.itemData(
                    index,
                    Qt.ItemDataRole.UserRole + 1,
                )
            )
            if index >= 0
            else False
        )
        self.add_discovered_rtlsdr_button.setEnabled(
            index >= 0 and self._can_add_rtlsdr and not already_configured
        )
        if already_configured:
            self.add_discovered_rtlsdr_button.setToolTip("Это подключение уже добавлено в профиль.")
        elif self._can_add_rtlsdr:
            self.add_discovered_rtlsdr_button.setToolTip(
                "Добавить выбранный приёмник в профиль и разрешить его запуск."
            )

    def discover_rtlsdr_devices(self) -> None:
        ok, result = call_runtime(self.runtime, "discover_rtlsdr_devices")
        if not ok:
            self._discovered_rtlsdr = ()
            self.discovered_rtlsdr_select.clear()
            self.add_discovered_rtlsdr_button.setEnabled(False)
            self.rtlsdr_discovery_notice.set_notice(
                "Поиск RTL-SDR не выполнен",
                str(result),
                level="critical",
            )
            return

        self._discovered_rtlsdr = items(result, "devices")
        state = value_of(attr(result, "state")).lower()
        issue_text = _discovery_issue_text(result)
        if self._discovered_rtlsdr:
            self._render_discovered_rtlsdr()
            count = len(self._discovered_rtlsdr)
            message = (
                f"USB-обнаружение завершено: найдено {count}. "
                "Выберите приёмник и нажмите «Добавить и включить». "
                "Поток пока не проверен и не считается запущенным."
            )
            if issue_text:
                message = f"{message} {issue_text}"
            self.rtlsdr_discovery_notice.set_notice(
                "RTL-SDR найден" if count == 1 else "RTL-SDR найдены",
                message,
                level="warning" if state == "partial" else "info",
            )
            return

        self.discovered_rtlsdr_select.clear()
        self.add_discovered_rtlsdr_button.setEnabled(False)
        if state in _DISCOVERY_FAILURE_STATES or issue_text:
            self.rtlsdr_discovery_notice.set_notice(
                "Поиск RTL-SDR не завершён",
                issue_text or "Runtime не вернул диагностическую причину.",
                level="critical",
            )
        else:
            self.rtlsdr_discovery_notice.set_notice(
                "RTL-SDR не найден",
                (
                    "Подключённый RTL-SDR не обнаружен. Проверьте USB-порт "
                    "и драйвер WinUSB, затем повторите поиск."
                ),
                level="warning",
            )

    def add_discovered_rtlsdr_device(self) -> None:
        connection = str(self.discovered_rtlsdr_select.currentData() or "").strip()
        if not connection:
            return
        if connection.upper() in self._configured_connections():
            self.rtlsdr_discovery_notice.set_notice(
                "Приёмник уже настроен",
                (
                    f"{connection} уже находится в профиле. "
                    "Используйте «Проверить настроенные» для повторной проверки."
                ),
                level="info",
            )
            self._render_discovered_rtlsdr()
            return

        ok, result = call_runtime(
            self.runtime,
            "add_discovered_rtlsdr_device",
            connection,
        )
        snapshot = result if ok else current_snapshot(self.runtime)
        if not ok:
            self.rtlsdr_discovery_notice.set_notice(
                "Приёмник не добавлен",
                str(result),
                level="critical",
            )
            return

        self.refresh(snapshot)
        device = next(
            (
                configured_device
                for configured_device in self._devices
                if str(attr(configured_device, "connection", "")).strip().upper()
                == connection.upper()
            ),
            None,
        )
        state = value_of(attr(device, "state")).lower()
        if state == "streaming":
            self.rtlsdr_discovery_notice.set_notice(
                "RTL-SDR добавлен, поток активен",
                (
                    f"{connection} добавлен в профиль. "
                    f"Состояние приёмника: {device_state_ru(device)}."
                ),
                level="ready",
            )
        elif state == "ready":
            self.rtlsdr_discovery_notice.set_notice(
                "RTL-SDR добавлен и готов",
                (
                    f"{connection} добавлен в профиль. "
                    f"Состояние приёмника: {device_state_ru(device)}. "
                    "Получение живых данных подтвердите на странице «Спектр»."
                ),
                level="ready",
            )
        elif device is not None:
            self.rtlsdr_discovery_notice.set_notice(
                "RTL-SDR добавлен, поток не готов",
                (f"{connection} сохранён и включён. {_device_failure_text(device)}"),
                level="warning",
            )
        else:
            self.rtlsdr_discovery_notice.set_notice(
                "Состояние RTL-SDR не подтверждено",
                (
                    f"Runtime принял {connection}, но не вернул это устройство "
                    "в снимке состояния. Повторите проверку настроенных."
                ),
                level="critical",
            )

    def _render_discovered_hackrf(self) -> None:
        self._render_generic_candidates(
            self._discovered_hackrf,
            self.discovered_hackrf_select,
            self.add_discovered_hackrf_button,
            can_add=self._can_add_hackrf,
            label=lambda candidate: (
                f"{attr(candidate, 'board_name', 'HackRF One')!s} · "
                f"{attr(candidate, 'serial', '—')!s} — "
                f"{attr(candidate, 'connection', '')!s}"
            ),
        )

    def _render_discovered_tinysa(self) -> None:
        self._render_generic_candidates(
            self._discovered_tinysa,
            self.discovered_tinysa_select,
            self.add_discovered_tinysa_button,
            can_add=self._can_add_tinysa,
            label=lambda candidate: (
                f"{attr(candidate, 'description', 'USB Serial')!s} — "
                f"{attr(candidate, 'connection', '')!s} · "
                f"{attr(candidate, 'evidence_ru', 'требует подтверждения')!s}"
            ),
        )

    def _render_generic_candidates(
        self,
        candidates: tuple[object, ...],
        selector: QComboBox,
        action_button: QPushButton,
        *,
        can_add: bool,
        label: Callable[[object], str],
    ) -> None:
        if not candidates:
            action_button.setEnabled(False)
            return
        previous_connection = str(selector.currentData() or "")
        configured = self._configured_connections()
        selector.blockSignals(True)
        selector.clear()
        preferred_index = 0
        for row, candidate in enumerate(candidates):
            connection = str(attr(candidate, "connection", "")).strip()
            already_configured = connection.upper() in configured
            candidate_label = label(candidate)
            suffix = " · уже в профиле" if already_configured else ""
            selector.addItem(f"{candidate_label}{suffix}", connection)
            selector.setItemData(
                row,
                already_configured,
                Qt.ItemDataRole.UserRole + 1,
            )
            if connection == previous_connection or (
                not already_configured
                and selector.count() > 1
                and bool(
                    selector.itemData(
                        preferred_index,
                        Qt.ItemDataRole.UserRole + 1,
                    )
                )
            ):
                preferred_index = row
        selector.setCurrentIndex(preferred_index)
        selector.blockSignals(False)
        self._update_generic_candidate_action(
            selector,
            action_button,
            can_add=can_add,
        )

    @staticmethod
    def _update_generic_candidate_action(
        selector: QComboBox,
        action_button: QPushButton,
        *,
        can_add: bool,
    ) -> None:
        index = selector.currentIndex()
        already_configured = (
            bool(
                selector.itemData(
                    index,
                    Qt.ItemDataRole.UserRole + 1,
                )
            )
            if index >= 0
            else False
        )
        action_button.setEnabled(
            index >= 0 and can_add and not already_configured
        )
        if already_configured:
            action_button.setToolTip("Это подключение уже добавлено в профиль.")
        elif can_add:
            action_button.setToolTip(
                "Явно подтвердить выбранное подключение и запустить проверку."
            )

    def _update_discovered_hackrf_action(self) -> None:
        self._update_generic_candidate_action(
            self.discovered_hackrf_select,
            self.add_discovered_hackrf_button,
            can_add=self._can_add_hackrf,
        )

    def _update_discovered_tinysa_action(self) -> None:
        self._update_generic_candidate_action(
            self.discovered_tinysa_select,
            self.add_discovered_tinysa_button,
            can_add=self._can_add_tinysa,
        )

    def discover_hackrf_devices(self) -> None:
        ok, result = call_runtime(self.runtime, "discover_hackrf_devices")
        if not ok:
            self._discovered_hackrf = ()
            self.discovered_hackrf_select.clear()
            self.add_discovered_hackrf_button.setEnabled(False)
            self.hackrf_discovery_notice.set_notice(
                "Поиск HackRF не выполнен",
                str(result),
                level="critical",
            )
            return
        self._discovered_hackrf = items(result, "devices")
        state = value_of(attr(result, "state")).lower()
        issue_text = _discovery_issue_text(result)
        if self._discovered_hackrf:
            self._render_discovered_hackrf()
            count = len(self._discovered_hackrf)
            self.hackrf_discovery_notice.set_notice(
                "HackRF подтверждён" if count == 1 else "HackRF подтверждены",
                (
                    f"Найдено в HackRF USB mode: {count}. "
                    "Выберите устройство; поддерживается только приём, "
                    "поток ещё не считается запущенным."
                    + (f" {issue_text}" if issue_text else "")
                ),
                level="warning" if state == "partial" else "info",
            )
            return
        self.discovered_hackrf_select.clear()
        self.add_discovered_hackrf_button.setEnabled(False)
        self.hackrf_discovery_notice.set_notice(
            "HackRF не готов к подключению",
            issue_text
            or (
                "Устройство не найдено. Для PortaPack вручную откройте "
                "HackRF USB mode, затем повторите поиск."
            ),
            level=(
                "critical"
                if state in _DISCOVERY_FAILURE_STATES
                else "warning"
            ),
        )

    def discover_tinysa_devices(self) -> None:
        ok, result = call_runtime(self.runtime, "discover_tinysa_devices")
        if not ok:
            self._discovered_tinysa = ()
            self.discovered_tinysa_select.clear()
            self.add_discovered_tinysa_button.setEnabled(False)
            self.tinysa_discovery_notice.set_notice(
                "Поиск tinySA не выполнен",
                str(result),
                level="critical",
            )
            return
        self._discovered_tinysa = items(result, "candidates")
        state = value_of(attr(result, "state")).lower()
        issue_text = _discovery_issue_text(result)
        if self._discovered_tinysa:
            self._render_discovered_tinysa()
            count = len(self._discovered_tinysa)
            self.tinysa_discovery_notice.set_notice(
                "Кандидат tinySA найден"
                if count == 1
                else "Кандидаты tinySA найдены",
                (
                    f"По системным описаниям найдено: {count}. "
                    "Ни один COM-порт не открывался; выберите и подтвердите один."
                    + (f" {issue_text}" if issue_text else "")
                ),
                level="warning",
            )
            return
        self.discovered_tinysa_select.clear()
        self.add_discovered_tinysa_button.setEnabled(False)
        self.tinysa_discovery_notice.set_notice(
            "tinySA-кандидаты не найдены",
            issue_text
            or "Подключите анализатор по USB и повторите metadata-only поиск.",
            level=(
                "critical"
                if state in _DISCOVERY_FAILURE_STATES
                else "warning"
            ),
        )

    def add_discovered_hackrf_device(self) -> None:
        self._add_discovered_receiver(
            selector=self.discovered_hackrf_select,
            runtime_method="add_discovered_hackrf_device",
            notice=self.hackrf_discovery_notice,
            display_name="HackRF",
        )

    def add_discovered_tinysa_device(self) -> None:
        self._add_discovered_receiver(
            selector=self.discovered_tinysa_select,
            runtime_method="add_discovered_tinysa_device",
            notice=self.tinysa_discovery_notice,
            display_name="tinySA",
        )

    def _add_discovered_receiver(
        self,
        *,
        selector: QComboBox,
        runtime_method: str,
        notice: InlineNotice,
        display_name: str,
    ) -> None:
        connection = str(selector.currentData() or "").strip()
        if not connection:
            return
        if connection.upper() in self._configured_connections():
            notice.set_notice(
                f"{display_name} уже настроен",
                f"{connection} уже находится в профиле.",
                level="info",
            )
            return
        ok, result = call_runtime(self.runtime, runtime_method, connection)
        snapshot = result if ok else current_snapshot(self.runtime)
        if not ok:
            notice.set_notice(
                f"{display_name} не добавлен",
                str(result),
                level="critical",
            )
            return
        self.refresh(snapshot)
        device = next(
            (
                item
                for item in self._devices
                if str(attr(item, "connection", "")).strip().upper()
                == connection.upper()
            ),
            None,
        )
        level = device_level(device)
        notice.set_notice(
            (
                f"{display_name} добавлен"
                if device is not None
                else f"Состояние {display_name} не подтверждено"
            ),
            (
                f"{connection}: {device_state_ru(device)}. "
                f"{_device_failure_text(device) if level != 'ready' else 'Приёмник готов.'}"
            ),
            level=(
                "ready"
                if level == "ready"
                else "warning"
                if device is not None
                else "critical"
            ),
        )

    def _selected_device_id(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _selected_device(self) -> object | None:
        selected = self._selected_device_id()
        return next(
            (device for device in self._devices if str(attr(device, "device_id", "")) == selected),
            None,
        )

    def _update_inspector(self) -> None:
        device = self._selected_device()
        available = device is not None
        state = value_of(attr(device, "state")).lower()
        reconnect_allowed = (
            available and self._can_reconnect and state not in _RECONNECT_BLOCKED_STATES
        )
        self.reconnect_button.setEnabled(reconnect_allowed)
        if available and state == "disabled":
            self.reconnect_button.setToolTip(
                "Приёмник отключён конфигурацией. Сначала включите его в настройках."
            )
        elif self._can_reconnect:
            self.reconnect_button.setToolTip(
                "Повторно открыть только выбранное настроенное подключение."
            )
        self.calibrate_button.setEnabled(available and self._can_calibrate)
        self.disconnect_button.setEnabled(available and self._can_disconnect)
        if device is None:
            self.inspector_title.setText("Устройство не выбрано")
            self.inspector_status.set_status("ОЖИДАНИЕ", "neutral")
            for label in self.detail_values.values():
                label.setText("—")
            return
        self.inspector_title.setText(str(attr(device, "display_name", "Устройство")))
        self.inspector_status.set_status(device_state_ru(device), device_level(device))
        metrics = attr(device, "metrics", {}) or {}
        sample_rate = attr(device, "sample_rate_hz")
        sync = attr(metrics, "sync", attr(metrics, "coherence", "Нет данных"))
        temperature = attr(metrics, "temperature_c", attr(metrics, "temperature", "—"))
        tuning_min = attr(metrics, "tuning_min_hz")
        tuning_max = attr(metrics, "tuning_max_hz")
        selected_profile = str(attr(metrics, "tuning_profile_id", "—"))
        detected_profile = str(
            attr(metrics, "detected_tuning_profile_id", selected_profile)
        )
        profile_selection = str(attr(metrics, "profile_selection", "automatic"))
        profile_text = _tuning_profile_label(selected_profile)
        if profile_selection == "operator_confirmed":
            profile_text += (
                " · выбран оператором; драйвер определил: "
                f"{_tuning_profile_label(detected_profile)}."
            )
        elif profile_selection == "operator_unconfirmed_fallback":
            profile_text += (
                " · запрошен Blog V4, но EEPROM не подтверждён; "
                "HF безопасно отключён."
            )
        values = {
            "id": attr(device, "device_id", "—"),
            "kind": attr(device, "kind", "—"),
            "connection": attr(device, "connection", "—"),
            "driver": attr(device, "driver", "—"),
            "usb_identity": (
                f"{attr(metrics, 'usb_manufacturer', '—')} · "
                f"{attr(metrics, 'usb_product', '—')}"
            ),
            "tuning_profile": profile_text,
            "tuning_range": (
                f"{float(tuning_min) / 1_000_000:g}–"
                f"{float(tuning_max) / 1_000_000:g} МГц"
                if isinstance(tuning_min, (int, float))
                and isinstance(tuning_max, (int, float))
                else "—"
            ),
            "sample_rate": (
                f"{float(sample_rate) / 1_000_000:.3f} MSPS"
                if isinstance(sample_rate, (int, float))
                else "—"
            ),
            "frequency": format_frequency(attr(device, "center_frequency_hz")),
            "sync": sync,
            "temperature": f"{temperature} °C" if temperature != "—" else "—",
            "reason": attr(device, "reason_ru", "Нет активной причины"),
            "action": attr(device, "recommended_action_ru", "Действие не требуется"),
        }
        profile_warning = str(attr(metrics, "profile_warning_ru", "")).strip()
        if profile_warning:
            values["action"] = profile_warning
        for key, value in values.items():
            self.detail_values[key].setText(str(value))

    def rescan(self) -> None:
        ok, result = call_runtime(self.runtime, "rescan")
        snapshot = result if ok else current_snapshot(self.runtime)
        devices = items(snapshot, "devices")
        usable = tuple(
            device
            for device in devices
            if value_of(attr(device, "state")).lower() in _RECONNECT_RECOVERED_STATES
        )
        if not ok:
            self.action_result.setText(str(result))
        elif usable:
            self.action_result.setText(
                f"Проверка завершена: доступно {len(usable)} из {len(devices)}."
            )
        elif devices:
            first = devices[0]
            self.action_result.setText(
                "Проверка завершена, но доступных приёмников нет. " + _device_failure_text(first)
            )
        else:
            self.action_result.setText("Проверка завершена: приёмники в профиле не настроены.")
        self.refresh(snapshot)

    def reconnect_selected(self) -> None:
        device_id = self._selected_device_id()
        if not device_id:
            return
        ok, result = call_runtime(self.runtime, "reconnect", device_id)
        snapshot = result if ok else current_snapshot(self.runtime)
        device = _device_from_result(snapshot, device_id)
        state = value_of(attr(device, "state")).lower()
        if ok and state in _RECONNECT_RECOVERED_STATES:
            self.action_result.setText(f"Приёмник восстановлен: {device_state_ru(device)}.")
        elif ok:
            self.action_result.setText(
                "Переподключение выполнено, но приёмник не восстановлен. "
                + _device_failure_text(device)
            )
        else:
            self.action_result.setText(str(result))
        self.refresh(snapshot)

    def calibrate_selected(self) -> None:
        device_id = self._selected_device_id()
        if not device_id:
            return
        ok, result = call_runtime(self.runtime, "calibrate", device_id)
        self.action_result.setText("Калибровка запущена" if ok else str(result))

    def disconnect_selected(self) -> None:
        device_id = self._selected_device_id()
        if not device_id:
            return
        answer = QMessageBox.question(
            self,
            "Подтверждение отключения",
            "Остановить активные сессии и отключить выбранное устройство?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, result = call_runtime(self.runtime, "disconnect", device_id)
        self.action_result.setText("Устройство отключено" if ok else str(result))
        self.refresh()


def _tuning_profile_label(profile_id: str) -> str:
    return {
        "generic_r820t": "Обычный RTL-SDR · 24–1766 МГц",
        "rtlsdr_blog_v4": "RTL-SDR Blog V4 · 0,5–1766 МГц",
        "rtlsdr_blog_v3_direct_q": "RTL-SDR Blog V3 · Q-direct для HF",
    }.get(profile_id, profile_id or "—")
