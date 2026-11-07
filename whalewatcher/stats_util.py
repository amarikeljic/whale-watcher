"""Small statistics helpers with no dependencies: bootstrap CIs, rank correlation, drawdown."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Hashable, Sequence


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


def cluster_bootstrap_mean_ci(groups: Sequence[Sequence[float]], *, samples: int = 10_000, alpha: float = 0.05,
                              seed: int = 12345) -> tuple[float | None, float | None, float | None]:
    """CI for the pooled mean when values come in clusters (positions within wallets).

    Resamples whole clusters, so one wallet with hundreds of positions cannot
    make the interval look tighter than the handful of wallets behind it.
    """
    groups = [list(g) for g in groups if g]
    pooled = [x for g in groups for x in g]
    if not pooled:
        return None, None, None
    m = sum(pooled) / len(pooled)
    if len(groups) < 2:
        return m, None, None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        total = count = 0
        for _ in range(len(groups)):
            g = groups[rng.randrange(len(groups))]
            total += sum(g)
            count += len(g)
        means.append(total / count)
    means.sort()
    lo = means[int(math.floor(alpha / 2 * samples))]
    hi = means[min(samples - 1, int(math.ceil((1 - alpha / 2) * samples)) - 1)]
    return m, lo, hi


def two_way_bootstrap_mean_ci(values: Sequence[float], rows: Sequence[Hashable], cols: Sequence[Hashable], *,
                              samples: int = 10_000, alpha: float = 0.05,
                              seed: int = 12345) -> tuple[float | None, float | None, float | None]:
    """CI for the pooled mean of values that each belong to a row cluster and a column cluster.

    Paper trades share a wallet and an event: one wallet's repeated buys, or several
    wallets on the same event, are not independent results. This resamples the rows
    (wallets) and the columns (events) independently and weights each value by how often
    its row times its column was drawn: Owen's pigeonhole bootstrap (Annals of Applied
    Statistics 1(2), 2007), mildly conservative. With one wallet it reduces to resampling
    events, and with one wallet and one event there is no interval: nothing independent
    to compare.
    """
    n = len(values)
    if n == 0:
        return None, None, None
    m = sum(values) / n
    row_ids = {k: i for i, k in enumerate(dict.fromkeys(rows))}
    col_ids = {k: i for i, k in enumerate(dict.fromkeys(cols))}
    if n == 1 or (len(row_ids) < 2 and len(col_ids) < 2):
        return m, None, None
    sums: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for v, r, c in zip(values, rows, cols, strict=True):
        cell = sums[(row_ids[r], col_ids[c])]
        cell[0] += v
        cell[1] += 1
    cells = [(r, c, total, count) for (r, c), (total, count) in sums.items()]
    n_rows, n_cols = len(row_ids), len(col_ids)
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        wr = [0] * n_rows
        for _ in range(n_rows):
            wr[rng.randrange(n_rows)] += 1
        wc = [0] * n_cols
        for _ in range(n_cols):
            wc[rng.randrange(n_cols)] += 1
        total = count = 0.0
        for r, c, cell_total, cell_count in cells:
            w = wr[r] * wc[c]
            if w:
                total += w * cell_total
                count += w * cell_count
        if count:
            means.append(total / count)
    if len(means) < 2:
        return m, None, None
    means.sort()
    k = len(means)
    return m, means[int(math.floor(alpha / 2 * k))], means[min(k - 1, int(math.ceil((1 - alpha / 2) * k)) - 1)]


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
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
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
