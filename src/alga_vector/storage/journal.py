from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

from alga_vector.domain.enums import IncidentSeverity
from alga_vector.domain.models import Incident
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
    SensorContribution,
)


@dataclass(slots=True, frozen=True)
class JournalSummary:
    total: int
    unacknowledged: int
    by_severity: dict[str, int]


class EventJournal:
    """SQLite/WAL-backed incident journal with one explicit writer connection."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = RLock()
        self._closed = False
        connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        self._connection = connection
        try:
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
        except Exception:
            # On Windows a failed constructor must not leave the corrupt file
            # locked by a half-created SQLite connection.
            connection.close()
            self._closed = True
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def journal_mode(self) -> str:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()

    def append(self, incident: Incident) -> None:
        payload = json.dumps(
            incident.technical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        )
        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, code, title_ru, message_ru, action_ru, severity,
                    source, occurred_at, acknowledged, technical_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    code = excluded.code,
                    title_ru = excluded.title_ru,
                    message_ru = excluded.message_ru,
                    action_ru = excluded.action_ru,
                    severity = excluded.severity,
                    source = excluded.source,
                    technical_json = excluded.technical_json,
                    acknowledged = MAX(incidents.acknowledged, excluded.acknowledged)
                """,
                (
                    incident.incident_id,
                    incident.code,
                    incident.title_ru,
                    incident.message_ru,
                    incident.action_ru,
                    incident.severity.value,
                    incident.source,
                    incident.occurred_at.isoformat(),
                    int(incident.acknowledged),
                    payload,
                ),
            )

    def append_many(self, incidents: Iterable[Incident]) -> None:
        for incident in incidents:
            self.append(incident)

    def upsert_rf_decision(self, decision: RfDecision) -> bool:
        """Persist one externally meaningful RF episode state.

        Candidate and idle decisions do not have a durable product meaning and
        are intentionally excluded. The caller writes only on lifecycle
        transitions, not for every acquired spectrum frame.
        """

        if decision.episode_id is None:
            return False
        payload = json.dumps(
            _rf_decision_payload(decision),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO rf_episodes (
                    episode_id, source_id, lifecycle, family,
                    first_observed_at, last_observed_at, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    lifecycle = excluded.lifecycle,
                    family = excluded.family,
                    last_observed_at = excluded.last_observed_at,
                    decision_json = excluded.decision_json
                """,
                (
                    decision.episode_id,
                    decision.source_id,
                    decision.lifecycle.value,
                    decision.family.value,
                    (decision.started_at or decision.observed_at).isoformat(),
                    decision.observed_at.isoformat(),
                    payload,
                ),
            )
        return True

    def append_rf_transition(self, transition: DecisionTransition) -> None:
        """Append one idempotent state-machine transition."""

        with self._lock, self._connection:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO rf_transitions (
                    transition_id, episode_id, source_id, kind, occurred_at,
                    family, reason_code, explanation_ru
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transition_id) DO NOTHING
                """,
                (
                    transition.transition_id,
                    transition.episode_id,
                    transition.source_id,
                    transition.kind.value,
                    transition.occurred_at.isoformat(),
                    transition.family.value,
                    transition.reason_code,
                    transition.explanation_ru,
                ),
            )

    def list_rf_decisions(self, *, limit: int = 200) -> tuple[RfDecision, ...]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be in range 1..10000")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT decision_json
                FROM rf_episodes
                ORDER BY last_observed_at DESC, episode_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            _rf_decision_from_payload(json.loads(str(row["decision_json"])))
            for row in rows
        )

    def list_rf_transitions(
        self,
        *,
        episode_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DecisionTransition, ...]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be in range 1..10000")
        where = ""
        parameters: list[Any] = []
        if episode_id is not None:
            if not episode_id.strip():
                raise ValueError("episode_id must not be blank")
            where = "WHERE episode_id = ?"
            parameters.append(episode_id)
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT *
                FROM rf_transitions
                {where}
                ORDER BY occurred_at DESC, transition_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(_rf_transition_from_row(row) for row in rows)

    def list_incidents(
        self,
        *,
        limit: int = 200,
        severities: Iterable[IncidentSeverity] | None = None,
        acknowledged: bool | None = None,
    ) -> tuple[Incident, ...]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be in range 1..10000")
        clauses: list[str] = []
        parameters: list[Any] = []
        severity_values = tuple(item.value for item in severities or ())
        if severity_values:
            placeholders = ",".join("?" for _ in severity_values)
            clauses.append(f"severity IN ({placeholders})")
            parameters.extend(severity_values)
        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            parameters.append(int(acknowledged))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"""
                SELECT * FROM incidents
                {where}
                ORDER BY occurred_at DESC, incident_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(_incident_from_row(row) for row in rows)

    def acknowledge(self, incident_id: str) -> bool:
        with self._lock, self._connection:
            self._ensure_open()
            cursor = self._connection.execute(
                "UPDATE incidents SET acknowledged = 1 WHERE incident_id = ?",
                (incident_id,),
            )
            return cursor.rowcount > 0

    def summary(self) -> JournalSummary:
        with self._lock:
            self._ensure_open()
            total_row = self._connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END) AS unacknowledged
                FROM incidents
                """
            ).fetchone()
            severity_rows = self._connection.execute(
                "SELECT severity, COUNT(*) AS count FROM incidents GROUP BY severity"
            ).fetchall()
        return JournalSummary(
            total=int(total_row["total"] or 0),
            unacknowledged=int(total_row["unacknowledged"] or 0),
            by_severity={str(row["severity"]): int(row["count"]) for row in severity_rows},
        )

    def checkpoint(self) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.commit()
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            finally:
                self._connection.close()
                self._closed = True

    def _configure(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    title_ru TEXT NOT NULL,
                    message_ru TEXT NOT NULL,
                    action_ru TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
                    technical_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS incidents_occurred_at_idx
                ON incidents(occurred_at DESC)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS incidents_ack_severity_idx
                ON incidents(acknowledged, severity)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rf_episodes (
                    episode_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    family TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    decision_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rf_episodes_last_observed_idx
                ON rf_episodes(last_observed_at DESC)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rf_transitions (
                    transition_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    family TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    explanation_ru TEXT NOT NULL,
                    FOREIGN KEY(episode_id) REFERENCES rf_episodes(episode_id)
                        ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rf_transitions_episode_time_idx
                ON rf_transitions(episode_id, occurred_at DESC)
                """
            )
            self._connection.execute("PRAGMA user_version=2")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event journal is closed")

    def __enter__(self) -> EventJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _incident_from_row(row: sqlite3.Row) -> Incident:
    technical = json.loads(str(row["technical_json"]))
    if not isinstance(technical, dict):
        technical = {}
    return Incident(
        incident_id=str(row["incident_id"]),
        code=str(row["code"]),
        title_ru=str(row["title_ru"]),
        message_ru=str(row["message_ru"]),
        action_ru=str(row["action_ru"]),
        severity=IncidentSeverity(str(row["severity"])),
        source=str(row["source"]),
        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        acknowledged=bool(row["acknowledged"]),
        technical=technical,
    )


def _rf_decision_payload(decision: RfDecision) -> dict[str, Any]:
    return {
        "source_id": decision.source_id,
        "observed_at": decision.observed_at.isoformat(),
        "lifecycle": decision.lifecycle.value,
        "family": decision.family.value,
        "family_explanation_ru": decision.family_explanation_ru,
        "episode_id": decision.episode_id,
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
        "calibrated_probability": None,
        "evidence_strength": decision.evidence_strength.value,
        "data_quality": decision.data_quality.value,
        "alertable": decision.alertable,
        "abstained": decision.abstained,
        "supporting_evidence": [
            _decision_evidence_payload(item)
            for item in decision.supporting_evidence
        ],
        "contradicting_evidence": [
            _decision_evidence_payload(item)
            for item in decision.contradicting_evidence
        ],
        "missing_confirmation": [
            _decision_evidence_payload(item)
            for item in decision.missing_confirmation
        ],
        "sensor_contributions": [
            {
                "source_id": item.source_id,
                "contribution": item.contribution,
                "data_quality": item.data_quality.value,
                "independent_confirmation": item.independent_confirmation,
                "explanation_ru": item.explanation_ru,
            }
            for item in decision.sensor_contributions
        ],
        "alternatives": [
            {
                "family": item.family.value,
                "explanation_ru": item.explanation_ru,
            }
            for item in decision.alternatives
        ],
        "limitations": [
            _decision_evidence_payload(item)
            for item in decision.limitations
        ],
    }


def _decision_evidence_payload(item: DecisionEvidence) -> dict[str, Any]:
    return {
        "code": item.code,
        "explanation_ru": item.explanation_ru,
        "measured": item.measured,
        "threshold": item.threshold,
    }


def _rf_decision_from_payload(raw: Any) -> RfDecision:
    if not isinstance(raw, dict):
        raise ValueError("RF decision payload must be an object")
    episode_id = raw.get("episode_id")
    return RfDecision(
        source_id=str(raw["source_id"]),
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        lifecycle=DecisionLifecycle(str(raw["lifecycle"])),
        family=RfFamily(str(raw["family"])),
        family_explanation_ru=str(raw["family_explanation_ru"]),
        episode_id=str(episode_id) if episode_id is not None else None,
        started_at=_optional_datetime(raw.get("started_at")),
        last_active_at=_optional_datetime(raw.get("last_active_at")),
        peak_frequency_hz=_optional_float(raw.get("peak_frequency_hz")),
        occupied_bandwidth_hz=_optional_float(
            raw.get("occupied_bandwidth_hz")
        ),
        heuristic_score=float(raw["heuristic_score"]),
        calibrated_probability=None,
        evidence_strength=EvidenceStrength(str(raw["evidence_strength"])),
        data_quality=DataQuality(str(raw["data_quality"])),
        alertable=bool(raw["alertable"]),
        abstained=bool(raw["abstained"]),
        supporting_evidence=_evidence_tuple(raw.get("supporting_evidence")),
        contradicting_evidence=_evidence_tuple(
            raw.get("contradicting_evidence")
        ),
        missing_confirmation=_evidence_tuple(raw.get("missing_confirmation")),
        sensor_contributions=_sensor_contribution_tuple(
            raw.get("sensor_contributions")
        ),
        alternatives=_alternative_tuple(raw.get("alternatives", [])),
        limitations=_evidence_tuple(raw.get("limitations", [])),
    )


def _evidence_tuple(raw: Any) -> tuple[DecisionEvidence, ...]:
    if not isinstance(raw, list):
        raise ValueError("decision evidence must be a list")
    output: list[DecisionEvidence] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("decision evidence item must be an object")
        output.append(
            DecisionEvidence(
                code=str(item["code"]),
                explanation_ru=str(item["explanation_ru"]),
                measured=_evidence_value(item.get("measured")),
                threshold=_evidence_value(item.get("threshold")),
            )
        )
    return tuple(output)


def _sensor_contribution_tuple(raw: Any) -> tuple[SensorContribution, ...]:
    if not isinstance(raw, list):
        raise ValueError("sensor contributions must be a list")
    output: list[SensorContribution] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("sensor contribution must be an object")
        output.append(
            SensorContribution(
                source_id=str(item["source_id"]),
                contribution=float(item["contribution"]),
                data_quality=DataQuality(str(item["data_quality"])),
                independent_confirmation=bool(
                    item["independent_confirmation"]
                ),
                explanation_ru=str(item["explanation_ru"]),
            )
        )
    return tuple(output)


def _alternative_tuple(raw: Any) -> tuple[DecisionAlternative, ...]:
    if not isinstance(raw, list):
        raise ValueError("decision alternatives must be a list")
    output: list[DecisionAlternative] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("decision alternative must be an object")
        output.append(
            DecisionAlternative(
                family=RfFamily(str(item["family"])),
                explanation_ru=str(item["explanation_ru"]),
            )
        )
    return tuple(output)


def _evidence_value(raw: Any) -> float | int | str | None:
    if raw is None or isinstance(raw, (float, int, str)):
        return raw
    raise ValueError("invalid decision evidence value")


def _optional_datetime(raw: Any) -> datetime | None:
    return datetime.fromisoformat(str(raw)) if raw is not None else None


def _optional_float(raw: Any) -> float | None:
    return float(raw) if raw is not None else None


def _rf_transition_from_row(row: sqlite3.Row) -> DecisionTransition:
    return DecisionTransition(
        transition_id=str(row["transition_id"]),
        episode_id=str(row["episode_id"]),
        source_id=str(row["source_id"]),
        kind=DecisionTransitionKind(str(row["kind"])),
        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        family=RfFamily(str(row["family"])),
        reason_code=str(row["reason_code"]),
        explanation_ru=str(row["explanation_ru"]),
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
