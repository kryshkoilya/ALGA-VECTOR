"""Status and data-provenance indicators."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from ..runtime import provenance_key, provenance_ru, runtime_error_detail
from ..theme import Colors


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class StatusBadge(QLabel):
    def __init__(
        self,
        text: str = "НЕИЗВЕСТНО",
        level: str = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(text, level)

    def set_status(self, text: str, level: str = "neutral") -> None:
        normalized = level if level in {"ready", "info", "warning", "critical"} else "neutral"
        self.setText(text)
        self.setProperty("statusLevel", normalized)
        _repolish(self)


class ProvenanceBanner(QFrame):
    """Always-visible indication that prevents simulated data being mistaken for live data."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("panel", "subtle")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 5, 9, 5)
        layout.setSpacing(7)
        self.dot = QFrame()
        self.dot.setFixedSize(7, 7)
        self.label = QLabel("ДАННЫЕ НЕДОСТУПНЫ")
        self.label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self.refresh(None)

    def refresh(self, snapshot: object | None) -> None:
        key = provenance_key(snapshot)
        runtime_error = runtime_error_detail(snapshot)
        color = {
            "live": Colors.READY,
            "replayed": Colors.TEAL,
            "simulated": Colors.WARNING,
            "demo": Colors.WARNING,
            "safe": Colors.WARNING,
            "unavailable": Colors.CRITICAL,
            "unknown": Colors.MUTED,
        }.get(key, Colors.MUTED)
        self.dot.setStyleSheet(
            f"background-color: {color}; border: 0; border-radius: 3px;"
        )
        self.label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.label.setText(provenance_ru(snapshot))
        self.setToolTip(runtime_error)
        self.label.setToolTip(runtime_error)


class InlineNotice(QFrame):
    """Non-modal explanation with an optional next action."""

    def __init__(
        self,
        title: str,
        message: str,
        *,
        level: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._message = message
        self._level = level
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        self.marker = QFrame()
        self.marker.setFixedSize(7, 7)
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        layout.addWidget(self.marker, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.text_label, 1)
        self.set_notice(title, message, level=level)

    def set_notice(self, title: str, message: str, *, level: str = "info") -> None:
        colors = {
            "ready": (Colors.READY, Colors.READY_DARK),
            "warning": (Colors.WARNING, Colors.WARNING_DARK),
            "critical": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "info": (Colors.TEAL, Colors.TEAL_DARK),
        }
        foreground, background = colors.get(level, colors["info"])
        self._title = title
        self._message = message
        self._level = level
        self.setStyleSheet(
            f"QFrame {{ background-color: {background}; border: 1px solid {foreground}; "
            "border-radius: 5px; }"
        )
        self.marker.setStyleSheet(
            f"background-color: {foreground}; border: 0; border-radius: 3px;"
        )
        self.text_label.setText(f"<b>{title}</b><br>{message}")
        self.text_label.setStyleSheet("border: 0; background-color: transparent;")


class SignalAlertBanner(QFrame):
    """Compact, clickable observation alert that never claims emitter identity."""

    open_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("signalAlertBanner")
        self.setFixedHeight(46)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 12, 6)
        layout.setSpacing(10)
        self.marker = QFrame()
        self.marker.setFixedSize(8, 8)
        self.title_label = QLabel("Наблюдение RF")
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setMaximumWidth(300)
        self.message_label = QLabel()
        self.message_label.setProperty("secondary", "true")
        self.message_label.setMinimumWidth(0)
        self.message_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.open_button = QPushButton("Открыть события")
        self.open_button.setFixedHeight(30)
        self.open_button.clicked.connect(self.open_requested.emit)
        layout.addWidget(self.marker)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label, 1)
        layout.addWidget(self.open_button)
        self.set_alert("Наблюдение RF", "", level="info", details="")

    def set_alert(
        self,
        title: str,
        message: str,
        *,
        level: str,
        details: str,
    ) -> None:
        colors = {
            "warning": (Colors.WARNING, Colors.WARNING_DARK),
            "critical": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "info": (Colors.TEAL, Colors.TEAL_DARK),
            "ready": (Colors.READY, Colors.READY_DARK),
        }
        foreground, background = colors.get(level, colors["info"])
        self.setStyleSheet(
            f"QFrame#signalAlertBanner {{ background-color: {background}; "
            f"border-bottom: 1px solid {foreground}; }}"
        )
        self.marker.setStyleSheet(
            f"background-color: {foreground}; border: 0; border-radius: 4px;"
        )
        self.title_label.setStyleSheet(
            f"color: {foreground}; font-weight: 600; border: 0;"
        )
        self.title_label.setText(title)
        self.message_label.setText(message)
        self.message_label.setToolTip(details)
        self.setToolTip(details)
