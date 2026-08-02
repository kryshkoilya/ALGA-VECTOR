from __future__ import annotations

import pytest

from alga_vector.signal_analysis import configs_for_detection_sensitivity


def test_high_sensitivity_lowers_energy_and_temporal_entry_gates() -> None:
    high = configs_for_detection_sensitivity("high")
    balanced = configs_for_detection_sensitivity("balanced")

    assert high.detector.activity_margin_db < balanced.detector.activity_margin_db
    assert high.temporal.attack_excess_db < balanced.temporal.attack_excess_db
    assert high.temporal.release_excess_db < balanced.temporal.release_excess_db
    assert (
        high.temporal.minimum_heuristic_score
        < balanced.temporal.minimum_heuristic_score
    )
    assert high.temporal.confirmation_observations >= 2


def test_low_sensitivity_raises_gates_without_changing_identity_policy() -> None:
    low = configs_for_detection_sensitivity("low")
    balanced = configs_for_detection_sensitivity("balanced")

    assert low.detector.activity_margin_db > balanced.detector.activity_margin_db
    assert low.temporal.attack_excess_db > balanced.temporal.attack_excess_db
    assert low.temporal.release_excess_db > balanced.temporal.release_excess_db
    assert low.temporal.minimum_heuristic_score > balanced.temporal.minimum_heuristic_score


def test_unknown_sensitivity_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="unsupported detection sensitivity"):
        configs_for_detection_sensitivity("turbo")
