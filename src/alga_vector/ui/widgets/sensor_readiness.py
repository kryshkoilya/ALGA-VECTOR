"""Horizontal readiness strip for the seven operator-facing sensor roles."""

from __future__ import annotations

# ruff: noqa: RUF001
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Colors


@dataclass(frozen=True, slots=True)
class SensorReadinessState:
    key: str
    name: str
    state: str
    reason: str
    impact: str


class SensorReadinessTile(QFrame):
    def __init__(
        self,
        key: str,
        name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.name = name
        self.setObjectName(f"sensorStatus_{key}")
        self.setProperty("panel", "subtle")
        self.setMinimumWidth(122)
        self.setFixedHeight(68)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        layout.setSpacing(3)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.dot = QFrame()
        self.dot.setFixedSize(7, 7)
        self.name_label = QLabel(name)
        self.name_label.setObjectName(f"sensorName_{key}")
        self.name_label.setStyleSheet("font-size: 11px; font-weight: 600;")
        header.addWidget(self.dot)
        header.addWidget(self.name_label, 1)
        layout.addLayout(header)

        self.state_label = QLabel("НЕДОСТУПЕН")
        self.state_label.setObjectName(f"sensorState_{key}")
        self.state_label.setStyleSheet("font-size: 10px; font-weight: 600;")
        layout.addWidget(self.state_label)

        self.reason_label = QLabel("Нет данных")
        self.reason_label.setObjectName(f"sensorReason_{key}")
        self.reason_label.setProperty("muted", "true")
        self.reason_label.setWordWrap(True)
        self.reason_label.setMaximumHeight(21)
        layout.addWidget(self.reason_label)

    def set_readiness(self, state: SensorReadinessState) -> None:
        normalized = (
            state.state
            if state.state in {"ready", "limited", "unavailable"}
            else "unavailable"
        )
        text, color = {
            "ready": ("ГОТОВ", Colors.READY),
            "limited": ("ОГРАНИЧЕН", Colors.WARNING),
            "unavailable": ("НЕДОСТУПЕН", Colors.MUTED),
        }[normalized]
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 600;"
        )
        self.dot.setStyleSheet(
            f"background-color: {color}; border: 0; border-radius: 3px;"
        )
        reason = state.reason or (
            "Работает штатно"
            if normalized == "ready"
            else "Состояние не передано"
        )
        self.reason_label.setText(reason)
        self.setToolTip(
            f"{state.name}: {text.casefold()}\n"
            f"Причина: {reason}\n"
            f"Влияние: {state.impact}"
        )
        self.setAccessibleDescription(
            f"{state.name}. {text}. {reason}. {state.impact}"
        )


class SensorReadinessStrip(QFrame):
    SENSOR_ORDER: tuple[tuple[str, str], ...] = (
        ("tinysa", "TinySA"),
        ("rtlsdr", "RTL-SDR"),
        ("krakensdr", "KrakenSDR"),
        ("acoustic", "Акустика"),
        ("adsb", "ADS-B"),
        ("passive_radar", "Пассивный радар"),
        ("fusion", "Fusion"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sensorReadinessStrip")
        self.setProperty("panel", "true")
        self.setMinimumHeight(92)
        self.setMaximumHeight(100)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)
        header = QHBoxLayout()
        caption = QLabel("ГОТОВНОСТЬ СЕНСОРОВ")
        caption.setProperty("sectionHeading", "true")
        self.summary_label = QLabel("0 из 7 готовы")
        self.summary_label.setObjectName("sensorReadinessSummary")
        self.summary_label.setProperty("muted", "true")
        header.addWidget(caption)
        header.addStretch(1)
        header.addWidget(self.summary_label)
        layout.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.tiles: dict[str, SensorReadinessTile] = {}
        for key, name in self.SENSOR_ORDER:
            tile = SensorReadinessTile(key, name)
            self.tiles[key] = tile
            row.addWidget(tile, 1)
        layout.addLayout(row)

    def set_states(self, states: tuple[SensorReadinessState, ...]) -> None:
        by_key = {state.key: state for state in states}
        ready_count = 0
        limited_count = 0
        for key, name in self.SENSOR_ORDER:
            state = by_key.get(
                key,
                SensorReadinessState(
                    key=key,
                    name=name,
                    state="unavailable",
                    reason="Нет данных о сенсоре",
                    impact="Вклад этого сенсора отсутствует",
                ),
            )
            self.tiles[key].set_readiness(state)
            ready_count += state.state == "ready"
            limited_count += state.state == "limited"
        suffix = f" · {limited_count} огранич." if limited_count else ""
        self.summary_label.setText(f"{ready_count} из 7 готовы{suffix}")


__all__ = [
    "SensorReadinessState",
    "SensorReadinessStrip",
    "SensorReadinessTile",
]
