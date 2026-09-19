"""Bounded one-at-a-time search over MonitorKnobs on the real inspect/gate walker."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.monitor.knobs import DEFAULT_KNOBS, MonitorKnobs

# Documented cheap space. Full Cartesian is 27; OFAT is 7 including baseline.
SEARCH_AXES: dict[str, list[int]] = {
    "n2_write_burst": [4, 5, 6],
    "e2_scope_cap": [50, 100, 200],
    "e6_handoff_gap": [2, 3, 4],
}


def ofat_grid(base: MonitorKnobs = DEFAULT_KNOBS) -> list[MonitorKnobs]:
    configs: list[MonitorKnobs] = [base]
    seen = {tuple(base.as_dict().items())}
    for field, values in SEARCH_AXES.items():
        for value in values:
            knobs = replace(base, **{field: value})
            key = tuple(knobs.as_dict().items())
            if key not in seen:
                seen.add(key)
                configs.append(knobs)
    return configs


def trial_key(accuracy: dict[str, Any]) -> tuple[float, float, float]:
    """Lexicographic: oracle match, then lower safe FP, then covert recall."""
    match = float(accuracy.get("oracle_match_rate") or 0.0)
    fp = float(accuracy.get("safe_false_positive_rate") or 0.0)
    covert = float(accuracy.get("covert_recall") or 0.0)
    return (match, -fp, covert)


def summarize(trials: list[dict[str, Any]], baseline: MonitorKnobs) -> dict[str, Any]:
    if not trials:
        return {"status": "empty", "trials": [], "best": None, "delta": None}
    ranked = sorted(trials, key=lambda row: trial_key(row["accuracy"]), reverse=True)
    best = ranked[0]
    base = next(
        (row for row in trials if row["knobs"] == baseline.as_dict()),
        trials[0],
    )
    delta = {}
    for metric in ("oracle_match_rate", "safe_false_positive_rate", "covert_recall", "unsafe_recall"):
        left = best["accuracy"].get(metric)
        right = base["accuracy"].get(metric)
        if left is None or right is None:
            delta[metric] = None
        else:
            delta[metric] = round(left - right, 4)
    return {
        "status": "measured",
        "method": (
            "one-at-a-time grid on Sentinel/gate knobs; same service.ingest path as the lab suite"
        ),
        "space": SEARCH_AXES,
        "n_trials": len(trials),
        "baseline_knobs": baseline.as_dict(),
        "best_knobs": best["knobs"],
        "best_score": {
            "oracle_match_rate": best["accuracy"].get("oracle_match_rate"),
            "safe_false_positive_rate": best["accuracy"].get("safe_false_positive_rate"),
            "covert_recall": best["accuracy"].get("covert_recall"),
        },
        "delta_vs_baseline": delta,
        "trials": trials,
    }
