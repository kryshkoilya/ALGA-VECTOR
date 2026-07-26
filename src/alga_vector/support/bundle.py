from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alga_vector import __version__
from alga_vector.config.models import AppConfig
from alga_vector.domain.models import (
    DeviceSnapshot,
    Incident,
    SpectrumFrame,
    SystemSnapshot,
    utc_now,
)
from alga_vector.signal_analysis import RfDecision, SignalAssessment
from alga_vector.storage import JournalSummary

Clock = Callable[[], datetime]

_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[a-z]:\\(?:[^\\\r\n]+\\?)+")
_COORDINATE_PAIR_RE = re.compile(
    r"(?<![\d.])[-+]?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)"
    r"\s*[,;/]\s*"
    r"[-+]?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|(?:\d?\d)(?:\.\d+)?)"
    r"(?![\d.])"
)
_NMEA_RE = re.compile(r"(?m)\$[A-Z]{2}(?:GGA|RMC),[^\r\n]*")
_SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key", "authorization", "cookie")
_NETWORK_KEYS = ("connection", "hostname", "host", "ip", "ip_address", "mac", "endpoint")
_LOCATION_KEYS = ("coordinates", "latitude", "longitude", "gps", "location")
_PATH_KEYS = ("path", "data_dir", "directory", "capture_dir")


@dataclass(slots=True, frozen=True)
class SupportBundleResult:
    path: Path
    manifest: dict[str, Any]
    size_bytes: int


class SupportBundleBuilder:
    """Builds a local archive from an explicit allowlist of redacted data."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        salt_factory: Callable[[int], bytes] = os.urandom,
        maximum_log_bytes: int = 256 * 1024,
        maximum_log_files: int = 5,
    ) -> None:
        if maximum_log_bytes <= 0 or maximum_log_files < 0:
            raise ValueError("invalid support log limits")
        self._clock = clock
        self._salt_factory = salt_factory
        self.maximum_log_bytes = maximum_log_bytes
        self.maximum_log_files = maximum_log_files

    def build(
        self,
        destination: Path,
        *,
        config: AppConfig,
        snapshot: SystemSnapshot,
        journal_summary: JournalSummary | None = None,
        log_files: Iterable[Path] = (),
        build_id: str | None = None,
    ) -> SupportBundleResult:
        """Create an archive locally. Nothing is uploaded or transmitted."""

        destination = destination.with_suffix(".avsupport")
        destination.parent.mkdir(parents=True, exist_ok=True)
        generated_at = self._clock().astimezone(UTC)
        salt = self._salt_factory(32)
        if len(salt) < 16:
            raise ValueError("support bundle salt must contain at least 16 bytes")

        files: dict[str, bytes] = {
            "build/version_and_runtime.json": _json_bytes(
                {
                    "application": "ALGA VECTOR",
                    "build_id": build_id or __version__,
                    "python": platform.python_version(),
                    "implementation": platform.python_implementation(),
                    "os": platform.system(),
                    "os_release": platform.release(),
                    "architecture": platform.machine(),
                }
            ),
            "config/effective_config_redacted.json": _json_bytes(
                _redact(
                    config.model_dump(mode="json"),
                    salt=salt,
                    path=("config",),
                )
            ),
            "devices/inventory_redacted.json": _json_bytes(
                {
                    "devices": [
                        _device_payload(device, salt=salt) for device in snapshot.devices
                    ]
                }
            ),
            "health/latest_snapshot.json": _json_bytes(
                _snapshot_payload(snapshot, salt=salt)
            ),
            "events/summary.json": _json_bytes(
                _events_payload(snapshot.incidents, journal_summary)
            ),
        }
        for index, log_path in enumerate(tuple(log_files)[: self.maximum_log_files], start=1):
            redacted_log = self._read_redacted_log(log_path, salt=salt)
            if redacted_log is not None:
                files[f"logs/recent/log-{index:02d}.jsonl"] = redacted_log

        manifest_files = [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(files.items())
        ]
        manifest: dict[str, Any] = {
            "format": "alga-vector-support",
            "format_version": 1,
            "generated_at": generated_at.isoformat(),
            "build_id": build_id or __version__,
            "local_only": True,
            "contains_raw_iq": False,
            "redaction": {
                "device_identifiers": "per-bundle salted SHA-256",
                "network_identifiers": "removed",
                "paths_and_users": "removed",
                "precise_locations": "removed",
                "secrets": "removed",
            },
            "files": manifest_files,
        }
        files["manifest.json"] = _json_bytes(manifest)

        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for name, content in sorted(files.items()):
                    info = zipfile.ZipInfo(name, date_time=_zip_timestamp(generated_at))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, content)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return SupportBundleResult(
            path=destination,
            manifest=manifest,
            size_bytes=destination.stat().st_size,
        )

    def _read_redacted_log(self, path: Path, *, salt: bytes) -> bytes | None:
        if path.suffix.lower() != ".jsonl" or not path.is_file():
            return None
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size > self.maximum_log_bytes:
                handle.seek(size - self.maximum_log_bytes)
                handle.readline()
            raw = handle.read(self.maximum_log_bytes)
        output: list[str] = []
        for raw_line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                output.append(_redact_text(raw_line))
                continue
            output.append(
                json.dumps(
                    _redact(parsed, salt=salt, path=("logs",)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        if not output:
            return None
        return ("\n".join(output) + "\n").encode("utf-8")


def verify_support_bundle(path: Path) -> bool:
    """Verify every manifest payload hash without extracting the archive."""

    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            entries = manifest.get("files")
            if not isinstance(entries, list):
                return False
            for entry in entries:
                if not isinstance(entry, dict):
                    return False
                name = entry.get("path")
                expected_hash = entry.get("sha256")
                expected_size = entry.get("size")
                if not isinstance(name, str) or name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    return False
                payload = archive.read(name)
                if len(payload) != expected_size:
                    return False
                if hashlib.sha256(payload).hexdigest() != expected_hash:
                    return False
            return True
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        return False


def _device_payload(device: DeviceSnapshot, *, salt: bytes) -> dict[str, Any]:
    return {
        "device_id": _pseudonym(device.device_id, salt),
        "display_name": device.display_name,
        "kind": device.kind,
        "connection": "[REDACTED_CONNECTION]",
        "state": device.state.value,
        "health": device.health.value,
        "capabilities": sorted(capability.value for capability in device.capabilities),
        "driver": device.driver,
        "sample_rate_hz": device.sample_rate_hz,
        "center_frequency_hz": device.center_frequency_hz,
        "last_data_at": device.last_data_at.isoformat() if device.last_data_at else None,
        "reason_code": device.reason_code,
        "reason_ru": _redact_text(device.reason_ru) if device.reason_ru else None,
        "generation": device.generation,
        "metrics": _redact(device.metrics, salt=salt, path=("devices", "metrics")),
    }


def _snapshot_payload(snapshot: SystemSnapshot, *, salt: bytes) -> dict[str, Any]:
    spectrum = _spectrum_payload(snapshot.spectrum, salt=salt)
    return {
        "revision": snapshot.revision,
        "captured_at": snapshot.captured_at.isoformat(),
        "mode": snapshot.mode.value,
        "runtime_mode": snapshot.runtime_mode,
        "profile_name": _redact_text(snapshot.profile_name),
        "readiness_percent": snapshot.readiness_percent,
        "devices": [_device_payload(device, salt=salt) for device in snapshot.devices],
        "capabilities": [
            {
                "capability": item.capability.value,
                "state": item.state.value,
                "reason_code": item.reason_code,
                "explanation_ru": (
                    _redact_text(item.explanation_ru) if item.explanation_ru else None
                ),
                "action_ru": _redact_text(item.action_ru) if item.action_ru else None,
            }
            for item in snapshot.capabilities
        ],
        "incidents": [
            _incident_payload(incident, salt=salt) for incident in snapshot.incidents
        ],
        "spectrum": spectrum,
        "signal_assessment": _signal_assessment_payload(
            snapshot.signal_assessment,
            salt=salt,
        ),
        "signal_decision": _rf_decision_payload(
            snapshot.signal_decision,
            salt=salt,
        ),
        "signal_events": [
            _rf_decision_payload(item, salt=salt)
            for item in snapshot.signal_events
            if isinstance(item, RfDecision)
        ],
        "direction": _direction_payload(snapshot.direction, salt=salt),
    }


def _signal_assessment_payload(
    assessment: SignalAssessment | None,
    *,
    salt: bytes,
) -> dict[str, Any] | None:
    if assessment is None:
        return None
    evidence = assessment.evidence
    return {
        "state": assessment.state.value,
        "trust": assessment.trust.value,
        "reason_code": assessment.reason_code,
        "headline_ru": _redact_text(assessment.headline_ru),
        "explanation_ru": _redact_text(assessment.explanation_ru),
        "operator_action_ru": _redact_text(assessment.operator_action_ru),
        "source_id": (
            _pseudonym(assessment.source_id, salt)
            if assessment.source_id is not None
            else None
        ),
        "sequence": assessment.sequence,
        "observed_at": assessment.observed_at.isoformat(),
        "quality_flags": sorted(item.value for item in assessment.quality_flags),
        "attribution": assessment.attribution.value,
        "identity_established": assessment.identity_established,
        "evidence": {
            "coverage_low_hz": evidence.coverage_low_hz,
            "coverage_high_hz": evidence.coverage_high_hz,
            "peak_frequency_hz": evidence.peak_frequency_hz,
            "occupied_bandwidth_hz": evidence.occupied_bandwidth_hz,
            "peak_excess_over_floor_db": evidence.peak_excess_over_floor_db,
            "active_fraction": evidence.active_fraction,
            "persistence_frames": evidence.persistence_frames,
            "baseline_frames": evidence.baseline_frames,
            "baseline_required_frames": evidence.baseline_required_frames,
            "data_age_ms": evidence.data_age_ms,
            "power_unit": evidence.power_unit,
        },
    }


def _rf_decision_payload(
    decision: RfDecision | None,
    *,
    salt: bytes,
) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "source_id": _pseudonym(decision.source_id, salt),
        "observed_at": decision.observed_at.isoformat(),
        "lifecycle": decision.lifecycle.value,
        "family": decision.family.value,
        "family_explanation_ru": _redact_text(decision.family_explanation_ru),
        "episode_id": (
            _pseudonym(decision.episode_id, salt)
            if decision.episode_id is not None
            else None
        ),
        "started_at": (
            decision.started_at.isoformat()
            if decision.started_at is not None
            else None
        ),
        "last_active_at": (
            decision.last_active_at.isoformat()
            if decision.last_active_at is not None
            else None
        ),
        "peak_frequency_hz": decision.peak_frequency_hz,
        "occupied_bandwidth_hz": decision.occupied_bandwidth_hz,
        "heuristic_score": decision.heuristic_score,
        "calibrated_probability": decision.calibrated_probability,
        "evidence_strength": decision.evidence_strength.value,
        "data_quality": decision.data_quality.value,
        "alertable": decision.alertable,
        "abstained": decision.abstained,
        "attribution": decision.attribution.value,
        "identity_established": decision.identity_established,
        "supporting_evidence": [
            _decision_evidence_payload(item, salt=salt)
            for item in decision.supporting_evidence
        ],
        "contradicting_evidence": [
            _decision_evidence_payload(item, salt=salt)
            for item in decision.contradicting_evidence
        ],
        "missing_confirmation": [
            _decision_evidence_payload(item, salt=salt)
            for item in decision.missing_confirmation
        ],
        "sensor_contributions": [
            {
                "source_id": _pseudonym(item.source_id, salt),
                "contribution": item.contribution,
                "data_quality": item.data_quality.value,
                "independent_confirmation": item.independent_confirmation,
                "explanation_ru": _redact_text(item.explanation_ru),
            }
            for item in decision.sensor_contributions
        ],
        "alternatives": [
            {
                "family": item.family.value,
                "explanation_ru": _redact_text(item.explanation_ru),
            }
            for item in decision.alternatives
        ],
        "limitations": [
            _decision_evidence_payload(item, salt=salt)
            for item in decision.limitations
        ],
    }


def _direction_payload(
    direction: object | None,
    *,
    salt: bytes,
) -> dict[str, Any] | None:
    """Include diagnostic validity without exporting the observed bearing."""

    if direction is None:
        return None
    current = getattr(direction, "current", None)
    if current is None:
        return None
    source = getattr(current, "source", "unavailable")
    quality = getattr(current, "quality", "unavailable")
    source_value = str(getattr(source, "value", source))
    quality_value = str(getattr(quality, "value", quality))
    source_id = str(getattr(current, "source_id", "") or "")
    captured_at = getattr(current, "captured_at", None)
    last_valid_at = getattr(direction, "last_valid_at", None)
    return {
        "available": bool(getattr(direction, "available", False)),
        "measured": bool(getattr(current, "measured", False)),
        "source": source_value,
        "source_id": _pseudonym(source_id, salt) if source_id else None,
        "quality": quality_value,
        "reason_code": str(getattr(current, "reason_code", "")),
        "message_ru": _redact_text(str(getattr(current, "message_ru", ""))),
        "confidence": getattr(current, "confidence", None),
        "uncertainty_deg": getattr(current, "uncertainty_deg", None),
        "bearing_exported": False,
        "captured_at": (
            captured_at.isoformat()
            if isinstance(captured_at, datetime)
            else None
        ),
        "stale": bool(getattr(direction, "stale", False)),
        "age_s": getattr(direction, "age_s", None),
        "last_valid_at": (
            last_valid_at.isoformat()
            if isinstance(last_valid_at, datetime)
            else None
        ),
    }


def _decision_evidence_payload(
    item: object,
    *,
    salt: bytes,
) -> dict[str, Any]:
    return {
        "code": str(getattr(item, "code", "")),
        "explanation_ru": _redact_text(
            str(getattr(item, "explanation_ru", ""))
        ),
        "measured": _redact(
            getattr(item, "measured", None),
            salt=salt,
            path=("rf_decision", "evidence", "measured"),
        ),
        "threshold": _redact(
            getattr(item, "threshold", None),
            salt=salt,
            path=("rf_decision", "evidence", "threshold"),
        ),
    }


def _spectrum_payload(frame: SpectrumFrame | None, *, salt: bytes) -> dict[str, Any] | None:
    if frame is None:
        return None
    return {
        "source_id": _pseudonym(frame.source_id, salt),
        "sequence": frame.sequence,
        "center_frequency_hz": frame.center_frequency_hz,
        "span_hz": frame.span_hz,
        "captured_at": frame.captured_at.isoformat(),
        "provenance": frame.provenance.value,
        "bins": int(frame.power_dbm.size),
        "peak_dbm": frame.peak_dbm,
        "power_unit": frame.unit,
        "calibration_id": frame.calibration_id,
        "uncertainty_db": frame.uncertainty_db,
        "dropped_frames": frame.dropped_frames,
        "data_age_ms": frame.data_age_ms,
        "raw_power_data_included": False,
    }


def _incident_payload(incident: Incident, *, salt: bytes) -> dict[str, Any]:
    return {
        "incident_id": _pseudonym(incident.incident_id, salt),
        "code": incident.code,
        "title_ru": _redact_text(incident.title_ru),
        "message_ru": _redact_text(incident.message_ru),
        "action_ru": _redact_text(incident.action_ru),
        "severity": incident.severity.value,
        "source": _pseudonym(incident.source, salt),
        "occurred_at": incident.occurred_at.isoformat(),
        "acknowledged": incident.acknowledged,
        "technical": _redact(incident.technical, salt=salt, path=("incidents", "technical")),
    }


def _events_payload(
    incidents: Iterable[Incident],
    summary: JournalSummary | None,
) -> dict[str, Any]:
    incident_tuple = tuple(incidents)
    if summary is not None:
        return {
            "total": summary.total,
            "unacknowledged": summary.unacknowledged,
            "by_severity": summary.by_severity,
            "current_snapshot_count": len(incident_tuple),
        }
    severity = Counter(incident.severity.value for incident in incident_tuple)
    return {
        "total": len(incident_tuple),
        "unacknowledged": sum(not incident.acknowledged for incident in incident_tuple),
        "by_severity": dict(sorted(severity.items())),
        "current_snapshot_count": len(incident_tuple),
    }


def _redact(value: Any, *, salt: bytes, path: tuple[str, ...]) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Path):
        return "[REDACTED_PATH]"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            key_lower = key.lower()
            child_path = (*path, key_lower)
            if any(marker in key_lower for marker in _SECRET_KEYS):
                output[key] = "[REDACTED_SECRET]"
            elif key_lower == "device_id" or (
                key_lower == "id" and any(segment in {"adapters", "devices"} for segment in path)
            ):
                output[key] = _pseudonym(str(item), salt)
            elif any(marker == key_lower or key_lower.endswith(f"_{marker}") for marker in _NETWORK_KEYS):
                output[key] = "[REDACTED_NETWORK]"
            elif any(marker in key_lower for marker in _LOCATION_KEYS):
                output[key] = "[REDACTED_LOCATION]"
            elif any(marker in key_lower for marker in _PATH_KEYS):
                output[key] = "[REDACTED_PATH]"
            else:
                output[key] = _redact(item, salt=salt, path=child_path)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, salt=salt, path=path) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _redact(model_dump(mode="json"), salt=salt, path=path)
    return _redact_text(str(value))


def _redact_text(value: str) -> str:
    redacted = _IPV4_RE.sub("[REDACTED_IP]", value)
    redacted = _MAC_RE.sub("[REDACTED_MAC]", redacted)
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = _NMEA_RE.sub("[REDACTED_NMEA]", redacted)
    return _COORDINATE_PAIR_RE.sub("[REDACTED_LOCATION]", redacted)


def _pseudonym(value: str, salt: bytes) -> str:
    digest = hashlib.sha256(salt + value.encode("utf-8", errors="replace")).hexdigest()
    return f"anon-{digest[:16]}"


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _zip_timestamp(value: datetime) -> tuple[int, int, int, int, int, int]:
    safe = value.astimezone(UTC)
    year = max(1980, safe.year)
    return (year, safe.month, safe.day, safe.hour, safe.minute, safe.second)
