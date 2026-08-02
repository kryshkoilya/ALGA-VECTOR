"""Expert-only target lifecycle and evidence breakdown.

The page is deliberately a presentation boundary.  It consumes target
entities prepared by the backend and never derives physical identity,
position, range, or direction from raw measurements.  A direction is rendered
only when the target carries an explicit, fresh validation marker.
"""

from __future__ import annotations

# ruff: noqa: RUF001
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..runtime import attr, current_snapshot, value_of
from ..theme import Colors
from ..widgets import InlineNotice, Panel, StatusBadge
from .common import OperatorPage

_EMPTY = "—"
_PRODUCTION_TARGET_CLOCK_SKEW_SECONDS = 0.05

_LIFECYCLE_LABELS = {
    "candidate": "Кандидат",
    "active": "Активна",
    "tracking": "Сопровождается",
    "holding": "Удержание",
    "resolved": "Завершена",
    "expired": "Истекла",
    "stale": "Устарела",
    "tombstoned": "Архивирована",
    "lost": "Потеряна",
    "closed": "Закрыта",
}

_CONFIRMATION_LABELS = {
    "background": "Фон",
    "suspicious": "Подозрительная активность",
    "suspicious_activity": "Подозрительная активность",
    "probable_source": "Вероятный источник",
    "likely_source": "Вероятный источник",
    "probable_target": "Вероятная цель",
    "likely_target": "Вероятная цель",
    "confirmed": "Подтверждённая цель",
    "confirmed_target": "Подтверждённая цель",
}

_CONFIDENCE_LABELS = {
    "not_available": "Не рассчитана",
    "unknown": "Не рассчитана",
    "low": "Низкая",
    "medium": "Средняя",
    "moderate": "Средняя",
    "high": "Высокая",
    "confirmed": "Подтверждено",
}

_STALE_LIFECYCLES = frozenset(
    {
        "holding",
        "resolved",
        "expired",
        "stale",
        "tombstoned",
        "lost",
        "closed",
    }
)


@dataclass(frozen=True, slots=True)
class _DirectionView:
    available: bool
    value: str
    detail: str


@dataclass(frozen=True, slots=True)
class _TargetVerdict:
    historical: bool
    table_lifecycle: str
    table_confirmation: str
    header_text: str
    header_level: str
    lifecycle_badge: str
    lifecycle_level: str
    confirmation_badge: str
    confirmation_level: str


class ExpertTargetsPage(OperatorPage):
    """Dense target-centric view for trained operators and diagnostics."""

    def __init__(
        self,
        runtime: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            runtime,
            "Цели",
            "Состояние, подтверждение, вклад сенсоров и проверяемые ограничения",
            action_text="Обновить",
            parent=parent,
        )
        self.setObjectName("expertTargetsPage")
        self.header.action.clicked.connect(lambda: self.refresh())
        self._targets: tuple[object, ...] = ()
        self._verdicts: tuple[_TargetVerdict, ...] = ()
        self._snapshot: object | None = None
        self._expert = False

        target_list_panel = Panel(
            "ЦЕЛИ",
            subtitle="НОВЫЕ И АКТИВНЫЕ СВЕРХУ",
            compact=True,
        )
        target_list_panel.setObjectName("targetListPanel")
        self.target_table = QTableWidget(0, 5)
        self.target_table.setObjectName("targetListTable")
        self.target_table.setHorizontalHeaderLabels(
            (
                "ID",
                "Состояние",
                "Подтверждение",
                "Рабочая гипотеза",
                "Последнее наблюдение",
            )
        )
        target_header = self.target_table.horizontalHeader()
        target_header.setStretchLastSection(False)
        for column in (0, 1, 2, 4):
            target_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        target_header.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.Stretch,
        )
        self.target_table.verticalHeader().setVisible(False)
        self.target_table.setAlternatingRowColors(True)
        self.target_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.target_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.target_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.target_table.setMaximumHeight(190)
        self.target_table.itemSelectionChanged.connect(self._show_selected)
        target_list_panel.content_layout.addWidget(self.target_table)
        self.root_layout.addWidget(target_list_panel)

        self.empty_state = InlineNotice(
            "Целей нет",
            (
                "Backend пока не сформировал ни одной target entity. "
                "События остаются доступны в экспертном журнале."
            ),
            level="info",
        )
        self.empty_state.setObjectName("targetEmptyState")
        self.root_layout.addWidget(self.empty_state)

        self.stale_notice = InlineNotice(
            "Цель неактуальна",
            (
                "Lifecycle завершён или срок действия данных истёк. "
                "Значения сохранены только для разбора и не считаются текущей обстановкой."
            ),
            level="warning",
        )
        self.stale_notice.setObjectName("targetStaleNotice")
        self.stale_notice.hide()
        self.root_layout.addWidget(self.stale_notice)

        details = QSplitter(Qt.Orientation.Horizontal)
        details.setObjectName("targetDetailsSplitter")
        self._details = details

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        overview = Panel("ТЕКУЩАЯ ЦЕЛЬ", compact=True)
        overview.setObjectName("targetOverviewPanel")
        self.overview_panel = overview
        overview_grid = QGridLayout()
        overview_grid.setHorizontalSpacing(14)
        overview_grid.setVerticalSpacing(7)

        self.target_id = QLabel(_EMPTY)
        self.target_id.setObjectName("targetIdValue")
        self.target_id.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lifecycle = StatusBadge("НЕТ ДАННЫХ", "neutral")
        self.lifecycle.setObjectName("targetLifecycleBadge")
        self.confirmation = StatusBadge("НЕ ПОДТВЕРЖДЕНО", "neutral")
        self.confirmation.setObjectName("targetConfirmationBadge")
        self.hypothesis = QLabel("Не установлена")
        self.hypothesis.setObjectName("targetHypothesisValue")
        self.hypothesis.setWordWrap(True)
        self.summary = QLabel("Операторское резюме отсутствует.")
        self.summary.setObjectName("targetSummaryValue")
        self.summary.setWordWrap(True)
        self.summary.setProperty("secondary", "true")
        self.confidence = QLabel("Не рассчитана")
        self.confidence.setObjectName("targetConfidenceValue")
        self.confidence.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.confidence_basis = QLabel(
            "Числовая оценка является силой признаков, а не вероятностью типа объекта."
        )
        self.confidence_basis.setObjectName("targetConfidenceBasis")
        self.confidence_basis.setWordWrap(True)
        self.confidence_basis.setProperty("secondary", "true")

        overview_grid.addWidget(_caption("ID цели"), 0, 0)
        overview_grid.addWidget(self.target_id, 0, 1)
        overview_grid.addWidget(_caption("Состояние"), 1, 0)
        overview_grid.addWidget(self.lifecycle, 1, 1)
        overview_grid.addWidget(_caption("Стадия подтверждения"), 2, 0)
        overview_grid.addWidget(self.confirmation, 2, 1)
        overview_grid.addWidget(
            _caption("Рабочая гипотеза backend — не физическая идентификация"),
            3,
            0,
        )
        overview_grid.addWidget(self.hypothesis, 3, 1)
        overview_grid.addWidget(_caption("Сила признаков"), 4, 0)
        overview_grid.addWidget(self.confidence, 4, 1)
        overview_grid.setColumnStretch(1, 1)
        overview.content_layout.addLayout(overview_grid)
        overview.content_layout.addWidget(self.confidence_basis)
        overview.content_layout.addWidget(self.summary)
        left_layout.addWidget(overview)

        timing = Panel("ВРЕМЕННАЯ ШКАЛА", compact=True)
        timing.setObjectName("targetTimingPanel")
        timing_grid = QGridLayout()
        timing_grid.setHorizontalSpacing(12)
        timing_grid.setVerticalSpacing(5)
        self.first_seen = QLabel(_EMPTY)
        self.first_seen.setObjectName("targetFirstSeenValue")
        self.last_seen = QLabel(_EMPTY)
        self.last_seen.setObjectName("targetLastSeenValue")
        self.valid_until = QLabel(_EMPTY)
        self.valid_until.setObjectName("targetValidUntilValue")
        timing_grid.addWidget(_caption("Первое наблюдение"), 0, 0)
        timing_grid.addWidget(self.first_seen, 0, 1)
        timing_grid.addWidget(_caption("Последнее наблюдение"), 1, 0)
        timing_grid.addWidget(self.last_seen, 1, 1)
        timing_grid.addWidget(_caption("Действительно до"), 2, 0)
        timing_grid.addWidget(self.valid_until, 2, 1)
        timing.content_layout.addLayout(timing_grid)
        left_layout.addWidget(timing)

        direction_panel = Panel(
            "ПРОВЕРЕННОЕ НАПРАВЛЕНИЕ",
            subtitle="БЕЗ РАСЧЁТА ДАЛЬНОСТИ И КООРДИНАТ",
            compact=True,
        )
        direction_panel.setObjectName("targetDirectionPanel")
        self.direction_value = QLabel("Направление недоступно")
        self.direction_value.setObjectName("targetDirectionValue")
        self.direction_value.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.direction_value.setWordWrap(True)
        self.direction_detail = QLabel(
            "Нет свежего направления с явной внешней валидацией."
        )
        self.direction_detail.setObjectName("targetDirectionDetail")
        self.direction_detail.setWordWrap(True)
        self.direction_detail.setProperty("secondary", "true")
        direction_panel.content_layout.addWidget(self.direction_value)
        direction_panel.content_layout.addWidget(self.direction_detail)
        left_layout.addWidget(direction_panel)

        recommendation_panel = Panel("РЕКОМЕНДАЦИЯ", compact=True)
        recommendation_panel.setObjectName("targetRecommendationPanel")
        self.recommendation_short = QLabel("Действие не сформировано.")
        self.recommendation_short.setObjectName("targetRecommendationShort")
        self.recommendation_short.setStyleSheet(
            f"color: {Colors.TEAL}; font-size: 16px; font-weight: 600;"
        )
        self.recommendation_short.setWordWrap(True)
        self.recommendation_detailed = QLabel(
            "Проверьте ограничения и доступность подтверждающих сенсоров."
        )
        self.recommendation_detailed.setObjectName(
            "targetRecommendationDetailed"
        )
        self.recommendation_detailed.setWordWrap(True)
        self.recommendation_detailed.setProperty("secondary", "true")
        recommendation_panel.content_layout.addWidget(
            self.recommendation_short
        )
        recommendation_panel.content_layout.addWidget(
            self.recommendation_detailed
        )
        left_layout.addWidget(recommendation_panel)
        left_layout.addStretch(1)
        self.left_scroll = QScrollArea()
        self.left_scroll.setObjectName("targetDetailsLeftScroll")
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.left_scroll.setWidget(left)
        details.addWidget(self.left_scroll)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        sources_panel = Panel(
            "ВКЛАД ИСТОЧНИКОВ",
            subtitle="АТРИБУЦИЯ BACKEND",
            compact=True,
        )
        sources_panel.setObjectName("targetSourcesPanel")
        self.source_table = QTableWidget(0, 5)
        self.source_table.setObjectName("targetSourceTable")
        self.source_table.setHorizontalHeaderLabels(
            ("Сенсор", "Тип", "Вклад", "Роль", "Объяснение")
        )
        source_header = self.source_table.horizontalHeader()
        source_header.setStretchLastSection(False)
        for column in range(4):
            source_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        source_header.setSectionResizeMode(
            4,
            QHeaderView.ResizeMode.Stretch,
        )
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.setAlternatingRowColors(True)
        self.source_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.source_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.source_table.setMinimumHeight(145)
        sources_panel.content_layout.addWidget(self.source_table)
        right_layout.addWidget(sources_panel)

        evidence_panel = Panel("ДОКАЗАТЕЛЬСТВА", compact=True)
        evidence_panel.setObjectName("targetEvidencePanel")
        self.evidence_table = QTableWidget(0, 4)
        self.evidence_table.setObjectName("targetEvidenceTable")
        self.evidence_table.setHorizontalHeaderLabels(
            ("Код", "Источник", "Измерение", "Объяснение")
        )
        evidence_header = self.evidence_table.horizontalHeader()
        evidence_header.setStretchLastSection(False)
        for column in range(3):
            evidence_header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        evidence_header.setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.Stretch,
        )
        self.evidence_table.verticalHeader().setVisible(False)
        self.evidence_table.setAlternatingRowColors(True)
        self.evidence_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.evidence_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.evidence_table.setMinimumHeight(145)
        evidence_panel.content_layout.addWidget(self.evidence_table)
        right_layout.addWidget(evidence_panel)

        limitations_panel = Panel("ОГРАНИЧЕНИЯ", compact=True)
        limitations_panel.setObjectName("targetLimitationsPanel")
        self.limitations = QListWidget()
        self.limitations.setObjectName("targetLimitationsList")
        self.limitations.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.limitations.setMinimumHeight(105)
        limitations_panel.content_layout.addWidget(self.limitations)
        right_layout.addWidget(limitations_panel)
        right_layout.addStretch(1)
        self.right_scroll = QScrollArea()
        self.right_scroll.setObjectName("targetDetailsRightScroll")
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.right_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.right_scroll.setWidget(right)
        details.addWidget(self.right_scroll)
        details.setChildrenCollapsible(False)
        details.setSizes((580, 680))
        self.root_layout.addWidget(details, 1)
        self._set_details_visible(False)

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        self._snapshot = snapshot
        self._expert = (
            _key(attr(snapshot, "experience_level", "guided")) == "expert"
        )
        selected_id = self._selected_target_id()
        self._targets = _collect_targets(snapshot)
        reference = _snapshot_time(snapshot)
        self._verdicts = tuple(
            _target_verdict(target, reference) for target in self._targets
        )
        self.target_table.clearSelection()
        self.target_table.setRowCount(len(self._targets))
        for row, (target, verdict) in enumerate(
            zip(self._targets, self._verdicts, strict=True)
        ):
            values = (
                _target_id(target),
                verdict.table_lifecycle,
                verdict.table_confirmation,
                _hypothesis_text(target),
                _format_time(_first_value(target, ("last_seen", "updated_at"))),
            )
            for column, value in enumerate(values):
                self.target_table.setItem(row, column, QTableWidgetItem(value))

        if not self._targets:
            self._verdicts = ()
            self.header.status.set_status("ЦЕЛЕЙ НЕТ", "neutral")
            self.empty_state.show()
            self.stale_notice.hide()
            self._set_details_visible(False)
            return

        self.empty_state.hide()
        self._set_details_visible(True)
        selected_row = next(
            (
                index
                for index, target in enumerate(self._targets)
                if _target_id(target) == selected_id
            ),
            0,
        )
        self.target_table.selectRow(selected_row)
        self._show_selected()

    def _selected_target_id(self) -> str:
        row = self.target_table.currentRow()
        if 0 <= row < len(self._targets):
            return _target_id(self._targets[row])
        return ""

    def _show_selected(self) -> None:
        row = self.target_table.currentRow()
        if row < 0 or row >= len(self._targets):
            return
        target = self._targets[row]
        verdict = self._verdicts[row]
        stale = verdict.historical
        self.header.status.set_status(
            verdict.header_text,
            verdict.header_level,
        )
        self.stale_notice.setVisible(stale)
        self.target_id.setText(_target_id(target))
        self.lifecycle.set_status(
            verdict.lifecycle_badge,
            verdict.lifecycle_level,
        )
        self.confirmation.set_status(
            verdict.confirmation_badge,
            verdict.confirmation_level,
        )
        self.hypothesis.setText(_hypothesis_text(target))
        self.summary.setText(
            _first_text(
                target,
                (
                    "short_operator_summary",
                    "operator_summary_ru",
                    "operator_explanation",
                    "summary_ru",
                    "explanation_ru",
                ),
                "Операторское резюме отсутствует.",
            )
        )
        confidence, basis = _confidence_text(target, expert=self._expert)
        self.confidence.setText(confidence)
        self.confidence_basis.setText(basis)
        self.first_seen.setText(
            _format_time(
                _first_value(
                    target,
                    ("first_seen", "created_at", "observed_at"),
                )
            )
        )
        self.last_seen.setText(
            _format_time(
                _first_value(
                    target,
                    ("last_seen", "updated_at", "observed_at"),
                )
            )
        )
        self.valid_until.setText(
            _format_time(_first_value(target, ("valid_until", "expires_at")))
        )

        direction = _direction_view(
            target,
            _snapshot_time(self._snapshot),
            historical=verdict.historical,
        )
        self.direction_value.setText(direction.value)
        self.direction_detail.setText(direction.detail)

        if stale:
            self.recommendation_short.setText(
                "Не используйте историческую запись как текущую цель."
            )
            self.recommendation_detailed.setText(
                "Дождитесь новой ACTIVE-цели со свежими валидными данными. "
                "Старые оперативные рекомендации намеренно скрыты."
            )
        else:
            recommendation = _first_value(target, ("recommendation",))
            self.recommendation_short.setText(
                _first_text(
                    recommendation,
                    ("short_ru", "short", "action_ru", "title_ru", "text_ru"),
                    _first_text(
                        target,
                        (
                            "recommended_action_short",
                            "recommendation_short_ru",
                            "recommendation_ru",
                        ),
                        "Действие не сформировано.",
                    ),
                )
            )
            self.recommendation_detailed.setText(
                _first_text(
                    recommendation,
                    (
                        "detailed_ru",
                        "detailed",
                        "detail_ru",
                        "explanation_ru",
                    ),
                    _first_text(
                        target,
                        (
                            "recommended_action_detailed",
                            "recommendation_detailed_ru",
                        ),
                        (
                            "Проверьте ограничения и доступность "
                            "подтверждающих сенсоров."
                        ),
                    ),
                )
            )
        self._render_sources(target)
        self._render_evidence(target)
        self._render_limitations(target)

    def _render_sources(self, target: object) -> None:
        sources = _source_items(target)
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            if isinstance(source, str):
                values = (source, _EMPTY, _EMPTY, "Указан backend", _EMPTY)
            else:
                contribution = _finite_number(
                    _first_value(source, ("contribution", "weight", "score"))
                )
                contribution_text = (
                    f"{contribution:.3f}"
                    if self._expert and contribution is not None
                    else _EMPTY
                )
                independent = _truth(
                    _first_value(
                        source,
                        ("independent_confirmation", "confirming"),
                    )
                )
                role = (
                    "Независимое подтверждение"
                    if independent is True
                    else "Контекст / вклад"
                )
                values = (
                    _first_text(
                        source,
                        ("sensor_id", "source_id", "id", "name"),
                        _EMPTY,
                    ),
                    _display_value(
                        _first_value(source, ("sensor_kind", "kind", "type"))
                    ),
                    contribution_text,
                    role,
                    _first_text(
                        source,
                        ("explanation_ru", "reason_ru", "summary_ru"),
                        _EMPTY,
                    ),
                )
            for column, value in enumerate(values):
                self.source_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
        self.source_table.resizeRowsToContents()

    def _render_evidence(self, target: object) -> None:
        evidence = _evidence_items(target)
        self.evidence_table.setRowCount(len(evidence))
        for row, fact in enumerate(evidence):
            if isinstance(fact, str):
                values = (_EMPTY, _EMPTY, _EMPTY, fact)
            else:
                measured = _first_value(
                    fact,
                    ("measured", "value", "measurement"),
                )
                unit = _first_text(fact, ("unit",), "")
                measurement = (
                    f"{_display_value(measured)} {unit}".strip()
                    if measured is not None
                    else _EMPTY
                )
                values = (
                    _first_text(fact, ("code", "id", "name"), _EMPTY),
                    _first_text(
                        fact,
                        ("source_id", "sensor_id"),
                        _EMPTY,
                    ),
                    measurement,
                    _first_text(
                        fact,
                        ("explanation_ru", "summary_ru", "detail_ru"),
                        _EMPTY,
                    ),
                )
            for column, value in enumerate(values):
                self.evidence_table.setItem(
                    row,
                    column,
                    QTableWidgetItem(value),
                )
        self.evidence_table.resizeRowsToContents()

    def _render_limitations(self, target: object) -> None:
        self.limitations.clear()
        limitations = _text_items(
            _first_value(target, ("limitations", "warnings", "constraints"))
        )
        if not limitations:
            item = QListWidgetItem("Ограничения backend не переданы.")
            item.setForeground(QColor(Colors.MUTED))
            self.limitations.addItem(item)
            return
        for limitation in limitations:
            self.limitations.addItem(QListWidgetItem(limitation))

    def _set_details_visible(self, visible: bool) -> None:
        self._details.setVisible(visible)


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", "true")
    label.setWordWrap(True)
    return label


def _collect_targets(snapshot: object | None) -> tuple[object, ...]:
    current = attr(snapshot, "current_target")
    raw = attr(snapshot, "targets", ())
    targets = list(_as_items(raw))
    if current is not None:
        targets.insert(0, current)
    unique: list[object] = []
    seen: set[str] = set()
    for target in targets:
        identifier = _target_id(target)
        key = identifier if identifier != _EMPTY else f"object:{id(target)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return tuple(unique)


def _as_items(value: object | None) -> tuple[object, ...]:
    if isinstance(value, dict):
        return tuple(value.values())
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return tuple(cast(Iterable[object], value))
    return ()


def _text_items(value: object | None) -> tuple[str, ...]:
    result: list[str] = []
    for item in _as_items(value):
        text = value_of(item).strip()
        if text:
            result.append(text)
    if isinstance(value, str) and value.strip():
        result.append(value.strip())
    return tuple(result)


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
    return (
        value_of(value)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _display_value(value: object | None) -> str:
    if value is None:
        return _EMPTY
    return value_of(value).strip() or _EMPTY


def _target_id(target: object) -> str:
    return _first_text(target, ("target_id", "id", "track_id"), _EMPTY)


def _lifecycle_key(target: object) -> str:
    return _key(_first_value(target, ("lifecycle", "state", "status")))


def _lifecycle_label(target: object) -> str:
    key = _lifecycle_key(target)
    if not key:
        return "Неизвестно"
    return _LIFECYCLE_LABELS.get(key, _display_value(key).replace("_", " "))


def _confirmation_key(target: object) -> str:
    return _key(
        _first_value(
            target,
            ("confirmation_stage", "confirmation", "stage"),
        )
    )


def _confirmation_label(target: object) -> str:
    key = _confirmation_key(target)
    if not key:
        return "Не подтверждено"
    return _CONFIRMATION_LABELS.get(
        key,
        _display_value(key).replace("_", " "),
    )


def _hypothesis_text(target: object) -> str:
    return _first_text(
        target,
        (
            "operator_label",
            "probable_type_label_ru",
            "probable_type",
            "classification_label_ru",
        ),
        "Не установлена",
    )


def _finite_number(value: object | None) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _confidence_text(target: object, *, expert: bool) -> tuple[str, str]:
    confidence = _first_value(
        target,
        (
            "confidence",
            "target_confidence",
            "confidence_score",
            "evidence_strength",
        ),
    )
    raw = _finite_number(confidence)
    if raw is None:
        raw = _finite_number(
            _first_value(
                confidence,
                ("value", "score", "heuristic_score"),
            )
        )
    band = _key(
        _first_value(
            confidence,
            ("band", "level", "evidence_strength"),
        )
    )
    if not band:
        band = _key(
            _first_value(
                target,
                ("confidence_band", "evidence_strength"),
            )
        )
    label = _CONFIDENCE_LABELS.get(band, "Не рассчитана")
    if label == "Не рассчитана" and raw is not None:
        label = "Низкая" if raw < 0.4 else "Средняя" if raw < 0.75 else "Высокая"
    value = f"{label} · {raw:.3f}" if expert and raw is not None else label
    basis = _first_text(
        confidence,
        ("basis_ru", "explanation_ru", "detail_ru"),
        _first_text(
            target,
            ("confidence_explanation_ru", "evidence_summary_ru"),
            (
                "Числовая оценка является силой признаков, "
                "а не вероятностью типа объекта."
            ),
        ),
    )
    if expert and raw is not None and "не вероят" not in basis.casefold():
        basis = f"{basis.rstrip('.')} · Эвристическая сила, не вероятность."
    return value, basis


def _truth(value: object | None) -> bool | None:
    if isinstance(value, bool):
        return value
    key = _key(value)
    if key in {"true", "yes", "1", "validated", "available", "fresh"}:
        return True
    if key in {
        "false",
        "no",
        "0",
        "unvalidated",
        "unavailable",
        "stale",
        "expired",
    }:
        return False
    return None


def _parse_aware_time(value: object | None) -> datetime | None:
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


def _snapshot_time(snapshot: object | None) -> datetime | None:
    return _parse_aware_time(
        _first_value(snapshot, ("captured_at", "generated_at", "observed_at"))
    )


def _is_production_fused_target(target: object) -> bool:
    target_type = type(target)
    return (
        target_type.__module__ == "alga_vector.targets.models"
        and target_type.__name__ == "FusedTarget"
    )


def _target_is_valid_at(target: object, reference: datetime) -> bool:
    future_tolerance = (
        _PRODUCTION_TARGET_CLOCK_SKEW_SECONDS
        if _is_production_fused_target(target)
        else 0.0
    )
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
        if timestamp is None or (
            timestamp - reference
        ).total_seconds() > future_tolerance:
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


def _is_stale(target: object, reference: datetime | None) -> bool:
    explicit = _truth(
        _first_value(target, ("is_stale", "stale", "expired"))
    )
    if explicit is True:
        return True
    if _lifecycle_key(target) != "active" or reference is None:
        return True
    active = _truth(_first_value(target, ("active", "is_active")))
    if active is False:
        return True
    return not _target_is_valid_at(target, reference)


def _target_verdict(
    target: object,
    reference: datetime | None,
) -> _TargetVerdict:
    if _is_stale(target, reference):
        lifecycle = _lifecycle_label(target)
        lifecycle_context = (
            "неактуальна"
            if _lifecycle_key(target) == "active"
            else lifecycle
        )
        return _TargetVerdict(
            historical=True,
            table_lifecycle=f"Историческая · {lifecycle_context}",
            table_confirmation="Историческая запись",
            header_text="ЦЕЛЬ НЕАКТУАЛЬНА",
            header_level="warning",
            lifecycle_badge=f"ИСТОРИЧЕСКАЯ · {lifecycle_context.upper()}",
            lifecycle_level="warning",
            confirmation_badge="ИСТОРИЧЕСКАЯ ЗАПИСЬ",
            confirmation_level="warning",
        )
    lifecycle = _lifecycle_label(target)
    confirmation = _confirmation_label(target)
    return _TargetVerdict(
        historical=False,
        table_lifecycle=lifecycle,
        table_confirmation=confirmation,
        header_text="ЦЕЛЬ АКТИВНА",
        header_level=_target_level(target),
        lifecycle_badge=lifecycle.upper(),
        lifecycle_level=_lifecycle_level(target),
        confirmation_badge=confirmation.upper(),
        confirmation_level=_confirmation_level(target),
    )


def _direction_is_associated(target: object, direction: object) -> bool:
    if (
        _is_production_fused_target(target)
        and attr(target, "direction") is direction
    ):
        return True
    target_id = _target_id(target)
    associated_target_id = _first_text(
        direction,
        ("associated_target_id", "target_id", "track_id"),
        _first_text(target, ("direction_target_id",), ""),
    )
    return (
        target_id != _EMPTY
        and bool(associated_target_id)
        and associated_target_id == target_id
    )


def _direction_view(
    target: object,
    reference: datetime | None,
    *,
    historical: bool,
) -> _DirectionView:
    direction = attr(target, "direction")
    if direction is None:
        return _DirectionView(
            False,
            "Направление недоступно",
            "Backend не передал валидированное направление для этой цели.",
        )
    if historical:
        return _DirectionView(
            False,
            "Направление устарело",
            "Цель не является ACTIVE; исторический пеленг скрыт.",
        )
    if reference is None:
        return _DirectionView(
            False,
            "Направление недоступно",
            "Время снимка отсутствует или не содержит часовой пояс.",
        )
    if not _direction_is_associated(target, direction):
        return _DirectionView(
            False,
            "Направление недоступно",
            "Пеленг скрыт: нет явной привязки к выбранной цели.",
        )
    validated = _truth(
        _first_value(
            direction,
            ("validated_external", "validated", "is_validated"),
        )
    )
    if validated is not True:
        return _DirectionView(
            False,
            "Направление недоступно",
            (
                "Пеленг скрыт: нет явного признака внешней валидации. "
                "Интерфейс не использует неподтверждённый азимут."
            ),
        )
    if _truth(attr(direction, "available")) is not True:
        return _DirectionView(
            False,
            "Направление недоступно",
            "Источник не подтвердил доступность направления.",
        )

    observed_at = _parse_aware_time(attr(direction, "observed_at"))
    valid_until = _parse_aware_time(attr(direction, "valid_until"))
    if observed_at is None or valid_until is None:
        return _DirectionView(
            False,
            "Направление недоступно",
            "Пеленг скрыт: отсутствуют валидные timezone-aware метки времени.",
        )
    if valid_until <= observed_at or observed_at > reference:
        return _DirectionView(
            False,
            "Направление недоступно",
            "Пеленг скрыт: временной интервал некорректен или находится в будущем.",
        )
    if reference > valid_until:
        return _DirectionView(
            False,
            "Направление устарело",
            "Срок действия валидированного пеленга истёк.",
        )

    explicit = _first_text(
        direction,
        ("sector_text_ru", "display_ru", "text_ru"),
        "",
    )
    bearing = _finite_number(
        _first_value(direction, ("bearing_deg", "azimuth_deg"))
    )
    uncertainty = _finite_number(
        _first_value(direction, ("uncertainty_deg", "half_width_deg"))
    )
    if (
        bearing is None
        or uncertainty is None
        or not 0.0 <= bearing < 360.0
        or not 0.0 <= uncertainty <= 180.0
    ):
        return _DirectionView(
            False,
            "Направление недоступно",
            "Валидированный источник не передал корректный азимут и сектор.",
        )
    value = explicit or f"Азимут {bearing:.1f}° · сектор ±{uncertainty:.1f}°"

    source = _first_text(direction, ("source_id", "sensor_id"), "внешний DF")
    calibration = _first_text(direction, ("calibration_id",), "не указана")
    observed = _format_time(attr(direction, "observed_at"))
    detail = (
        f"Источник: {source}. Калибровка: {calibration}. "
        f"Наблюдение: {observed}. Дальность и координаты не вычисляются."
    )
    return _DirectionView(True, value, detail)


def _source_items(target: object) -> tuple[object, ...]:
    return _as_items(
        _first_value(
            target,
            ("sources", "source_attribution", "sensors_used"),
        )
    )


def _evidence_items(target: object) -> tuple[object, ...]:
    return _as_items(
        _first_value(
            target,
            (
                "evidence",
                "evidence_facts",
                "observations",
            ),
        )
    )


def _format_time(value: object | None) -> str:
    parsed = _parse_aware_time(value)
    if parsed is not None:
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    text = value_of(value).strip() if value is not None else ""
    return text or _EMPTY


def _lifecycle_level(target: object) -> str:
    lifecycle = _lifecycle_key(target)
    if lifecycle in _STALE_LIFECYCLES:
        return "warning"
    if lifecycle in {"active", "tracking"}:
        return "info"
    return "neutral"


def _confirmation_level(target: object) -> str:
    stage = _confirmation_key(target)
    if stage in {"confirmed", "confirmed_target"}:
        return "ready"
    if stage in {"probable_target", "likely_target"}:
        return "warning"
    if stage in {
        "suspicious",
        "suspicious_activity",
        "probable_source",
        "likely_source",
    }:
        return "info"
    return "neutral"


def _target_level(target: object) -> str:
    confirmation = _confirmation_level(target)
    return confirmation if confirmation != "neutral" else _lifecycle_level(target)


__all__ = ["ExpertTargetsPage"]
