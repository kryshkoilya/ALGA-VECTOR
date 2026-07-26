"""Conservative temporal acoustic monitoring without source attribution."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock

import numpy as np

from .features import (
    AcousticFeatureError,
    FeatureExtractionConfig,
    extract_acoustic_features,
)
from .models import (
    AcousticAssessment,
    AcousticDataQuality,
    AcousticEvidence,
    AcousticFamily,
    AcousticFeatures,
    AcousticLifecycle,
    AcousticProvenance,
    AcousticQualityFlag,
    PcmWindow,
)

ACOUSTIC_LIMITATIONS_RU: tuple[str, ...] = (
    "Оценка является эвристическим баллом признаков, а не калиброванной вероятностью.",
    "Акустическая форма не устанавливает марку, страну, назначение или тип объекта.",
    "Один микрофон не определяет направление, дальность или координаты источника.",
    "Шум оборудования, транспорта, ветра и отражения могут давать сходные признаки.",
    "Для значимых решений требуется независимое подтверждение другим разрешённым сенсором.",
)


@dataclass(frozen=True, slots=True)
class AcousticMonitorConfig:
    """Validated thresholds for classification and temporal confirmation."""

    feature_config: FeatureExtractionConfig = field(
        default_factory=FeatureExtractionConfig
    )
    minimum_active_rms: float = 0.008
    broadband_rms: float = 0.04
    attack_score: float = 0.64
    minimum_consecutive_windows: int = 3
    release_windows: int = 2
    maximum_data_age_seconds: float = 2.0
    maximum_window_gap_seconds: float = 2.0
    maximum_sources: int = 16

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum_active_rms", self.minimum_active_rms),
            ("broadband_rms", self.broadband_rms),
            ("maximum_data_age_seconds", self.maximum_data_age_seconds),
            ("maximum_window_gap_seconds", self.maximum_window_gap_seconds),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if self.broadband_rms <= self.minimum_active_rms:
            raise ValueError("broadband_rms must exceed minimum_active_rms")
        if not 0.0 < self.attack_score <= 1.0:
            raise ValueError("attack_score must be in (0, 1]")
        if self.minimum_consecutive_windows < 3:
            raise ValueError("minimum_consecutive_windows must be at least 3")
        if self.release_windows < 1:
            raise ValueError("release_windows must be positive")
        if self.maximum_sources < 1:
            raise ValueError("maximum_sources must be positive")


@dataclass(slots=True)
class _SourceState:
    candidate_family: AcousticFamily | None = None
    candidate_count: int = 0
    candidate_started_sequence: int | None = None
    confirmed_family: AcousticFamily | None = None
    episode_id: str | None = None
    release_count: int = 0
    last_sequence: int | None = None
    last_captured_at: datetime | None = None
    last_sample_rate_hz: int | None = None
    last_provenance_fingerprint: tuple[str, str] | None = None

    def reset_temporal(self) -> None:
        self.candidate_family = None
        self.candidate_count = 0
        self.candidate_started_sequence = None
        self.confirmed_family = None
        self.episode_id = None
        self.release_count = 0

    def reset_all(self) -> None:
        self.reset_temporal()
        self.last_sequence = None
        self.last_captured_at = None
        self.last_sample_rate_hz = None
        self.last_provenance_fingerprint = None


@dataclass(frozen=True, slots=True)
class _WindowClassification:
    family: AcousticFamily
    score: float
    explanation_ru: str
    evidence: tuple[AcousticEvidence, ...]
    alternatives: tuple[AcousticFamily, ...]


class AcousticMonitor:
    """Stateful, bounded, fail-closed monitor over supplied mono PCM windows."""

    def __init__(self, config: AcousticMonitorConfig | None = None) -> None:
        self.config = config or AcousticMonitorConfig()
        self._states: OrderedDict[str, _SourceState] = OrderedDict()
        self._lock = RLock()

    @property
    def tracked_source_count(self) -> int:
        with self._lock:
            return len(self._states)

    def process(self, window: PcmWindow) -> AcousticAssessment:
        """Validate, classify and temporally debounce one PCM window."""

        with self._lock:
            state = self._state_for(window.provenance.source_id)
            validation_flags = self._validate_window(window)
            blocking_flags = {
                AcousticQualityFlag.INVALID_SAMPLE_RATE,
                AcousticQualityFlag.INVALID_SAMPLE_SHAPE,
                AcousticQualityFlag.UNSUPPORTED_SAMPLE_DTYPE,
                AcousticQualityFlag.NON_FINITE_SAMPLES,
                AcousticQualityFlag.PCM_OUT_OF_RANGE,
                AcousticQualityFlag.WINDOW_TOO_SHORT,
                AcousticQualityFlag.WINDOW_TOO_LARGE,
                AcousticQualityFlag.TIMESTAMP_INVALID,
                AcousticQualityFlag.DATA_STALE,
            }
            if validation_flags & blocking_flags:
                state.reset_all()
                return self._data_hold(window, validation_flags)

            continuity_flags = self._continuity_flags(window, state)
            if continuity_flags:
                state.reset_temporal()
                self._set_continuity_anchor(window, state)
                return self._data_hold(window, continuity_flags)

            try:
                features = extract_acoustic_features(
                    window.samples,
                    window.sample_rate_hz,
                    config=self.config.feature_config,
                )
            except AcousticFeatureError as exc:
                state.reset_all()
                return self._data_hold(window, frozenset({exc.flag}), code=exc.code)

            quality_flags: set[AcousticQualityFlag] = set()
            if features.rms < self.config.minimum_active_rms:
                quality_flags.add(AcousticQualityFlag.LOW_SIGNAL)
            if features.clipped_fraction > 0.001:
                quality_flags.add(AcousticQualityFlag.CLIPPING_DETECTED)

            classified = self._classify(features)
            if AcousticQualityFlag.CLIPPING_DETECTED in quality_flags:
                classified = _WindowClassification(
                    family=classified.family,
                    score=min(classified.score, self.config.attack_score - 0.01),
                    explanation_ru=(
                        f"{classified.explanation_ru} Обнаружено ограничение амплитуды; "
                        "подтверждение заблокировано до получения чистых окон."
                    ),
                    evidence=classified.evidence,
                    alternatives=classified.alternatives,
                )

            assessment = self._advance(
                window,
                state,
                features,
                classified,
                frozenset(quality_flags),
            )
            self._set_continuity_anchor(window, state)
            return assessment

    def reset_source(self, source_id: str) -> None:
        """Forget all temporal state for one source."""

        with self._lock:
            self._states.pop(source_id, None)

    def _state_for(self, source_id: str) -> _SourceState:
        state = self._states.get(source_id)
        if state is not None:
            self._states.move_to_end(source_id)
            return state
        if len(self._states) >= self.config.maximum_sources:
            self._states.popitem(last=False)
        state = _SourceState()
        self._states[source_id] = state
        return state

    def _validate_window(
        self,
        window: PcmWindow,
    ) -> frozenset[AcousticQualityFlag]:
        flags: set[AcousticQualityFlag] = set()
        config = self.config.feature_config
        if (
            isinstance(window.sample_rate_hz, bool)
            or window.sample_rate_hz < config.minimum_sample_rate_hz
            or window.sample_rate_hz > config.maximum_sample_rate_hz
        ):
            flags.add(AcousticQualityFlag.INVALID_SAMPLE_RATE)
        if window.samples.ndim != 1:
            flags.add(AcousticQualityFlag.INVALID_SAMPLE_SHAPE)
        if window.samples.size < config.minimum_samples:
            flags.add(AcousticQualityFlag.WINDOW_TOO_SHORT)
        if window.samples.size > config.maximum_samples:
            flags.add(AcousticQualityFlag.WINDOW_TOO_LARGE)

        is_numeric = (
            not bool(window.samples.dtype == bool)
            and (
                window.samples.dtype.kind in {"i", "u"}
                or window.samples.dtype.kind == "f"
            )
        )
        if not is_numeric:
            flags.add(AcousticQualityFlag.UNSUPPORTED_SAMPLE_DTYPE)
        elif window.samples.dtype.kind == "f" and window.samples.size:
            finite = bool(np.all(np.isfinite(window.samples)))
            if not finite:
                flags.add(AcousticQualityFlag.NON_FINITE_SAMPLES)
            elif float(np.max(np.abs(window.samples))) > 1.000_001:
                flags.add(AcousticQualityFlag.PCM_OUT_OF_RANGE)

        captured_valid = _is_aware(window.captured_at)
        received_valid = _is_aware(window.received_at)
        if not captured_valid or not received_valid:
            flags.add(AcousticQualityFlag.TIMESTAMP_INVALID)
        else:
            age_seconds = (window.received_at - window.captured_at).total_seconds()
            if age_seconds < -0.050:
                flags.add(AcousticQualityFlag.TIMESTAMP_INVALID)
            elif age_seconds > self.config.maximum_data_age_seconds:
                flags.add(AcousticQualityFlag.DATA_STALE)
        return frozenset(flags)

    def _continuity_flags(
        self,
        window: PcmWindow,
        state: _SourceState,
    ) -> frozenset[AcousticQualityFlag]:
        flags: set[AcousticQualityFlag] = set()
        if window.discontinuity:
            flags.add(AcousticQualityFlag.DISCONTINUITY_REPORTED)
        if window.dropped_samples:
            flags.add(AcousticQualityFlag.DROPPED_SAMPLES_REPORTED)
        if state.last_sequence is not None and window.sequence != state.last_sequence + 1:
            flags.add(AcousticQualityFlag.SEQUENCE_GAP)
        if (
            state.last_sample_rate_hz is not None
            and window.sample_rate_hz != state.last_sample_rate_hz
        ):
            flags.add(AcousticQualityFlag.SAMPLE_RATE_CHANGED)
        fingerprint = (window.provenance.device_id, window.provenance.session_id)
        if (
            state.last_provenance_fingerprint is not None
            and fingerprint != state.last_provenance_fingerprint
        ):
            flags.add(AcousticQualityFlag.SOURCE_SESSION_CHANGED)
        if state.last_captured_at is not None:
            gap = (window.captured_at - state.last_captured_at).total_seconds()
            if gap <= 0.0 or gap > self.config.maximum_window_gap_seconds:
                flags.add(AcousticQualityFlag.TIMING_DISCONTINUITY)
        return frozenset(flags)

    @staticmethod
    def _set_continuity_anchor(window: PcmWindow, state: _SourceState) -> None:
        state.last_sequence = window.sequence
        state.last_captured_at = window.captured_at
        state.last_sample_rate_hz = window.sample_rate_hz
        state.last_provenance_fingerprint = (
            window.provenance.device_id,
            window.provenance.session_id,
        )

    def _classify(self, features: AcousticFeatures) -> _WindowClassification:
        common = _feature_evidence(features)
        low = features.band_ratio("low")
        mid = features.band_ratio("mid")
        upper_mid = features.band_ratio("upper_mid")
        high = features.band_ratio("high")
        largest_band = max(low, mid, upper_mid, high)

        if features.rms < self.config.minimum_active_rms:
            return _WindowClassification(
                family=AcousticFamily.AMBIENT_NOISE,
                score=max(0.0, 1.0 - features.rms / self.config.minimum_active_rms),
                explanation_ru=(
                    "Уровень окна ниже порога активного акустического события; "
                    "наблюдается фоновая обстановка."
                ),
                evidence=common,
                alternatives=(),
            )

        rotor_match = (
            35.0 <= features.dominant_frequency_hz <= 260.0
            and low >= 0.50
            and features.spectral_centroid_hz <= 700.0
            and features.zero_crossing_rate <= 0.18
            and features.crest_factor <= 6.0
        )
        if rotor_match:
            score = min(
                0.96,
                0.50
                + 0.18 * min(low / 0.80, 1.0)
                + 0.12
                * max(
                    0.0,
                    1.0 - abs(features.dominant_frequency_hz - 120.0) / 180.0,
                )
                + 0.08
                * max(0.0, 1.0 - features.zero_crossing_rate / 0.18),
            )
            return _WindowClassification(
                family=AcousticFamily.ROTOR_LIKE,
                score=score,
                explanation_ru=(
                    "В окне устойчиво доминирует низкочастотная периодическая форма, "
                    "совместимая с rotor-like акустикой."
                ),
                evidence=common,
                alternatives=(AcousticFamily.ENGINE_LIKE,),
            )

        engine_match = (
            100.0 <= features.dominant_frequency_hz <= 1_200.0
            and low + mid >= 0.55
            and features.spectral_centroid_hz <= 2_000.0
            and features.zero_crossing_rate <= 0.35
            and features.crest_factor <= 7.0
        )
        if engine_match:
            score = min(
                0.94,
                0.50
                + 0.18 * min((low + mid) / 0.85, 1.0)
                + 0.10
                * max(0.0, 1.0 - features.zero_crossing_rate / 0.35),
            )
            return _WindowClassification(
                family=AcousticFamily.ENGINE_LIKE,
                score=score,
                explanation_ru=(
                    "Энергия и периодичность сосредоточены в низкой и средней полосах, "
                    "что совместимо с engine-like акустической формой."
                ),
                evidence=common,
                alternatives=(AcousticFamily.ROTOR_LIKE,),
            )

        broadband_match = (
            features.rms >= self.config.broadband_rms
            and features.zero_crossing_rate >= 0.22
            and features.spectral_centroid_hz >= 900.0
            and largest_band <= 0.75
        )
        if broadband_match:
            spread = 1.0 - largest_band
            score = min(0.92, 0.54 + 0.28 * min(spread / 0.50, 1.0))
            return _WindowClassification(
                family=AcousticFamily.BROADBAND_ANOMALY,
                score=score,
                explanation_ru=(
                    "Повышенная энергия распределена между несколькими полосами; "
                    "это широкополосная аномалия без установления источника."
                ),
                evidence=common,
                alternatives=(AcousticFamily.AMBIENT_NOISE,),
            )

        active_ratio = min(features.rms / (self.config.minimum_active_rms * 4.0), 1.0)
        return _WindowClassification(
            family=AcousticFamily.UNKNOWN_AERIAL_LIKE,
            score=min(0.78, 0.57 + 0.12 * active_ratio),
            explanation_ru=(
                "Активное устойчивое окно не соответствует проверенным rotor-like, "
                "engine-like или широкополосным правилам; источник не установлен."
            ),
            evidence=common,
            alternatives=(
                AcousticFamily.AMBIENT_NOISE,
                AcousticFamily.BROADBAND_ANOMALY,
            ),
        )

    def _advance(
        self,
        window: PcmWindow,
        state: _SourceState,
        features: AcousticFeatures,
        classified: _WindowClassification,
        quality_flags: frozenset[AcousticQualityFlag],
    ) -> AcousticAssessment:
        active = (
            classified.family != AcousticFamily.AMBIENT_NOISE
            and classified.score >= self.config.attack_score
        )
        lifecycle = AcousticLifecycle.IDLE
        effective_family = classified.family
        alertable = False
        episode_id = state.episode_id

        if active:
            state.release_count = 0
            if state.candidate_family == classified.family:
                state.candidate_count += 1
            else:
                state.candidate_family = classified.family
                state.candidate_count = 1
                state.candidate_started_sequence = window.sequence

            if (
                state.confirmed_family == classified.family
                or state.candidate_count >= self.config.minimum_consecutive_windows
            ):
                state.confirmed_family = classified.family
                if state.episode_id is None:
                    state.episode_id = _episode_id(
                        window.provenance,
                        state.candidate_started_sequence or window.sequence,
                    )
                lifecycle = AcousticLifecycle.CONFIRMED
                alertable = True
                episode_id = state.episode_id
            else:
                lifecycle = AcousticLifecycle.CANDIDATE
        else:
            state.candidate_family = None
            state.candidate_count = 0
            state.candidate_started_sequence = None
            if state.confirmed_family is not None:
                state.release_count += 1
                if state.release_count <= self.config.release_windows:
                    lifecycle = AcousticLifecycle.HOLDING
                    effective_family = state.confirmed_family
                    alertable = True
                    episode_id = state.episode_id
                else:
                    state.reset_temporal()
                    lifecycle = AcousticLifecycle.IDLE
                    episode_id = None

        evidence = (
            *classified.evidence,
            AcousticEvidence(
                code="AUDIO.TEMPORAL_CONFIRMATION",
                explanation_ru=(
                    "Число последовательных согласованных окон до публикации события."
                ),
                measured=state.candidate_count,
                threshold=self.config.minimum_consecutive_windows,
            ),
        )
        if lifecycle == AcousticLifecycle.HOLDING:
            explanation = (
                "Текущее окно не подтверждает активную форму, но короткий разрыв "
                "удерживается гистерезисом; новые данные ещё не переатрибутированы."
            )
        else:
            explanation = classified.explanation_ru

        data_quality = (
            AcousticDataQuality.MEDIUM
            if quality_flags
            else AcousticDataQuality.HIGH
        )
        return AcousticAssessment(
            observed_at=window.received_at,
            provenance=window.provenance,
            lifecycle=lifecycle,
            family=effective_family,
            window_family=classified.family,
            heuristic_score=classified.score,
            alertable=alertable,
            data_quality=data_quality,
            quality_flags=quality_flags,
            explanation_ru=explanation,
            evidence=evidence,
            limitations=ACOUSTIC_LIMITATIONS_RU,
            alternatives=tuple(
                item for item in classified.alternatives if item != effective_family
            ),
            features=features,
            consecutive_windows=state.candidate_count,
            episode_id=episode_id,
        )

    def _data_hold(
        self,
        window: PcmWindow,
        flags: frozenset[AcousticQualityFlag],
        *,
        code: str = "AUDIO.DATA_HOLD",
    ) -> AcousticAssessment:
        rendered = ", ".join(sorted(item.value for item in flags))
        return AcousticAssessment(
            observed_at=(
                window.received_at
                if _is_aware(window.received_at)
                else _fallback_aware(window.captured_at)
            ),
            provenance=window.provenance,
            lifecycle=AcousticLifecycle.DATA_HOLD,
            family=AcousticFamily.UNKNOWN_AERIAL_LIKE,
            window_family=AcousticFamily.UNKNOWN_AERIAL_LIKE,
            heuristic_score=0.0,
            alertable=False,
            data_quality=AcousticDataQuality.LOW,
            quality_flags=flags,
            explanation_ru=(
                "Акустическое решение приостановлено: входные данные не прошли "
                "проверку непрерывности или качества."
            ),
            evidence=(
                AcousticEvidence(
                    code=code,
                    explanation_ru="Fail-closed: накопленное подтверждение сброшено.",
                    measured=rendered,
                ),
            ),
            limitations=ACOUSTIC_LIMITATIONS_RU,
        )


def _feature_evidence(features: AcousticFeatures) -> tuple[AcousticEvidence, ...]:
    return (
        AcousticEvidence(
            code="AUDIO.RMS",
            explanation_ru="Среднеквадратический уровень нормализованного PCM.",
            measured=features.rms,
        ),
        AcousticEvidence(
            code="AUDIO.CREST",
            explanation_ru="Пик-фактор окна.",
            measured=features.crest_factor,
        ),
        AcousticEvidence(
            code="AUDIO.ZCR",
            explanation_ru="Доля переходов через ноль.",
            measured=features.zero_crossing_rate,
        ),
        AcousticEvidence(
            code="AUDIO.DOMINANT_FREQUENCY",
            explanation_ru="Частота максимума оконного FFT.",
            measured=features.dominant_frequency_hz,
        ),
        AcousticEvidence(
            code="AUDIO.SPECTRAL_CENTROID",
            explanation_ru="Спектральный центр тяжести окна.",
            measured=features.spectral_centroid_hz,
        ),
        AcousticEvidence(
            code="AUDIO.BAND_ENERGY",
            explanation_ru="Доли энергии low/mid/upper_mid/high.",
            measured=";".join(
                f"{item.name}={item.ratio:.4f}" for item in features.band_energy
            ),
        ),
    )


def _episode_id(provenance: AcousticProvenance, sequence: int) -> str:
    payload = (
        f"{provenance.source_id}|{provenance.device_id}|"
        f"{provenance.session_id}|{sequence}"
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=10).hexdigest()


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _fallback_aware(value: datetime) -> datetime:
    if _is_aware(value):
        return value
    return value.replace(tzinfo=UTC)
