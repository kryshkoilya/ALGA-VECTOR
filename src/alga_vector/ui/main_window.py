"""Main 112 px navigation shell."""

# ruff: noqa: RUF001

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from alga_vector import __version__

from .pages import (
    DashboardPage,
    DevicesPage,
    DiagnosticsPage,
    DirectionPage,
    MapPage,
    SettingsPage,
    SignalEventsPage,
    SimpleSituationPage,
    SpectrumPage,
)
from .runtime import (
    attr,
    current_snapshot,
    items,
    provenance_ru,
    runtime_error_detail,
    unavailable_snapshot,
    value_of,
)
from .signal_notifications import build_signal_notification
from .theme import FOOTER_HEIGHT, HEADER_HEIGHT, NAV_WIDTH, Colors
from .widgets import ProvenanceBanner, SignalAlertBanner, StatusBadge


class MainWindow(QMainWindow):
    """Operator shell. Runtime lifetime remains owned by the composition root."""

    SIMPLE_PAGE_SPECS: tuple[tuple[str, str], ...] = (
        ("situation", "Обстановка"),
        ("devices", "Устройства"),
        ("events", "События"),
        ("direction", "Направление"),
        ("settings", "Настройки"),
    )
    EXPERT_PAGE_SPECS: tuple[tuple[str, str], ...] = (
        ("situation", "Обстановка"),
        ("dashboard", "Обзор"),
        ("devices", "Устройства"),
        ("spectrum", "Спектр"),
        ("events", "События"),
        ("direction", "Направление"),
        ("map", "Карта"),
        ("diagnostics", "Диагностика"),
        ("settings", "Настройки"),
    )
    PAGE_SPECS = EXPERT_PAGE_SPECS

    def __init__(self, runtime: object | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.setWindowTitle("ALGA VECTOR — гражданское раннее предупреждение")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self._page_indexes: dict[str, int] = {}
        self._page_widgets: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}
        self._page_specs = self.SIMPLE_PAGE_SPECS
        self._interface_mode = "simple"
        self._local_mode_override: str | None = None
        self._pages_initialized = False
        self._signal_notification_signature: tuple[str, ...] | None = None
        self._signal_notification_target = "events"

        central = QWidget()
        central.setObjectName("contentRoot")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_navigation())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_header())
        self.signal_alert = SignalAlertBanner()
        self.signal_alert.open_requested.connect(self._open_signal_notification)
        self.signal_alert.hide()
        right.addWidget(self.signal_alert)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right.addWidget(self.stack, 1)
        right.addWidget(self._build_footer())
        root.addLayout(right, 1)
        self.setCentralWidget(central)

        self._create_pages()
        self.navigate("situation")
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1_000)
        self.refresh_timer.timeout.connect(self.poll_runtime)
        self.refresh_timer.start()
        self.refresh_snapshot()

    @property
    def current_page_key(self) -> str:
        index = self.stack.currentIndex()
        return next(
            (key for key, page_index in self._page_indexes.items() if page_index == index),
            "dashboard",
        )

    def page(self, key: str) -> QWidget:
        return self._page_widgets[key]

    def _build_navigation(self) -> QWidget:
        navigation = QFrame()
        navigation.setObjectName("navigation")
        navigation.setFixedWidth(NAV_WIDTH)
        layout = QVBoxLayout(navigation)
        layout.setContentsMargins(8, 12, 8, 10)
        layout.setSpacing(5)
        brand = QLabel("AV")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setFixedSize(42, 42)
        brand.setStyleSheet(
            f"background-color: {Colors.READY}; color: {Colors.BG}; "
            "font-size: 16px; font-weight: 700; border-radius: 4px;"
        )
        layout.addWidget(brand, 0, Qt.AlignmentFlag.AlignHCenter)
        product = QLabel("ALGA\nVECTOR")
        product.setAlignment(Qt.AlignmentFlag.AlignCenter)
        product.setStyleSheet("font-weight: 600;")
        layout.addWidget(product)
        layout.addSpacing(6)
        self._navigation_container = QWidget(navigation)
        self._navigation_layout = QVBoxLayout(self._navigation_container)
        self._navigation_layout.setContentsMargins(0, 0, 0, 0)
        self._navigation_layout.setSpacing(5)
        layout.addWidget(self._navigation_container)
        self._navigation_group = QButtonGroup(self._navigation_container)
        self._navigation_group.setExclusive(True)
        self._rebuild_navigation()
        layout.addStretch(1)
        version = QLabel(f"v{__version__}")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version.setProperty("muted", "true")
        layout.addWidget(version)
        return navigation

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("topHeader")
        header.setFixedHeight(HEADER_HEIGHT)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(17, 0, 17, 0)
        layout.setSpacing(12)
        title = QLabel("ALGA VECTOR")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        subtitle = QLabel("Гражданская мультисенсорная платформа")
        subtitle.setProperty("muted", "true")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)
        self.profile_label = QLabel("Профиль не загружен")
        self.profile_label.setProperty("secondary", "true")
        self.mode_group = QButtonGroup(header)
        self.mode_group.setExclusive(True)
        self.simple_mode_button = QPushButton("SIMPLE MODE")
        self.simple_mode_button.setObjectName("mode_simple")
        self.simple_mode_button.setProperty("modeSwitch", "true")
        self.simple_mode_button.setCheckable(True)
        self.simple_mode_button.setToolTip(
            "Простой режим: только обстановка, понятные события и следующий шаг."
        )
        self.expert_mode_button = QPushButton("EXPERT MODE")
        self.expert_mode_button.setObjectName("mode_expert")
        self.expert_mode_button.setProperty("modeSwitch", "true")
        self.expert_mode_button.setCheckable(True)
        self.expert_mode_button.setToolTip(
            "Экспертный режим: спектр, карта, пеленгация и диагностика."
        )
        self.mode_group.addButton(self.simple_mode_button)
        self.mode_group.addButton(self.expert_mode_button)
        self.simple_mode_button.clicked.connect(
            lambda checked=False: self._mode_button_clicked("simple", checked)
        )
        self.expert_mode_button.clicked.connect(
            lambda checked=False: self._mode_button_clicked("expert", checked)
        )
        self.simple_mode_button.setChecked(True)
        self.global_provenance = ProvenanceBanner()
        self.system_status = StatusBadge("ИНИЦИАЛИЗАЦИЯ", "neutral")
        layout.addWidget(self.profile_label)
        layout.addWidget(self.simple_mode_button)
        layout.addWidget(self.expert_mode_button)
        layout.addWidget(self.global_provenance)
        layout.addWidget(self.system_status)
        return header

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        footer.setFixedHeight(FOOTER_HEIGHT)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(18)
        build = QLabel(f"BUILD v{__version__}")
        build.setProperty("muted", "true")
        mode = QLabel("OFFLINE-FIRST")
        mode.setProperty("muted", "true")
        self.footer_source = QLabel("ИСТОЧНИК: —")
        self.footer_source.setProperty("muted", "true")
        signature = QLabel("Разработал: Буйвол и Задира")
        signature.setProperty("muted", "true")
        layout.addWidget(build)
        layout.addWidget(mode)
        layout.addWidget(self.footer_source)
        layout.addStretch(1)
        layout.addWidget(signature)
        return footer

    def _create_pages(self) -> None:
        factories: dict[str, Callable[[object | None], QWidget]] = {
            "situation": SimpleSituationPage,
            "dashboard": DashboardPage,
            "devices": DevicesPage,
            "spectrum": SpectrumPage,
            "events": SignalEventsPage,
            "direction": DirectionPage,
            "map": MapPage,
            "diagnostics": DiagnosticsPage,
            "settings": SettingsPage,
        }
        for key, factory in factories.items():
            page = factory(self.runtime)
            self._page_widgets[key] = page
            self._page_indexes[key] = self.stack.addWidget(page)
        dashboard = self._page_widgets["dashboard"]
        if isinstance(dashboard, DashboardPage):
            dashboard.open_page.connect(self.navigate)

    def navigate(self, key: str) -> None:
        if key not in self._page_indexes:
            return
        self.stack.setCurrentIndex(self._page_indexes[key])
        button = self._nav_buttons.get(key)
        if button is not None:
            button.setChecked(True)
        self.refresh_snapshot()

    def _rebuild_navigation(self) -> None:
        """Build only the routes relevant to the selected operator mode."""

        while self._navigation_layout.count():
            item = self._navigation_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._nav_buttons.clear()
        self._navigation_group.deleteLater()
        self._navigation_group = QButtonGroup(self._navigation_container)
        self._navigation_group.setExclusive(True)
        for key, label in self._page_specs:
            button = QPushButton(label)
            button.setObjectName(f"nav_{key}")
            button.setProperty("nav", "true")
            button.setCheckable(True)
            button.setMinimumHeight(48)
            button.clicked.connect(
                lambda checked=False, page_key=key: self.navigate(page_key)
            )
            self._navigation_group.addButton(button)
            self._nav_buttons[key] = button
            self._navigation_layout.addWidget(button)
        if hasattr(self, "stack"):
            active = self._nav_buttons.get(self.current_page_key)
            if active is not None:
                active.setChecked(True)

    def _mode_button_clicked(self, mode: str, checked: bool) -> None:
        if checked:
            self._request_interface_mode(mode)

    def _request_interface_mode(self, mode: str) -> None:
        """Persist a mode change when supported, otherwise keep it UI-local."""

        experience = "expert" if mode == "expert" else "guided"
        setter = getattr(self.runtime, "set_experience_level", None)
        updater = (
            setter
            if callable(setter)
            else getattr(self.runtime, "update_settings", None)
        )
        if callable(updater):
            try:
                if callable(setter):
                    setter(experience)
                else:
                    updater({"ui": {"experience_level": experience}})
            except Exception as exc:
                self.system_status.set_status("РЕЖИМ НЕ ИЗМЕНЁН", "critical")
                self.system_status.setToolTip(f"{type(exc).__name__}: {exc}")
                expected = (
                    self.expert_mode_button
                    if self._interface_mode == "expert"
                    else self.simple_mode_button
                )
                expected.setChecked(True)
                return
            self._local_mode_override = None
        else:
            self._local_mode_override = mode
        self._set_interface_mode(mode)

    def _set_interface_mode(self, mode: str) -> None:
        normalized = "expert" if mode == "expert" else "simple"
        changed = normalized != self._interface_mode
        if changed:
            self._interface_mode = normalized
            self._page_specs = (
                self.EXPERT_PAGE_SPECS
                if normalized == "expert"
                else self.SIMPLE_PAGE_SPECS
            )
            self._rebuild_navigation()
        is_expert = normalized == "expert"
        self.expert_mode_button.setChecked(is_expert)
        self.simple_mode_button.setChecked(not is_expert)
        if changed and self.current_page_key not in dict(self._page_specs):
            self.stack.setCurrentIndex(self._page_indexes["situation"])
        active = self._nav_buttons.get(self.current_page_key)
        if active is not None:
            active.setChecked(True)

    def _open_signal_notification(self) -> None:
        target = self._signal_notification_target
        self.navigate(target if target in self._page_indexes else "dashboard")

    def refresh_snapshot(self) -> None:
        snapshot = current_snapshot(self.runtime)
        self._render_snapshot(snapshot)

    def poll_runtime(self) -> None:
        """Advance a runtime snapshot on the timer without affecting navigation reads."""

        snapshot: object | None = None
        for method_name in ("tick", "snapshot"):
            try:
                method = getattr(self.runtime, method_name, None)
            except Exception as exc:
                snapshot = unavailable_snapshot(
                    exc,
                    operation=f"runtime.{method_name}",
                )
                break
            if not callable(method):
                continue
            try:
                snapshot = method()
            except Exception as exc:
                snapshot = unavailable_snapshot(
                    exc,
                    operation=f"runtime.{method_name}",
                )
            break
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        self._render_snapshot(snapshot)

    def _render_snapshot(self, snapshot: object | None) -> None:
        self.global_provenance.refresh(snapshot)
        notification = build_signal_notification(snapshot)
        if notification.active:
            self._signal_notification_target = notification.target_page
            details = (
                f"{notification.details}\n\nЧто делать: "
                f"{notification.next_action}"
            )
            signature = (
                notification.key,
                notification.level,
                notification.title,
                notification.message,
                details,
                notification.target_page,
            )
            if signature != self._signal_notification_signature:
                self.signal_alert.set_alert(
                    notification.title,
                    notification.message,
                    level=notification.level,
                    details=details,
                )
                self._signal_notification_signature = signature
            self.signal_alert.show()
        else:
            self._signal_notification_signature = None
            self._signal_notification_target = "events"
            self.signal_alert.hide()
        self.profile_label.setText(str(attr(snapshot, "profile_name", "Профиль не загружен")))
        experience = str(attr(snapshot, "experience_level", "guided")).lower()
        if self._local_mode_override is None:
            self._set_interface_mode(
                "expert" if experience == "expert" else "simple"
            )
        readiness = int(attr(snapshot, "readiness_percent", 0))
        incidents = items(snapshot, "incidents")
        has_critical_incident = any(
            value_of(attr(incident, "severity")).lower() in {"critical", "error"}
            for incident in incidents
        )
        assessment_state = value_of(
            attr(attr(snapshot, "signal_assessment"), "state", "no_data")
        ).lower()
        runtime_error = runtime_error_detail(snapshot)
        self.system_status.setToolTip(runtime_error)
        if runtime_error:
            self.system_status.set_status("ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ", "critical")
        elif has_critical_incident:
            self.system_status.set_status("КРИТИЧЕСКИЙ ИНЦИДЕНТ", "critical")
        elif assessment_state == "data_unreliable":
            self.system_status.set_status("КАЧЕСТВО ДАННЫХ СНИЖЕНО", "critical")
        elif readiness >= 90:
            self.system_status.set_status("RF-ЯДРО ГОТОВО", "ready")
        elif readiness > 0:
            self.system_status.set_status(f"ГОТОВНОСТЬ {readiness}%", "warning")
        else:
            self.system_status.set_status("ОЖИДАНИЕ ДАННЫХ", "neutral")
        self.footer_source.setText(f"ИСТОЧНИК: {provenance_ru(snapshot)}")
        pages = (
            tuple(self._page_widgets.values())
            if not self._pages_initialized or bool(runtime_error)
            else (self._page_widgets.get(self.current_page_key),)
        )
        for page in pages:
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                refresh(snapshot)
        self._pages_initialized = True

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop UI timers only; the composition root owns runtime shutdown."""

        self.refresh_timer.stop()
        event.accept()
