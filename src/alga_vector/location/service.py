# ruff: noqa: RUF001

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock

from .geometry import haversine_distance_m
from .models import (
    GeoPoint,
    GpsFixDimension,
    GpsFixState,
    LocationFix,
    LocationPolicy,
    LocationSnapshot,
    LocationSource,
    LocationStatus,
)
from .nmea import NmeaNoFixError, NmeaRecord, parse_nmea_sentence

Clock = Callable[[], datetime]


class LocationService:
    """Validate local positioning without exposing or transmitting coordinates."""

    def __init__(
        self,
        policy: LocationPolicy | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy or LocationPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._manual_base: GeoPoint | None = None
        self._samples: list[LocationFix] = []
        self._snapshot = LocationSnapshot(
            status=LocationStatus.UNSET,
            message_ru="База не задана. Абсолютная геопривязка отключена.",
        )

    def current_snapshot(self) -> LocationSnapshot:
        with self._lock:
            return self._refresh_locked(self._now())

    snapshot = current_snapshot

    def clear(self) -> LocationSnapshot:
        with self._lock:
            self._manual_base = None
            self._samples.clear()
            self._snapshot = LocationSnapshot(
                status=LocationStatus.UNSET,
                gps_fix_state=GpsFixState.DISCONNECTED,
                message_ru="База удалена. Абсолютная геопривязка отключена.",
            )
            return self._snapshot

    def begin_collection(self) -> LocationSnapshot:
        with self._lock:
            self._samples.clear()
            self._snapshot = LocationSnapshot(
                status=LocationStatus.COLLECTING,
                base=self._manual_base,
                source=(
                    LocationSource.MANUAL if self._manual_base is not None else None
                ),
                gps_fix_state=GpsFixState.SEARCHING,
                message_ru="Собираются GPS-измерения для проверки неподвижной базы.",
            )
            return self._snapshot

    def set_manual_base(
        self,
        point: GeoPoint,
        *,
        captured_at: datetime | None = None,
    ) -> LocationSnapshot:
        if not isinstance(point, GeoPoint):
            raise TypeError("point must be a GeoPoint")
        timestamp = self._normalize_time(captured_at or self._now())
        with self._lock:
            self._manual_base = point
            self._samples.clear()
            self._snapshot = LocationSnapshot(
                status=LocationStatus.MANUAL_UNVERIFIED,
                base=point,
                source=LocationSource.MANUAL,
                captured_at=timestamp,
                gps_fix_state=GpsFixState.DISCONNECTED,
                message_ru=(
                    "Ручная точка сохранена локально, но не подтверждена "
                    "независимым GPS-источником."
                ),
                warnings_ru=(
                    "При ошибочной точке абсолютные наложения будут смещены.",
                ),
            )
            return self._snapshot

    def ingest_nmea(
        self,
        sentence: str | bytes,
        *,
        received_at: datetime | None = None,
    ) -> LocationSnapshot:
        received = self._normalize_time(received_at or self._now())
        try:
            record: NmeaRecord = parse_nmea_sentence(sentence, received_at=received)
        except NmeaNoFixError:
            return self.report_no_fix(received_at=received)
        if record.fix is None:
            return self.report_fix_dimension(
                record.fix_dimension,
                received_at=received,
            )
        return self.ingest(record.fix, now=received)

    def report_fix_dimension(
        self,
        dimension: GpsFixDimension,
        *,
        received_at: datetime | None = None,
    ) -> LocationSnapshot:
        """Record a checksum-valid GSA state without inventing a position."""

        if not isinstance(dimension, GpsFixDimension):
            raise TypeError("dimension must be a GpsFixDimension")
        received = self._normalize_time(received_at or self._now())
        if dimension is GpsFixDimension.NONE:
            return self.report_no_fix(received_at=received)
        state = _fix_state(dimension)
        with self._lock:
            message = self._snapshot.message_ru
            if self._snapshot.status is LocationStatus.COLLECTING:
                label = "3D" if dimension is GpsFixDimension.THREE_D else "2D"
                message = (
                    f"GPS сообщает {label}-фиксацию; ожидаются качественные "
                    "координатные GGA-измерения."
                )
            self._snapshot = replace(
                self._snapshot,
                gps_fix_state=state,
                fix_dimension=dimension,
                last_receiver_at=received,
                message_ru=message,
            )
            return self._snapshot

    def report_no_fix(
        self,
        *,
        received_at: datetime | None = None,
    ) -> LocationSnapshot:
        """Fail closed when a valid NMEA record says that no fix exists."""

        received = self._normalize_time(received_at or self._now())
        with self._lock:
            previous = self._snapshot
            self._samples.clear()
            status = (
                LocationStatus.STALE
                if previous.status is LocationStatus.VERIFIED
                else LocationStatus.COLLECTING
            )
            self._snapshot = LocationSnapshot(
                status=status,
                base=previous.base or self._manual_base,
                source=previous.source,
                captured_at=previous.captured_at,
                horizontal_accuracy_m=previous.horizontal_accuracy_m,
                accuracy_is_estimate=previous.accuracy_is_estimate,
                sample_count=0,
                gps_fix_state=GpsFixState.NO_FIX,
                fix_dimension=GpsFixDimension.NONE,
                last_receiver_at=received,
                message_ru="GPS работает, но сейчас сообщает: фиксации нет.",
                warnings_ru=(
                    "Геопривязка по GPS приостановлена. Проверьте антенну и обзор неба.",
                    "При необходимости явно переключитесь на ручную базу.",
                ),
            )
            return self._snapshot

    def ingest(
        self,
        fix: LocationFix,
        *,
        now: datetime | None = None,
    ) -> LocationSnapshot:
        if not isinstance(fix, LocationFix):
            raise TypeError("fix must be a LocationFix")
        current = self._normalize_time(now or self._now())
        with self._lock:
            temporal_issue = self._temporal_issue(fix, current)
            if temporal_issue is not None:
                self._snapshot = LocationSnapshot(
                    status=LocationStatus.STALE,
                    base=self._snapshot.base or self._manual_base,
                    source=fix.source,
                    captured_at=fix.captured_at,
                    sample_count=len(self._samples),
                    gps_fix_state=GpsFixState.STALE,
                    fix_dimension=self._snapshot.fix_dimension,
                    last_receiver_at=current,
                    message_ru=temporal_issue,
                    warnings_ru=("Абсолютная геопривязка приостановлена.",),
                )
                return self._snapshot

            quality_issue = self._quality_issue(fix)
            if quality_issue is not None:
                self._snapshot = LocationSnapshot(
                    status=LocationStatus.COLLECTING,
                    base=self._manual_base,
                    source=fix.source,
                    captured_at=fix.captured_at,
                    sample_count=len(self._samples),
                    gps_fix_state=_fix_state(self._snapshot.fix_dimension),
                    fix_dimension=self._snapshot.fix_dimension,
                    last_receiver_at=current,
                    message_ru=quality_issue,
                    warnings_ru=("Измерение не использовано для подтверждения базы.",),
                )
                return self._snapshot

            self._prune_samples(current)
            jump_issue = self._jump_issue(fix)
            if jump_issue is not None:
                self._snapshot = LocationSnapshot(
                    status=LocationStatus.JUMP_SUSPECTED,
                    base=self._snapshot.base or self._manual_base,
                    source=fix.source,
                    captured_at=fix.captured_at,
                    horizontal_accuracy_m=self._snapshot.horizontal_accuracy_m,
                    accuracy_is_estimate=self._snapshot.accuracy_is_estimate,
                    sample_count=len(self._samples),
                    gps_fix_state=GpsFixState.JUMP_SUSPECTED,
                    fix_dimension=self._snapshot.fix_dimension,
                    last_receiver_at=current,
                    message_ru=jump_issue,
                    warnings_ru=(
                        "Скачок не принят и не изменил положение базы.",
                        "Проверьте GPS, антенну и повторите проверку неподвижной базы.",
                    ),
                )
                return self._snapshot
            self._samples.append(fix)
            return self._evaluate_samples(current)

    def refresh(self, *, now: datetime | None = None) -> LocationSnapshot:
        current = self._normalize_time(now or self._now())
        with self._lock:
            return self._refresh_locked(current)

    def _evaluate_samples(self, current: datetime) -> LocationSnapshot:
        sample_count = len(self._samples)
        latest = self._samples[-1]
        if sample_count < self.policy.minimum_samples:
            self._snapshot = LocationSnapshot(
                status=LocationStatus.COLLECTING,
                base=self._manual_base,
                source=latest.source,
                captured_at=latest.captured_at,
                sample_count=sample_count,
                gps_fix_state=_fix_state(self._snapshot.fix_dimension),
                fix_dimension=self._snapshot.fix_dimension,
                last_receiver_at=current,
                message_ru=(
                    f"Качественных GPS-измерений: {sample_count} из "
                    f"{self.policy.minimum_samples}."
                ),
            )
            return self._snapshot

        candidate = _median_point(self._samples)
        radius = max(
            haversine_distance_m(candidate, sample.point)
            for sample in self._samples
        )
        if radius > self.policy.maximum_stationary_radius_m:
            self._snapshot = LocationSnapshot(
                status=LocationStatus.CONFLICT,
                base=self._manual_base,
                source=latest.source,
                captured_at=latest.captured_at,
                sample_count=sample_count,
                gps_fix_state=_fix_state(self._snapshot.fix_dimension),
                fix_dimension=self._snapshot.fix_dimension,
                last_receiver_at=current,
                message_ru=(
                    "GPS-измерения не подтверждают неподвижную базу: "
                    "разброс превышает допустимый радиус."
                ),
                warnings_ru=("Проверьте антенну, обзор неба и положение базы.",),
            )
            return self._snapshot

        if self._manual_base is not None:
            difference = haversine_distance_m(self._manual_base, candidate)
            if difference > self.policy.manual_conflict_distance_m:
                self._snapshot = LocationSnapshot(
                    status=LocationStatus.CONFLICT,
                    base=self._manual_base,
                    source=latest.source,
                    captured_at=latest.captured_at,
                    sample_count=sample_count,
                    gps_fix_state=_fix_state(self._snapshot.fix_dimension),
                    fix_dimension=self._snapshot.fix_dimension,
                    last_receiver_at=current,
                    message_ru=(
                        "Ручная точка и GPS расходятся. Система не выбирает "
                        "«правильную» точку автоматически."
                    ),
                    warnings_ru=(
                        "Повторно проверьте ручной ввод и GPS перед геопривязкой.",
                    ),
                )
                return self._snapshot

        median_hdop = statistics.median(
            sample.hdop for sample in self._samples if sample.hdop is not None
        )
        estimated_accuracy = max(radius, float(median_hdop) * 5.0)
        self._snapshot = LocationSnapshot(
            status=LocationStatus.VERIFIED,
            base=candidate,
            source=latest.source,
            captured_at=latest.captured_at,
            horizontal_accuracy_m=estimated_accuracy,
            accuracy_is_estimate=True,
            sample_count=sample_count,
            gps_fix_state=_fix_state(self._snapshot.fix_dimension),
            fix_dimension=self._snapshot.fix_dimension,
            last_receiver_at=current,
            message_ru="Неподвижная база подтверждена серией локальных GPS-измерений.",
            warnings_ru=(
                "Точность оценена по HDOP и разбросу; это не прямое измерение приёмника.",
            ),
        )
        self._prune_samples(current)
        return self._snapshot

    def _refresh_locked(self, current: datetime) -> LocationSnapshot:
        captured_at = self._snapshot.captured_at
        if (
            self._snapshot.status
            in {LocationStatus.VERIFIED, LocationStatus.COLLECTING}
            and captured_at is not None
            and (current - captured_at).total_seconds()
            > self.policy.maximum_fix_age_s
        ):
            self._snapshot = LocationSnapshot(
                status=LocationStatus.STALE,
                base=self._snapshot.base,
                source=self._snapshot.source,
                captured_at=captured_at,
                horizontal_accuracy_m=self._snapshot.horizontal_accuracy_m,
                accuracy_is_estimate=self._snapshot.accuracy_is_estimate,
                sample_count=self._snapshot.sample_count,
                gps_fix_state=GpsFixState.STALE,
                fix_dimension=self._snapshot.fix_dimension,
                last_receiver_at=self._snapshot.last_receiver_at,
                message_ru="GPS-данные устарели. Абсолютная геопривязка приостановлена.",
            )
        return self._snapshot

    def _temporal_issue(self, fix: LocationFix, current: datetime) -> str | None:
        age = (current - fix.captured_at).total_seconds()
        if age > self.policy.maximum_fix_age_s:
            return "Получено устаревшее GPS-измерение."
        if age < -self.policy.maximum_future_skew_s:
            return "Время GPS заметно опережает системное время."
        return None

    def _jump_issue(self, fix: LocationFix) -> str | None:
        reference_point: GeoPoint | None = None
        reference_time: datetime | None = None
        verified_reference = False
        if self._samples:
            previous = self._samples[-1]
            reference_point = previous.point
            reference_time = previous.captured_at
        elif (
            self._snapshot.base is not None
            and self._snapshot.source is not LocationSource.MANUAL
            and self._snapshot.status
            in {
                LocationStatus.VERIFIED,
                LocationStatus.STALE,
                LocationStatus.JUMP_SUSPECTED,
            }
        ):
            reference_point = self._snapshot.base
            reference_time = self._snapshot.captured_at
            verified_reference = True
        if reference_point is None:
            return None
        distance = haversine_distance_m(reference_point, fix.point)
        if distance <= self.policy.maximum_jump_distance_m:
            return None
        elapsed_s = (
            (fix.captured_at - reference_time).total_seconds()
            if reference_time is not None
            else 0.0
        )
        speed = distance / max(elapsed_s, 0.001)
        if not verified_reference and speed <= self.policy.maximum_jump_speed_m_s:
            return None
        return (
            "GPS сообщил резкий скачок положения, несовместимый с "
            "неподвижной базой."
        )

    def _quality_issue(self, fix: LocationFix) -> str | None:
        if fix.hdop is None or fix.satellites is None:
            return "GPS-измерение не содержит HDOP и число спутников."
        if fix.hdop > self.policy.maximum_hdop:
            return "HDOP превышает допустимый порог."
        if fix.satellites < self.policy.minimum_satellites:
            return "Недостаточно спутников для подтверждения базы."
        if (
            fix.speed_m_s is not None
            and fix.speed_m_s > self.policy.maximum_stationary_speed_m_s
        ):
            return "GPS сообщает движение; база должна быть неподвижна."
        return None

    def _prune_samples(self, current: datetime) -> None:
        threshold = current - timedelta(seconds=self.policy.sample_window_s)
        self._samples = [
            sample for sample in self._samples if sample.captured_at >= threshold
        ]

    def _now(self) -> datetime:
        return self._normalize_time(self._clock())

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time must be timezone-aware")
        return value.astimezone(UTC)


def _median_point(samples: list[LocationFix]) -> GeoPoint:
    latitude = statistics.median(sample.point.latitude_deg for sample in samples)
    longitude = statistics.median(sample.point.longitude_deg for sample in samples)
    altitudes = [
        sample.point.altitude_m
        for sample in samples
        if sample.point.altitude_m is not None
    ]
    altitude = statistics.median(altitudes) if altitudes else None
    return GeoPoint(latitude, longitude, altitude)


def _fix_state(dimension: GpsFixDimension) -> GpsFixState:
    return {
        GpsFixDimension.TWO_D: GpsFixState.FIX_2D,
        GpsFixDimension.THREE_D: GpsFixState.FIX_3D,
    }.get(dimension, GpsFixState.FIX)


__all__ = ["LocationService"]
