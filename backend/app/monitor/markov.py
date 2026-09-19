from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from itertools import pairwise

State = tuple[int, int]


class CompactMarkovModel:
    """Small monotonic absorbing chain over (worst_band, elevated_band_count)."""

    def __init__(self, transitions: dict[State, dict[State, float]]) -> None:
        self.transitions = transitions

    @classmethod
    def prior(cls) -> CompactMarkovModel:
        transitions: dict[State, dict[State, float]] = {}
        for maximum in range(5):
            for elevated in range(7):
                state = (maximum, elevated)
                if maximum == 4:
                    transitions[state] = {state: 1.0}
                    continue
                violation = min(0.35, 0.01 + 0.025 * maximum + 0.015 * elevated)
                band_advance = min(0.35, 0.08 + 0.04 * maximum)
                breadth_advance = min(0.25, 0.04 + 0.025 * elevated)
                targets: dict[State, float] = {(4, max(1, elevated)): violation}
                next_maximum = min(3, maximum + 1)
                next_band = (
                    next_maximum,
                    max(elevated, 1) if next_maximum >= 2 else 0,
                )
                targets[next_band] = targets.get(next_band, 0.0) + band_advance
                next_breadth = (maximum, min(6, elevated + 1)) if maximum >= 2 else state
                targets[next_breadth] = targets.get(next_breadth, 0.0) + breadth_advance
                used = sum(targets.values())
                targets[state] = targets.get(state, 0.0) + max(0.0, 1.0 - used)
                transitions[state] = targets
        return cls(transitions)

    @classmethod
    def fit(
        cls,
        trajectories: Iterable[Sequence[State]],
        *,
        alpha: float = 1.0,
    ) -> CompactMarkovModel:
        counts: defaultdict[State, defaultdict[State, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for trajectory in trajectories:
            for source, target in pairwise(trajectory):
                if _monotonic(source, target):
                    counts[source][target] += 1.0

        prior = cls.prior()
        fitted: dict[State, dict[State, float]] = {}
        for source, prior_targets in prior.transitions.items():
            if source[0] == 4:
                fitted[source] = {source: 1.0}
                continue
            allowed = set(prior_targets) | set(counts[source])
            weighted = {
                target: counts[source][target] + alpha * prior_targets.get(target, 0.01)
                for target in allowed
                if _monotonic(source, target)
            }
            total = sum(weighted.values())
            fitted[source] = {target: value / total for target, value in weighted.items()}
        return cls(fitted)

    def p_violation(self, state: State, horizon: int) -> float:
        distribution = {state: 1.0}
        for _ in range(horizon):
            next_distribution: defaultdict[State, float] = defaultdict(float)
            for source, source_probability in distribution.items():
                for target, probability in self.transitions.get(source, {source: 1.0}).items():
                    next_distribution[target] += source_probability * probability
            distribution = dict(next_distribution)
        return round(
            sum(probability for (maximum, _), probability in distribution.items() if maximum == 4),
            6,
        )


def _monotonic(source: State, target: State) -> bool:
    source_maximum, source_elevated = source
    target_maximum, target_elevated = target
    return target_maximum >= source_maximum and (
        target_maximum > source_maximum or target_elevated >= source_elevated
    )
