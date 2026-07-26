from __future__ import annotations

import ctypes
import json
import os
import sys
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import GeoPoint, LocationSource

_MAGIC = b"ALGA-GEO\x01"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecureStoreError(RuntimeError):
    pass


class SecureStoreUnavailableError(SecureStoreError):
    pass


class SecureStoreCorruptError(SecureStoreError):
    pass


class DataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


@dataclass(slots=True, frozen=True)
class StoredLocation:
    point: GeoPoint = field(repr=False)
    source: LocationSource
    saved_at: datetime

    def __post_init__(self) -> None:
        if self.saved_at.tzinfo is None or self.saved_at.utcoffset() is None:
            raise ValueError("saved_at must be timezone-aware")
        object.__setattr__(self, "saved_at", self.saved_at.astimezone(UTC))

    def __repr__(self) -> str:
        return (
            f"StoredLocation(point=<redacted>, source={self.source.value!r}, "
            f"saved_at={self.saved_at.isoformat()!r})"
        )


class WindowsDpapiProtector:
    """Current-user DPAPI wrapper; it never uses machine-wide decryption."""

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecureStoreUnavailableError("Windows DPAPI is unavailable")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError as exc:
            raise SecureStoreUnavailableError("Windows DPAPI could not be loaded") from exc
        blob_pointer = ctypes.POINTER(self._DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, protect=False)

    def _transform(self, payload: bytes, *, protect: bool) -> bytes:
        if not isinstance(payload, bytes):
            raise TypeError("DPAPI payload must be bytes")
        if not payload:
            raise SecureStoreError("DPAPI payload cannot be empty")
        input_buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        input_blob = self._DataBlob(
            len(payload),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = self._DataBlob()
        if protect:
            succeeded = bool(
                self._crypt32.CryptProtectData(
                    ctypes.byref(input_blob),
                    "ALGA VECTOR local base location",
                    None,
                    None,
                    None,
                    _CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            )
        else:
            description = wintypes.LPWSTR()
            succeeded = bool(
                self._crypt32.CryptUnprotectData(
                    ctypes.byref(input_blob),
                    ctypes.byref(description),
                    None,
                    None,
                    None,
                    _CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            )
            if description:
                self._kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        if not succeeded:
            error_code = ctypes.get_last_error()
            raise SecureStoreError(f"Windows DPAPI operation failed ({error_code})")
        try:
            return bytes(ctypes.string_at(output_blob.pbData, output_blob.cbData))
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(output_blob.pbData, wintypes.HLOCAL)
                )


class SecureLocationStore:
    """Atomic encrypted storage for one local base point."""

    def __init__(
        self,
        path: Path,
        protector: DataProtector | None = None,
    ) -> None:
        self.path = Path(path)
        self.protector = protector or WindowsDpapiProtector()

    def save(
        self,
        point: GeoPoint,
        source: LocationSource,
        *,
        saved_at: datetime | None = None,
    ) -> StoredLocation:
        if not isinstance(point, GeoPoint):
            raise TypeError("point must be a GeoPoint")
        if not isinstance(source, LocationSource):
            raise TypeError("source must be a LocationSource")
        timestamp = saved_at or datetime.now(UTC)
        stored = StoredLocation(point, source, timestamp)
        payload = {
            "format": "alga-vector-location",
            "version": 1,
            "latitude_deg": point.latitude_deg,
            "longitude_deg": point.longitude_deg,
            "altitude_m": point.altitude_m,
            "source": source.value,
            "saved_at": stored.saved_at.isoformat(),
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        protected = self.protector.protect(plaintext)
        if not protected:
            raise SecureStoreError("protector returned an empty payload")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(_MAGIC)
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return stored

    def load(self) -> StoredLocation | None:
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise SecureStoreError("protected location could not be read") from exc
        if not raw.startswith(_MAGIC) or len(raw) == len(_MAGIC):
            raise SecureStoreCorruptError("protected location header is invalid")
        try:
            plaintext = self.protector.unprotect(raw[len(_MAGIC) :])
            decoded = json.loads(plaintext.decode("utf-8"))
            return _stored_location_from_payload(decoded)
        except SecureStoreError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SecureStoreCorruptError("protected location payload is invalid") from exc

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        try:
            self.path.unlink()
        except OSError as exc:
            raise SecureStoreError("protected location could not be removed") from exc
        return True


def _stored_location_from_payload(value: object) -> StoredLocation:
    if not isinstance(value, Mapping):
        raise SecureStoreCorruptError("protected location root must be an object")
    expected_keys = {
        "format",
        "version",
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "source",
        "saved_at",
    }
    if set(value) != expected_keys:
        raise SecureStoreCorruptError("protected location fields are invalid")
    if value["format"] != "alga-vector-location" or value["version"] != 1:
        raise SecureStoreCorruptError("protected location version is unsupported")
    latitude = _payload_number(value["latitude_deg"], "latitude_deg")
    longitude = _payload_number(value["longitude_deg"], "longitude_deg")
    raw_altitude = value["altitude_m"]
    altitude = (
        None
        if raw_altitude is None
        else _payload_number(raw_altitude, "altitude_m")
    )
    source_value = value["source"]
    timestamp_value = value["saved_at"]
    if not isinstance(source_value, str) or not isinstance(timestamp_value, str):
        raise SecureStoreCorruptError("protected location metadata is invalid")
    return StoredLocation(
        GeoPoint(latitude, longitude, altitude),
        LocationSource(source_value),
        datetime.fromisoformat(timestamp_value),
    )


def _payload_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise SecureStoreCorruptError(f"{name} is not numeric")
    return float(value)


__all__ = [
    "DataProtector",
    "SecureLocationStore",
    "SecureStoreCorruptError",
    "SecureStoreError",
    "SecureStoreUnavailableError",
    "StoredLocation",
    "WindowsDpapiProtector",
]
