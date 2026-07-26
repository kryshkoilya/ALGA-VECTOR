from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alga_vector.airspace import (
    AirspaceDataQuality,
    AirspaceFeedState,
    AirspacePayloadError,
    CivilAirspacePolicy,
    CivilAirspaceService,
    DeterministicCivilAirspaceSource,
    load_dump1090_aircraft_file,
    parse_dump1090_aircraft_json,
)


def _payload(now: datetime) -> dict[str, object]:
    return {
        "now": now.timestamp(),
        "messages": 17,
        "aircraft": [
            {
                "hex": "ABC123",
                "flight": " ual 42 ",
                "alt_baro": 31_000,
                "gs": 421.5,
                "track": 360.0,
                "seen": 1.25,
                "lat": 50.45,
                "lon": 30.52,
                "rssi": -18.2,
                "category": "A3",
            }
        ],
    }


def test_parser_keeps_only_bounded_public_broadcast_facts() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    parsed = parse_dump1090_aircraft_json(_payload(now), received_at=now)

    assert parsed.timestamp_from_payload
    assert parsed.rejected_record_count == 0
    aircraft = parsed.aircraft[0]
    assert aircraft.hex == "abc123"
    assert aircraft.callsign == "UAL 42"
    assert aircraft.altitude_ft == pytest.approx(31_000.0)
    assert aircraft.ground_speed_kt == pytest.approx(421.5)
    assert aircraft.track_deg == pytest.approx(0.0)
    assert aircraft.seen_s == pytest.approx(1.25)
    assert aircraft.observed_at == now - timedelta(seconds=1.25)
    assert not hasattr(aircraft, "lat")
    assert not hasattr(aircraft, "lon")
    assert not hasattr(aircraft, "nationality")
    assert not hasattr(aircraft, "military")
    assert not hasattr(aircraft, "hostile")


def test_malformed_record_is_isolated_without_losing_valid_context() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    payload = _payload(now)
    aircraft = payload["aircraft"]
    assert isinstance(aircraft, list)
    aircraft.extend(
        [
            {"hex": "bad", "seen": 0.0},
            {"hex": "def456", "seen": "recent"},
            {
                "hex": "fedcba",
                "flight": "CIV100",
                "alt_baro": "ground",
                "gs": 0,
                "track": 90,
                "seen": 0.25,
            },
        ]
    )

    parsed = parse_dump1090_aircraft_json(payload, received_at=now)

    assert [item.hex for item in parsed.aircraft] == ["abc123", "fedcba"]
    assert parsed.rejected_record_count == 2
    malformed = [issue for issue in parsed.issues if issue.code == "AIRSPACE.MALFORMED_RECORD"]
    assert len(malformed) == 2
    assert {issue.record_index for issue in malformed} == {1, 2}
    assert parsed.aircraft[1].on_ground is True
    assert parsed.aircraft[1].altitude_ft is None


def test_duplicate_address_keeps_freshest_record_deterministically() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    payload = {
        "now": now.timestamp(),
        "aircraft": [
            {"hex": "abcdef", "flight": "OLD", "seen": 8.0},
            {"hex": "ABCDEF", "flight": "NEW", "seen": 0.5},
        ],
    }

    parsed = parse_dump1090_aircraft_json(payload, received_at=now)

    assert len(parsed.aircraft) == 1
    assert parsed.aircraft[0].callsign == "NEW"
    assert parsed.rejected_record_count == 1
    assert parsed.issues[0].code == "AIRSPACE.DUPLICATE_RECORD"
    assert parsed.issues[0].record_index == 0


def test_pseudonymous_identifier_is_stable_and_not_the_public_hex() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    first = parse_dump1090_aircraft_json(_payload(now), received_at=now)
    second = parse_dump1090_aircraft_json(_payload(now), received_at=now)

    identifier = first.aircraft[0].pseudonymous_id
    assert identifier == second.aircraft[0].pseudonymous_id
    assert identifier.startswith("ac-")
    assert "abc123" not in identifier


def test_service_summary_is_explicitly_context_only() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(clock=lambda: now)

    snapshot = service.ingest_payload(_payload(now))

    summary = snapshot.summary
    assert summary.state is AirspaceFeedState.CURRENT
    assert summary.active_count == 1
    assert summary.nearby_context_available
    assert summary.context_only
    assert summary.context_scope == "public_civil_broadcast_only"
    assert not summary.supports_identity_correlation
    assert not summary.supports_friend_or_foe
    assert not summary.supports_threat_inference
    assert any("does not correlate" in limitation for limitation in summary.limitations)
    assert any("does not prove" in limitation for limitation in summary.limitations)


def test_aircraft_and_feed_ttl_fail_closed() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(
        CivilAirspacePolicy(aircraft_ttl_s=3.0, feed_ttl_s=5.0),
        clock=lambda: now,
    )
    service.ingest_payload(_payload(now))

    no_active_aircraft = service.snapshot(now=now + timedelta(seconds=3))
    stale_feed = service.snapshot(now=now + timedelta(seconds=6))

    assert no_active_aircraft.summary.state is AirspaceFeedState.CURRENT
    assert no_active_aircraft.summary.active_count == 0
    assert not no_active_aircraft.summary.nearby_context_available
    assert stale_feed.summary.state is AirspaceFeedState.STALE
    assert stale_feed.summary.stale
    assert stale_feed.summary.active_count == 0
    assert stale_feed.summary.data_quality is AirspaceDataQuality.UNAVAILABLE


def test_missing_source_timestamp_is_received_but_quality_is_partial() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(clock=lambda: now)

    snapshot = service.ingest_payload(
        {"aircraft": [{"hex": "abc123", "seen": 0.0}]}
    )

    assert snapshot.summary.state is AirspaceFeedState.CURRENT
    assert snapshot.summary.data_quality is AirspaceDataQuality.PARTIAL
    assert snapshot.issues[0].code == "AIRSPACE.SOURCE_TIME_MISSING"
    assert snapshot.summary.source_generated_at == now


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"{not-json", "AIRSPACE.INVALID_JSON"),
        (b"[]", "AIRSPACE.INVALID_ROOT"),
        ({"now": True, "aircraft": []}, "AIRSPACE.INVALID_SOURCE_TIME"),
        ({"now": 1_783_000_000.0}, "AIRSPACE.INVALID_AIRCRAFT_LIST"),
    ],
)
def test_root_errors_fail_closed(
    payload: object,
    expected_code: str,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(clock=lambda: now)

    snapshot = service.ingest_payload(payload)  # type: ignore[arg-type]

    assert snapshot.summary.state is AirspaceFeedState.INVALID
    assert snapshot.summary.stale
    assert not snapshot.summary.nearby_context_available
    assert snapshot.aircraft == ()
    assert snapshot.issues[0].code == expected_code


def test_future_source_time_is_rejected() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(
        CivilAirspacePolicy(future_tolerance_s=1.0),
        clock=lambda: now,
    )
    payload = _payload(now + timedelta(seconds=2))

    snapshot = service.ingest_payload(payload)

    assert snapshot.summary.state is AirspaceFeedState.INVALID
    assert snapshot.issues[0].code == "AIRSPACE.SOURCE_TIME_IN_FUTURE"


def test_local_file_loading_is_bounded_and_never_accepts_url(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    path = tmp_path / "aircraft.json"
    path.write_text(json.dumps(_payload(now)), encoding="utf-8")

    parsed = load_dump1090_aircraft_file(path, received_at=now)

    assert len(parsed.aircraft) == 1
    with pytest.raises(AirspacePayloadError) as too_large:
        load_dump1090_aircraft_file(
            path,
            received_at=now,
            max_payload_bytes=8,
        )
    assert too_large.value.code == "AIRSPACE.PAYLOAD_TOO_LARGE"
    with pytest.raises(AirspacePayloadError) as non_local:
        load_dump1090_aircraft_file(
            "https://example.invalid/aircraft.json",
            received_at=now,
        )
    assert non_local.value.code == "AIRSPACE.NON_LOCAL_SOURCE"


def test_missing_file_is_an_io_state_without_reusing_old_context(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    service = CivilAirspaceService(clock=lambda: now)
    service.ingest_payload(_payload(now))

    snapshot = service.ingest_file(tmp_path / "missing.json")

    assert snapshot.summary.state is AirspaceFeedState.IO_ERROR
    assert snapshot.summary.active_count == 0
    assert snapshot.aircraft == ()
    assert snapshot.issues[0].code == "AIRSPACE.FILE_NOT_FOUND"


def test_deterministic_source_is_explicitly_fake_and_repeatable() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    source = DeterministicCivilAirspaceSource()

    first = source.read_payload(now)
    second = source.read_payload(now)
    decoded = json.loads(first)

    assert first == second
    assert source.simulated
    assert source.source_id == "fake-civil-airspace"
    assert decoded["simulated"] is True
    assert all(record["hex"].startswith("~f") for record in decoded["aircraft"])
    snapshot = CivilAirspaceService(clock=lambda: now).ingest_payload(first)
    assert snapshot.summary.active_count == 2


def test_parser_rejects_naive_time_and_record_flood() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_dump1090_aircraft_json(
            {"aircraft": []},
            received_at=datetime(2026, 7, 26, 12, 0),
        )
    with pytest.raises(AirspacePayloadError) as flood:
        parse_dump1090_aircraft_json(
            {
                "now": now.timestamp(),
                "aircraft": [{"hex": "abc123", "seen": 0.0}] * 2,
            },
            received_at=now,
            max_records=1,
        )
    assert flood.value.code == "AIRSPACE.TOO_MANY_RECORDS"
