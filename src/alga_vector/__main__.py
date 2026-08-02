"""Windows executable entry point for ALGA VECTOR."""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import os
import sys
import traceback
from contextlib import suppress
from pathlib import Path

from alga_vector.bootstrap import RuntimeMode, build_context
from alga_vector.domain.errors import AppError
from alga_vector.single_instance import SingleInstanceGuard, isolated_smoke_mutex_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alga-vector",
        description="ALGA VECTOR — пассивный операционный интерфейс RF-наблюдения.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--demo",
        action="store_true",
        help="детерминированные синтетические источники для обучения и проверки",
    )
    modes.add_argument("--safe", action="store_true", help="безопасный режим без real adapters")
    modes.add_argument("--live", action="store_true", help="профиль реальных адаптеров")
    parser.add_argument("--onboarding", action="store_true", help="показать мастер первого запуска")
    parser.add_argument(
        "--skip-onboarding",
        action="store_true",
        help="не показывать мастер первого запуска",
    )
    parser.add_argument(
        "--headless-smoke",
        action="store_true",
        help="создать все экраны off-screen и завершиться",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="явный каталог локальных журналов и данных",
    )
    parser.add_argument(
        "--hardware-preflight",
        action="store_true",
        help=(
            "проверить обязательный hardware runtime и опциональные "
            "receive-only приёмники"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="включить подробный pipeline-лог и вывести traceback при сбое",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _configure_console_streams()
    args = build_parser().parse_args(argv)
    if args.headless_smoke:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if args.hardware_preflight:
        return _hardware_preflight()

    mutex_name = ""
    if args.headless_smoke and args.data_dir is not None:
        mutex_name = isolated_smoke_mutex_name(args.data_dir)
    with SingleInstanceGuard(mutex_name) as instance:
        if not instance.acquired:
            error = RuntimeError("ALGA VECTOR уже запущен для этого пользователя.")
            _report_fatal(error, headless=args.headless_smoke)
            return 3
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    mode = _mode_from_args(args)
    context = None
    try:
        context = build_context(
            mode_override=mode,
            data_dir_override=args.data_dir,
            # Smoke/build verification must be deterministic and must never
            # contact the online tile provider.
            network_maps_override=False if args.headless_smoke else None,
            debug_logging_override=args.debug,
        )
        if context.warning is not None and sys.stderr is not None:
            print(str(context.warning), file=sys.stderr)
        context.runtime.start()
        from alga_vector.ui.app import run_app

        show_onboarding = (
            args.onboarding
            or (not args.skip_onboarding and not context.config.first_run_complete)
        )
        if args.headless_smoke:
            show_onboarding = False
        return run_app(
            context.runtime,
            show_onboarding=show_onboarding,
            headless_smoke=args.headless_smoke,
        )
    except (AppError, OSError, RuntimeError, ValueError) as exc:
        if args.debug and sys.stderr is not None:
            traceback.print_exc()
        _report_fatal(exc, headless=args.headless_smoke)
        return 2
    finally:
        if context is not None:
            context.runtime.shutdown()


def _hardware_preflight() -> int:
    """Verify mandatory runtime and report optional receivers without probing COM."""

    try:
        for module_name in ("serial", "rtlsdr", "usb"):
            importlib.import_module(module_name)
        from alga_vector.devices import RtlSdrDiscoveryService

        discovery = RtlSdrDiscoveryService().discover()
    except (ImportError, OSError) as exc:
        _report_fatal(exc, headless=True)
        return 2
    if not discovery.successful:
        issue = discovery.issues[0] if discovery.issues else None
        message = (
            issue.message_ru
            if issue is not None
            else "Безопасный поиск RTL-SDR не вернул результат."
        )
        _report_fatal(RuntimeError(message), headless=True)
        return 2
    if sys.stdout is not None:
        devices = ", ".join(
            f"{device.description} ({device.connection})"
            for device in discovery.devices
        )
        summary = devices or "не подключены"
        print(
            "ALGA VECTOR hardware runtime: OK; "
            "required modules: pyserial=OK, pyrtlsdr=OK, pyusb=OK; "
            f"RTL-SDR descriptors: {summary}"
        )
        for line in _optional_receiver_preflight_lines():
            print(line)
    return 0


def _optional_receiver_preflight_lines() -> tuple[str, ...]:
    """Describe optional hardware; failures here never fail the core preflight."""

    try:
        from alga_vector.devices import (
            HackRfDiscoveryService,
            TinySaSerialDiscoveryService,
        )
        from alga_vector.devices.host_tools import SubprocessHostTools
    except Exception as exc:
        return (
            "HackRF (optional, RX-only): support module unavailable; "
            f"{type(exc).__name__}.",
            (
                "tinySA (optional): metadata-only discovery unavailable; "
                "COM ports are not opened automatically."
            ),
        )

    try:
        host_tools = SubprocessHostTools()
        hackrf_info = host_tools.find("hackrf_info")
        hackrf_transfer = host_tools.find("hackrf_transfer")
    except Exception as exc:
        return (
            (
                "HackRF (optional, RX-only / только приём): "
                f"host-tool check unavailable ({type(exc).__name__}); "
                "ALGA VECTOR never exposes a TX command."
            ),
            (
                "tinySA (optional): metadata-only discovery unavailable; "
                "COM ports are not opened automatically / "
                "COM-порты автоматически не открываются."
            ),
        )
    info_status = _host_tool_status(hackrf_info)
    transfer_status = _host_tool_status(hackrf_transfer)
    hackrf_summary = "device discovery skipped: hackrf_info is absent"
    if hackrf_info is not None:
        try:
            result = HackRfDiscoveryService(
                host_tools=host_tools,
                timeout_seconds=2.0,
                attempts=1,
            ).discover()
        except Exception as exc:
            hackrf_summary = f"discovery unavailable ({type(exc).__name__})"
        else:
            if result.devices:
                board_names = ", ".join(
                    sorted({device.board_name for device in result.devices})
                )
                hackrf_summary = (
                    f"confirmed receivers: {len(result.devices)}"
                    + (f" ({board_names})" if board_names else "")
                )
            elif result.successful:
                hackrf_summary = "receivers not connected"
            else:
                issue = result.issues[0] if result.issues else None
                hackrf_summary = (
                    issue.message_ru
                    if issue is not None
                    else "descriptor discovery unavailable"
                )

    try:
        tinysa_result = TinySaSerialDiscoveryService().discover()
        candidates = getattr(tinysa_result, "candidates", ())
        if candidates:
            labels = ", ".join(
                f"{candidate.description} ({candidate.connection})"
                for candidate in candidates
            )
            tinysa_summary = f"metadata candidates: {labels}"
        elif tinysa_result.successful:
            tinysa_summary = "metadata candidates not found"
        else:
            issue = tinysa_result.issues[0] if tinysa_result.issues else None
            tinysa_summary = (
                issue.message_ru
                if issue is not None
                else "metadata discovery unavailable"
            )
    except Exception as exc:
        tinysa_summary = f"metadata discovery unavailable ({type(exc).__name__})"

    return (
        (
            "HackRF (optional, RX-only / только приём): "
            f"hackrf_info={info_status}; "
            f"hackrf_transfer={transfer_status}; {hackrf_summary}. "
            "ALGA VECTOR never exposes a TX command."
        ),
        (
            "tinySA (optional): metadata-only discovery; "
            "COM ports are not opened automatically / "
            f"COM-порты автоматически не открываются; {tinysa_summary}."
        ),
    )


def _host_tool_status(path: str | None) -> str:
    if path is None:
        return "absent"
    origin = (
        "hardware-tools"
        if Path(path).parent.name.casefold() == "hardware-tools"
        else "PATH"
    )
    return f"available ({origin})"


def _mode_from_args(args: argparse.Namespace) -> RuntimeMode:
    if args.safe:
        return "safe"
    if args.live:
        return "live"
    if args.demo:
        return "demo"
    # Simulation is opt-in.  A stale user profile can never turn an ordinary
    # desktop/CLI launch back into demo mode.
    return "live"


def _report_fatal(exc: Exception, *, headless: bool) -> None:
    message = str(exc)
    if sys.stderr is not None:
        print(f"ALGA VECTOR: {message}", file=sys.stderr)
    if headless:
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox.critical(
            None,
            "ALGA VECTOR — ошибка запуска",
            f"Приложение не может продолжить работу.\n\n{message}",
        )
        app.processEvents()
    except Exception:
        # The stderr message remains available to launchers and support tooling.
        return


def _configure_console_streams() -> None:
    """Use deterministic Unicode output in the optional Windows CLI build."""

    if os.name == "nt":
        with suppress(AttributeError, OSError):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
