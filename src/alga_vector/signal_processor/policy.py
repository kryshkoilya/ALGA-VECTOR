"""Central fail-closed policy for normalized operator events."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    EventPolicyViolation,
    NormalizedEvent,
    NormalizedEventType,
    SensorKind,
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


class FailClosedEventPolicy:
    """Defense-in-depth checks in addition to schema construction guards."""

    def assess(self, event: NormalizedEvent) -> PolicyDecision:
        reasons: list[str] = []
        if (
            event.event_type
            in {
                NormalizedEventType.LIKELY_DRONE_SIGNATURE,
                NormalizedEventType.TARGET_CONFIRMED,
            }
            and all(
                source.sensor_kind
                in {SensorKind.RF_TRIGGER, SensorKind.RF_SPECTRUM}
                for source in event.sources
            )
        ):
            reasons.append("POLICY.IDENTITY_FROM_RF_ONLY")
        if (
            event.direction is not None
            and not event.direction.is_fresh_at(event.received_at)
        ):
            reasons.append("POLICY.STALE_DIRECTION")
        if event.confidence.is_calibrated_probability:
            reasons.append("POLICY.FALSE_PROBABILITY")
        return PolicyDecision(
            allowed=not reasons,
            reason_codes=tuple(reasons),
        )

    def require_safe(self, event: NormalizedEvent) -> None:
        decision = self.assess(event)
        if not decision.allowed:
            raise EventPolicyViolation(
                "event rejected: " + ", ".join(decision.reason_codes)
            )


__all__ = ["FailClosedEventPolicy", "PolicyDecision"]
