"""Small, hardware-agnostic runtime access helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable


@runtime_checkable
class RuntimeLike(Protocol):
    """Minimum synchronous surface consumed by the desktop shell."""

    def current_snapshot(self) -> object: ...

    def rescan(self) -> object: ...

    def reconnect(self, device_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimeReadError:
    """Sanitized technical context for a failed UI snapshot read."""

    operation: str
    exception_type: str
    message: str

    @property
    def technical(self) -> str:
        return f"{self.operation}: {self.exception_type}: {self.message}"

    def __str__(self) -> str:
        return self.technical


@dataclass(frozen=True, slots=True)
class UnavailableSignalAssessment:
    """Conservative assessment used only while runtime state is unavailable."""

    state: str = "data_unreliable"
    trust: str = "low"
    source_id: str = "runtime"
    reason_code: str = "UI.RUNTIME_SNAPSHOT_READ_FAILED"
    headline_ru: str = "Ошибка чтения состояния"
    explanation_ru: str = (
        "Интерфейс не получил актуальное состояние системы; прежние показатели "
        "не выдаются за текущие."
    )
    operator_action_ru: str = (
        "Повторите обновление. Если ошибка сохраняется, откройте диагностику."
    )
    quality_flags: tuple[str, ...] = ("runtime_snapshot_unavailable",)
    evidence: None = None


@dataclass(frozen=True, slots=True)
class RuntimeReadIncident:
    """Visible, non-persistent incident describing a UI/runtime boundary failure."""

    incident_id: None
    code: str
    title_ru: str
    message_ru: str
    action_ru: str
    severity: str
    source: str
    occurred_at: str
    acknowledged: bool
    acknowledgeable: bool
    technical: RuntimeReadError


@dataclass(frozen=True, slots=True)
class UnavailableRuntimeSnapshot:
    """Immutable fail-closed snapshot returned instead of a silent ``None``."""

    runtime_error: RuntimeReadError
    incidents: tuple[RuntimeReadIncident, ...]
    signal_assessment: UnavailableSignalAssessment
    revision: int = 0
    devices: tuple[object, ...] = ()
    capabilities: tuple[object, ...] = ()
    signal_events: tuple[object, ...] = ()
    spectrum: None = None
    mode: str = "unavailable"
    runtime_mode: str = "unavailable"
    profile_name: str = "Состояние недоступно"
    experience_level: str = "guided"
    readiness_percent: int = 0
    location: None = None
    map_status: None = None
    direction: None = None
    acoustic: None = None
    airspace: None = None
    fusion_decision: None = None
    scan_plan: None = None
    operator_situation: None = None
    normalized_events: tuple[object, ...] = ()
    targets: tuple[object, ...] = ()
    current_target: None = None
    sensor_readiness: None = None


def value_of(value: object, default: str = "") -> str:
    """Normalize enums and arbitrary values for operator-facing text."""

    if value is None:
        return default
    raw = getattr(value, "value", value)
    return str(raw)


def attr(source: object | None, name: str, default: Any = None) -> Any:
    """Read a mapping key or object attribute without constraining runtime types."""

    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def items(source: object | None, name: str) -> tuple[object, ...]:
    """Read an iterable snapshot collection while excluding strings."""

    raw = attr(source, name, ())
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, dict)):
        return tuple(raw)
    return ()


def unavailable_snapshot(
    error: Exception,
    *,
    operation: str = "current_snapshot",
) -> UnavailableRuntimeSnapshot:
    """Build an immutable operator-visible failure snapshot."""

    failure = RuntimeReadError(
        operation=operation,
        exception_type=type(error).__name__,
        message=str(error).strip() or "исключение без сообщения",
    )
    incident = RuntimeReadIncident(
        incident_id=None,
        code="UI.RUNTIME_SNAPSHOT_READ_FAILED",
        title_ru="Ошибка чтения состояния",
        message_ru=(
            "Актуальное состояние системы недоступно. Готовность принудительно "
            "сброшена до 0%; пустые данные не считаются нормальной работой."
        ),
        action_ru=(
            "Повторите обновление и откройте диагностику, если ошибка сохраняется."
        ),
        severity="critical",
        source="desktop-ui",
        occurred_at="—",
        acknowledged=False,
        acknowledgeable=False,
        technical=failure,
    )
    return UnavailableRuntimeSnapshot(
        runtime_error=failure,
        incidents=(incident,),
        signal_assessment=UnavailableSignalAssessment(),
    )


def runtime_error_detail(snapshot: object | None) -> str:
    """Return sanitized runtime-read detail carried by a failure snapshot."""

    error = attr(snapshot, "runtime_error")
    if error is None:
        return ""
    technical = attr(error, "technical")
    return str(technical if technical is not None else error).strip()


def current_snapshot(runtime: object | None) -> object | None:
    """Return the newest snapshot or an explicit fail-closed UI snapshot."""

    if runtime is None:
        return None
    try:
        getter = getattr(runtime, "current_snapshot", None)
    except Exception as exc:
        return unavailable_snapshot(exc, operation="runtime.current_snapshot")
    if callable(getter):
        try:
            snapshot = getter()
        except Exception as exc:
            return unavailable_snapshot(exc, operation="runtime.current_snapshot")
        if snapshot is None:
            return unavailable_snapshot(
                RuntimeError("runtime.current_snapshot() вернул None"),
                operation="runtime.current_snapshot",
            )
        return cast(object, snapshot)
    try:
        snapshot = getattr(runtime, "snapshot", runtime)
        if callable(snapshot):
            snapshot = snapshot()
    except Exception as exc:
        return unavailable_snapshot(exc, operation="runtime.snapshot")
    if snapshot is None:
        return unavailable_snapshot(
            RuntimeError("runtime.snapshot вернул None"),
            operation="runtime.snapshot",
        )
    return cast(object, snapshot)


def call_runtime(runtime: object | None, method: str, *args: object) -> tuple[bool, object]:
    """Invoke an optional runtime action and convert failures into safe results."""

    if runtime is None:
        return False, "Runtime недоступен"
    action = getattr(runtime, method, None)
    if not callable(action):
        return False, f"Действие «{method}» не поддерживается"
    try:
        return True, action(*args)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def provenance_key(snapshot: object | None) -> str:
    """Return an explicit provenance state without inventing simulation.

    A missing snapshot normally means that the runtime is still starting or
    that reading it failed.  Treating that state as simulation made a live
    launch look like demo mode precisely when diagnostics mattered most.
    """

    if snapshot is None:
        return "unavailable"
    if runtime_error_detail(snapshot):
        return "unavailable"
    runtime_mode = value_of(attr(snapshot, "runtime_mode")).strip().lower()
    if runtime_mode == "unavailable":
        return "unavailable"
    if runtime_mode in {"safe", "demo"}:
        return runtime_mode
    mode = value_of(attr(snapshot, "mode")).strip().lower()
    if mode in {"live", "replayed", "simulated", "demo"}:
        return mode
    if runtime_mode == "live":
        return "live"
    return "unknown"


def provenance_ru(snapshot: object | None) -> str:
    if runtime_error_detail(snapshot):
        return "ОШИБКА ЧТЕНИЯ СОСТОЯНИЯ"
    return {
        "live": "ЖИВЫЕ ДАННЫЕ",
        "replayed": "ВОСПРОИЗВЕДЕНИЕ",
        "simulated": "ДЕМО · СИМУЛЯЦИЯ",
        "demo": "ДЕМО · СИМУЛЯЦИЯ",
        "safe": "БЕЗОПАСНЫЙ РЕЖИМ",
        "unavailable": "ДАННЫЕ НЕДОСТУПНЫ",
        "unknown": "ИСТОЧНИК НЕИЗВЕСТЕН",
    }.get(provenance_key(snapshot), "ИСТОЧНИК НЕИЗВЕСТЕН")
