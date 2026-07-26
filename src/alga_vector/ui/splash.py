"""Flat startup splash without image or network dependencies."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSplashScreen

from .theme import Colors, preferred_font_family


def _splash_pixmap() -> QPixmap:
    pixmap = QPixmap(640, 340)
    pixmap.fill(QColor(Colors.BG))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(Colors.BORDER), 1))
    for x in range(0, 640, 40):
        painter.drawLine(x, 0, x, 340)
    for y in range(0, 340, 40):
        painter.drawLine(0, y, 640, y)
    painter.setPen(QPen(QColor(Colors.READY), 3))
    painter.drawEllipse(50, 52, 54, 54)
    painter.drawLine(77, 79, 119, 79)
    title_font = QFont(preferred_font_family())
    title_font.setPixelSize(30)
    title_font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(title_font)
    painter.setPen(QColor(Colors.TEXT))
    painter.drawText(142, 88, "ALGA VECTOR")
    subtitle_font = QFont(preferred_font_family())
    subtitle_font.setPixelSize(13)
    painter.setFont(subtitle_font)
    painter.setPen(QColor(Colors.TEXT_SECONDARY))
    painter.drawText(54, 142, "ЕДИНАЯ ОПЕРАТОРСКАЯ RF-ПЛАТФОРМА")
    painter.setPen(QColor(Colors.MUTED))
    painter.drawText(54, 266, "OFFLINE-FIRST · PASSIVE OBSERVATION")
    painter.drawText(54, 292, "Разработал: Буйвол и Задира")
    painter.end()
    return pixmap


class StartupSplash(QSplashScreen):
    def __init__(self) -> None:
        super().__init__(_splash_pixmap())
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.set_stage("Инициализация интерфейса…")

    def set_stage(self, message: str) -> None:
        self.showMessage(
            message,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            QColor(Colors.READY),
        )
