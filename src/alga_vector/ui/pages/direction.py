"""Direction-only operator page without maps or inferred localization."""

from __future__ import annotations

# ruff: noqa: RUF001
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from alga_vector.direction import (
    QUALITY_LABELS_RU,
    SOURCE_LABELS_RU,
    DirectionSnapshot,
    DirectionSource,
)

from ..direction_presenter import (
    RANGE_LIMITATION_RU,
    ReceivedLevelTrendPresenter,
    present_bearing,
)
from ..runtime import attr, call_runtime, current_snapshot
from ..theme import Colors
from ..widgets.direction_plot import DirectionPlot
from ..widgets.panel import MetricTile, Panel
from ..widgets.status import InlineNotice
from .common import OperatorPage


class DirectionPage(OperatorPage):
    """Display validated bearing or an explicit unavailable state."""

    def __init__(
        self,
        runtime: object | None,
        parent: QWidget | None = None,
    ) -> None:
        self._rf_trend_presenter = ReceivedLevelTrendPresenter()
        super().__init__(
            runtime,
            "Направление",
            "Угловые наблюдения без карты и без расчёта положения",
            parent=parent,
        )
        content = QHBoxLayout()
        content.setSpacing(12)

        plot_panel = Panel(
            "360° панель",
            subtitle="Север сверху · полный круг 000–359,9°",
        )
        self.plot = DirectionPlot()
        plot_panel.content_layout.addWidget(self.plot)
        content.addWidget(plot_panel, 2)

        side = Panel("Источник и достоверность")
        side.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Minimum,
        )
        metrics = QHBoxLayout()
        self.bearing_metric = MetricTile("Азимут", "—")
        self.uncertainty_metric = MetricTile("Сектор ±", "—")
        metrics.addWidget(self.bearing_metric)
        metrics.addWidget(self.uncertainty_metric)
        side.content_layout.addLayout(metrics)

        self.source_metric = MetricTile("Источник", "НЕТ ИСТОЧНИКА")
        self.source_metric.value_label.setWordWrap(True)
        self.quality_metric = MetricTile("Качество", "НЕДОСТУПНО")
        side.content_layout.addWidget(self.source_metric)
        side.content_layout.addWidget(self.quality_metric)

        self.rf_trend_metric = MetricTile(
            "Тренд принятого RF-уровня",
            "НЕТ ДАННЫХ",
        )
        self.rf_trend_metric.value_label.setWordWrap(True)
        side.content_layout.addWidget(self.rf_trend_metric)
        self.rf_trend_detail = QLabel(
            f"Свежего измеренного RF-уровня пока нет. {RANGE_LIMITATION_RU}"
        )
        self.rf_trend_detail.setWordWrap(True)
        self.rf_trend_detail.setProperty("secondary", "true")
        self.rf_trend_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        side.content_layout.addWidget(self.rf_trend_detail)
        self.range_limitation_notice = InlineNotice(
            "RF-уровень не является дальностью",
            RANGE_LIMITATION_RU,
            level="info",
        )
        side.content_layout.addWidget(self.range_limitation_notice)

        self.source_notice = InlineNotice(
            "Направление недоступно",
            "Подключите валидированный внешний датчик или внесите ручную отметку.",
            level="warning",
        )
        side.content_layout.addWidget(self.source_notice)

        manual_panel = Panel("Ручная отметка", compact=True)
        manual_explanation = QLabel(
            "Ручной азимут — запись оператора. Он не считается измерением "
            "приёмника, а уверенность для него не вычисляется."
        )
        manual_explanation.setWordWrap(True)
        manual_explanation.setProperty("muted", "true")
        manual_panel.content_layout.addWidget(manual_explanation)
        form = QFormLayout()
        self.bearing_input = QDoubleSpinBox()
        self.bearing_input.setRange(0.0, 359.9)
        self.bearing_input.setDecimals(1)
        self.bearing_input.setSingleStep(1.0)
        self.bearing_input.setSuffix("°")
        self.bearing_input.setToolTip("Угол, явно введённый оператором; система его не измеряла.")
        self.uncertainty_input = QDoubleSpinBox()
        self.uncertainty_input.setRange(0.0, 180.0)
        self.uncertainty_input.setDecimals(1)
        self.uncertainty_input.setSingleStep(1.0)
        self.uncertainty_input.setValue(15.0)
        self.uncertainty_input.setSuffix("°")
        self.uncertainty_input.setToolTip("Полуширина сектора, указанная оператором.")
        form.addRow("Азимут", self.bearing_input)
        form.addRow("Полуширина сектора", self.uncertainty_input)
        manual_panel.content_layout.addLayout(form)
        actions = QHBoxLayout()
        self.apply_manual_button = QPushButton("Показать ручной луч")
        self.apply_manual_button.setProperty("primary", "true")
        self.apply_manual_button.clicked.connect(self._apply_manual)
        self.clear_button = QPushButton("Очистить")
        self.clear_button.clicked.connect(self._clear_direction)
        actions.addWidget(self.apply_manual_button, 1)
        actions.addWidget(self.clear_button)
        manual_panel.content_layout.addLayout(actions)
        side.content_layout.addWidget(manual_panel)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setProperty("secondary", "true")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        side.content_layout.addWidget(self.detail)
        side.content_layout.addWidget(
            InlineNotice(
                "Границы метода",
                "Обычный SDR-приёмник сам по себе не создаёт азимут. "
                "Активный луч появляется только из ручного ввода, "
                "валидированного внешнего датчика или явно помеченной "
                "демо-симуляции.",
                level="info",
            )
        )

        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.side_scroll.setWidget(side)
        content.addWidget(self.side_scroll, 1)
        self.root_layout.addLayout(content, 1)

    def refresh(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            snapshot = current_snapshot(self.runtime)
        super().refresh(snapshot)
        self._refresh_rf_trend(snapshot)
        direction = attr(snapshot, "direction")
        if not isinstance(direction, DirectionSnapshot):
            direction = attr(snapshot, "direction_status")
        if not isinstance(direction, DirectionSnapshot):
            self._show_unavailable("Runtime не предоставил валидное состояние направления.")
            return
        self._apply_snapshot(direction)

    def set_direction_snapshot(self, snapshot: DirectionSnapshot) -> None:
        """Apply state directly for adapters that push direction asynchronously."""

        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: DirectionSnapshot) -> None:
        self.plot.set_snapshot(snapshot)
        current = snapshot.current
        bearing_view = present_bearing(snapshot)
        source_label = SOURCE_LABELS_RU[current.source]
        quality_label = QUALITY_LABELS_RU[current.quality]
        self.source_metric.set_value(source_label)
        self.quality_metric.set_value(quality_label)

        if not snapshot.available:
            self.bearing_metric.set_value("—", Colors.MUTED)
            self.uncertainty_metric.set_value("—", Colors.MUTED)
            tone = "warning" if snapshot.stale else "info"
            title = "Данные устарели" if snapshot.stale else "Направление недоступно"
            self.source_notice.set_notice(
                title,
                f"{bearing_view.detail} {current.message_ru}",
                level=tone,
            )
            self.header.status.set_status(
                "НАПРАВЛЕНИЕ НЕДОСТУПНО",
                "warning" if snapshot.stale else "neutral",
            )
            last_valid = self._format_timestamp(snapshot.last_valid_at)
            self.detail.setText(
                f"Причина: {current.reason_code}\n"
                f"Последнее валидное наблюдение: {last_valid}\n"
                f"Точек в угловой истории: {len(snapshot.trail)}"
            )
            return

        assert current.bearing_deg is not None
        assert current.uncertainty_deg is not None
        self.bearing_metric.set_value(
            bearing_view.value,
            Colors.TEAL if bearing_view.measured else Colors.WARNING,
        )
        self.uncertainty_metric.set_value(
            f"{current.uncertainty_deg:.1f}°",
            Colors.WARNING if current.source is DirectionSource.MANUAL else Colors.TEAL,
        )
        if current.source is DirectionSource.EXTERNAL:
            self.header.status.set_status(bearing_view.state, "ready")
            self.source_notice.set_notice(
                "Измеренный азимут",
                f"{bearing_view.detail} {current.message_ru}",
                level="ready",
            )
        elif current.source is DirectionSource.MANUAL:
            self.header.status.set_status(bearing_view.state, "warning")
            self.source_notice.set_notice(
                "Введено оператором · не измерено",
                f"{bearing_view.detail} {current.message_ru}",
                level="warning",
            )
        else:
            self.header.status.set_status(bearing_view.state, "warning")
            self.source_notice.set_notice(
                "Синтетические данные · Demo",
                f"{bearing_view.detail} {current.message_ru}",
                level="warning",
            )

        confidence = (
            f"{current.confidence * 100:.0f}%"
            if current.confidence is not None
            else "не измерялась"
        )
        evidence_detail = ""
        if current.evidence is not None:
            evidence_detail = (
                f"\nКалибровка: {current.evidence.calibration_id}; "
                f"подтверждающих отсчётов: {current.evidence.sample_count}; "
                f"качество evidence: {current.evidence.quality_score * 100:.0f}%"
            )
        self.detail.setText(
            f"Источник: {current.source_id}\n"
            f"Время наблюдения: {self._format_timestamp(current.captured_at)}\n"
            f"Уверенность: {confidence}\n"
            f"Точек в угловой истории: {len(snapshot.trail)}"
            f"{evidence_detail}"
        )

    def _refresh_rf_trend(self, snapshot: object | None) -> None:
        view = self._rf_trend_presenter.present(snapshot)
        color = {
            "ready": Colors.READY,
            "warning": Colors.WARNING,
            "critical": Colors.CRITICAL,
            "info": Colors.TEAL,
        }.get(view.level, Colors.MUTED)
        self.rf_trend_metric.set_value(view.value, color)
        self.rf_trend_detail.setText(view.detail)

    def _show_unavailable(self, reason_ru: str) -> None:
        self.plot.clear(reason_ru)
        self.bearing_metric.set_value("—", Colors.MUTED)
        self.uncertainty_metric.set_value("—", Colors.MUTED)
        self.source_metric.set_value("НЕТ ИСТОЧНИКА", Colors.MUTED)
        self.quality_metric.set_value("НЕДОСТУПНО", Colors.MUTED)
        self.source_notice.set_notice(
            "Направление недоступно",
            reason_ru,
            level="warning",
        )
        self.header.status.set_status("НАПРАВЛЕНИЕ НЕДОСТУПНО", "neutral")
        self.detail.setText("Активный луч скрыт.")

    def _apply_manual(self) -> None:
        ok, result = call_runtime(
            self.runtime,
            "set_manual_direction",
            self.bearing_input.value(),
            self.uncertainty_input.value(),
        )
        if ok:
            if isinstance(result, DirectionSnapshot):
                self._apply_snapshot(result)
            else:
                self.refresh(current_snapshot(self.runtime))
            return
        self.source_notice.set_notice(
            "Ручная отметка не сохранена",
            str(result),
            level="critical",
        )
        self.header.status.set_status("ОШИБКА РУЧНОГО ВВОДА", "critical")

    def _clear_direction(self) -> None:
        ok, result = call_runtime(self.runtime, "clear_direction")
        if ok:
            if isinstance(result, DirectionSnapshot):
                self._apply_snapshot(result)
            else:
                self.refresh(current_snapshot(self.runtime))
            return
        self.source_notice.set_notice(
            "Не удалось очистить направление",
            str(result),
            level="critical",
        )

    @staticmethod
    def _format_timestamp(value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.astimezone().strftime("%d.%m.%Y %H:%M:%S")


__all__ = ["DirectionPage"]
