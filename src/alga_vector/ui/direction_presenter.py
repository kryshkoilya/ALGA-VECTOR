"""Honest direction and received-level context for the operator UI.

The received-level trend is intentionally scoped to the level measured at the
receiver input.  It never estimates range, approach, coordinates, or emitter
location.  A bearing is marked as measured only when the direction domain
model exposes a fresh, validated external-DF observation.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from alga_vector.direction import DirectionSnapshot, DirectionSource

from .runtime import attr, value_of

RANGE_LIMITATION_RU = (
    "Расстояние не определяется: RSSI зависит от мощности, антенны и трассы."
)


@dataclass(frozen=True, slots=True)
class BearingView:
    """Safe operator projection of one direction snapshot."""

    value: str
    state: str
    detail: str
    level: str
    measured: bool
    simulated: bool


@dataclass(frozen=True, slots=True)
class ReceivedLevelTrendView:
    """Measured receiver-level context without a range interpretation."""

    state: str
    value: str
    detail: str
    level: str
    sample_count: int
    slope_db_per_frame: float | None


def present_bearing(snapshot: DirectionSnapshot) -> BearingView:
    """Expose measurement provenance without upgrading manual/demo bearings."""

    current = snapshot.current
    if (
        snapshot.available
        and not snapshot.stale
        and current.source is DirectionSource.EXTERNAL
        and current.measured
        and current.evidence is not None
        and current.bearing_deg is not None
    ):
        return BearingView(
            value=f"{current.bearing_deg:05.1f}°",
            state="ИЗМЕРЕНО · ВНЕШНИЙ DF",
            detail=(
                "Свежий азимут принят от валидированного внешнего "
                "пеленгатора."
            ),
            level="ready",
            measured=True,
            simulated=False,
        )
    if (
        snapshot.available
        and not snapshot.stale
        and current.source is DirectionSource.SIMULATED
        and current.bearing_deg is not None
    ):
        return BearingView(
            value=f"{current.bearing_deg:05.1f}° · ДЕМО",
            state="СИМУЛЯЦИЯ · НЕ ИЗМЕРЕНИЕ",
            detail=(
                "Учебный азимут создан Demo-режимом и не получен "
                "от физического DF-датчика."
            ),
            level="warning",
            measured=False,
            simulated=True,
        )
    if (
        snapshot.available
        and not snapshot.stale
        and current.source is DirectionSource.MANUAL
        and current.bearing_deg is not None
    ):
        return BearingView(
            value=f"{current.bearing_deg:05.1f}° · ВВОД",
            state="ВВОД ОПЕРАТОРА · НЕ ИЗМЕРЕНИЕ",
            detail=(
                "Ручная угловая отметка показана отдельно и не считается "
                "измерением системы."
            ),
            level="info",
            measured=False,
            simulated=False,
        )
    state = "ДАННЫЕ УСТАРЕЛИ" if snapshot.stale else "НЕТ ИСТОЧНИКА"
    return BearingView(
        value="—",
        state=state,
        detail=(
            "Измеренный азимут появляется только от свежего "
            "валидированного внешнего DF."
        ),
        level="warning" if snapshot.stale else "neutral",
        measured=False,
        simulated=False,
    )


class ReceivedLevelTrendPresenter:
    """Maintain a bounded, deduplicated trend of measured RF peak levels."""

    def __init__(
        self,
        *,
        window_size: int = 5,
        minimum_samples: int = 3,
        stable_threshold_db_per_frame: float = 0.75,
    ) -> None:
        if window_size < 3:
            raise ValueError("window_size must be at least 3")
        if minimum_samples < 3 or minimum_samples > window_size:
            raise ValueError("minimum_samples must be within [3, window_size]")
        if (
            not math.isfinite(stable_threshold_db_per_frame)
            or stable_threshold_db_per_frame <= 0.0
        ):
            raise ValueError(
                "stable_threshold_db_per_frame must be finite and positive"
            )
        self._levels: deque[float] = deque(maxlen=window_size)
        self._minimum_samples = minimum_samples
        self._stable_threshold = stable_threshold_db_per_frame
        self._source_id: str | None = None
        self._spectral_grid: tuple[int, int] | None = None
        self._last_key: tuple[str, int, object] | None = None

    def present(self, snapshot: object | None) -> ReceivedLevelTrendView:
        """Consume at most one unique valid frame and return a safe projection."""

        assessment = attr(snapshot, "signal_assessment")
        assessment_state = value_of(
            attr(assessment, "state", "no_data")
        ).lower()
        if assessment_state == "data_unreliable":
            self.reset()
            return self._unavailable(
                "Кадр RF отклонён проверками качества; тренд сброшен.",
                level="critical",
            )

        frame = attr(snapshot, "spectrum")
        if frame is None:
            self.reset()
            return self._unavailable(
                "Свежего измеренного RF-уровня пока нет.",
            )

        source_id = str(attr(frame, "source_id", "")).strip()
        sequence = _as_int(attr(frame, "sequence"))
        captured_at = attr(frame, "captured_at")
        center_frequency_hz = _as_positive_int(
            attr(frame, "center_frequency_hz")
        )
        span_hz = _as_positive_int(attr(frame, "span_hz"))
        level = _peak_level(frame)
        unit = str(attr(frame, "unit", "")).strip()
        if (
            not source_id
            or sequence is None
            or captured_at is None
            or center_frequency_hz is None
            or span_hz is None
            or level is None
            or not unit
        ):
            self.reset()
            return self._unavailable(
                "RF-кадр не содержит полного набора измерений для тренда.",
                level="critical",
            )

        if self._source_id is not None and source_id != self._source_id:
            self.reset()
        self._source_id = source_id
        spectral_grid = (center_frequency_hz, span_hz)
        if (
            self._spectral_grid is not None
            and spectral_grid != self._spectral_grid
        ):
            # Levels from different tuning windows are not directly
            # comparable.  A planned scan retune must start a new trend.
            self.reset()
            self._source_id = source_id
        self._spectral_grid = spectral_grid
        key = (source_id, sequence, captured_at)
        if (
            self._last_key is not None
            and key != self._last_key
            and sequence <= self._last_key[1]
        ):
            self.reset()
            self._source_id = source_id
        if key != self._last_key:
            self._levels.append(level)
            self._last_key = key

        count = len(self._levels)
        measured = f"{level:.1f} {unit}"
        if count < self._minimum_samples:
            return ReceivedLevelTrendView(
                state="НАКОПЛЕНИЕ",
                value="НАКОПЛЕНИЕ",
                detail=(
                    f"Принятый RF-уровень: {measured}. "
                    f"Нужно ещё {self._minimum_samples - count} "
                    f"{_frame_noun(self._minimum_samples - count)}. "
                    f"{RANGE_LIMITATION_RU}"
                ),
                level="info",
                sample_count=count,
                slope_db_per_frame=None,
            )

        slope = _linear_slope(tuple(self._levels))
        if slope > self._stable_threshold:
            state = "РАСТЁТ"
            level_name = "warning"
        elif slope < -self._stable_threshold:
            state = "ПАДАЕТ"
            level_name = "info"
        else:
            state = "СТАБИЛЕН"
            level_name = "ready"
        return ReceivedLevelTrendView(
            state=state,
            value=state,
            detail=(
                f"Принятый RF-уровень: {measured}; "
                f"измеренный тренд: {slope:+.2f} dB/кадр. "
                f"Это изменение уровня на входе приёмника без "
                f"пространственной интерпретации. "
                f"{RANGE_LIMITATION_RU}"
            ),
            level=level_name,
            sample_count=count,
            slope_db_per_frame=slope,
        )

    def reset(self) -> None:
        self._levels.clear()
        self._source_id = None
        self._spectral_grid = None
        self._last_key = None

    @staticmethod
    def _unavailable(
        reason: str,
        *,
        level: str = "neutral",
    ) -> ReceivedLevelTrendView:
        return ReceivedLevelTrendView(
            state="НЕТ ДАННЫХ",
            value="НЕТ ДАННЫХ",
            detail=f"{reason} {RANGE_LIMITATION_RU}",
            level=level,
            sample_count=0,
            slope_db_per_frame=None,
        )


def _peak_level(frame: object) -> float | None:
    direct = attr(frame, "peak_level")
    if direct is not None:
        try:
            value = float(cast(Any, direct))
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) else None
    raw = attr(frame, "power_dbm")
    if raw is None:
        return None
    try:
        values = tuple(float(item) for item in cast(Any, raw))
    except (TypeError, ValueError, OverflowError):
        return None
    if not values or any(not math.isfinite(item) for item in values):
        return None
    return max(values)


def _as_int(value: object | None) -> int | None:
    try:
        result = int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _as_positive_int(value: object | None) -> int | None:
    result = _as_int(value)
    return result if result is not None and result > 0 else None


def _linear_slope(values: tuple[float, ...]) -> float:
    count = len(values)
    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    if denominator == 0.0:
        return 0.0
    numerator = sum(
        (index - mean_x) * (value - mean_y)
        for index, value in enumerate(values)
    )
    return numerator / denominator


def _frame_noun(count: int) -> str:
    if count == 1:
        return "кадр"
    if count in {2, 3, 4}:
        return "кадра"
    return "кадров"


__all__ = [
    "RANGE_LIMITATION_RU",
    "BearingView",
    "ReceivedLevelTrendPresenter",
    "ReceivedLevelTrendView",
    "present_bearing",
]
