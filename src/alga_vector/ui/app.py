"""Qt application composition helpers."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import cast

from PySide6.QtWidgets import QApplication, QDialog

from .main_window import MainWindow
from .onboarding import OnboardingDialog
from .splash import StartupSplash
from .theme import apply_theme


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the process QApplication and apply the approved theme."""

    instance = QApplication.instance()
    if instance is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    else:
        app = cast(QApplication, instance)
    app.setApplicationName("ALGA VECTOR")
    app.setOrganizationName("Буйвол и Задира")
    apply_theme(app)
    return app


def build_main_window(runtime: object | None = None) -> MainWindow:
    """Construct the shell without taking ownership of the runtime."""

    return MainWindow(runtime)


def run_app(
    runtime: object | None,
    *,
    show_onboarding: bool = False,
    headless_smoke: bool = False,
) -> int:
    """Run the operator UI.

    The caller owns runtime startup and shutdown.  Closing the window only stops
    Qt timers, preventing duplicate worker shutdown during composition teardown.
    """

    app = create_application()
    window = build_main_window(runtime)
    if headless_smoke:
        window.show()
        app.processEvents()
        window.refresh_snapshot()
        app.processEvents()
        window.close()
        app.processEvents()
        return 0

    splash = StartupSplash()
    splash.show()
    app.processEvents()
    splash.set_stage("Загрузка операционных экранов…")
    app.processEvents()
    if show_onboarding:
        splash.close()
        dialog = OnboardingDialog(runtime)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return 0
    window.showMaximized()
    splash.finish(window)
    return app.exec()


run = run_app


__all__ = ["build_main_window", "create_application", "run", "run_app"]
