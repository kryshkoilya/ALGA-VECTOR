"""Truthful receive-only tuning capabilities for supported RTL-SDR hardware."""

from __future__ import annotations

# ruff: noqa: RUF001
from dataclasses import dataclass
from enum import StrEnum

from alga_vector.domain.errors import AppError

RTLSDR_TUNER_MIN_HZ = 24_000_000
RTLSDR_TUNER_MAX_HZ = 1_766_000_000
RTLSDR_HF_MIN_HZ = 500_000
RTLSDR_BLOG_V4_HF_SWITCH_HZ = 28_800_000
RTLSDR_STABLE_SAMPLE_RATE_HZ = 2_560_000
RTLSDR_MAX_SAMPLE_RATE_HZ = 3_200_000
RTLSDR_SAMPLE_RATE_RANGES_HZ = (
    (225_001, 300_000),
    (900_001, RTLSDR_MAX_SAMPLE_RATE_HZ),
)


class RtlSdrInputMode(StrEnum):
    """Hardware input path selected for one centre frequency."""

    TUNER = "tuner"
    BLOG_V4_UPCONVERTER = "blog_v4_upconverter"
    DIRECT_Q = "direct_q"


@dataclass(frozen=True, slots=True)
class RtlSdrTuningProfile:
    profile_id: str
    display_name_ru: str
    minimum_frequency_hz: int
    maximum_frequency_hz: int
    hf_mode: RtlSdrInputMode | None = None
    hf_switch_hz: int = RTLSDR_TUNER_MIN_HZ

    def input_mode_for(self, center_frequency_hz: int) -> RtlSdrInputMode:
        if self.hf_mode is not None and center_frequency_hz < self.hf_switch_hz:
            return self.hf_mode
        return RtlSdrInputMode.TUNER

    def supports_center(self, center_frequency_hz: int) -> bool:
        return (
            self.minimum_frequency_hz
            <= center_frequency_hz
            <= self.maximum_frequency_hz
        )


GENERIC_RTLSDR_PROFILE = RtlSdrTuningProfile(
    profile_id="generic_r820t",
    display_name_ru="Обычный RTL-SDR (тюнерный вход)",
    minimum_frequency_hz=RTLSDR_TUNER_MIN_HZ,
    maximum_frequency_hz=RTLSDR_TUNER_MAX_HZ,
)

BLOG_V4_PROFILE = RtlSdrTuningProfile(
    profile_id="rtlsdr_blog_v4",
    display_name_ru="RTL-SDR Blog V4",
    minimum_frequency_hz=RTLSDR_HF_MIN_HZ,
    maximum_frequency_hz=RTLSDR_TUNER_MAX_HZ,
    hf_mode=RtlSdrInputMode.BLOG_V4_UPCONVERTER,
    hf_switch_hz=RTLSDR_BLOG_V4_HF_SWITCH_HZ,
)

BLOG_V3_DIRECT_Q_PROFILE = RtlSdrTuningProfile(
    profile_id="rtlsdr_blog_v3_direct_q",
    display_name_ru="RTL-SDR Blog V3 (Q-ветвь для HF)",
    minimum_frequency_hz=RTLSDR_HF_MIN_HZ,
    maximum_frequency_hz=RTLSDR_TUNER_MAX_HZ,
    hf_mode=RtlSdrInputMode.DIRECT_Q,
    hf_switch_hz=RTLSDR_TUNER_MIN_HZ,
)

_PROFILES_BY_ID = {
    profile.profile_id: profile
    for profile in (
        GENERIC_RTLSDR_PROFILE,
        BLOG_V4_PROFILE,
        BLOG_V3_DIRECT_Q_PROFILE,
    )
}


@dataclass(frozen=True, slots=True)
class FrequencyPreset:
    preset_id: str
    label_ru: str
    center_frequency_hz: int
    span_hz: int
    note_ru: str


# These are receive-only navigation shortcuts for widely documented civilian,
# broadcast and licence-exempt allocations. They never classify an observed
# transmission and deliberately contain no drone or military frequency list.
FREQUENCY_PRESETS: tuple[FrequencyPreset, ...] = (
    FrequencyPreset(
        "broadcast_am",
        "AM-радиовещание · участок 1,0 МГц",
        1_000_000,
        1_000_000,
        "Радиовещательный участок; доступен только при подтверждённом HF-входе.",
    ),
    FrequencyPreset(
        "broadcast_shortwave",
        "Коротковолновое вещание · 9,75 МГц",
        9_750_000,
        2_400_000,
        "Один участок КВ-вещания; частотный план зависит от региона и времени.",
    ),
    FrequencyPreset(
        "broadcast_fm",
        "FM-радиовещание · участок 98 МГц",
        98_000_000,
        2_400_000,
        "Показывает один участок широкой FM-полосы, а не весь диапазон сразу.",
    ),
    FrequencyPreset(
        "civil_air",
        "Гражданский авиационный AM · участок 125 МГц",
        125_000_000,
        2_400_000,
        "Только пассивный просмотр спектра; правила прослушивания зависят от страны.",
    ),
    FrequencyPreset(
        "weather_satellite",
        "Метеоспутники · участок 137,1 МГц",
        137_100_000,
        2_400_000,
        "Общий гражданский метеоспутниковый участок.",
    ),
    FrequencyPreset(
        "marine_vhf",
        "Морская VHF · участок 156,8 МГц",
        156_800_000,
        2_400_000,
        "Только пассивный спектр; местные правила прослушивания обязательны.",
    ),
    FrequencyPreset(
        "weather_radio",
        "Погодное вещание · участок 162,5 МГц",
        162_500_000,
        500_000,
        "Региональный гражданский сервис; может отсутствовать в вашей стране.",
    ),
    FrequencyPreset(
        "ism_433",
        "ISM/SRD · 433,92 МГц",
        433_920_000,
        2_000_000,
        "Безлицензионные маломощные устройства; назначение зависит от региона.",
    ),
    FrequencyPreset(
        "ism_868",
        "ISM/SRD · 868,3 МГц",
        868_300_000,
        2_000_000,
        "Региональный участок маломощных устройств.",
    ),
    FrequencyPreset(
        "ism_915",
        "ISM · участок 915 МГц",
        915_000_000,
        2_000_000,
        "Доступность и границы ISM-участка зависят от региона.",
    ),
    FrequencyPreset(
        "civil_adsb",
        "Гражданские транспондеры · 1090 МГц",
        1_090_000_000,
        2_000_000,
        "Публичный гражданский транспондерный участок; без декодирования сообщений.",
    ),
)


@dataclass(frozen=True, slots=True)
class TuningValidation:
    accepted: bool
    input_mode: RtlSdrInputMode | None = None
    code: str | None = None
    message_ru: str | None = None
    operator_action_ru: str | None = None
    warning_ru: str | None = None


def identify_rtlsdr_profile(
    manufacturer: str,
    product: str,
    *,
    direct_sampling_api: bool,
) -> RtlSdrTuningProfile:
    """Identify only hardware variants whose RF input path is known."""

    identity = f"{manufacturer} {product}".casefold().replace("-", "").replace("_", "")
    if "rtlsdrblog" in identity and "blog v4" in identity:
        return BLOG_V4_PROFILE
    if (
        "rtlsdrblog" in identity
        and "blog v3" in identity
        and direct_sampling_api
    ):
        return BLOG_V3_DIRECT_Q_PROFILE
    return GENERIC_RTLSDR_PROFILE


def rtlsdr_profile_by_id(profile_id: object) -> RtlSdrTuningProfile:
    return _PROFILES_BY_ID.get(str(profile_id), GENERIC_RTLSDR_PROFILE)


def select_rtlsdr_profile(
    detected: RtlSdrTuningProfile,
    *,
    override: str,
    direct_sampling_api: bool,
) -> RtlSdrTuningProfile:
    """Apply an explicit per-adapter operator declaration, never an assumption."""

    if override == "auto":
        return detected
    if override == "generic":
        return GENERIC_RTLSDR_PROFILE
    if override == "blog_v4":
        # The bundled driver switches the V4 RF path only after its own exact
        # EEPROM identification. A UI declaration cannot force that internal
        # upconverter path, so an unconfirmed device stays on the tuner range.
        return (
            BLOG_V4_PROFILE
            if detected == BLOG_V4_PROFILE
            else GENERIC_RTLSDR_PROFILE
        )
    if override == "blog_v3_direct_q":
        if not direct_sampling_api:
            raise AppError(
                code="DEVICE.RTLSDR_DIRECT_SAMPLING_API_MISSING",
                message_ru=(
                    "Для выбранного профиля Blog V3 недоступно управление "
                    "Q-ветвью direct sampling."
                ),
                operator_action_ru=(
                    "Обновите librtlsdr/pyrtlsdr либо выберите автоматический "
                    "или обычный профиль RTL-SDR."
                ),
                retryable=False,
            )
        return BLOG_V3_DIRECT_Q_PROFILE
    raise AppError(
        code="DEVICE.RTLSDR_PROFILE_UNKNOWN",
        message_ru="В конфигурации указан неизвестный аппаратный профиль RTL-SDR.",
        operator_action_ru="Выберите auto, generic, Blog V4 или Blog V3 Q-direct.",
        retryable=False,
    )


def available_frequency_presets(
    profile: RtlSdrTuningProfile | None,
) -> tuple[FrequencyPreset, ...]:
    if profile is None:
        return FREQUENCY_PRESETS
    return tuple(
        preset
        for preset in FREQUENCY_PRESETS
        if profile.supports_center(preset.center_frequency_hz)
    )


def validate_rtlsdr_tuning(
    profile: RtlSdrTuningProfile,
    *,
    center_frequency_hz: int,
    span_hz: int,
    sample_rate_hz: int,
) -> TuningValidation:
    if not profile.supports_center(center_frequency_hz):
        return TuningValidation(
            accepted=False,
            code="SPECTRUM.FREQUENCY_OUTSIDE_DEVICE_RANGE",
            message_ru=(
                "Центральная частота вне подтверждённого диапазона "
                f"{profile.display_name_ru}: "
                f"{profile.minimum_frequency_hz / 1_000_000:g}–"
                f"{profile.maximum_frequency_hz / 1_000_000:g} МГц."
            ),
            operator_action_ru=(
                "Выберите частоту внутри показанного аппаратного диапазона. "
                "HF ниже 24 МГц разрешается только после точного определения "
                "совместимого входа."
            ),
        )
    window_low_hz = center_frequency_hz - span_hz // 2
    window_high_hz = center_frequency_hz + span_hz // 2
    if (
        window_low_hz < profile.minimum_frequency_hz
        or window_high_hz > profile.maximum_frequency_hz
    ):
        return TuningValidation(
            accepted=False,
            code="SPECTRUM.WINDOW_OUTSIDE_DEVICE_RANGE",
            message_ru=(
                "Часть выбранного окна спектра выходит за подтверждённый "
                f"диапазон {profile.minimum_frequency_hz / 1_000_000:g}–"
                f"{profile.maximum_frequency_hz / 1_000_000:g} МГц."
            ),
            operator_action_ru=(
                "Сместите центральную частоту дальше от границы или уменьшите "
                "мгновенную полосу. Неподдерживаемые bins не отображаются как измеренные."
            ),
        )
    if span_hz <= 0 or span_hz > sample_rate_hz:
        return TuningValidation(
            accepted=False,
            code="SPECTRUM.SPAN_EXCEEDS_SAMPLE_RATE",
            message_ru=(
                "Мгновенная полоса RTL-SDR не может превышать частоту "
                "дискретизации и должна быть положительной."
            ),
            operator_action_ru=(
                "Уменьшите полосу либо выберите допустимую частоту дискретизации."
            ),
        )
    if center_frequency_hz - span_hz // 2 < 0:
        return TuningValidation(
            accepted=False,
            code="SPECTRUM.WINDOW_BELOW_ZERO",
            message_ru="Выбранное окно спектра уходит ниже 0 Гц.",
            operator_action_ru="Уменьшите полосу или увеличьте центральную частоту.",
        )
    if not any(
        minimum <= sample_rate_hz <= maximum
        for minimum, maximum in RTLSDR_SAMPLE_RATE_RANGES_HZ
    ):
        return TuningValidation(
            accepted=False,
            code="SPECTRUM.RTLSDR_SAMPLE_RATE_UNSUPPORTED",
            message_ru=(
                "RTL-SDR не поддерживает выбранную частоту дискретизации. "
                "Допустимы 0,225001–0,300 MSPS или 0,900001–3,200 MSPS."
            ),
            operator_action_ru=(
                "Для обычного просмотра выберите 2,4 MSPS; это устойчивый режим."
            ),
        )
    warning = None
    if sample_rate_hz > RTLSDR_STABLE_SAMPLE_RATE_HZ:
        warning = (
            "Выбрано больше 2,56 MSPS: драйвер допускает до 3,2 MSPS, "
            "но возможны пропуски кадров."
        )
    return TuningValidation(
        accepted=True,
        input_mode=profile.input_mode_for(center_frequency_hz),
        warning_ru=warning,
    )


def require_rtlsdr_tuning(
    profile: RtlSdrTuningProfile,
    *,
    center_frequency_hz: int,
    span_hz: int,
    sample_rate_hz: int,
) -> TuningValidation:
    validation = validate_rtlsdr_tuning(
        profile,
        center_frequency_hz=center_frequency_hz,
        span_hz=span_hz,
        sample_rate_hz=sample_rate_hz,
    )
    if validation.accepted:
        return validation
    raise AppError(
        code=validation.code or "SPECTRUM.RTLSDR_TUNING_REJECTED",
        message_ru=validation.message_ru or "RTL-SDR отклонил параметры диапазона.",
        operator_action_ru=(
            validation.operator_action_ru
            or "Выберите параметры внутри аппаратного диапазона."
        ),
        retryable=False,
    )


__all__ = [
    "BLOG_V3_DIRECT_Q_PROFILE",
    "BLOG_V4_PROFILE",
    "FREQUENCY_PRESETS",
    "GENERIC_RTLSDR_PROFILE",
    "RTLSDR_MAX_SAMPLE_RATE_HZ",
    "RTLSDR_SAMPLE_RATE_RANGES_HZ",
    "RTLSDR_STABLE_SAMPLE_RATE_HZ",
    "FrequencyPreset",
    "RtlSdrInputMode",
    "RtlSdrTuningProfile",
    "TuningValidation",
    "available_frequency_presets",
    "identify_rtlsdr_profile",
    "require_rtlsdr_tuning",
    "rtlsdr_profile_by_id",
    "select_rtlsdr_profile",
    "validate_rtlsdr_tuning",
]
