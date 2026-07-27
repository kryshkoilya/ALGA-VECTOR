"""Target-centric, human-readable situation page for non-technical operators."""

from __future__ import annotations

# ruff: noqa: RUF001
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..runtime import attr, current_snapshot, value_of
from ..theme import Colors
from ..widgets import (
    CompactSectorView,
    InlineNotice,
    Panel,
    SectorViewState,
    SensorReadinessState,
    SensorReadinessStrip,
    TargetCardState,
    TargetSummaryCard,
)
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

_STAGE_PRESENTATION = {
    "background": ("Фон", "neutral"),
    "suspicious_activity": ("Подозрительная активность", "warning"),
    "probable_source": ("Вероятный источник", "info"),
    "probable_target": ("Вероятная цель", "warning"),
    "confirmed_target": ("Подтверждённая цель", "ready"),
}

_STAGE_ALIASES = {
    "none": "background",
    "quiet": "background",
    "silence": "background",
    "background_only": "background",
    "activity": "suspicious_activity",
    "suspicious": "suspicious_activity",
    "candidate": "suspicious_activity",
    "likely_source": "probable_source",
    "source_likely": "probable_source",
    "likely_target": "probable_target",
    "target_likely": "probable_target",
    "confirmed": "confirmed_target",
    "target_confirmed": "confirmed_target",
}

_EVENT_TITLES = {
    "noise_background": "Обычный фон",
    "radio_activity_detected": "Обнаружена RF-активность",
    "likely_handheld_radio": "Вероятная портативная радиосвязь",
    "likely_video_link": "Вероятный видеоканал",
    "likely_drone_signature": "Сигнатура требует проверки",
    "adsb_contact": "Контакт ADS-B",
    "acoustic_anomaly": "Акустическая аномалия",
    "direction_estimated": "Получен сектор направления",
    "multisensor_correlated": "Согласованная активность нескольких сенсоров",
    "target_confirmed": "Цель подтверждена независимыми данными",
    "sensor_unavailable": "Сенсор недоступен",
}

_TYPE_LABELS = {
    "noise_background": "Фоновая активность",
    "radio_activity_detected": "Неподтверждённый RF-источник",
    "likely_handheld_radio": "Вероятная портативная радиосвязь",
    "likely_video_link": "Вероятный видеоканал",
    "likely_drone_signature": "Вероятная воздушная цель",
    "acoustic_anomaly": "Акустическая аномалия",
    "multisensor_correlated": "Мультисенсорная аномалия",
    "target_confirmed": "Подтверждённая цель",
    "unknown": "Неподтверждённый источник",
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

_READINESS_META = {
    "tinysa": (
        "TinySA",
        "RF-триггер не участвует в раннем обнаружении",
    ),
    "rtlsdr": (
        "RTL-SDR",
        "Спектральное подтверждение недоступно",
    ),
    "krakensdr": (
        "KrakenSDR",
        "Направление цели не определяется",
    ),
    "acoustic": (
        "Акустика",
        "Звуковое подтверждение отключено",
    ),
    "adsb": (
        "ADS-B",
        "Гражданский воздушный контекст недоступен",
    ),
    "passive_radar": (
        "Пассивный радар",
        "Радиолокационное подтверждение недоступно",
    ),
    "fusion": (
        "Fusion",
        "Объединение независимых наблюдений ограничено",
    ),
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
    """Decision screen backed by interpreted situation and fused-target contracts.

    The view consumes the 1.0 interpreted snapshot contracts:
    ``operator_situation``, ``current_target``/``targets`` and
    ``sensor_readiness`` through compatibility-safe duck typing. It never
    reconstructs a conclusion from IQ, RSSI, a waterfall, raw spectrum,
    signal level, or an unvalidated geographic position.
    """

    def __init__(
        self,
        runtime: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            runtime,
            "Простая обстановка",
            "Главное решение за несколько секунд — без сырых инженерных данных",
            action_text="Обновить",
            parent=parent,
        )
        self.setObjectName("simpleSituationPage")
        self.header.action.clicked.connect(lambda: self.refresh())

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("simpleSituationScroll")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.body_scroll.setStyleSheet(
            "QScrollArea#simpleSituationScroll { border: 0; background: transparent; }"
        )
        self.body = QWidget()
        self.body.setObjectName("simpleSituationContent")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        self.body_scroll.setWidget(self.body)
        self.root_layout.addWidget(self.body_scroll, 1)

        self.hero = Panel("ОБСТАНОВКА СЕЙЧАС")
        self.hero.setObjectName("situationHeroCard")
        self.hero.setMinimumHeight(104)
        self.hero.setMaximumHeight(124)
        self.hero.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        hero_row = QHBoxLayout()
        hero_row.setSpacing(14)
        self.mode_label = QLabel("НЕТ ДАННЫХ")
        self.mode_label.setObjectName("situationMode")
        self.mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_label.setMinimumWidth(136)
        self.mode_label.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        hero_row.addWidget(self.mode_label, 0, Qt.AlignmentFlag.AlignTop)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(5)
        self.headline = QLabel("Обстановка недоступна")
        self.headline.setObjectName("situationHeadline")
        self.headline.setStyleSheet("font-size: 29px; font-weight: 600;")
        self.headline.setWordWrap(True)
        self.explanation = QLabel(
            "Интерпретированный вывод ещё не получен. Сырые данные не выдаются "
            "за операторское заключение."
        )
        self.explanation.setObjectName("situationExplanation")
        self.explanation.setProperty("secondary", "true")
        self.explanation.setStyleSheet("font-size: 13px;")
        self.explanation.setWordWrap(True)
        hero_text.addWidget(self.headline)
        hero_text.addWidget(self.explanation)
        hero_row.addLayout(hero_text, 1)
        self.hero.content_layout.addLayout(hero_row)
        self.body_layout.addWidget(self.hero)

        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        self.target_card = TargetSummaryCard()
        self.sector_view = CompactSectorView()
        target_row.addWidget(self.target_card, 3)
        target_row.addWidget(self.sector_view, 2)
        self.body_layout.addLayout(target_row)

        # Compatibility aliases remain available to plugins and 0.7 UI tests.
        self.direction_card = self.sector_view
        self.direction_value = self.sector_view.value_label
        self.direction_detail = self.sector_view.detail_label
        self.confidence_card = self.target_card
        self.confidence_value = QLabel(self)
        self.confidence_value.setObjectName("situationConfidenceLegacy")
        self.confidence_value.hide()
        self.confidence_detail = QLabel(self)
        self.confidence_detail.setObjectName("situationConfidenceDetailLegacy")
        self.confidence_detail.hide()

        self.recommendation_card = Panel("ЧТО ДЕЛАТЬ ДАЛЬШЕ", compact=True)
        self.recommendation_card.setObjectName("recommendationCard")
        self.recommendation_card.setMinimumHeight(68)
        self.recommendation_card.setMaximumHeight(86)
        self.recommendation_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
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
        self.body_layout.addWidget(self.recommendation_card)

        self.sensor_notice = InlineNotice(
            "Ограничение сенсоров",
            "Пеленгация недоступна: нет подтверждённых данных направления.",
            level="warning",
        )
        self.sensor_notice.setObjectName("sensorFallbackNotice")
        self.body_layout.addWidget(self.sensor_notice)

        self.events_card = Panel("ПОСЛЕДНИЕ ВАЖНЫЕ СОБЫТИЯ")
        self.events_card.setObjectName("recentEventsCard")
        self.events_card.setMinimumHeight(106)
        self.events_card.setMaximumHeight(132)
        self.events_card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
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
        self.events_list.setMinimumHeight(62)
        self.events_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.events_card.content_layout.addWidget(self.events_list)
        self.body_layout.addWidget(self.events_card)

        self.sensor_strip = SensorReadinessStrip()
        self.body_layout.addWidget(self.sensor_strip)

        self._event_views: tuple[_EventView, ...] = ()
        self._apply_mode("unavailable")
        self._set_target(None, None, "unavailable")
        self._refresh_sensor_strip(None, None)
        self._render_events()

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        situation = attr(snapshot, "operator_situation")
        reference = _reference_time(snapshot, situation)
        target = _current_target(snapshot, situation, reference)
        if situation is None and target is None:
            self._show_unavailable(snapshot)
            return

        mode = _mode_key(situation, target)
        severity = _key(
            _first_value(situation, ("severity",))
            or _first_value(target, ("severity",))
        )
        self._apply_mode(mode, severity)
        if target is not None:
            headline = _first_text(
                target,
                ("headline_ru", "operator_label"),
                _probable_type(
                    target,
                    attr(situation, "primary_event"),
                    mode,
                ),
            )
            explanation = _first_text(
                target,
                (
                    "short_operator_summary",
                    "operator_summary_ru",
                    "operator_explanation",
                    "explanation_ru",
                    "summary_ru",
                ),
                "Краткое объяснение цели пока недоступно.",
            )
        else:
            headline = _first_text(
                situation,
                ("headline_ru", "headline", "status_text_ru", "status_text"),
                "Активная цель не сформирована",
            )
            explanation = _first_text(
                situation,
                ("explanation_ru", "explanation", "summary_ru", "summary"),
                "Краткое объяснение от процессора событий пока недоступно.",
            )
        self.headline.setText(headline)
        self.explanation.setText(explanation)
        self._set_target(target, situation, mode)
        self._refresh_direction(target, situation, reference)
        self._refresh_legacy_confidence(situation)
        self._refresh_recommendation(target, situation)
        self._refresh_sensor_notice(target, situation)
        self._event_views = tuple(
            _event_view(event)
            for event in _event_items(target, situation)
        )
        self._render_events()
        self._refresh_sensor_strip(snapshot, situation)

    def _show_unavailable(self, snapshot: object | None) -> None:
        self._apply_mode("unavailable")
        self.headline.setText("Данные обстановки недоступны")
        self.explanation.setText(
            "signal_processor не предоставил операторское заключение. "
            "Сырые SDR-данные намеренно не интерпретируются внутри экрана."
        )
        self._set_target(None, None, "unavailable")
        self.sector_view.set_direction(
            SectorViewState(
                available=False,
                label="Пеленгация недоступна",
                detail="Нет подтверждённых данных направления.",
            )
        )
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
        self._refresh_sensor_strip(snapshot, None)

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
            f"color: {color}; font-size: 29px; font-weight: 600;"
        )
        self.header.status.set_status(label, level)

    def _set_target(
        self,
        target: object | None,
        situation: object | None,
        mode: str,
    ) -> None:
        stage_key = _confirmation_stage(target, situation, mode)
        stage_text, stage_level = _stage_presentation(stage_key)
        primary_event = attr(situation, "primary_event")
        probable_type = _probable_type(target, primary_event, mode)
        summary = _first_text(
            target,
            (
                "short_operator_summary",
                "operator_summary_ru",
                "operator_explanation",
                "explanation_ru",
                "summary_ru",
                "summary",
            ),
            _first_text(
                primary_event,
                ("explanation_ru", "summary_ru"),
                _first_text(
                    situation,
                    ("explanation_ru", "summary_ru"),
                    "Система продолжает наблюдение и объединяет события.",
                ),
            ),
        )
        target_id = _first_text(target, ("target_id", "id"), "")
        last_seen = _format_timestamp(
            _first_value(
                target,
                ("last_seen", "last_observed_at", "updated_at", "observed_at"),
            )
            or _first_value(primary_event, ("observed_at", "timestamp"))
        )
        sensors = _target_sensors(target, primary_event)
        self.target_card.set_target(
            TargetCardState(
                probable_type=probable_type,
                confirmation_stage=stage_text,
                summary=summary,
                target_id=target_id,
                last_seen=last_seen,
                sensors=sensors,
                stage_level=stage_level,
            )
        )

    def _refresh_direction(
        self,
        target: object | None,
        situation: object | None,
        reference: datetime | None,
    ) -> None:
        direction = _first_value(
            target,
            ("direction", "direction_estimate", "bearing"),
        )
        if direction is None:
            self.sector_view.set_direction(
                SectorViewState(
                    available=False,
                    label="Пеленгация недоступна",
                    detail=_direction_unavailable_reason(situation, None),
                )
            )
            return
        direct_text = _first_text(
            target,
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
            ("display_ru", "text_ru", "sector_text_ru", "azimuth_text_ru"),
            direct_text,
        )
        explicit_angles = _direction_angles(direction, direction_text)
        if not _direction_is_current_and_associated(
            target,
            direction,
            reference,
        ):
            self.sector_view.set_direction(
                SectorViewState(
                    available=False,
                    label="Пеленгация недоступна",
                    detail=_direction_unavailable_reason(situation, None),
                )
            )
            return
        bearing, start, end = explicit_angles
        label = direction_text or _format_explicit_sector(bearing, start, end)
        detail = _first_text(
            direction,
            ("explanation_ru", "detail_ru", "source_summary_ru"),
            "Показано подтверждённое угловое наблюдение.",
        )
        source = _first_text(
            direction,
            ("source_name", "source_id", "sensor_id"),
            "",
        )
        self.sector_view.set_direction(
            SectorViewState(
                available=True,
                label=label or "Подтверждённый сектор",
                detail=detail,
                bearing_deg=bearing,
                sector_start_deg=start,
                sector_end_deg=end,
                source=source,
            )
        )

    def _refresh_legacy_confidence(self, situation: object | None) -> None:
        """Keep 0.7 integrations readable while the visible UI uses stages."""

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
        self.confidence_value.setText(_legacy_confidence_label(strength, confidence))
        self.confidence_detail.setText(
            _first_text(
                confidence,
                ("basis_ru", "explanation_ru", "detail_ru", "source_summary_ru"),
                _first_text(
                    situation,
                    (
                        "confidence_explanation_ru",
                        "evidence_summary_ru",
                        "source_attribution_ru",
                    ),
                    "Сила признаков не является вероятностью типа объекта.",
                ),
            )
        )

    def _refresh_recommendation(
        self,
        target: object | None,
        situation: object | None,
    ) -> None:
        recommendation = _first_value(
            target,
            ("recommendation",),
        ) or attr(situation, "recommendation")
        value = _first_text(
            recommendation,
            (
                "recommended_action_short",
                "action_ru",
                "text_ru",
                "recommendation_ru",
                "title_ru",
            ),
            _first_text(
                target,
                (
                    "recommended_action_short",
                    "recommended_action_ru",
                    "recommendation_ru",
                ),
                _first_text(
                    situation,
                    (
                        "recommended_action_short",
                        "recommended_action_ru",
                        "recommendation_ru",
                        "operator_action_ru",
                        "action_ru",
                    ),
                    "Рекомендация пока не сформирована.",
                ),
            ),
        )
        detail = _first_text(
            recommendation,
            (
                "recommended_action_detailed",
                "explanation_ru",
                "detail_ru",
                "reason_ru",
            ),
            _first_text(
                target,
                (
                    "recommended_action_detailed",
                    "recommendation_explanation_ru",
                ),
                _first_text(
                    situation,
                    (
                        "recommended_action_detailed",
                        "recommendation_explanation_ru",
                        "action_explanation_ru",
                    ),
                    "Сверьте доступность сенсоров и дождитесь обновления обстановки.",
                ),
            ),
        )
        self.recommendation_value.setText(value)
        self.recommendation_detail.setText(detail)

    def _refresh_sensor_notice(
        self,
        target: object | None,
        situation: object | None,
    ) -> None:
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
            limitations = tuple(
                value_of(item).strip()
                for item in _as_items(attr(target, "limitations"))
                if value_of(item).strip()
            )
            fallback = " ".join(limitations[:2])
        if not fallback:
            self.sensor_notice.hide()
            return
        self.sensor_notice.set_notice(
            "Ограничение подтверждения",
            fallback,
            level="warning",
        )
        self.sensor_notice.show()

    def _refresh_sensor_strip(
        self,
        snapshot: object | None,
        situation: object | None,
    ) -> None:
        self.sensor_strip.set_states(
            _sensor_readiness_states(snapshot, situation)
        )

    def _render_events(self, _checked: bool = False) -> None:
        self.events_list.clear()
        events = (
            tuple(event for event in self._event_views if event.important)
            if self.important_only.isChecked()
            else self._event_views
        )[:5]
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


def _mode_key(
    situation: object | None,
    target: object | None,
) -> str:
    if target is not None:
        target_stage = _confirmation_stage(target, situation, "unavailable")
        if target_stage == "confirmed_target":
            return "confirmed"
        if target_stage in {
            "suspicious_activity",
            "probable_source",
            "probable_target",
        }:
            return "activity"
    primary_event = attr(situation, "primary_event")
    event_type = _key(
        _first_value(primary_event, ("event_type", "type", "kind", "code"))
    )
    if target is None and event_type == "direction_estimated":
        return "background"
    raw = _key(
        _first_value(
            situation,
            ("mode", "state", "situation_state", "operating_state"),
        )
    )
    normalized = _MODE_ALIASES.get(raw, raw)
    if normalized in _MODE_PRESENTATION:
        return normalized
    stage = _confirmation_stage(target, situation, "unavailable")
    if stage == "confirmed_target":
        return "confirmed"
    if stage in {"suspicious_activity", "probable_source", "probable_target"}:
        return "activity"
    if target is not None:
        return "background"
    return "unavailable"


def _parse_aware_time(value: object | None) -> datetime | None:
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _reference_time(
    snapshot: object | None,
    situation: object | None,
) -> datetime | None:
    snapshot_value = _first_value(
        snapshot,
        ("captured_at", "generated_at", "observed_at"),
    )
    if snapshot_value is not None:
        return _parse_aware_time(snapshot_value)
    return _parse_aware_time(
        _first_value(situation, ("generated_at", "captured_at", "observed_at"))
    )


def _target_is_valid_at(target: object, reference: datetime) -> bool:
    for field_name in (
        "created_at",
        "updated_at",
        "last_seen",
        "last_observed_at",
        "observed_at",
    ):
        raw = attr(target, field_name)
        if raw is None:
            continue
        timestamp = _parse_aware_time(raw)
        if timestamp is None or timestamp > reference:
            return False

    valid_from_raw = _first_value(target, ("valid_from", "valid_after"))
    if valid_from_raw is not None:
        valid_from = _parse_aware_time(valid_from_raw)
        if valid_from is None or valid_from > reference:
            return False

    valid_until_raw = _first_value(target, ("valid_until", "expires_at"))
    if valid_until_raw is not None:
        valid_until = _parse_aware_time(valid_until_raw)
        if valid_until is None or reference > valid_until:
            return False
    return True


def _direction_is_current_and_associated(
    target: object | None,
    direction: object,
    reference: datetime | None,
) -> bool:
    if target is None or reference is None:
        return False
    if _optional_bool(attr(direction, "validated_external")) is not True:
        return False
    if _optional_bool(
        _first_value(direction, ("available", "is_available"))
    ) is not True:
        return False

    target_type = type(target)
    nested_production_direction = (
        target_type.__module__ == "alga_vector.targets.models"
        and target_type.__name__ == "FusedTarget"
        and attr(target, "direction") is direction
    )
    if not nested_production_direction:
        target_id = _first_text(target, ("target_id", "id", "track_id"), "")
        associated_target_id = _first_text(
            direction,
            ("associated_target_id", "target_id", "track_id"),
            _first_text(target, ("direction_target_id",), ""),
        )
        if not target_id or associated_target_id != target_id:
            return False

    observed_at = _parse_aware_time(attr(direction, "observed_at"))
    valid_until = _parse_aware_time(attr(direction, "valid_until"))
    if (
        observed_at is None
        or valid_until is None
        or valid_until <= observed_at
        or observed_at > reference
        or reference > valid_until
    ):
        return False

    bearing = _number(
        _first_value(direction, ("bearing_deg", "azimuth_deg", "bearing"))
    )
    uncertainty = _number(
        _first_value(direction, ("uncertainty_deg", "half_width_deg"))
    )
    return (
        bearing is not None
        and uncertainty is not None
        and math.isfinite(bearing)
        and math.isfinite(uncertainty)
        and 0.0 <= bearing < 360.0
        and 0.0 <= uncertainty <= 180.0
    )


def _current_target(
    snapshot: object | None,
    situation: object | None,
    reference: datetime | None,
) -> object | None:
    direct = _first_value(snapshot, ("current_target", "active_target"))
    if direct is not None and _is_current_target(direct, reference):
        return direct
    direct = _first_value(situation, ("current_target", "target"))
    if direct is not None and _is_current_target(direct, reference):
        return direct
    targets = _as_items(_first_value(snapshot, ("targets", "fused_targets")))
    for target in targets:
        if _is_current_target(target, reference):
            return target
    return None


def _is_current_target(
    target: object,
    reference: datetime | None,
) -> bool:
    if reference is None:
        return False
    lifecycle = _key(
        _first_value(target, ("lifecycle", "state", "status"))
    )
    if lifecycle != "active":
        return False
    active = _optional_bool(
        _first_value(target, ("active", "is_active"))
    )
    if active is False:
        return False
    return _target_is_valid_at(target, reference)


def _confirmation_stage(
    target: object | None,
    situation: object | None,
    mode: str,
) -> str:
    raw_value = _first_value(
        target,
        ("confirmation_stage", "stage", "target_stage"),
    ) or _first_value(
        situation,
        ("confirmation_stage", "target_stage"),
    )
    raw = _key(raw_value)
    normalized = _STAGE_ALIASES.get(raw, raw)
    if normalized in _STAGE_PRESENTATION:
        return normalized
    if mode == "confirmed":
        return "confirmed_target"
    primary_event = attr(situation, "primary_event")
    event_type = _key(
        _first_value(primary_event, ("event_type", "type", "kind", "code"))
    )
    if event_type == "target_confirmed":
        return "confirmed_target"
    if event_type == "likely_drone_signature":
        return "probable_target"
    if event_type == "direction_estimated":
        return "background"
    if event_type in {
        "likely_video_link",
        "likely_handheld_radio",
        "multisensor_correlated",
    }:
        return "probable_source"
    if mode == "activity":
        return "suspicious_activity"
    return "background"


def _stage_presentation(stage: str) -> tuple[str, str]:
    if stage in _STAGE_PRESENTATION:
        return _STAGE_PRESENTATION[stage]
    if stage and "%" not in stage:
        return stage.replace("_", " ").capitalize(), "warning"
    return _STAGE_PRESENTATION["background"]


def _probable_type(
    target: object | None,
    primary_event: object | None,
    mode: str,
) -> str:
    explicit = _first_text(
        target,
        (
            "operator_label",
            "probable_type_ru",
            "type_label_ru",
            "label_ru",
            "probable_type",
            "type",
        ),
        "",
    )
    if explicit:
        mapped = _TYPE_LABELS.get(_key(explicit))
        return mapped if mapped is not None else explicit
    if target is None and mode in {"quiet", "background", "unavailable"}:
        return "Активная цель не сформирована"
    event_type = _key(
        _first_value(primary_event, ("event_type", "type", "kind", "code"))
    )
    if target is None and event_type == "direction_estimated":
        return "Активная цель не сформирована"
    if event_type:
        return _TYPE_LABELS.get(event_type, _EVENT_TITLES.get(event_type, "Наблюдение"))
    return "Неподтверждённый источник"


def _target_sensors(
    target: object | None,
    primary_event: object | None,
) -> str:
    raw = _first_value(
        target,
        ("sensors_used", "sensor_ids", "sources", "source_attribution"),
    )
    if raw is None:
        raw = attr(primary_event, "sources")
    labels: list[str] = []
    for source in _as_items(raw):
        if isinstance(source, str):
            label = _SENSOR_LABELS.get(_key(source), source)
        else:
            kind = _key(_first_value(source, ("sensor_kind", "kind")))
            label = _first_text(
                source,
                ("display_name", "name"),
                _SENSOR_LABELS.get(
                    kind,
                    _first_text(source, ("sensor_id", "source_id"), ""),
                ),
            )
        if label and label not in labels:
            labels.append(label)
    return " · ".join(labels[:4])


def _direction_angles(
    direction: object | None,
    text: str,
) -> tuple[float | None, float | None, float | None]:
    bearing = _number(
        _first_value(direction, ("bearing_deg", "azimuth_deg", "bearing"))
    )
    start = _number(
        _first_value(direction, ("sector_start_deg", "sector_low_deg"))
    )
    end = _number(
        _first_value(direction, ("sector_end_deg", "sector_high_deg"))
    )
    uncertainty = _number(
        _first_value(direction, ("uncertainty_deg", "half_width_deg"))
    )
    if bearing is not None and uncertainty is not None:
        start = (bearing - uncertainty) % 360.0
        end = (bearing + uncertainty) % 360.0
    if (start is None or end is None) and text:
        match = re.search(
            r"(\d+(?:[.,]\d+)?)\s*[–—-]\s*(\d+(?:[.,]\d+)?)\s*°",
            text,
        )
        if match is not None:
            start = float(match.group(1).replace(",", "."))
            end = float(match.group(2).replace(",", "."))
    return bearing, start, end


def _format_explicit_sector(
    bearing: float | None,
    start: float | None,
    end: float | None,
) -> str:
    if start is not None and end is not None and bearing is not None:
        return f"Сектор {start:.0f}–{end:.0f}° · азимут {bearing:.0f}°"
    if start is not None and end is not None:
        return f"Сектор {start:.0f}–{end:.0f}°"
    if bearing is not None:
        return f"Азимут {bearing:.1f}°"
    return ""


def _direction_unavailable_reason(
    situation: object | None,
    direction: object | None,
) -> str:
    explicit = _first_text(
        direction,
        ("unavailable_reason_ru", "reason_ru", "explanation_ru", "detail_ru"),
        _first_text(situation, ("direction_unavailable_reason_ru",), ""),
    )
    if not explicit:
        direction_text = _first_text(situation, ("direction_ru",), "")
        if direction_text.casefold().startswith("пеленгация недоступна"):
            explicit = direction_text
    if explicit:
        if explicit.casefold().startswith("пеленгация недоступна"):
            _, separator, reason = explicit.partition(":")
            return reason.strip().rstrip(".") if separator else explicit
        return explicit
    for sensor in _sensor_items(situation):
        name = _sensor_name(sensor)
        kind = _key(attr(sensor, "sensor_kind"))
        if "kraken" not in name.casefold() and kind != "direction_finder":
            continue
        if _sensor_unavailable(sensor):
            reason = _first_text(
                sensor,
                ("message_ru", "reason_ru", "explanation_ru", "fallback_ru"),
                "KrakenSDR не подключён.",
            )
            return reason.rstrip(".")
    return "Нет свежего валидного азимута от внешнего пеленгатора."


def _legacy_confidence_label(
    strength: object | None,
    confidence: object | None,
) -> str:
    labels = {
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
    if isinstance(strength, str):
        stripped = strength.strip()
        return labels.get(_key(strength), stripped)
    if strength is not None:
        mapped = labels.get(_key(strength))
        if mapped is not None:
            return mapped
    numeric = _number(confidence)
    if numeric is None:
        numeric = _number(_first_value(confidence, ("score", "value", "normalized")))
    if numeric is None:
        return "Не рассчитана"
    normalized = numeric / 100.0 if numeric > 1.0 else numeric
    if normalized < 0.35:
        return "Низкая"
    if normalized < 0.7:
        return "Средняя"
    return "Высокая"


def _sensor_readiness_states(
    snapshot: object | None,
    situation: object | None,
) -> tuple[SensorReadinessState, ...]:
    candidates: list[tuple[str, object]] = []
    for raw in (
        _first_value(snapshot, ("sensor_readiness", "readiness")),
        _first_value(situation, ("sensor_availability", "sensors", "sensor_states")),
    ):
        candidates.extend(_readiness_candidates(raw))

    resolved: dict[str, SensorReadinessState] = {}
    for hint, sensor in candidates:
        role = _sensor_role(hint, sensor)
        if role is None or role in resolved:
            continue
        name, impact = _READINESS_META[role]
        state = _readiness_state(sensor)
        reason = _first_text(
            sensor,
            (
                "short_reason",
                "message_ru",
                "reason_ru",
                "explanation_ru",
                "fallback_ru",
                "message",
                "reason",
            ),
            "",
        )
        if not reason and isinstance(sensor, str):
            reason = sensor
        if not reason:
            reason = (
                "Работает штатно"
                if state == "ready"
                else "Доступность ограничена"
                if state == "limited"
                else "Нет данных о сенсоре"
            )
        explicit_impact = _first_text(
            sensor,
            ("impact_ru", "impact", "capability_impact_ru"),
            impact,
        )
        resolved[role] = SensorReadinessState(
            key=role,
            name=name,
            state=state,
            reason=reason,
            impact=explicit_impact,
        )

    return tuple(
        resolved.get(
            role,
            SensorReadinessState(
                key=role,
                name=name,
                state="unavailable",
                reason="Не подключён или данные не поступают",
                impact=impact,
            ),
        )
        for role, (name, impact) in _READINESS_META.items()
    )


def _readiness_candidates(raw: object | None) -> list[tuple[str, object]]:
    if raw is None or isinstance(raw, (int, float, bool)):
        return []
    if isinstance(raw, Mapping):
        return [(str(key), value) for key, value in raw.items()]
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        return [("", item) for item in raw]
    nested = attr(raw, "sensors")
    if nested is not None and nested is not raw:
        return _readiness_candidates(nested)
    candidates: list[tuple[str, object]] = []
    for key, aliases in {
        "tinysa": ("tinysa", "tiny_sa", "rf_trigger"),
        "rtlsdr": ("rtlsdr", "rtl_sdr", "rf_spectrum"),
        "krakensdr": ("krakensdr", "kraken_sdr", "direction_finder"),
        "acoustic": ("acoustic", "audio"),
        "adsb": ("adsb", "ads_b"),
        "passive_radar": ("passive_radar", "radar"),
        "fusion": ("fusion", "sensor_fusion"),
    }.items():
        value = _first_value(raw, aliases)
        if value is not None:
            candidates.append((key, value))
    return candidates


def _sensor_role(hint: str, sensor: object) -> str | None:
    identity = " ".join(
        (
            hint,
            _first_text(
                sensor,
                (
                    "key",
                    "sensor_id",
                    "source_id",
                    "display_name",
                    "name",
                    "role",
                    "sensor_kind",
                    "kind",
                ),
                "",
            ),
        )
    ).casefold().replace("-", "_")
    if "tinysa" in identity or "tiny_sa" in identity or "rf_trigger" in identity:
        return "tinysa"
    if "rtl" in identity or "rf_spectrum" in identity:
        return "rtlsdr"
    if "kraken" in identity or "direction_finder" in identity:
        return "krakensdr"
    if "acoustic" in identity or "audio" in identity or "акуст" in identity:
        return "acoustic"
    if "adsb" in identity or "ads_b" in identity:
        return "adsb"
    if "passive_radar" in identity or "пассив" in identity:
        return "passive_radar"
    if "fusion" in identity or "объедин" in identity:
        return "fusion"
    return None


def _readiness_state(sensor: object) -> str:
    raw = _key(
        _first_value(
            sensor,
            ("state", "availability", "status", "readiness", "level"),
        )
    )
    if raw in {
        "ready",
        "available",
        "online",
        "healthy",
        "streaming",
        "active",
        "ok",
    }:
        return "ready"
    if raw in {
        "limited",
        "degraded",
        "stale",
        "probing",
        "reconnecting",
        "partial",
    }:
        return "limited"
    if raw in {
        "unavailable",
        "absent",
        "offline",
        "failed",
        "disabled",
        "disconnected",
        "error",
    }:
        return "unavailable"
    available = _optional_bool(_first_value(sensor, ("available", "is_available")))
    if available is True:
        return "ready"
    if available is False:
        return "unavailable"
    if isinstance(sensor, str):
        return _readiness_state_value(sensor)
    if isinstance(sensor, bool):
        return "ready" if sensor else "unavailable"
    return "unavailable"


def _readiness_state_value(value: str) -> str:
    normalized = _key(value)
    if normalized in {"ready", "available", "online", "healthy", "ok"}:
        return "ready"
    if normalized in {"limited", "degraded", "stale", "partial"}:
        return "limited"
    return "unavailable"


def _sensor_items(situation: object | None) -> tuple[object, ...]:
    raw = _first_value(
        situation,
        ("sensor_availability", "sensors", "sensor_states"),
    )
    return _as_items(raw)


def _event_items(
    target: object | None,
    situation: object | None,
) -> tuple[object, ...]:
    collected: list[object] = []
    for raw in (
        _first_value(target, ("recent_events", "events", "related_events")),
        _first_value(situation, ("recent_events", "events", "normalized_events")),
    ):
        collected.extend(_as_items(raw))
    unique: list[object] = []
    seen: set[str] = set()
    for event in collected:
        identity = _first_text(
            event,
            ("event_id", "id"),
            f"{_key(_first_value(event, ('event_type', 'type', 'kind')))}:"
            f"{_format_timestamp(_first_value(event, ('observed_at', 'timestamp')))}:"
            f"{_first_text(event, ('headline_ru', 'title_ru', 'summary_ru'), '')}",
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(event)
    return tuple(unique)


def _sensor_name(sensor: object) -> str:
    explicit = _first_text(sensor, ("display_name", "name"), "")
    if explicit:
        return explicit
    kind = _key(_first_value(sensor, ("sensor_kind", "kind")))
    if kind in _SENSOR_LABELS:
        return _SENSOR_LABELS[kind]
    return _first_text(sensor, ("sensor_id", "source_id"), "Сенсор")


def _sensor_unavailable(sensor: object) -> bool:
    availability = _key(_first_value(sensor, ("availability", "state")))
    if availability:
        return availability in {
            "absent",
            "unavailable",
            "offline",
            "failed",
            "disconnected",
            "disabled",
            "stale",
        }
    available = _optional_bool(
        _first_value(sensor, ("available", "is_available"))
    )
    return available is False


def _sensor_fallback_text(situation: object | None) -> str:
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
            "operator_label",
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
            "operator_explanation",
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


def _number(value: object | None) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_items(raw: object | None) -> tuple[object, ...]:
    if isinstance(raw, Mapping):
        return tuple(raw.values())
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        return tuple(raw)
    return ()


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
