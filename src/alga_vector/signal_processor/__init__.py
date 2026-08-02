"""Unified normalized signal/event pipeline for Simple and Expert UI modes."""

from .bus import EventBusDiagnostics, PublishResult, UnifiedEventBus
from .interpretation import HumanReadableInterpreter
from .normalizer import NormalizationResult, SnapshotEventNormalizer
from .policy import FailClosedEventPolicy, PolicyDecision
from .processor import UnifiedSignalProcessor
from .recommendations import OperatorRecommendation, RecommendationEngine
from .schema import (
    ConfidenceBand,
    ConfidenceScore,
    DirectionEstimate,
    EventPolicyViolation,
    EventSeverity,
    EvidenceFact,
    NormalizedEvent,
    NormalizedEventType,
    OperatorSituation,
    OperatorSituationMode,
    SensorAvailability,
    SensorKind,
    SensorState,
    SourceAttribution,
    ValidatedIdentityEvidence,
)

__all__ = [
    "ConfidenceBand",
    "ConfidenceScore",
    "DirectionEstimate",
    "EventBusDiagnostics",
    "EventPolicyViolation",
    "EventSeverity",
    "EvidenceFact",
    "FailClosedEventPolicy",
    "HumanReadableInterpreter",
    "NormalizationResult",
    "NormalizedEvent",
    "NormalizedEventType",
    "OperatorRecommendation",
    "OperatorSituation",
    "OperatorSituationMode",
    "PolicyDecision",
    "PublishResult",
    "RecommendationEngine",
    "SensorAvailability",
    "SensorKind",
    "SensorState",
    "SnapshotEventNormalizer",
    "SourceAttribution",
    "UnifiedEventBus",
    "UnifiedSignalProcessor",
    "ValidatedIdentityEvidence",
]
