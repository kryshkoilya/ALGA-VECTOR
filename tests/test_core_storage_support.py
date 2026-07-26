from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from alga_vector.config.models import (
    AdapterConfig,
    AppConfig,
    DevicesConfig,
    StorageConfig,
)
from alga_vector.direction import DirectionService
from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
    Provenance,
)
from alga_vector.domain.models import (
    CapabilityStatus,
    DeviceSnapshot,
    Incident,
    SpectrumFrame,
    SystemSnapshot,
)
from alga_vector.signal_analysis import (
    DataQuality,
    DecisionAlternative,
    DecisionEvidence,
    DecisionLifecycle,
    DecisionTransition,
    DecisionTransitionKind,
    EvidenceStrength,
    RfDecision,
    RfFamily,
)
from alga_vector.storage import (
    EventJournal,
    SpectrumCaptureWriter,
    prune_spectrum_captures,
)
from alga_vector.support import SupportBundleBuilder, verify_support_bundle

FIXED_TIME = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)


def test_event_journal_uses_wal_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state" / "events.sqlite3"
    incident = Incident(
        incident_id="incident-1",
        code="DEVICE.ABSENT",
        title_ru="Устройство отсутствует",
        message_ru="KrakenSDR не обнаружен.",
        action_ru="Проверьте питание.",
        severity=IncidentSeverity.WARNING,
        source="kraken-01",
        occurred_at=FIXED_TIME,
        technical={"attempt": 1},
    )
    journal = EventJournal(path)

    assert journal.journal_mode == "wal"
    journal.append(incident)
    journal.append(incident)
    assert journal.summary().total == 1
    assert journal.acknowledge(incident.incident_id)
    journal.close()

    reopened = EventJournal(path)
    loaded = reopened.list_incidents()
    assert len(loaded) == 1
    assert loaded[0].acknowledged
    assert loaded[0].technical == {"attempt": 1}
    reopened.close()


def test_event_journal_persists_rf_episode_and_idempotent_transitions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "events.sqlite3"
    confirmed = RfDecision(
        source_id="receiver-a",
        observed_at=FIXED_TIME,
        lifecycle=DecisionLifecycle.CONFIRMED,
        family=RfFamily.CARRIER,
        family_explanation_ru="Устойчивая спектральная линия.",
        episode_id="episode-1",
        started_at=FIXED_TIME,
        last_active_at=FIXED_TIME,
        peak_frequency_hz=433_920_000.0,
        occupied_bandwidth_hz=12_500.0,
        heuristic_score=0.81,
        calibrated_probability=None,
        evidence_strength=EvidenceStrength.HIGH,
        data_quality=DataQuality.HIGH,
        alertable=True,
        abstained=False,
        supporting_evidence=(),
        contradicting_evidence=(),
        missing_confirmation=(),
        sensor_contributions=(),
        alternatives=(
            DecisionAlternative(
                family=RfFamily.UNKNOWN,
                explanation_ru="Аппаратный spur пока нельзя исключить.",
            ),
        ),
        limitations=(
            DecisionEvidence(
                code="RF.HEURISTIC_NOT_PROBABILITY",
                explanation_ru="Score не является калиброванной вероятностью.",
            ),
        ),
    )
    opened = DecisionTransition(
        transition_id="transition-open",
        episode_id="episode-1",
        source_id="receiver-a",
        kind=DecisionTransitionKind.CONFIRMED,
        occurred_at=FIXED_TIME,
        family=confirmed.family,
        reason_code="RF.EPISODE_CONFIRMED",
        explanation_ru="Эпизод подтверждён.",
    )
    resolved_at = FIXED_TIME + timedelta(seconds=2)
    resolved = replace(
        confirmed,
        observed_at=resolved_at,
        lifecycle=DecisionLifecycle.RESOLVED,
        alertable=False,
    )
    closed = DecisionTransition(
        transition_id="transition-close",
        episode_id="episode-1",
        source_id="receiver-a",
        kind=DecisionTransitionKind.RESOLVED,
        occurred_at=resolved_at,
        family=confirmed.family,
        reason_code="RF.EPISODE_RESOLVED",
        explanation_ru="Эпизод завершён.",
    )

    journal = EventJournal(path)
    assert journal.upsert_rf_decision(confirmed)
    journal.append_rf_transition(opened)
    journal.append_rf_transition(opened)
    assert journal.upsert_rf_decision(resolved)
    journal.append_rf_transition(closed)
    journal.close()

    reopened = EventJournal(path)
    decisions = reopened.list_rf_decisions()
    transitions = reopened.list_rf_transitions(episode_id="episode-1")

    assert len(decisions) == 1
    assert decisions[0].lifecycle == DecisionLifecycle.RESOLVED
    assert decisions[0].episode_id == "episode-1"
    assert decisions[0].alternatives == confirmed.alternatives
    assert decisions[0].limitations == confirmed.limitations
    assert [item.kind for item in transitions] == [
        DecisionTransitionKind.RESOLVED,
        DecisionTransitionKind.CONFIRMED,
    ]
    reopened.close()


def test_spectrum_capture_is_atomic_checksummed_and_truthfully_labeled(
    tmp_path: Path,
) -> None:
    writer = SpectrumCaptureWriter(tmp_path / "captures", clock=lambda: FIXED_TIME)
    started = writer.start()
    frame = SpectrumFrame(
        source_id="receiver-01",
        sequence=7,
        center_frequency_hz=433_920_000,
        span_hz=2_000_000,
        power_dbm=np.asarray([-101.25, -72.5, -48.0], dtype=np.float32),
        captured_at=FIXED_TIME,
        provenance=Provenance.LIVE,
        unit="dBFS",
    )

    writer.append(frame)
    active = writer.status()
    result = writer.stop()

    assert started.active
    assert active.frames == 1
    assert not writer.status().active
    assert result.path.suffix == ".jsonl"
    assert result.path.is_file()
    assert not list(result.path.parent.glob("*.partial"))
    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert result.path.with_suffix(".jsonl.sha256").is_file()
    records = [
        json.loads(line)
        for line in result.path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["content_kind"] == "processed_spectrum"
    assert records[0]["raw_iq"] is False
    assert records[1]["unit"] == "dBFS"
    assert records[1]["power"] == [-101.25, -72.5, -48.0]
    assert records[-1]["frames"] == 1


def test_aborted_spectrum_capture_retains_partial_file(tmp_path: Path) -> None:
    writer = SpectrumCaptureWriter(tmp_path / "captures", clock=lambda: FIXED_TIME)
    writer.start()

    partial = writer.abort()

    assert partial is not None and partial.name.endswith(".partial")
    assert partial.is_file()
    assert not writer.active


def test_retention_removes_only_expired_finalized_captures(tmp_path: Path) -> None:
    writer = SpectrumCaptureWriter(tmp_path / "captures", clock=lambda: FIXED_TIME)
    writer.start()
    finalized = writer.stop().path
    partial_writer = SpectrumCaptureWriter(
        tmp_path / "captures",
        clock=lambda: FIXED_TIME,
    )
    partial_writer.start()
    partial = partial_writer.abort()
    assert partial is not None
    old_timestamp = (FIXED_TIME - timedelta(days=31)).timestamp()
    for path in (finalized, finalized.with_suffix(".jsonl.sha256"), partial):
        os.utime(path, (old_timestamp, old_timestamp))

    result = prune_spectrum_captures(
        tmp_path / "captures",
        retention_days=30,
        now=FIXED_TIME,
    )

    assert result.removed_files == 2
    assert not finalized.exists()
    assert not finalized.with_suffix(".jsonl.sha256").exists()
    assert partial.exists()
    assert result.skipped_partial_files == 1


def test_support_bundle_is_redacted_and_manifest_hashes_payloads(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        first_run_complete=True,
        storage=StorageConfig(data_dir=Path(r"C:\Users\Operator\captures")),
        devices=DevicesConfig(
            adapters=[
                AdapterConfig(
                    id="serial-super-secret",
                    kind="rtlsdr",
                    connection="RTLSDR:99",
                )
            ]
        ),
    )
    incident = Incident(
        incident_id="incident-secret",
        code="DEVICE.ABSENT",
        title_ru="KrakenSDR недоступен",
        message_ru="Нет ответа от 192.168.1.100",
        action_ru="Проверьте C:\\Users\\Operator\\device.txt",
        severity=IncidentSeverity.WARNING,
        source="serial-super-secret",
        occurred_at=FIXED_TIME,
        technical={"password": "never-export-me", "ip_address": "192.168.1.100"},
    )
    decision = RfDecision(
        source_id="serial-super-secret",
        observed_at=FIXED_TIME,
        lifecycle=DecisionLifecycle.CONFIRMED,
        family=RfFamily.CONTINUOUS_CARRIER_OR_SPUR,
        family_explanation_ru="Устойчивая спектральная линия.",
        episode_id="episode-secret",
        started_at=FIXED_TIME,
        last_active_at=FIXED_TIME,
        peak_frequency_hz=433_920_000.0,
        occupied_bandwidth_hz=12_500.0,
        heuristic_score=0.81,
        calibrated_probability=None,
        evidence_strength=EvidenceStrength.HIGH,
        data_quality=DataQuality.HIGH,
        alertable=True,
        abstained=False,
        supporting_evidence=(),
        contradicting_evidence=(),
        missing_confirmation=(),
        sensor_contributions=(),
        alternatives=(
            DecisionAlternative(
                family=RfFamily.UNKNOWN,
                explanation_ru="Аппаратный spur нельзя исключить.",
            ),
        ),
        limitations=(
            DecisionEvidence(
                code="RF.HEURISTIC_NOT_PROBABILITY",
                explanation_ru="Score не является вероятностью.",
            ),
        ),
    )
    direction = DirectionService(clock=lambda: FIXED_TIME).set_manual(
        123.0,
        uncertainty_deg=20.0,
    )
    snapshot = SystemSnapshot(
        revision=1,
        devices=(
            DeviceSnapshot(
                device_id="serial-super-secret",
                display_name="KrakenSDR",
                kind="rtlsdr",
                connection="RTLSDR:99",
                state=DeviceState.ABSENT,
                health=HealthLevel.UNKNOWN,
                capabilities=frozenset({Capability.DF_OBSERVATION}),
            ),
        ),
        capabilities=(
            CapabilityStatus(
                Capability.DF_OBSERVATION,
                CapabilityState.BLOCKED,
                explanation_ru="Нет ответа от 192.168.1.100",
            ),
        ),
        incidents=(incident,),
        spectrum=SpectrumFrame(
            source_id="serial-super-secret",
            sequence=1,
            center_frequency_hz=433_920_000,
            span_hz=5_000_000,
            power_dbm=np.asarray([-100.0, -42.0], dtype=np.float32),
            captured_at=FIXED_TIME,
            provenance=Provenance.SIMULATED,
        ),
        mode=Provenance.SIMULATED,
        profile_name="Полевой профиль",
        readiness_percent=67,
        direction=direction,
        signal_decision=decision,
        signal_events=(decision,),
        captured_at=FIXED_TIME,
    )
    log_path = tmp_path / "runtime.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "message": (
                    # 12.3456, 65.4321 is an intentionally synthetic test-only base.
                    "User Operator at 192.168.1.100; base 12.3456, 65.4321; "
                    "$GPGGA,120000,1220.736,N,06525.926,E,1,08,0.9,120.0,M,0.0,M,,*00"
                ),
                "token": "never-export-me",
                "device_id": "serial-super-secret",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    builder = SupportBundleBuilder(
        clock=lambda: FIXED_TIME,
        salt_factory=lambda size: b"s" * size,
    )

    result = builder.build(
        tmp_path / "support-request.zip",
        config=config,
        snapshot=snapshot,
        log_files=(log_path,),
        build_id="test-build",
    )

    assert result.path.suffix == ".avsupport"
    assert verify_support_bundle(result.path)
    with zipfile.ZipFile(result.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        all_payload = b"\n".join(archive.read(name) for name in archive.namelist())
        assert b"192.168.1.100" not in all_payload
        assert b"serial-super-secret" not in all_payload
        assert b"episode-secret" not in all_payload
        assert b"never-export-me" not in all_payload
        assert b"C:\\\\Users\\\\Operator" not in all_payload
        assert b"12.3456" not in all_payload
        assert b"1220.736" not in all_payload
        assert b"[-100.0" not in all_payload
        health = json.loads(archive.read("health/latest_snapshot.json"))
        assert health["signal_decision"]["lifecycle"] == "confirmed"
        assert health["signal_events"][0]["family"] == (
            "continuous_carrier_or_spur"
        )
        assert health["signal_decision"]["calibrated_probability"] is None
        assert health["signal_decision"]["alternatives"][0]["family"] == "unknown"
        assert health["signal_decision"]["limitations"][0]["code"] == (
            "RF.HEURISTIC_NOT_PROBABILITY"
        )
        assert health["direction"]["source"] == "manual"
        assert health["direction"]["measured"] is False
        assert health["direction"]["bearing_exported"] is False
        assert "bearing_deg" not in health["direction"]
        assert manifest["contains_raw_iq"] is False
        for entry in manifest["files"]:
            payload = archive.read(entry["path"])
            assert len(payload) == entry["size"]
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
