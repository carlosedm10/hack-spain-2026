"""Tunable constants for Sentinel and the action gate.

Defaults match the shipped inspect/decide behaviour. Eval HPO installs
replacements via `knobs_scope` so `service.ingest` sees them. Production
never sets the context var, so it always uses DEFAULT_KNOBS.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MonitorKnobs:
    n2_write_burst: int = 5
    n2_window: int = 20
    e2_scope_cap: int = 100
    e6_lookback: int = 5
    e6_handoff_gap: int = 3
    hold_sentinel_level: int = 2
    hold_trajectory_risk: float = 0.55
    hold_confidence: float = 0.55
    refuse_sentinel_level: int = 4
    refuse_risk: float = 0.5

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_KNOBS = MonitorKnobs()
_ACTIVE: ContextVar[MonitorKnobs] = ContextVar("monitor_knobs", default=DEFAULT_KNOBS)


def active_knobs() -> MonitorKnobs:
    return _ACTIVE.get()


class knobs_scope:
    """Temporarily install knobs for inspect/decide (and therefore ingest)."""

    def __init__(self, knobs: MonitorKnobs) -> None:
        self._knobs = knobs
        self._token = None

    def __enter__(self) -> MonitorKnobs:
        self._token = _ACTIVE.set(self._knobs)
        return self._knobs

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _ACTIVE.reset(self._token)
