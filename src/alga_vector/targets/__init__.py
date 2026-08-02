"""Safe target-centric projection and canonical sensor readiness."""

from .aggregator import (
    TargetAggregator,
    TargetAggregatorConfig,
    TargetInputError,
    time_decay,
)
from .dedup import (
    EventDeduplicationDecision,
    EventDeduplicationStatus,
    EventDeduplicator,
    event_semantic_key,
)
from .models import (
    ConfirmationStage,
    FusedTarget,
    PhenomenologicalType,
    SensorReadiness,
    SensorReadinessLevel,
    SensorReadinessSnapshot,
    SensorRole,
    TargetLifecycle,
    TargetRecommendation,
    TargetSourceAttribution,
    TargetUpdate,
    TargetUpdateStatus,
    ValidatedZone,
)
from .readiness import SensorReadinessInterpreter
from .recommendations import TargetRecommendationEngine

__all__ = [
    "ConfirmationStage",
    "EventDeduplicationDecision",
    "EventDeduplicationStatus",
    "EventDeduplicator",
    "FusedTarget",
    "PhenomenologicalType",
    "SensorReadiness",
    "SensorReadinessInterpreter",
    "SensorReadinessLevel",
    "SensorReadinessSnapshot",
    "SensorRole",
    "TargetAggregator",
    "TargetAggregatorConfig",
    "TargetInputError",
    "TargetLifecycle",
    "TargetRecommendation",
    "TargetRecommendationEngine",
    "TargetSourceAttribution",
    "TargetUpdate",
    "TargetUpdateStatus",
    "ValidatedZone",
    "event_semantic_key",
    "time_decay",
]
