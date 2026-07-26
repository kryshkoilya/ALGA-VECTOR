"""Shared page helpers."""

from __future__ import annotations

# ruff: noqa: RUF001
from typing import Any, cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..runtime import attr, value_of
from ..theme import Colors
from ..widgets import ProvenanceBanner, StatusBadge

DEVICE_STATE_RU = {
    "absent": "НЕ ОБНАРУЖЕНО",
    "discovered": "ОБНАРУЖЕНО",
    "probing": "ПРОВЕРКА",
    "ready": "ГОТОВО",
    "starting": "ЗАПУСК",
    "streaming": "ПОТОК АКТИВЕН",
    "stopping": "ОСТАНОВКА",
    "degraded": "ОГРАНИЧЕНО",
    "reconnecting": "ПЕРЕПОДКЛЮЧЕНИЕ",
    "failed": "ОШИБКА",
    "quarantined": "КАРАНТИН",
    "disabled": "ОТКЛЮЧЕНО",
}


def device_state_ru(device: object | None) -> str:
    state = value_of(attr(device, "state", "unknown")).lower()
    return DEVICE_STATE_RU.get(state, state.upper() if state else "НЕИЗВЕСТНО")


def device_level(device: object | None) -> str:
    health = value_of(attr(device, "health", "unknown")).lower()
    state = value_of(attr(device, "state", "unknown")).lower()
    if health in {"error", "critical"} or state in {"failed", "quarantined"}:
        return "critical"
    if health == "degraded" or state in {"degraded", "reconnecting", "probing"}:
        return "warning"
    if health == "healthy" or state in {"ready", "streaming"}:
        return "ready"
    return "neutral"


def format_frequency(value: object, *, precision: int = 3) -> str:
    try:
        hz = float(cast(Any, value))
    except (TypeError, ValueError):
        return "—"
    if abs(hz) >= 1_000_000:
        return f"{hz / 1_000_000:.{precision}f} МГц"
    if abs(hz) >= 1_000:
        return f"{hz / 1_000:.1f} кГц"
    return f"{hz:.0f} Гц"


def set_label_value(label: QLabel, caption: str, value: object) -> None:
    label.setText(f"<span style='color:{Colors.MUTED}'>{caption}:</span> {value}")


class PageHeader(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str,
        *,
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setProperty("heading", "true")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setProperty("secondary", "true")
        text_layout.addWidget(self.title)
        text_layout.addWidget(self.subtitle)
        layout.addLayout(text_layout, 1)
        self.status = StatusBadge("ОЖИДАНИЕ", "neutral")
        layout.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        self.action = QPushButton(action_text)
        self.action.setVisible(bool(action_text))
        layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter)


class OperatorPage(QWidget):
    """Base page with a consistent header and provenance row."""

    def __init__(
        self,
        runtime: object | None,
        title: str,
        subtitle: str,
        *,
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(18, 16, 18, 16)
        self.root_layout.setSpacing(12)
        self.header = PageHeader(title, subtitle, action_text=action_text)
        self.provenance = ProvenanceBanner()
        self.root_layout.addWidget(self.header)
        self.root_layout.addWidget(self.provenance)

    def refresh(self, snapshot: object | None = None) -> None:
        self.provenance.refresh(snapshot)
