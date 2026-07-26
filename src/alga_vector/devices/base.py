from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from alga_vector.domain.enums import Capability
from alga_vector.domain.errors import AppError
from alga_vector.domain.models import DeviceSnapshot, SpectrumFrame, utc_now

if TYPE_CHECKING:
    from .capabilities import ReceiverHardwareProfile

Clock = Callable[[], datetime]


class DeviceAdapter(ABC):
    """Narrow, side-effect-aware adapter boundary.

    Implementations may inspect only the single configured device represented by
    the adapter. Discovery of arbitrary serial/USB devices is intentionally not
    part of this interface.
    """

    def __init__(
        self,
        *,
        adapter_id: str,
        display_name: str,
        kind: str,
        connection: str,
        capabilities: frozenset[Capability],
        clock: Clock = utc_now,
    ) -> None:
        self.adapter_id = adapter_id
        self.display_name = display_name
        self.kind = kind
        self.connection = connection
        self.capabilities = capabilities
        self._clock = clock
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def receiver_profile(self) -> ReceiverHardwareProfile | None:
        """Return the enforceable receive envelope when one is known."""

        return None

    @abstractmethod
    def inspect(self) -> DeviceSnapshot:
        """Return a non-blocking snapshot for this configured device only."""

    def read_spectrum(
        self,
        *,
        sequence: int,
        center_frequency_hz: int,
        span_hz: int,
        bins: int = 512,
    ) -> SpectrumFrame | None:
        """Return a spectrum frame when supported; default adapters return none."""

        self._ensure_open()
        return None

    def close(self) -> None:
        """Release resources. The operation is deliberately idempotent."""

        self._closed = True

    def reconnect(self) -> DeviceSnapshot:
        """Reconnect this one configured adapter without discovering other devices."""

        self._ensure_open()
        return self.inspect()

    def _ensure_open(self) -> None:
        if self._closed:
            raise AppError(
                code="DEVICE.ADAPTER_CLOSED",
                message_ru="Адаптер устройства уже остановлен.",
                operator_action_ru="Перезапустите сеанс мониторинга.",
                retryable=False,
                technical_details={"adapter_id": self.adapter_id},
            )
