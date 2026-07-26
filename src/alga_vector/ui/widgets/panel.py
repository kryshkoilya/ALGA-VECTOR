"""Flat panel and metric primitives."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import Colors


class Panel(QFrame):
    """A titled flat surface with a public content layout."""

    def __init__(
        self,
        title: str = "",
        *,
        subtitle: str = "",
        parent: QWidget | None = None,
        compact: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setProperty("panel", "true")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer = QVBoxLayout(self)
        margin = 10 if compact else 14
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.setSpacing(9)
        if title:
            header = QHBoxLayout()
            header.setSpacing(8)
            self.title_label = QLabel(title)
            self.title_label.setProperty("sectionHeading", "true")
            header.addWidget(self.title_label)
            header.addStretch(1)
            if subtitle:
                self.subtitle_label = QLabel(subtitle)
                self.subtitle_label.setProperty("muted", "true")
                header.addWidget(self.subtitle_label)
            outer.addLayout(header)
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(8)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.content_layout, 1)


class MetricTile(QFrame):
    """Compact label/value pair used in dense status rows."""

    def __init__(
        self,
        label: str,
        value: str = "—",
        *,
        accent: str = Colors.TEXT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("panel", "subtle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)
        caption = QLabel(label.upper())
        caption.setProperty("muted", "true")
        caption.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet(f"color: {accent}; font-size: 15px; font-weight: 600;")
        self.value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(caption)
        layout.addWidget(self.value_label)

    def set_value(self, value: object, accent: str | None = None) -> None:
        self.value_label.setText(str(value))
        if accent is not None:
            self.value_label.setStyleSheet(
                f"color: {accent}; font-size: 15px; font-weight: 600;"
            )
