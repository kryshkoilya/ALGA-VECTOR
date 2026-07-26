"""Minimal, human-readable situation page for non-technical operators."""

from __future__ import annotations

# ruff: noqa: RUF001
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QWidget,
)

from ..runtime import attr, current_snapshot, value_of
from ..theme import Colors
from ..widgets import InlineNotice, Panel
from .common import OperatorPage

_MODE_PRESENTATION = {
    "quiet": ("ТИШИНА", "Фон чист", "ready"),
    "background": ("ФОН", "Наблюдается обычный фон", "info"),
    "activity": ("АКТИВНОСТЬ", "Обнаружена активность", "warning"),
    "confirmed": ("ПОДТВЕРЖДЁННАЯ ЦЕЛЬ", "Цель подтверждена", "critical"),
    "unavailable": ("НЕТ ДАННЫХ", "Обстановка недоступна", "neutral"),
}

_MODE_ALIASES = {
    "silence": "quiet",
    "clean": "quiet",
    "clean_background": "quiet",
    "no_activity": "quiet",
    "noise_background": "background",
    "background_only": "background",
    "active": "activity",
    "radio_activity_detected": "activity",
    "likely_handheld_radio": "activity",
    "likely_video_link": "activity",
    "likely_drone_signature": "activity",
    "target_confirmed": "confirmed",
    "confirmed_target": "confirmed",
}

_EVENT_TITLES = {
    "noise_background": "Обычный фон",
    "radio_activity_detected": "Обнаружена RF-активность",
    "likely_handheld_radio": "Признаки портативной радиосвязи",
    "likely_video_link": "Признаки видеоканала",
    "likely_drone_signature": "Сигнатура требует проверки",
    "adsb_contact": "Контакт ADS-B",
    "acoustic_anomaly": "Акустическая аномалия",
    "direction_estimated": "Получен сектор направления",
    "multisensor_correlated": "Согласованная активность нескольких сенсоров",
    "target_confirmed": "Цель подтверждена независимыми данными",
    "sensor_unavailable": "Сенсор недоступен",
}

_CONFIDENCE_LABELS = {
    "none": "Не рассчитана",
    "not_available": "Не рассчитана",
    "unknown": "Не рассчитана",
    "very_low": "Очень низкая",
    "low": "Низкая",
    "medium": "Средняя",
    "moderate": "Средняя",
    "high": "Высокая",
    "very_high": "Очень высокая",
    "confirmed": "Подтверждено",
}

_IMPORTANT_EVENT_TYPES = {
    "likely_video_link",
    "likely_drone_signature",
    "acoustic_anomaly",
    "direction_estimated",
    "multisensor_correlated",
    "target_confirmed",
    "sensor_unavailable",
}

_SENSOR_LABELS = {
    "rf_trigger": "RF-триггер",
    "rf_spectrum": "RF-приёмник",
    "direction_finder": "Пеленгатор",
    "acoustic": "Акустический сенсор",
    "adsb": "ADS-B",
    "passive_radar": "Пассивный радар",
    "camera": "Камера",
    "classifier": "Классификатор",
    "fusion": "Модуль объединения",
    "system": "Система",
}


@dataclass(frozen=True, slots=True)
class _EventView:
    title: str
    detail: str
    timestamp: str
    severity: str
    important: bool

    @property
    def rendered(self) -> str:
        prefix = f"{self.timestamp}  " if self.timestamp else ""
        detail = f"\n{self.detail}" if self.detail else ""
        return f"{prefix}{self.title}{detail}"


class SimpleSituationPage(OperatorPage):
    """One-screen situation summary backed only by interpreted events.

    The page intentionally does not inspect spectrum, RSSI, IQ samples, raw
    direction state, or legacy classification fields.  Missing
    ``operator_situation`` is therefore rendered as unavailable instead of
    being reconstructed in the UI.
    """

    def __init__(
        self,
        runtime: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            runtime,
            "Простая обстановка",
            "Главное состояние, подтверждение и следующий безопасный шаг",
            action_text="Обновить",
            parent=parent,
        )
        self.setObjectName("simpleSituationPage")
        self.header.action.clicked.connect(lambda: self.refresh())

        self.hero = Panel("ТЕКУЩАЯ ОБСТАНОВКА")
        self.hero.setObjectName("situationHeroCard")
        self.hero.setMinimumHeight(168)
        hero_top = QHBoxLayout()
        hero_top.setSpacing(12)
        self.mode_label = QLabel("НЕТ ДАННЫХ")
        self.mode_label.setObjectName("situationMode")
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_label.setMinimumWidth(132)
        self.mode_label.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        hero_top.addWidget(self.mode_label, 0, Qt.AlignmentFlag.AlignTop)
        hero_text = QGridLayout()
        hero_text.setVerticalSpacing(6)
        self.headline = QLabel("Обстановка недоступна")
        self.headline.setObjectName("situationHeadline")
        self.headline.setStyleSheet("font-size: 30px; font-weight: 600;")
        self.headline.setWordWrap(True)
        self.explanation = QLabel(
            "Интерпретированный вывод ещё не получен. Сырые данные не выдаются "
            "за операторское заключение."
        )
        self.explanation.setObjectName("situationExplanation")
        self.explanation.setProperty("secondary", "true")
        self.explanation.setStyleSheet("font-size: 14px;")
        self.explanation.setWordWrap(True)
        hero_text.addWidget(self.headline, 0, 0)
        hero_text.addWidget(self.explanation, 1, 0)
        hero_top.addLayout(hero_text, 1)
        self.hero.content_layout.addLayout(hero_top)
        self.root_layout.addWidget(self.hero)

        facts = QGridLayout()
        facts.setHorizontalSpacing(10)
        facts.setVerticalSpacing(10)

        self.direction_card = Panel("ГДЕ", compact=True)
        self.direction_card.setObjectName("directionCard")
        self.direction_value = QLabel("Пеленгация недоступна")
        self.direction_value.setObjectName("situationDirection")
        self.direction_value.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.direction_value.setWordWrap(True)
        self.direction_detail = QLabel(
            "Нет подтверждённых данных направления."
        )
        self.direction_detail.setObjectName("situationDirectionDetail")
        self.direction_detail.setProperty("secondary", "true")
        self.direction_detail.setWordWrap(True)
        self.direction_card.content_layout.addWidget(self.direction_value)
        self.direction_card.content_layout.addWidget(self.direction_detail)
        facts.addWidget(self.direction_card, 0, 0)

        self.confidence_card = Panel("НАСКОЛЬКО ПОДТВЕРЖДЕНО", compact=True)
        self.confidence_card.setObjectName("confidenceCard")
        self.confidence_value = QLabel("Не рассчитана")
        self.confidence_value.setObjectName("situationConfidence")
        self.confidence_value.setStyleSheet("font-size: 19px; font-weight: 600;")
        self.confidence_value.setWordWrap(True)
        self.confidence_detail = QLabel(
            "Нет интерпретированного набора признаков."
        )
        self.confidence_detail.setObjectName("situationConfidenceDetail")
        self.confidence_detail.setProperty("secondary", "true")
        self.confidence_detail.setWordWrap(True)
        self.confidence_card.content_layout.addWidget(self.confidence_value)
        self.confidence_card.content_layout.addWidget(self.confidence_detail)
        facts.addWidget(self.confidence_card, 0, 1)

        self.recommendation_card = Panel("ЧТО ДЕЛАТЬ ДАЛЬШЕ", compact=True)
        self.recommendation_card.setObjectName("recommendationCard")
        self.recommendation_value = QLabel(
            "Проверьте состояние процессора событий в диагностике."
        )
        self.recommendation_value.setObjectName("situationRecommendation")
        self.recommendation_value.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.recommendation_value.setWordWrap(True)
        self.recommendation_detail = QLabel(
            "До появления интерпретированного события не делайте вывод об источнике."
        )
        self.recommendation_detail.setObjectName("situationRecommendationDetail")
        self.recommendation_detail.setProperty("secondary", "true")
        self.recommendation_detail.setWordWrap(True)
        self.recommendation_card.content_layout.addWidget(self.recommendation_value)
        self.recommendation_card.content_layout.addWidget(self.recommendation_detail)
        facts.addWidget(self.recommendation_card, 1, 0, 1, 2)

        facts.setColumnStretch(0, 1)
        facts.setColumnStretch(1, 1)
        self.root_layout.addLayout(facts)

        self.sensor_notice = InlineNotice(
            "Ограничение сенсоров",
            "Пеленгация недоступна: нет подтверждённых данных направления.",
            level="warning",
        )
        self.sensor_notice.setObjectName("sensorFallbackNotice")
        self.root_layout.addWidget(self.sensor_notice)

        self.events_card = Panel("ПОСЛЕДНИЕ СОБЫТИЯ")
        self.events_card.setObjectName("recentEventsCard")
        event_tools = QHBoxLayout()
        event_tools.addStretch(1)
        self.important_only = QCheckBox("Показывать только важное")
        self.important_only.setObjectName("importantOnlyCheckBox")
        self.important_only.setChecked(True)
        self.important_only.toggled.connect(self._render_events)
        event_tools.addWidget(self.important_only)
        self.events_card.content_layout.addLayout(event_tools)
        self.events_list = QListWidget()
        self.events_list.setObjectName("simpleRecentEvents")
        self.events_list.setAlternatingRowColors(True)
        self.events_list.setMinimumHeight(126)
        self.events_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.events_card.content_layout.addWidget(self.events_list)
        self.root_layout.addWidget(self.events_card, 1)

        self._event_views: tuple[_EventView, ...] = ()
        self._apply_mode("unavailable")
        self._render_events()

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        situation = attr(snapshot, "operator_situation")
        if situation is None:
            self._show_unavailable()
            return

        mode = _mode_key(situation)
        self._apply_mode(mode, _key(attr(situation, "severity")))
        _, default_headline, _ = _MODE_PRESENTATION[mode]
        self.headline.setText(
            _first_text(
                situation,
                (
                    "headline_ru",
                    "headline",
                    "status_text_ru",
                    "status_text",
                ),
                default_headline,
            )
        )
        self.explanation.setText(
            _first_text(
                situation,
                (
                    "explanation_ru",
                    "explanation",
                    "summary_ru",
                    "summary",
                ),
                "Краткое объяснение от процессора событий пока недоступно.",
            )
        )
        self._refresh_direction(situation)
        self._refresh_confidence(situation)
        self._refresh_recommendation(situation)
        self._refresh_sensor_notice(situation)
        self._event_views = tuple(
            _event_view(event)
            for event in _event_items(situation)
        )
        self._render_events()

    def _show_unavailable(self) -> None:
        self._apply_mode("unavailable")
        self.headline.setText("Данные обстановки недоступны")
        self.explanation.setText(
            "signal_processor не предоставил операторское заключение. "
            "Сырые SDR-данные намеренно не интерпретируются внутри экрана."
        )
        self.direction_value.setText("Пеленгация недоступна")
        self.direction_detail.setText("Нет подтверждённых данных направления.")
        self.confidence_value.setText("Не рассчитана")
        self.confidence_detail.setText("Нет интерпретированного набора признаков.")
        self.recommendation_value.setText(
            "Проверьте состояние процессора событий в диагностике."
        )
        self.recommendation_detail.setText(
            "До появления интерпретированного события не делайте вывод об источнике."
        )
        self.sensor_notice.set_notice(
            "Ограничение сенсоров",
            "Пеленгация недоступна: нет подтверждённых данных направления.",
            level="warning",
        )
        self.sensor_notice.show()
        self._event_views = ()
        self._render_events()

    def _apply_mode(self, mode: str, severity: str = "") -> None:
        normalized = mode if mode in _MODE_PRESENTATION else "unavailable"
        label, _, level = _MODE_PRESENTATION[normalized]
        if severity in {"warning", "alarm", "critical"}:
            level = "critical" if severity in {"alarm", "critical"} else "warning"
        color, background = {
            "ready": (Colors.READY, Colors.READY_DARK),
            "info": (Colors.TEAL, Colors.TEAL_DARK),
            "warning": (Colors.WARNING, Colors.WARNING_DARK),
            "critical": (Colors.CRITICAL, Colors.CRITICAL_DARK),
            "neutral": (Colors.TEXT_SECONDARY, Colors.SURFACE_ALT),
        }[level]
        self.mode_label.setText(label)
        self.mode_label.setStyleSheet(
            f"color: {color}; background-color: {background}; "
            f"border: 1px solid {color}; border-radius: 5px; "
            "padding: 7px 10px; font-weight: 600;"
        )
        self.headline.setStyleSheet(
            f"color: {color}; font-size: 30px; font-weight: 600;"
        )
        self.header.status.set_status(label, level)

    def _refresh_direction(self, situation: object) -> None:
        direction = _first_value(
            situation,
            ("direction", "direction_estimate", "bearing"),
        )
        direct_text = _first_text(
            situation,
            (
                "direction_ru",
                "direction_text_ru",
                "sector_text_ru",
                "azimuth_text_ru",
            ),
            "",
        )
        direction_text = _first_text(
            direction,
            (
                "display_ru",
                "text_ru",
                "sector_text_ru",
                "azimuth_text_ru",
            ),
            direct_text,
        )
        if (
            direction is None
            and direct_text.casefold().startswith("пеленгация недоступна")
        ):
            _, separator, reason = direct_text.partition(":")
            self.direction_value.setText("Пеленгация недоступна")
            self.direction_detail.setText(
                reason.strip().rstrip(".")
                if separator and reason.strip()
                else "Свежего валидного азимута от внешнего пеленгатора нет."
            )
            return
        availability = _optional_bool(
            _first_value(direction, ("available", "is_available"))
        )
        if direction_text and availability is not False:
            self.direction_value.setText(direction_text)
            self.direction_detail.setText(
                _first_text(
                    direction,
                    ("explanation_ru", "detail_ru", "source_summary_ru"),
                    _first_text(
                        situation,
                        ("direction_explanation_ru", "direction_detail_ru"),
                        "Вывод передан процессором событий без расчётов в интерфейсе.",
                    ),
                )
            )
            return
        validated_external = _optional_bool(
            attr(direction, "validated_external")
        )
        if availability is True or validated_external is True:
            sector = _explicit_sector(direction)
            if sector:
                self.direction_value.setText(sector)
                self.direction_detail.setText(
                    _first_text(
                        direction,
                        ("explanation_ru", "detail_ru", "source_summary_ru"),
                        "Показано подтверждённое угловое наблюдение.",
                    )
                )
                return
        reason = _direction_unavailable_reason(situation, direction)
        self.direction_value.setText("Пеленгация недоступна")
        self.direction_detail.setText(reason)

    def _refresh_confidence(self, situation: object) -> None:
        confidence = attr(situation, "confidence")
        strength = _first_value(
            situation,
            ("evidence_strength", "confidence_level", "confirmation_level"),
        )
        if strength is None and confidence is not None and not isinstance(
            confidence, (int, float)
        ):
            strength = _first_value(
                confidence,
                ("evidence_strength", "band", "level", "label_ru", "label"),
            )
        label = _confidence_label(strength, confidence)
        self.confidence_value.setText(label)
        self.confidence_detail.setText(
            _first_text(
                confidence,
                (
                    "basis_ru",
                    "explanation_ru",
                    "detail_ru",
                    "source_summary_ru",
                ),
                _first_text(
                    situation,
                    (
                        "confidence_explanation_ru",
                        "evidence_summary_ru",
                        "source_attribution_ru",
                    ),
                    (
                        "Сила подтверждения отражает качество доступных "
                        "признаков, а не вероятность типа объекта."
                    ),
                ),
            )
        )

    def _refresh_recommendation(self, situation: object) -> None:
        recommendation = attr(situation, "recommendation")
        value = _first_text(
            recommendation,
            ("action_ru", "text_ru", "recommendation_ru", "title_ru"),
            _first_text(
                situation,
                (
                    "recommended_action_ru",
                    "recommendation_ru",
                    "operator_action_ru",
                    "action_ru",
                ),
                "Рекомендация пока не сформирована.",
            ),
        )
        detail = _first_text(
            recommendation,
            ("explanation_ru", "detail_ru", "reason_ru"),
            _first_text(
                situation,
                ("recommendation_explanation_ru", "action_explanation_ru"),
                "Сверьте доступность сенсоров и дождитесь обновления обстановки.",
            ),
        )
        self.recommendation_value.setText(value)
        self.recommendation_detail.setText(detail)

    def _refresh_sensor_notice(self, situation: object) -> None:
        fallback = _first_text(
            situation,
            (
                "sensor_fallback_explanation_ru",
                "fallback_explanation_ru",
                "sensor_limitations_ru",
            ),
            "",
        )
        if not fallback:
            fallback = _sensor_fallback_text(situation)
        if not fallback:
            self.sensor_notice.hide()
            return
        self.sensor_notice.set_notice(
            "Ограничение сенсоров",
            fallback,
            level="warning",
        )
        self.sensor_notice.show()

    def _render_events(self, _checked: bool = False) -> None:
        self.events_list.clear()
        events = (
            tuple(event for event in self._event_views if event.important)
            if self.important_only.isChecked()
            else self._event_views
        )
        if not events:
            message = (
                "Важных событий нет."
                if self._event_views and self.important_only.isChecked()
                else "Событий пока нет."
            )
            item = QListWidgetItem(message)
            item.setForeground(QBrush(QColor(Colors.MUTED)))
            self.events_list.addItem(item)
            return
        for event in events:
            item = QListWidgetItem(event.rendered)
            item.setForeground(
                QBrush(
                    QColor(
                        {
                            "critical": Colors.CRITICAL,
                            "warning": Colors.WARNING,
                            "info": Colors.TEAL,
                            "ready": Colors.READY,
                        }.get(event.severity, Colors.TEXT_SECONDARY)
                    )
                )
            )
            item.setToolTip(event.detail)
            self.events_list.addItem(item)


def _mode_key(situation: object) -> str:
    raw = _key(
        _first_value(
            situation,
            ("mode", "state", "situation_state", "operating_state"),
        )
    )
    normalized = _MODE_ALIASES.get(raw, raw)
    return normalized if normalized in _MODE_PRESENTATION else "unavailable"


def _first_value(source: object | None, names: tuple[str, ...]) -> object | None:
    for name in names:
        value = attr(source, name)
        if value is not None:
            return cast(object, value)
    return None


def _first_text(
    source: object | None,
    names: tuple[str, ...],
    default: str,
) -> str:
    for name in names:
        value = attr(source, name)
        if value is None:
            continue
        text = value_of(value).strip()
        if text:
            return text
    return default


def _key(value: object | None) -> str:
    return value_of(value).strip().lower().replace("-", "_").replace(" ", "_")


def _optional_bool(value: object | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "available", "ready", "online"}:
            return True
        if normalized in {
            "false",
            "no",
            "0",
            "unavailable",
            "absent",
            "offline",
            "failed",
            "disconnected",
        }:
            return False
    return None


def _explicit_sector(direction: object | None) -> str:
    sector = attr(direction, "sector")
    if isinstance(sector, str) and sector.strip():
        return sector.strip()
    low = _number(_first_value(direction, ("sector_start_deg", "sector_low_deg")))
    high = _number(_first_value(direction, ("sector_end_deg", "sector_high_deg")))
    if low is not None and high is not None:
        return f"Сектор {low:.0f}–{high:.0f}°"
    bearing = _number(_first_value(direction, ("bearing_deg", "azimuth_deg")))
    uncertainty = _number(
        _first_value(direction, ("uncertainty_deg", "half_width_deg"))
    )
    if bearing is not None and uncertainty is not None:
        return f"Азимут {bearing:.1f}° · сектор ±{uncertainty:.1f}°"
    if bearing is not None:
        return f"Азимут {bearing:.1f}°"
    return ""


def _number(value: object | None) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _direction_unavailable_reason(
    situation: object,
    direction: object | None,
) -> str:
    explicit = _first_text(
        direction,
        ("unavailable_reason_ru", "reason_ru", "explanation_ru", "detail_ru"),
        _first_text(
            situation,
            ("direction_unavailable_reason_ru", "direction_explanation_ru"),
            "",
        ),
    )
    if explicit:
        return explicit
    for sensor in _sensor_items(situation):
        name = _sensor_name(sensor)
        kind = _key(attr(sensor, "sensor_kind"))
        if "kraken" not in name.casefold() and kind != "direction_finder":
            continue
        if _sensor_unavailable(sensor):
            reason = _first_text(
                sensor,
                (
                    "message_ru",
                    "reason_ru",
                    "explanation_ru",
                    "fallback_ru",
                ),
                "KrakenSDR не подключён.",
            )
            return f"Пеленгация недоступна: {reason.rstrip('.')}"
    return "Нет подтверждённых данных направления."


def _confidence_label(
    strength: object | None,
    confidence: object | None,
) -> str:
    if isinstance(strength, str):
        stripped = strength.strip()
        mapped = _CONFIDENCE_LABELS.get(_key(strength))
        return mapped if mapped is not None else stripped
    if strength is not None:
        mapped = _CONFIDENCE_LABELS.get(_key(strength))
        if mapped is not None:
            return mapped
    numeric = _number(confidence)
    if numeric is None:
        numeric = _number(
            _first_value(confidence, ("score", "value", "normalized"))
        )
    if numeric is None:
        return "Не рассчитана"
    normalized = numeric / 100.0 if numeric > 1.0 else numeric
    if normalized < 0.35:
        return "Низкая"
    if normalized < 0.7:
        return "Средняя"
    return "Высокая"


def _sensor_items(situation: object) -> tuple[object, ...]:
    raw = _first_value(
        situation,
        ("sensor_availability", "sensors", "sensor_states"),
    )
    return _as_items(raw)


def _event_items(situation: object) -> tuple[object, ...]:
    raw = _first_value(
        situation,
        ("recent_events", "events", "normalized_events"),
    )
    return _as_items(raw)


def _as_items(raw: object | None) -> tuple[object, ...]:
    if isinstance(raw, dict):
        return tuple(raw.values())
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        return tuple(raw)
    return ()


def _sensor_name(sensor: object) -> str:
    explicit = _first_text(
        sensor,
        ("display_name", "name"),
        "",
    )
    if explicit:
        return explicit
    kind = _key(_first_value(sensor, ("sensor_kind", "kind")))
    if kind in _SENSOR_LABELS:
        return _SENSOR_LABELS[kind]
    return _first_text(sensor, ("sensor_id", "source_id"), "Сенсор")


def _sensor_unavailable(sensor: object) -> bool:
    available = _optional_bool(
        _first_value(sensor, ("available", "is_available"))
    )
    if available is not None:
        return not available
    state = _key(_first_value(sensor, ("state", "availability")))
    return state in {
        "absent",
        "unavailable",
        "offline",
        "failed",
        "disconnected",
        "disabled",
        "stale",
    }


def _sensor_fallback_text(situation: object) -> str:
    messages: list[str] = []
    for sensor in _sensor_items(situation):
        state = _key(_first_value(sensor, ("state", "availability")))
        if not _sensor_unavailable(sensor) and state != "degraded":
            continue
        name = _sensor_name(sensor)
        reason = _first_text(
            sensor,
            ("message_ru", "fallback_ru", "reason_ru", "explanation_ru"),
            "источник данных недоступен",
        )
        messages.append(f"{name}: {reason.rstrip('.')}.")
    return " ".join(messages[:3])


def _event_view(event: object) -> _EventView:
    event_type = _key(
        _first_value(event, ("event_type", "type", "kind", "code"))
    )
    severity = _key(attr(event, "severity", "info"))
    if severity in {"error", "danger", "alert", "alarm", "high"}:
        severity = "critical"
    elif severity in {"medium", "warn"}:
        severity = "warning"
    elif severity not in {"critical", "warning", "info", "ready"}:
        severity = "info"
    title = _first_text(
        event,
        (
            "headline_ru",
            "title_ru",
            "summary_ru",
            "message_ru",
            "label_ru",
        ),
        _EVENT_TITLES.get(event_type, "Событие сенсора"),
    )
    detail = _first_text(
        event,
        (
            "explanation_ru",
            "summary_ru",
            "detail_ru",
            "recommendation_ru",
        ),
        "",
    )
    timestamp = _format_timestamp(
        _first_value(event, ("observed_at", "timestamp", "occurred_at"))
    )
    explicit_important = _optional_bool(
        _first_value(event, ("important", "is_important"))
    )
    important = (
        explicit_important
        if explicit_important is not None
        else severity in {"critical", "warning"}
        or event_type in _IMPORTANT_EVENT_TYPES
    )
    return _EventView(
        title=title,
        detail=detail,
        timestamp=timestamp,
        severity=severity,
        important=important,
    )


def _format_timestamp(value: object | None) -> str:
    if isinstance(value, datetime):
        return value.astimezone().strftime("%H:%M:%S")
    if isinstance(value, str):
        stripped = value.strip()
        if "T" in stripped:
            try:
                return datetime.fromisoformat(
                    stripped.replace("Z", "+00:00")
                ).astimezone().strftime("%H:%M:%S")
            except ValueError:
                pass
        return stripped
    return ""


__all__ = ["SimpleSituationPage"]
