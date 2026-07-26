"""PySide6 operator interface for ALGA VECTOR.

The UI deliberately talks to a small duck-typed runtime surface.  Hardware
adapters and native drivers stay outside the Qt process boundary.
"""

from __future__ import annotations

from .app import build_main_window, create_application, run
from .main_window import MainWindow

__all__ = ["MainWindow", "build_main_window", "create_application", "run"]
