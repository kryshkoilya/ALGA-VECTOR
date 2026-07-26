from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import IncidentSeverity


@dataclass(slots=True, frozen=True)
class AppError(Exception):
    code: str
    message_ru: str
    operator_action_ru: str
    severity: IncidentSeverity = IncidentSeverity.ERROR
    retryable: bool = False
    technical_details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message_ru}"

