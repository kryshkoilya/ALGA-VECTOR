"""Named, testable RF detector sensitivity profiles.

The profiles change the energy/candidate gates only.  They never relax the
independent-evidence policy used for emitter or target identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from .decision import TemporalDecisionConfig
from .detector import DetectorConfig


class DetectionSensitivity(StrEnum):
    LOW = "low"
    BALANCED = "balanced"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class DetectionSensitivityConfigs:
    detector: DetectorConfig
    temporal: TemporalDecisionConfig


def configs_for_detection_sensitivity(
    sensitivity: DetectionSensitivity | str,
    *,
    detector_base: DetectorConfig | None = None,
    temporal_base: TemporalDecisionConfig | None = None,
) -> DetectionSensitivityConfigs:
    """Return explicit gates for the requested operator sensitivity.

    ``high`` intentionally favours visibility of weak anomalies.  Confirmation
    still requires temporal support, and all RF-only output remains generic.
    """

    try:
        selected = DetectionSensitivity(str(sensitivity).strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported detection sensitivity: {sensitivity!r}") from exc

    detector = detector_base or DetectorConfig()
    temporal = temporal_base or TemporalDecisionConfig()
    if selected is DetectionSensitivity.HIGH:
        return DetectionSensitivityConfigs(
            detector=replace(
                detector,
                activity_margin_db=5.0,
                impulse_median_excess_db=8.0,
            ),
            temporal=replace(
                temporal,
                confirmation_window=4,
                confirmation_observations=2,
                minimum_heuristic_score=0.35,
                attack_excess_db=6.5,
                release_excess_db=3.5,
                minimum_confirm_dwell_seconds=0.08,
            ),
        )
    if selected is DetectionSensitivity.LOW:
        return DetectionSensitivityConfigs(
            detector=replace(
                detector,
                activity_margin_db=10.0,
                impulse_median_excess_db=15.0,
            ),
            temporal=replace(
                temporal,
                minimum_heuristic_score=0.65,
                attack_excess_db=13.0,
                release_excess_db=8.0,
            ),
        )
    return DetectionSensitivityConfigs(detector=detector, temporal=temporal)


__all__ = [
    "DetectionSensitivity",
    "DetectionSensitivityConfigs",
    "configs_for_detection_sensitivity",
]
