from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .models import GeoPoint, GpsFixDimension, LocationFix, LocationSource

_MAX_SENTENCE_LENGTH = 256
_KNOT_TO_METRES_PER_SECOND = 0.5144444444444445


class NmeaError(ValueError):
    """Base class for rejected NMEA input."""


class NmeaChecksumError(NmeaError):
    pass


class NmeaParseError(NmeaError):
    pass


class NmeaNoFixError(NmeaError):
    pass


@dataclass(slots=True, frozen=True)
class NmeaRecord:
    sentence_type: str
    fix: LocationFix | None
    fix_quality: int | None = None
    track_deg: float | None = None
    fix_dimension: GpsFixDimension = GpsFixDimension.UNKNOWN
    hdop: float | None = None
    vdop: float | None = None


def parse_nmea_sentence(
    sentence: str | bytes,
    *,
    received_at: datetime | None = None,
) -> NmeaRecord:
    """Parse one checksum-protected GGA, RMC or GSA sentence.

    The parser intentionally supports only the fields required for local
    positioning and rejects absent checksums and vendor payloads. GSA records
    carry the receiver's 2D/3D state without exposing coordinates.
    """

    text = _decode_sentence(sentence)
    payload = _validated_payload(text)
    fields = payload.split(",")
    if not fields or len(fields[0]) != 5:
        raise NmeaParseError("invalid NMEA talker/message identifier")
    message_type = fields[0][-3:].upper()
    received = _received_at(received_at)
    if message_type == "GGA":
        return _parse_gga(fields, received)
    if message_type == "RMC":
        return _parse_rmc(fields, received)
    if message_type == "GSA":
        return _parse_gsa(fields)
    raise NmeaParseError(f"unsupported NMEA sentence type: {message_type}")


def _decode_sentence(sentence: str | bytes) -> str:
    if isinstance(sentence, bytes):
        try:
            text = sentence.decode("ascii")
        except UnicodeDecodeError as exc:
            raise NmeaParseError("NMEA sentence must be ASCII") from exc
    elif isinstance(sentence, str):
        text = sentence
    else:
        raise TypeError("sentence must be str or bytes")
    text = text.strip()
    if not text or len(text) > _MAX_SENTENCE_LENGTH:
        raise NmeaParseError("NMEA sentence is empty or oversized")
    if any(ord(character) < 32 or ord(character) > 126 for character in text):
        raise NmeaParseError("NMEA sentence contains non-printable data")
    return text


def _validated_payload(text: str) -> str:
    if not text.startswith("$"):
        raise NmeaParseError("NMEA sentence must start with '$'")
    if text.count("*") != 1:
        raise NmeaChecksumError("NMEA checksum is required")
    payload, checksum_text = text[1:].split("*", 1)
    if len(checksum_text) != 2:
        raise NmeaChecksumError("NMEA checksum must contain two hexadecimal digits")
    try:
        expected = int(checksum_text, 16)
    except ValueError as exc:
        raise NmeaChecksumError("NMEA checksum is not hexadecimal") from exc
    actual = 0
    for character in payload:
        actual ^= ord(character)
    if actual != expected:
        raise NmeaChecksumError("NMEA checksum mismatch")
    return payload


def _parse_gga(fields: list[str], received_at: datetime) -> NmeaRecord:
    if len(fields) < 11:
        raise NmeaParseError("GGA sentence has too few fields")
    fix_quality = _integer(fields[6], "GGA fix quality")
    if fix_quality <= 0:
        raise NmeaNoFixError("GGA sentence reports no valid fix")
    point = GeoPoint(
        _coordinate(fields[2], fields[3], is_latitude=True),
        _coordinate(fields[4], fields[5], is_latitude=False),
        _optional_float(fields[9], "GGA altitude"),
    )
    captured_at = _nearest_datetime(
        _parse_time(fields[1]),
        received_at,
    )
    satellites = _integer(fields[7], "GGA satellite count")
    hdop = _optional_float(fields[8], "GGA HDOP")
    if hdop is None:
        raise NmeaParseError("GGA HDOP is required")
    fix = LocationFix(
        point=point,
        captured_at=captured_at,
        source=LocationSource.NMEA_GGA,
        hdop=hdop,
        satellites=satellites,
    )
    return NmeaRecord("GGA", fix, fix_quality=fix_quality)


def _parse_rmc(fields: list[str], received_at: datetime) -> NmeaRecord:
    if len(fields) < 10:
        raise NmeaParseError("RMC sentence has too few fields")
    if fields[2].upper() != "A":
        raise NmeaNoFixError("RMC sentence reports an invalid fix")
    point = GeoPoint(
        _coordinate(fields[3], fields[4], is_latitude=True),
        _coordinate(fields[5], fields[6], is_latitude=False),
    )
    captured_at = _parse_rmc_datetime(fields[1], fields[9], received_at)
    speed_knots = _optional_float(fields[7], "RMC speed")
    track = _optional_float(fields[8], "RMC track")
    if track is not None and not 0.0 <= track <= 360.0:
        raise NmeaParseError("RMC track is outside 0..360 degrees")
    fix = LocationFix(
        point=point,
        captured_at=captured_at,
        source=LocationSource.NMEA_RMC,
        speed_m_s=(
            speed_knots * _KNOT_TO_METRES_PER_SECOND
            if speed_knots is not None
            else None
        ),
    )
    return NmeaRecord("RMC", fix, track_deg=track)


def _parse_gsa(fields: list[str]) -> NmeaRecord:
    if len(fields) < 18:
        raise NmeaParseError("GSA sentence has too few fields")
    mode = _integer(fields[2], "GSA fix dimension")
    dimension = {
        1: GpsFixDimension.NONE,
        2: GpsFixDimension.TWO_D,
        3: GpsFixDimension.THREE_D,
    }.get(mode)
    if dimension is None:
        raise NmeaParseError("GSA fix dimension is outside 1..3")
    # NMEA 4.10 may append a system-id after VDOP, so DOP indices are fixed
    # rather than selected from the end of the record.
    hdop = _optional_float(fields[16], "GSA HDOP")
    vdop = _optional_float(fields[17], "GSA VDOP")
    return NmeaRecord(
        sentence_type="GSA",
        fix=None,
        fix_dimension=dimension,
        hdop=hdop,
        vdop=vdop,
    )


def _coordinate(value: str, hemisphere: str, *, is_latitude: bool) -> float:
    if not value:
        raise NmeaParseError("coordinate is missing")
    degree_digits = 2 if is_latitude else 3
    if len(value) <= degree_digits:
        raise NmeaParseError("coordinate has an invalid shape")
    try:
        degrees = int(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError as exc:
        raise NmeaParseError("coordinate is not numeric") from exc
    if not math.isfinite(minutes) or not 0.0 <= minutes < 60.0:
        raise NmeaParseError("coordinate minutes are outside 0..60")
    allowed = {"N", "S"} if is_latitude else {"E", "W"}
    normalized_hemisphere = hemisphere.upper()
    if normalized_hemisphere not in allowed:
        raise NmeaParseError("coordinate hemisphere is invalid")
    result = float(degrees) + minutes / 60.0
    if normalized_hemisphere in {"S", "W"}:
        result = -result
    limit = 90.0 if is_latitude else 180.0
    if not -limit <= result <= limit:
        raise NmeaParseError("coordinate is outside WGS84 bounds")
    return result


def _parse_time(value: str) -> time:
    if len(value) < 6:
        raise NmeaParseError("NMEA UTC time is missing or truncated")
    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        second_value = float(value[4:])
    except ValueError as exc:
        raise NmeaParseError("NMEA UTC time is invalid") from exc
    second = int(second_value)
    microsecond = round((second_value - second) * 1_000_000)
    if microsecond == 1_000_000:
        second += 1
        microsecond = 0
    try:
        return time(hour, minute, second, microsecond, tzinfo=UTC)
    except ValueError as exc:
        raise NmeaParseError("NMEA UTC time is outside valid bounds") from exc


def _parse_rmc_datetime(
    time_text: str,
    date_text: str,
    received_at: datetime,
) -> datetime:
    parsed_time = _parse_time(time_text)
    if len(date_text) != 6 or not date_text.isdigit():
        raise NmeaParseError("RMC date must use DDMMYY")
    day = int(date_text[0:2])
    month = int(date_text[2:4])
    short_year = int(date_text[4:6])
    candidates: list[datetime] = []
    for century in (1900, 2000, 2100):
        try:
            parsed_date = date(century + short_year, month, day)
        except ValueError:
            continue
        candidates.append(datetime.combine(parsed_date, parsed_time).astimezone(UTC))
    if not candidates:
        raise NmeaParseError("RMC date is outside valid bounds")
    return min(candidates, key=lambda candidate: abs(candidate - received_at))


def _nearest_datetime(parsed_time: time, received_at: datetime) -> datetime:
    base = datetime.combine(received_at.date(), parsed_time).astimezone(UTC)
    candidates = (base - timedelta(days=1), base, base + timedelta(days=1))
    return min(candidates, key=lambda candidate: abs(candidate - received_at))


def _optional_float(value: str, label: str) -> float | None:
    if value == "":
        return None
    try:
        converted = float(value)
    except ValueError as exc:
        raise NmeaParseError(f"{label} is not numeric") from exc
    if not math.isfinite(converted):
        raise NmeaParseError(f"{label} must be finite")
    return converted


def _integer(value: str, label: str) -> int:
    if not value:
        raise NmeaParseError(f"{label} is missing")
    try:
        return int(value)
    except ValueError as exc:
        raise NmeaParseError(f"{label} is not an integer") from exc


def _received_at(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    return result.astimezone(UTC)


__all__ = [
    "NmeaChecksumError",
    "NmeaError",
    "NmeaNoFixError",
    "NmeaParseError",
    "NmeaRecord",
    "parse_nmea_sentence",
]
