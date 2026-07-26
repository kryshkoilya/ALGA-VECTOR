"""Truthful, receive-only RF hardware capability profiles.

The profiles in this module describe limits that the application can enforce
before touching a device.  They intentionally do not describe transmit
capabilities, undocumented harmonic reception, or inferred device identities.
"""

# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from alga_vector.domain.errors import AppError

HACKRF_MIN_FREQUENCY_HZ = 1_000_000
HACKRF_MAX_FREQUENCY_HZ = 6_000_000_000
HACKRF_MIN_SAMPLE_RATE_HZ = 2_000_000
HACKRF_MAX_SAMPLE_RATE_HZ = 20_000_000


class CaptureTopology(StrEnum):
    """How a receiver obtains one spectrum view."""

    IQ = "iq"
    SWEPT = "swept"


class TinySaModel(StrEnum):
    """tinySA models whose official receive limits are represented."""

    BASIC = "basic"
    ULTRA_ZS405 = "ultra_zs405"
    ULTRA_PLUS_ZS406 = "ultra_plus_zs406"
    ULTRA_PLUS_ZS407 = "ultra_plus_zs407"


@dataclass(frozen=True, slots=True)
class TuningBand:
    """One continuous, application-supported receive range."""

    band_id: str
    minimum_frequency_hz: int
    maximum_frequency_hz: int
    mode_label_ru: str
    caveat_ru: str | None = None

    def supports_window(self, center_frequency_hz: int, span_hz: int) -> bool:
        if span_hz <= 0:
            return False
        lower = center_frequency_hz - span_hz // 2
        upper = center_frequency_hz + span_hz // 2
        return (
            self.minimum_frequency_hz <= lower
            and upper <= self.maximum_frequency_hz
        )


@dataclass(frozen=True, slots=True)
class HardwareTuningValidation:
    """Result of validating a requested view against one hardware profile."""

    accepted: bool
    code: str | None = None
    message_ru: str | None = None
    operator_action_ru: str | None = None
    warning_ru: str | None = None
    band_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiverHardwareProfile:
    """Receive-only hardware envelope used by adapters and UI callers."""

    profile_id: str
    display_name_ru: str
    capture_topology: CaptureTopology
    tuning_bands: tuple[TuningBand, ...]
    minimum_sample_rate_hz: int | None = None
    maximum_sample_rate_hz: int | None = None
    maximum_instantaneous_span_hz: int | None = None
    maximum_sweep_points: int | None = None
    level_calibrated_maximum_hz: int | None = None
    source_reference: str = ""

    @property
    def minimum_frequency_hz(self) -> int:
        return min(band.minimum_frequency_hz for band in self.tuning_bands)

    @property
    def maximum_frequency_hz(self) -> int:
        return max(band.maximum_frequency_hz for band in self.tuning_bands)

    def validate_tuning(
        self,
        *,
        center_frequency_hz: int,
        span_hz: int,
        sample_rate_hz: int | None = None,
    ) -> HardwareTuningValidation:
        if center_frequency_hz <= 0 or span_hz <= 0:
            return HardwareTuningValidation(
                accepted=False,
                code="SPECTRUM.INVALID_TUNING",
                message_ru="Центральная частота и полоса должны быть положительными.",
                operator_action_ru="Укажите положительные значения частоты и полосы.",
            )

        selected_band = next(
            (
                band
                for band in self.tuning_bands
                if band.supports_window(center_frequency_hz, span_hz)
            ),
            None,
        )
        if selected_band is None:
            return HardwareTuningValidation(
                accepted=False,
                code="SPECTRUM.WINDOW_OUTSIDE_DEVICE_RANGE",
                message_ru=(
                    "Всё выбранное окно спектра должно находиться внутри "
                    f"подтверждённого диапазона {self.display_name_ru}: "
                    f"{self.minimum_frequency_hz / 1_000_000:g}–"
                    f"{self.maximum_frequency_hz / 1_000_000:g} МГц."
                ),
                operator_action_ru=(
                    "Сместите центральную частоту от аппаратной границы "
                    "или уменьшите полосу."
                ),
            )

        if self.capture_topology == CaptureTopology.IQ:
            if sample_rate_hz is None:
                return HardwareTuningValidation(
                    accepted=False,
                    code="SPECTRUM.SAMPLE_RATE_REQUIRED",
                    message_ru="Для IQ-приёмника требуется частота дискретизации.",
                    operator_action_ru="Выберите поддерживаемую частоту дискретизации.",
                )
            if (
                self.minimum_sample_rate_hz is None
                or self.maximum_sample_rate_hz is None
                or not (
                    self.minimum_sample_rate_hz
                    <= sample_rate_hz
                    <= self.maximum_sample_rate_hz
                )
            ):
                return HardwareTuningValidation(
                    accepted=False,
                    code="SPECTRUM.SAMPLE_RATE_UNSUPPORTED",
                    message_ru=(
                        f"{self.display_name_ru} не поддерживает выбранную "
                        "частоту дискретизации."
                    ),
                    operator_action_ru=(
                        "Выберите частоту дискретизации внутри показанного "
                        "аппаратного диапазона."
                    ),
                )
            span_limit = min(
                sample_rate_hz,
                self.maximum_instantaneous_span_hz or sample_rate_hz,
            )
            if span_hz > span_limit:
                return HardwareTuningValidation(
                    accepted=False,
                    code="SPECTRUM.SPAN_EXCEEDS_RECEIVER_LIMIT",
                    message_ru=(
                        "Мгновенная полоса превышает частоту дискретизации "
                        "или аппаратный предел приёмника."
                    ),
                    operator_action_ru=(
                        f"Уменьшите полосу до {span_limit / 1_000_000:g} МГц "
                        "или выберите более высокую допустимую частоту дискретизации."
                    ),
                )

        return HardwareTuningValidation(
            accepted=True,
            warning_ru=selected_band.caveat_ru,
            band_id=selected_band.band_id,
        )


HACKRF_ONE_PROFILE = ReceiverHardwareProfile(
    profile_id="hackrf_one_rx",
    display_name_ru="HackRF One · только приём",
    capture_topology=CaptureTopology.IQ,
    tuning_bands=(
        TuningBand(
            band_id="hackrf_one",
            minimum_frequency_hz=HACKRF_MIN_FREQUENCY_HZ,
            maximum_frequency_hz=HACKRF_MAX_FREQUENCY_HZ,
            mode_label_ru="HackRF USB mode · RX",
        ),
    ),
    minimum_sample_rate_hz=HACKRF_MIN_SAMPLE_RATE_HZ,
    maximum_sample_rate_hz=HACKRF_MAX_SAMPLE_RATE_HZ,
    maximum_instantaneous_span_hz=HACKRF_MAX_SAMPLE_RATE_HZ,
    source_reference="Great Scott Gadgets HackRF One documentation",
)


@dataclass(frozen=True, slots=True)
class _TinySaSpecification:
    model: TinySaModel
    display_name_ru: str
    normal_maximum_hz: int
    ultra_maximum_hz: int | None
    calibrated_maximum_hz: int
    maximum_points: int


_TINYSA_SPECIFICATIONS = {
    TinySaModel.BASIC: _TinySaSpecification(
        model=TinySaModel.BASIC,
        display_name_ru="tinySA Basic",
        normal_maximum_hz=350_000_000,
        ultra_maximum_hz=None,
        calibrated_maximum_hz=350_000_000,
        maximum_points=290,
    ),
    TinySaModel.ULTRA_ZS405: _TinySaSpecification(
        model=TinySaModel.ULTRA_ZS405,
        display_name_ru="tinySA Ultra ZS405",
        normal_maximum_hz=800_000_000,
        ultra_maximum_hz=5_300_000_000,
        calibrated_maximum_hz=6_000_000_000,
        maximum_points=450,
    ),
    TinySaModel.ULTRA_PLUS_ZS406: _TinySaSpecification(
        model=TinySaModel.ULTRA_PLUS_ZS406,
        display_name_ru="tinySA Ultra+ ZS406",
        normal_maximum_hz=900_000_000,
        ultra_maximum_hz=5_400_000_000,
        calibrated_maximum_hz=6_000_000_000,
        maximum_points=450,
    ),
    TinySaModel.ULTRA_PLUS_ZS407: _TinySaSpecification(
        model=TinySaModel.ULTRA_PLUS_ZS407,
        display_name_ru="tinySA Ultra+ ZS407",
        normal_maximum_hz=900_000_000,
        ultra_maximum_hz=7_300_000_000,
        calibrated_maximum_hz=7_300_000_000,
        maximum_points=450,
    ),
}


def tinysa_hardware_profile(
    model: TinySaModel,
    *,
    ultra_mode_enabled: bool,
) -> ReceiverHardwareProfile:
    """Build a profile without assuming that Ultra mode is enabled on-device."""

    specification = _TINYSA_SPECIFICATIONS[model]
    maximum_hz = specification.normal_maximum_hz
    caveat = None
    band_id = "normal"
    if ultra_mode_enabled and specification.ultra_maximum_hz is not None:
        maximum_hz = specification.ultra_maximum_hz
        band_id = "ultra"
        caveat = (
            "Ultra mode использует подавление зеркал несколькими измерениями: "
            "короткие и широкополосные эпизоды могут быть пропущены или искажены. "
            "Проверяйте результат внешним фильтром/эталонным приёмником."
        )
    return ReceiverHardwareProfile(
        profile_id=f"tinysa_{model.value}_{band_id}",
        display_name_ru=specification.display_name_ru,
        capture_topology=CaptureTopology.SWEPT,
        tuning_bands=(
            TuningBand(
                band_id=band_id,
                minimum_frequency_hz=100_000,
                maximum_frequency_hz=maximum_hz,
                mode_label_ru=(
                    "Ultra mode"
                    if band_id == "ultra"
                    else "обычный входной режим"
                ),
                caveat_ru=caveat,
            ),
        ),
        maximum_sweep_points=specification.maximum_points,
        level_calibrated_maximum_hz=specification.calibrated_maximum_hz,
        source_reference="official tinySA model comparison",
    )


def identify_tinysa_model(identity_text: str) -> TinySaModel:
    """Identify only explicit model markers; otherwise use a conservative Basic profile."""

    normalized = identity_text.casefold().replace(" ", "").replace("-", "")
    if "zs407" in normalized:
        return TinySaModel.ULTRA_PLUS_ZS407
    if "zs406" in normalized:
        return TinySaModel.ULTRA_PLUS_ZS406
    if "zs405" in normalized:
        return TinySaModel.ULTRA_ZS405
    if "ultra+" in identity_text.casefold() or "ultraplus" in normalized:
        # ZS406 is the conservative Ultra+ fallback when the rear-label model
        # is not present in firmware text.
        return TinySaModel.ULTRA_PLUS_ZS406
    if "ultra" in normalized or "tinysa4" in normalized:
        return TinySaModel.ULTRA_ZS405
    return TinySaModel.BASIC


def require_hardware_tuning(
    profile: ReceiverHardwareProfile,
    *,
    center_frequency_hz: int,
    span_hz: int,
    sample_rate_hz: int | None = None,
) -> HardwareTuningValidation:
    validation = profile.validate_tuning(
        center_frequency_hz=center_frequency_hz,
        span_hz=span_hz,
        sample_rate_hz=sample_rate_hz,
    )
    if validation.accepted:
        return validation
    raise AppError(
        code=validation.code or "SPECTRUM.HARDWARE_TUNING_REJECTED",
        message_ru=validation.message_ru or "Приёмник отклонил параметры спектра.",
        operator_action_ru=(
            validation.operator_action_ru
            or "Выберите параметры внутри подтверждённого аппаратного диапазона."
        ),
        retryable=False,
    )


__all__ = [
    "HACKRF_MAX_FREQUENCY_HZ",
    "HACKRF_MAX_SAMPLE_RATE_HZ",
    "HACKRF_MIN_FREQUENCY_HZ",
    "HACKRF_MIN_SAMPLE_RATE_HZ",
    "HACKRF_ONE_PROFILE",
    "CaptureTopology",
    "HardwareTuningValidation",
    "ReceiverHardwareProfile",
    "TinySaModel",
    "TuningBand",
    "identify_tinysa_model",
    "require_hardware_tuning",
    "tinysa_hardware_profile",
]
