"""Target-centric presentation primitives for the simple operator view."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
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
class TargetCardState:
    """UI-safe target summary with no numeric confidence or inferred position."""

    probable_type: str
    confirmation_stage: str
    summary: str
    target_id: str = ""
    last_seen: str = ""
    sensors: str = ""
    stage_level: str = "warning"


class ConfirmationStageBadge(QLabel):
    """A verbal confirmation stage; percentages deliberately do not enter this widget."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("ФОН", parent)
        self.setObjectName("targetConfirmationStage")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(28)
        self.set_stage("Фон", "neutral")

    def set_stage(self, text: str, level: str) -> None:
        foreground, background = {
            "ready": (Colors.READY, Colors.READY_DARK),
            "info": (Colors.TEAL, Colors.TEAL_DARK),
            "warning": (Colors.WARNING, Colors.WARNING_DARK),
            "critical": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "neutral": (Colors.TEXT_SECONDARY, Colors.SURFACE_ALT),
        }.get(level, (Colors.TEXT_SECONDARY, Colors.SURFACE_ALT))
        self.setText(text.upper())
        self.setStyleSheet(
            f"color: {foreground}; background-color: {background}; "
            f"border: 1px solid {foreground}; border-radius: 5px; "
            "padding: 3px 8px; font-size: 11px; font-weight: 600;"
        )


class TargetSummaryCard(QFrame):
    """Dominant target card used by SIMPLE MODE."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("currentTargetCard")
        self.setProperty("panel", "true")
        self.setMinimumHeight(146)
        self.setMaximumHeight(166)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 11)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        caption = QLabel("ТЕКУЩАЯ ЦЕЛЬ")
        caption.setObjectName("currentTargetCaption")
        caption.setProperty("sectionHeading", "true")
        header.addWidget(caption)
        header.addStretch(1)
        self.stage_badge = ConfirmationStageBadge()
        header.addWidget(self.stage_badge)
        layout.addLayout(header)

        self.type_label = QLabel("Активная цель не сформирована")
        self.type_label.setObjectName("targetProbableType")
        self.type_label.setStyleSheet(
            f"color: {Colors.TEXT}; font-size: 23px; font-weight: 600;"
        )
        self.type_label.setWordWrap(True)
        layout.addWidget(self.type_label)

        self.summary_label = QLabel(
            "Система продолжает наблюдение и объединяет подтверждённые события."
        )
        self.summary_label.setObjectName("targetOperatorSummary")
        self.summary_label.setProperty("secondary", "true")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        layout.addStretch(1)
        metadata = QHBoxLayout()
        metadata.setSpacing(14)
        self.id_label = QLabel()
        self.id_label.setObjectName("targetId")
        self.id_label.setProperty("muted", "true")
        self.last_seen_label = QLabel()
        self.last_seen_label.setObjectName("targetLastSeen")
        self.last_seen_label.setProperty("muted", "true")
        metadata.addWidget(self.id_label)
        metadata.addWidget(self.last_seen_label)
        metadata.addStretch(1)
        layout.addLayout(metadata)

        self.sensors_label = QLabel()
        self.sensors_label.setObjectName("targetSensorAttribution")
        self.sensors_label.setProperty("muted", "true")
        self.sensors_label.setWordWrap(True)
        layout.addWidget(self.sensors_label)

    @property
    def confirmation_stage_label(self) -> QLabel:
        return self.stage_badge

    def set_target(self, state: TargetCardState) -> None:
        self.type_label.setText(state.probable_type)
        self.summary_label.setText(state.summary)
        self.stage_badge.set_stage(
            state.confirmation_stage,
            state.stage_level,
        )
        self.id_label.setText(f"ID · {state.target_id}" if state.target_id else "")
        self.id_label.setVisible(bool(state.target_id))
        self.last_seen_label.setText(
            f"Последнее наблюдение · {state.last_seen}"
            if state.last_seen
            else ""
        )
        self.last_seen_label.setVisible(bool(state.last_seen))
        self.sensors_label.setText(
            f"Участвуют: {state.sensors}"
            if state.sensors
            else "Участвующие сенсоры не указаны"
        )


__all__ = [
    "ConfirmationStageBadge",
    "TargetCardState",
    "TargetSummaryCard",
]
