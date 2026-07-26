"""Bounded parser for local dump1090/FlightAware ``aircraft.json`` data."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from .models import (
    AirspaceDataQuality,
    AirspaceParseIssue,
    CivilAircraftContext,
    ParsedCivilAirspacePayload,
)

DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RECORDS = 20_000
_MINIMUM_SOURCE_TIME = datetime(2000, 1, 1, tzinfo=UTC)
_MAXIMUM_SOURCE_TIME = datetime(2100, 1, 1, tzinfo=UTC)
_HEX_PATTERN = re.compile(r"~?[0-9a-fA-F]{6}")


class AirspacePayloadError(ValueError):
    """Root-level payload error safe to surface without raw input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _RecordError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _decode_root(
    payload: str | bytes | bytearray | Mapping[str, object],
    *,
    max_payload_bytes: int,
) -> Mapping[str, object]:
    if max_payload_bytes < 1:
        raise ValueError("max_payload_bytes must be positive")
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
    else:
        raise TypeError("payload must be JSON text, bytes, or a mapping")
    if len(encoded) > max_payload_bytes:
        raise AirspacePayloadError(
            "AIRSPACE.PAYLOAD_TOO_LARGE",
            "Local aircraft payload exceeds the configured size limit.",
        )
    try:
        decoded = cast(object, json.loads(encoded.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_JSON",
            "Local aircraft payload is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(decoded, dict):
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_ROOT",
            "Local aircraft payload root must be an object.",
        )
    return cast(Mapping[str, object], decoded)


def _source_time(
    root: Mapping[str, object],
    received_at: datetime,
) -> tuple[datetime, bool, AirspaceParseIssue | None]:
    raw = root.get("now")
    if raw is None:
        return (
            received_at,
            False,
            AirspaceParseIssue(
                code="AIRSPACE.SOURCE_TIME_MISSING",
                message="Source timestamp is missing; receive time is used.",
                field="now",
            ),
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_SOURCE_TIME",
            "Source timestamp must be a Unix timestamp.",
        )
    timestamp = float(raw)
    if not math.isfinite(timestamp):
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_SOURCE_TIME",
            "Source timestamp must be finite.",
        )
    try:
        generated_at = datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_SOURCE_TIME",
            "Source timestamp is outside the supported range.",
        ) from exc
    if not _MINIMUM_SOURCE_TIME <= generated_at < _MAXIMUM_SOURCE_TIME:
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_SOURCE_TIME",
            "Source timestamp is outside the supported 2000..2100 range.",
        )
    return generated_at, True, None


def _required_number(
    value: object,
    field: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _RecordError(field, f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise _RecordError(field, f"{field} is outside the supported range")
    return number


def _optional_number(
    record: Mapping[str, object],
    keys: tuple[str, ...],
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    for key in keys:
        if key not in record or record[key] is None:
            continue
        return _required_number(
            record[key],
            key,
            minimum=minimum,
            maximum=maximum,
        )
    return None


def _normalized_hex(record: Mapping[str, object]) -> str:
    raw = record.get("hex")
    if not isinstance(raw, str) or _HEX_PATTERN.fullmatch(raw.strip()) is None:
        raise _RecordError("hex", "hex must be a six-digit broadcast address")
    return raw.strip().lower()


def _pseudonymous_identifier(hex_address: str) -> str:
    digest = hashlib.blake2s(
        hex_address.encode("ascii"),
        digest_size=6,
        person=b"ALGAAIR",
    ).hexdigest()
    return f"ac-{digest}"


def _callsign(record: Mapping[str, object]) -> str | None:
    raw = record.get("flight", record.get("callsign"))
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _RecordError("flight", "callsign must be text")
    normalized = " ".join(raw.strip().upper().split())
    if not normalized:
        return None
    if len(normalized) > 16 or not normalized.isascii():
        raise _RecordError("flight", "callsign must be 1..16 ASCII characters")
    if any(not (character.isalnum() or character in " ._-") for character in normalized):
        raise _RecordError("flight", "callsign contains unsupported characters")
    return normalized


def _altitude(record: Mapping[str, object]) -> tuple[float | None, bool | None]:
    raw = record.get("alt_baro")
    if raw is None:
        raw = record.get("alt_geom")
    if raw is None:
        return None, None
    if isinstance(raw, str):
        if raw.strip().lower() == "ground":
            return None, True
        raise _RecordError("alt_baro", "altitude text must be 'ground'")
    altitude = _required_number(
        raw,
        "alt_baro",
        minimum=-2_000.0,
        maximum=100_000.0,
    )
    return altitude, False


def _record_quality(
    callsign: str | None,
    altitude_ft: float | None,
    on_ground: bool | None,
    ground_speed_kt: float | None,
    track_deg: float | None,
) -> AirspaceDataQuality:
    facts = sum(
        value is not None
        for value in (callsign, altitude_ft, on_ground, ground_speed_kt, track_deg)
    )
    if facts >= 4:
        return AirspaceDataQuality.GOOD
    if facts >= 2:
        return AirspaceDataQuality.PARTIAL
    return AirspaceDataQuality.LIMITED


def _parse_record(
    raw: object,
    *,
    generated_at: datetime,
) -> CivilAircraftContext:
    if not isinstance(raw, dict):
        raise _RecordError("record", "aircraft record must be an object")
    record = cast(Mapping[str, object], raw)
    hex_address = _normalized_hex(record)
    callsign = _callsign(record)
    altitude_ft, on_ground = _altitude(record)
    ground_speed_kt = _optional_number(
        record,
        ("gs", "ground_speed"),
        minimum=0.0,
        maximum=2_000.0,
    )
    track_deg = _optional_number(
        record,
        ("track",),
        minimum=0.0,
        maximum=360.0,
    )
    if track_deg == 360.0:
        track_deg = 0.0
    if "seen" not in record:
        raise _RecordError("seen", "seen is required")
    seen_s = _required_number(
        record["seen"],
        "seen",
        minimum=0.0,
        maximum=604_800.0,
    )
    return CivilAircraftContext(
        hex=hex_address,
        pseudonymous_id=_pseudonymous_identifier(hex_address),
        callsign=callsign,
        altitude_ft=altitude_ft,
        on_ground=on_ground,
        ground_speed_kt=ground_speed_kt,
        track_deg=track_deg,
        seen_s=seen_s,
        observed_at=generated_at - timedelta(seconds=seen_s),
        data_quality=_record_quality(
            callsign,
            altitude_ft,
            on_ground,
            ground_speed_kt,
            track_deg,
        ),
    )


def parse_dump1090_aircraft_json(
    payload: str | bytes | bytearray | Mapping[str, object],
    *,
    received_at: datetime,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> ParsedCivilAirspacePayload:
    """Parse local ``aircraft.json`` while isolating malformed records."""

    _require_aware(received_at, "received_at")
    if max_records < 1:
        raise ValueError("max_records must be positive")
    root = _decode_root(payload, max_payload_bytes=max_payload_bytes)
    generated_at, timestamp_from_payload, timestamp_issue = _source_time(root, received_at)
    raw_aircraft = root.get("aircraft")
    if not isinstance(raw_aircraft, list):
        raise AirspacePayloadError(
            "AIRSPACE.INVALID_AIRCRAFT_LIST",
            "Local aircraft payload must contain an aircraft array.",
        )
    if len(raw_aircraft) > max_records:
        raise AirspacePayloadError(
            "AIRSPACE.TOO_MANY_RECORDS",
            "Local aircraft payload exceeds the configured record limit.",
        )

    issues: list[AirspaceParseIssue] = []
    if timestamp_issue is not None:
        issues.append(timestamp_issue)
    unique: dict[str, tuple[int, CivilAircraftContext]] = {}
    for index, raw_record in enumerate(raw_aircraft):
        try:
            aircraft = _parse_record(raw_record, generated_at=generated_at)
        except _RecordError as exc:
            issues.append(
                AirspaceParseIssue(
                    code="AIRSPACE.MALFORMED_RECORD",
                    message=str(exc),
                    record_index=index,
                    field=exc.field,
                )
            )
            continue
        prior = unique.get(aircraft.hex)
        if prior is None:
            unique[aircraft.hex] = (index, aircraft)
            continue
        prior_index, prior_aircraft = prior
        if aircraft.seen_s < prior_aircraft.seen_s:
            dropped_index = prior_index
            unique[aircraft.hex] = (index, aircraft)
        else:
            dropped_index = index
        issues.append(
            AirspaceParseIssue(
                code="AIRSPACE.DUPLICATE_RECORD",
                message="Duplicate broadcast address was reduced to the freshest record.",
                record_index=dropped_index,
                field="hex",
            )
        )

    parsed_aircraft = tuple(
        item[1]
        for item in sorted(
            unique.values(),
            key=lambda indexed: indexed[1].hex,
        )
    )
    return ParsedCivilAirspacePayload(
        generated_at=generated_at,
        received_at=received_at,
        timestamp_from_payload=timestamp_from_payload,
        aircraft=parsed_aircraft,
        issues=tuple(issues),
        total_record_count=len(raw_aircraft),
    )


def load_dump1090_aircraft_file(
    path: str | os.PathLike[str],
    *,
    received_at: datetime,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> ParsedCivilAirspacePayload:
    """Read one bounded local file; URLs and implicit network fetching are unsupported."""

    _require_aware(received_at, "received_at")
    if max_payload_bytes < 1:
        raise ValueError("max_payload_bytes must be positive")
    raw_path = os.fspath(path)
    if "://" in raw_path:
        raise AirspacePayloadError(
            "AIRSPACE.NON_LOCAL_SOURCE",
            "Only a local filesystem path is accepted.",
        )
    file_path = Path(raw_path)
    try:
        if not file_path.is_file():
            raise AirspacePayloadError(
                "AIRSPACE.FILE_NOT_FOUND",
                "Local aircraft file does not exist or is not a regular file.",
            )
        if file_path.stat().st_size > max_payload_bytes:
            raise AirspacePayloadError(
                "AIRSPACE.PAYLOAD_TOO_LARGE",
                "Local aircraft file exceeds the configured size limit.",
            )
        with file_path.open("rb") as stream:
            payload = stream.read(max_payload_bytes + 1)
    except AirspacePayloadError:
        raise
    except OSError as exc:
        raise AirspacePayloadError(
            "AIRSPACE.FILE_IO",
            "Local aircraft file could not be read.",
        ) from exc
    if len(payload) > max_payload_bytes:
        raise AirspacePayloadError(
            "AIRSPACE.PAYLOAD_TOO_LARGE",
            "Local aircraft file changed and exceeded the configured size limit.",
        )
    return parse_dump1090_aircraft_json(
        payload,
        received_at=received_at,
        max_payload_bytes=max_payload_bytes,
        max_records=max_records,
    )


parse_aircraft_json = parse_dump1090_aircraft_json
load_aircraft_json = load_dump1090_aircraft_file


__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_RECORDS",
    "AirspacePayloadError",
    "load_aircraft_json",
    "load_dump1090_aircraft_file",
    "parse_aircraft_json",
    "parse_dump1090_aircraft_json",
]
