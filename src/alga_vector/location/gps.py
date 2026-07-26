"""Read-only NMEA receiver and metadata-only COM-port candidate discovery."""

# ruff: noqa: RUF001

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, RLock, Thread
from types import ModuleType
from typing import Any

from .nmea import NmeaError
from .service import LocationService

_COM_PORT_RE = re.compile(r"(?i)^COM(?:[1-9]|[1-9]\d|[12]\d\d)$")
_GPS_HINTS = (
    "gps",
    "gnss",
    "nmea",
    "u-blox",
    "ublox",
    "garmin",
    "sirf",
    "neo-6",
    "neo-7",
    "neo-8",
    "zed-f",
)


class GpsPortConfidence(StrEnum):
    LIKELY = "likely"
    POSSIBLE = "possible"


@dataclass(slots=True, frozen=True)
class GpsPortCandidate:
    port: str
    display_name: str
    confidence: GpsPortConfidence
    reason_ru: str

    def __post_init__(self) -> None:
        normalized = self.port.strip().upper()
        if _COM_PORT_RE.fullmatch(normalized) is None:
            raise ValueError("candidate port must be one Windows COM port")
        object.__setattr__(self, "port", normalized)


PortEnumerator = Callable[[], Iterable[object]]


class GpsReceiverError(RuntimeError):
    pass


def discover_nmea_port_candidates(
    *,
    port_enumerator: PortEnumerator | None = None,
) -> tuple[GpsPortCandidate, ...]:
    """List serial metadata without opening, probing or writing to any port."""

    enumerator = port_enumerator
    if enumerator is None:
        try:
            list_ports = importlib.import_module("serial.tools.list_ports")
        except ImportError as exc:
            raise GpsReceiverError("pyserial is not installed") from exc
        enumerator = list_ports.comports
    candidates: list[GpsPortCandidate] = []
    seen: set[str] = set()
    try:
        ports = enumerator()
    except Exception as exc:
        raise GpsReceiverError(
            f"cannot enumerate serial metadata: {type(exc).__name__}"
        ) from exc
    for info in ports:
        port = str(getattr(info, "device", "")).strip().upper()
        if _COM_PORT_RE.fullmatch(port) is None or port in seen:
            continue
        seen.add(port)
        description = _safe_metadata(getattr(info, "description", ""))
        manufacturer = _safe_metadata(getattr(info, "manufacturer", ""))
        product = _safe_metadata(getattr(info, "product", ""))
        metadata = " ".join(
            part for part in (description, manufacturer, product) if part
        )
        lowered = metadata.casefold()
        likely = any(hint in lowered for hint in _GPS_HINTS)
        display = description or product or manufacturer or "Последовательный порт"
        candidates.append(
            GpsPortCandidate(
                port=port,
                display_name=f"{port} · {display}",
                confidence=(
                    GpsPortConfidence.LIKELY
                    if likely
                    else GpsPortConfidence.POSSIBLE
                ),
                reason_ru=(
                    "В системном описании есть признак GPS/GNSS."
                    if likely
                    else "Это доступный COM-порт; назначение нужно подтвердить."
                ),
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.confidence is not GpsPortConfidence.LIKELY,
                _port_number(item.port),
            ),
        )
    )


class NmeaSerialReceiver:
    """Read one explicitly selected GPS/NMEA serial port on a daemon thread."""

    def __init__(
        self,
        service: LocationService,
        port: str,
        *,
        baudrate: int = 9_600,
        serial_module: ModuleType | None = None,
    ) -> None:
        normalized = port.strip().upper()
        if _COM_PORT_RE.fullmatch(normalized) is None:
            raise ValueError("GPS port must be one explicit Windows COM port")
        if not 1_200 <= baudrate <= 921_600:
            raise ValueError("GPS baudrate is outside the supported range")
        self.service = service
        self.port = normalized
        self.baudrate = baudrate
        self._serial_module = serial_module
        self._serial: Any | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = RLock()
        self._last_error = ""
        self._accepted_sentences = 0
        self._rejected_sentences = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def status(self) -> dict[str, object]:
        location = self.service.current_snapshot()
        with self._lock:
            return {
                "port": self.port,
                "baudrate": self.baudrate,
                "running": self.running,
                "accepted_sentences": self._accepted_sentences,
                "rejected_sentences": self._rejected_sentences,
                "last_error": self._last_error,
                "fix_state": location.gps_fix_state.value,
                "fix_dimension": location.fix_dimension.value,
                "location_status": location.status.value,
                "last_receiver_at": location.last_receiver_at,
            }

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._open()
            self.service.begin_collection()
            self._thread = Thread(
                target=self._run,
                name=f"alga-gps-{self.port}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        serial_port = self._serial
        if serial_port is not None:
            with suppress(Exception):
                serial_port.cancel_read()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._serial = None
        if serial_port is not None:
            with suppress(Exception):
                serial_port.close()

    def process_line(self, line: bytes | str) -> bool:
        """Parse one bounded line; exposed for deterministic hardware tests."""

        raw = line.encode("ascii", errors="ignore") if isinstance(line, str) else bytes(line)
        if len(raw) > 256:
            with self._lock:
                self._rejected_sentences += 1
                self._last_error = "NMEA sentence exceeds 256 bytes"
            return False
        try:
            self.service.ingest_nmea(raw)
        except (NmeaError, TypeError, ValueError) as exc:
            with self._lock:
                self._rejected_sentences += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        with self._lock:
            self._accepted_sentences += 1
            self._last_error = ""
        return True

    def _open(self) -> None:
        module = self._serial_module
        if module is None:
            try:
                module = importlib.import_module("serial")
            except ImportError as exc:
                raise GpsReceiverError("pyserial is not installed") from exc
        try:
            self._serial = module.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=1.0,
                write_timeout=1.0,
            )
        except Exception as exc:
            raise GpsReceiverError(
                f"cannot open configured GPS port {self.port}: {type(exc).__name__}"
            ) from exc

    def _run(self) -> None:
        serial_port = self._serial
        if serial_port is None:
            return
        while not self._stop.is_set():
            try:
                line = bytes(serial_port.readline(257))
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: serial read failed"
                return
            if line:
                self.process_line(line)


def _safe_metadata(value: object) -> str:
    text = " ".join(str(value or "").split())
    return "".join(character for character in text if character.isprintable())[:120]


def _port_number(port: str) -> int:
    return int(port[3:])


__all__ = [
    "GpsPortCandidate",
    "GpsPortConfidence",
    "GpsReceiverError",
    "NmeaSerialReceiver",
    "discover_nmea_port_candidates",
]
