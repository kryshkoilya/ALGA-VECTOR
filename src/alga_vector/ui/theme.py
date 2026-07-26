"""Shared visual tokens and application-wide Qt style."""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from alga_vector.resources import asset_path


class Colors:
    """Approved flat charcoal palette."""

    BG = "#050707"
    BG_ALT = "#070A0A"
    NAV = "#0A0F0E"
    SURFACE = "#111817"
    SURFACE_ALT = "#16201E"
    BORDER = "#1D2926"
    BORDER_STRONG = "#344540"
    TEXT = "#EDF4F1"
    TEXT_SECONDARY = "#A6B2AD"
    MUTED = "#707D78"
    READY = "#25C78D"
    READY_DARK = "#163C31"
    TEAL = "#35B7AA"
    TEAL_DARK = "#153633"
    WARNING = "#E1A84B"
    WARNING_DARK = "#3B3020"
    CRITICAL = "#E35B65"
    CRITICAL_DARK = "#3B2025"
    INFO = "#83AFA6"


NAV_WIDTH = 112
HEADER_HEIGHT = 64
FOOTER_HEIGHT = 28
BASE_FONT_PX = 12
_FONT_FILES = ("GolosText-Regular.ttf", "GolosText-SemiBold.ttf")
_LOADED_FONT_IDS: list[int] = []


APP_STYLE = f"""
* {{
    font-size: {BASE_FONT_PX}px;
    color: {Colors.TEXT};
}}
QMainWindow, QWidget {{
    background-color: {Colors.BG};
}}
QWidget#contentRoot {{
    background-color: {Colors.BG};
}}
QFrame[panel="true"] {{
    background-color: {Colors.SURFACE};
    border: 1px solid {Colors.BORDER};
    border-radius: 7px;
}}
QFrame[panel="subtle"] {{
    background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER};
    border-radius: 5px;
}}
QFrame#topHeader {{
    background-color: {Colors.NAV};
    border-bottom: 1px solid {Colors.BORDER};
}}
QFrame#footer {{
    background-color: {Colors.BG};
    border-top: 1px solid {Colors.BORDER};
}}
QFrame#navigation {{
    background-color: {Colors.NAV};
    border-right: 1px solid {Colors.BORDER};
}}
QLabel {{
    background-color: transparent;
}}
QLabel[muted="true"] {{
    color: {Colors.MUTED};
}}
QLabel[secondary="true"] {{
    color: {Colors.TEXT_SECONDARY};
}}
QLabel[heading="true"] {{
    color: {Colors.TEXT};
    font-size: 20px;
    font-weight: 600;
}}
QLabel[sectionHeading="true"] {{
    color: {Colors.TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QLabel[statusLevel="ready"] {{
    color: {Colors.READY};
    background-color: {Colors.READY_DARK};
    border: 1px solid {Colors.READY};
    border-radius: 4px;
    padding: 3px 7px;
}}
QLabel[statusLevel="info"] {{
    color: {Colors.TEAL};
    background-color: {Colors.TEAL_DARK};
    border: 1px solid {Colors.TEAL};
    border-radius: 4px;
    padding: 3px 7px;
}}
QLabel[statusLevel="warning"] {{
    color: {Colors.WARNING};
    background-color: {Colors.WARNING_DARK};
    border: 1px solid {Colors.WARNING};
    border-radius: 4px;
    padding: 3px 7px;
}}
QLabel[statusLevel="critical"] {{
    color: {Colors.CRITICAL};
    background-color: {Colors.CRITICAL_DARK};
    border: 1px solid {Colors.CRITICAL};
    border-radius: 4px;
    padding: 3px 7px;
}}
QLabel[statusLevel="neutral"] {{
    color: {Colors.TEXT_SECONDARY};
    background-color: {Colors.SURFACE_ALT};
    border: 1px solid {Colors.BORDER_STRONG};
    border-radius: 4px;
    padding: 3px 7px;
}}
QPushButton {{
    min-height: 32px;
    background-color: {Colors.SURFACE_ALT};
    border: 1px solid {Colors.BORDER_STRONG};
    border-radius: 5px;
    padding: 0 11px;
}}
QPushButton:hover {{
    border-color: {Colors.INFO};
    background-color: {Colors.BORDER};
}}
QPushButton:pressed {{
    background-color: {Colors.NAV};
}}
QPushButton:disabled {{
    color: {Colors.MUTED};
    border-color: {Colors.BORDER};
    background-color: {Colors.NAV};
}}
QPushButton[primary="true"] {{
    color: {Colors.BG};
    font-weight: 600;
    background-color: {Colors.READY};
    border-color: {Colors.READY};
}}
QPushButton[danger="true"] {{
    color: {Colors.CRITICAL};
    background-color: {Colors.CRITICAL_DARK};
    border-color: {Colors.CRITICAL};
}}
QPushButton[primary="true"]:disabled,
QPushButton[danger="true"]:disabled {{
    color: {Colors.MUTED};
    font-weight: 400;
    background-color: {Colors.NAV};
    border-color: {Colors.BORDER};
}}
QPushButton[modeSwitch="true"] {{
    min-height: 28px;
    max-height: 28px;
    color: {Colors.TEXT_SECONDARY};
    background-color: {Colors.NAV};
    border-color: {Colors.BORDER_STRONG};
    padding: 0 8px;
}}
QPushButton[modeSwitch="true"]:checked {{
    color: {Colors.READY};
    background-color: {Colors.READY_DARK};
    border-color: {Colors.READY};
    font-weight: 600;
}}
QPushButton[nav="true"] {{
    min-height: 49px;
    color: {Colors.TEXT_SECONDARY};
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px;
    text-align: center;
}}
QPushButton[nav="true"]:hover {{
    color: {Colors.TEXT};
    background-color: {Colors.SURFACE};
    border-color: {Colors.BORDER};
}}
QPushButton[nav="true"]:checked {{
    color: {Colors.READY};
    background-color: {Colors.SURFACE};
    border-color: {Colors.READY};
    font-weight: 600;
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    min-height: 31px;
    color: {Colors.TEXT};
    selection-color: {Colors.BG};
    selection-background-color: {Colors.TEAL};
    background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER_STRONG};
    border-radius: 4px;
    padding: 0 7px;
}}
QPlainTextEdit, QTextEdit {{
    padding: 6px;
}}
QComboBox::drop-down {{
    border: 0;
    width: 22px;
}}
QCheckBox, QRadioButton {{
    spacing: 7px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
}}
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
    background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER_STRONG};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {Colors.READY};
    border: 1px solid {Colors.READY};
}}
QTableWidget, QTableView, QListWidget, QTreeWidget {{
    color: {Colors.TEXT};
    background-color: {Colors.SURFACE};
    alternate-background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER};
    gridline-color: {Colors.BORDER};
    selection-color: {Colors.TEXT};
    selection-background-color: {Colors.TEAL_DARK};
}}
QHeaderView::section {{
    min-height: 30px;
    color: {Colors.TEXT_SECONDARY};
    background-color: {Colors.NAV};
    border: 0;
    border-right: 1px solid {Colors.BORDER};
    border-bottom: 1px solid {Colors.BORDER};
    padding: 0 7px;
    font-weight: 600;
}}
QTabWidget::pane {{
    border: 1px solid {Colors.BORDER};
    background-color: {Colors.SURFACE};
}}
QTabBar::tab {{
    min-height: 30px;
    min-width: 90px;
    color: {Colors.TEXT_SECONDARY};
    background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER};
    padding: 0 10px;
}}
QTabBar::tab:selected {{
    color: {Colors.READY};
    border-bottom-color: {Colors.READY};
}}
QProgressBar {{
    min-height: 9px;
    max-height: 9px;
    color: transparent;
    background-color: {Colors.NAV};
    border: 1px solid {Colors.BORDER};
    border-radius: 3px;
}}
QProgressBar::chunk {{
    background-color: {Colors.READY};
    border-radius: 2px;
}}
QScrollBar:vertical {{
    width: 10px;
    background-color: {Colors.BG};
}}
QScrollBar::handle:vertical {{
    min-height: 24px;
    background-color: {Colors.BORDER_STRONG};
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QToolTip {{
    color: {Colors.TEXT};
    background-color: {Colors.SURFACE_ALT};
    border: 1px solid {Colors.BORDER_STRONG};
    padding: 4px;
}}
"""


def preferred_font_family() -> str:
    """Return Golos Text when installed, otherwise the Windows-safe fallback."""

    families = set(QFontDatabase.families())
    if "Golos Text" in families:
        return "Golos Text"
    if "Segoe UI" in families:
        return "Segoe UI"
    return QFont().defaultFamily()


def load_bundled_fonts() -> None:
    """Register Golos Text so Cyrillic never depends on host font discovery."""

    if _LOADED_FONT_IDS:
        return
    for filename in _FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(
            str(asset_path("fonts", filename))
        )
        if font_id >= 0:
            _LOADED_FONT_IDS.append(font_id)


def apply_theme(app: QApplication) -> None:
    """Apply the deterministic production palette and a 12 px base font."""

    load_bundled_fonts()
    font = QFont(preferred_font_family())
    font.setPixelSize(BASE_FONT_PX)
    app.setFont(font)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, Colors.BG)
    palette.setColor(QPalette.ColorRole.WindowText, Colors.TEXT)
    palette.setColor(QPalette.ColorRole.Base, Colors.NAV)
    palette.setColor(QPalette.ColorRole.AlternateBase, Colors.SURFACE)
    palette.setColor(QPalette.ColorRole.Text, Colors.TEXT)
    palette.setColor(QPalette.ColorRole.Button, Colors.SURFACE_ALT)
    palette.setColor(QPalette.ColorRole.ButtonText, Colors.TEXT)
    palette.setColor(QPalette.ColorRole.Highlight, Colors.TEAL)
    palette.setColor(QPalette.ColorRole.HighlightedText, Colors.BG)
    palette.setColor(QPalette.ColorRole.PlaceholderText, Colors.MUTED)
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)
