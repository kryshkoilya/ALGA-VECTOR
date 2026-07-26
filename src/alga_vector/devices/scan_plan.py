"""Capability-gated, receive-only spectrum scan plans.

This module schedules generic spectrum observation.  A frequency range is not
a source signature: detections still need observable RF evidence and temporal
classification elsewhere in the application.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from threading import RLock

from alga_vector.domain.errors import AppError

from .capabilities import CaptureTopology, ReceiverHardwareProfile
from .tuning import (
    RTLSDR_SAMPLE_RATE_RANGES_HZ,
    RtlSdrTuningProfile,
    validate_rtlsdr_tuning,
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MINIMUM_SCAN_WIDTH_HZ = 1_000


class ScanPlanLimitationSeverity(StrEnum):
    """Operator-facing importance of one plan limitation."""

    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class ScanRange:
    """One generic requested observation interval."""

    range_id: str
    label_ru: str
    start_frequency_hz: int
    stop_frequency_hz: int

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.range_id) is None:
            raise ValueError(
                "range_id must use 1..64 lowercase ASCII letters, digits, '_' or '-'"
            )
        if not self.label_ru.strip():
            raise ValueError("scan range label must not be empty")
        if self.start_frequency_hz < 1:
            raise ValueError("scan range start_frequency_hz must be positive")
        if self.stop_frequency_hz - self.start_frequency_hz < _MINIMUM_SCAN_WIDTH_HZ:
            raise ValueError("scan range width must be at least 1000 Hz")

    @property
    def width_hz(self) -> int:
        return self.stop_frequency_hz - self.start_frequency_hz


@dataclass(frozen=True, slots=True)
class ScanPlanPreset:
    """A source-neutral navigation preset."""

    preset_id: str
    label_ru: str
    note_ru: str
    ranges: tuple[ScanRange, ...]

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.preset_id) is None:
            raise ValueError("preset_id has an invalid format")
        if not self.label_ru.strip() or not self.note_ru.strip():
            raise ValueError("preset label and note must not be empty")
        if not self.ranges:
            raise ValueError("scan preset must contain at least one range")


# Broad engineering regions only.  They are not signal-source labels and are
# deliberately independent of country, operator, platform or intended use.
GENERAL_SCAN_PRESETS: tuple[ScanPlanPreset, ...] = (
    ScanPlanPreset(
        preset_id="general_vhf",
        label_ru="Общий обзор VHF · 30–300 МГц",
        note_ru="Широкий обзор участка спектра без предположения об источнике.",
        ranges=(
            ScanRange("general_vhf", "VHF · общий участок", 30_000_000, 300_000_000),
        ),
    ),
    ScanPlanPreset(
        preset_id="general_uhf",
        label_ru="Общий обзор UHF · 300–1000 МГц",
        note_ru="Широкий обзор участка спектра без предположения об источнике.",
        ranges=(
            ScanRange("general_uhf", "UHF · общий участок", 300_000_000, 1_000_000_000),
        ),
    ),
    ScanPlanPreset(
        preset_id="general_l_band",
        label_ru="Общий обзор L · 1–2 ГГц",
        note_ru="Широкий обзор участка спектра без предположения об источнике.",
        ranges=(
            ScanRange("general_l_band", "L · общий участок", 1_000_000_000, 2_000_000_000),
        ),
    ),
    ScanPlanPreset(
        preset_id="general_s_band",
        label_ru="Общий обзор S · 2–4 ГГц",
        note_ru="Широкий обзор участка спектра без предположения об источнике.",
        ranges=(
            ScanRange("general_s_band", "S · общий участок", 2_000_000_000, 4_000_000_000),
        ),
    ),
    ScanPlanPreset(
        preset_id="general_c_band",
        label_ru="Общий обзор C · 4–6 ГГц",
        note_ru="Широкий обзор участка спектра без предположения об источнике.",
        ranges=(
            ScanRange("general_c_band", "C · общий участок", 4_000_000_000, 6_000_000_000),
        ),
    ),
    ScanPlanPreset(
        preset_id="general_wide",
        label_ru="Широкий общий обзор · 30 МГц–6 ГГц",
        note_ru=(
            "Последовательный обзор общих участков; фактическое покрытие всегда "
            "ограничивается подтверждёнными возможностями приёмника."
        ),
        ranges=(
            ScanRange("wide_vhf", "VHF · общий участок", 30_000_000, 300_000_000),
            ScanRange("wide_uhf", "UHF · общий участок", 300_000_000, 1_000_000_000),
            ScanRange("wide_l", "L · общий участок", 1_000_000_000, 2_000_000_000),
            ScanRange("wide_s", "S · общий участок", 2_000_000_000, 4_000_000_000),
            ScanRange("wide_c", "C · общий участок", 4_000_000_000, 6_000_000_000),
        ),
    ),
)

_PRESETS_BY_ID = {preset.preset_id: preset for preset in GENERAL_SCAN_PRESETS}


@dataclass(frozen=True, slots=True)
class ScanPlanRequest:
    """Validated operator request before hardware gating."""

    plan_id: str
    ranges: tuple[ScanRange, ...]
    window_span_hz: int = 2_000_000
    overlap_fraction: float = 0.10
    dwell_time_ms: int = 120
    dwell_frames: int = 12
    retune_settle_ms: int = 35
    maximum_windows: int = 4_096

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.plan_id) is None:
            raise ValueError("plan_id has an invalid format")
        if not self.ranges:
            raise ValueError("scan plan must contain at least one range")
        identifiers = [item.range_id for item in self.ranges]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("scan range ids must be unique")
        ordered = sorted(
            self.ranges,
            key=lambda item: (item.start_frequency_hz, item.stop_frequency_hz),
        )
        if any(
            current.stop_frequency_hz > following.start_frequency_hz
            for current, following in pairwise(ordered)
        ):
            raise ValueError("scan ranges must not overlap")
        if self.window_span_hz < _MINIMUM_SCAN_WIDTH_HZ:
            raise ValueError("window_span_hz must be at least 1000 Hz")
        if not 0.0 <= self.overlap_fraction < 0.75:
            raise ValueError("overlap_fraction must be in range 0.0..<0.75")
        if not 1 <= self.dwell_time_ms <= 60_000:
            raise ValueError("dwell_time_ms must be in range 1..60000")
        if not 3 <= self.dwell_frames <= 1_000:
            raise ValueError("dwell_frames must be in range 3..1000")
        if not 0 <= self.retune_settle_ms <= 60_000:
            raise ValueError("retune_settle_ms must be in range 0..60000")
        if not 1 <= self.maximum_windows <= 100_000:
            raise ValueError("maximum_windows must be in range 1..100000")


@dataclass(frozen=True, slots=True)
class ScanWindow:
    """One hardware-valid tuning window in a cyclic schedule."""

    window_id: str
    ordinal: int
    range_id: str
    label_ru: str
    start_frequency_hz: int
    stop_frequency_hz: int
    center_frequency_hz: int
    span_hz: int
    dwell_frames: int


@dataclass(frozen=True, slots=True)
class ExcludedScanRange:
    """Requested range that could not be scheduled on this receiver."""

    requested: ScanRange
    code: str
    reason_ru: str


@dataclass(frozen=True, slots=True)
class ScanPlanLimitation:
    """Stable, explainable limitation suitable for runtime and UI."""

    code: str
    severity: ScanPlanLimitationSeverity
    message_ru: str
    operator_action_ru: str


@dataclass(frozen=True, slots=True)
class CompiledScanPlan:
    """Immutable plan containing hardware-valid windows only."""

    plan_id: str
    profile_id: str
    capture_topology: CaptureTopology
    requested_ranges: tuple[ScanRange, ...]
    covered_ranges: tuple[ScanRange, ...]
    excluded_ranges: tuple[ExcludedScanRange, ...]
    windows: tuple[ScanWindow, ...]
    limitations: tuple[ScanPlanLimitation, ...]
    dwell_time_ms: int
    retune_settle_ms: int
    estimated_cycle_ms: int
    coverage_fraction: float

    @property
    def accepted(self) -> bool:
        return bool(self.windows) and not any(
            item.severity == ScanPlanLimitationSeverity.BLOCKING
            for item in self.limitations
        )

    @property
    def sequential(self) -> bool:
        return len(self.windows) > 1 or self.capture_topology == CaptureTopology.SWEPT

    def window_for_sequence(self, sequence: int) -> ScanWindow:
        if not self.accepted:
            raise AppError(
                code="SCAN_PLAN.NOT_RUNNABLE",
                message_ru="План обзора не прошёл аппаратную проверку.",
                operator_action_ru="Исправьте блокирующие ограничения плана.",
                retryable=False,
                technical_details={"plan_id": self.plan_id},
            )
        return self.windows[sequence % len(self.windows)]


@dataclass(frozen=True, slots=True)
class ScanWindowResult:
    """Last explicitly reported acquisition result."""

    window_id: str
    success: bool
    recorded_at: datetime
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class ScanPlanCursorStatus:
    """Small immutable status for structured logs and presentation."""

    plan_id: str
    current_ordinal: int
    pending_window_id: str | None
    successful_frames_in_window: int
    completed_attempts: int
    completed_windows: int
    completed_cycles: int
    failed_windows: int
    last_result: ScanWindowResult | None


class ScanPlanCursor:
    """Thread-safe cyclic cursor that advances only after ``mark_result``."""

    def __init__(
        self,
        plan: CompiledScanPlan,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not plan.accepted:
            raise ValueError("scan cursor requires an accepted compiled plan")
        self._plan = plan
        self._clock = clock or (lambda: datetime.now(UTC))
        self._index = 0
        self._pending: ScanWindow | None = None
        self._successful_frames_in_window = 0
        self._completed_attempts = 0
        self._completed_windows = 0
        self._completed_cycles = 0
        self._failed_windows = 0
        self._last_result: ScanWindowResult | None = None
        self._lock = RLock()

    @property
    def plan(self) -> CompiledScanPlan:
        return self._plan

    def next_window(self) -> ScanWindow:
        """Return the current window, retaining it until a result is marked."""

        with self._lock:
            if self._pending is None:
                self._pending = self._plan.windows[self._index]
            return self._pending

    def mark_result(
        self,
        success: bool,
        *,
        detail_code: str | None = None,
    ) -> ScanWindowResult:
        """Record one attempt; advance after the window's successful dwell."""

        with self._lock:
            if self._pending is None:
                raise AppError(
                    code="SCAN_PLAN.NO_PENDING_WINDOW",
                    message_ru="Нельзя записать результат: окно обзора ещё не выдано.",
                    operator_action_ru="Сначала запросите next_window().",
                    retryable=False,
                    technical_details={"plan_id": self._plan.plan_id},
                )
            if detail_code is not None and not detail_code.strip():
                raise ValueError("detail_code must be non-empty when provided")
            result = ScanWindowResult(
                window_id=self._pending.window_id,
                success=success,
                recorded_at=self._clock(),
                detail_code=detail_code,
            )
            self._last_result = result
            self._completed_attempts += 1
            if not success:
                self._failed_windows += 1
            self._pending = None
            if success:
                self._successful_frames_in_window += 1
            if self._successful_frames_in_window >= self._plan.windows[
                self._index
            ].dwell_frames:
                self._successful_frames_in_window = 0
                self._completed_windows += 1
                self._index += 1
                if self._index == len(self._plan.windows):
                    self._index = 0
                    self._completed_cycles += 1
            return result

    def snapshot(self) -> ScanPlanCursorStatus:
        with self._lock:
            return ScanPlanCursorStatus(
                plan_id=self._plan.plan_id,
                current_ordinal=self._index,
                pending_window_id=(
                    self._pending.window_id if self._pending is not None else None
                ),
                successful_frames_in_window=self._successful_frames_in_window,
                completed_attempts=self._completed_attempts,
                completed_windows=self._completed_windows,
                completed_cycles=self._completed_cycles,
                failed_windows=self._failed_windows,
                last_result=self._last_result,
            )


@dataclass(frozen=True, slots=True)
class _SupportedInterval:
    start_frequency_hz: int
    stop_frequency_hz: int


@dataclass(frozen=True, slots=True)
class _CompilationContext:
    profile_id: str
    capture_topology: CaptureTopology
    intervals: tuple[_SupportedInterval, ...]
    maximum_window_span_hz: int
    validate_window: Callable[[int, int], tuple[bool, str | None, str | None]]
    blocking_limitation: ScanPlanLimitation | None = None


def scan_request_from_preset(
    preset_id: str,
    *,
    window_span_hz: int = 2_000_000,
    overlap_fraction: float = 0.10,
    dwell_time_ms: int = 120,
    dwell_frames: int = 12,
    retune_settle_ms: int = 35,
    maximum_windows: int = 4_096,
) -> ScanPlanRequest:
    """Build a request from one stable general-purpose preset ID."""

    try:
        preset = _PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"unknown general scan preset: {preset_id}") from exc
    return ScanPlanRequest(
        plan_id=f"preset_{preset.preset_id}",
        ranges=preset.ranges,
        window_span_hz=window_span_hz,
        overlap_fraction=overlap_fraction,
        dwell_time_ms=dwell_time_ms,
        dwell_frames=dwell_frames,
        retune_settle_ms=retune_settle_ms,
        maximum_windows=maximum_windows,
    )


def full_supported_scan_request(
    profile: RtlSdrTuningProfile | ReceiverHardwareProfile,
    *,
    window_span_hz: int = 2_000_000,
    overlap_fraction: float = 0.10,
    dwell_time_ms: int = 120,
    dwell_frames: int = 12,
    retune_settle_ms: int = 35,
    maximum_windows: int = 4_096,
) -> ScanPlanRequest:
    """Request the declared receive envelope without inventing extra coverage."""

    intervals: tuple[tuple[int, int, str], ...]
    if isinstance(profile, RtlSdrTuningProfile):
        intervals = (
            (
                profile.minimum_frequency_hz,
                profile.maximum_frequency_hz,
                "Подтверждённый диапазон RTL-SDR",
            ),
        )
    else:
        intervals = tuple(
            (
                band.minimum_frequency_hz,
                band.maximum_frequency_hz,
                band.mode_label_ru,
            )
            for band in profile.tuning_bands
        )
    ranges = tuple(
        ScanRange(
            range_id=f"supported_{index + 1}",
            label_ru=label,
            start_frequency_hz=start,
            stop_frequency_hz=stop,
        )
        for index, (start, stop, label) in enumerate(intervals)
    )
    return ScanPlanRequest(
        plan_id="full_supported",
        ranges=ranges,
        window_span_hz=window_span_hz,
        overlap_fraction=overlap_fraction,
        dwell_time_ms=dwell_time_ms,
        dwell_frames=dwell_frames,
        retune_settle_ms=retune_settle_ms,
        maximum_windows=maximum_windows,
    )


def compile_scan_plan(
    profile: RtlSdrTuningProfile | ReceiverHardwareProfile,
    request: ScanPlanRequest,
    *,
    sample_rate_hz: int | None = None,
) -> CompiledScanPlan:
    """Compile only intersections that the declared receiver can observe."""

    context = _compilation_context(
        profile,
        requested_span_hz=request.window_span_hz,
        sample_rate_hz=sample_rate_hz,
    )
    limitations: list[ScanPlanLimitation] = [_frequency_is_not_identity_limitation()]
    if context.blocking_limitation is not None:
        limitations.append(context.blocking_limitation)
        return _empty_plan(request, context, limitations)

    covered_ranges: list[ScanRange] = []
    excluded_ranges: list[ExcludedScanRange] = []
    coverage_width_hz = 0
    clipped = False
    for requested in request.ranges:
        intersections = _intersections(requested, context.intervals)
        if not intersections:
            excluded_ranges.append(
                ExcludedScanRange(
                    requested=requested,
                    code="SCAN_PLAN.RANGE_OUTSIDE_DEVICE",
                    reason_ru=(
                        "Запрошенный участок полностью вне подтверждённого "
                        "диапазона этого приёмника."
                    ),
                )
            )
            clipped = True
            continue
        for part_index, (start_hz, stop_hz) in enumerate(intersections):
            part_id = (
                requested.range_id
                if len(intersections) == 1
                else f"{requested.range_id}-part-{part_index + 1}"
            )
            covered = ScanRange(
                range_id=part_id,
                label_ru=requested.label_ru,
                start_frequency_hz=start_hz,
                stop_frequency_hz=stop_hz,
            )
            covered_ranges.append(covered)
            coverage_width_hz += covered.width_hz
        if sum(stop - start for start, stop in intersections) < requested.width_hz:
            clipped = True

    if excluded_ranges:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.UNSUPPORTED_RANGES_EXCLUDED",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=(
                    "Часть запрошенных участков исключена: приёмник не подтверждает "
                    "работу на этих частотах."
                ),
                operator_action_ru=(
                    "Используйте другой подтверждённый приёмник либо уменьшите план."
                ),
            )
        )
    if clipped:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.COVERAGE_CLIPPED_TO_HARDWARE",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=(
                    "Фактическое покрытие ограничено аппаратным диапазоном; "
                    "неподдерживаемые частоты не будут показаны как измеренные."
                ),
                operator_action_ru="Проверьте покрытые и исключённые участки плана.",
            )
        )
    if context.maximum_window_span_hz < request.window_span_hz:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.WINDOW_SPAN_CAPPED",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=(
                    "Размер одного окна уменьшен до мгновенной полосы приёмника."
                ),
                operator_action_ru=(
                    "Учитывайте большее число перестроек и более долгий цикл обзора."
                ),
            )
        )

    windows: list[ScanWindow] = []
    hardware_warnings: set[str] = set()
    for covered in covered_ranges:
        for start_hz, stop_hz in _window_edges(
            covered.start_frequency_hz,
            covered.stop_frequency_hz,
            span_hz=context.maximum_window_span_hz,
            overlap_fraction=request.overlap_fraction,
        ):
            center_hz, actual_span_hz = _center_and_even_span(start_hz, stop_hz)
            accepted, code, warning = context.validate_window(
                center_hz,
                actual_span_hz,
            )
            if not accepted:
                limitations.append(
                    ScanPlanLimitation(
                        code=code or "SCAN_PLAN.HARDWARE_VALIDATION_FAILED",
                        severity=ScanPlanLimitationSeverity.BLOCKING,
                        message_ru=(
                            "Аппаратная проверка отклонила одно из окон плана."
                        ),
                        operator_action_ru=(
                            "Измените границы, полосу или профиль приёмника."
                        ),
                    )
                )
                return _empty_plan(
                    request,
                    context,
                    limitations,
                    covered_ranges=tuple(covered_ranges),
                    excluded_ranges=tuple(excluded_ranges),
                    coverage_width_hz=coverage_width_hz,
                )
            if warning:
                hardware_warnings.add(warning)
            ordinal = len(windows)
            windows.append(
                ScanWindow(
                    window_id=f"{covered.range_id}_{start_hz}_{stop_hz}",
                    ordinal=ordinal,
                    range_id=covered.range_id,
                    label_ru=covered.label_ru,
                    start_frequency_hz=start_hz,
                    stop_frequency_hz=stop_hz,
                    center_frequency_hz=center_hz,
                    span_hz=actual_span_hz,
                    dwell_frames=request.dwell_frames,
                )
            )
            if len(windows) > request.maximum_windows:
                limitations.append(
                    ScanPlanLimitation(
                        code="SCAN_PLAN.TOO_MANY_WINDOWS",
                        severity=ScanPlanLimitationSeverity.BLOCKING,
                        message_ru=(
                            "План требует больше окон, чем разрешено защитным лимитом."
                        ),
                        operator_action_ru=(
                            "Увеличьте полосу окна, сократите диапазоны или явно "
                            "увеличьте maximum_windows после оценки нагрузки."
                        ),
                    )
                )
                return _empty_plan(
                    request,
                    context,
                    limitations,
                    covered_ranges=tuple(covered_ranges),
                    excluded_ranges=tuple(excluded_ranges),
                    coverage_width_hz=coverage_width_hz,
                )

    for warning in sorted(hardware_warnings):
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.HARDWARE_WARNING",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=warning,
                operator_action_ru="Учитывайте аппаратное ограничение при интерпретации.",
            )
        )

    estimated_cycle_ms = len(windows) * (
        request.dwell_frames * request.dwell_time_ms
        + request.retune_settle_ms
    )
    if len(windows) > 1 or context.capture_topology == CaptureTopology.SWEPT:
        limitations.append(_sequential_sweep_limitation())
    if estimated_cycle_ms > 2_000:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.LONG_REVISIT_INTERVAL",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=(
                    f"Оценочный повторный визит к участку — до "
                    f"{estimated_cycle_ms / 1_000:.1f} с."
                ),
                operator_action_ru=(
                    "Сократите план или используйте несколько независимых приёмников "
                    "для более частого наблюдения."
                ),
            )
        )
    if request.dwell_frames < 11:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.DWELL_BELOW_TEMPORAL_RECOMMENDATION",
                severity=ScanPlanLimitationSeverity.WARNING,
                message_ru=(
                    "На окно выделено меньше 11 успешных кадров: этого может не "
                    "хватить для базовой линии и временного подтверждения."
                ),
                operator_action_ru=(
                    "Для стандартного temporal pipeline используйте dwell_frames=12."
                ),
            )
        )
    if not windows:
        limitations.append(
            ScanPlanLimitation(
                code="SCAN_PLAN.NO_SUPPORTED_COVERAGE",
                severity=ScanPlanLimitationSeverity.BLOCKING,
                message_ru="Приёмник не поддерживает ни одного участка плана.",
                operator_action_ru="Выберите совместимый профиль или другой диапазон.",
            )
        )

    requested_width_hz = sum(item.width_hz for item in request.ranges)
    return CompiledScanPlan(
        plan_id=request.plan_id,
        profile_id=context.profile_id,
        capture_topology=context.capture_topology,
        requested_ranges=request.ranges,
        covered_ranges=tuple(covered_ranges),
        excluded_ranges=tuple(excluded_ranges),
        windows=tuple(windows),
        limitations=tuple(_deduplicate_limitations(limitations)),
        dwell_time_ms=request.dwell_time_ms,
        retune_settle_ms=request.retune_settle_ms,
        estimated_cycle_ms=estimated_cycle_ms,
        coverage_fraction=min(1.0, coverage_width_hz / requested_width_hz),
    )


def _compilation_context(
    profile: RtlSdrTuningProfile | ReceiverHardwareProfile,
    *,
    requested_span_hz: int,
    sample_rate_hz: int | None,
) -> _CompilationContext:
    if isinstance(profile, RtlSdrTuningProfile):
        blocking = None
        if sample_rate_hz is None:
            blocking = _sample_rate_required_limitation()
            maximum_span_hz = requested_span_hz
        elif not any(
            minimum <= sample_rate_hz <= maximum
            for minimum, maximum in RTLSDR_SAMPLE_RATE_RANGES_HZ
        ):
            blocking = ScanPlanLimitation(
                code="SPECTRUM.RTLSDR_SAMPLE_RATE_UNSUPPORTED",
                severity=ScanPlanLimitationSeverity.BLOCKING,
                message_ru="RTL-SDR не поддерживает указанную частоту дискретизации.",
                operator_action_ru="Выберите подтверждённую частоту дискретизации.",
            )
            maximum_span_hz = requested_span_hz
        else:
            maximum_span_hz = min(requested_span_hz, sample_rate_hz)

        def validate_rtlsdr_window(
            center_hz: int,
            span_hz: int,
        ) -> tuple[bool, str | None, str | None]:
            if sample_rate_hz is None:
                return False, "SPECTRUM.SAMPLE_RATE_REQUIRED", None
            result = validate_rtlsdr_tuning(
                profile,
                center_frequency_hz=center_hz,
                span_hz=span_hz,
                sample_rate_hz=sample_rate_hz,
            )
            return result.accepted, result.code, result.warning_ru

        return _CompilationContext(
            profile_id=profile.profile_id,
            capture_topology=CaptureTopology.IQ,
            intervals=(
                _SupportedInterval(
                    profile.minimum_frequency_hz,
                    profile.maximum_frequency_hz,
                ),
            ),
            maximum_window_span_hz=maximum_span_hz,
            validate_window=validate_rtlsdr_window,
            blocking_limitation=blocking,
        )

    blocking = None
    maximum_span_hz = requested_span_hz
    if profile.capture_topology == CaptureTopology.IQ:
        if sample_rate_hz is None:
            blocking = _sample_rate_required_limitation()
        elif (
            profile.minimum_sample_rate_hz is None
            or profile.maximum_sample_rate_hz is None
            or not (
                profile.minimum_sample_rate_hz
                <= sample_rate_hz
                <= profile.maximum_sample_rate_hz
            )
        ):
            blocking = ScanPlanLimitation(
                code="SPECTRUM.SAMPLE_RATE_UNSUPPORTED",
                severity=ScanPlanLimitationSeverity.BLOCKING,
                message_ru="Приёмник не поддерживает указанную частоту дискретизации.",
                operator_action_ru="Выберите значение из аппаратного диапазона.",
            )
        else:
            maximum_span_hz = min(
                requested_span_hz,
                sample_rate_hz,
                profile.maximum_instantaneous_span_hz or sample_rate_hz,
            )

    def validate_receiver_window(
        center_hz: int,
        span_hz: int,
    ) -> tuple[bool, str | None, str | None]:
        result = profile.validate_tuning(
            center_frequency_hz=center_hz,
            span_hz=span_hz,
            sample_rate_hz=sample_rate_hz,
        )
        return result.accepted, result.code, result.warning_ru

    return _CompilationContext(
        profile_id=profile.profile_id,
        capture_topology=profile.capture_topology,
        intervals=tuple(
            _SupportedInterval(
                band.minimum_frequency_hz,
                band.maximum_frequency_hz,
            )
            for band in profile.tuning_bands
        ),
        maximum_window_span_hz=maximum_span_hz,
        validate_window=validate_receiver_window,
        blocking_limitation=blocking,
    )


def _intersections(
    requested: ScanRange,
    intervals: tuple[_SupportedInterval, ...],
) -> tuple[tuple[int, int], ...]:
    matches: list[tuple[int, int]] = []
    for interval in intervals:
        start_hz = max(requested.start_frequency_hz, interval.start_frequency_hz)
        stop_hz = min(requested.stop_frequency_hz, interval.stop_frequency_hz)
        if stop_hz - start_hz >= _MINIMUM_SCAN_WIDTH_HZ:
            matches.append((start_hz, stop_hz))
    return tuple(matches)


def _window_edges(
    start_hz: int,
    stop_hz: int,
    *,
    span_hz: int,
    overlap_fraction: float,
) -> tuple[tuple[int, int], ...]:
    width_hz = stop_hz - start_hz
    if width_hz <= span_hz:
        return ((start_hz, stop_hz),)
    step_hz = max(1, int(span_hz * (1.0 - overlap_fraction)))
    output: list[tuple[int, int]] = []
    cursor_hz = start_hz
    while cursor_hz + span_hz < stop_hz:
        output.append((cursor_hz, cursor_hz + span_hz))
        next_hz = cursor_hz + step_hz
        final_start_hz = stop_hz - span_hz
        cursor_hz = min(next_hz, final_start_hz)
        if output[-1][0] == cursor_hz:
            break
    final = (stop_hz - span_hz, stop_hz)
    if not output or output[-1] != final:
        output.append(final)
    return tuple(output)


def _center_and_even_span(start_hz: int, stop_hz: int) -> tuple[int, int]:
    center_hz = (start_hz + stop_hz) // 2
    span_hz = 2 * min(center_hz - start_hz, stop_hz - center_hz)
    return center_hz, span_hz


def _sample_rate_required_limitation() -> ScanPlanLimitation:
    return ScanPlanLimitation(
        code="SPECTRUM.SAMPLE_RATE_REQUIRED",
        severity=ScanPlanLimitationSeverity.BLOCKING,
        message_ru="Для IQ-приёмника нужна подтверждённая частота дискретизации.",
        operator_action_ru="Перед компиляцией плана укажите sample_rate_hz.",
    )


def _frequency_is_not_identity_limitation() -> ScanPlanLimitation:
    return ScanPlanLimitation(
        code="SCAN_PLAN.FREQUENCY_IS_NOT_SOURCE_IDENTITY",
        severity=ScanPlanLimitationSeverity.INFO,
        message_ru=(
            "Наличие энергии на частоте само по себе не определяет тип, назначение "
            "или оператора источника."
        ),
        operator_action_ru=(
            "Используйте форму сигнала, время, повторяемость и независимые сенсоры."
        ),
    )


def _sequential_sweep_limitation() -> ScanPlanLimitation:
    return ScanPlanLimitation(
        code="SCAN_PLAN.SEQUENTIAL_SWEEP_MAY_MISS_SHORT_EVENTS",
        severity=ScanPlanLimitationSeverity.WARNING,
        message_ru=(
            "Участки просматриваются последовательно, не одновременно; короткий "
            "эпизод между визитами может быть пропущен."
        ),
        operator_action_ru=(
            "Сократите покрытие, увеличьте мгновенную полосу или используйте "
            "несколько независимых приёмников."
        ),
    )


def _empty_plan(
    request: ScanPlanRequest,
    context: _CompilationContext,
    limitations: list[ScanPlanLimitation],
    *,
    covered_ranges: tuple[ScanRange, ...] = (),
    excluded_ranges: tuple[ExcludedScanRange, ...] = (),
    coverage_width_hz: int = 0,
) -> CompiledScanPlan:
    requested_width_hz = sum(item.width_hz for item in request.ranges)
    return CompiledScanPlan(
        plan_id=request.plan_id,
        profile_id=context.profile_id,
        capture_topology=context.capture_topology,
        requested_ranges=request.ranges,
        covered_ranges=covered_ranges,
        excluded_ranges=excluded_ranges,
        windows=(),
        limitations=tuple(_deduplicate_limitations(limitations)),
        dwell_time_ms=request.dwell_time_ms,
        retune_settle_ms=request.retune_settle_ms,
        estimated_cycle_ms=0,
        coverage_fraction=min(1.0, coverage_width_hz / requested_width_hz),
    )


def _deduplicate_limitations(
    limitations: list[ScanPlanLimitation],
) -> list[ScanPlanLimitation]:
    output: list[ScanPlanLimitation] = []
    seen: set[tuple[str, str]] = set()
    for item in limitations:
        identity = (item.code, item.message_ru)
        if identity not in seen:
            output.append(item)
            seen.add(identity)
    return output


__all__ = [
    "GENERAL_SCAN_PRESETS",
    "CompiledScanPlan",
    "ExcludedScanRange",
    "ScanPlanCursor",
    "ScanPlanCursorStatus",
    "ScanPlanLimitation",
    "ScanPlanLimitationSeverity",
    "ScanPlanPreset",
    "ScanPlanRequest",
    "ScanRange",
    "ScanWindow",
    "ScanWindowResult",
    "compile_scan_plan",
    "full_supported_scan_request",
    "scan_request_from_preset",
]
