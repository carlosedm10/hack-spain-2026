from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Level(IntEnum):
    NONE = 0
    MILD = 1
    MODERATE = 2
    SEVERE = 3
    CRITICAL = 4
    CATASTROPHIC = 5

    @classmethod
    def from_choice(cls, choice: str) -> Level:
        try:
            return cls(int(choice.split("_")[1]))
        except (IndexError, ValueError) as e:
            raise ValueError(f"unknown jev choice: {choice!r}") from e


@dataclass
class Verdict:
    level: Level
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    intent: str | None = None
    containment_breached: bool = False
    degraded: bool = False
    degraded_reason: str | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    latency_ms: float | None = None


@dataclass
class WatcherVerdict:
    escalate: bool
    suspected_level: int | None = None
    note: str | None = None
