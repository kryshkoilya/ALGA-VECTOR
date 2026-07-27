# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Literal
from uuid import uuid4

from alga_vector.acoustics import AcousticAssessment, PcmWindow
from alga_vector.airspace import CivilAirspaceSnapshot
from alga_vector.config.models import AdapterConfig, AppConfig
from alga_vector.devices import (
    BLOG_V4_PROFILE,
    GENERAL_SCAN_PRESETS,
    GENERIC_RTLSDR_PROFILE,
    HACKRF_ONE_PROFILE,
    CaptureTopology,
    DeviceManagerLike,
    HackRfDiscoveryResult,
    HackRfDiscoveryService,
    ReceiverHardwareProfile,
    RtlSdrDiscoveryResult,
    RtlSdrDiscoveryService,
    RtlSdrTuningProfile,
    ScanPlanPreset,
    ScanWindow,
    TinySaDiscoveryResult,
    TinySaModel,
    TinySaSerialDiscoveryService,
    build_device_manager,
    compile_scan_plan,
    full_supported_scan_request,
    has_enabled_real_hardware,
    rtlsdr_profile_by_id,
    scan_request_from_preset,
    tinysa_hardware_profile,
    validate_rtlsdr_tuning,
)
from alga_vector.devices.tuning import BLOG_V3_DIRECT_Q_PROFILE
from alga_vector.direction import (
    DirectionService,
    DirectionSnapshot,
    ExternalDirectionEvidence,
)
from alga_vector.domain.enums import (
    Capability,
    CapabilityState,
    DeviceState,
    HealthLevel,
    IncidentSeverity,
    Provenance,
)
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import (
    CapabilityStatus,
    DeviceSnapshot,
    Incident,
    SpectrumFrame,
    SystemSnapshot,
    utc_now,
)
from alga_vector.location import (
    GeoPoint,
    GpsPortCandidate,
    GpsReceiverError,
    LocationPolicy,
    LocationService,
    LocationSource,
    NmeaSerialReceiver,
    SecureLocationStore,
    SecureStoreError,
    discover_nmea_port_candidates,
)
from alga_vector.maps import (
    MapAvailability,
    MapCatalog,
    MapCatalogError,
    MapSnapshot,
    MBTilesError,
    OfflineMapService,
    OnlineMapSnapshot,
    OnlineMapState,
    OnlineTileService,
)
from alga_vector.observability import HealthAggregator, JsonlRotatingLogger
from alga_vector.signal_analysis import (
    DecisionTransition,
    FrameValidationError,
    QualityFlag,
    RfDecision,
    RfDecisionEngine,
    RfEventDetector,
    SignalAssessment,
    SourceObservationMetadata,
    SpectrumAcquisitionMode,
    assessment_with_data_age,
    data_unreliable_assessment,
    no_data_assessment,
)
from alga_vector.signal_processor import (
    NormalizedEvent,
    PublishResult,
    UnifiedEventBus,
    UnifiedSignalProcessor,
)
from alga_vector.storage import (
    EventJournal,
    SpectrumCaptureError,
    SpectrumCaptureResult,
    SpectrumCaptureStatus,
    SpectrumCaptureWriter,
    prune_spectrum_captures,
)
from alga_vector.support import SupportBundleBuilder
from alga_vector.targets import TargetAggregator, TargetAggregatorConfig

from .multisensor import MultiSensorCoordinator
from .rf_scan import (
    FrequencyScopedRfPipelinePool,
    RfScanSession,
    ScanRuntimeStatus,
)

Clock = Callable[[], datetime]
ConfigSaver = Callable[[AppConfig], None]

_REQUIRED_CAPABILITIES = frozenset(
    {
        Capability.SPECTRUM_SWEEP,
    }
)
_DEFAULT_ACQUISITION_PERIOD_SECONDS = 0.05
_DEFAULT_ACQUISITION_REFRESH_SECONDS = 2.0
_DEFAULT_ACQUISITION_STALE_SECONDS = 5.0
_ACQUISITION_JOIN_TIMEOUT_SECONDS = 1.0
_SCAN_MAXIMUM_WINDOWS = 1_024
_SCAN_DETECTOR_HISTORY_FRAMES = 16
_RTLSDR_CONNECTION_RE = re.compile(r"(?i)^RTLSDR:(\d{1,3})$")
_HACKRF_CONNECTION_RE = re.compile(r"(?i)^HACKRF:[0-9a-f]{8,64}$")
_COM_CONNECTION_RE = re.compile(r"(?i)^COM(?:[1-9]|[1-9]\d|[12]\d\d)$")


class RuntimeState(StrEnum):
    NEW = "new"
    RUNNING = "running"
    DRAINING = "draining"
    CLOSED = "closed"


class ApplicationRuntime:
    """UI-independent coordinator with optional continuous hardware acquisition."""

    def __init__(
        self,
        config: AppConfig,
        *,
        device_manager: DeviceManagerLike | None = None,
        rtl_discovery_service: RtlSdrDiscoveryService | None = None,
        hackrf_discovery_service: HackRfDiscoveryService | None = None,
        tinysa_discovery_service: TinySaSerialDiscoveryService | None = None,
        online_map_service: OnlineTileService | None = None,
        direction_service: DirectionService | None = None,
        signal_processor: UnifiedSignalProcessor | None = None,
        journal: EventJournal | None = None,
        event_logger: JsonlRotatingLogger | None = None,
        config_saver: ConfigSaver | None = None,
        startup_warnings: tuple[AppError, ...] = (),
        mode_lock: str | None = None,
        clock: Clock = utc_now,
        background_acquisition: bool | None = None,
        acquisition_period_seconds: float = _DEFAULT_ACQUISITION_PERIOD_SECONDS,
        acquisition_refresh_seconds: float = _DEFAULT_ACQUISITION_REFRESH_SECONDS,
        acquisition_stale_seconds: float = _DEFAULT_ACQUISITION_STALE_SECONDS,
    ) -> None:
        for value, name in (
            (acquisition_period_seconds, "acquisition_period_seconds"),
            (acquisition_refresh_seconds, "acquisition_refresh_seconds"),
            (acquisition_stale_seconds, "acquisition_stale_seconds"),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.config = config
        self._config_saver = config_saver
        self._mode_lock = mode_lock
        self._clock = clock
        self._lock = RLock()
        self._device_lock = RLock()
        self._acquisition_lock = RLock()
        self._state = RuntimeState.NEW
        self._revision = 0
        self._latest: SystemSnapshot | None = None
        self._active_incidents: dict[str, Incident] = {}
        self._acknowledged_incidents: set[str] = set()
        self._journaled_incidents: set[str] = set()
        self._startup_faults: list[tuple[str, str]] = []
        self._startup_warnings = startup_warnings
        self._spectrum_failure_active = False
        self._spectrum_failure_episode = 0
        self._capture_fault: tuple[str, str] | None = None
        self._signal_detector = RfEventDetector()
        self._signal_decision_engine = RfDecisionEngine()
        self._signal_events: deque[RfDecision] = deque(maxlen=64)
        self._signal_decision: RfDecision | None = None
        self._signal_source_id: str | None = None
        self._rf_scan_session: RfScanSession | None = None
        self._rf_scan_pipelines: FrequencyScopedRfPipelinePool | None = None
        self._rf_scan_last_window_id: str | None = None
        self._rf_scan_warmup_window_ids: set[str] = set()
        self._fixed_tuning_warmup_pending = False
        self._acquisition_transition_pending = False
        self._rf_scan_forced_background = False
        self._multisensor = MultiSensorCoordinator(config, clock=clock)
        if signal_processor is None:
            tracking = config.target_tracking
            target_aggregator = TargetAggregator(
                TargetAggregatorConfig(
                    correlation_window_seconds=(
                        tracking.correlation_window_seconds
                    ),
                    deduplication_window_seconds=(
                        tracking.deduplication_window_seconds
                    ),
                    decay_half_life_seconds=tracking.decay_half_life_seconds,
                    stale_after_seconds=tracking.stale_after_seconds,
                    retire_after_seconds=tracking.retire_after_seconds,
                    maximum_active_targets=tracking.maximum_active_targets,
                )
            )
            self._signal_processor = UnifiedSignalProcessor(
                target_aggregator=target_aggregator
            )
        else:
            self._signal_processor = signal_processor
        self._last_operator_situation_key: tuple[str, str | None] | None = None
        self._signal_assessment = no_data_assessment(
            self._clock(),
            reason_code="SIGNAL.NOT_STARTED",
            explanation_ru="Получение спектра ещё не запущено.",
            operator_action_ru="Запустите систему и дождитесь первого кадра приёмника.",
            baseline_required_frames=self._signal_detector.config.min_history_frames,
        )
        self._acquisition_period_seconds = acquisition_period_seconds
        self._acquisition_refresh_seconds = acquisition_refresh_seconds
        self._acquisition_stale_seconds = acquisition_stale_seconds
        self._background_acquisition_override = background_acquisition
        self._manager_owned_for_acquisition = device_manager is None
        self._rtl_discovery_service = (
            rtl_discovery_service or RtlSdrDiscoveryService()
        )
        self._hackrf_discovery_service = (
            hackrf_discovery_service or HackRfDiscoveryService()
        )
        self._tinysa_discovery_service = (
            tinysa_discovery_service or TinySaSerialDiscoveryService()
        )
        self._background_acquisition_enabled = False
        self._acquisition_thread: Thread | None = None
        self._acquisition_stop_event: Event | None = None
        self._acquisition_sequence = 0
        self._acquisition_bins = 512
        self._acquisition_center_frequency_hz = config.spectrum.center_frequency_hz
        self._acquisition_span_hz = config.spectrum.span_hz
        self._acquisition_latest_frame: SpectrumFrame | None = None
        self._acquisition_latest_monotonic: float | None = None
        self._acquisition_failure: tuple[str, str] | None = None
        self._last_consumed_frame_key: tuple[str, int, datetime] | None = None
        self._direction_service = direction_service or DirectionService(
            demo_mode=config.mode == "demo",
            clock=clock,
        )
        self._location_service = LocationService(
            _location_policy(config),
            clock=clock,
        )
        self._location_store: SecureLocationStore | None = None
        self._gps_receiver: NmeaSerialReceiver | None = None
        self._gps_started_at: datetime | None = None
        self._map_service = OfflineMapService(cache_mib=config.map.tile_cache_mib)
        self._online_map_service = online_map_service or OnlineTileService(
            config.storage.data_dir / "maps" / "online-cache",
            network_enabled=config.map.network_enabled,
            cache_mib=config.map.online_cache_mib,
        )
        self._capture_writer = SpectrumCaptureWriter(
            config.storage.data_dir / "captures",
            clock=clock,
        )
        self._initialize_location_store(config.storage.data_dir)
        self._initialize_map(config)
        self._device_manager = device_manager or build_device_manager(
            config,
            clock=clock,
        )
        self._background_acquisition_enabled = self._should_use_background_acquisition(
            config,
            manager_owned=self._manager_owned_for_acquisition,
        )
        self._journal = journal
        self._event_logger = event_logger
        self._owns_journal = journal is None
        self._owns_logger = event_logger is None
        if journal is None:
            self._journal = self._open_default_journal(config.storage.data_dir)
        if self._journal is not None and not self._journal.closed:
            try:
                self._acknowledged_incidents = {
                    item.incident_id
                    for item in self._journal.list_incidents(
                        limit=10_000,
                        acknowledged=True,
                    )
                }
            except (OSError, RuntimeError, sqlite3.Error):
                self._acknowledged_incidents = set()
            try:
                self._signal_events.extend(
                    self._journal.list_rf_decisions(limit=64)
                )
            except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._startup_faults.append(
                    (
                        "STORAGE.RF_HISTORY_READ_FAILED",
                        f"История RF-решений не прочитана: {type(exc).__name__}.",
                    )
                )
        if event_logger is None:
            self._event_logger = self._open_default_logger(config.storage.data_dir)
        self._health = HealthAggregator(_REQUIRED_CAPABILITIES)

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def closed(self) -> bool:
        return self._state == RuntimeState.CLOSED

    @property
    def journal(self) -> EventJournal | None:
        return self._journal

    @property
    def operator_event_bus(self) -> UnifiedEventBus:
        """Return the normalized, bounded event bus for optional adapters."""

        return self._signal_processor.event_bus

    @property
    def logger_path(self) -> Path | None:
        return self._event_logger.path if self._event_logger is not None else None

    @property
    def latest_snapshot(self) -> SystemSnapshot | None:
        return self._latest

    @property
    def background_acquisition_enabled(self) -> bool:
        return self._background_acquisition_enabled

    @property
    def acquisition_running(self) -> bool:
        thread = self._acquisition_thread
        return thread is not None and thread.is_alive()

    @property
    def scan_plan_presets(self) -> tuple[ScanPlanPreset, ...]:
        """Return source-neutral plan choices for the operator UI."""

        return GENERAL_SCAN_PRESETS

    def scan_plan_status(self) -> ScanRuntimeStatus | None:
        with self._acquisition_lock:
            session = self._rf_scan_session
            return session.status() if session is not None else None

    def start_scan_plan(self, preset_id: str) -> ScanRuntimeStatus:
        """Compile and start a bounded cyclic plan for the active receiver."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            if self._state == RuntimeState.NEW:
                self.start()
            with self._acquisition_lock:
                if self._capture_writer.active:
                    raise AppError(
                        code="SCAN_PLAN.CAPTURE_ACTIVE",
                        message_ru="Автообзор нельзя менять во время записи спектра.",
                        operator_action_ru="Завершите запись и повторите действие.",
                        retryable=False,
                    )
            try:
                with self._device_lock:
                    snapshots = self._device_manager.snapshots()
            except (AppError, RuntimeError, ValueError):
                snapshots = ()
            profile, adapter_id = _scan_profile_for_runtime(
                self.config,
                snapshots,
            )
            is_iq = (
                isinstance(profile, RtlSdrTuningProfile)
                or profile.capture_topology is CaptureTopology.IQ
            )
            window_span_hz = (
                self.config.spectrum.sample_rate_hz
                if is_iq
                else self.config.spectrum.span_hz
            )
            request_kwargs = {
                "window_span_hz": window_span_hz,
                "dwell_time_ms": max(
                    50,
                    round(self._acquisition_period_seconds * 1_000),
                ),
                "dwell_frames": 12,
                "retune_settle_ms": 35,
                "maximum_windows": _SCAN_MAXIMUM_WINDOWS,
            }
            if preset_id == "full_supported":
                request = full_supported_scan_request(
                    profile,
                    **request_kwargs,
                )
            else:
                try:
                    request = scan_request_from_preset(
                        preset_id,
                        **request_kwargs,
                    )
                except ValueError as exc:
                    raise AppError(
                        code="SCAN_PLAN.PRESET_UNKNOWN",
                        message_ru="Выбран неизвестный профиль автообзора.",
                        operator_action_ru=(
                            "Обновите список профилей и выберите доступный участок."
                        ),
                        retryable=False,
                        technical_details={"preset_id": preset_id},
                    ) from exc
            plan = compile_scan_plan(
                profile,
                request,
                sample_rate_hz=(
                    self.config.spectrum.sample_rate_hz
                    if is_iq
                    else None
                ),
            )
            if not plan.accepted:
                blocking = next(
                    (
                        limitation
                        for limitation in plan.limitations
                        if limitation.severity.value == "blocking"
                    ),
                    None,
                )
                raise AppError(
                    code=(
                        blocking.code
                        if blocking is not None
                        else "SCAN_PLAN.NOT_RUNNABLE"
                    ),
                    message_ru=(
                        blocking.message_ru
                        if blocking is not None
                        else "План автообзора не прошёл аппаратную проверку."
                    ),
                    operator_action_ru=(
                        blocking.operator_action_ru
                        if blocking is not None
                        else "Выберите меньший диапазон или другой приёмник."
                    ),
                    retryable=False,
                    technical_details={
                        "preset_id": preset_id,
                        "profile_id": plan.profile_id,
                        "window_count": len(plan.windows),
                    },
                )
            if self.acquisition_running and not self._stop_acquisition_thread():
                raise AppError(
                    code="SCAN_PLAN.ACQUISITION_BUSY",
                    message_ru="Фоновый приём не остановился для смены плана.",
                    operator_action_ru="Повторите действие или перезапустите приложение.",
                    retryable=True,
                )
            with self._acquisition_lock:
                scan_detector_config = replace(
                    self._signal_detector.config,
                    history_frames=max(
                        self._signal_detector.config.min_history_frames,
                        _SCAN_DETECTOR_HISTORY_FRAMES,
                    ),
                    max_sources=1,
                )
                scan_decision_config = replace(
                    self._signal_decision_engine.config,
                    maximum_sources=1,
                )
                self._rf_scan_session = RfScanSession(
                    plan,
                    source_id=adapter_id,
                    clock=self._clock,
                )
                self._rf_scan_pipelines = FrequencyScopedRfPipelinePool(
                    maximum_pipelines=len(plan.windows),
                    detector_config=scan_detector_config,
                    decision_config=scan_decision_config,
                )
                self._rf_scan_last_window_id = None
                self._rf_scan_warmup_window_ids.clear()
                self._fixed_tuning_warmup_pending = False
                self._acquisition_transition_pending = True
                self._signal_detector.reset()
                self._signal_decision_engine.reset()
                self._signal_decision = None
                self._signal_source_id = None
                self._signal_assessment = no_data_assessment(
                    self._clock(),
                    reason_code="SCAN_PLAN.STARTED",
                    explanation_ru=(
                        "Автообзор запущен; каждое окно сначала изучает собственный фон."
                    ),
                    operator_action_ru=(
                        "Оставьте приёмник включённым и учитывайте время полного цикла."
                    ),
                    baseline_required_frames=(
                        self._signal_detector.config.min_history_frames
                    ),
                )
                self._acquisition_latest_frame = None
                self._acquisition_latest_monotonic = None
                self._acquisition_failure = None
                self._last_consumed_frame_key = None
                self._spectrum_failure_active = False
            if (
                not self._background_acquisition_enabled
                and self._manager_owned_for_acquisition
            ):
                self._rf_scan_forced_background = True
                self._background_acquisition_enabled = True
            self._latest = None
            self._log(
                "scan_plan.started",
                "Запущен аппаратно проверенный циклический обзор спектра.",
                preset_id=preset_id,
                plan_id=plan.plan_id,
                adapter_id=adapter_id,
                profile_id=plan.profile_id,
                windows=len(plan.windows),
                estimated_cycle_ms=plan.estimated_cycle_ms,
                coverage_fraction=plan.coverage_fraction,
                limitation_codes=[
                    limitation.code for limitation in plan.limitations
                ],
            )
            self._start_acquisition_if_enabled()
            return self.scan_plan_status() or self._scan_status_unavailable()

    def stop_scan_plan(self) -> str:
        """Stop cyclic retuning and return to the configured fixed window."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            if self.acquisition_running and not self._stop_acquisition_thread():
                raise AppError(
                    code="SCAN_PLAN.ACQUISITION_BUSY",
                    message_ru="Фоновый приём не остановился для выключения автообзора.",
                    operator_action_ru="Повторите действие или перезапустите приложение.",
                    retryable=True,
                )
            with self._acquisition_lock:
                had_session = self._rf_scan_session is not None
                self._rf_scan_session = None
                if self._rf_scan_pipelines is not None:
                    self._rf_scan_pipelines.reset()
                self._rf_scan_pipelines = None
                self._rf_scan_last_window_id = None
                self._rf_scan_warmup_window_ids.clear()
                self._fixed_tuning_warmup_pending = had_session
                self._acquisition_transition_pending = had_session
                self._signal_detector.reset()
                self._signal_decision_engine.reset()
                self._signal_decision = None
                self._signal_source_id = None
                self._signal_assessment = no_data_assessment(
                    self._clock(),
                    reason_code="SCAN_PLAN.STOPPED",
                    explanation_ru=(
                        "Автообзор остановлен; приёмник вернулся к выбранному окну."
                    ),
                    operator_action_ru=(
                        "Дождитесь повторного изучения фона фиксированного диапазона."
                    ),
                    baseline_required_frames=(
                        self._signal_detector.config.min_history_frames
                    ),
                )
                self._acquisition_latest_frame = None
                self._acquisition_latest_monotonic = None
                self._acquisition_failure = None
                self._last_consumed_frame_key = None
                self._spectrum_failure_active = False
            self._acquisition_center_frequency_hz = (
                self.config.spectrum.center_frequency_hz
            )
            self._acquisition_span_hz = self.config.spectrum.span_hz
            self._background_acquisition_enabled = (
                self._should_use_background_acquisition(
                    self.config,
                    manager_owned=self._manager_owned_for_acquisition,
                )
            )
            self._rf_scan_forced_background = False
            self._latest = None
            if had_session:
                self._log(
                    "scan_plan.stopped",
                    "Циклический обзор остановлен; восстановлено фиксированное окно.",
                )
            self._start_acquisition_if_enabled()
            return (
                "Автообзор остановлен; выбранное фиксированное окно восстановлено."
                if had_session
                else "Автообзор уже выключен."
            )

    @staticmethod
    def _scan_status_unavailable() -> ScanRuntimeStatus:
        raise RuntimeError("scan plan status was not published")

    def start(self) -> None:
        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            if self._state == RuntimeState.RUNNING:
                return
            if self._state == RuntimeState.DRAINING:
                raise AppError(
                    code="RUNTIME.DRAINING",
                    message_ru="Приложение завершает работу.",
                    operator_action_ru="Дождитесь завершения и запустите приложение повторно.",
                )
            with self._device_lock:
                self._device_manager.refresh()
            self._state = RuntimeState.RUNNING
            self._start_acquisition_if_enabled()
            if self.config.location.source == "gps":
                try:
                    self.start_gps()
                except AppError as exc:
                    fault = (exc.code, exc.message_ru)
                    if fault not in self._startup_faults:
                        self._startup_faults.append(fault)
            try:
                retention = prune_spectrum_captures(
                    self.config.storage.data_dir / "captures",
                    retention_days=self.config.storage.retention_days,
                    now=self._clock(),
                )
            except (OSError, ValueError) as exc:
                fault = (
                    "STORAGE.RETENTION_FAILED",
                    f"Не удалось применить срок хранения записей: {type(exc).__name__}",
                )
                if fault not in self._startup_faults:
                    self._startup_faults.append(fault)
            else:
                if retention.removed_files:
                    self._log(
                        "storage.retention_applied",
                        "Удалены истёкшие финализированные записи спектра.",
                        removed_files=retention.removed_files,
                        removed_bytes=retention.removed_bytes,
                        partial_files_preserved=retention.skipped_partial_files,
                    )
            self._log(
                "runtime.started",
                "Ядро ALGA VECTOR запущено.",
                mode=self.config.mode,
                profile=self.config.profile_name,
            )

    def _should_use_background_acquisition(
        self,
        config: AppConfig,
        *,
        manager_owned: bool,
    ) -> bool:
        override = self._background_acquisition_override
        if override is not None:
            return override
        return manager_owned and has_enabled_real_hardware(config)

    def _start_acquisition_if_enabled(self) -> None:
        if not self._background_acquisition_enabled or self.acquisition_running:
            return
        stop_event = Event()
        thread = Thread(
            target=self._acquisition_main,
            args=(stop_event,),
            name="ALGA-VECTOR-acquisition",
            daemon=True,
        )
        self._acquisition_stop_event = stop_event
        self._acquisition_thread = thread
        thread.start()

    def _stop_acquisition_thread(
        self,
        *,
        timeout: float = _ACQUISITION_JOIN_TIMEOUT_SECONDS,
    ) -> bool:
        thread = self._acquisition_thread
        stop_event = self._acquisition_stop_event
        if thread is None:
            return True
        if stop_event is not None:
            stop_event.set()
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._acquisition_thread = None
            self._acquisition_stop_event = None
        return stopped

    def _next_acquisition_tuning(
        self,
        *,
        anticipate_deferred_completion: bool = False,
    ) -> tuple[int, int, ScanWindow | None, bool]:
        with self._acquisition_lock:
            session = self._rf_scan_session
            if session is None:
                return (
                    self._acquisition_center_frequency_hz,
                    self._acquisition_span_hz,
                    None,
                    False,
                )
            window = session.request_window(
                anticipate_deferred_completion=(
                    anticipate_deferred_completion
                )
            )
            changed = window.window_id != self._rf_scan_last_window_id
            return (
                window.center_frequency_hz,
                window.span_hz,
                window,
                changed,
            )

    def _scan_iteration_period_seconds(self) -> float:
        with self._acquisition_lock:
            if self._rf_scan_session is None:
                return self._acquisition_period_seconds
            return max(
                self._acquisition_period_seconds,
                self._rf_scan_session.plan.dwell_time_ms / 1_000.0,
            )

    def _acquisition_main(self, stop_event: Event) -> None:
        next_refresh = 0.0
        try:
            while not stop_event.is_set():
                iteration_started = time.monotonic()
                frame: SpectrumFrame | None = None
                accepted_retune = False
                settle_seconds = 0.0
                try:
                    (
                        center_frequency_hz,
                        span_hz,
                        scan_window,
                        _scan_window_changed,
                    ) = self._next_acquisition_tuning(
                        anticipate_deferred_completion=(
                            _uses_deferred_spectrum_requests(
                                self._device_manager
                            )
                        )
                    )
                    with self._device_lock:
                        now = time.monotonic()
                        if now >= next_refresh:
                            self._device_manager.refresh()
                            next_refresh = now + self._acquisition_refresh_seconds
                        candidate_sequence = self._acquisition_sequence + 1
                        frame = self._device_manager.read_spectrum(
                            sequence=candidate_sequence,
                            center_frequency_hz=center_frequency_hz,
                            span_hz=span_hz,
                            bins=self._acquisition_bins,
                        )
                        request_accepted = _read_request_was_accepted(
                            self._device_manager,
                            frame,
                        )
                        accepted_retune = self._mark_scan_request_accepted(
                            request_accepted,
                            scan_window,
                        )
                        if accepted_retune:
                            with self._acquisition_lock:
                                session = self._rf_scan_session
                                if session is not None:
                                    settle_seconds = (
                                        session.plan.retune_settle_ms
                                        / 1_000.0
                                    )
                        if request_accepted:
                            self._acquisition_sequence = candidate_sequence
                except (AppError, OSError, RuntimeError, ValueError) as exc:
                    self._publish_acquisition_failure(exc)
                else:
                    if frame is not None:
                        self._consume_spectrum_frame(frame)
                # A retune happens inside the accepted hardware request.  The
                # first frame is a warm-up frame and is discarded below; only
                # after that request do we spend the declared settle interval.
                if (
                    accepted_retune
                    and settle_seconds > 0.0
                    and stop_event.wait(settle_seconds)
                ):
                    break
                elapsed = time.monotonic() - iteration_started
                period = self._scan_iteration_period_seconds()
                stop_event.wait(max(0.001, period - elapsed))
        except Exception as exc:
            self._publish_acquisition_internal_failure(exc)

    def _publish_acquisition_failure(self, exc: Exception) -> None:
        if isinstance(exc, AppError):
            detail = exc.message_ru
        else:
            detail = f"внутренняя ошибка {type(exc).__name__}"
        failure = (
            "SPECTRUM.READ_FAILED",
            f"Не удалось получить спектр: {detail}.",
        )
        with self._acquisition_lock:
            self._set_spectrum_failure_locked(failure)
            if self._rf_scan_session is not None:
                with suppress(AppError):
                    self._rf_scan_session.mark_result(
                        False,
                        detail_code=(
                            exc.code
                            if isinstance(exc, AppError)
                            else "SPECTRUM.READ_FAILED"
                        ),
                    )

    def _mark_scan_request_accepted(
        self,
        accepted: bool,
        scan_window: ScanWindow | None,
    ) -> bool:
        """Record the tuning only after the manager accepted the request.

        Deferred managers can be busy while a new window is proposed.  A
        retune and its warm-up discard therefore begin on acceptance, not on
        proposal.  The return value tells the acquisition loop whether the
        declared post-retune settling interval must now elapse.
        """

        with self._acquisition_lock:
            session = self._rf_scan_session
            if session is not None:
                session.mark_request_accepted(accepted)
            if (
                not accepted
                or session is None
                or scan_window is None
            ):
                return False
            changed = (
                scan_window.window_id != self._rf_scan_last_window_id
            )
            if changed:
                self._rf_scan_last_window_id = scan_window.window_id
                self._rf_scan_warmup_window_ids.add(
                    scan_window.window_id
                )
                if (
                    scan_window.window_id
                    == session.next_window().window_id
                ):
                    session.mark_retune_accepted()
            return changed

    def _publish_acquisition_internal_failure(self, exc: Exception) -> None:
        """Expose an unexpected acquisition-thread exit instead of failing silently."""

        failure = (
            "SPECTRUM.ACQUISITION_INTERNAL_ERROR",
            "Фоновый цикл приёма остановлен после внутренней ошибки.",
        )
        with self._acquisition_lock:
            self._set_spectrum_failure_locked(failure)
            if self._rf_scan_session is not None:
                with suppress(AppError):
                    self._rf_scan_session.mark_result(
                        False,
                        detail_code="SPECTRUM.ACQUISITION_INTERNAL_ERROR",
                    )
        with suppress(Exception):
            self._log(
                "acquisition.unhandled_exception",
                "Фоновый цикл приёма остановлен после необработанной ошибки.",
                level=logging.ERROR,
                error_type=type(exc).__name__,
            )

    def _set_spectrum_failure_locked(
        self,
        failure: tuple[str, str] | None,
        *,
        data_age_ms: int | None = None,
    ) -> None:
        if failure is not None:
            if not self._spectrum_failure_active:
                self._spectrum_failure_episode += 1
            self._spectrum_failure_active = True
            self._acquisition_failure = failure
            explanation, action, quality_flag = _guided_failure_text(failure[0])
            if self._signal_assessment.source_id is None:
                self._signal_assessment = no_data_assessment(
                    self._clock(),
                    reason_code=failure[0],
                    explanation_ru=explanation,
                    operator_action_ru=action,
                    baseline_required_frames=(
                        self._signal_detector.config.min_history_frames
                    ),
                )
            else:
                self._signal_assessment = data_unreliable_assessment(
                    self._signal_assessment,
                    self._clock(),
                    reason_code=failure[0],
                    explanation_ru=explanation,
                    operator_action_ru=action,
                    data_age_ms=data_age_ms,
                    quality_flag=quality_flag,
                )
            self._signal_decision = None
            return
        self._spectrum_failure_active = False
        self._acquisition_failure = None

    def _consume_spectrum_frame(self, spectrum: SpectrumFrame) -> bool:
        """Analyze and optionally record one unique frame exactly once."""

        key = (spectrum.source_id, spectrum.sequence, spectrum.captured_at)
        with self._acquisition_lock:
            if key == self._last_consumed_frame_key:
                return False
            scan_session = self._rf_scan_session
            scan_pipelines = self._rf_scan_pipelines
            scan_window = (
                scan_session.next_window()
                if scan_session is not None
                else None
            )
            expected_center_hz = (
                scan_window.center_frequency_hz
                if scan_window is not None
                else self._acquisition_center_frequency_hz
            )
            expected_span_hz = (
                scan_window.span_hz
                if scan_window is not None
                else self._acquisition_span_hz
            )
            tuning_matches = (
                spectrum.center_frequency_hz == expected_center_hz
                and spectrum.span_hz == expected_span_hz
            )
            expected_trailing = (
                scan_session.consume_expected_trailing_frame(
                    center_frequency_hz=spectrum.center_frequency_hz,
                    span_hz=spectrum.span_hz,
                )
                if scan_session is not None
                else False
            )
            if not tuning_matches:
                self._last_consumed_frame_key = key
                if self._acquisition_transition_pending:
                    # One deferred request from the previous tuning mode may
                    # complete after start/stop/settings.  It is tagged by its
                    # measured grid and discarded without poisoning the new
                    # scan state or being published as fixed-mode data.
                    self._acquisition_transition_pending = False
                    self._log(
                        "spectrum.transition_frame_discarded",
                        "Кадр предыдущей настройки отброшен при смене режима.",
                        expected_center_hz=expected_center_hz,
                        actual_center_hz=spectrum.center_frequency_hz,
                        expected_span_hz=expected_span_hz,
                        actual_span_hz=spectrum.span_hz,
                    )
                    return False
                if expected_trailing:
                    self._log(
                        "scan_plan.trailing_frame_discarded",
                        "Отложенный кадр завершённого окна отброшен после перехода.",
                        actual_center_hz=spectrum.center_frequency_hz,
                        actual_span_hz=spectrum.span_hz,
                    )
                    return False
                if scan_session is not None:
                    scan_session.mark_result(
                        False,
                        detail_code="SCAN_PLAN.FRAME_TUNING_MISMATCH",
                    )
                    failure_code = "SCAN_PLAN.FRAME_TUNING_MISMATCH"
                    failure_text = (
                        "Приёмник вернул кадр другого окна; "
                        "данные не включены в temporal-анализ."
                    )
                    log_event = "scan_plan.frame_tuning_mismatch"
                else:
                    failure_code = "SPECTRUM.FRAME_TUNING_MISMATCH"
                    failure_text = (
                        "Приёмник вернул кадр не выбранного фиксированного окна; "
                        "данные не используются."
                    )
                    log_event = "spectrum.frame_tuning_mismatch"
                self._set_spectrum_failure_locked(
                    (failure_code, failure_text)
                )
                self._log(
                    log_event,
                    "Кадр спектра отклонён из-за несовпадения настройки.",
                    level=logging.WARNING,
                    expected_center_hz=expected_center_hz,
                    actual_center_hz=spectrum.center_frequency_hz,
                    expected_span_hz=expected_span_hz,
                    actual_span_hz=spectrum.span_hz,
                )
                return False
            self._acquisition_transition_pending = False
            if (
                scan_session is not None
                and spectrum.source_id != scan_session.source_id
            ):
                self._last_consumed_frame_key = key
                scan_session.mark_result(
                    False,
                    detail_code="SCAN_PLAN.SOURCE_MISMATCH",
                )
                self._set_spectrum_failure_locked(
                    (
                        "SCAN_PLAN.SOURCE_MISMATCH",
                        (
                            "Менеджер вернул кадр другого приёмника; "
                            "автообзор не использует fallback-источник."
                        ),
                    )
                )
                self._log(
                    "scan_plan.source_mismatch",
                    "Кадр автообзора отклонён: источник не совпал с планом.",
                    level=logging.ERROR,
                    expected_source_id=scan_session.source_id,
                    actual_source_id=spectrum.source_id,
                )
                return False
            if (
                scan_session is not None
                and scan_window is not None
                and scan_window.window_id
                in self._rf_scan_warmup_window_ids
            ):
                self._rf_scan_warmup_window_ids.discard(
                    scan_window.window_id
                )
                scan_session.mark_warmup_discarded()
                self._last_consumed_frame_key = key
                self._log(
                    "scan_plan.warmup_frame_discarded",
                    "Первый кадр после перестройки отброшен до temporal-анализа.",
                    window_id=scan_window.window_id,
                    center_frequency_hz=scan_window.center_frequency_hz,
                )
                return False
            if scan_window is None and self._fixed_tuning_warmup_pending:
                self._fixed_tuning_warmup_pending = False
                self._last_consumed_frame_key = key
                self._log(
                    "spectrum.warmup_frame_discarded",
                    "Первый кадр фиксированного окна после перестройки отброшен.",
                    center_frequency_hz=expected_center_hz,
                )
                return False
            previous_source = self._signal_source_id
            if (
                scan_session is None
                and previous_source is not None
                and previous_source != spectrum.source_id
            ):
                # A learned floor is meaningful only for one physical/logical
                # source. Switching sources must re-enter background learning,
                # even when the detector has seen that source in the past.
                self._signal_detector.reset()
                self._signal_decision_engine.reset()
                self._signal_decision = None
                self._signal_assessment = no_data_assessment(
                    self._clock(),
                    reason_code="SIGNAL.SOURCE_CHANGED",
                    explanation_ru=(
                        "Источник спектра изменился; фон прежнего приёмника больше не используется."
                    ),
                    operator_action_ru=(
                        "Дождитесь первого кадра нового источника и повторного изучения фона."
                    ),
                    baseline_required_frames=(
                        self._signal_detector.config.min_history_frames
                    ),
                )
            self._signal_source_id = spectrum.source_id
            metadata = _source_observation_metadata(
                self.config,
                spectrum.source_id,
            )
            try:
                if scan_session is None:
                    self._signal_detector.register_source_metadata(
                        spectrum.source_id,
                        metadata,
                    )
                    analysis = self._signal_detector.analyze(spectrum)
                    decision_update = self._signal_decision_engine.process(
                        analysis
                    )
                else:
                    if scan_window is None:
                        raise RuntimeError(
                            "scan session has no current window"
                        )
                    if scan_pipelines is None:
                        raise RuntimeError(
                            "scan session has no frequency-scoped pipeline"
                        )
                    analysis, decision_update = scan_pipelines.process(
                        scan_window,
                        spectrum,
                        metadata,
                    )
            except FrameValidationError as exc:
                if scan_session is not None:
                    with suppress(AppError):
                        scan_session.mark_result(
                            False,
                            detail_code=exc.code,
                        )
                self._set_spectrum_failure_locked(
                    (
                        "SPECTRUM.FRAME_REJECTED",
                        f"Кадр спектра отклонён проверкой качества: {exc.code}.",
                    )
                )
                self._log(
                    "signal_analysis.frame_rejected",
                    "Кадр спектра отклонён анализатором качества.",
                    level=logging.WARNING,
                    code=exc.code,
                )
                return False

            self._last_consumed_frame_key = key
            self._acquisition_latest_frame = spectrum
            self._acquisition_latest_monotonic = time.monotonic()
            self._signal_assessment = analysis.assessment
            if scan_session is not None:
                before = scan_session.status()
                scan_session.mark_result(True)
                after = scan_session.status()
                persistent_alert = (
                    scan_pipelines.latest_alertable_decision(
                        now=spectrum.captured_at,
                    )
                    if scan_pipelines is not None
                    else None
                )
                self._signal_decision = (
                    decision_update.decision
                    if decision_update.decision.alertable
                    else persistent_alert or decision_update.decision
                )
                if before.current_window_id != after.current_window_id:
                    self._log(
                        "scan_plan.window_advanced",
                        "Завершена временная выдержка окна автообзора.",
                        completed_window_id=before.current_window_id,
                        next_window_id=after.current_window_id,
                        completed_windows=after.completed_windows,
                        completed_cycles=after.completed_cycles,
                    )
            else:
                self._signal_decision = decision_update.decision
            self._set_spectrum_failure_locked(None)
            self._store_signal_decision(
                decision_update.decision,
                publish_new=decision_update.transition is not None,
            )
            if decision_update.transition is not None:
                self._persist_rf_transition(
                    decision_update.decision,
                    decision_update.transition,
                )
                self._log(
                    "rf_decision.transition",
                    decision_update.transition.explanation_ru,
                    transition=decision_update.transition.kind.value,
                    reason_code=decision_update.transition.reason_code,
                    family=decision_update.transition.family.value,
                    episode_id=decision_update.transition.episode_id,
                    source_id=decision_update.transition.source_id,
                )
            if self._capture_writer.active:
                try:
                    self._capture_writer.append(spectrum)
                except SpectrumCaptureError as exc:
                    partial = self._capture_writer.abort()
                    self._capture_fault = (
                        "CAPTURE.WRITE_FAILED",
                        "Запись спектра остановлена после ошибки локального хранилища.",
                    )
                    self._log(
                        "capture.write_failed",
                        "Запись спектра аварийно остановлена.",
                        level=logging.ERROR,
                        error_type=type(exc).__name__,
                        partial_retained=partial is not None,
                    )
            return True

    def _background_acquisition_view(
        self,
    ) -> tuple[
        SpectrumFrame | None,
        tuple[str, str] | None,
        int,
        tuple[RfDecision, ...],
        tuple[str, str] | None,
        SignalAssessment,
        RfDecision | None,
    ]:
        with self._acquisition_lock:
            frame = self._acquisition_latest_frame
            latest_monotonic = self._acquisition_latest_monotonic
            if not self.acquisition_running and self._acquisition_failure is None:
                self._set_spectrum_failure_locked(
                    (
                        "SPECTRUM.ACQUISITION_STOPPED",
                        "Фоновый цикл приёма не работает.",
                    )
                )
            elif (
                (frame is None or latest_monotonic is None)
                and self._acquisition_failure is None
            ):
                self._set_spectrum_failure_locked(
                    (
                        "SPECTRUM.NO_RECENT_FRAME",
                        "Валидный кадр спектра ещё не получен.",
                    )
                )
            elif frame is not None and latest_monotonic is not None:
                age_seconds = max(0.0, time.monotonic() - latest_monotonic)
                frame = replace(
                    frame,
                    data_age_ms=max(
                        frame.data_age_ms,
                        round(age_seconds * 1000),
                    ),
                )
                if (
                    self._signal_assessment.source_id == frame.source_id
                    and self._signal_assessment.sequence == frame.sequence
                ):
                    self._signal_assessment = assessment_with_data_age(
                        self._signal_assessment,
                        frame.data_age_ms,
                    )
                if (
                    age_seconds > self._acquisition_stale_seconds
                    and self._acquisition_failure is None
                ):
                    self._set_spectrum_failure_locked(
                        (
                            "SPECTRUM.STALE_FRAME",
                            "Последний валидный кадр спектра устарел.",
                        ),
                        data_age_ms=frame.data_age_ms,
                    )
            failure = self._acquisition_failure
            failure_generation = self._spectrum_failure_episode
            events = tuple(self._signal_events)
            capture_fault = self._capture_fault
            assessment = self._signal_assessment
            decision = self._signal_decision
        return (
            frame,
            failure,
            failure_generation,
            events,
            capture_fault,
            assessment,
            decision,
        )

    @staticmethod
    def _gate_spectrum_capability(
        capabilities: list[CapabilityStatus],
        failure: tuple[str, str] | None,
    ) -> list[CapabilityStatus]:
        if failure is None:
            return capabilities
        return [
            replace(
                status,
                state=CapabilityState.BLOCKED,
                reason_code=failure[0],
                explanation_ru=failure[1],
                action_ru="Проверьте приёмник и дождитесь нового валидного кадра.",
            )
            if status.capability == Capability.SPECTRUM_SWEEP
            else status
            for status in capabilities
        ]

    def snapshot(self, *, bins: int = 512) -> SystemSnapshot:
        with self._lock:
            if self._state == RuntimeState.NEW:
                self.start()
            if self._state != RuntimeState.RUNNING:
                raise _runtime_closed_error()

            self._revision += 1
            if self._background_acquisition_enabled:
                try:
                    with self._device_lock:
                        devices = self._device_manager.snapshots()
                        capabilities = list(
                            self._device_manager.resolve_capabilities()
                        )
                except (AppError, ValueError, RuntimeError) as exc:
                    devices = ()
                    capabilities = []
                    self._publish_acquisition_failure(exc)
                (
                    spectrum,
                    spectrum_failure,
                    spectrum_failure_generation,
                    signal_events,
                    capture_fault,
                    signal_assessment,
                    signal_decision,
                ) = self._background_acquisition_view()
            else:
                accepted_retune = False
                settle_seconds = 0.0
                (
                    center_frequency_hz,
                    span_hz,
                    scan_window,
                    _scan_window_changed,
                ) = self._next_acquisition_tuning(
                    anticipate_deferred_completion=(
                        _uses_deferred_spectrum_requests(
                            self._device_manager
                        )
                    )
                )
                try:
                    with self._device_lock:
                        devices = self._device_manager.refresh()
                        capabilities = list(
                            self._device_manager.resolve_capabilities()
                        )
                        spectrum = self._device_manager.read_spectrum(
                            sequence=self._revision,
                            center_frequency_hz=center_frequency_hz,
                            span_hz=span_hz,
                            bins=bins,
                        )
                        accepted_retune = self._mark_scan_request_accepted(
                            _read_request_was_accepted(
                                self._device_manager,
                                spectrum,
                            ),
                            scan_window,
                        )
                        if accepted_retune:
                            with self._acquisition_lock:
                                session = self._rf_scan_session
                                if session is not None:
                                    settle_seconds = (
                                        session.plan.retune_settle_ms
                                        / 1_000.0
                                    )
                        devices = self._device_manager.snapshots()
                except (AppError, ValueError, RuntimeError) as exc:
                    self._publish_acquisition_failure(exc)
                    with self._device_lock:
                        devices = self._device_manager.snapshots()
                        capabilities = list(
                            self._device_manager.resolve_capabilities()
                        )
                    spectrum = None
                else:
                    if spectrum is not None:
                        if not self._consume_spectrum_frame(spectrum):
                            spectrum = None
                    else:
                        with self._acquisition_lock:
                            self._set_spectrum_failure_locked(None)
                            self._signal_assessment = no_data_assessment(
                                self._clock(),
                                reason_code="SIGNAL.NO_FRAME",
                                explanation_ru=(
                                    "Настроенный источник не передал кадр спектра."
                                ),
                                operator_action_ru=(
                                    "Проверьте состояние приёмника и повторите получение данных."
                                ),
                                baseline_required_frames=(
                                    self._signal_detector.config.min_history_frames
                                ),
                            )
                            self._signal_decision = None
                if accepted_retune and settle_seconds > 0.0:
                    time.sleep(settle_seconds)
                with self._acquisition_lock:
                    spectrum_failure = self._acquisition_failure
                    spectrum_failure_generation = self._spectrum_failure_episode
                    signal_events = tuple(self._signal_events)
                    capture_fault = self._capture_fault
                    signal_assessment = self._signal_assessment
                    signal_decision = self._signal_decision

            capabilities = self._gate_spectrum_capability(
                capabilities,
                spectrum_failure,
            )

            snapshot_time = self._clock()
            location = self._location_service.current_snapshot()
            gps_failure = self._gps_failure(location)
            incidents = self._synchronize_incidents(
                devices,
                spectrum_failure,
                spectrum_failure_generation,
                capture_fault,
                gps_failure,
            )
            capabilities = self._apply_local_capabilities(capabilities)
            health = self._health.aggregate(devices, capabilities, incidents)
            mode = self._resolve_provenance(devices)
            map_status = self._combined_map_snapshot()
            if location.base is not None and map_status.available:
                in_coverage = self._map_contains(location.base)
                map_status = replace(
                    map_status,
                    base_in_coverage=in_coverage,
                    message_ru=(
                        "Пакет карты не объявляет bounds; покрытие базы не подтверждено."
                        if in_coverage is None
                        else (
                            map_status.message_ru
                            if in_coverage
                            else "База находится вне заявленного покрытия пакета карты."
                        )
                    ),
                )
            if self.config.mode == "demo":
                # Demo direction is generated only inside the explicitly
                # selected training process and remains permanently marked as
                # simulated by the direction model and UI.
                self._direction_service.set_simulated(
                    float((self._revision * 7) % 360),
                    uncertainty_deg=12.0,
                    confidence=0.78,
                    captured_at=snapshot_time,
                )
            direction = self._direction_service.snapshot(now=snapshot_time)
            fusion_decision = self._multisensor.advance(
                now=snapshot_time,
                revision=self._revision,
                rf_decision=signal_decision,
                direction=direction,
            )
            fusion_transition = self._multisensor.last_transition
            if fusion_transition is not None:
                self._log(
                    "sensor_fusion.transition",
                    fusion_transition.explanation,
                    transition=fusion_transition.kind.value,
                    reason_code=fusion_transition.reason_code,
                    classification=fusion_transition.classification.value,
                    episode_id=fusion_transition.episode_id,
                )
            scan_plan = self.scan_plan_status()
            snapshot = SystemSnapshot(
                revision=self._revision,
                devices=devices,
                capabilities=tuple(capabilities),
                incidents=incidents,
                spectrum=spectrum,
                mode=mode,
                profile_name=self.config.profile_name,
                readiness_percent=health.readiness_percent,
                runtime_mode=self.config.mode,
                experience_level=self.config.ui.experience_level,
                location=location,
                map_status=map_status,
                direction=direction,
                acoustic=self._multisensor.acoustic_assessment,
                airspace=self._multisensor.airspace_snapshot,
                fusion_decision=fusion_decision,
                scan_plan=scan_plan,
                signal_events=signal_events,
                signal_assessment=signal_assessment,
                signal_decision=signal_decision,
                captured_at=snapshot_time,
            )
            try:
                operator_situation = self._signal_processor.process_snapshot(
                    snapshot
                )
            except Exception as exc:
                failure_incident = Incident(
                    incident_id="signal-processor-failed",
                    code="SIGNAL_PROCESSOR.FAILED",
                    title_ru="Операторская интерпретация недоступна",
                    message_ru=(
                        "Сырые сенсорные данные не выданы за заключение: "
                        "единый процессор событий завершил обновление с ошибкой."
                    ),
                    action_ru=(
                        "Откройте диагностику, сохраните журнал и повторите "
                        "обновление. До восстановления не делайте вывод об источнике."
                    ),
                    severity=IncidentSeverity.ERROR,
                    source="signal_processor",
                    occurred_at=snapshot_time,
                    technical={
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                snapshot = replace(
                    snapshot,
                    incidents=(*snapshot.incidents, failure_incident),
                    readiness_percent=min(snapshot.readiness_percent, 75),
                )
                self._last_operator_situation_key = None
                self._log(
                    "signal_processor.failed",
                    "Единый процессор событий не построил операторскую обстановку.",
                    exception_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                normalized_events = self._signal_processor.event_bus.recent(
                    limit=64
                )
                snapshot = replace(
                    snapshot,
                    operator_situation=operator_situation,
                    normalized_events=normalized_events,
                    targets=self._signal_processor.targets,
                    current_target=self._signal_processor.current_target,
                    sensor_readiness=self._signal_processor.sensor_readiness,
                )
                primary = operator_situation.primary_event
                situation_key = (
                    operator_situation.mode.value,
                    primary.event_id if primary is not None else None,
                )
                if situation_key != self._last_operator_situation_key:
                    self._last_operator_situation_key = situation_key
                    self._log(
                        "signal_processor.situation_changed",
                        operator_situation.explanation_ru,
                        mode=operator_situation.mode.value,
                        severity=operator_situation.severity.value,
                        event_id=(
                            primary.event_id if primary is not None else None
                        ),
                        event_type=(
                            primary.event_type.value
                            if primary is not None
                            else None
                        ),
                    )
            self._latest = snapshot
            self._log(
                "runtime.snapshot",
                "Снимок состояния обновлён.",
                revision=snapshot.revision,
                readiness_percent=snapshot.readiness_percent,
                devices=len(snapshot.devices),
                incidents=len(snapshot.incidents),
                health=health.level.value,
                fusion_classification=fusion_decision.classification.value,
                fusion_lifecycle=fusion_decision.lifecycle.value,
                fusion_alertable=fusion_decision.alertable,
                scan_plan_id=(
                    scan_plan.plan_id if scan_plan is not None else None
                ),
                scan_window_id=(
                    scan_plan.current_window_id
                    if scan_plan is not None
                    else None
                ),
            )
            return snapshot

    def ingest_normalized_event(
        self,
        event: NormalizedEvent,
    ) -> PublishResult:
        """Ingest a policy-checked event from an optional external adapter."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            result = self._signal_processor.ingest(event)
            self._log(
                "signal_processor.external_event",
                "Нормализованное событие внешнего адаптера обработано.",
                event_id=event.event_id,
                event_type=event.event_type.value,
                accepted=result.accepted,
                duplicate=result.duplicate,
                delivery_failures=result.delivery_failures,
            )
            return result

    def tick(self, *, bins: int = 512) -> SystemSnapshot:
        return self.snapshot(bins=bins)

    def direction_snapshot(self) -> DirectionSnapshot:
        """Return the current fail-closed angular state."""

        with self._lock:
            return self._direction_service.snapshot(now=self._clock())

    def ingest_acoustic_window(
        self,
        window: PcmWindow,
    ) -> AcousticAssessment:
        """Accept one explicitly supplied PCM window; no microphone is opened."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            try:
                assessment = self._multisensor.ingest_acoustic_window(window)
            except ValueError as exc:
                raise AppError(
                    code="ACOUSTIC.INGEST_REJECTED",
                    message_ru=(
                        "Акустическое окно отклонено проверкой профиля, "
                        "происхождения или качества."
                    ),
                    operator_action_ru=(
                        "Проверьте source_id, sample rate, режим и явное "
                        "разрешение external PCM."
                    ),
                    retryable=False,
                    technical_details={"error": str(exc)},
                ) from exc
            self._latest = None
            self._log(
                "acoustic.window_processed",
                assessment.explanation_ru,
                source_id=assessment.provenance.source_id,
                lifecycle=assessment.lifecycle.value,
                family=assessment.family.value,
                alertable=assessment.alertable,
                data_quality=assessment.data_quality.value,
                simulated=(
                    assessment.provenance.kind.value == "simulated"
                ),
            )
            return assessment

    def refresh_civil_airspace(self) -> CivilAirspaceSnapshot:
        """Refresh the configured local civilian ``aircraft.json`` context."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            snapshot = self._multisensor.refresh_airspace_file()
            self._latest = None
            summary = snapshot.summary
            self._log(
                "airspace.local_context_refreshed",
                (
                    "Локальный контекст гражданского кооперативного "
                    "вещания обновлён."
                ),
                state=summary.state.value,
                data_quality=summary.data_quality.value,
                active_count=summary.active_count,
                context_only=True,
                supports_identity_correlation=False,
                supports_friend_or_foe=False,
            )
            return snapshot

    def set_manual_direction(
        self,
        bearing_deg: float,
        uncertainty_deg: float = 15.0,
    ) -> DirectionSnapshot:
        """Store an operator reference without presenting it as a measurement."""

        with self._lock:
            snapshot = self._direction_service.set_manual(
                bearing_deg,
                uncertainty_deg=uncertainty_deg,
                captured_at=self._clock(),
            )
            self._log(
                "direction.manual_set",
                "Ручная угловая отметка сохранена как неизмеренная.",
                bearing_deg=snapshot.current.bearing_deg,
                uncertainty_deg=snapshot.current.uncertainty_deg,
                source=snapshot.current.source.value,
            )
            return snapshot

    def clear_direction(self) -> DirectionSnapshot:
        """Clear the active angular observation while retaining its bounded trail."""

        with self._lock:
            snapshot = self._direction_service.clear()
            self._log(
                "direction.cleared",
                "Активное направление очищено.",
            )
            return snapshot

    def ingest_external_direction(
        self,
        bearing_deg: float,
        uncertainty_deg: float,
        confidence: float,
        captured_at: datetime,
        source_id: str,
        evidence: ExternalDirectionEvidence,
    ) -> DirectionSnapshot:
        """Accept a validated external-sensor sample through the policy gate."""

        with self._lock:
            snapshot = self._direction_service.ingest_external(
                bearing_deg,
                uncertainty_deg=uncertainty_deg,
                confidence=confidence,
                captured_at=captured_at,
                source_id=source_id,
                evidence=evidence,
            )
            self._log(
                "direction.external_ingested",
                snapshot.current.message_ru,
                accepted=snapshot.available,
                reason_code=snapshot.current.reason_code,
                source_id=source_id,
            )
            return snapshot

    def _store_signal_decision(
        self,
        decision: RfDecision,
        *,
        publish_new: bool,
    ) -> None:
        """Keep one stable row per temporal episode in the live event view.

        Candidate observations remain visible in the current decision card but
        never create history rows. A row is first published only on a meaningful
        transition and is then updated in place through holding/resolution.
        """

        episode_id = decision.episode_id
        if episode_id is None:
            return
        for index, previous in enumerate(self._signal_events):
            if previous.episode_id == episode_id:
                self._signal_events[index] = decision
                return
        if publish_new:
            self._signal_events.appendleft(decision)

    def _persist_rf_transition(
        self,
        decision: RfDecision,
        transition: DecisionTransition,
    ) -> None:
        """Persist only meaningful FSM transitions, never every FFT frame."""

        journal = self._journal
        if journal is None or journal.closed:
            return
        try:
            journal.upsert_rf_decision(decision)
            journal.append_rf_transition(transition)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            self._journal = None
            fault = (
                "STORAGE.RF_HISTORY_WRITE_FAILED",
                (
                    "Журнал RF-решений отключён после ошибки записи: "
                    f"{type(exc).__name__}."
                ),
            )
            if fault not in self._startup_faults:
                self._startup_faults.append(fault)
            if self._owns_journal and not journal.closed:
                with suppress(Exception):
                    journal.close()
            self._log(
                "rf_decision.persistence_failed",
                "Не удалось сохранить переход RF-решения.",
                level=logging.ERROR,
                error_type=type(exc).__name__,
                episode_id=decision.episode_id,
                transition_id=transition.transition_id,
            )

    def current_snapshot(self) -> SystemSnapshot:
        """Return the latest immutable snapshot, creating the first one lazily."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            if self._latest is not None:
                return self._latest
            return self.snapshot()

    def rescan(self) -> SystemSnapshot:
        """Refresh configured adapters and publish a new application snapshot."""

        with self._lock:
            if self._state == RuntimeState.NEW:
                self.start()
            if self._state != RuntimeState.RUNNING:
                raise _runtime_closed_error()
            with self._device_lock:
                self._device_manager.refresh()
            return self.snapshot()

    def discover_rtlsdr_devices(self) -> RtlSdrDiscoveryResult:
        """Enumerate descriptors without opening, tuning or enabling receivers."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
        return self._rtl_discovery_service.discover()

    def discover_hackrf_devices(self) -> HackRfDiscoveryResult:
        """Run bounded receive-side HackRF descriptor discovery."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
        return self._hackrf_discovery_service.discover()

    def discover_tinysa_devices(self) -> TinySaDiscoveryResult:
        """Enumerate tinySA serial metadata without opening candidate ports."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
        return self._tinysa_discovery_service.discover()

    def add_discovered_rtlsdr_device(self, connection: str) -> SystemSnapshot:
        """Confirm and enable one receiver that is still present right now.

        Discovery and activation are intentionally separate actions.  Calling
        this method is the operator-confirmation boundary: it re-runs the
        descriptor-only scan before changing configuration.
        """

        normalized = connection.strip().upper()
        if _RTLSDR_CONNECTION_RE.fullmatch(normalized) is None:
            raise AppError(
                code="DEVICE.RTLSDR_CONNECTION_INVALID",
                message_ru="Выбран некорректный идентификатор RTL-SDR.",
                operator_action_ru="Повторите поиск и выберите устройство из списка.",
                retryable=False,
            )
        with self._lock:
            self._ensure_live_hardware_configuration_allowed()

        discovery = self.discover_rtlsdr_devices()
        discovered = next(
            (
                item
                for item in discovery.devices
                if item.connection.upper() == normalized
            ),
            None,
        )
        if discovered is None:
            first_issue = discovery.issues[0].code if discovery.issues else None
            raise AppError(
                code="DEVICE.RTLSDR_NOT_DISCOVERED",
                message_ru="Выбранный RTL-SDR больше не обнаружен.",
                operator_action_ru=(
                    "Переподключите приёмник, повторите поиск и выберите его заново."
                ),
                retryable=True,
                technical_details={
                    "discovery_state": discovery.state.value,
                    "issue_code": first_issue,
                },
            )

        with self._lock:
            self._ensure_live_hardware_configuration_allowed()
            adapters = list(self.config.devices.adapters)
            matching_index = next(
                (
                    index
                    for index, adapter in enumerate(adapters)
                    if adapter.kind == "rtlsdr"
                    and adapter.connection.strip().upper() == normalized
                ),
                None,
            )
            already_enabled = (
                matching_index is not None
                and adapters[matching_index].enabled
                and self.config.devices.enable_real_adapters
            )
            if already_enabled:
                return self.snapshot()

            if matching_index is None:
                identifiers = {adapter.id for adapter in adapters}
                adapters.append(
                    AdapterConfig(
                        id=_available_rtlsdr_adapter_id(
                            discovered.index,
                            identifiers,
                        ),
                        kind="rtlsdr",
                        enabled=True,
                        connection=discovered.connection,
                    )
                )
            else:
                adapters[matching_index] = adapters[matching_index].model_copy(
                    update={"enabled": True}
                )

            self.update_settings(
                {
                    "devices": {
                        "enable_real_adapters": True,
                        "adapters": [
                            adapter.model_dump(mode="python")
                            for adapter in adapters
                        ],
                    }
                }
            )
            return self.snapshot()

    def add_discovered_hackrf_device(self, connection: str) -> SystemSnapshot:
        """Enable one HackRF that is still visible in standard HackRF USB mode."""

        normalized = connection.strip().upper()
        if _HACKRF_CONNECTION_RE.fullmatch(normalized) is None:
            raise AppError(
                code="DEVICE.HACKRF_CONNECTION_INVALID",
                message_ru="Выбран некорректный идентификатор HackRF.",
                operator_action_ru="Повторите поиск и выберите устройство из списка.",
                retryable=False,
            )
        with self._lock:
            self._ensure_live_hardware_configuration_allowed()
        discovery = self.discover_hackrf_devices()
        discovered = next(
            (
                item
                for item in discovery.devices
                if item.connection.strip().upper() == normalized
            ),
            None,
        )
        if discovered is None:
            issue_code = discovery.issues[0].code if discovery.issues else None
            raise AppError(
                code="DEVICE.HACKRF_NOT_DISCOVERED",
                message_ru=(
                    "Выбранный HackRF не подтверждён. PortaPack виден только "
                    "после ручного перехода в HackRF USB mode."
                ),
                operator_action_ru=(
                    "Переведите устройство в HackRF USB mode, переподключите USB "
                    "и повторите поиск."
                ),
                retryable=True,
                technical_details={
                    "discovery_state": discovery.state.value,
                    "issue_code": issue_code,
                },
            )
        return self._enable_discovered_receiver(
            kind="hackrf",
            connection=discovered.connection,
            identifier_base=f"hackrf-auto-{discovered.index}",
        )

    def add_discovered_tinysa_device(self, connection: str) -> SystemSnapshot:
        """Enable one explicitly selected metadata candidate.

        Discovery itself never opens a COM port. Calling this method is the
        operator confirmation boundary; the candidate list is re-read before
        configuration changes.
        """

        normalized = connection.strip().upper()
        if _COM_CONNECTION_RE.fullmatch(normalized) is None:
            raise AppError(
                code="DEVICE.TINYSA_CONNECTION_INVALID",
                message_ru="Выбран некорректный COM-порт tinySA.",
                operator_action_ru="Повторите поиск и выберите точный COM-порт.",
                retryable=False,
            )
        with self._lock:
            self._ensure_live_hardware_configuration_allowed()
        discovery = self.discover_tinysa_devices()
        discovered = next(
            (
                item
                for item in discovery.candidates
                if item.connection.strip().upper() == normalized
            ),
            None,
        )
        if discovered is None:
            issue_code = discovery.issues[0].code if discovery.issues else None
            raise AppError(
                code="DEVICE.TINYSA_NOT_DISCOVERED",
                message_ru="Выбранный tinySA-кандидат больше не обнаружен.",
                operator_action_ru=(
                    "Переподключите анализатор, повторите поиск и подтвердите порт."
                ),
                retryable=True,
                technical_details={
                    "discovery_state": discovery.state.value,
                    "issue_code": issue_code,
                },
            )
        return self._enable_discovered_receiver(
            kind="tinysa",
            connection=discovered.connection,
            identifier_base=f"tinysa-auto-{normalized.lower()}",
        )

    def _enable_discovered_receiver(
        self,
        *,
        kind: Literal["tinysa", "hackrf"],
        connection: str,
        identifier_base: str,
    ) -> SystemSnapshot:
        """Persist an already re-confirmed receiver candidate."""

        with self._lock:
            self._ensure_live_hardware_configuration_allowed()
            adapters = list(self.config.devices.adapters)
            normalized = connection.strip().upper()
            matching_index = next(
                (
                    index
                    for index, adapter in enumerate(adapters)
                    if adapter.kind == kind
                    and adapter.connection.strip().upper() == normalized
                ),
                None,
            )
            if (
                matching_index is not None
                and adapters[matching_index].enabled
                and self.config.devices.enable_real_adapters
            ):
                return self.snapshot()

            if matching_index is None:
                identifiers = {adapter.id for adapter in adapters}
                adapters.append(
                    AdapterConfig(
                        id=_available_adapter_id(identifier_base, identifiers),
                        kind=kind,
                        enabled=True,
                        connection=connection,
                    )
                )
            else:
                adapters[matching_index] = adapters[matching_index].model_copy(
                    update={"enabled": True}
                )
            self.update_settings(
                {
                    "devices": {
                        "enable_real_adapters": True,
                        "adapters": [
                            adapter.model_dump(mode="python")
                            for adapter in adapters
                        ],
                    }
                }
            )
            return self.snapshot()

    def _ensure_live_hardware_configuration_allowed(self) -> None:
        if self._state == RuntimeState.CLOSED:
            raise _runtime_closed_error()
        if self.config.mode != "live" or self._mode_lock in {"safe", "demo"}:
            raise AppError(
                code="DEVICE.HARDWARE_MODE_BLOCKED",
                message_ru="Подключение реального приёмника разрешено только в live-режиме.",
                operator_action_ru=(
                    "Перезапустите приложение в live-режиме и повторите поиск."
                ),
                retryable=False,
            )

    def reconnect(self, device_id: str) -> SystemSnapshot:
        """Reopen one explicitly configured adapter and publish a new snapshot."""

        with self._lock:
            if self._state == RuntimeState.NEW:
                self.start()
            if self._state != RuntimeState.RUNNING:
                raise _runtime_closed_error()
            with self._device_lock:
                result = self._device_manager.reconnect(device_id)
            self._log(
                "device.reconnect_requested",
                "Запрошено безопасное переподключение устройства.",
                device_id=result.device_id,
                state=result.state.value,
            )
            return self.snapshot()

    def acknowledge_incident(self, incident_id: str) -> str:
        """Persist acknowledgement and keep the current snapshot consistent."""

        with self._lock:
            incident = self._active_incidents.get(incident_id)
            journal_updated = False
            if self._journal is not None and not self._journal.closed:
                journal_updated = self._journal.acknowledge(incident_id)
            if incident is None and not journal_updated:
                raise AppError(
                    code="INCIDENT.NOT_FOUND",
                    message_ru="Событие не найдено в активном журнале.",
                    operator_action_ru="Обновите снимок диагностики.",
                    retryable=False,
                )
            if incident is not None:
                self._active_incidents[incident_id] = replace(incident, acknowledged=True)
            self._acknowledged_incidents.add(incident_id)
            if self._latest is not None:
                self._latest = replace(
                    self._latest,
                    incidents=tuple(
                        replace(item, acknowledged=True)
                        if item.incident_id == incident_id
                        else item
                        for item in self._latest.incidents
                    ),
                )
            self._log(
                "incident.acknowledged",
                "Оператор подтвердил ознакомление с событием.",
                incident_id=incident_id,
            )
            return "Событие подтверждено"

    def export_support_bundle(self) -> Path:
        """Create a local-only redacted support archive and return its path."""

        with self._lock:
            snapshot = self.current_snapshot()
            destination = (
                self.config.storage.data_dir
                / "support"
                / f"alga-vector-support-{self._clock():%Y%m%d-%H%M%S}.avsupport"
            )
            summary = None
            if self._journal is not None and not self._journal.closed:
                summary = self._journal.summary()
            logs: tuple[Path, ...] = ()
            if self.logger_path is not None:
                logs = tuple(
                    sorted(
                        self.logger_path.parent.glob(f"{self.logger_path.name}*"),
                        key=lambda item: item.stat().st_mtime,
                        reverse=True,
                    )
                )
            result = SupportBundleBuilder(clock=self._clock).build(
                destination,
                config=self.config,
                snapshot=snapshot,
                journal_summary=summary,
                log_files=logs,
            )
            self._log(
                "support.bundle_created",
                "Локальный пакет поддержки сформирован.",
                path=str(result.path),
                size_bytes=result.size_bytes,
            )
            return result.path

    def start_recording(self) -> SpectrumCaptureStatus:
        """Start a durable recording of processed spectrum frames.

        This intentionally does not claim to be a raw-IQ recorder.  Every frame
        retains its real unit, provenance and calibration metadata.
        """

        with self._lock:
            snapshot = (
                self.snapshot()
                if self._background_acquisition_enabled
                else self.current_snapshot()
            )
            spectrum_available = any(
                status.capability == Capability.SPECTRUM_SWEEP
                and status.state == CapabilityState.AVAILABLE
                for status in snapshot.capabilities
            )
            if snapshot.spectrum is None or not spectrum_available:
                raise AppError(
                    code="CAPTURE.NO_SPECTRUM",
                    message_ru="Нельзя начать запись: измеренный спектр недоступен.",
                    operator_action_ru="Подключите приёмник и дождитесь первого кадра.",
                    retryable=True,
                )
            with self._acquisition_lock:
                if self._capture_writer.active:
                    raise AppError(
                        code="CAPTURE.ALREADY_ACTIVE",
                        message_ru="Запись спектра уже выполняется.",
                        operator_action_ru="Остановите текущую запись перед новой.",
                        retryable=False,
                    )
            _prepare_data_directory(
                self.config.storage.data_dir,
                self.config.storage.minimum_free_gib,
            )
            try:
                with self._acquisition_lock:
                    status = self._capture_writer.start()
                    self._capture_fault = None
            except SpectrumCaptureError as exc:
                raise AppError(
                    code="CAPTURE.START_FAILED",
                    message_ru="Не удалось начать локальную запись спектра.",
                    operator_action_ru="Проверьте свободное место и права на каталог данных.",
                    retryable=True,
                ) from exc
            self._log(
                "capture.started",
                "Начата локальная запись обработанного спектра.",
                content_kind=status.content_kind,
                raw_iq=False,
            )
            return status

    def stop_recording(self) -> SpectrumCaptureResult:
        """Flush, checksum and atomically finalize the active spectrum capture."""

        with self._lock:
            with self._acquisition_lock:
                if not self._capture_writer.active:
                    raise AppError(
                        code="CAPTURE.NOT_ACTIVE",
                        message_ru="Активной записи спектра нет.",
                        operator_action_ru="Сначала начните запись.",
                        retryable=False,
                    )
                try:
                    result = self._capture_writer.stop()
                except SpectrumCaptureError as exc:
                    partial = self._capture_writer.abort()
                    self._capture_fault = (
                        "CAPTURE.FINALIZE_FAILED",
                        "Не удалось завершить запись; частичный файл сохранён для диагностики.",
                    )
                    raise AppError(
                        code="CAPTURE.FINALIZE_FAILED",
                        message_ru="Не удалось безопасно завершить запись спектра.",
                        operator_action_ru="Проверьте диск; файл .partial не удалён.",
                        retryable=True,
                        technical_details={"partial_retained": partial is not None},
                    ) from exc
                self._capture_fault = None
            self._log(
                "capture.completed",
                "Запись обработанного спектра завершена.",
                frames=result.frames,
                bytes_written=result.bytes_written,
                dropped_frames=result.dropped_frames,
                sha256=result.sha256,
            )
            return result

    def recording_status(self) -> SpectrumCaptureStatus:
        with self._acquisition_lock:
            return self._capture_writer.status()

    def settings_snapshot(self) -> dict[str, Any]:
        """Return a detached validated settings payload for the UI."""

        with self._lock:
            payload = self.config.model_dump(mode="python")
            payload["runtime_override"] = self._mode_lock
            return payload

    def set_experience_level(self, level: str) -> str:
        """Persist a presentation-only mode without touching acquisition."""

        if level not in {"guided", "expert"}:
            raise AppError(
                code="CONFIG.UI_MODE_INVALID",
                message_ru="Неизвестный режим интерфейса.",
                operator_action_ru=(
                    "Выберите SIMPLE MODE или EXPERT MODE."
                ),
                retryable=False,
                technical_details={"requested": level},
            )
        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            candidate = AppConfig.model_validate(
                self.config.model_copy(
                    update={
                        "ui": self.config.ui.model_copy(
                            update={"experience_level": level}
                        )
                    }
                ).model_dump(mode="python")
            )
            if self._config_saver is not None:
                self._config_saver(candidate)
            self.config = candidate
            self._latest = None
            self._log(
                "config.interface_mode_changed",
                "Режим интерфейса сохранён без изменения измерительного контура.",
                experience_level=level,
            )
            return (
                "EXPERT MODE включён."
                if level == "expert"
                else "SIMPLE MODE включён."
            )

    def update_settings(self, payload: Mapping[str, Any]) -> str:
        """Validate, persist and apply the operator-editable configuration."""

        with self._lock:
            if self._state == RuntimeState.CLOSED:
                raise _runtime_closed_error()
            with self._acquisition_lock:
                capture_active = self._capture_writer.active
            if capture_active:
                raise AppError(
                    code="CONFIG.CAPTURE_ACTIVE",
                    message_ru="Настройки нельзя менять во время записи спектра.",
                    operator_action_ru="Завершите запись и повторите применение настроек.",
                    retryable=False,
                )
            merged = _deep_merge(self.config.model_dump(mode="python"), payload)
            storage_payload = merged.get("storage")
            if isinstance(storage_payload, dict):
                raw_data_dir = storage_payload.get("data_dir")
                if isinstance(raw_data_dir, str):
                    if not raw_data_dir.strip():
                        raise AppError(
                            code="STORAGE.EMPTY_PATH",
                            message_ru="Каталог данных не может быть пустым.",
                            operator_action_ru="Выберите локальный каталог данных.",
                            retryable=False,
                        )
                    requested = Path(raw_data_dir)
                    if not requested.is_absolute():
                        storage_payload["data_dir"] = (
                            self.config.storage.data_dir.parent / requested
                        ).resolve()
            candidate = AppConfig.model_validate(merged)
            if self._mode_lock is not None:
                candidate = candidate.model_copy(update={"mode": self._mode_lock})
            if candidate.mode == "safe":
                candidate = candidate.model_copy(
                    update={
                        "devices": candidate.devices.model_copy(
                            update={
                                "enable_real_adapters": False,
                                "adapters": [
                                    adapter.model_copy(update={"enabled": False})
                                    for adapter in candidate.devices.adapters
                                    if not adapter.connection.upper().startswith("SIM:")
                                ],
                            }
                        ),
                        "acoustic": candidate.acoustic.model_copy(
                            update={"enabled": False, "source": "disabled"}
                        ),
                        "airspace": candidate.airspace.model_copy(
                            update={"enabled": False}
                        ),
                    }
                )
            elif candidate.mode == "demo":
                candidate = candidate.model_copy(
                    update={
                        "devices": candidate.devices.model_copy(
                            update={"enable_real_adapters": False}
                        )
                    }
                )
            if candidate.mode == "demo" and self._mode_lock == "demo":
                candidate = candidate.model_copy(update={"devices": self.config.devices})
            # ``model_copy`` intentionally skips validation; re-enter the strict
            # boundary after process-mode policies so live/safe can never retain
            # a synthetic source.
            candidate = AppConfig.model_validate(
                candidate.model_dump(mode="python")
            )
            try:
                with self._device_lock:
                    tuning_snapshots = self._device_manager.snapshots()
            except (AppError, RuntimeError, ValueError):
                tuning_snapshots = ()
            _validate_candidate_rtlsdr_tuning(candidate, tuning_snapshots)

            storage_changed = candidate.storage.data_dir != self.config.storage.data_dir
            if storage_changed:
                _prepare_data_directory(
                    candidate.storage.data_dir,
                    candidate.storage.minimum_free_gib,
                )
            manager_changed = (
                candidate.devices != self.config.devices
                or candidate.spectrum.sample_rate_hz
                != self.config.spectrum.sample_rate_hz
            )
            spectrum_grid_changed = (
                candidate.spectrum.center_frequency_hz
                != self.config.spectrum.center_frequency_hz
                or candidate.spectrum.span_hz
                != self.config.spectrum.span_hz
                or candidate.spectrum.sample_rate_hz
                != self.config.spectrum.sample_rate_hz
            )
            signal_pipeline_changed = (
                candidate.devices != self.config.devices
                or spectrum_grid_changed
                or candidate.mode != self.config.mode
            )
            multisensor_changed = (
                candidate.mode != self.config.mode
                or candidate.acoustic != self.config.acoustic
                or candidate.airspace != self.config.airspace
                or candidate.fusion != self.config.fusion
            )
            map_changed = candidate.map != self.config.map
            logging_changed = candidate.logging != self.config.logging
            acquisition_rebind_required = (
                signal_pipeline_changed or storage_changed or logging_changed
            )
            replacement_map: OfflineMapService | None = None
            replacement_online_map: OnlineTileService | None = None
            replacement_manager: DeviceManagerLike | None = None
            replacement_store: SecureLocationStore | None = None
            replacement_journal: EventJournal | None = None
            replacement_logger: JsonlRotatingLogger | None = None
            acquisition_paused = False
            try:
                if map_changed:
                    replacement_map = OfflineMapService(
                        cache_mib=candidate.map.tile_cache_mib
                    )
                if map_changed or storage_changed:
                    replacement_online_map = OnlineTileService(
                        candidate.storage.data_dir / "maps" / "online-cache",
                        network_enabled=candidate.map.network_enabled,
                        cache_mib=candidate.map.online_cache_mib,
                    )
                if replacement_map is not None and candidate.map.package_path is not None:
                    replacement_map.open(candidate.map.package_path)
                if manager_changed:
                    # A live native adapter receives a separate spawn worker.
                    # Adapter inspection remains deferred until the committed
                    # manager is refreshed.
                    replacement_manager = build_device_manager(
                        candidate,
                        clock=self._clock,
                    )
                if storage_changed:
                    replacement_store = SecureLocationStore(
                        candidate.storage.data_dir / "state" / "base-location.dpapi"
                    )
                    location = self._location_service.current_snapshot()
                    if location.base is not None:
                        replacement_store.save(
                            location.base,
                            location.source or LocationSource.MANUAL,
                            saved_at=location.captured_at or self._clock(),
                        )
                    replacement_journal = EventJournal(
                        candidate.storage.data_dir / "state" / "events.sqlite3"
                    )
                if storage_changed or logging_changed:
                    replacement_logger = JsonlRotatingLogger(
                        candidate.storage.data_dir / "logs" / "alga-vector.jsonl",
                        level=candidate.logging.level,
                        max_bytes=candidate.logging.max_bytes,
                        max_files=candidate.logging.max_files,
                    )
                if acquisition_rebind_required and self.acquisition_running:
                    if not self._stop_acquisition_thread():
                        raise AppError(
                            code="CONFIG.ACQUISITION_BUSY",
                            message_ru="Фоновый приём не остановился в отведённое время.",
                            operator_action_ru="Повторите действие или перезапустите приложение.",
                            retryable=True,
                        )
                    acquisition_paused = True
                if self._config_saver is not None:
                    self._config_saver(candidate)
            except Exception as exc:
                if replacement_manager is not None:
                    replacement_manager.close()
                if replacement_map is not None:
                    replacement_map.close()
                if replacement_online_map is not None:
                    replacement_online_map.close()
                if replacement_journal is not None and not replacement_journal.closed:
                    replacement_journal.close()
                if replacement_logger is not None and not replacement_logger.closed:
                    replacement_logger.close()
                if acquisition_paused:
                    self._start_acquisition_if_enabled()
                if isinstance(exc, MBTilesError):
                    raise AppError(
                        code="MAP.PACKAGE_INVALID",
                        message_ru="Пакет офлайн-карты не прошёл проверку.",
                        operator_action_ru="Выберите корректный raster MBTiles либо очистите путь.",
                        retryable=False,
                        technical_details={"error": type(exc).__name__},
                    ) from exc
                if isinstance(exc, AppError):
                    raise
                if storage_changed:
                    raise AppError(
                        code="STORAGE.REBIND_FAILED",
                        message_ru="Новый каталог данных не удалось подключить целиком.",
                        operator_action_ru="Проверьте права, DPAPI и локальный диск.",
                        retryable=True,
                        technical_details={"error": type(exc).__name__},
                    ) from exc
                raise

            previous_manager = self._device_manager
            previous_map = self._map_service
            previous_online_map = self._online_map_service
            previous_journal = self._journal
            previous_logger = self._event_logger
            previous_owns_journal = self._owns_journal
            previous_owns_logger = self._owns_logger
            previous_location_config = self.config.location
            if replacement_manager is not None:
                with self._device_lock:
                    self._device_manager = replacement_manager
                self._manager_owned_for_acquisition = True
            if replacement_map is not None:
                self._map_service = replacement_map
            if replacement_online_map is not None:
                self._online_map_service = replacement_online_map
            if storage_changed:
                if replacement_store is None or replacement_journal is None:
                    raise RuntimeError("storage replacement was not prepared")
                self._location_store = replacement_store
                self._journal = replacement_journal
                self._owns_journal = True
                with self._acquisition_lock:
                    self._capture_writer = SpectrumCaptureWriter(
                        candidate.storage.data_dir / "captures",
                        clock=self._clock,
                    )
                with suppress(OSError, RuntimeError, sqlite3.Error):
                    self._acknowledged_incidents.update(
                        item.incident_id
                        for item in replacement_journal.list_incidents(
                            limit=10_000,
                            acknowledged=True,
                        )
                    )
            if storage_changed or logging_changed:
                if replacement_logger is None:
                    raise RuntimeError("logging replacement was not prepared")
                self._event_logger = replacement_logger
                self._owns_logger = True
            with self._device_lock:
                self.config = candidate
                self._acquisition_center_frequency_hz = (
                    candidate.spectrum.center_frequency_hz
                )
                self._acquisition_span_hz = candidate.spectrum.span_hz
            if multisensor_changed:
                self._multisensor = MultiSensorCoordinator(
                    candidate,
                    clock=self._clock,
                )
            normal_background_acquisition = (
                self._should_use_background_acquisition(
                    candidate,
                    manager_owned=self._manager_owned_for_acquisition,
                )
            )
            self._background_acquisition_enabled = (
                normal_background_acquisition
                or (
                    self._rf_scan_forced_background
                    and not signal_pipeline_changed
                )
            )
            self._location_service.policy = _location_policy(candidate)
            self._latest = None
            if storage_changed:
                # The replacement journal must receive active incidents on the
                # next synchronization pass. UI-only settings must never
                # duplicate or reopen an existing incident.
                self._journaled_incidents.clear()
            if signal_pipeline_changed:
                with self._acquisition_lock:
                    self._rf_scan_session = None
                    if self._rf_scan_pipelines is not None:
                        self._rf_scan_pipelines.reset()
                    self._rf_scan_pipelines = None
                    self._rf_scan_last_window_id = None
                    self._rf_scan_warmup_window_ids.clear()
                    self._fixed_tuning_warmup_pending = True
                    self._acquisition_transition_pending = True
                    self._rf_scan_forced_background = False
                    self._signal_detector.reset()
                    self._signal_decision_engine.reset()
                    self._signal_decision = None
                    self._signal_source_id = None
                    self._signal_assessment = no_data_assessment(
                        self._clock(),
                        reason_code="SIGNAL.SETTINGS_CHANGED",
                        explanation_ru=(
                            "Настройки источника или анализа изменились; прежний фон сброшен."
                        ),
                        operator_action_ru=(
                            "Дождитесь нового кадра и завершения повторного изучения фона."
                        ),
                        baseline_required_frames=(
                            self._signal_detector.config.min_history_frames
                        ),
                    )
                    self._acquisition_latest_frame = None
                    self._acquisition_latest_monotonic = None
                    self._acquisition_failure = None
                    self._last_consumed_frame_key = None
                    self._spectrum_failure_active = False
            if replacement_manager is not None:
                with self._device_lock:
                    previous_manager.close()
            if replacement_map is not None:
                previous_map.close()
            if replacement_online_map is not None:
                previous_online_map.close()
            if (
                storage_changed
                and previous_journal is not None
                and previous_owns_journal
                and not previous_journal.closed
            ):
                previous_journal.close()
            if (
                (storage_changed or logging_changed)
                and previous_logger is not None
                and previous_owns_logger
                and not previous_logger.closed
            ):
                previous_logger.close()
            if (
                candidate.location.source != "gps"
                or candidate.location.gps_port != previous_location_config.gps_port
                or candidate.location.gps_baud != previous_location_config.gps_baud
            ):
                self._stop_gps()
            self._log(
                "config.updated",
                "Настройки проверены и применены.",
                profile=self.config.profile_name,
                mode=self.config.mode,
                storage_rebound=storage_changed,
                hardware_restarted=manager_changed,
                signal_pipeline_restarted=signal_pipeline_changed,
                logging_rebound=logging_changed,
            )
            if self._state == RuntimeState.RUNNING:
                self._start_acquisition_if_enabled()
            if storage_changed:
                return "Настройки сохранены; журнал, записи и защищённая база перенесены в новый каталог."
            return "Настройки сохранены и применены"

    configure = update_settings

    def set_manual_base(self, latitude: float, longitude: float) -> str:
        """Persist an exact manual base locally and keep it unverified."""

        point = GeoPoint(latitude, longitude)
        with self._lock:
            self._stop_gps()
            if self._location_store is None:
                raise AppError(
                    code="LOCATION.SECURE_STORE_UNAVAILABLE",
                    message_ru="Защищённое хранилище координат недоступно.",
                    operator_action_ru="Проверьте профиль Windows и локальный каталог данных.",
                    retryable=False,
                )
            try:
                self._location_store.save(point, LocationSource.MANUAL, saved_at=self._clock())
            except SecureStoreError as exc:
                raise AppError(
                    code="LOCATION.SECURE_SAVE_FAILED",
                    message_ru="Не удалось защищённо сохранить базовую точку.",
                    operator_action_ru="Проверьте права на локальный каталог данных.",
                    retryable=True,
                ) from exc
            snapshot = self._location_service.set_manual_base(
                point,
                captured_at=self._clock(),
            )
            self._latest = None
            self._log(
                "location.manual_base_saved",
                "Ручная базовая точка защищённо сохранена локально.",
                status=snapshot.status.value,
            )
            return (
                "Ручная база сохранена локально. Она не подтверждена GPS; "
                "абсолютные наложения заблокированы."
            )

    def clear_base_location(self) -> str:
        """Remove the protected point and stop active GPS collection."""

        with self._lock:
            self._stop_gps()
            if self._location_store is not None:
                try:
                    self._location_store.clear()
                except SecureStoreError as exc:
                    raise AppError(
                        code="LOCATION.SECURE_DELETE_FAILED",
                        message_ru="Не удалось удалить защищённую базовую точку.",
                        operator_action_ru="Проверьте локальный каталог состояния.",
                        retryable=True,
                    ) from exc
            self._location_service.clear()
            self._latest = None
            self._log("location.cleared", "Локальная базовая точка удалена.")
            return "Базовая точка удалена"

    def start_gps(self, port: str | None = None) -> str:
        """Start NMEA collection from exactly one operator-selected COM port."""

        with self._lock:
            selected_port = (port or self.config.location.gps_port).strip()
            current = self._gps_receiver
            if (
                current is not None
                and current.running
                and current.port == selected_port.upper()
                and current.baudrate == self.config.location.gps_baud
            ):
                return "GPS уже работает на выбранном порту"
            self._stop_gps()
            receiver = NmeaSerialReceiver(
                self._location_service,
                selected_port,
                baudrate=self.config.location.gps_baud,
            )
            try:
                receiver.start()
            except (GpsReceiverError, OSError, ValueError) as exc:
                raise AppError(
                    code="LOCATION.GPS_OPEN_FAILED",
                    message_ru="Не удалось открыть выбранный GPS-порт.",
                    operator_action_ru="Проверьте COM-порт, скорость и доступ к GPS-приёмнику.",
                    retryable=True,
                    technical_details={"port": selected_port, "error": type(exc).__name__},
                ) from exc
            self._gps_receiver = receiver
            self._gps_started_at = self._clock()
            self._latest = None
            self._log(
                "location.gps_started",
                "Сбор GPS/NMEA запущен с явно выбранного порта.",
                port=selected_port,
            )
            return "GPS запущен; база будет подтверждена только после серии качественных GGA"

    def discover_gps_ports(self) -> tuple[GpsPortCandidate, ...]:
        """Return metadata-only candidates; no COM port is opened or probed."""

        try:
            return discover_nmea_port_candidates()
        except GpsReceiverError as exc:
            raise AppError(
                code="LOCATION.GPS_DISCOVERY_FAILED",
                message_ru="Не удалось получить список COM-портов Windows.",
                operator_action_ru="Проверьте установку pyserial и диспетчер устройств.",
                retryable=True,
                technical_details={"error": type(exc).__name__},
            ) from exc

    def ingest_nmea(self, sentence: str | bytes) -> object:
        """Ingest one sentence for controlled integrations and hardware tests."""

        with self._lock:
            result = self._location_service.ingest_nmea(sentence, received_at=self._clock())
            self._latest = None
            return result

    def gps_status(self) -> dict[str, object]:
        with self._lock:
            receiver = self._gps_receiver
            if receiver is None:
                location = self._location_service.current_snapshot()
                return {
                    "configured": self.config.location.source == "gps",
                    "running": False,
                    "port": self.config.location.gps_port,
                    "started_at": self._gps_started_at,
                    "fix_state": location.gps_fix_state.value,
                    "fix_dimension": location.fix_dimension.value,
                    "location_status": location.status.value,
                    "last_receiver_at": location.last_receiver_at,
                }
            status = dict(receiver.status)
            status["configured"] = True
            status["started_at"] = self._gps_started_at
            return status

    def import_map_package(self, source: str | Path) -> str:
        """Validate, hash and import a raster MBTiles package into local storage."""

        with self._lock:
            catalog = MapCatalog(self.config.storage.data_dir / "maps")
            try:
                entry = catalog.import_package(Path(source))
                replacement = OfflineMapService(
                    entry.path,
                    cache_mib=self.config.map.tile_cache_mib,
                )
            except (MapCatalogError, MBTilesError, OSError, ValueError) as exc:
                raise AppError(
                    code="MAP.IMPORT_FAILED",
                    message_ru="Пакет офлайн-карты не прошёл проверку.",
                    operator_action_ru="Выберите корректный raster MBTiles с лицензией и атрибуцией.",
                    retryable=False,
                    technical_details={"error": type(exc).__name__},
                ) from exc
            candidate = self.config.model_copy(
                update={
                    "map": self.config.map.model_copy(
                        update={"package_path": entry.path}
                    )
                }
            )
            if self._config_saver is not None:
                self._config_saver(candidate)
            previous = self._map_service
            self._map_service = replacement
            self.config = candidate
            self._latest = None
            previous.close()
            self._log(
                "map.package_imported",
                "Проверенный пакет карты импортирован локально.",
                package_id=entry.package_id,
            )
            return f"Карта «{entry.metadata.name}» импортирована; SHA-256 {entry.sha256[:12]}…"

    def map_tile(self, zoom: int, x: int, y: int) -> bytes | None:
        """Return a local/cache hit or enqueue one visible online tile.

        Local MBTiles always has priority.  Online work runs in a bounded
        background service; this method itself never waits for HTTP.
        """

        with self._lock:
            local = self._map_service.get_tile(zoom, x, y)
            if local is not None:
                return local
            return self._online_map_service.get_tile(zoom, x, y)

    def map_tile_generation(self) -> int:
        """Monotonic cache generation for coalesced UI repaint polling."""

        return self._online_map_service.generation

    def map_visible_tiles(
        self,
        keys: tuple[tuple[int, int, int], ...],
    ) -> None:
        """Declare the visible viewport; this method never prefetches tiles."""

        self._online_map_service.set_visible_tiles(keys)

    def map_snapshot(self) -> MapSnapshot:
        with self._lock:
            return self._combined_map_snapshot()

    def online_map_snapshot(self) -> OnlineMapSnapshot:
        """Return sanitized diagnostics without tile or coordinate identifiers."""

        return self._online_map_service.snapshot()

    def retry_online_map(self) -> str:
        """Clear network-map retry backoff without requesting a hidden region."""

        if self._online_map_service.force_retry():
            return "Повтор разрешён; перерисуйте текущую видимую область."
        raise AppError(
            code="MAP.ONLINE_DISABLED",
            message_ru="Сетевая карта отключена.",
            operator_action_ru="Включите сетевую карту в настройках либо используйте MBTiles.",
            retryable=False,
        )

    def complete_onboarding(self, data_dir: str | None = None) -> str:
        """Persist first-run completion through the same validation boundary."""

        payload: dict[str, Any] = {"first_run_complete": True}
        if data_dir:
            payload["storage"] = {"data_dir": data_dir}
        return self.update_settings(payload)

    def shutdown(self) -> None:
        with self._lock:
            if self._state == RuntimeState.CLOSED:
                return
            self._state = RuntimeState.DRAINING
            self._log("runtime.shutdown_started", "Начато корректное завершение ядра.")
            shutdown_errors: list[str] = []
            if not self._stop_acquisition_thread():
                shutdown_errors.append("acquisition: bounded stop timeout")
            try:
                self._stop_gps()
                self._map_service.close()
                self._online_map_service.close()
                with self._acquisition_lock:
                    if self._capture_writer.active:
                        self._capture_writer.stop()
            except Exception as exc:
                shutdown_errors.append(f"geo services: {type(exc).__name__}: {exc}")
            try:
                with self._device_lock:
                    self._device_manager.close()
            except Exception as exc:  # best-effort drain must continue
                shutdown_errors.append(f"devices: {type(exc).__name__}: {exc}")
            if self.acquisition_running:
                self._stop_acquisition_thread(timeout=0.25)
            if self._journal is not None and not self._journal.closed:
                try:
                    self._journal.checkpoint()
                except Exception as exc:
                    shutdown_errors.append(f"journal checkpoint: {type(exc).__name__}: {exc}")
                if self._owns_journal:
                    try:
                        self._journal.close()
                    except Exception as exc:
                        shutdown_errors.append(f"journal close: {type(exc).__name__}: {exc}")
            self._log(
                "runtime.shutdown_complete",
                "Ядро ALGA VECTOR остановлено.",
                errors=shutdown_errors,
            )
            if self._event_logger is not None:
                try:
                    self._event_logger.flush()
                    if self._owns_logger:
                        self._event_logger.close()
                except Exception:
                    # No other local writer remains; shutdown still completes.
                    pass
            self._state = RuntimeState.CLOSED

    close = shutdown

    def _initialize_location_store(self, data_dir: Path) -> None:
        try:
            store = SecureLocationStore(data_dir / "state" / "base-location.dpapi")
            stored = store.load()
            self._location_store = store
            if stored is not None:
                self._location_service.set_manual_base(
                    stored.point,
                    captured_at=stored.saved_at,
                )
        except SecureStoreError as exc:
            self._location_store = None
            self._startup_faults.append(
                (
                    "LOCATION.SECURE_STORE_UNAVAILABLE",
                    f"Защищённое хранилище базовой точки недоступно: {type(exc).__name__}",
                )
            )

    def _combined_map_snapshot(self) -> MapSnapshot:
        local = self._map_service.snapshot()
        online = self._online_map_service.snapshot()
        if local.available:
            return replace(
                local,
                source="offline_priority",
                message_ru=(
                    f"{local.message_ru} "
                    "При отсутствии локального тайла разрешён ограниченный online/cache fallback."
                    if online.available
                    else local.message_ru
                ),
                network_enabled=online.network_enabled,
                online_cached_tiles=online.cached_tiles,
                online_pending_tiles=online.pending_requests,
                online_last_error_code=online.last_error_code,
                online_state=online.state.value,
            )
        if online.available:
            return MapSnapshot(
                availability=MapAvailability.READY,
                name="OpenStreetMap · видимые тайлы",
                minimum_zoom=online.minimum_zoom,
                maximum_zoom=online.maximum_zoom,
                bounds=(-180.0, -85.05112878, 180.0, 85.05112878),
                center=(0.0, 0.0, 2),
                attribution=online.attribution,
                message_ru=online.message_ru,
                error_code=online.last_error_code,
                source="online_visible_cache",
                network_enabled=online.network_enabled,
                online_cached_tiles=online.cached_tiles,
                online_pending_tiles=online.pending_requests,
                online_last_error_code=online.last_error_code,
                online_state=online.state.value,
            )
        return MapSnapshot(
            availability=(
                MapAvailability.ERROR
                if online.state is OnlineMapState.ERROR
                else MapAvailability.UNSET
            ),
            name="OpenStreetMap · видимые тайлы",
            minimum_zoom=online.minimum_zoom,
            maximum_zoom=online.maximum_zoom,
            bounds=(-180.0, -85.05112878, 180.0, 85.05112878),
            center=(0.0, 0.0, 2),
            attribution=online.attribution,
            source="online_not_ready",
            message_ru=online.message_ru,
            error_code=online.last_error_code,
            network_enabled=online.network_enabled,
            online_cached_tiles=online.cached_tiles,
            online_pending_tiles=online.pending_requests,
            online_last_error_code=online.last_error_code,
            online_state=online.state.value,
        )

    def _map_contains(self, point: GeoPoint) -> bool | None:
        local = self._map_service.snapshot()
        if local.available:
            return self._map_service.contains(point)
        online = self._online_map_service.snapshot()
        if not online.available:
            return None
        return -85.05112878 <= point.latitude_deg <= 85.05112878

    def _initialize_map(self, config: AppConfig) -> None:
        if config.map.package_path is None:
            return
        try:
            self._map_service.open(config.map.package_path)
        except (MBTilesError, OSError, ValueError) as exc:
            self._startup_faults.append(
                (
                    "MAP.PACKAGE_INVALID",
                    f"Пакет офлайн-карты отключён после проверки: {type(exc).__name__}",
                )
            )

    def _stop_gps(self) -> None:
        receiver = self._gps_receiver
        self._gps_receiver = None
        self._gps_started_at = None
        if receiver is not None:
            receiver.stop()

    def _gps_failure(self, location: object) -> tuple[str, str] | None:
        if self.config.location.source != "gps":
            return None
        receiver = self._gps_receiver
        if receiver is None:
            return (
                "LOCATION.GPS_NOT_RUNNING",
                "Настроенный GPS-приёмник не запущен.",
            )
        if not receiver.running:
            status = receiver.status
            detail = str(status.get("last_error", "")).strip()
            return (
                "LOCATION.GPS_READER_STOPPED",
                (
                    f"Чтение GPS остановлено: {detail}"
                    if detail
                    else "Поток GPS/NMEA неожиданно остановлен."
                ),
            )
        fix_state = str(getattr(location, "gps_fix_state", "")).lower()
        if fix_state.endswith("jump_suspected"):
            return (
                "LOCATION.GPS_JUMP_SUSPECTED",
                "GPS сообщил резкий скачок; положение базы не изменено.",
            )
        if fix_state.endswith("stale"):
            return (
                "LOCATION.GPS_STALE",
                "Последняя качественная GPS-фиксация устарела.",
            )
        if fix_state.endswith("no_fix"):
            return (
                "LOCATION.GPS_NO_FIX",
                "GPS принимает NMEA, но сейчас сообщает отсутствие фиксации.",
            )
        raw_captured_at = getattr(location, "captured_at", None)
        captured_at = (
            raw_captured_at
            if isinstance(raw_captured_at, datetime)
            else None
        )
        if (
            captured_at is None
            and self._gps_started_at is not None
            and (self._clock() - self._gps_started_at).total_seconds()
            > self.config.location.maximum_fix_age_seconds
        ):
            return (
                "LOCATION.GPS_NO_FIX",
                "GPS запущен, но качественная фиксация базы не получена вовремя.",
            )
        return None

    def _synchronize_incidents(
        self,
        devices: tuple[DeviceSnapshot, ...],
        spectrum_failure: tuple[str, str] | None,
        spectrum_failure_generation: int,
        capture_failure: tuple[str, str] | None,
        gps_failure: tuple[str, str] | None,
    ) -> tuple[Incident, ...]:
        candidates: list[Incident] = []
        for device in devices:
            incident = _incident_for_device(device, self._clock)
            if incident is not None:
                candidates.append(incident)
        for code, detail in self._startup_faults:
            candidates.append(
                _make_incident(
                    code=code,
                    source="runtime",
                    generation=0,
                    title_ru="Локальная служба недоступна",
                    message_ru=detail,
                    action_ru="Проверьте доступ к каталогу данных.",
                    severity=IncidentSeverity.ERROR,
                    clock=self._clock,
                )
            )
        for warning in self._startup_warnings:
            candidates.append(
                _make_incident(
                    code=warning.code,
                    source="config",
                    generation=0,
                    title_ru="Конфигурация восстановлена",
                    message_ru=warning.message_ru,
                    action_ru=warning.operator_action_ru,
                    severity=warning.severity,
                    clock=self._clock,
                    technical=warning.technical_details,
                )
            )
        if spectrum_failure is not None:
            candidates.append(
                _make_incident(
                    code=spectrum_failure[0],
                    source="spectrum",
                    generation=spectrum_failure_generation,
                    title_ru="Спектр временно недоступен",
                    message_ru=spectrum_failure[1],
                    action_ru="Проверьте состояние приёмника.",
                    severity=IncidentSeverity.WARNING,
                    clock=self._clock,
                )
            )
        if capture_failure is not None:
            candidates.append(
                _make_incident(
                    code=capture_failure[0],
                    source="capture",
                    generation=0,
                    title_ru="Запись спектра остановлена",
                    message_ru=capture_failure[1],
                    action_ru="Проверьте локальный диск и начните новую запись.",
                    severity=IncidentSeverity.ERROR,
                    clock=self._clock,
                )
            )
        if gps_failure is not None:
            candidates.append(
                _make_incident(
                    code=gps_failure[0],
                    source="gps",
                    generation=0,
                    title_ru="GPS требует внимания",
                    message_ru=gps_failure[1],
                    action_ru="Проверьте выбранный COM-порт, антенну и обзор неба.",
                    severity=IncidentSeverity.WARNING,
                    clock=self._clock,
                )
            )
        if self.config.mode == "safe":
            candidates.append(
                _make_incident(
                    code="RUNTIME.SAFE_MODE",
                    source="runtime",
                    generation=0,
                    title_ru="Безопасный режим",
                    message_ru="Реальные адаптеры и необязательные модули отключены.",
                    action_ru="Экспортируйте пакет поддержки перед обычным запуском.",
                    severity=IncidentSeverity.INFO,
                    clock=self._clock,
                )
            )

        synchronized: dict[str, Incident] = {}
        for incident in candidates:
            existing = self._active_incidents.get(incident.incident_id)
            if existing is not None:
                incident = replace(
                    incident,
                    occurred_at=existing.occurred_at,
                    acknowledged=existing.acknowledged,
                )
            elif incident.incident_id in self._acknowledged_incidents:
                incident = replace(incident, acknowledged=True)
            synchronized[incident.incident_id] = incident
            if incident.incident_id not in self._journaled_incidents:
                self._persist_incident(incident)
                self._journaled_incidents.add(incident.incident_id)
        self._active_incidents = synchronized
        severity_rank = {
            IncidentSeverity.CRITICAL: 0,
            IncidentSeverity.ERROR: 1,
            IncidentSeverity.WARNING: 2,
            IncidentSeverity.INFO: 3,
        }
        return tuple(
            sorted(
                synchronized.values(),
                key=lambda item: (severity_rank[item.severity], item.occurred_at, item.incident_id),
            )
        )

    def _persist_incident(self, incident: Incident) -> None:
        if self._journal is not None and not self._journal.closed:
            try:
                self._journal.append(incident)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                failed_journal = self._journal
                self._journal = None
                fault = (
                    "STORAGE.JOURNAL_WRITE_FAILED",
                    f"Журнал событий отключён после ошибки записи: {exc}",
                )
                if fault not in self._startup_faults:
                    self._startup_faults.append(fault)
                if (
                    self._owns_journal
                    and failed_journal is not None
                    and not failed_journal.closed
                ):
                    with suppress(Exception):
                        failed_journal.close()
                self._log(
                    "journal.write_failed",
                    "Не удалось записать событие в журнал.",
                    level=logging.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                )
        self._log(
            "incident.opened",
            incident.message_ru,
            level=_logging_level(incident.severity),
            code=incident.code,
            severity=incident.severity.value,
            source=incident.source,
            incident_id=incident.incident_id,
        )

    def _apply_local_capabilities(
        self,
        statuses: list[CapabilityStatus],
    ) -> list[CapabilityStatus]:
        local = CapabilityStatus(
            capability=Capability.LOCAL_CAPTURE_STORAGE,
            state=(
                CapabilityState.AVAILABLE
                if self._journal is not None and not self._journal.closed
                else CapabilityState.BLOCKED
            ),
            reason_code=(
                None
                if self._journal is not None and not self._journal.closed
                else "STORAGE.UNAVAILABLE"
            ),
            explanation_ru=(
                None
                if self._journal is not None and not self._journal.closed
                else "Локальный журнал и хранилище недоступны."
            ),
            action_ru=(
                None
                if self._journal is not None and not self._journal.closed
                else "Проверьте каталог данных."
            ),
        )
        output = [
            local if status.capability == Capability.LOCAL_CAPTURE_STORAGE else status
            for status in statuses
        ]
        return output

    def _resolve_provenance(self, devices: tuple[DeviceSnapshot, ...]) -> Provenance:
        if self.config.mode == "demo":
            return Provenance.SIMULATED
        if any(device.connection.upper().startswith("SIM:") for device in devices):
            return Provenance.SIMULATED
        return Provenance.LIVE

    def _open_default_journal(self, data_dir: Path) -> EventJournal | None:
        try:
            return EventJournal(data_dir / "state" / "events.sqlite3")
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self._startup_faults.append(
                ("STORAGE.JOURNAL_UNAVAILABLE", f"Журнал событий недоступен: {exc}")
            )
            return None

    def _open_default_logger(self, data_dir: Path) -> JsonlRotatingLogger | None:
        try:
            return JsonlRotatingLogger(
                data_dir / "logs" / "alga-vector.jsonl",
                level=self.config.logging.level,
                max_bytes=self.config.logging.max_bytes,
                max_files=self.config.logging.max_files,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            self._startup_faults.append(
                ("LOGGING.UNAVAILABLE", f"Структурированный журнал недоступен: {exc}")
            )
            return None

    def _log(
        self,
        event: str,
        message_ru: str,
        *,
        level: int = logging.INFO,
        **context: object,
    ) -> None:
        if self._event_logger is not None:
            self._event_logger.event(
                event,
                message_ru,
                level=level,
                **context,
            )

    def __enter__(self) -> ApplicationRuntime:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()


def _incident_for_device(
    device: DeviceSnapshot,
    clock: Clock,
) -> Incident | None:
    if device.state in {DeviceState.READY, DeviceState.STREAMING} and (
        device.health == HealthLevel.HEALTHY
    ):
        return None
    if device.state == DeviceState.DISABLED:
        severity = IncidentSeverity.INFO
        title = f"{device.display_name}: отключено"
    elif device.state in {DeviceState.FAILED, DeviceState.QUARANTINED} or (
        device.health == HealthLevel.ERROR
    ):
        severity = IncidentSeverity.ERROR
        title = f"{device.display_name}: ошибка"
    else:
        severity = IncidentSeverity.WARNING
        title = f"{device.display_name}: ограничение"
    return _make_incident(
        code=device.reason_code or "DEVICE.UNAVAILABLE",
        source=device.device_id,
        generation=device.generation,
        title_ru=title,
        message_ru=device.reason_ru or "Устройство недоступно.",
        action_ru=device.recommended_action_ru or "Откройте диагностику устройства.",
        severity=severity,
        clock=clock,
        technical={"state": device.state.value, "kind": device.kind},
    )


def _make_incident(
    *,
    code: str,
    source: str,
    generation: int,
    title_ru: str,
    message_ru: str,
    action_ru: str,
    severity: IncidentSeverity,
    clock: Clock,
    technical: dict[str, object] | None = None,
) -> Incident:
    identity = f"{code}|{source}|{generation}".encode()
    incident_id = f"inc-{hashlib.sha256(identity).hexdigest()[:16]}"
    return Incident(
        incident_id=incident_id,
        code=code,
        title_ru=title_ru,
        message_ru=message_ru,
        action_ru=action_ru,
        severity=severity,
        source=source,
        occurred_at=clock(),
        technical=technical or {},
    )


def _logging_level(severity: IncidentSeverity) -> int:
    return {
        IncidentSeverity.INFO: logging.INFO,
        IncidentSeverity.WARNING: logging.WARNING,
        IncidentSeverity.ERROR: logging.ERROR,
        IncidentSeverity.CRITICAL: logging.CRITICAL,
    }[severity]


def _guided_failure_text(
    code: str,
) -> tuple[str, str, QualityFlag | None]:
    """Translate technical acquisition failures into bounded novice guidance."""

    if code == "SPECTRUM.STALE_FRAME":
        return (
            "Последний пригодный кадр устарел, поэтому прежнее наблюдение больше не актуально.",
            "Проверьте приёмник и дождитесь нового свежего кадра.",
            QualityFlag.DATA_STALE,
        )
    if code == "SPECTRUM.FRAME_REJECTED":
        return (
            "Новый кадр не прошёл проверку целостности и не используется для вывода.",
            "Дождитесь следующего кадра; при повторении откройте диагностику приёмника.",
            None,
        )
    if code == "SPECTRUM.ACQUISITION_STOPPED":
        return (
            "Цикл получения данных остановлен, поэтому текущего наблюдения нет.",
            "Перезапустите получение данных или приложение.",
            None,
        )
    if code in {"SPECTRUM.NO_RECENT_FRAME", "SPECTRUM.NO_FRAME"}:
        return (
            "Пригодный кадр спектра ещё не получен.",
            "Проверьте подключение приёмника и дождитесь первого кадра.",
            None,
        )
    return (
        "Приёмник не передал новый пригодный кадр, поэтому прежний вывод приостановлен.",
        "Проверьте состояние приёмника и дождитесь нового кадра.",
        None,
    )


def _read_request_was_accepted(
    manager: DeviceManagerLike,
    frame: SpectrumFrame | None,
) -> bool:
    """Distinguish an accepted async capture from a non-blocking poll.

    Local/synchronous managers predate the optional acceptance marker and
    return a frame for every completed read.  The isolated hardware manager
    exposes the marker because ``None`` can also mean "request still pending".
    """

    accepted = getattr(manager, "last_read_request_accepted", None)
    if isinstance(accepted, bool):
        return accepted
    return frame is not None


def _uses_deferred_spectrum_requests(manager: DeviceManagerLike) -> bool:
    """Return whether one poll can consume a frame and submit another.

    The optional boolean marker is part of the isolated hardware manager's
    compatibility contract.  Synchronous managers intentionally omit it.
    """

    return isinstance(
        getattr(manager, "last_read_request_accepted", None),
        bool,
    )


def _runtime_closed_error() -> AppError:
    return AppError(
        code="RUNTIME.CLOSED",
        message_ru="Ядро приложения остановлено.",
        operator_action_ru="Запустите приложение повторно.",
        retryable=False,
    )


def _location_policy(config: AppConfig) -> LocationPolicy:
    return LocationPolicy(
        maximum_fix_age_s=config.location.maximum_fix_age_seconds,
        maximum_hdop=config.location.maximum_hdop,
        manual_conflict_distance_m=config.location.verification_radius_m,
        maximum_jump_distance_m=config.location.maximum_jump_distance_m,
        maximum_jump_speed_m_s=config.location.maximum_jump_speed_m_s,
    )


def _source_observation_metadata(
    config: AppConfig,
    source_id: str,
) -> SourceObservationMetadata:
    """Describe acquisition topology from explicit adapter configuration only."""

    adapter = next(
        (item for item in config.devices.adapters if item.id == source_id),
        None,
    )
    if adapter is None:
        return SourceObservationMetadata()
    if adapter.kind == "tinysa":
        model = (
            "tinySA · модель определяется при подключении"
            if adapter.tinysa_model == "auto"
            else adapter.tinysa_model
        )
        return SourceObservationMetadata(
            acquisition_mode=SpectrumAcquisitionMode.SWEPT_SPECTRUM,
            receiver_model=model,
        )
    if adapter.kind in {"rtlsdr", "hackrf"}:
        return SourceObservationMetadata(
            acquisition_mode=SpectrumAcquisitionMode.SIMULTANEOUS_FFT,
            receiver_model=(
                "HackRF One · RX"
                if adapter.kind == "hackrf"
                else "RTL-SDR · IQ"
            ),
        )
    return SourceObservationMetadata()


def _deep_merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    output = dict(base)
    for key, value in overlay.items():
        existing = output.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            output[key] = _deep_merge(existing, value)
        else:
            output[key] = value
    return output


def _available_rtlsdr_adapter_id(index: int, identifiers: set[str]) -> str:
    return _available_adapter_id(f"rtl-auto-{index}", identifiers)


def _available_adapter_id(base: str, identifiers: set[str]) -> str:
    if base not in identifiers:
        return base
    suffix = 2
    while f"{base}-{suffix}" in identifiers:
        suffix += 1
    return f"{base}-{suffix}"


def _validate_candidate_rtlsdr_tuning(
    config: AppConfig,
    snapshots: tuple[DeviceSnapshot, ...],
) -> None:
    for adapter in config.devices.adapters:
        if adapter.kind != "rtlsdr" or not adapter.enabled:
            continue
        profile = _candidate_rtlsdr_profile(adapter, snapshots)
        validation = validate_rtlsdr_tuning(
            profile,
            center_frequency_hz=config.spectrum.center_frequency_hz,
            span_hz=config.spectrum.span_hz,
            sample_rate_hz=config.spectrum.sample_rate_hz,
        )
        if validation.accepted:
            continue
        raise AppError(
            code=validation.code or "SPECTRUM.RTLSDR_TUNING_REJECTED",
            message_ru=validation.message_ru or "RTL-SDR отклонил диапазон.",
            operator_action_ru=(
                validation.operator_action_ru
                or "Выберите параметры внутри аппаратного диапазона."
            ),
            retryable=False,
            technical_details={
                "adapter_id": adapter.id,
                "profile": profile.profile_id,
            },
        )


def _candidate_rtlsdr_profile(
    adapter: AdapterConfig,
    snapshots: tuple[DeviceSnapshot, ...],
) -> RtlSdrTuningProfile:
    if adapter.rtlsdr_profile == "generic":
        return GENERIC_RTLSDR_PROFILE
    if adapter.rtlsdr_profile == "blog_v3_direct_q":
        return BLOG_V3_DIRECT_Q_PROFILE
    snapshot = next(
        (
            item
            for item in snapshots
            if item.kind == "rtlsdr" and item.connection == adapter.connection
        ),
        None,
    )
    if snapshot is None:
        return GENERIC_RTLSDR_PROFILE
    detected_profile_id = snapshot.metrics.get(
        "detected_tuning_profile_id",
        snapshot.metrics.get("tuning_profile_id", "generic_r820t"),
    )
    if adapter.rtlsdr_profile == "blog_v4":
        return (
            BLOG_V4_PROFILE
            if detected_profile_id == BLOG_V4_PROFILE.profile_id
            else GENERIC_RTLSDR_PROFILE
        )
    return rtlsdr_profile_by_id(detected_profile_id)


def _scan_profile_for_runtime(
    config: AppConfig,
    snapshots: tuple[DeviceSnapshot, ...],
) -> tuple[RtlSdrTuningProfile | ReceiverHardwareProfile, str]:
    """Resolve only the first operable configured receive provider."""

    operable = {DeviceState.READY, DeviceState.STREAMING}
    by_id = {snapshot.device_id: snapshot for snapshot in snapshots}
    for adapter in config.devices.adapters:
        if not adapter.enabled:
            continue
        snapshot = by_id.get(adapter.id)
        if snapshot is None:
            snapshot = next(
                (
                    item
                    for item in snapshots
                    if item.kind == adapter.kind
                    and item.connection == adapter.connection
                ),
                None,
            )
        if snapshot is None or snapshot.state not in operable:
            continue
        if adapter.kind == "rtlsdr":
            return _candidate_rtlsdr_profile(adapter, snapshots), adapter.id
        if adapter.kind == "hackrf":
            return HACKRF_ONE_PROFILE, adapter.id
        if adapter.kind != "tinysa":
            continue

        model_key = str(
            snapshot.metrics.get(
                "detected_model",
                adapter.tinysa_model,
            )
        ).lower()
        if model_key == "auto":
            # The deterministic demo adapter does not claim a hardware model.
            # Use the conservative Basic receive envelope rather than granting
            # an unverified Ultra range.
            if adapter.connection.upper() == "SIM:TINYSA":
                model = TinySaModel.BASIC
            else:
                raise AppError(
                    code="SCAN_PLAN.TINYSA_MODEL_UNCONFIRMED",
                    message_ru=(
                        "Модель tinySA не подтверждена, поэтому широкий обзор "
                        "не может безопасно выбрать аппаратный диапазон."
                    ),
                    operator_action_ru=(
                        "Обновите состояние устройства или явно выберите модель "
                        "в настройках."
                    ),
                    retryable=False,
                    technical_details={"adapter_id": adapter.id},
                )
        else:
            try:
                model = TinySaModel(model_key)
            except ValueError as exc:
                raise AppError(
                    code="SCAN_PLAN.TINYSA_MODEL_UNKNOWN",
                    message_ru="Приёмник сообщил неизвестную модель tinySA.",
                    operator_action_ru=(
                        "Обновите приложение либо выберите поддерживаемую модель."
                    ),
                    retryable=False,
                    technical_details={
                        "adapter_id": adapter.id,
                        "model": model_key,
                    },
                ) from exc
        ultra_confirmed = bool(
            snapshot.metrics.get(
                "ultra_mode_operator_confirmed",
                adapter.tinysa_ultra_mode,
            )
        )
        return (
            tinysa_hardware_profile(
                model,
                ultra_mode_enabled=ultra_confirmed,
            ),
            adapter.id,
        )
    raise AppError(
        code="SCAN_PLAN.NO_OPERABLE_RECEIVER",
        message_ru="Для автообзора нет готового приёмника.",
        operator_action_ru=(
            "Подключите и включите RTL-SDR, HackRF или tinySA, затем повторите."
        ),
        retryable=True,
    )


def _prepare_data_directory(path: Path, minimum_free_gib: float) -> None:
    """Validate a requested storage target before configuration is persisted."""

    try:
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".alga-write-probe-{uuid4().hex}"
        try:
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("ALGA VECTOR storage probe\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            with suppress(OSError):
                probe.unlink()
        free_bytes = shutil.disk_usage(path).free
    except OSError as exc:
        raise AppError(
            code="STORAGE.PREFLIGHT_FAILED",
            message_ru="Каталог данных недоступен для безопасной записи.",
            operator_action_ru="Выберите доступный локальный каталог.",
            retryable=True,
            technical_details={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    required_bytes = int(minimum_free_gib * 1024**3)
    if free_bytes < required_bytes:
        raise AppError(
            code="STORAGE.LOW_SPACE",
            message_ru="В выбранном каталоге недостаточно свободного места.",
            operator_action_ru="Освободите место или выберите другой диск.",
            retryable=True,
            technical_details={
                "path": str(path),
                "free_bytes": free_bytes,
                "required_bytes": required_bytes,
            },
        )
