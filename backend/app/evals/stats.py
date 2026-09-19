"""Scientific summaries for lab-suite walks. Stdlib only; no scipy."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any

# Two-sided 95% Student-t critical values by degrees of freedom.
# df>=30 uses z=1.96. Source: standard t tables.
_T_CRIT_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    12: 2.179,
    14: 2.145,
    16: 2.120,
    18: 2.101,
    20: 2.086,
    24: 2.064,
    29: 2.045,
}


def _t_crit_95(df: int) -> float:
    if df < 1:
        return float("nan")
    if df >= 30:
        return 1.96
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    below = max(key for key in _T_CRIT_95 if key < df)
    return _T_CRIT_95[below]


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or isinstance(value, bool) or not math.isfinite(value):
        return None
    return round(value, digits)


def describe(values: list[float] | list[int]) -> dict[str, Any]:
    """n, mean, median, sample stdev, min/max, and 95% t CI of the mean (n>=2)."""
    series = [float(item) for item in values]
    n = len(series)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "stdev": None,
            "min": None,
            "max": None,
            "ci95": None,
        }
    mean = statistics.fmean(series)
    median = statistics.median(series)
    stdev = statistics.stdev(series) if n >= 2 else 0.0
    ci = None
    if n >= 2 and stdev > 0:
        half = _t_crit_95(n - 1) * stdev / math.sqrt(n)
        ci = {"low": _round(mean - half), "high": _round(mean + half)}
    elif n >= 2:
        ci = {"low": _round(mean), "high": _round(mean)}
    return {
        "n": n,
        "mean": _round(mean),
        "median": _round(median),
        "stdev": _round(stdev),
        "min": _round(min(series)),
        "max": _round(max(series)),
        "ci95": ci,
    }


def wilson_ci(successes: int, n: int, z: float = 1.96) -> dict[str, float] | None:
    """95% Wilson score interval for a binomial rate. None when n=0."""
    if n <= 0:
        return None
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / denom
    return {"low": round(centre - half, 4), "high": round(centre + half, 4)}


def rate(successes: int, n: int) -> dict[str, Any]:
    if n <= 0:
        return {"n": 0, "k": 0, "rate": None, "ci95": None}
    return {
        "n": n,
        "k": successes,
        "rate": round(successes / n, 4),
        "ci95": wilson_ci(successes, n),
    }


def confusion(
    pairs: list[tuple[str, str]],
    row_labels: list[str],
    col_labels: list[str],
) -> dict[str, Any]:
    counts: Counter[tuple[str, str]] = Counter(pairs)
    matrix = [
        [counts[(row, col)] for col in col_labels] for row in row_labels
    ]
    return {
        "rows": row_labels,
        "cols": col_labels,
        "matrix": matrix,
        "n": len(pairs),
    }


def group_rates(
    rows: list[dict[str, Any]],
    key: str,
    predicate,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key, "unknown"))].append(row)
    return {
        name: rate(sum(1 for item in items if predicate(item)), len(items))
        for name, items in sorted(buckets.items())
    }
