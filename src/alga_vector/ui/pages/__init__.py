"""Top-level operator pages."""

from __future__ import annotations

from .dashboard import DashboardPage
from .devices import DevicesPage
from .diagnostics import DiagnosticsPage
from .direction import DirectionPage
from .events import SignalEventsPage
from .map import MapPage
from .settings import SettingsPage
from .simple_situation import SimpleSituationPage
from .spectrum import SpectrumPage
from .targets import ExpertTargetsPage

__all__ = [
    "DashboardPage",
    "DevicesPage",
    "DiagnosticsPage",
    "DirectionPage",
    "ExpertTargetsPage",
    "MapPage",
    "SettingsPage",
    "SignalEventsPage",
    "SimpleSituationPage",
    "SpectrumPage",
]
