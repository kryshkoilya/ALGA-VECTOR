"""Read-only discovery for HackRF host tools and tinySA serial metadata."""

# ruff: noqa: RUF001

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .host_tools import (
    HostToolError,
    HostTools,
    HostToolTimedOut,
    SubprocessHostTools,
)
from .live import is_explicit_windows_com_port

_HACKRF_INFO_OUTPUT_LIMIT = 512 * 1024
_DISPLAY_TEXT_LIMIT = 160
_MAX_SERIAL_PORTS = 64
_HACKRF_SERIAL_RE = re.compile(r"(?i)^[0-9a-f]{8,64}$")


class ReceiverDiscoveryState(StrEnum):
    COMPLETE = "complete"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ReceiverDiscoveryIssue:
    code: str
    message_ru: str
    operator_action_ru: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class HackRfDiscoveredDevice:
    """One device confirmed by ``hackrf_info`` in HackRF USB mode."""

    index: int
    connection: str
    serial: str
    board_name: str
    firmware: str | None = None


@dataclass(frozen=True, slots=True)
class HackRfDiscoveryResult:
    state: ReceiverDiscoveryState
    devices: tuple[HackRfDiscoveredDevice, ...]
    issues: tuple[ReceiverDiscoveryIssue, ...] = ()

    @property
    def successful(self) -> bool:
        return self.state in {
            ReceiverDiscoveryState.COMPLETE,
            ReceiverDiscoveryState.EMPTY,
            ReceiverDiscoveryState.PARTIAL,
        }


class HackRfDiscoveryService:
    """Discover devices through the official descriptor tool without TX APIs."""

    def __init__(
        self,
        *,
        host_tools: HostTools | None = None,
        timeout_seconds: float = 3.0,
        attempts: int = 2,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= attempts <= 3:
            raise ValueError("attempts must be in range 1..3")
        self._host_tools = host_tools or SubprocessHostTools()
        self._timeout_seconds = timeout_seconds
        self._attempts = attempts

    def discover(self) -> HackRfDiscoveryResult:
        tool = self._host_tools.find("hackrf_info")
        if tool is None:
            return HackRfDiscoveryResult(
                state=ReceiverDiscoveryState.UNAVAILABLE,
                devices=(),
                issues=(
                    ReceiverDiscoveryIssue(
                        code="DEVICE.HACKRF_INFO_MISSING",
                        message_ru="Официальный инструмент hackrf_info не найден.",
                        operator_action_ru=(
                            "Установите совместимый пакет HackRF host tools "
                            "или поместите его в каталог hardware-tools приложения."
                        ),
                        retryable=False,
                    ),
                ),
            )

        last_issue: ReceiverDiscoveryIssue | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                result = self._host_tools.run(
                    (tool,),
                    timeout_seconds=self._timeout_seconds,
                    maximum_stdout_bytes=_HACKRF_INFO_OUTPUT_LIMIT,
                )
            except HostToolTimedOut:
                last_issue = ReceiverDiscoveryIssue(
                    code="DEVICE.HACKRF_DISCOVERY_TIMEOUT",
                    message_ru="hackrf_info не завершил поиск в отведённое время.",
                    operator_action_ru=(
                        "Переведите PortaPack в HackRF USB mode, "
                        "переподключите USB и повторите поиск."
                    ),
                    retryable=True,
                )
                if attempt < self._attempts:
                    continue
                return HackRfDiscoveryResult(
                    state=ReceiverDiscoveryState.TIMED_OUT,
                    devices=(),
                    issues=(last_issue,),
                )
            except HostToolError:
                last_issue = ReceiverDiscoveryIssue(
                    code="DEVICE.HACKRF_DISCOVERY_FAILED",
                    message_ru="Не удалось безопасно выполнить hackrf_info.",
                    operator_action_ru=(
                        "Проверьте версию host tools и USB-драйвер HackRF."
                    ),
                    retryable=True,
                )
                if attempt < self._attempts:
                    continue
                return HackRfDiscoveryResult(
                    state=ReceiverDiscoveryState.FAILED,
                    devices=(),
                    issues=(last_issue,),
                )

            combined = result.stdout + b"\n" + result.stderr
            text = combined.decode("utf-8", errors="replace")
            devices, parse_issues = _parse_hackrf_info(text)
            if devices:
                state = (
                    ReceiverDiscoveryState.PARTIAL
                    if parse_issues or result.returncode != 0
                    else ReceiverDiscoveryState.COMPLETE
                )
                return HackRfDiscoveryResult(
                    state=state,
                    devices=devices,
                    issues=parse_issues,
                )
            if "no hackrf boards found" in text.casefold():
                return HackRfDiscoveryResult(
                    state=ReceiverDiscoveryState.EMPTY,
                    devices=(),
                )
            last_issue = ReceiverDiscoveryIssue(
                code="DEVICE.HACKRF_INFO_REJECTED",
                message_ru="hackrf_info завершился без подтверждённого устройства.",
                operator_action_ru=(
                    "Убедитесь, что PortaPack находится в HackRF USB mode, "
                    "и проверьте USB-драйвер."
                ),
                retryable=True,
            )
            if attempt < self._attempts:
                continue
            return HackRfDiscoveryResult(
                state=ReceiverDiscoveryState.FAILED,
                devices=(),
                issues=(last_issue,),
            )

        # The loop always returns, but keep the fail-closed branch explicit.
        return HackRfDiscoveryResult(
            state=ReceiverDiscoveryState.FAILED,
            devices=(),
            issues=((last_issue,) if last_issue is not None else ()),
        )


class SerialCandidateConfidence(StrEnum):
    CONFIRMED_METADATA = "confirmed_metadata"
    POSSIBLE_USB_SERIAL = "possible_usb_serial"


@dataclass(frozen=True, slots=True)
class TinySaSerialCandidate:
    """Serial metadata only; no port was opened during discovery."""

    connection: str
    description: str
    manufacturer: str | None
    product: str | None
    vid: int | None
    pid: int | None
    confidence: SerialCandidateConfidence
    evidence_ru: str


@dataclass(frozen=True, slots=True)
class TinySaDiscoveryResult:
    state: ReceiverDiscoveryState
    candidates: tuple[TinySaSerialCandidate, ...]
    scanned_port_count: int
    issues: tuple[ReceiverDiscoveryIssue, ...] = ()

    @property
    def successful(self) -> bool:
        return self.state in {
            ReceiverDiscoveryState.COMPLETE,
            ReceiverDiscoveryState.EMPTY,
            ReceiverDiscoveryState.PARTIAL,
        }


class TinySaSerialDiscoveryService:
    """Enumerate serial descriptors only; never opens or writes to a COM port."""

    def __init__(
        self,
        *,
        port_provider: Callable[[], Iterable[object]] | None = None,
        maximum_ports: int = _MAX_SERIAL_PORTS,
    ) -> None:
        if not 1 <= maximum_ports <= _MAX_SERIAL_PORTS:
            raise ValueError(f"maximum_ports must be in range 1..{_MAX_SERIAL_PORTS}")
        self._port_provider = port_provider
        self._maximum_ports = maximum_ports

    def discover(self) -> TinySaDiscoveryResult:
        provider = self._port_provider
        if provider is None:
            try:
                module = importlib.import_module("serial.tools.list_ports")
            except ImportError:
                return TinySaDiscoveryResult(
                    state=ReceiverDiscoveryState.UNAVAILABLE,
                    candidates=(),
                    scanned_port_count=0,
                    issues=(
                        ReceiverDiscoveryIssue(
                            code="DEVICE.PYSERIAL_MISSING",
                            message_ru="Модуль pyserial для просмотра COM-портов не установлен.",
                            operator_action_ru="Установите аппаратный пакет ALGA VECTOR.",
                            retryable=False,
                        ),
                    ),
                )
            provider = cast(Callable[[], Iterable[object]], cast(Any, module).comports)

        try:
            ports = tuple(provider())
        except Exception:
            return TinySaDiscoveryResult(
                state=ReceiverDiscoveryState.FAILED,
                candidates=(),
                scanned_port_count=0,
                issues=(
                    ReceiverDiscoveryIssue(
                        code="DEVICE.SERIAL_METADATA_FAILED",
                        message_ru="Windows не вернул метаданные последовательных портов.",
                        operator_action_ru="Обновите список устройств или переподключите USB.",
                        retryable=True,
                    ),
                ),
            )

        truncated = len(ports) > self._maximum_ports
        candidates = tuple(
            candidate
            for port in ports[: self._maximum_ports]
            if (candidate := _tinysa_candidate_from_port(port)) is not None
        )
        issues: list[ReceiverDiscoveryIssue] = []
        if truncated:
            issues.append(
                ReceiverDiscoveryIssue(
                    code="DEVICE.SERIAL_METADATA_TRUNCATED",
                    message_ru="Список COM-портов ограничен безопасным пределом.",
                    operator_action_ru="Отключите лишние USB-COM устройства и повторите поиск.",
                    retryable=True,
                )
            )
        if any(
            candidate.confidence == SerialCandidateConfidence.POSSIBLE_USB_SERIAL
            for candidate in candidates
        ):
            issues.append(
                ReceiverDiscoveryIssue(
                    code="DEVICE.TINYSA_CONFIRMATION_REQUIRED",
                    message_ru=(
                        "Часть портов похожа только на USB Serial; "
                        "принадлежность tinySA не подтверждена метаданными."
                    ),
                    operator_action_ru=(
                        "Сверьте выбранный COM-порт в диспетчере устройств. "
                        "Порт будет открыт только после явного добавления."
                    ),
                    retryable=False,
                )
            )

        if not candidates:
            state = (
                ReceiverDiscoveryState.PARTIAL
                if issues
                else ReceiverDiscoveryState.EMPTY
            )
        else:
            state = (
                ReceiverDiscoveryState.PARTIAL
                if issues
                else ReceiverDiscoveryState.COMPLETE
            )
        return TinySaDiscoveryResult(
            state=state,
            candidates=candidates,
            scanned_port_count=min(len(ports), self._maximum_ports),
            issues=tuple(issues),
        )


def _parse_hackrf_info(
    payload: str,
) -> tuple[
    tuple[HackRfDiscoveredDevice, ...],
    tuple[ReceiverDiscoveryIssue, ...],
]:
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if line.casefold().startswith("found hackrf"):
            if current:
                records.append(current)
            current = {}
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().casefold()] = _clean_text(value)
    if current is not None:
        records.append(current)

    devices: list[HackRfDiscoveredDevice] = []
    issues: list[ReceiverDiscoveryIssue] = []
    seen_serials: set[str] = set()
    for fallback_index, record in enumerate(records):
        serial = record.get("serial number", "").strip().lower()
        if _HACKRF_SERIAL_RE.fullmatch(serial) is None or serial in seen_serials:
            issues.append(
                ReceiverDiscoveryIssue(
                    code="DEVICE.HACKRF_DESCRIPTOR_INCOMPLETE",
                    message_ru="hackrf_info вернул неполный или повторяющийся дескриптор.",
                    operator_action_ru="Обновите host tools/прошивку и повторите поиск.",
                    retryable=True,
                )
            )
            continue
        index_text = record.get("index", str(fallback_index))
        try:
            index = max(0, int(index_text))
        except ValueError:
            index = fallback_index
        board = record.get("board id number", "HackRF One")
        if "(" in board and ")" in board:
            board = board.split("(", 1)[1].rsplit(")", 1)[0].strip()
        seen_serials.add(serial)
        devices.append(
            HackRfDiscoveredDevice(
                index=index,
                connection=f"HACKRF:{serial}",
                serial=serial,
                board_name=_clean_text(board) or "HackRF One",
                firmware=_optional_text(record.get("firmware version", "")),
            )
        )
    return tuple(devices), tuple(issues)


def _tinysa_candidate_from_port(port: object) -> TinySaSerialCandidate | None:
    connection = _clean_text(getattr(port, "device", ""))
    if not is_explicit_windows_com_port(connection):
        return None
    description = _clean_text(getattr(port, "description", "")) or "USB Serial"
    manufacturer = _optional_text(getattr(port, "manufacturer", ""))
    product = _optional_text(getattr(port, "product", ""))
    interface = _optional_text(getattr(port, "interface", ""))
    fields = " ".join(
        value
        for value in (description, manufacturer, product, interface)
        if value
    ).casefold()
    vid = _optional_int(getattr(port, "vid", None))
    pid = _optional_int(getattr(port, "pid", None))
    if "tinysa" in fields:
        confidence = SerialCandidateConfidence.CONFIRMED_METADATA
        evidence = "В USB/serial-метаданных присутствует явная строка tinySA."
    elif vid is not None and (
        "usb serial" in fields
        or "usb-serial" in fields
        or "cdc" in fields
    ):
        confidence = SerialCandidateConfidence.POSSIBLE_USB_SERIAL
        evidence = (
            "Это USB Serial/CDC-порт, но метаданные не подтверждают модель tinySA."
        )
    else:
        return None
    return TinySaSerialCandidate(
        connection=connection.upper(),
        description=description,
        manufacturer=manufacturer,
        product=product,
        vid=vid,
        pid=pid,
        confidence=confidence,
        evidence_ru=evidence,
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and 0 <= value <= 0xFFFF:
        return value
    return None


def _optional_text(value: object) -> str | None:
    cleaned = _clean_text(value)
    return cleaned or None


def _clean_text(value: object) -> str:
    return " ".join(str(value).replace("\x00", " ").split())[:_DISPLAY_TEXT_LIMIT]


__all__ = [
    "HackRfDiscoveredDevice",
    "HackRfDiscoveryResult",
    "HackRfDiscoveryService",
    "ReceiverDiscoveryIssue",
    "ReceiverDiscoveryState",
    "SerialCandidateConfidence",
    "TinySaDiscoveryResult",
    "TinySaSerialCandidate",
    "TinySaSerialDiscoveryService",
]
