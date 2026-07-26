from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from alga_vector.location import (
    GeoPoint,
    GpsFixDimension,
    GpsFixState,
    GpsPortConfidence,
    LocationFix,
    LocationPolicy,
    LocationService,
    LocationSource,
    LocationStatus,
    NmeaChecksumError,
    NmeaNoFixError,
    NmeaSerialReceiver,
    SecureLocationStore,
    SecureStoreCorruptError,
    discover_nmea_port_candidates,
    parse_nmea_sentence,
)

# Deliberately synthetic test-only coordinate; it does not represent a deployed site.
_SYNTHETIC_LATITUDE_DEG = 12.3456
_SYNTHETIC_LONGITUDE_DEG = 65.4321
_SYNTHETIC_NMEA_LATITUDE = "1220.7360"
_SYNTHETIC_NMEA_LONGITUDE = "06525.9260"


def _nmea(payload: str) -> str:
    checksum = 0
    for character in payload:
        checksum ^= ord(character)
    return f"${payload}*{checksum:02X}"


def _fix(
    point: GeoPoint,
    captured_at: datetime,
    *,
    hdop: float = 0.9,
    satellites: int = 9,
) -> LocationFix:
    return LocationFix(
        point,
        captured_at,
        LocationSource.NMEA_GGA,
        hdop=hdop,
        satellites=satellites,
    )


class _TestProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"test-tag:" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"test-tag:"):
            raise SecureStoreCorruptError("authentication failed")
        return bytes(value ^ 0xA5 for value in ciphertext[len(b"test-tag:") :])


def test_geo_point_is_strict_and_redacts_repr() -> None:
    point = GeoPoint(_SYNTHETIC_LATITUDE_DEG, _SYNTHETIC_LONGITUDE_DEG, 156.0)
    rendered = repr(point)
    assert "12.3456" not in rendered
    assert "65.4321" not in rendered
    assert "redacted" in rendered

    for latitude in (float("nan"), float("inf"), 91.0):
        with pytest.raises(ValueError):
            GeoPoint(latitude, 0.0)
    with pytest.raises(ValueError):
        GeoPoint(0.0, -181.0)
    with pytest.raises(TypeError):
        GeoPoint("55.7", 37.6)  # type: ignore[arg-type]


def test_parse_checksum_protected_gga_and_rmc() -> None:
    received = datetime(2026, 7, 25, 12, 36, tzinfo=UTC)
    gga = _nmea(
        f"GNGGA,123519.00,{_SYNTHETIC_NMEA_LATITUDE},N,"
        f"{_SYNTHETIC_NMEA_LONGITUDE},E,1,10,0.8,156.2,M,0.0,M,,"
    )
    record = parse_nmea_sentence(gga, received_at=received)
    assert record.sentence_type == "GGA"
    assert record.fix.source is LocationSource.NMEA_GGA
    assert record.fix.point.latitude_deg == pytest.approx(
        _SYNTHETIC_LATITUDE_DEG,
        abs=1e-5,
    )
    assert record.fix.point.longitude_deg == pytest.approx(
        _SYNTHETIC_LONGITUDE_DEG,
        abs=1e-5,
    )
    assert record.fix.hdop == 0.8
    assert record.fix.satellites == 10

    rmc = _nmea(
        f"GNRMC,123520.00,A,{_SYNTHETIC_NMEA_LATITUDE},N,"
        f"{_SYNTHETIC_NMEA_LONGITUDE},E,0.10,84.4,250726,,,A"
    )
    rmc_record = parse_nmea_sentence(rmc, received_at=received)
    assert rmc_record.sentence_type == "RMC"
    assert rmc_record.fix.captured_at == datetime(2026, 7, 25, 12, 35, 20, tzinfo=UTC)
    assert rmc_record.fix.speed_m_s == pytest.approx(0.0514444)
    assert rmc_record.track_deg == 84.4


def test_parse_gsa_reports_2d_and_3d_without_coordinates() -> None:
    satellites = ["01", "02", "03", "04", "", "", "", "", "", "", "", ""]
    record_2d = parse_nmea_sentence(
        _nmea(",".join(["GNGSA", "A", "2", *satellites, "1.4", "0.9", "1.1"]))
    )
    assert record_2d.sentence_type == "GSA"
    assert record_2d.fix is None
    assert record_2d.fix_dimension is GpsFixDimension.TWO_D
    assert record_2d.hdop == 0.9

    record_3d = parse_nmea_sentence(
        _nmea(",".join(["GNGSA", "A", "3", *satellites, "1.2", "0.7", "0.9"]))
    )
    assert record_3d.fix_dimension is GpsFixDimension.THREE_D


def test_nmea_rejects_missing_bad_checksum_and_no_fix() -> None:
    with pytest.raises(NmeaChecksumError):
        parse_nmea_sentence("$GNGGA,123519.00,,,,,0,00,99.9,,,,,,")
    with pytest.raises(NmeaChecksumError):
        parse_nmea_sentence("$GNGGA,123519.00,,,,,0,00,99.9,,,,,,*00")
    with pytest.raises(NmeaNoFixError):
        parse_nmea_sentence(_nmea("GNRMC,123520.00,V,,,,,,,250726,,,N"))


def test_location_service_requires_quality_stationarity_and_cross_check() -> None:
    started = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    policy = LocationPolicy(
        minimum_samples=3,
        maximum_fix_age_s=5.0,
        sample_window_s=30.0,
        maximum_stationary_radius_m=20.0,
        manual_conflict_distance_m=50.0,
    )
    service = LocationService(policy, clock=lambda: started)
    manual = GeoPoint(_SYNTHETIC_LATITUDE_DEG, _SYNTHETIC_LONGITUDE_DEG)
    snapshot = service.set_manual_base(manual, captured_at=started)
    assert snapshot.status is LocationStatus.MANUAL_UNVERIFIED
    assert not snapshot.absolute_position_allowed
    assert "12.3456" not in repr(snapshot)

    points = (
        GeoPoint(12.34560, 65.43210),
        GeoPoint(12.34561, 65.43211),
        GeoPoint(12.34559, 65.43209),
    )
    for index, point in enumerate(points):
        current = started + timedelta(seconds=index)
        snapshot = service.ingest(_fix(point, current), now=current)
    assert snapshot.status is LocationStatus.VERIFIED
    assert snapshot.absolute_position_allowed
    assert snapshot.accuracy_is_estimate
    assert snapshot.horizontal_accuracy_m is not None

    stale = service.refresh(now=started + timedelta(seconds=20))
    assert stale.status is LocationStatus.STALE
    assert stale.gps_fix_state is GpsFixState.STALE
    assert not stale.absolute_position_allowed

    conflict_service = LocationService(policy)
    conflict_service.set_manual_base(GeoPoint(0.0, 0.0), captured_at=started)
    for index, point in enumerate(points):
        current = started + timedelta(seconds=index)
        conflict = conflict_service.ingest(_fix(point, current), now=current)
    assert conflict.status is LocationStatus.CONFLICT


def test_location_service_rejects_low_quality_fix_without_promoting_it() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    service = LocationService(LocationPolicy(minimum_samples=1))
    snapshot = service.ingest(
        _fix(GeoPoint(55.0, 37.0), now, hdop=8.0, satellites=2),
        now=now,
    )
    assert snapshot.status is LocationStatus.COLLECTING
    assert snapshot.sample_count == 0
    assert not snapshot.absolute_position_allowed


def test_location_service_tracks_2d_no_fix_and_3d_states() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    satellites = ["01", "02", "03", "04", "", "", "", "", "", "", "", ""]
    gsa_2d = _nmea(
        ",".join(["GNGSA", "A", "2", *satellites, "1.4", "0.9", "1.1"])
    )
    gga = _nmea(
        f"GNGGA,120000.00,{_SYNTHETIC_NMEA_LATITUDE},N,"
        f"{_SYNTHETIC_NMEA_LONGITUDE},E,1,10,0.8,156.2,M,0.0,M,,"
    )
    service = LocationService(LocationPolicy(minimum_samples=1), clock=lambda: now)
    dimension = service.ingest_nmea(gsa_2d, received_at=now)
    assert dimension.gps_fix_state is GpsFixState.FIX_2D
    verified = service.ingest_nmea(gga, received_at=now)
    assert verified.status is LocationStatus.VERIFIED
    assert verified.gps_fix_state is GpsFixState.FIX_2D

    no_fix = service.ingest_nmea(
        _nmea("GNRMC,120001.00,V,,,,,,,250726,,,N"),
        received_at=now + timedelta(seconds=1),
    )
    assert no_fix.status is LocationStatus.STALE
    assert no_fix.gps_fix_state is GpsFixState.NO_FIX
    assert not no_fix.absolute_position_allowed

    gsa_3d = _nmea(
        ",".join(["GNGSA", "A", "3", *satellites, "1.2", "0.7", "0.9"])
    )
    recovered = service.ingest_nmea(
        gsa_3d,
        received_at=now + timedelta(seconds=2),
    )
    assert recovered.gps_fix_state is GpsFixState.FIX_3D


def test_location_service_rejects_implausible_position_jump() -> None:
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    service = LocationService(
        LocationPolicy(
            minimum_samples=1,
            maximum_jump_distance_m=100.0,
            maximum_jump_speed_m_s=20.0,
        )
    )
    first = service.ingest(_fix(GeoPoint(55.0, 37.0), now), now=now)
    assert first.status is LocationStatus.VERIFIED
    jumped = service.ingest(
        _fix(GeoPoint(56.0, 38.0), now + timedelta(seconds=1)),
        now=now + timedelta(seconds=1),
    )
    assert jumped.status is LocationStatus.JUMP_SUSPECTED
    assert jumped.gps_fix_state is GpsFixState.JUMP_SUSPECTED
    assert jumped.base == first.base


def test_secure_location_store_round_trip_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "location.bin"
    store = SecureLocationStore(path, _TestProtector())
    point = GeoPoint(_SYNTHETIC_LATITUDE_DEG, _SYNTHETIC_LONGITUDE_DEG, 156.0)
    saved_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    saved = store.save(point, LocationSource.MANUAL, saved_at=saved_at)
    assert "12.3456" not in repr(saved)
    raw = path.read_bytes()
    assert b"12.3456" not in raw
    assert b"65.4321" not in raw

    loaded = store.load()
    assert loaded is not None
    assert loaded.point == point
    assert loaded.source is LocationSource.MANUAL
    assert loaded.saved_at == saved_at

    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))
    with pytest.raises((SecureStoreCorruptError, UnicodeDecodeError)):
        store.load()

    assert store.clear()
    assert store.load() is None


def test_nmea_serial_receiver_never_enumerates_and_rejects_oversized_data() -> None:
    now = datetime(2026, 7, 25, 12, 36, tzinfo=UTC)
    service = LocationService(clock=lambda: now)
    receiver = NmeaSerialReceiver(service, "COM8")
    sentence = _nmea(
        f"GNGGA,123519.00,{_SYNTHETIC_NMEA_LATITUDE},N,"
        f"{_SYNTHETIC_NMEA_LONGITUDE},E,1,10,0.8,156.2,M,0.0,M,,"
    )

    assert receiver.process_line(sentence)
    assert receiver.status["accepted_sentences"] == 1
    assert not receiver.process_line(b"$" + b"A" * 300)
    assert receiver.status["rejected_sentences"] == 1

    with pytest.raises(ValueError, match="explicit Windows COM port"):
        NmeaSerialReceiver(service, "AUTO")


def test_gps_candidate_discovery_reads_metadata_without_opening_ports() -> None:
    enumerations = 0

    def enumerate_ports() -> list[object]:
        nonlocal enumerations
        enumerations += 1
        return [
            SimpleNamespace(
                device="COM12",
                description="u-blox GNSS receiver",
                manufacturer="u-blox",
                product="GNSS",
            ),
            SimpleNamespace(
                device="COM3",
                description="USB Serial Port",
                manufacturer="FTDI",
                product="",
            ),
            SimpleNamespace(device="/dev/ttyUSB0", description="ignored"),
        ]

    candidates = discover_nmea_port_candidates(port_enumerator=enumerate_ports)
    assert enumerations == 1
    assert [item.port for item in candidates] == ["COM12", "COM3"]
    assert candidates[0].confidence is GpsPortConfidence.LIKELY
    assert candidates[1].confidence is GpsPortConfidence.POSSIBLE
