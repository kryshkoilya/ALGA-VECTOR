"""Crash-aware local recording for measured spectrum frames.

The format is newline-delimited JSON so a partially written capture remains
inspectable without a proprietary reader.  It records processed spectrum
frames, not raw IQ samples, and says so explicitly in the header.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from alga_vector.domain.models import SpectrumFrame, utc_now

Clock = Callable[[], datetime]


class SpectrumCaptureError(RuntimeError):
    """Raised when a spectrum capture cannot be started or finalized safely."""


@dataclass(slots=True, frozen=True)
class SpectrumCaptureResult:
    path: Path
    sha256: str
    frames: int
    bytes_written: int
    dropped_frames: int
    started_at: datetime
    stopped_at: datetime


@dataclass(slots=True, frozen=True)
class SpectrumCaptureStatus:
    active: bool
    path: Path | None
    completed_path: Path | None
    started_at: datetime | None
    elapsed_seconds: float
    frames: int
    bytes_written: int
    bytes_per_second: float
    dropped_frames: int
    format_name: str = "ALGA Spectrum JSONL v1"
    content_kind: str = "processed_spectrum"


class SpectrumCaptureWriter:
    """Write bounded spectrum snapshots to a durable local artifact."""

    def __init__(self, directory: Path, *, clock: Clock = utc_now) -> None:
        self._directory = Path(directory)
        self._clock = clock
        self._handle: BinaryIO | None = None
        self._partial_path: Path | None = None
        self._started_at: datetime | None = None
        self._frames = 0
        self._bytes_written = 0
        self._dropped_frames = 0
        self._digest = hashlib.sha256()
        self._last_result: SpectrumCaptureResult | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self) -> SpectrumCaptureStatus:
        if self.active:
            raise SpectrumCaptureError("spectrum capture is already active")
        self._directory.mkdir(parents=True, exist_ok=True)
        started_at = self._clock()
        stem = f"alga-spectrum-{started_at:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
        partial_path = self._directory / f"{stem}.jsonl.partial"
        try:
            handle = partial_path.open("xb")
        except OSError as exc:
            raise SpectrumCaptureError(f"cannot create capture: {exc}") from exc

        self._handle = handle
        self._partial_path = partial_path
        self._started_at = started_at
        self._frames = 0
        self._bytes_written = 0
        self._dropped_frames = 0
        self._digest = hashlib.sha256()
        try:
            self._write_record(
                {
                    "type": "header",
                    "schema": "alga-vector-spectrum-capture",
                    "schema_version": 1,
                    "content_kind": "processed_spectrum",
                    "raw_iq": False,
                    "started_at": started_at.isoformat(),
                },
                sync=True,
            )
        except SpectrumCaptureError:
            self.abort()
            raise
        return self.status()

    def append(self, frame: SpectrumFrame) -> None:
        if not self.active:
            raise SpectrumCaptureError("spectrum capture is not active")
        record = {
            "type": "frame",
            "sequence": frame.sequence,
            "source_id": frame.source_id,
            "captured_at": frame.captured_at.isoformat(),
            "center_frequency_hz": frame.center_frequency_hz,
            "span_hz": frame.span_hz,
            "unit": frame.unit,
            "calibration_id": frame.calibration_id,
            "uncertainty_db": frame.uncertainty_db,
            "provenance": frame.provenance.value,
            "dropped_frames": frame.dropped_frames,
            "data_age_ms": frame.data_age_ms,
            "power": [float(value) for value in frame.power_dbm],
        }
        self._write_record(record, sync=(self._frames + 1) % 8 == 0)
        self._frames += 1
        self._dropped_frames += max(0, frame.dropped_frames)

    def stop(self) -> SpectrumCaptureResult:
        handle = self._handle
        partial_path = self._partial_path
        started_at = self._started_at
        if handle is None or partial_path is None or started_at is None:
            raise SpectrumCaptureError("spectrum capture is not active")
        stopped_at = self._clock()
        try:
            self._write_record(
                {
                    "type": "footer",
                    "frames": self._frames,
                    "dropped_frames": self._dropped_frames,
                    "stopped_at": stopped_at.isoformat(),
                },
                sync=True,
            )
            handle.close()
            self._handle = None
            final_path = partial_path.with_suffix("")
            os.replace(partial_path, final_path)
            digest = self._digest.hexdigest()
            _write_checksum(final_path, digest)
        except OSError as exc:
            self.abort()
            raise SpectrumCaptureError(f"cannot finalize capture: {exc}") from exc

        result = SpectrumCaptureResult(
            path=final_path,
            sha256=digest,
            frames=self._frames,
            bytes_written=self._bytes_written,
            dropped_frames=self._dropped_frames,
            started_at=started_at,
            stopped_at=stopped_at,
        )
        self._last_result = result
        self._partial_path = None
        self._started_at = None
        return result

    def abort(self) -> Path | None:
        """Close the writer but retain its `.partial` evidence for recovery."""

        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
            with suppress(OSError):
                handle.close()
        path = self._partial_path
        self._partial_path = None
        self._started_at = None
        return path

    def status(self) -> SpectrumCaptureStatus:
        now = self._clock()
        started_at = self._started_at
        elapsed = (
            max(0.0, (now - started_at).total_seconds())
            if started_at is not None
            else 0.0
        )
        completed = self._last_result.path if self._last_result is not None else None
        return SpectrumCaptureStatus(
            active=self.active,
            path=self._partial_path,
            completed_path=completed,
            started_at=started_at,
            elapsed_seconds=elapsed,
            frames=self._frames if self.active else 0,
            bytes_written=self._bytes_written if self.active else 0,
            bytes_per_second=(self._bytes_written / elapsed if elapsed > 0 else 0.0),
            dropped_frames=self._dropped_frames if self.active else 0,
        )

    def _write_record(self, payload: dict[str, object], *, sync: bool) -> None:
        handle = self._handle
        if handle is None:
            raise SpectrumCaptureError("spectrum capture is not active")
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        try:
            handle.write(encoded)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        except (OSError, ValueError) as exc:
            raise SpectrumCaptureError(f"cannot write capture frame: {exc}") from exc
        self._digest.update(encoded)
        self._bytes_written += len(encoded)


def _write_checksum(path: Path, digest: str) -> None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    temporary = checksum_path.with_name(f".{checksum_path.name}.{uuid4().hex}.tmp")
    payload = f"{digest}  {path.name}\n"
    with temporary.open("x", encoding="ascii", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, checksum_path)


__all__ = [
    "SpectrumCaptureError",
    "SpectrumCaptureResult",
    "SpectrumCaptureStatus",
    "SpectrumCaptureWriter",
]
