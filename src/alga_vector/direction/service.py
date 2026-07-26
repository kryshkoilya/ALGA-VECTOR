"""Policy-enforced direction state and bounded history."""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from .models import (
    DirectionObservation,
    DirectionQuality,
    DirectionSnapshot,
    DirectionSource,
    DirectionTrailPoint,
    ExternalDirectionEvidence,
)


@dataclass(frozen=True, slots=True)
class DirectionPolicy:
    """Freshness and evidence requirements for each source."""

    external_max_age_s: float = 3.0
    manual_max_age_s: float = 3_600.0
    simulated_max_age_s: float = 5.0
    maximum_calibration_age_s: float = 300.0
    future_tolerance_s: float = 1.0
    minimum_evidence_samples: int = 3
    minimum_evidence_quality: float = 0.55
    minimum_external_confidence: float = 0.40
    history_limit: int = 24

    def __post_init__(self) -> None:
        for name in (
            "external_max_age_s",
            "manual_max_age_s",
            "simulated_max_age_s",
            "maximum_calibration_age_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.future_tolerance_s) or self.future_tolerance_s < 0.0:
            raise ValueError("future_tolerance_s must be finite and non-negative")
        if self.minimum_evidence_samples < 1:
            raise ValueError("minimum_evidence_samples must be positive")
        for name in ("minimum_evidence_quality", "minimum_external_confidence"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within 0..1")
        if self.history_limit < 1:
            raise ValueError("history_limit must be positive")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DirectionService:
    """Accept direction only from explicit and policy-valid sources."""

    def __init__(
        self,
        policy: DirectionPolicy | None = None,
        *,
        demo_mode: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._policy = policy or DirectionPolicy()
        self._demo_mode = bool(demo_mode)
        self._clock = clock
        self._lock = RLock()
        self._trail: deque[DirectionTrailPoint] = deque(maxlen=self._policy.history_limit)
        now = self._now()
        self._snapshot = DirectionSnapshot(
            current=DirectionObservation.unavailable(),
            trail=(),
            evaluated_at=now,
            stale=False,
            age_s=None,
            last_valid_at=None,
        )

    @property
    def policy(self) -> DirectionPolicy:
        return self._policy

    @property
    def demo_mode(self) -> bool:
        return self._demo_mode

    def set_manual(
        self,
        bearing_deg: float,
        *,
        uncertainty_deg: float = 15.0,
        captured_at: datetime | None = None,
        source_id: str = "operator",
    ) -> DirectionSnapshot:
        """Store operator input without presenting it as a sensor measurement."""

        with self._lock:
            now = self._now()
            captured = captured_at or now
            self._validate_not_future(captured, now)
            if (now - captured).total_seconds() > self._policy.manual_max_age_s:
                return self._set_unavailable(
                    "DIRECTION.MANUAL_STALE",
                    "Ручная отметка устарела; введите актуальный азимут.",
                    now,
                )
            observation = DirectionObservation(
                source=DirectionSource.MANUAL,
                bearing_deg=self._normalize_bearing(bearing_deg),
                uncertainty_deg=float(uncertainty_deg),
                confidence=None,
                quality=DirectionQuality.UNMEASURED,
                captured_at=captured,
                source_id=source_id,
                reason_code="DIRECTION.MANUAL_OPERATOR_INPUT",
                message_ru=("Азимут введён оператором и не является измерением приёмника."),
            )
            return self._accept(observation, now)

    def ingest_external(
        self,
        bearing_deg: float,
        *,
        uncertainty_deg: float,
        confidence: float,
        captured_at: datetime,
        source_id: str,
        evidence: ExternalDirectionEvidence,
    ) -> DirectionSnapshot:
        """Accept a real sensor sample only with fresh calibration and evidence."""

        with self._lock:
            now = self._now()
            rejection = self._external_rejection(
                confidence=float(confidence),
                captured_at=captured_at,
                evidence=evidence,
                now=now,
            )
            if rejection is not None:
                code, message = rejection
                return self._set_unavailable(code, message, now)
            effective_confidence = min(float(confidence), evidence.quality_score)
            quality = self._quality_for(effective_confidence)
            observation = DirectionObservation(
                source=DirectionSource.EXTERNAL,
                bearing_deg=self._normalize_bearing(bearing_deg),
                uncertainty_deg=float(uncertainty_deg),
                confidence=effective_confidence,
                quality=quality,
                captured_at=captured_at,
                source_id=source_id,
                reason_code="DIRECTION.EXTERNAL_VALIDATED",
                message_ru=(
                    "Направление получено от внешнего датчика с действующей "
                    "калибровкой и свежим подтверждением."
                ),
                evidence=evidence,
            )
            return self._accept(observation, now)

    def set_simulated(
        self,
        bearing_deg: float,
        *,
        uncertainty_deg: float = 10.0,
        confidence: float = 0.80,
        captured_at: datetime | None = None,
        source_id: str = "demo-direction",
    ) -> DirectionSnapshot:
        """Publish a clearly labelled synthetic sample only in demo mode."""

        with self._lock:
            now = self._now()
            if not self._demo_mode:
                return self._set_unavailable(
                    "DIRECTION.SIMULATION_BLOCKED",
                    "Симуляция направления запрещена вне демо-режима.",
                    now,
                )
            captured = captured_at or now
            self._validate_not_future(captured, now)
            if (now - captured).total_seconds() > self._policy.simulated_max_age_s:
                return self._set_unavailable(
                    "DIRECTION.SIMULATION_STALE",
                    "Синтетический кадр устарел и не показан.",
                    now,
                )
            observation = DirectionObservation(
                source=DirectionSource.SIMULATED,
                bearing_deg=self._normalize_bearing(bearing_deg),
                uncertainty_deg=float(uncertainty_deg),
                confidence=float(confidence),
                quality=DirectionQuality.SIMULATED,
                captured_at=captured,
                source_id=source_id,
                reason_code="DIRECTION.SIMULATED_DEMO",
                message_ru=(
                    "Синтетическое направление показано только для проверки "
                    "интерфейса в демо-режиме."
                ),
            )
            return self._accept(observation, now)

    def clear(
        self,
        message_ru: str = "Направление очищено оператором.",
    ) -> DirectionSnapshot:
        with self._lock:
            return self._set_unavailable(
                "DIRECTION.CLEARED",
                message_ru,
                self._now(),
            )

    def snapshot(self, *, now: datetime | None = None) -> DirectionSnapshot:
        """Return a fresh state; expired samples are removed from active display."""

        with self._lock:
            evaluated = now or self._now()
            if evaluated.tzinfo is None or evaluated.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            current = self._snapshot.current
            if not current.available or current.captured_at is None:
                last_valid = self._snapshot.last_valid_at
                age_s = (
                    max(0.0, (evaluated - last_valid).total_seconds())
                    if last_valid is not None
                    else None
                )
                self._snapshot = DirectionSnapshot(
                    current=current,
                    trail=tuple(self._trail),
                    evaluated_at=evaluated,
                    stale=self._snapshot.stale,
                    age_s=age_s,
                    last_valid_at=last_valid,
                )
                return self._snapshot
            age_s = max(0.0, (evaluated - current.captured_at).total_seconds())
            if age_s > self._maximum_age_for(current.source):
                self._snapshot = DirectionSnapshot(
                    current=DirectionObservation.unavailable(
                        "Последнее направление устарело; активный луч скрыт.",
                        reason_code="DIRECTION.STALE",
                        source_id=current.source_id,
                    ),
                    trail=tuple(self._trail),
                    evaluated_at=evaluated,
                    stale=True,
                    age_s=age_s,
                    last_valid_at=current.captured_at,
                )
            else:
                self._snapshot = DirectionSnapshot(
                    current=current,
                    trail=tuple(self._trail),
                    evaluated_at=evaluated,
                    stale=False,
                    age_s=age_s,
                    last_valid_at=current.captured_at,
                )
            return self._snapshot

    current_snapshot = snapshot

    def _accept(
        self,
        observation: DirectionObservation,
        now: datetime,
    ) -> DirectionSnapshot:
        point = DirectionTrailPoint.from_observation(observation)
        self._trail.append(point)
        age_s = max(
            0.0,
            (now - point.captured_at).total_seconds(),
        )
        self._snapshot = DirectionSnapshot(
            current=observation,
            trail=tuple(self._trail),
            evaluated_at=now,
            stale=False,
            age_s=age_s,
            last_valid_at=point.captured_at,
        )
        return self._snapshot

    def _set_unavailable(
        self,
        reason_code: str,
        message_ru: str,
        now: datetime,
    ) -> DirectionSnapshot:
        last_valid = self._snapshot.last_valid_at
        age_s = max(0.0, (now - last_valid).total_seconds()) if last_valid is not None else None
        self._snapshot = DirectionSnapshot(
            current=DirectionObservation.unavailable(
                message_ru,
                reason_code=reason_code,
            ),
            trail=tuple(self._trail),
            evaluated_at=now,
            stale=False,
            age_s=age_s,
            last_valid_at=last_valid,
        )
        return self._snapshot

    def _external_rejection(
        self,
        *,
        confidence: float,
        captured_at: datetime,
        evidence: ExternalDirectionEvidence,
        now: datetime,
    ) -> tuple[str, str] | None:
        if not evidence.calibration_valid:
            return (
                "DIRECTION.CALIBRATION_INVALID",
                "Внешняя калибровка не подтверждена; направление скрыто.",
            )
        if evidence.sample_count < self._policy.minimum_evidence_samples:
            return (
                "DIRECTION.EVIDENCE_INSUFFICIENT",
                "Недостаточно подтверждающих отсчётов внешнего датчика.",
            )
        if evidence.quality_score < self._policy.minimum_evidence_quality:
            return (
                "DIRECTION.EVIDENCE_LOW_QUALITY",
                "Качество подтверждения ниже допустимого; направление скрыто.",
            )
        if (
            not math.isfinite(confidence)
            or confidence < self._policy.minimum_external_confidence
            or confidence > 1.0
        ):
            return (
                "DIRECTION.CONFIDENCE_TOO_LOW",
                "Уверенность внешнего измерения недостаточна.",
            )
        for timestamp, code, message in (
            (
                captured_at,
                "DIRECTION.SAMPLE_FROM_FUTURE",
                "Время измерения некорректно; направление скрыто.",
            ),
            (
                evidence.evidence_at,
                "DIRECTION.EVIDENCE_FROM_FUTURE",
                "Время подтверждения некорректно; направление скрыто.",
            ),
            (
                evidence.calibrated_at,
                "DIRECTION.CALIBRATION_FROM_FUTURE",
                "Время калибровки некорректно; направление скрыто.",
            ),
        ):
            if (
                timestamp.tzinfo is None
                or timestamp.utcoffset() is None
                or (timestamp - now).total_seconds() > self._policy.future_tolerance_s
            ):
                return code, message
        sample_age = (now - captured_at).total_seconds()
        evidence_age = (now - evidence.evidence_at).total_seconds()
        calibration_age = (captured_at - evidence.calibrated_at).total_seconds()
        if sample_age > self._policy.external_max_age_s:
            return (
                "DIRECTION.SAMPLE_STALE",
                "Измерение внешнего датчика устарело; направление скрыто.",
            )
        if evidence_age > self._policy.external_max_age_s:
            return (
                "DIRECTION.EVIDENCE_STALE",
                "Подтверждение внешнего датчика устарело; направление скрыто.",
            )
        if (
            calibration_age < -self._policy.future_tolerance_s
            or calibration_age > self._policy.maximum_calibration_age_s
        ):
            return (
                "DIRECTION.CALIBRATION_STALE",
                "Калибровка внешнего датчика устарела; направление скрыто.",
            )
        return None

    def _validate_not_future(self, captured_at: datetime, now: datetime) -> None:
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        if (captured_at - now).total_seconds() > self._policy.future_tolerance_s:
            raise ValueError("captured_at is too far in the future")

    def _maximum_age_for(self, source: DirectionSource) -> float:
        if source is DirectionSource.MANUAL:
            return self._policy.manual_max_age_s
        if source is DirectionSource.SIMULATED:
            return self._policy.simulated_max_age_s
        return self._policy.external_max_age_s

    @staticmethod
    def _normalize_bearing(value: float) -> float:
        bearing = float(value)
        if not math.isfinite(bearing):
            raise ValueError("bearing_deg must be finite")
        return bearing % 360.0

    @staticmethod
    def _quality_for(confidence: float) -> DirectionQuality:
        if confidence >= 0.80:
            return DirectionQuality.HIGH
        if confidence >= 0.65:
            return DirectionQuality.MEDIUM
        return DirectionQuality.LOW

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now


__all__ = ["DirectionPolicy", "DirectionService"]
