"""Temporal RF episodes with a readable, non-attributive decision chain."""

from __future__ import annotations

# ruff: noqa: RUF001
from PySide6.QtWidgets import QHeaderView, QLabel, QTableWidget, QTableWidgetItem

from ..runtime import attr, current_snapshot, items, value_of
from ..signal_presenter import (
    RfDecisionView,
    event_plain_meaning,
    present_rf_decision,
    translate_quality_flags,
)
from ..widgets import InlineNotice, Panel
from .common import OperatorPage, format_frequency

_LEGACY_CLASS_RU = {
    "narrowband_activity": "Узкополосное изменение",
    "broadband_activity": "Широкополосное изменение",
    "transient_burst": "Короткий всплеск",
    "impulsive_interference": "Короткий всплеск",
    "unknown": "Неоднозначное изменение",
}

_IDENTITY_LIMIT = (
    "RF-признаки описывают принятую форму сигнала, но не устанавливают "
    "тип физического источника, расстояние, направление или приближение."
)

_EMPTY_DETAIL = (
    "Выберите эпизод, чтобы увидеть: что говорит в пользу решения, что ему "
    "противоречит, какого подтверждения не хватает и каков вклад сенсоров."
)


class SignalEventsPage(OperatorPage):
    def __init__(self, runtime: object | None = None) -> None:
        super().__init__(
            runtime,
            "События сигнала",
            "Временные RF-эпизоды, доказательства и ограничения решения",
        )
        self.root_layout.addWidget(
            InlineNotice(
                "Честная интерпретация",
                "Оповещение появляется только после временного подтверждения. "
                "Совместимость RF-формы не является идентификацией физического источника.",
                level="info",
            )
        )
        panel = Panel("Последние RF-эпизоды", subtitle="До 64 локальных эпизодов")
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            (
                "Время",
                "Состояние",
                "RF-семейство",
                "Пик / полоса",
                "Качество данных",
                "Сила RF-признаков",
                "Почему",
            )
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_selected_detail)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        panel.content_layout.addWidget(self.table)
        self.detail = QLabel(_EMPTY_DETAIL)
        self.detail.setWordWrap(True)
        self.detail.setProperty("secondary", "true")
        panel.content_layout.addWidget(self.detail)
        self.root_layout.addWidget(panel, 1)
        self._events: tuple[object, ...] = ()
        self._expert = False

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        self._expert = (
            str(attr(snapshot, "experience_level", "guided")).lower() == "expert"
        )
        self._configure_mode(self._expert)
        self._events = _collect_events(snapshot)
        self.table.clearSelection()
        self.table.setRowCount(len(self._events))
        for row, event in enumerate(self._events):
            view = present_rf_decision(event)
            values = (
                _decision_row(event, view, expert=self._expert)
                if view is not None
                else _legacy_row(event, expert=self._expert)
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

        if self._events:
            self.header.status.set_status(
                f"ЭПИЗОДОВ: {len(self._events)}",
                "info",
            )
            self.table.selectRow(0)
        else:
            self.header.status.set_status("RF-ЭПИЗОДОВ НЕТ", "neutral")
            self.detail.setText(_EMPTY_DETAIL)

    def _configure_mode(self, expert: bool) -> None:
        if expert:
            self.header.title.setText("События сигнала")
            self.header.subtitle.setText(
                "Временные RF-эпизоды, доказательства и ограничения решения"
            )
            self.table.setHorizontalHeaderLabels(
                (
                    "Время",
                    "Lifecycle",
                    "Наблюдаемое RF-семейство",
                    "Пик / занятая полоса",
                    "Качество данных",
                    "Сила признаков / эвристика",
                    "Краткая причина",
                )
            )
            for column in range(7):
                self.table.setColumnHidden(column, False)
            return

        self.header.title.setText("Что менялось в эфире")
        self.header.subtitle.setText(
            "Понятная история проверенных и отклонённых RF-эпизодов"
        )
        self.table.setHorizontalHeaderLabels(
            (
                "Время",
                "Статус",
                "Что похоже по форме",
                "",
                "Качество данных",
                "Сила признаков",
                "Почему",
            )
        )
        self.table.setColumnHidden(3, True)
        for column in (0, 1, 2, 4, 5, 6):
            self.table.setColumnHidden(column, False)

    def _show_selected_detail(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._events):
            self.detail.setText(_EMPTY_DETAIL)
            return
        event = self._events[row]
        view = present_rf_decision(event)
        if view is None:
            self.detail.setText(_legacy_detail(event))
            return
        self.detail.setText(_decision_detail(view, expert=self._expert))


def _collect_events(snapshot: object | None) -> tuple[object, ...]:
    events = list(items(snapshot, "signal_events"))
    current = attr(snapshot, "signal_decision")
    current_view = present_rf_decision(current)
    if current_view is None or current_view.lifecycle == "idle":
        return tuple(events[:64])

    if current_view.episode_id:
        for index, event in enumerate(events):
            event_view = present_rf_decision(event)
            if (
                event_view is not None
                and event_view.episode_id == current_view.episode_id
            ):
                events[index] = current
                break
        else:
            events.insert(0, current)
    elif current not in events:
        events.insert(0, current)
    return tuple(events[:64])


def _decision_row(
    event: object,
    view: RfDecisionView,
    *,
    expert: bool,
) -> tuple[str, ...]:
    evidence = _short_label(view.evidence_strength_label)
    if expert:
        evidence = (
            f"{evidence}; эвристика {view.heuristic_score:.2f} "
            "(не вероятность)"
        )
    reason = view.supporting_evidence[0] if view.supporting_evidence else view.summary
    if not view.alertable:
        reason = f"Без оповещения. {reason}"
    return (
        _format_time(attr(event, "observed_at")),
        view.lifecycle_label,
        view.family_label,
        _peak_and_band(view),
        _short_label(view.data_quality_label),
        evidence,
        reason,
    )


def _legacy_row(event: object, *, expert: bool) -> tuple[str, ...]:
    classification = value_of(attr(event, "classification", "unknown")).lower()
    evidence = attr(event, "evidence")
    confidence = _as_float(attr(event, "confidence"))
    flags = attr(event, "quality_flags", ())
    quality = (
        translate_quality_flags(flags)
        if expert
        else "Старый формат: качество потока и сила признаков не разделены"
    )
    score = (
        f"Эвристика {confidence:.2f} (не вероятность)"
        if confidence is not None
        else "Не указана"
    )
    return (
        _format_time(attr(event, "observed_at")),
        "Старое наблюдение",
        _LEGACY_CLASS_RU.get(classification, _LEGACY_CLASS_RU["unknown"]),
        (
            f"{format_frequency(attr(evidence, 'peak_frequency_hz'))} / "
            f"{format_frequency(attr(evidence, 'occupied_bandwidth_hz'))}"
        ),
        quality,
        score,
        event_plain_meaning(classification),
    )


def _decision_detail(view: RfDecisionView, *, expert: bool) -> str:
    lines = [
        f"Состояние: {view.lifecycle_label}",
        f"RF-семейство: {view.family_label}. {view.summary}",
        f"Качество данных: {view.data_quality_label}",
        f"Сила RF-признаков: {view.evidence_strength_label}",
    ]
    if expert:
        if view.episode_id:
            lines.append(f"Эпизод: {view.episode_id}")
        lines.append(
            f"Эвристический балл: {view.heuristic_score:.2f}; "
            "калиброванная вероятность недоступна."
        )
    lines.extend(_section("За", view.supporting_evidence))
    lines.extend(_section("Против", view.contradicting_evidence))
    lines.extend(_section("Не хватает", view.missing_confirmation))
    lines.extend(_section("Альтернативные объяснения", view.alternatives))
    lines.extend(_section("Вклад сенсоров", view.sensor_contributions))
    lines.extend(_section("Ограничения", view.limitations))
    lines.append(f"Ограничение: {_IDENTITY_LIMIT}")
    return "\n".join(lines)


def _legacy_detail(event: object) -> str:
    classification = value_of(attr(event, "classification", "unknown")).lower()
    return (
        "Это наблюдение сохранено в старом формате без temporal lifecycle.\n"
        f"Смысл: {event_plain_meaning(classification)}\n"
        f"Ограничение: {_IDENTITY_LIMIT}"
    )


def _section(title: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        return (f"{title}: нет отмеченных данных.",)
    return (f"{title}:", *(f"• {value}" for value in values))


def _peak_and_band(view: RfDecisionView) -> str:
    peak = format_frequency(view.peak_frequency_hz)
    band = format_frequency(view.occupied_bandwidth_hz)
    return f"{peak} / {band}"


def _short_label(label: str) -> str:
    short = label.split(":", maxsplit=1)[0].strip()
    return short[:1].upper() + short[1:] if short else "Неизвестно"


def _format_time(observed: object) -> str:
    try:
        return str(
            observed.astimezone().strftime("%H:%M:%S")  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError, OSError):
        return "—"


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


__all__ = ["SignalEventsPage"]
