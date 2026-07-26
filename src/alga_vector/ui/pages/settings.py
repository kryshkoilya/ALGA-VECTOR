"""Validated operator settings surface."""

# ruff: noqa: RUF001

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from alga_vector.devices import (
    HACKRF_ONE_PROFILE,
    ReceiverHardwareProfile,
    TinySaModel,
    tinysa_hardware_profile,
)
from alga_vector.devices.tuning import (
    BLOG_V3_DIRECT_Q_PROFILE,
    BLOG_V4_PROFILE,
    GENERIC_RTLSDR_PROFILE,
    RtlSdrTuningProfile,
    rtlsdr_profile_by_id,
    validate_rtlsdr_tuning,
)

from ..runtime import attr, call_runtime, current_snapshot, provenance_key
from ..widgets import InlineNotice, Panel
from .common import OperatorPage


def _has_enabled_rtlsdr(adapters: list[dict[str, object]]) -> bool:
    return any(
        adapter.get("kind") == "rtlsdr"
        and bool(adapter.get("enabled", True))
        for adapter in adapters
    )


class SettingsPage(OperatorPage):
    settings_applied = Signal(dict)

    def __init__(self, runtime: object | None = None) -> None:
        self._dirty = False
        self._loading = False
        self._loaded_once = False
        self._loaded_location_source = "unset"
        self._loaded_gps_port = ""
        self._loaded_gps_baud = 9_600
        self._loaded_map_path = ""
        self._online_cache_mib = 256
        super().__init__(
            runtime,
            "Настройки",
            "Профиль, хранилище и безопасные параметры приёма",
        )
        self.settings_content = QWidget()
        self.settings_layout = QVBoxLayout(self.settings_content)
        self.settings_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_layout.setSpacing(12)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.settings_scroll.setWidget(self.settings_content)
        self.root_layout.addWidget(self.settings_scroll, 1)
        columns = QHBoxLayout()
        general = Panel("Рабочий профиль", subtitle="Применяется после валидации")
        general_form = QFormLayout()
        self.profile_name = QLineEdit("Полевой профиль 01")
        self.mode = QComboBox()
        self.mode.addItem("Живые данные", "live")
        self.mode.addItem("Безопасный режим", "safe")
        self.mode.setToolTip(
            "Демо-источники доступны только при явном запуске с --demo."
        )
        self.data_dir = QLineEdit("runtime-data")
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self.choose_data_dir)
        path_row = QHBoxLayout()
        path_row.addWidget(self.data_dir, 1)
        path_row.addWidget(browse)
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setValue(30)
        self.retention.setSuffix(" дней")
        self.minimum_free = QDoubleSpinBox()
        self.minimum_free.setRange(0.5, 10_000.0)
        self.minimum_free.setValue(5.0)
        self.minimum_free.setSuffix(" ГиБ")
        general_form.addRow("Имя профиля", self.profile_name)
        general_form.addRow("Режим запуска", self.mode)
        general_form.addRow("Каталог данных", path_row)
        general_form.addRow("Хранить записи", self.retention)
        general_form.addRow("Резерв свободного места", self.minimum_free)
        general.content_layout.addLayout(general_form)
        columns.addWidget(general)

        receiver = Panel("Параметры спектра", subtitle="Без передачи RF")
        receiver_form = QFormLayout()
        self.center_frequency = QDoubleSpinBox()
        self.center_frequency.setRange(0.001, 6_000.0)
        self.center_frequency.setDecimals(3)
        self.center_frequency.setValue(433.920)
        self.center_frequency.setSuffix(" МГц")
        self.span = QDoubleSpinBox()
        self.span.setRange(1.0, 100_000.0)
        self.span.setDecimals(1)
        self.span.setValue(2_000.0)
        self.span.setSuffix(" кГц")
        self.sample_rate = QDoubleSpinBox()
        self.sample_rate.setRange(0.008, 64.0)
        self.sample_rate.setDecimals(3)
        self.sample_rate.setValue(2.400)
        self.sample_rate.setSuffix(" MSPS")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-200.0, 30.0)
        self.threshold.setDecimals(1)
        self.threshold.setValue(-72.4)
        self.real_adapters = QCheckBox("Разрешить реальные адаптеры")
        self.real_adapters.setToolTip(
            "Включайте только после установки и проверки подписанных драйверов."
        )
        receiver_form.addRow("Центральная частота", self.center_frequency)
        receiver_form.addRow("Полоса", self.span)
        receiver_form.addRow("Частота дискретизации", self.sample_rate)
        receiver_form.addRow(
            "Линия индикации (не порог детекции)",
            self.threshold,
        )
        receiver_form.addRow("Аппаратные источники", self.real_adapters)
        self.receiver_capability_note = QLabel(
            "Диапазон перестройки, допустимая полоса и частота дискретизации "
            "будут взяты из подтверждённого профиля выбранного приёмника."
        )
        self.receiver_capability_note.setWordWrap(True)
        self.receiver_capability_note.setProperty("secondary", "true")
        receiver_form.addRow("", self.receiver_capability_note)
        receiver.content_layout.addLayout(receiver_form)
        columns.addWidget(receiver)
        self.settings_layout.addLayout(columns)

        operational = QHBoxLayout()
        hardware = Panel(
            "Оборудование и представление",
            subtitle="Явная настройка и подтверждение подключения",
        )
        hardware.setMinimumHeight(470)
        hardware_form = QFormLayout()
        self.experience = QComboBox()
        self.experience.addItem("Простой режим · понятная обстановка", "guided")
        self.experience.addItem("Экспертный режим · полная телеметрия", "expert")
        self.hardware_kind = QComboBox()
        self.hardware_kind.addItem("Не настроено", "")
        self.hardware_kind.addItem("tinySA · USB Serial", "tinysa")
        self.hardware_kind.addItem("RTL-SDR · IQ", "rtlsdr")
        self.hardware_kind.addItem("HackRF One · только приём", "hackrf")
        self.hardware_id = QLineEdit("")
        self.hardware_id.setPlaceholderText("Например: receiver-01")
        self.hardware_connection = QLineEdit("")
        self.hardware_connection.setPlaceholderText(
            "tinySA: COM7 · RTL-SDR: RTLSDR:0 · HackRF: HACKRF:<serial>"
        )
        self.hardware_rtlsdr_profile = QComboBox()
        self.hardware_rtlsdr_profile.addItem(
            "Автоопределение · безопасный вариант",
            "auto",
        )
        self.hardware_rtlsdr_profile.addItem(
            "Обычный RTL-SDR · 24–1766 МГц",
            "generic",
        )
        self.hardware_rtlsdr_profile.addItem(
            "RTL-SDR Blog V4 · требуется подтверждение драйвера",
            "blog_v4",
        )
        self.hardware_rtlsdr_profile.addItem(
            "RTL-SDR Blog V3 · подтверждаю Q-direct",
            "blog_v3_direct_q",
        )
        self.hardware_rtlsdr_profile.setToolTip(
            "Blog V4 получает HF только после точного EEPROM-подтверждения "
            "драйвером. Надпись на корпусе или ручной выбор не заменяют эту проверку."
        )
        self.hardware_rtlsdr_profile.setEnabled(False)
        self.hardware_tinysa_model = QComboBox()
        self.hardware_tinysa_model.addItem(
            "Авто по firmware · обычный режим",
            "auto",
        )
        self.hardware_tinysa_model.addItem(
            "tinySA Basic · 0,1–350 МГц",
            TinySaModel.BASIC.value,
        )
        self.hardware_tinysa_model.addItem(
            "tinySA Ultra ZS405",
            TinySaModel.ULTRA_ZS405.value,
        )
        self.hardware_tinysa_model.addItem(
            "tinySA Ultra+ ZS406",
            TinySaModel.ULTRA_PLUS_ZS406.value,
        )
        self.hardware_tinysa_model.addItem(
            "tinySA Ultra+ ZS407",
            TinySaModel.ULTRA_PLUS_ZS407.value,
        )
        self.hardware_tinysa_model.setEnabled(False)
        self.hardware_tinysa_model.setToolTip(
            "Авто использует модель из ответа firmware. Ручной выбор нужен "
            "только когда идентификация проверена по устройству."
        )
        self.hardware_tinysa_ultra_mode = QCheckBox(
            "Подтверждаю Ultra mode на анализаторе"
        )
        self.hardware_tinysa_ultra_mode.setEnabled(False)
        self.hardware_tinysa_ultra_mode.setToolTip(
            "Это не включает Ultra mode командой. Флажок только подтверждает, "
            "что оператор уже включил режим на совместимом tinySA и принимает "
            "ограничения sweep/зеркальных откликов."
        )
        self.hardware_enabled = QCheckBox("Использовать этот приёмник")
        self.hardware_enabled.setChecked(True)
        hardware_form.addRow("Уровень интерфейса", self.experience)
        hardware_form.addRow("Тип приёмника", self.hardware_kind)
        hardware_form.addRow("ID приёмника", self.hardware_id)
        hardware_form.addRow("Точное подключение", self.hardware_connection)
        hardware_form.addRow("Аппаратный профиль RTL-SDR", self.hardware_rtlsdr_profile)
        hardware_form.addRow("Модель tinySA", self.hardware_tinysa_model)
        hardware_form.addRow("Расширенный sweep tinySA", self.hardware_tinysa_ultra_mode)
        hardware_form.addRow("Состояние", self.hardware_enabled)
        hardware.content_layout.addLayout(hardware_form)
        hardware_actions = QHBoxLayout()
        self.save_hardware_button = QPushButton("Добавить / обновить")
        self.save_hardware_button.clicked.connect(self.commit_hardware_editor)
        self.remove_hardware_button = QPushButton("Удалить выбранный")
        self.remove_hardware_button.clicked.connect(self.remove_selected_hardware)
        hardware_actions.addWidget(self.save_hardware_button)
        hardware_actions.addWidget(self.remove_hardware_button)
        hardware.content_layout.addLayout(hardware_actions)
        self.hardware_table = QTableWidget(0, 5)
        self.hardware_table.setHorizontalHeaderLabels(
            ["ID", "Тип", "Подключение", "RF-профиль", "Состояние"]
        )
        self.hardware_table.horizontalHeader().setStretchLastSection(True)
        self.hardware_table.verticalHeader().setVisible(False)
        self.hardware_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.hardware_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.hardware_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.hardware_table.setMinimumHeight(130)
        self.hardware_table.itemSelectionChanged.connect(
            self.load_selected_hardware
        )
        hardware.content_layout.addWidget(self.hardware_table)
        hardware.content_layout.addWidget(
            InlineNotice(
                "Без автоматического перебора",
                "ALGA VECTOR открывает только указанный COM-порт или индекс RTL-SDR. "
                "Произвольные USB/COM-устройства не сканируются.",
                level="ready",
            )
        )
        operational.addWidget(hardware)

        location = Panel("Карта и местоположение", subtitle="Точные координаты остаются локально")
        location.setMinimumHeight(470)
        location_form = QFormLayout()
        self.map_package = QLineEdit("")
        self.map_package.setPlaceholderText("Путь к локальному пакету .mbtiles")
        self.network_maps = QCheckBox(
            "Автоматически загружать только видимые тайлы по HTTPS"
        )
        self.network_maps.setChecked(True)
        self.network_maps.setToolTip(
            "Запросы ограничены текущим окном карты и сохраняются в локальный кэш."
        )
        map_browse = QPushButton("Выбрать…")
        map_browse.clicked.connect(self.choose_map_package)
        map_row = QHBoxLayout()
        map_row.addWidget(self.map_package, 1)
        map_row.addWidget(map_browse)
        self.location_source = QComboBox()
        self.location_source.addItem("Не задано", "unset")
        self.location_source.addItem("Вручную · неподтверждённая база", "manual")
        self.location_source.addItem("GPS/NMEA · выбранный COM-порт", "gps")
        self.gps_port = QLineEdit("")
        self.gps_port.setPlaceholderText("Например: COM8")
        self.gps_candidates = QComboBox()
        self.gps_candidates.addItem("Кандидаты ещё не найдены", "")
        self.gps_candidates.setToolTip(
            "Поиск читает только системные названия COM-портов и не открывает их."
        )
        gps_find = QPushButton("Найти GPS-порты")
        gps_find.clicked.connect(self.discover_gps_ports)
        gps_candidate_row = QHBoxLayout()
        gps_candidate_row.addWidget(self.gps_candidates, 1)
        gps_candidate_row.addWidget(gps_find)
        self.gps_baud = QComboBox()
        for baud in (4_800, 9_600, 38_400, 57_600, 115_200):
            self.gps_baud.addItem(f"{baud:,}".replace(",", " "), baud)
        self.manual_latitude = QDoubleSpinBox()
        self.manual_latitude.setRange(-90.0, 90.0)
        self.manual_latitude.setDecimals(7)
        self.manual_longitude = QDoubleSpinBox()
        self.manual_longitude.setRange(-180.0, 180.0)
        self.manual_longitude.setDecimals(7)
        self.manual_update_confirm = QCheckBox(
            "Изменить защищённую ручную точку значениями ниже"
        )
        self.manual_update_confirm.setToolTip(
            "Сохранённые координаты намеренно не загружаются в интерфейс."
        )
        self.manual_latitude.setEnabled(False)
        self.manual_longitude.setEnabled(False)
        location_form.addRow("Сетевая карта", self.network_maps)
        location_form.addRow("Офлайн-пакет", map_row)
        location_form.addRow("Источник базы", self.location_source)
        location_form.addRow("Найденные кандидаты", gps_candidate_row)
        location_form.addRow("GPS-порт", self.gps_port)
        location_form.addRow("Скорость NMEA", self.gps_baud)
        location_form.addRow("Ручная база", self.manual_update_confirm)
        location_form.addRow("Широта вручную", self.manual_latitude)
        location_form.addRow("Долгота вручную", self.manual_longitude)
        location.content_layout.addLayout(location_form)
        location.content_layout.addWidget(
            InlineNotice(
                "Проверка геопривязки обязательна",
                "При ошибочной базе маркер карты и географические наложения будут "
                "некорректны. tinySA и одиночный RTL-SDR не измеряют азимут; "
                "система не рисует неподтверждённое направление.",
                level="warning",
            )
        )
        self.gps_status_notice = InlineNotice(
            "GPS не подключён",
            "Нажмите «Найти GPS-порты», выберите кандидат и примените настройки.",
            level="info",
        )
        location.content_layout.addWidget(self.gps_status_notice)
        # Kept only as an in-memory compatibility surface for old profiles and
        # tests. It is intentionally not placed in the visible layout and none
        # of these values participate in apply_settings().
        self._legacy_location_panel = location
        location.setObjectName("legacyLocationCompatibilityPanel")
        location.setParent(self.settings_content)
        location.hide()

        self.direction_panel = Panel(
            "Направление",
            subtitle="Необязательный угловой источник",
        )
        self.direction_panel.setObjectName("directionSettingsPanel")
        self.direction_panel.setMinimumHeight(470)
        self.direction_limitations = InlineNotice(
            "Без валидного источника угол недоступен",
            "Одиночный tinySA, RTL-SDR или HackRF One не измеряет азимут, "
            "положение или расстояние до физического источника.",
            level="warning",
        )
        self.direction_panel.content_layout.addWidget(self.direction_limitations)
        self.direction_panel.content_layout.addWidget(
            InlineNotice(
                "Допустимые источники направления",
                "Внешний откалиброванный датчик может дать измеренный угол. "
                "Ручная отметка всегда помечается как введённая оператором и "
                "неизмеренная; синтетическое направление доступно только в демо.",
                level="info",
            )
        )
        self.direction_panel.content_layout.addWidget(
            InlineNotice(
                "Направление не влияет на готовность RF",
                "Основной контур готов после цепочки «приёмник → измеренный "
                "кадр → интерпретация и события». Отсутствие углового датчика "
                "показывается отдельно и не подменяется расчётом по уровню.",
                level="ready",
            )
        )
        self.direction_panel.content_layout.addStretch(1)
        operational.addWidget(self.direction_panel)
        self.settings_layout.addLayout(operational)

        self.restart_notice = InlineNotice(
            "Контролируемое применение",
            "Параметры сначала валидируются. Изменения драйверов могут потребовать перезапуск.",
            level="info",
        )
        self.settings_layout.addWidget(self.restart_notice)
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        reset = QPushButton("Вернуть значения снимка")
        reset.clicked.connect(self.reset_from_runtime)
        self._settings_method = next(
            (
                method
                for method in ("update_settings", "configure")
                if callable(getattr(self.runtime, method, None))
            ),
            None,
        )
        self.apply_button = QPushButton(
            "Проверить и применить"
            if self._settings_method is not None
            else "Проверить значения"
        )
        self.apply_button.setProperty("primary", "true")
        self.apply_button.clicked.connect(self.apply_settings)
        if self._settings_method is None:
            self.apply_button.setToolTip(
                "Runtime работает только для чтения: значения будут проверены, но не сохранены."
            )
        action_row.addWidget(reset)
        action_row.addWidget(self.apply_button)
        self.settings_layout.addLayout(action_row)
        self.result = QLabel("")
        self.result.setProperty("secondary", "true")
        self.settings_layout.addWidget(self.result)
        self.settings_layout.addStretch(1)
        self._connect_dirty_signals()

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        if not self._loaded_once and not self._dirty:
            self._load_runtime_settings(snapshot)
        if self._dirty:
            self.header.status.set_status("ЕСТЬ НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ", "warning")
        else:
            self.header.status.set_status("КОНФИГУРАЦИЯ ЗАГРУЖЕНА", "ready")

    def _connect_dirty_signals(self) -> None:
        self.profile_name.textEdited.connect(self._mark_dirty)
        self.data_dir.textEdited.connect(self._mark_dirty)
        self.mode.currentIndexChanged.connect(self._mark_dirty)
        self.retention.valueChanged.connect(self._mark_dirty)
        self.minimum_free.valueChanged.connect(self._mark_dirty)
        self.center_frequency.valueChanged.connect(self._mark_dirty)
        self.span.valueChanged.connect(self._mark_dirty)
        self.sample_rate.valueChanged.connect(self._mark_dirty)
        self.threshold.valueChanged.connect(self._mark_dirty)
        self.real_adapters.toggled.connect(self._mark_dirty)
        self.experience.currentIndexChanged.connect(self._mark_dirty)
        self.hardware_kind.currentIndexChanged.connect(self._mark_dirty)
        self.hardware_kind.currentIndexChanged.connect(
            self._hardware_kind_changed
        )
        self.hardware_id.textEdited.connect(self._mark_dirty)
        self.hardware_connection.textEdited.connect(self._mark_dirty)
        self.hardware_rtlsdr_profile.currentIndexChanged.connect(self._mark_dirty)
        self.hardware_tinysa_model.currentIndexChanged.connect(
            self._hardware_tinysa_model_changed
        )
        self.hardware_tinysa_model.currentIndexChanged.connect(self._mark_dirty)
        self.hardware_tinysa_ultra_mode.toggled.connect(self._mark_dirty)
        self.hardware_enabled.toggled.connect(self._mark_dirty)
        self.manual_update_confirm.toggled.connect(self._manual_update_toggled)
        self.map_package.textEdited.connect(self._mark_dirty)
        self.network_maps.toggled.connect(self._mark_dirty)
        self.location_source.currentIndexChanged.connect(self._location_source_changed)
        self.gps_port.textEdited.connect(self._mark_dirty)
        self.gps_candidates.currentIndexChanged.connect(
            self._gps_candidate_selected
        )
        self.gps_baud.currentIndexChanged.connect(self._mark_dirty)
        self.manual_latitude.valueChanged.connect(self._mark_dirty)
        self.manual_longitude.valueChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args: object) -> None:
        if self._loading:
            return
        self._dirty = True
        self.header.status.set_status("ЕСТЬ НЕСОХРАНЁННЫЕ ИЗМЕНЕНИЯ", "warning")

    def _manual_update_toggled(self, checked: bool) -> None:
        enabled = checked and str(self.location_source.currentData()) == "manual"
        self.manual_latitude.setEnabled(enabled)
        self.manual_longitude.setEnabled(enabled)
        self._mark_dirty()

    def _location_source_changed(self, *_args: object) -> None:
        source = str(self.location_source.currentData())
        self.gps_port.setEnabled(source == "gps")
        self.gps_candidates.setEnabled(source == "gps")
        self.gps_baud.setEnabled(source == "gps")
        self.manual_update_confirm.setEnabled(source == "manual")
        if source != "manual":
            self.manual_update_confirm.setChecked(False)
        self._manual_update_toggled(self.manual_update_confirm.isChecked())
        self._mark_dirty()

    def _load_runtime_settings(self, snapshot: object | None) -> None:
        getter = getattr(self.runtime, "settings_snapshot", None)
        source = getter() if callable(getter) else attr(self.runtime, "config")
        self._loading = True
        try:
            self.profile_name.setText(
                str(attr(source, "profile_name", attr(snapshot, "profile_name", "Профиль")))
            )
            mode_key = str(attr(source, "mode", provenance_key(snapshot))).lower()
            runtime_override = attr(source, "runtime_override")
            explicit_demo = runtime_override == "demo"
            if explicit_demo and self.mode.findData("demo") < 0:
                self.mode.addItem("Демо · явный запуск --demo", "demo")
            display_mode = (
                "demo"
                if explicit_demo
                else ("live" if mode_key in {"demo", "simulated"} else mode_key)
            )
            index = self.mode.findData(display_mode)
            if index >= 0:
                self.mode.setCurrentIndex(index)
            locked = runtime_override in {"live", "demo", "safe"}
            self.mode.setEnabled(not locked)
            self.real_adapters.setEnabled(
                not locked or runtime_override == "live"
            )
            if locked:
                self.mode.setToolTip(
                    f"Режим «{runtime_override}» зафиксирован параметром запуска."
                )

            storage = attr(source, "storage")
            self.data_dir.setText(str(attr(storage, "data_dir", "runtime-data")))
            self.retention.setValue(int(attr(storage, "retention_days", 30)))
            self.minimum_free.setValue(float(attr(storage, "minimum_free_gib", 5.0)))

            devices = attr(source, "devices")
            self.real_adapters.setChecked(
                bool(attr(devices, "enable_real_adapters", False))
            )
            adapters = attr(devices, "adapters", [])
            self._set_hardware_rows(
                adapters if isinstance(adapters, (list, tuple)) else ()
            )

            map_config = attr(source, "map")
            package_path = attr(map_config, "package_path", "")
            self._loaded_map_path = "" if package_path is None else str(package_path)
            self.map_package.setText(self._loaded_map_path)
            self.network_maps.setChecked(
                bool(attr(map_config, "network_enabled", True))
            )
            self._online_cache_mib = int(
                attr(map_config, "online_cache_mib", 256)
            )

            location = attr(source, "location")
            self._loaded_location_source = str(attr(location, "source", "unset"))
            self._loaded_gps_port = str(attr(location, "gps_port", ""))
            self._loaded_gps_baud = int(attr(location, "gps_baud", 9_600))
            source_index = self.location_source.findData(self._loaded_location_source)
            if source_index >= 0:
                self.location_source.setCurrentIndex(source_index)
            self.gps_port.setText(self._loaded_gps_port)
            baud_index = self.gps_baud.findData(self._loaded_gps_baud)
            if baud_index >= 0:
                self.gps_baud.setCurrentIndex(baud_index)
            self.manual_update_confirm.setChecked(False)
            self._location_source_changed()

            ui_config = attr(source, "ui")
            experience_index = self.experience.findData(
                str(attr(ui_config, "experience_level", "guided"))
            )
            if experience_index >= 0:
                self.experience.setCurrentIndex(experience_index)

            spectrum = attr(source, "spectrum")
            self.center_frequency.setValue(
                float(attr(spectrum, "center_frequency_hz", 433_920_000)) / 1_000_000
            )
            self.span.setValue(float(attr(spectrum, "span_hz", 2_000_000)) / 1_000)
            self.sample_rate.setValue(
                float(attr(spectrum, "sample_rate_hz", 2_400_000)) / 1_000_000
            )
            self.threshold.setValue(float(attr(spectrum, "threshold_level", -72.4)))
        finally:
            self._loading = False
        self._dirty = False
        self._loaded_once = True

    def reset_from_runtime(self) -> None:
        self._dirty = False
        self._loaded_once = False
        self.result.clear()
        self.refresh(current_snapshot(self.runtime))

    def choose_data_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Каталог данных ALGA VECTOR", self.data_dir.text()
        )
        if selected:
            self.data_dir.setText(selected)
            self._mark_dirty()

    def choose_map_package(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Локальный пакет карты",
            self.map_package.text(),
            "MBTiles (*.mbtiles)",
        )
        if selected:
            self.map_package.setText(selected)
            self._mark_dirty()

    def discover_gps_ports(self) -> None:
        discover = getattr(self.runtime, "discover_gps_ports", None)
        if not callable(discover):
            self.gps_status_notice.set_notice(
                "Поиск GPS недоступен",
                "Этот runtime не поддерживает чтение списка COM-портов.",
                level="warning",
            )
            return
        ok, result = call_runtime(self.runtime, "discover_gps_ports")
        if not ok:
            self.gps_status_notice.set_notice(
                "Не удалось получить COM-порты",
                str(result),
                level="critical",
            )
            return
        candidates = tuple(result) if isinstance(result, (list, tuple)) else ()
        selected_port = self.gps_port.text().strip().upper()
        self.gps_candidates.blockSignals(True)
        self.gps_candidates.clear()
        self.gps_candidates.addItem("Выберите найденный порт…", "")
        selected_index = -1
        for candidate in candidates:
            port = str(attr(candidate, "port", "")).strip().upper()
            if not port:
                continue
            label = str(attr(candidate, "display_name", port))
            confidence = str(attr(candidate, "confidence", "possible"))
            prefix = "GPS/GNSS вероятен" if confidence == "likely" else "Проверить"
            self.gps_candidates.addItem(f"{prefix} · {label}", port)
            row = self.gps_candidates.count() - 1
            self.gps_candidates.setItemData(
                row,
                str(attr(candidate, "reason_ru", "")),
                Qt.ItemDataRole.ToolTipRole,
            )
            if port == selected_port:
                selected_index = row
        if self.gps_candidates.count() == 1:
            self.gps_candidates.addItem("COM-порты не найдены", "")
        elif selected_index >= 0:
            self.gps_candidates.setCurrentIndex(selected_index)
        self.gps_candidates.blockSignals(False)
        self.gps_status_notice.set_notice(
            "Кандидаты обновлены",
            (
                f"Найдено COM-портов: {len(candidates)}. Ни один порт не был открыт."
                if candidates
                else "Windows не сообщил доступных COM-портов. Ни один порт не открывался."
            ),
            level="ready" if candidates else "warning",
        )

    def _gps_candidate_selected(self, index: int) -> None:
        port = str(self.gps_candidates.itemData(index) or "").strip()
        if not port:
            return
        self.gps_port.setText(port)
        self._mark_dirty()

    def _refresh_gps_status(self) -> None:
        getter = getattr(self.runtime, "gps_status", None)
        if not callable(getter):
            return
        try:
            status = getter()
        except Exception:
            self.gps_status_notice.set_notice(
                "GPS-статус недоступен",
                "Не удалось прочитать локальное состояние GPS.",
                level="warning",
            )
            return
        if not isinstance(status, dict):
            return
        state = str(status.get("fix_state", "disconnected"))
        title, message, level = {
            "searching": (
                "GPS ищет фиксацию",
                "Порт открыт только для чтения NMEA; ожидаются GSA/GGA.",
                "info",
            ),
            "no_fix": (
                "GPS: фиксации нет",
                "Проверьте антенну и обзор неба; координаты не используются.",
                "warning",
            ),
            "fix": (
                "GPS-фиксация есть",
                "Приёмник не сообщил размерность; ожидается GSA.",
                "ready",
            ),
            "fix_2d": (
                "GPS: 2D-фиксация",
                "Горизонтальная позиция доступна; высота не подтверждена.",
                "ready",
            ),
            "fix_3d": (
                "GPS: 3D-фиксация",
                "Приёмник сообщил горизонтальную позицию и высоту.",
                "ready",
            ),
            "stale": (
                "GPS-данные устарели",
                "Карта не использует старую фиксацию до поступления свежих данных.",
                "critical",
            ),
            "jump_suspected": (
                "GPS-скачок отклонён",
                "Положение базы не изменено; проверьте приёмник и антенну.",
                "critical",
            ),
        }.get(
            state,
            (
                "GPS не подключён",
                "Выберите COM-порт либо используйте ручную базу.",
                "info",
            ),
        )
        accepted = int(status.get("accepted_sentences", 0))
        rejected = int(status.get("rejected_sentences", 0))
        if accepted or rejected:
            message += f" NMEA принято: {accepted}; отклонено: {rejected}."
        self.gps_status_notice.set_notice(title, message, level=level)

    def apply_settings(self) -> None:
        profile = self.profile_name.text().strip()
        if not profile:
            self.header.status.set_status("ИМЯ ПРОФИЛЯ ОБЯЗАТЕЛЬНО", "critical")
            return
        if not self._commit_hardware_editor(show_error=True):
            return
        adapters = self._hardware_rows_payload()
        requested_center_hz = round(self.center_frequency.value() * 1_000_000)
        requested_span_hz = round(self.span.value() * 1_000)
        requested_sample_rate_hz = round(self.sample_rate.value() * 1_000_000)
        tuning_warning = ""
        for tuning_profile in _settings_rtlsdr_profiles(
            adapters,
            current_snapshot(self.runtime),
        ):
            validation = validate_rtlsdr_tuning(
                tuning_profile,
                center_frequency_hz=requested_center_hz,
                span_hz=requested_span_hz,
                sample_rate_hz=requested_sample_rate_hz,
            )
            if not validation.accepted:
                self.header.status.set_status(
                    "ПАРАМЕТРЫ ВНЕ ВОЗМОЖНОСТЕЙ RTL-SDR",
                    "critical",
                )
                self.result.setText(
                    " ".join(
                        part
                        for part in (
                            validation.message_ru,
                            validation.operator_action_ru,
                        )
                        if part
                    )
                )
                return
            tuning_warning = validation.warning_ru or tuning_warning
        for hardware_profile in _settings_receiver_hardware_profiles(
            adapters,
            current_snapshot(self.runtime),
        ):
            hardware_validation = hardware_profile.validate_tuning(
                center_frequency_hz=requested_center_hz,
                span_hz=requested_span_hz,
                sample_rate_hz=requested_sample_rate_hz,
            )
            if not hardware_validation.accepted:
                self.header.status.set_status(
                    "ПАРАМЕТРЫ ВНЕ ВОЗМОЖНОСТЕЙ ПРИЁМНИКА",
                    "critical",
                )
                self.result.setText(
                    " ".join(
                        part
                        for part in (
                            hardware_validation.message_ru,
                            hardware_validation.operator_action_ru,
                        )
                        if part
                    )
                )
                return
            tuning_warning = hardware_validation.warning_ru or tuning_warning
        payload = {
            "profile_name": profile,
            "mode": self.mode.currentData(),
            "storage": {
                "data_dir": self.data_dir.text().strip(),
                "retention_days": self.retention.value(),
                "minimum_free_gib": self.minimum_free.value(),
            },
            "devices": {
                "enable_real_adapters": self.real_adapters.isChecked(),
                "adapters": adapters,
            },
            "spectrum": {
                "center_frequency_hz": requested_center_hz,
                "span_hz": requested_span_hz,
                "sample_rate_hz": requested_sample_rate_hz,
                "threshold_level": self.threshold.value(),
            },
            "ui": {
                "experience_level": self.experience.currentData(),
                "hide_exact_coordinates": True,
            },
        }
        self.settings_applied.emit(payload)
        if self._settings_method is None:
            self.header.status.set_status("ЗНАЧЕНИЯ ПРОВЕРЕНЫ", "warning")
            self.result.setText(
                "Текущий runtime работает только для чтения. "
                "Для сохранения нужен update_settings(payload)."
            )
            return
        ok, result = call_runtime(self.runtime, self._settings_method, payload)
        if ok:
            self.header.status.set_status("НАСТРОЙКИ ПРИМЕНЕНЫ", "ready")
            result_text = str(result) if result is not None else "Изменения сохранены"
            if tuning_warning:
                result_text = f"{result_text}\nПредупреждение: {tuning_warning}"
            self.result.setText(result_text)
            self._dirty = False
            self._loaded_once = False
            self.refresh(current_snapshot(self.runtime))
        else:
            self.header.status.set_status("ОШИБКА СОХРАНЕНИЯ", "critical")
            self.result.setText(str(result))

    def _set_hardware_rows(self, adapters: object) -> None:
        entries = tuple(adapters) if isinstance(adapters, (list, tuple)) else ()
        self.hardware_table.setRowCount(len(entries))
        for row, adapter in enumerate(entries):
            enabled = bool(attr(adapter, "enabled", True))
            rtlsdr_profile = str(attr(adapter, "rtlsdr_profile", "auto"))
            tinysa_model = str(
                getattr(
                    attr(adapter, "tinysa_model", "auto"),
                    "value",
                    attr(adapter, "tinysa_model", "auto"),
                )
            )
            tinysa_ultra_mode = bool(
                attr(adapter, "tinysa_ultra_mode", False)
            )
            values = (
                str(attr(adapter, "id", "")),
                str(attr(adapter, "kind", "")),
                str(attr(adapter, "connection", "")),
                _receiver_profile_editor_label(
                    str(attr(adapter, "kind", "")),
                    rtlsdr_profile=rtlsdr_profile,
                    tinysa_model=tinysa_model,
                    tinysa_ultra_mode=tinysa_ultra_mode,
                ),
                "Включён" if enabled else "Отключён",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 3:
                    item.setData(0x0100, rtlsdr_profile)
                if column == 4:
                    item.setData(0x0100, enabled)
                if column == 0:
                    item.setData(0x0101, tinysa_model)
                    item.setData(0x0102, tinysa_ultra_mode)
                self.hardware_table.setItem(row, column, item)
        if entries:
            self.hardware_table.selectRow(0)
        else:
            self._clear_hardware_editor()
        self._update_receiver_limits()

    def load_selected_hardware(self) -> None:
        row = self.hardware_table.currentRow()
        if row < 0:
            return
        previous_loading = self._loading
        self._loading = True
        try:
            kind = self._hardware_cell(row, 1)
            index = self.hardware_kind.findData(kind)
            if index < 0:
                self.hardware_kind.addItem(f"Сохранённый тип · {kind}", kind)
                index = self.hardware_kind.count() - 1
            self.hardware_kind.setCurrentIndex(index)
            self.hardware_id.setText(self._hardware_cell(row, 0))
            self.hardware_connection.setText(self._hardware_cell(row, 2))
            profile_item = self.hardware_table.item(row, 3)
            profile_value = (
                str(profile_item.data(0x0100) or "auto")
                if profile_item is not None
                else "auto"
            )
            profile_index = self.hardware_rtlsdr_profile.findData(profile_value)
            self.hardware_rtlsdr_profile.setCurrentIndex(
                profile_index if profile_index >= 0 else 0
            )
            self.hardware_rtlsdr_profile.setEnabled(kind == "rtlsdr")
            id_item = self.hardware_table.item(row, 0)
            tinysa_model = (
                str(id_item.data(0x0101) or "auto")
                if id_item is not None
                else "auto"
            )
            tinysa_index = self.hardware_tinysa_model.findData(tinysa_model)
            self.hardware_tinysa_model.setCurrentIndex(
                tinysa_index if tinysa_index >= 0 else 0
            )
            self.hardware_tinysa_ultra_mode.setChecked(
                bool(id_item.data(0x0102))
                if id_item is not None
                else False
            )
            self._sync_tinysa_editor(kind == "tinysa")
            status = self.hardware_table.item(row, 4)
            self.hardware_enabled.setChecked(
                bool(status.data(0x0100)) if status is not None else True
            )
        finally:
            self._loading = previous_loading

    def commit_hardware_editor(self) -> None:
        self._commit_hardware_editor(show_error=True)

    def _commit_hardware_editor(self, *, show_error: bool) -> bool:
        kind = str(self.hardware_kind.currentData() or "")
        adapter_id = self.hardware_id.text().strip()
        connection = self.hardware_connection.text().strip()
        if not any((kind, adapter_id, connection)):
            return True
        if not all((kind, adapter_id, connection)):
            if show_error:
                self.header.status.set_status(
                    "НЕПОЛНАЯ НАСТРОЙКА ПРИЁМНИКА",
                    "critical",
                )
                self.result.setText(
                    "Укажите тип, ID и точное подключение либо очистите редактор."
                )
            return False
        row = next(
            (
                index
                for index in range(self.hardware_table.rowCount())
                if self._hardware_cell(index, 0) == adapter_id
            ),
            -1,
        )
        if row < 0:
            row = self.hardware_table.rowCount()
            self.hardware_table.insertRow(row)
        enabled = self.hardware_enabled.isChecked()
        tinysa_model = (
            str(self.hardware_tinysa_model.currentData() or "auto")
            if kind == "tinysa"
            else "auto"
        )
        tinysa_ultra_mode = (
            self.hardware_tinysa_ultra_mode.isChecked()
            if kind == "tinysa"
            else False
        )
        rtlsdr_profile = (
            str(self.hardware_rtlsdr_profile.currentData() or "auto")
            if kind == "rtlsdr"
            else "auto"
        )
        values = (
            adapter_id,
            kind,
            connection,
            _receiver_profile_editor_label(
                kind,
                rtlsdr_profile=rtlsdr_profile,
                tinysa_model=tinysa_model,
                tinysa_ultra_mode=tinysa_ultra_mode,
            ),
            "Включён" if enabled else "Отключён",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 3:
                item.setData(0x0100, rtlsdr_profile)
            if column == 4:
                item.setData(0x0100, enabled)
            if column == 0:
                item.setData(0x0101, tinysa_model)
                item.setData(0x0102, tinysa_ultra_mode)
            self.hardware_table.setItem(row, column, item)
        self.hardware_table.selectRow(row)
        self._update_receiver_limits()
        self._mark_dirty()
        return True

    def remove_selected_hardware(self) -> None:
        row = self.hardware_table.currentRow()
        if row < 0:
            return
        self.hardware_table.removeRow(row)
        self._clear_hardware_editor()
        self._update_receiver_limits()
        self._mark_dirty()

    def _clear_hardware_editor(self) -> None:
        previous_loading = self._loading
        self._loading = True
        try:
            self.hardware_kind.setCurrentIndex(0)
            self.hardware_id.clear()
            self.hardware_connection.clear()
            self.hardware_rtlsdr_profile.setCurrentIndex(0)
            self.hardware_rtlsdr_profile.setEnabled(False)
            self.hardware_tinysa_model.setCurrentIndex(0)
            self.hardware_tinysa_model.setEnabled(False)
            self.hardware_tinysa_ultra_mode.setChecked(False)
            self.hardware_tinysa_ultra_mode.setEnabled(False)
            self.hardware_enabled.setChecked(True)
        finally:
            self._loading = previous_loading

    def _hardware_rows_payload(self) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for row in range(self.hardware_table.rowCount()):
            id_item = self.hardware_table.item(row, 0)
            profile_item = self.hardware_table.item(row, 3)
            status = self.hardware_table.item(row, 4)
            kind = self._hardware_cell(row, 1)
            entry: dict[str, object] = {
                "id": self._hardware_cell(row, 0),
                "kind": kind,
                "enabled": bool(status.data(0x0100)) if status is not None else True,
                "connection": self._hardware_cell(row, 2),
                "rtlsdr_profile": (
                    str(profile_item.data(0x0100) or "auto")
                    if profile_item is not None
                    else "auto"
                ),
            }
            if kind == "tinysa":
                entry["tinysa_model"] = (
                    str(id_item.data(0x0101) or "auto")
                    if id_item is not None
                    else "auto"
                )
                entry["tinysa_ultra_mode"] = (
                    bool(id_item.data(0x0102))
                    if id_item is not None
                    else False
                )
            output.append(entry)
        return output

    def _hardware_kind_changed(self, _index: int = -1) -> None:
        kind = str(self.hardware_kind.currentData() or "")
        is_rtlsdr = kind == "rtlsdr"
        self.hardware_rtlsdr_profile.setEnabled(is_rtlsdr)
        if not is_rtlsdr:
            self.hardware_rtlsdr_profile.setCurrentIndex(0)
        self._sync_tinysa_editor(kind == "tinysa")

    def _hardware_tinysa_model_changed(self, _index: int = -1) -> None:
        self._sync_tinysa_editor(
            str(self.hardware_kind.currentData() or "") == "tinysa"
        )

    def _sync_tinysa_editor(self, is_tinysa: bool) -> None:
        self.hardware_tinysa_model.setEnabled(is_tinysa)
        model = str(self.hardware_tinysa_model.currentData() or "auto")
        ultra_capable = model in {
            TinySaModel.ULTRA_ZS405.value,
            TinySaModel.ULTRA_PLUS_ZS406.value,
            TinySaModel.ULTRA_PLUS_ZS407.value,
        }
        self.hardware_tinysa_ultra_mode.setEnabled(
            is_tinysa and ultra_capable
        )
        if not is_tinysa or not ultra_capable:
            self.hardware_tinysa_ultra_mode.setChecked(False)

    def _update_receiver_limits(self) -> None:
        adapters = self._hardware_rows_payload()
        snapshot = current_snapshot(self.runtime)
        rtl_profiles = _settings_rtlsdr_profiles(adapters, snapshot)
        hardware_profiles = _settings_receiver_hardware_profiles(
            adapters,
            snapshot,
        )
        if not rtl_profiles and not hardware_profiles:
            self.center_frequency.setRange(0.001, 6_000.0)
            self.span.setMaximum(100_000.0)
            self.sample_rate.setRange(0.008, 64.0)
            self.receiver_capability_note.setText(
                "Аппаратный профиль ещё не подтверждён. Точные границы "
                "появятся после выбора или обнаружения приёмника; runtime "
                "всё равно отклонит неподдерживаемую настройку до захвата."
            )
            return
        minimum_values_hz = [
            profile.minimum_frequency_hz for profile in rtl_profiles
        ]
        minimum_values_hz.extend(
            profile.minimum_frequency_hz for profile in hardware_profiles
        )
        maximum_values_hz = [
            profile.maximum_frequency_hz for profile in rtl_profiles
        ]
        maximum_values_hz.extend(
            profile.maximum_frequency_hz for profile in hardware_profiles
        )
        minimum_hz = max(minimum_values_hz)
        maximum_hz = min(maximum_values_hz)
        self.center_frequency.setRange(
            minimum_hz / 1_000_000,
            maximum_hz / 1_000_000,
        )
        span_limits_hz = [3_200_000 for _profile in rtl_profiles]
        span_limits_hz.extend(
            profile.maximum_instantaneous_span_hz
            or (profile.maximum_frequency_hz - profile.minimum_frequency_hz)
            for profile in hardware_profiles
        )
        self.span.setMaximum(min(span_limits_hz) / 1_000)
        sample_minimums_hz = [225_001 for _profile in rtl_profiles]
        sample_maximums_hz = [3_200_000 for _profile in rtl_profiles]
        sample_minimums_hz.extend(
            profile.minimum_sample_rate_hz
            for profile in hardware_profiles
            if profile.minimum_sample_rate_hz is not None
        )
        sample_maximums_hz.extend(
            profile.maximum_sample_rate_hz
            for profile in hardware_profiles
            if profile.maximum_sample_rate_hz is not None
        )
        if sample_minimums_hz and sample_maximums_hz:
            self.sample_rate.setRange(
                max(sample_minimums_hz) / 1_000_000,
                min(sample_maximums_hz) / 1_000_000,
            )
        else:
            self.sample_rate.setRange(0.008, 64.0)
        profile_names = [
            profile.display_name_ru for profile in rtl_profiles
        ]
        profile_names.extend(
            profile.display_name_ru for profile in hardware_profiles
        )
        selected_names = ", ".join(dict.fromkeys(profile_names))
        self.receiver_capability_note.setText(
            f"Подтверждённый общий диапазон перестройки: "
            f"{minimum_hz / 1_000_000:g}–"
            f"{maximum_hz / 1_000_000:g} МГц ({selected_names}). "
            f"Допустимая полоса в этом профиле: до "
            f"{min(span_limits_hz) / 1_000_000:g} МГц. "
            "Широкий диапазон просматривается перестройкой, а не считается "
            "одновременным приёмом."
        )

    def _hardware_cell(self, row: int, column: int) -> str:
        item = self.hardware_table.item(row, column)
        return item.text().strip() if item is not None else ""


def _rtlsdr_profile_editor_label(profile: str) -> str:
    return {
        "auto": "Авто",
        "generic": "Обычный · 24–1766 МГц",
        "blog_v4": "Blog V4 · HF после подтверждения драйвера",
        "blog_v3_direct_q": "Blog V3 · Q-direct",
    }.get(profile, "Авто")


def _receiver_profile_editor_label(
    kind: str,
    *,
    rtlsdr_profile: str,
    tinysa_model: str,
    tinysa_ultra_mode: bool,
) -> str:
    if kind == "rtlsdr":
        return _rtlsdr_profile_editor_label(rtlsdr_profile)
    if kind == "hackrf":
        return "HackRF One · RX 1–6000 МГц"
    if kind == "tinysa":
        model_label = {
            "auto": "tinySA · авто по firmware",
            TinySaModel.BASIC.value: "tinySA Basic",
            TinySaModel.ULTRA_ZS405.value: "tinySA Ultra ZS405",
            TinySaModel.ULTRA_PLUS_ZS406.value: "tinySA Ultra+ ZS406",
            TinySaModel.ULTRA_PLUS_ZS407.value: "tinySA Ultra+ ZS407",
        }.get(tinysa_model, "tinySA · модель не подтверждена")
        return (
            f"{model_label} · Ultra mode подтверждён"
            if tinysa_ultra_mode
            else f"{model_label} · обычный режим"
        )
    return "Авто"


def _settings_rtlsdr_profiles(
    adapters: list[dict[str, object]],
    snapshot: object | None,
) -> tuple[RtlSdrTuningProfile, ...]:
    raw_devices = attr(snapshot, "devices", ())
    devices = (
        tuple(raw_devices)
        if isinstance(raw_devices, (list, tuple))
        else ()
    )
    profiles: list[RtlSdrTuningProfile] = []
    for adapter in adapters:
        if adapter.get("kind") != "rtlsdr" or not bool(
            adapter.get("enabled", True)
        ):
            continue
        override = str(adapter.get("rtlsdr_profile", "auto"))
        connection = str(adapter.get("connection", ""))
        matched = next(
            (
                device
                for device in devices
                if str(attr(device, "kind", "")).lower() == "rtlsdr"
                and str(attr(device, "connection", "")) == connection
            ),
            None,
        )
        metrics = attr(matched, "metrics", {}) or {}
        detected_profile_id = attr(metrics, "detected_tuning_profile_id")
        if override == "blog_v4":
            profiles.append(
                BLOG_V4_PROFILE
                if detected_profile_id == BLOG_V4_PROFILE.profile_id
                else GENERIC_RTLSDR_PROFILE
            )
            continue
        if override == "blog_v3_direct_q":
            profiles.append(BLOG_V3_DIRECT_Q_PROFILE)
            continue
        if override == "generic":
            profiles.append(GENERIC_RTLSDR_PROFILE)
            continue
        profile_id = attr(metrics, "tuning_profile_id")
        profiles.append(
            rtlsdr_profile_by_id(profile_id)
            if profile_id
            else GENERIC_RTLSDR_PROFILE
        )
    return tuple(profiles)


def _settings_receiver_hardware_profiles(
    adapters: list[dict[str, object]],
    snapshot: object | None,
) -> tuple[ReceiverHardwareProfile, ...]:
    """Resolve non-RTL limits from configuration and measured device metadata."""

    raw_devices = attr(snapshot, "devices", ())
    devices = (
        tuple(raw_devices)
        if isinstance(raw_devices, (list, tuple))
        else ()
    )
    profiles: list[ReceiverHardwareProfile] = []
    for adapter in adapters:
        if not bool(adapter.get("enabled", True)):
            continue
        kind = str(adapter.get("kind", "")).lower()
        if kind == "hackrf":
            profiles.append(HACKRF_ONE_PROFILE)
            continue
        if kind != "tinysa":
            continue

        connection = str(adapter.get("connection", ""))
        matched = next(
            (
                device
                for device in devices
                if str(attr(device, "kind", "")).lower() == "tinysa"
                and str(attr(device, "connection", "")) == connection
            ),
            None,
        )
        metrics = attr(matched, "metrics", {}) or {}
        configured_model = str(adapter.get("tinysa_model", "auto"))
        model_value = (
            str(attr(metrics, "detected_model", ""))
            if configured_model == "auto"
            else configured_model
        )
        try:
            model = TinySaModel(model_value)
        except ValueError:
            continue
        profiles.append(
            tinysa_hardware_profile(
                model,
                ultra_mode_enabled=bool(
                    adapter.get("tinysa_ultra_mode", False)
                ),
            )
        )
    return tuple(profiles)
