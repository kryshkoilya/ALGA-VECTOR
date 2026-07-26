"""Small source/adapter boundary for deterministic acoustic integration tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Protocol

from .models import AcousticAssessment, PcmWindow
from .monitoring import AcousticMonitor


class AcousticWindowSource(Protocol):
    """A pull source that supplies already-authorized PCM windows."""

    def read_window(self) -> PcmWindow | None:
        """Return the next window, or ``None`` when no data is available."""

        ...


class DeterministicAcousticSource:
    """Finite in-memory source; it never opens a live microphone."""

    def __init__(self, windows: Iterable[PcmWindow]) -> None:
        self._windows: deque[PcmWindow] = deque(windows)

    @property
    def remaining(self) -> int:
        return len(self._windows)

    def read_window(self) -> PcmWindow | None:
        if not self._windows:
            return None
        return self._windows.popleft()


class AcousticCoreAdapter:
    """Connect any explicit source boundary to the safe acoustic core."""

    def __init__(
        self,
        source: AcousticWindowSource,
        monitor: AcousticMonitor | None = None,
    ) -> None:
        self.source = source
        self.monitor = monitor or AcousticMonitor()

    def poll_once(self) -> AcousticAssessment | None:
        """Process at most one source window without blocking."""

        window = self.source.read_window()
        if window is None:
            return None
        return self.monitor.process(window)
