"""First-run validation dialog for civilian multi-sensor early warning."""

from __future__ import annotations

# ruff: noqa: RUF001
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .runtime import attr, call_runtime, current_snapshot, items, value_of
from .theme import Colors
from .widgets import InlineNotice, Panel, StatusBadge


class OnboardingPage(QWidget):
    """Simple titled page reusable by the onboarding dialog."""

    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setProperty("heading", "true")
        description = QLabel(subtitle)
        description.setWordWrap(True)
        description.setProperty("secondary", "true")
        layout.addWidget(heading)
        layout.addWidget(description)
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(10)
        layout.addLayout(self.content_layout, 1)


class OnboardingDialog(QDialog):
    """Six-step setup that performs no hardware action without an explicit click."""

    def __init__(self, runtime: object | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.setWindowTitle("Первый запуск · ALGA VECTOR")
        self.setModal(True)
        self.resize(760, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._welcome_page())
        self.stack.addWidget(self._experience_page())
        self.stack.addWidget(self._storage_page())
        self.stack.addWidget(self._receiver_page())
        self.stack.addWidget(self._interpretation_page())
        self.stack.addWidget(self._finish_page())
        root.addWidget(self.stack, 1)

        footer = QWidget()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.step_label = QLabel("Шаг 1 из 6")
        self.step_label.setProperty("muted", "true")
        signature = QLabel("Разработал: Буйвол и Задира")
        signature.setProperty("muted", "true")
        self.completion_error = QLabel("")
        self.completion_error.setStyleSheet(f"color: {Colors.CRITICAL};")
        self.back_button = QPushButton("Назад")
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Продолжить")
        self.next_button.setProperty("primary", "true")
        self.next_button.clicked.connect(self.next)
        footer_layout.addWidget(self.step_label)
        footer_layout.addWidget(signature)
        footer_layout.addWidget(self.completion_error, 1)
        footer_layout.addWidget(self.back_button)
        footer_layout.addWidget(self.next_button)
        root.addWidget(footer)
        self._sync_navigation()

    def _welcome_page(self) -> QWidget:
        page = OnboardingPage(
            "Добро пожаловать в ALGA VECTOR",
            "Настроим локальное хранилище, первый сенсор и способ представления "
            "наблюдений. Основные контуры работают локально и без облака.",
        )
        page.content_layout.addWidget(
            InlineNotice(
                "Пассивное гражданское наблюдение",
                "ALGA VECTOR принимает RF- и акустические данные, а также "
                "локальный публичный ADS-B/Mode-S контекст. Передача RF не выполняется.",
                level="ready",
            )
        )
        product = QLabel(
            "Операционный интерфейс:\n"
            "• спектральный контроль и водопад;\n"
            "• акустические признаки с проверкой качества и устойчивости;\n"
            "• объяснимая временная корреляция независимых сенсоров;\n"
            "• угловой контекст только от явно валидированного источника;\n"
            "• публичный гражданский эфирный контекст без определения статуса;\n"
            "• локальный журнал и support bundle."
        )
        product.setProperty("secondary", "true")
        page.content_layout.addWidget(product)
        page.content_layout.addStretch(1)
        return page

    def _experience_page(self) -> QWidget:
        page = OnboardingPage(
            "Как показывать информацию",
            "Оба режима используют одни и те же сенсорные данные, временные "
            "фильтры и защитные блокировки.",
        )
        panel = Panel("Уровень интерфейса")
        self.experience = QComboBox()
        self.experience.addItem("Новичок · объяснения и следующий шаг", "guided")
        self.experience.addItem("Эксперт · полная телеметрия и флаги", "expert")
        panel.content_layout.addWidget(self.experience)
        panel.content_layout.addWidget(
            InlineNotice(
                "Можно изменить позже",
                "Режим «Новичок» раскладывает вывод по шагам, а "
                "режим «Эксперт» не отключает проверки качества.",
                level="info",
            )
        )
        page.content_layout.addWidget(panel)
        page.content_layout.addStretch(1)
        return page

    def _storage_page(self) -> QWidget:
        page = OnboardingPage(
            "Локальное хранилище",
            "Записи создаются только в выбранном оператором каталоге.",
        )
        panel = Panel("Каталог данных")
        self.storage_path = QLineEdit("runtime-data")
        self.storage_path.setPlaceholderText("Например: D:\\ALGA_CAPTURE")
        panel.content_layout.addWidget(self.storage_path)
        self.storage_status = StatusBadge("БУДЕТ ПРОВЕРЕНО ПРИ ПРИМЕНЕНИИ", "info")
        panel.content_layout.addWidget(
            self.storage_status,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        page.content_layout.addWidget(panel)
        page.content_layout.addWidget(
            InlineNotice(
                "Защита данных",
                "При нехватке места запись останавливается с явной ошибкой; "
                "успешное сохранение не имитируется.",
                level="warning",
            )
        )
        page.content_layout.addStretch(1)
        return page

    def _receiver_page(self) -> QWidget:
        page = OnboardingPage(
            "Приёмник",
            "Можно пропустить шаг и добавить устройство позже. Проверка уже "
            "настроенных подключений запускается только кнопкой ниже.",
        )
        panel = Panel("Первый приёмник")
        form = QFormLayout()
        self.hardware_kind = QComboBox()
        self.hardware_kind.addItem("Пока не настраивать", "")
        self.hardware_kind.addItem("tinySA · USB Serial", "tinysa")
        self.hardware_kind.addItem("RTL-SDR · IQ", "rtlsdr")
        self.hardware_kind.addItem("HackRF One · только приём", "hackrf")
        self.hardware_id = QLineEdit("")
        self.hardware_id.setPlaceholderText("Например: receiver-01")
        self.hardware_connection = QLineEdit("")
        self.hardware_connection.setPlaceholderText(
            "COM7 · RTLSDR:0 · HACKRF:<серийный номер>"
        )
        form.addRow("Тип", self.hardware_kind)
        form.addRow("Стабильный ID", self.hardware_id)
        form.addRow("Точное подключение", self.hardware_connection)
        panel.content_layout.addLayout(form)
        panel.content_layout.addWidget(
            InlineNotice(
                "Пределы задаёт устройство",
                "Диапазон перестройки и мгновенная полоса берутся из "
                "подтверждённого аппаратного профиля. Неподдерживаемая "
                "настройка блокируется до запуска измерений.",
                level="info",
            )
        )
        page.content_layout.addWidget(panel)
        scan = QPushButton("Проверить уже настроенные подключения")
        scan.clicked.connect(self.scan_devices)
        self.device_status = StatusBadge("ПРОВЕРКА НЕ ЗАПУСКАЛАСЬ", "neutral")
        self.device_list = QListWidget()
        page.content_layout.addWidget(scan)
        page.content_layout.addWidget(
            self.device_status,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        page.content_layout.addWidget(self.device_list, 1)
        return page

    def _interpretation_page(self) -> QWidget:
        page = OnboardingPage(
            "Интерпретация и ограничения",
            "Система описывает наблюдаемые RF-, акустические и временные "
            "признаки, но не приписывает им физический источник или намерение.",
        )
        page.setObjectName("interpretationLimitsPage")
        panel = Panel("Что означают результаты")
        panel.content_layout.addWidget(
            InlineNotice(
                "Наблюдения, а не личности объектов",
                "Доступны нейтральные классы RF-активности, акустического "
                "изменения, неподтверждённой аномалии, фонового состояния "
                "и согласованного многосенсорного наблюдения.",
                level="ready",
            )
        )
        panel.content_layout.addWidget(
            InlineNotice(
                "Уверенность — качество гипотезы",
                "Сила признаков отражает согласованность измерений, качество "
                "данных и устойчивость во времени. Это не вероятность "
                "идентичности физического источника.",
                level="warning",
            )
        )
        panel.content_layout.addWidget(
            InlineNotice(
                "Направление необязательно",
                "Одиночный приёмник не создаёт угловой контекст или расстояние. "
                "Направление показывается только от валидного внешнего датчика, "
                "ручной отметки оператора или явно маркированной демо-симуляции.",
                level="info",
            )
        )
        panel.content_layout.addWidget(
            InlineNotice(
                "Гражданский ADS-B — только контекст",
                "Публичные эфирные сообщения не определяют статус, не подтверждают "
                "другой сенсорный эпизод и не устанавливают намерение.",
                level="info",
            )
        )
        page.content_layout.addWidget(panel)
        page.content_layout.addStretch(1)
        return page

    def _finish_page(self) -> QWidget:
        page = OnboardingPage(
            "Готово к безопасному запуску",
            "Профиль можно изменить позже; живой режим не подставляет "
            "синтетические данные вместо отсутствующего сенсора.",
        )
        page.content_layout.addWidget(
            InlineNotice(
                "Происхождение данных",
                "Живые, воспроизведённые и синтетические данные маркируются "
                "на каждом экране.",
                level="info",
            )
        )
        summary = QLabel(
            "После завершения откроется обзор системы. Готовность основного "
            "контура определяется цепочкой «сенсоры → проверка качества → "
            "устойчивость во времени → объяснимая корреляция». Отсутствующий "
            "необязательный контекст не подменяется догадкой."
        )
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        page.content_layout.addWidget(summary)
        page.content_layout.addStretch(1)
        return page

    def scan_devices(self) -> None:
        ok, result = call_runtime(self.runtime, "rescan")
        snapshot = result if ok else current_snapshot(self.runtime)
        devices = items(snapshot, "devices")
        self.device_list.clear()
        usable = 0
        for device in devices:
            name = str(
                attr(device, "display_name", attr(device, "device_id", "Устройство"))
            )
            state = value_of(attr(device, "state", "unknown")).lower()
            if state in {"ready", "streaming"}:
                usable += 1
            self.device_list.addItem(QListWidgetItem(f"{name} · {state.upper()}"))
        if not ok:
            self.device_status.set_status(str(result), "warning")
        elif usable:
            self.device_status.set_status(
                f"ДОСТУПНО: {usable} ИЗ {len(devices)}",
                "ready",
            )
        elif devices:
            self.device_status.set_status(
                f"ПРОВЕРЕНО: {len(devices)} · ДОСТУПНЫХ НЕТ",
                "warning",
            )
        else:
            self.device_status.set_status("ПРИЁМНИКИ НЕ НАСТРОЕНЫ", "neutral")

    def back(self) -> None:
        self.stack.setCurrentIndex(max(0, self.stack.currentIndex() - 1))
        self._sync_navigation()

    def next(self) -> None:
        if self.stack.currentIndex() == self.stack.count() - 1:
            if self._finish_setup():
                self.accept()
            return
        self.completion_error.clear()
        self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
        self._sync_navigation()

    def _finish_setup(self) -> bool:
        """Persist receive-only setup and write completion as the final mutation."""

        self.completion_error.clear()
        storage_path = self.storage_path.text().strip()
        hardware_kind = str(self.hardware_kind.currentData() or "")
        hardware_id = self.hardware_id.text().strip()
        hardware_connection = self.hardware_connection.text().strip()

        if not storage_path:
            self.completion_error.setText("Укажите каталог локальных данных.")
            return False
        if any((hardware_kind, hardware_id, hardware_connection)) and not all(
            (hardware_kind, hardware_id, hardware_connection)
        ):
            self.completion_error.setText(
                "Для приёмника заполните тип, ID и точное подключение."
            )
            return False
        if hardware_kind == "tinysa" and re.fullmatch(
            r"(?i)COM(?:[1-9]|[1-9]\d|[12]\d\d)",
            hardware_connection,
        ) is None:
            self.completion_error.setText(
                "Подключение tinySA должно быть точным COM-портом, например COM7."
            )
            return False
        if hardware_kind == "rtlsdr" and re.fullmatch(
            r"(?i)RTLSDR:\d{1,3}",
            hardware_connection,
        ) is None:
            self.completion_error.setText(
                "Подключение RTL-SDR задаётся как RTLSDR:<индекс>, например RTLSDR:0."
            )
            return False
        if hardware_kind == "hackrf" and re.fullmatch(
            r"(?i)HACKRF:[0-9a-f]+",
            hardware_connection,
        ) is None:
            self.completion_error.setText(
                "HackRF задаётся как HACKRF:<hex-серийный номер> из обнаружения."
            )
            return False

        updater = getattr(self.runtime, "update_settings", None)
        complete = getattr(self.runtime, "complete_onboarding", None)
        if not callable(complete):
            self.completion_error.setText(
                "Runtime не поддерживает надёжное завершение первого запуска."
            )
            return False

        settings_payload: dict[str, object] = {
            "storage": {"data_dir": storage_path},
            "ui": {"experience_level": self.experience.currentData()},
        }
        if hardware_kind:
            settings_payload["devices"] = {
                "enable_real_adapters": True,
                "adapters": [
                    {
                        "id": hardware_id,
                        "kind": hardware_kind,
                        "enabled": True,
                        "connection": hardware_connection,
                    }
                ],
            }

        if callable(updater):
            ok, result = call_runtime(
                self.runtime,
                "update_settings",
                settings_payload,
            )
            if not ok:
                self.completion_error.setText(str(result))
                return False
            self.storage_status.set_status(
                "КАТАЛОГ ПРОВЕРЕН И ПОДКЛЮЧЁН",
                "ready",
            )

        ok, result = call_runtime(
            self.runtime,
            "complete_onboarding",
            None if callable(updater) else storage_path,
        )
        if not ok:
            self.completion_error.setText(
                f"Настройка применена, но завершение не зафиксировано: {result}"
            )
            return False
        return True

    def _sync_navigation(self) -> None:
        index = self.stack.currentIndex()
        self.step_label.setText(f"Шаг {index + 1} из {self.stack.count()}")
        self.back_button.setEnabled(index > 0)
        self.next_button.setText(
            "Открыть ALGA VECTOR"
            if index == self.stack.count() - 1
            else "Продолжить"
        )
