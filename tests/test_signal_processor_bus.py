from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock, Thread

from alga_vector.signal_processor import (
    ConfidenceScore,
    EventSeverity,
    NormalizedEvent,
    NormalizedEventType,
    SensorKind,
    SourceAttribution,
    UnifiedEventBus,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _event(
    event_id: str,
    *,
    severity: EventSeverity = EventSeverity.INFO,
    episode_id: str | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type=NormalizedEventType.RADIO_ACTIVITY_DETECTED,
        observed_at=NOW,
        received_at=NOW,
        severity=severity,
        confidence=ConfidenceScore.heuristic(
            0.5,
            "Эвристическая сила; не вероятность.",
        ),
        summary_ru="RF-активность",
        explanation_ru="Общий спектральный эпизод.",
        recommendation_ru="Продолжайте наблюдение.",
        sources=(
            SourceAttribution(
                sensor_id="rtl",
                sensor_kind=SensorKind.RF_SPECTRUM,
                contribution=0.5,
                independent_confirmation=False,
                explanation_ru="RF-наблюдение.",
            ),
        ),
        episode_id=episode_id,
    )


def test_bus_deduplicates_semantic_episode_and_isolates_subscriber_failure() -> None:
    monotonic = [100.0]
    bus = UnifiedEventBus(
        capacity=4,
        dedup_window_seconds=2.0,
        monotonic_clock=lambda: monotonic[0],
    )
    delivered: list[str] = []

    bus.subscribe(lambda event: delivered.append(event.event_id))

    def fail(_event: NormalizedEvent) -> None:
        raise RuntimeError("subscriber failure")

    bus.subscribe(fail)
    first = bus.publish(_event("first", episode_id="episode"))
    duplicate = bus.publish(_event("second", episode_id="episode"))
    monotonic[0] = 103.0
    repeated = bus.publish(_event("third", episode_id="episode"))

    assert first.accepted and first.delivery_failures == 1
    assert duplicate.duplicate and not duplicate.accepted
    assert repeated.accepted
    assert delivered == ["first", "third"]


def test_bus_preserves_alarm_history_under_info_flood() -> None:
    bus = UnifiedEventBus(capacity=2, dedup_window_seconds=0.0)
    bus.publish(_event("alarm-1", severity=EventSeverity.ALARM))
    bus.publish(_event("alarm-2", severity=EventSeverity.CRITICAL))
    low = bus.publish(_event("info", severity=EventSeverity.INFO))

    assert low.accepted
    assert not low.retained_in_history
    assert {item.event_id for item in bus.recent(limit=10)} == {
        "alarm-1",
        "alarm-2",
    }


def test_bus_history_and_dedup_index_are_bounded() -> None:
    bus = UnifiedEventBus(
        capacity=3,
        dedup_window_seconds=60.0,
        monotonic_clock=lambda: 1.0,
    )
    for index in range(100):
        bus.publish(_event(f"event-{index}", episode_id=f"episode-{index}"))

    assert len(bus.recent(limit=100)) == 3
    assert bus.dedup_entry_count <= 16


def test_bus_sequence_is_thread_safe() -> None:
    bus = UnifiedEventBus(capacity=64, dedup_window_seconds=0.0)
    start = Lock()
    start.acquire()

    def publish(index: int) -> None:
        with start:
            pass
        bus.publish(_event(f"thread-{index}", episode_id=f"thread-{index}"))

    threads = [Thread(target=publish, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    start.release()
    for thread in threads:
        thread.join()

    assert bus.sequence == 20
    assert len(bus.recent(limit=100)) == 20
