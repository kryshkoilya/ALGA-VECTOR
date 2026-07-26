"""Bounded, read-only RTL-SDR discovery.

Discovery deliberately uses only librtlsdr's descriptor functions.  It never
constructs ``RtlSdr``, opens a receiver, changes a frequency, or mutates the
application configuration.  The native calls run in a disposable process so a
broken USB backend cannot hold the desktop process indefinitely.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import ctypes
import importlib
import multiprocessing
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import Lock
from types import ModuleType
from typing import Any, Protocol, cast

_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 4.0
_DEFAULT_MAX_DEVICES = 16
_MAX_ALLOWED_DEVICES = 64
_PROCESS_JOIN_TIMEOUT_SECONDS = 0.35
_USB_STRING_BUFFER_BYTES = 256
_DISPLAY_STRING_LIMIT = 160
_WINDOWS_ERROR_INSUFFICIENT_BUFFER = 122
_WINDOWS_ERROR_NO_MORE_ITEMS = 259
_WINDOWS_DIGCF_PRESENT = 0x00000002
_WINDOWS_DIGCF_ALLCLASSES = 0x00000004
_WINDOWS_SPDRP_DEVICEDESC = 0x00000000
_WINDOWS_SPDRP_HARDWAREID = 0x00000001
_WINDOWS_SPDRP_SERVICE = 0x00000004
_WINDOWS_SPDRP_DRIVER = 0x00000009
_WINDOWS_SPDRP_FRIENDLYNAME = 0x0000000C
_WINDOWS_REG_MULTI_SZ = 7
_WINDOWS_CR_SUCCESS = 0
_WINDOWS_RTL_USB_IDS = (
    "USB\\VID_0BDA&PID_2832",
    "USB\\VID_0BDA&PID_2838",
)
_WINDOWS_RTL_NAME_MARKERS = ("RTL2832", "RTL-SDR", "RTLSDR")
_WINDOWS_PROBLEM_TEXT_RU = {
    10: "устройство не запускается",
    22: "устройство отключено в Windows",
    28: "драйвер не установлен",
    31: "Windows не может загрузить требуемый драйвер",
    39: "драйвер повреждён или отсутствует",
    43: "Windows остановила устройство после ошибки",
    52: "Windows не может проверить цифровую подпись драйвера",
}


class RtlSdrDiscoveryState(StrEnum):
    """Terminal state of one detached discovery attempt."""

    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class RtlSdrDiscoveryIssue:
    """Sanitized, operator-facing problem from enumeration."""

    code: str
    message_ru: str
    operator_action_ru: str
    retryable: bool
    device_index: int | None = None


@dataclass(frozen=True, slots=True)
class RtlSdrDiscoveredDevice:
    """One read-only descriptor; ``connection`` is safe to feed to settings."""

    index: int
    connection: str
    description: str
    serial: str | None = None
    manufacturer: str | None = None


@dataclass(frozen=True, slots=True)
class RtlSdrEnumeration:
    """Raw, bounded output produced by an enumeration backend."""

    devices: tuple[RtlSdrDiscoveredDevice, ...]
    reported_count: int
    truncated: bool = False
    issues: tuple[RtlSdrDiscoveryIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class RtlSdrDiscoveryResult:
    """Immutable result suitable for presentation by any UI."""

    state: RtlSdrDiscoveryState
    devices: tuple[RtlSdrDiscoveredDevice, ...]
    reported_count: int
    scanned_count: int
    issues: tuple[RtlSdrDiscoveryIssue, ...] = ()

    @property
    def successful(self) -> bool:
        return self.state in {
            RtlSdrDiscoveryState.COMPLETE,
            RtlSdrDiscoveryState.EMPTY,
            RtlSdrDiscoveryState.PARTIAL,
        }


class RtlSdrEnumerator(Protocol):
    """Injectable descriptor-only enumeration boundary."""

    def enumerate_devices(self, *, max_devices: int) -> RtlSdrEnumeration: ...


class RtlSdrPnpDiagnostic(Protocol):
    """Optional read-only OS diagnostic used when librtlsdr sees no receiver."""

    def diagnose_attached_receiver(self) -> RtlSdrDiscoveryIssue | None: ...


@dataclass(frozen=True, slots=True)
class WindowsPnpDeviceStatus:
    """Sanitized read-only state of one present Windows PnP device.

    Instance paths and serial numbers are deliberately not retained.  The data
    is sufficient to explain a driver binding problem without exposing a stable
    hardware identifier.
    """

    hardware_ids: tuple[str, ...]
    description: str
    service: str | None
    driver_key: str | None
    problem_code: int | None


class WindowsRtlSdrPnpDiagnostic:
    """Inspect present Windows devices through SetupAPI/CfgMgr32 without mutation."""

    def __init__(
        self,
        device_reader: Callable[[], Iterable[WindowsPnpDeviceStatus]] | None = None,
    ) -> None:
        self._device_reader = device_reader or _read_present_windows_pnp_devices

    def diagnose_attached_receiver(self) -> RtlSdrDiscoveryIssue | None:
        if sys.platform != "win32" and self._device_reader is _read_present_windows_pnp_devices:
            return None
        try:
            devices = tuple(self._device_reader())
        except Exception:
            # A best-effort explanation must not replace the librtlsdr result.
            return None
        return _windows_pnp_issue(devices)


class PyRtlSdrEnumerator:
    """Read USB descriptors through pyrtlsdr's configured librtlsdr."""

    def __init__(self, rtlsdr_module: ModuleType | None = None) -> None:
        self._rtlsdr_module = rtlsdr_module

    def enumerate_devices(self, *, max_devices: int) -> RtlSdrEnumeration:
        module = self._rtlsdr_module or importlib.import_module("rtlsdr")
        library = getattr(module, "librtlsdr", None)
        if library is None:
            raise RuntimeError("librtlsdr binding is unavailable")

        get_count = getattr(library, "rtlsdr_get_device_count", None)
        get_name = getattr(library, "rtlsdr_get_device_name", None)
        get_usb_strings = getattr(library, "rtlsdr_get_device_usb_strings", None)
        if not callable(get_count) or not callable(get_name) or not callable(get_usb_strings):
            raise RuntimeError("required librtlsdr descriptor functions are unavailable")

        reported_count = int(get_count())
        if reported_count < 0:
            raise RuntimeError("librtlsdr returned an invalid device count")
        scan_count = min(reported_count, max_devices)
        issues: list[RtlSdrDiscoveryIssue] = []
        devices: list[RtlSdrDiscoveredDevice] = []

        for index in range(scan_count):
            name = ""
            manufacturer = ""
            product = ""
            serial = ""
            try:
                name = _decode_native_string(get_name(index))
            except Exception:
                issues.append(_descriptor_issue(index))

            try:
                manufacturer_buffer = (ctypes.c_ubyte * _USB_STRING_BUFFER_BYTES)()
                product_buffer = (ctypes.c_ubyte * _USB_STRING_BUFFER_BYTES)()
                serial_buffer = (ctypes.c_ubyte * _USB_STRING_BUFFER_BYTES)()
                status = int(
                    get_usb_strings(
                        index,
                        manufacturer_buffer,
                        product_buffer,
                        serial_buffer,
                    )
                )
                if status != 0:
                    raise OSError("librtlsdr descriptor query failed")
                manufacturer = _decode_byte_buffer(manufacturer_buffer)
                product = _decode_byte_buffer(product_buffer)
                serial = _decode_byte_buffer(serial_buffer)
            except Exception:
                issues.append(_usb_strings_issue(index))

            description = product or name or f"RTL-SDR #{index}"
            devices.append(
                RtlSdrDiscoveredDevice(
                    index=index,
                    connection=f"RTLSDR:{index}",
                    description=_clean_display_string(description)
                    or f"RTL-SDR #{index}",
                    serial=_optional_clean_string(serial),
                    manufacturer=_optional_clean_string(manufacturer),
                )
            )

        truncated = reported_count > scan_count
        if truncated:
            issues.append(
                RtlSdrDiscoveryIssue(
                    code="DEVICE.RTLSDR_DISCOVERY_LIMIT",
                    message_ru=(
                        "Обнаружено больше RTL-SDR, чем можно безопасно обработать "
                        "за один проход."
                    ),
                    operator_action_ru=(
                        "Отключите лишние приёмники и повторите поиск нужного устройства."
                    ),
                    retryable=True,
                )
            )
        return RtlSdrEnumeration(
            devices=tuple(devices),
            reported_count=reported_count,
            truncated=truncated,
            issues=tuple(issues),
        )


class RtlSdrDiscoveryService:
    """Serialize attempts and isolate the production native backend."""

    def __init__(
        self,
        *,
        enumerator: RtlSdrEnumerator | None = None,
        timeout_seconds: float = _DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        max_devices: int = _DEFAULT_MAX_DEVICES,
        mp_context: Any | None = None,
        pnp_diagnostic: RtlSdrPnpDiagnostic | None = None,
    ) -> None:
        if not 0.0 < timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds must be in (0, 30]")
        if not 1 <= max_devices <= _MAX_ALLOWED_DEVICES:
            raise ValueError(f"max_devices must be in [1, {_MAX_ALLOWED_DEVICES}]")
        self._enumerator = enumerator
        self._timeout_seconds = timeout_seconds
        self._max_devices = max_devices
        self._mp_context = mp_context
        self._pnp_diagnostic = pnp_diagnostic
        if self._pnp_diagnostic is None and enumerator is None and sys.platform == "win32":
            self._pnp_diagnostic = WindowsRtlSdrPnpDiagnostic()
        self._lock = Lock()

    def discover(self) -> RtlSdrDiscoveryResult:
        with self._lock:
            if self._enumerator is not None:
                result = _discover_direct(self._enumerator, self._max_devices)
            else:
                result = self._discover_isolated()
            return _with_pnp_diagnostic(result, self._pnp_diagnostic)

    def _discover_isolated(self) -> RtlSdrDiscoveryResult:
        context = self._mp_context or multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process: BaseProcess | None = None
        try:
            process = context.Process(
                target=_discovery_worker,
                args=(sending, self._max_devices),
                name="alga-rtl-discovery",
                daemon=True,
            )
            process.start()
            sending.close()
            if not receiving.poll(self._timeout_seconds):
                return _failure_result(
                    state=RtlSdrDiscoveryState.TIMED_OUT,
                    code="DEVICE.RTLSDR_DISCOVERY_TIMEOUT",
                    message_ru="Поиск RTL-SDR не завершился за отведённое время.",
                    operator_action_ru=(
                        "Переподключите приёмник, проверьте драйвер WinUSB и повторите поиск."
                    ),
                    retryable=True,
                )
            try:
                payload = receiving.recv()
            except (EOFError, OSError):
                payload = None
            if isinstance(payload, RtlSdrDiscoveryResult):
                return payload
            return _failure_result(
                state=RtlSdrDiscoveryState.FAILED,
                code="DEVICE.RTLSDR_DISCOVERY_WORKER_FAILED",
                message_ru="Изолированный поиск RTL-SDR завершился без результата.",
                operator_action_ru=(
                    "Проверьте драйвер WinUSB и аппаратный пакет, затем повторите поиск."
                ),
                retryable=True,
            )
        except (OSError, RuntimeError):
            return _failure_result(
                state=RtlSdrDiscoveryState.FAILED,
                code="DEVICE.RTLSDR_DISCOVERY_START_FAILED",
                message_ru="Не удалось запустить безопасный поиск RTL-SDR.",
                operator_action_ru="Перезапустите приложение и повторите поиск.",
                retryable=True,
            )
        finally:
            with suppress(OSError):
                receiving.close()
            with suppress(OSError):
                sending.close()
            if process is not None:
                _stop_process(process)


def _discovery_worker(connection: Connection, max_devices: int) -> None:
    try:
        result = _discover_direct(PyRtlSdrEnumerator(), max_devices)
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        with suppress(OSError):
            connection.close()


def _discover_direct(
    enumerator: RtlSdrEnumerator,
    max_devices: int,
) -> RtlSdrDiscoveryResult:
    try:
        enumeration = enumerator.enumerate_devices(max_devices=max_devices)
    except ModuleNotFoundError:
        return _failure_result(
            state=RtlSdrDiscoveryState.UNAVAILABLE,
            code="DEVICE.PYRTLSDR_MISSING",
            message_ru="Модуль pyrtlsdr не установлен.",
            operator_action_ru="Установите аппаратный пакет ALGA VECTOR.",
            retryable=False,
        )
    except ImportError:
        return _failure_result(
            state=RtlSdrDiscoveryState.UNAVAILABLE,
            code="DEVICE.RTLSDR_LIBRARY_MISSING",
            message_ru="Библиотека librtlsdr не загружена.",
            operator_action_ru="Переустановите аппаратный пакет и драйвер WinUSB.",
            retryable=False,
        )
    except OSError:
        return _failure_result(
            state=RtlSdrDiscoveryState.UNAVAILABLE,
            code="DEVICE.RTLSDR_BACKEND_UNAVAILABLE",
            message_ru="Драйвер RTL-SDR недоступен для чтения списка устройств.",
            operator_action_ru="Проверьте привязку устройства к WinUSB и повторите поиск.",
            retryable=True,
        )
    except Exception:
        return _failure_result(
            state=RtlSdrDiscoveryState.FAILED,
            code="DEVICE.RTLSDR_DISCOVERY_FAILED",
            message_ru="Не удалось прочитать список RTL-SDR.",
            operator_action_ru="Переподключите приёмник и повторите поиск.",
            retryable=True,
        )

    if not isinstance(enumeration, RtlSdrEnumeration):
        return _failure_result(
            state=RtlSdrDiscoveryState.FAILED,
            code="DEVICE.RTLSDR_DISCOVERY_INVALID_RESULT",
            message_ru="Поиск RTL-SDR вернул некорректный результат.",
            operator_action_ru="Переустановите аппаратный пакет и повторите поиск.",
            retryable=True,
        )

    devices = enumeration.devices[:max_devices]
    issues = list(enumeration.issues)
    truncated = enumeration.truncated or len(enumeration.devices) > max_devices
    if len(enumeration.devices) > max_devices and not any(
        issue.code == "DEVICE.RTLSDR_DISCOVERY_LIMIT" for issue in issues
    ):
        issues.append(
            RtlSdrDiscoveryIssue(
                code="DEVICE.RTLSDR_DISCOVERY_LIMIT",
                message_ru="Результат поиска RTL-SDR ограничен безопасным пределом.",
                operator_action_ru="Отключите лишние приёмники и повторите поиск.",
                retryable=True,
            )
        )

    if devices:
        state = (
            RtlSdrDiscoveryState.PARTIAL
            if issues or truncated
            else RtlSdrDiscoveryState.COMPLETE
        )
    elif issues:
        state = RtlSdrDiscoveryState.FAILED
    else:
        state = RtlSdrDiscoveryState.EMPTY
    return RtlSdrDiscoveryResult(
        state=state,
        devices=devices,
        reported_count=max(0, enumeration.reported_count),
        scanned_count=len(devices),
        issues=tuple(issues),
    )


def _failure_result(
    *,
    state: RtlSdrDiscoveryState,
    code: str,
    message_ru: str,
    operator_action_ru: str,
    retryable: bool,
) -> RtlSdrDiscoveryResult:
    return RtlSdrDiscoveryResult(
        state=state,
        devices=(),
        reported_count=0,
        scanned_count=0,
        issues=(
            RtlSdrDiscoveryIssue(
                code=code,
                message_ru=message_ru,
                operator_action_ru=operator_action_ru,
                retryable=retryable,
            ),
        ),
    )


def _with_pnp_diagnostic(
    result: RtlSdrDiscoveryResult,
    diagnostic: RtlSdrPnpDiagnostic | None,
) -> RtlSdrDiscoveryResult:
    if diagnostic is None or not _needs_pnp_diagnostic(result):
        return result
    try:
        issue = diagnostic.diagnose_attached_receiver()
    except Exception:
        # OS diagnostics are supplemental and must never break normal discovery.
        return result
    if issue is None:
        return result

    issues = (
        issue,
        *(existing for existing in result.issues if existing.code != issue.code),
    )
    state = (
        RtlSdrDiscoveryState.UNAVAILABLE
        if result.state == RtlSdrDiscoveryState.EMPTY
        else result.state
    )
    return RtlSdrDiscoveryResult(
        state=state,
        devices=result.devices,
        reported_count=result.reported_count,
        scanned_count=result.scanned_count,
        issues=issues,
    )


def _needs_pnp_diagnostic(result: RtlSdrDiscoveryResult) -> bool:
    if result.devices:
        return False
    if result.state in {
        RtlSdrDiscoveryState.EMPTY,
        RtlSdrDiscoveryState.TIMED_OUT,
    }:
        return True
    return any(
        issue.code
        in {
            "DEVICE.RTLSDR_BACKEND_UNAVAILABLE",
            "DEVICE.RTLSDR_DISCOVERY_WORKER_FAILED",
        }
        for issue in result.issues
    )


def _windows_pnp_issue(
    devices: Iterable[WindowsPnpDeviceStatus],
) -> RtlSdrDiscoveryIssue | None:
    candidates = _preferred_rtl_pnp_candidates(
        tuple(device for device in devices if _is_rtl2832u_device(device))
    )
    if not candidates:
        return None
    device = min(candidates, key=_windows_pnp_issue_priority)
    problem_code = device.problem_code

    if problem_code == 28:
        return RtlSdrDiscoveryIssue(
            code="DEVICE.RTLSDR_WINDOWS_DRIVER_CODE_28",
            message_ru=(
                "Windows видит RTL2832U, но драйвер не установлен "
                "(код диспетчера устройств 28)."
            ),
            operator_action_ru=(
                "Установите WinUSB для интерфейса RTL2832U "
                "(обычно Interface 0), переподключите приёмник и повторите поиск."
            ),
            retryable=True,
        )

    if problem_code is not None and problem_code != 0:
        explanation = _WINDOWS_PROBLEM_TEXT_RU.get(
            problem_code,
            "Windows сообщает об ошибке устройства",
        )
        if problem_code == 22:
            action = (
                "Включите RTL2832U в диспетчере устройств, "
                "затем переподключите его и повторите поиск."
            )
        else:
            action = (
                "Исправьте ошибку устройства в Windows и привяжите WinUSB "
                "к интерфейсу RTL2832U, затем повторите поиск."
            )
        return RtlSdrDiscoveryIssue(
            code="DEVICE.RTLSDR_WINDOWS_DEVICE_PROBLEM",
            message_ru=(
                f"Windows видит RTL2832U, но {explanation} "
                f"(код диспетчера устройств {problem_code})."
            ),
            operator_action_ru=action,
            retryable=True,
        )

    service = _clean_display_string(device.service or "")
    if service.casefold() != "winusb":
        current_driver = f"«{service}»" if service else "не определён"
        return RtlSdrDiscoveryIssue(
            code="DEVICE.RTLSDR_WINDOWS_DRIVER_NOT_WINUSB",
            message_ru=(
                "Windows видит RTL2832U, но его USB-интерфейс привязан "
                f"к драйверу {current_driver}; этой сборке нужен WinUSB."
            ),
            operator_action_ru=(
                "Привяжите WinUSB к интерфейсу RTL2832U "
                "(обычно Interface 0), переподключите приёмник и повторите поиск."
            ),
            retryable=True,
        )

    if not (device.driver_key or "").strip():
        return RtlSdrDiscoveryIssue(
            code="DEVICE.RTLSDR_WINDOWS_DRIVER_INCOMPLETE",
            message_ru=(
                "Windows видит RTL2832U и службу WinUSB, "
                "но запись установленного драйвера неполна."
            ),
            operator_action_ru=(
                "Переустановите WinUSB для интерфейса RTL2832U, "
                "переподключите приёмник и повторите поиск."
            ),
            retryable=True,
        )

    return RtlSdrDiscoveryIssue(
        code="DEVICE.RTLSDR_WINDOWS_BACKEND_HIDDEN",
        message_ru=(
            "Windows видит RTL2832U с рабочей привязкой WinUSB, "
            "но librtlsdr не получил устройство."
        ),
        operator_action_ru=(
            "Закройте другие SDR-программы, переподключите приёмник "
            "в другой USB-порт и повторите поиск."
        ),
        retryable=True,
    )


def _preferred_rtl_pnp_candidates(
    candidates: tuple[WindowsPnpDeviceStatus, ...],
) -> tuple[WindowsPnpDeviceStatus, ...]:
    """Prefer the SDR data interface over composite parents and auxiliary MI_01."""

    interface_zero = tuple(
        device
        for device in candidates
        if any("&MI_00" in identifier.upper() for identifier in device.hardware_ids)
    )
    if interface_zero:
        return interface_zero
    other_interfaces = tuple(
        device
        for device in candidates
        if any("&MI_" in identifier.upper() for identifier in device.hardware_ids)
    )
    return other_interfaces or candidates


def _is_rtl2832u_device(device: WindowsPnpDeviceStatus) -> bool:
    identifiers = tuple(identifier.upper() for identifier in device.hardware_ids)
    if any(
        rtl_usb_id in identifier
        for identifier in identifiers
        for rtl_usb_id in _WINDOWS_RTL_USB_IDS
    ):
        return True
    description = device.description.upper()
    return any(marker in description for marker in _WINDOWS_RTL_NAME_MARKERS)


def _windows_pnp_issue_priority(device: WindowsPnpDeviceStatus) -> tuple[int, int]:
    problem_code = device.problem_code
    if problem_code == 28:
        return (0, problem_code)
    if problem_code is not None and problem_code != 0:
        return (1, problem_code)
    if (device.service or "").strip().casefold() != "winusb":
        return (2, 0)
    if not (device.driver_key or "").strip():
        return (3, 0)
    return (4, 0)


class _WindowsGuid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _WindowsSpDevinfoData(ctypes.Structure):
    _fields_ = [
        ("cb_size", ctypes.c_uint32),
        ("class_guid", _WindowsGuid),
        ("dev_inst", ctypes.c_uint32),
        ("reserved", ctypes.c_size_t),
    ]


def _read_present_windows_pnp_devices() -> tuple[WindowsPnpDeviceStatus, ...]:
    """Return present RTL-like PnP records using read-only Windows APIs."""

    if sys.platform != "win32":
        return ()
    setupapi, cfgmgr32 = _load_windows_pnp_libraries()
    flags = _WINDOWS_DIGCF_PRESENT | _WINDOWS_DIGCF_ALLCLASSES
    handle = setupapi.SetupDiGetClassDevsW(None, None, None, flags)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid_handle:
        raise OSError(_windows_last_error(), "SetupDiGetClassDevsW failed")

    devices: list[WindowsPnpDeviceStatus] = []
    try:
        index = 0
        while True:
            device_info = _WindowsSpDevinfoData()
            device_info.cb_size = ctypes.sizeof(_WindowsSpDevinfoData)
            if not setupapi.SetupDiEnumDeviceInfo(
                handle,
                index,
                ctypes.byref(device_info),
            ):
                error_code = _windows_last_error()
                if error_code == _WINDOWS_ERROR_NO_MORE_ITEMS:
                    break
                raise OSError(error_code, "SetupDiEnumDeviceInfo failed")
            index += 1

            hardware_ids = _read_windows_device_property(
                setupapi,
                handle,
                device_info,
                _WINDOWS_SPDRP_HARDWAREID,
            )
            friendly_name = _first_property_value(
                _read_windows_device_property(
                    setupapi,
                    handle,
                    device_info,
                    _WINDOWS_SPDRP_FRIENDLYNAME,
                )
            )
            description = friendly_name or _first_property_value(
                _read_windows_device_property(
                    setupapi,
                    handle,
                    device_info,
                    _WINDOWS_SPDRP_DEVICEDESC,
                )
            )
            candidate = WindowsPnpDeviceStatus(
                hardware_ids=hardware_ids,
                description=_clean_display_string(description),
                service=_optional_clean_string(
                    _first_property_value(
                        _read_windows_device_property(
                            setupapi,
                            handle,
                            device_info,
                            _WINDOWS_SPDRP_SERVICE,
                        )
                    )
                ),
                driver_key=_optional_clean_string(
                    _first_property_value(
                        _read_windows_device_property(
                            setupapi,
                            handle,
                            device_info,
                            _WINDOWS_SPDRP_DRIVER,
                        )
                    )
                ),
                problem_code=_read_windows_problem_code(cfgmgr32, device_info),
            )
            if _is_rtl2832u_device(candidate):
                devices.append(candidate)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return tuple(devices)


def _load_windows_pnp_libraries() -> tuple[Any, Any]:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows native library loader is unavailable")
    setupapi = win_dll("setupapi", use_last_error=True)
    cfgmgr32 = win_dll("cfgmgr32", use_last_error=True)

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsSpDevinfoData),
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsSpDevinfoData),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_int
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
    cfgmgr32.CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    cfgmgr32.CM_Get_DevNode_Status.restype = ctypes.c_uint32
    return setupapi, cfgmgr32


def _read_windows_device_property(
    setupapi: Any,
    handle: object,
    device_info: _WindowsSpDevinfoData,
    property_code: int,
) -> tuple[str, ...]:
    registry_type = ctypes.c_uint32()
    required_bytes = ctypes.c_uint32()
    success = setupapi.SetupDiGetDeviceRegistryPropertyW(
        handle,
        ctypes.byref(device_info),
        property_code,
        ctypes.byref(registry_type),
        None,
        0,
        ctypes.byref(required_bytes),
    )
    if not success and _windows_last_error() != _WINDOWS_ERROR_INSUFFICIENT_BUFFER:
        return ()
    if required_bytes.value == 0 or required_bytes.value > 65_536:
        return ()

    buffer = (ctypes.c_ubyte * required_bytes.value)()
    success = setupapi.SetupDiGetDeviceRegistryPropertyW(
        handle,
        ctypes.byref(device_info),
        property_code,
        ctypes.byref(registry_type),
        buffer,
        required_bytes.value,
        ctypes.byref(required_bytes),
    )
    if not success:
        return ()
    text = bytes(buffer[: required_bytes.value]).decode("utf-16-le", "replace")
    values = tuple(
        _clean_display_string(value)
        for value in text.rstrip("\0").split("\0")
        if _clean_display_string(value)
    )
    if registry_type.value == _WINDOWS_REG_MULTI_SZ:
        return values
    return values[:1]


def _read_windows_problem_code(
    cfgmgr32: Any,
    device_info: _WindowsSpDevinfoData,
) -> int | None:
    status = ctypes.c_uint32()
    problem_code = ctypes.c_uint32()
    result = cfgmgr32.CM_Get_DevNode_Status(
        ctypes.byref(status),
        ctypes.byref(problem_code),
        device_info.dev_inst,
        0,
    )
    if int(result) != _WINDOWS_CR_SUCCESS:
        return None
    return int(problem_code.value)


def _first_property_value(values: tuple[str, ...]) -> str:
    return values[0] if values else ""


def _windows_last_error() -> int:
    get_last_error = getattr(ctypes, "get_last_error", None)
    return int(get_last_error()) if callable(get_last_error) else 0


def _descriptor_issue(index: int) -> RtlSdrDiscoveryIssue:
    return RtlSdrDiscoveryIssue(
        code="DEVICE.RTLSDR_NAME_UNAVAILABLE",
        message_ru="Описание одного RTL-SDR недоступно.",
        operator_action_ru="Идентифицируйте приёмник по индексу или серийному номеру.",
        retryable=True,
        device_index=index,
    )


def _usb_strings_issue(index: int) -> RtlSdrDiscoveryIssue:
    return RtlSdrDiscoveryIssue(
        code="DEVICE.RTLSDR_USB_STRINGS_UNAVAILABLE",
        message_ru="USB-описание одного RTL-SDR прочитано не полностью.",
        operator_action_ru="Проверьте драйвер WinUSB или выберите приёмник по индексу.",
        retryable=True,
        device_index=index,
    )


def _decode_byte_buffer(buffer: object) -> str:
    raw = bytes(
        value
        for value in cast(Iterable[int], buffer)
        if isinstance(value, int)
    )
    return _decode_native_string(raw.split(b"\0", 1)[0])


def _decode_native_string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _clean_display_string(value.split(b"\0", 1)[0].decode("utf-8", "replace"))
    return _clean_display_string(str(value))


def _optional_clean_string(value: str) -> str | None:
    cleaned = _clean_display_string(value)
    return cleaned or None


def _clean_display_string(value: str) -> str:
    cleaned = " ".join(
        "".join(character if character.isprintable() else " " for character in value).split()
    )
    return cleaned[:_DISPLAY_STRING_LIMIT]


def _stop_process(process: BaseProcess) -> None:
    alive = False
    with suppress(OSError, ValueError):
        alive = process.is_alive()
    if alive:
        with suppress(OSError, ValueError):
            process.terminate()
        with suppress(OSError, ValueError):
            process.join(_PROCESS_JOIN_TIMEOUT_SECONDS)
    with suppress(OSError, ValueError):
        alive = process.is_alive()
    if alive:
        with suppress(OSError, ValueError):
            process.kill()
        with suppress(OSError, ValueError):
            process.join(_PROCESS_JOIN_TIMEOUT_SECONDS)
    else:
        with suppress(OSError, ValueError):
            process.join(0)
    with suppress(OSError, ValueError):
        process.close()


__all__ = [
    "PyRtlSdrEnumerator",
    "RtlSdrDiscoveredDevice",
    "RtlSdrDiscoveryIssue",
    "RtlSdrDiscoveryResult",
    "RtlSdrDiscoveryService",
    "RtlSdrDiscoveryState",
    "RtlSdrEnumeration",
    "RtlSdrEnumerator",
    "RtlSdrPnpDiagnostic",
    "WindowsPnpDeviceStatus",
    "WindowsRtlSdrPnpDiagnostic",
]
