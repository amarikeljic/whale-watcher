"""Small statistics helpers with no dependencies: bootstrap CIs, rank correlation, drawdown."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def bootstrap_mean_ci(values: Sequence[float], *, samples: int = 10_000, alpha: float = 0.05,
                      seed: int = 12345) -> tuple[float | None, float | None, float | None]:
    """Percentile bootstrap CI for the mean. Seeded so a report reads the same twice."""
    n = len(values)
    if n == 0:
        return None, None, None
    m = sum(values) / n
    if n == 1:
        return m, None, None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo = means[int(math.floor(alpha / 2 * samples))]
    hi = means[min(samples - 1, int(math.ceil((1 - alpha / 2) * samples)) - 1)]
    return m, lo, hi


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def max_drawdown(pnls_in_order: Sequence[float]) -> float:
    """Largest peak-to-trough fall of cumulative P&L (a positive number, 0 if none)."""
    peak = cumulative = 0.0
    worst = 0.0
    for pnl in pnls_in_order:
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst
