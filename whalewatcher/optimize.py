"""Find the wallet requirements that pick wallets worth copying, without fooling ourselves.

Any search over thousands of threshold combinations will find one that looked
brilliant on the data it was searched on: with enough tries, chance supplies a
winner. So the search is walk-forward, and its answer is judged only on data
it did not see while choosing.

Folds      cutoffs with back-to-back, non-overlapping holdouts (default: three
           two-month holdouts ending today). Each fold has its own leak-free
           candidate pool, built from large trades placed before its cutoff,
           and its own selection metrics computed with data up to the cutoff.
Outcome    what copying would have earned. Every holdout buy group that would
           have fired an alert (at least ``min_whale_trade_usd`` within the
           dedupe window, VWAP inside ``min_price``..``max_price``) is one
           flat-stake trade, bought at that VWAP plus the market's taker fee
           and held to resolution. Only markets resolving inside the holdout
           count, so long-dated bets are under-represented.
Objective  the lower 95% bound of the mean ROI per alert, wallets as clusters,
           so a set that wins on a handful of wallets cannot outrank one that
           wins on many. A requirement set must keep at least ``min_wallets``
           wallets with alerts and ``min_alerts`` alerts in every fold it is
           judged on. Of the sets within one standard error of the best, the
           simplest is chosen (the one-standard-error rule), because the very
           best of thousands of sets is mostly the one that fit the noise.
Steps      choose on the folds before k, test on fold k. The test result is set
           against every eligible wallet, the spec's thresholds, and random sets
           of the same number of wallets (a permutation p-value).
Answer     the requirements chosen on all folds, with the walk-forward tests
           as the evidence for or against trusting them.

The search selects every wallet that passes (no top_k); ranking among passing
wallets is a separate question.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .app import App
from .ledger import Buy, build_ledgers
from .polymarket import Activity, MarketInfo
from .scoring import (
    DAY,
    WalletMetrics,
    _outcome_count,
    _token_index,
    analyse,
    candidate_pool,
    complete_history,
    compute_clv,
    load_wallet_rows,
    rows_from_db,
)
from .stats_util import cluster_bootstrap_mean_ci, spearman
from .timeutil import add_months, iso
from .timing import Progress, Stopwatch

log = logging.getLogger(__name__)

# Values tried for each requirement. None switches a requirement off. The order
# puts the most selective requirements first, which lets the search prune early.
GRID: dict[str, list[Any]] = {
    "min_settled_markets": [10, 20, 30, 50, 75, 100],
    "min_profitable_months": [0, 2, 3, 4, 5],
    "min_roi": [None, 0.0, 0.05, 0.1, 0.2],
    "max_single_market_share": [0.2, 0.3, 0.5, 1.0, None],
    "min_avg_stake_usd": [0, 100, 500, 2000],
    "min_clv": [None, 0.0, 0.02],
    "min_win_rate": [None, 0.5, 0.6],
    "max_two_sided_ratio": [0.1, 0.2, 0.4, None],
    "max_days_since_last_trade": [7, 30],
}
KEYS = list(GRID)
LOOSEST = {"min_settled_markets": 10, "min_profitable_months": 0, "min_roi": None,
           "max_single_market_share": None, "min_avg_stake_usd": 0, "min_clv": None, "min_win_rate": None,
           "max_two_sided_ratio": None, "max_days_since_last_trade": 30}


# The spec's original thresholds, fixed here rather than read from the config: the
# shipped config.yaml uses the profile this search recommended, and the "spec"
# comparison must keep meaning the spec. They equal ScoringConfig's defaults.
SPEC_REQUIREMENTS: dict[str, Any] = {
    "min_settled_markets": 50, "min_profitable_months": 4, "min_roi": 0.05, "max_single_market_share": 0.30,
    "min_avg_stake_usd": 100.0, "min_clv": None, "min_win_rate": None, "max_two_sided_ratio": 0.40,
    "max_days_since_last_trade": 30.0,
}


def passes(m: WalletMetrics, key: str, value: Any) -> bool:
    """One requirement, with the same semantics as scoring.apply_filters."""
    if key == "min_settled_markets":
        return m.settled_markets >= value
    if key == "min_profitable_months":
        return m.profitable_months >= value
    if key == "max_single_market_share":
        return value is None or (m.max_single_market_share is not None and m.max_single_market_share <= value)
    if key == "min_avg_stake_usd":
        return m.avg_stake_usd is not None and m.avg_stake_usd >= value
    if key == "max_days_since_last_trade":
        return m.days_since_last_trade is not None and m.days_since_last_trade <= value
    if key == "max_two_sided_ratio":
        return value is None or m.two_sided_ratio is None or m.two_sided_ratio <= value
    if key == "min_roi":
        return value is None or (m.roi is not None and m.roi >= value)
    if key == "min_win_rate":
        return value is None or (m.win_rate is not None and m.win_rate >= value)
    if key == "min_clv":
        return value is None or (m.clv is not None and m.clv >= value)
    raise KeyError(key)


def passes_all(m: WalletMetrics, req: dict[str, Any]) -> bool:
    return all(passes(m, k, v) for k, v in req.items())


# -- the outcome: what copying the alerts would have earned ----------------------------------


def alert_prices(buys: list[Buy], window: int, min_usd: float) -> list[float]:
    """VWAP of each buy group the monitor would have alerted on.

    Same grouping as monitor.py: a group starts at its first fill and takes
    every fill within ``window`` seconds of it; it alerts once its total
    reaches ``min_usd``.
    """
    out: list[float] = []
    group_start: int | None = None
    usd = shares = 0.0
    for b in sorted(buys, key=lambda b: b.ts):
        if group_start is None or b.ts - group_start > window:
            if group_start is not None and usd >= min_usd and shares > 0:
                out.append(usd / shares)
            group_start, usd, shares = b.ts, 0.0, 0.0
        usd += b.usdc
        shares += b.size
    if group_start is not None and usd >= min_usd and shares > 0:
        out.append(usd / shares)
    return out


def copy_roi(price: float, payout: float, market: MarketInfo) -> float:
    """Buy one share at ``price`` plus the taker fee, hold to resolution."""
    fee = market.fee_rate * (price * (1 - price)) ** (market.fee_exponent or 1) if market.fees_enabled else 0.0
    return payout / (price + fee) - 1


def holdout_alert_rois(rows: list[Activity], markets: dict[str, MarketInfo], start: int, end: int,
                       *, window: int, min_usd: float, min_price: float, max_price: float) -> list[float]:
    """Copy-trade ROI of every alert-worthy buy group in [start, end) on a market resolved in it."""
    ledgers = build_ledgers(rows, end, token_index=_token_index(markets), outcome_count=_outcome_count(markets))
    out: list[float] = []
    for cid, led in ledgers.items():
        m = markets.get(cid)
        if m is None or m.payouts is None or not m.resolved_by(end) or m.resolved_at < start:
            continue
        for i in sorted(led.outcomes_bought):
            if i >= len(m.payouts):
                continue
            buys = [b for b in led.buys if b.outcome_index == i and start <= b.ts < m.resolved_at]
            for price in alert_prices(buys, window, min_usd):
                if min_price <= price <= max_price:
                    out.append(copy_roi(price, m.payouts[i], m))
    return out


# -- the dataset -------------------------------------------------------------------------------


@dataclass
class FoldWallet:
    wallet: str
    m: WalletMetrics
    rois: list[float]


@dataclass
class Fold:
    cutoff: int
    end: int
    candidates: int
    too_active: int
    wallets: list[FoldWallet] = field(default_factory=list)


async def build_folds(app: App, cutoffs: list[int], *, holdout_months: int, clv_sample: int,
                      clv_min_settled: int, say: Callable[[str], None], watch: Stopwatch) -> list[Fold]:
    sc, al = app.cfg.scoring, app.cfg.alerts
    cutoffs = sorted(cutoffs)

    def selection_start(c: int) -> int:
        return add_months(c, -sc.lookback_months) - sc.entry_lookback_days * DAY

    pools: dict[int, dict[str, set[str]]] = {}
    with watch.stage("candidate pools"):
        for c in cutoffs:
            pools[c], _ = await candidate_pool(app, c, historical=True)
            say(f"pool for {iso(c)[:10]}: {len(pools[c])} wallets")

    # One fetch per wallet covering every fold it belongs to. The activity cap is a
    # rate (rows per selection-window second), so a wider window does not trip it.
    windows: dict[str, tuple[int, int]] = {}
    for c in cutoffs:
        s, e = selection_start(c), add_months(c, holdout_months)
        for w in pools[c]:
            ws, we = windows.get(w, (s, e))
            windows[w] = (min(ws, s), max(we, e))
    rate = sc.max_activity_rows / max(1, cutoffs[0] - selection_start(cutoffs[0]))
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "wallet activity", len(windows))

    async def fetch(w: str) -> tuple[str, list[Activity], bool]:
        s, e = windows[w]
        async with sem:
            try:
                rows, truncated = await load_wallet_rows(app, w, s, e, max_rows=int(rate * (e - s)))
            except Exception as exc:
                log.warning("fetch failed for %s: %s", w, exc)
                rows, truncated = [], True
        ticker.tick()
        return w, rows, truncated

    with watch.stage("wallet activity"):
        fetched = await asyncio.gather(*(fetch(w) for w in windows))
    rows_by_wallet = {w: rows for w, rows, t in fetched if not t}
    capped = {w for w, _, t in fetched if t}
    say(f"loading markets for {len(rows_by_wallet)} wallets")
    with watch.stage("markets"):
        markets = await app.markets.ensure({r.condition_id for rows in rows_by_wallet.values() for r in rows})

    folds: list[Fold] = []
    for c in cutoffs:
        sel_start, end = selection_start(c), add_months(c, holdout_months)
        fold = Fold(c, end, len(pools[c]), 0)
        analyses = {}
        for w in pools[c]:
            rows = rows_by_wallet.get(w)
            if rows is None or sum(1 for r in rows if sel_start <= r.ts <= c) > sc.max_activity_rows:
                fold.too_active += 1
                continue
            analyses[w] = analyse(w, rows, markets, c, sc)
        incomplete = {w: a.incomplete for w, a in analyses.items() if a.incomplete}
        if incomplete:
            with watch.stage(f"{iso(c)[:10]}: older history"):
                async def complete(w: str, conds: list[str], c: int = c) -> None:
                    async with sem:
                        try:
                            await complete_history(app, w, conds, c)
                        except Exception as exc:
                            log.warning("history completion failed for %s: %s", w, exc)

                await asyncio.gather(*(complete(w, cs) for w, cs in incomplete.items()))
                for w in incomplete:
                    rows_by_wallet[w] = rows_from_db(app, w, windows[w][1])
                extra = {r.condition_id for w in incomplete for r in rows_by_wallet[w]} - set(markets)
                if extra:
                    markets.update(await app.markets.ensure(extra))
                for w in incomplete:
                    analyses[w] = analyse(w, rows_by_wallet[w], markets, c, sc)
        measured = 0
        for w, a in analyses.items():
            total = a.metrics.settled_markets + a.metrics.unreliable_markets
            if total and a.metrics.unreliable_markets / total > sc.max_unreliable_market_share:
                continue  # cannot be measured, so never eligible (same as scoring)
            rois = holdout_alert_rois(rows_by_wallet[w], markets, c, end, window=al.dedupe_window_seconds,
                                      min_usd=al.min_whale_trade_usd, min_price=al.min_price,
                                      max_price=al.max_price)
            fold.wallets.append(FoldWallet(w, a.metrics, rois))
            measured += 1
        clv_jobs = [fw for fw in fold.wallets if fw.m.settled_markets >= clv_min_settled]
        clv_ticker = Progress(say, f"{iso(c)[:10]} CLV", len(clv_jobs))

        async def clv(fw: FoldWallet, c: int = c, analyses: dict = analyses, ticker: Progress = clv_ticker) -> None:
            async with sem:
                fw.m.clv, fw.m.clv_n, _, _ = await compute_clv(
                    analyses[fw.wallet].clv_positions, app.prices, c, sc.markout_hours,
                    sample=clv_sample, seed=fw.wallet, want_markout=False)
            ticker.tick()

        with watch.stage(f"{iso(c)[:10]}: CLV ({len(clv_jobs)} wallets)"):
            await asyncio.gather(*(clv(fw) for fw in clv_jobs))
        say(f"fold {iso(c)[:10]}: {measured} wallets measured, "
            f"{sum(1 for fw in fold.wallets if fw.rois)} with holdout alerts, "
            f"{sum(len(fw.rois) for fw in fold.wallets)} alerts")
        folds.append(fold)
    del capped
    return folds


# -- scoring a set of wallets ----------------------------------------------------------------------


@dataclass
class Score:
    wallets: int
    alerts: int
    mean: float | None
    se: float | None

    @property
    def lcb(self) -> float | None:
        return None if self.mean is None or self.se is None else self.mean - 1.96 * self.se


class FoldIndex:
    """Per-fold sums and the passing set of every grid value, for a fast search."""

    def __init__(self, fold: Fold) -> None:
        self.fold = fold
        ws = fold.wallets
        self.S = [sum(w.rois) for w in ws]
        self.N = [len(w.rois) for w in ws]
        self.has = frozenset(i for i, n in enumerate(self.N) if n > 0)
        self.sets = {key: {v: frozenset(i for i, w in enumerate(ws) if passes(w.m, key, v)) for v in values}
                     for key, values in GRID.items()}
        self.all = frozenset(range(len(ws)))

    def select(self, req: dict[str, Any]) -> frozenset[int]:
        return frozenset(i for i, w in enumerate(self.fold.wallets) if passes_all(w.m, req))

    def sums(self, chosen: frozenset[int]) -> tuple[int, int, float, float, float, float]:
        live = chosen & self.has
        s = sn = s2 = n2 = 0.0
        n_alerts = 0
        for i in live:
            si, ni = self.S[i], self.N[i]
            s += si
            n_alerts += ni
            s2 += si * si
            sn += si * ni
            n2 += ni * ni
        return len(live), n_alerts, s, s2, sn, n2

    def groups(self, chosen: frozenset[int]) -> list[list[float]]:
        return [self.fold.wallets[i].rois for i in sorted(chosen & self.has)]


def score_from_sums(wallets: int, alerts: int, s: float, s2: float, sn: float, n2: float) -> Score:
    """Pooled mean ROI per alert with a cluster-robust (by wallet) standard error."""
    if alerts == 0:
        return Score(wallets, alerts, None, None)
    mean = s / alerts
    if wallets < 2:
        return Score(wallets, alerts, mean, None)
    resid = max(0.0, s2 - 2 * mean * sn + mean * mean * n2)   # sum over wallets of (S_w - mean * N_w)^2
    se = math.sqrt(wallets / (wallets - 1) * resid) / alerts
    return Score(wallets, alerts, mean, se)


def score_set(index: FoldIndex, chosen: frozenset[int]) -> Score:
    return score_from_sums(*index.sums(chosen))


def pooled(indexes: list[FoldIndex], chosen: list[frozenset[int]]) -> Score:
    totals = [0, 0, 0.0, 0.0, 0.0, 0.0]
    for idx, ch in zip(indexes, chosen, strict=True):
        for k, v in enumerate(idx.sums(ch)):
            totals[k] += v
    return score_from_sums(*totals)


@dataclass
class Candidate:
    req: dict[str, Any]
    train: Score
    per_fold: list[Score]
    test: Score | None = None

    @property
    def complexity(self) -> int:
        return sum(1 for k, v in self.req.items() if v != LOOSEST[k])

    def objective(self, kind: str) -> float | None:
        """``pooled``: lower 95% bound of the mean over all training folds together.
        ``worst``: the lowest per-fold lower bound, so a set that loses in any one
        period ranks low however well it did elsewhere."""
        if kind == "pooled":
            return self.train.lcb
        bounds = [f.lcb for f in self.per_fold]
        return None if any(b is None for b in bounds) else min(bounds)

    def objective_se(self, kind: str) -> float | None:
        if kind == "pooled":
            return self.train.se
        worst = min(self.per_fold, key=lambda f: f.lcb if f.lcb is not None else -9e9)
        return worst.se


OBJECTIVES = ("pooled", "worst")


def search(train: list[FoldIndex], *, min_wallets: int, min_alerts: int,
           test: FoldIndex | None = None, objective: str = "pooled") -> list[Candidate]:
    """Every requirement set feasible on all training folds, best first."""
    out: list[Candidate] = []

    def rec(level: int, current: list[frozenset[int]], current_test: frozenset[int] | None,
            chosen: dict[str, Any]) -> None:
        for idx, cur in zip(train, current, strict=True):
            if len(cur & idx.has) < min_wallets:
                return   # every stricter set is smaller still
        if level == len(KEYS):
            per_fold = [score_set(idx, cur) for idx, cur in zip(train, current, strict=True)]
            if any(sc.alerts < min_alerts for sc in per_fold):
                return
            cand = Candidate(dict(chosen), pooled(train, current), per_fold)
            if test is not None and current_test is not None:
                cand.test = score_set(test, current_test)
            out.append(cand)
            return
        key = KEYS[level]
        for value in GRID[key]:
            chosen[key] = value
            rec(level + 1, [cur & idx.sets[key][value] for idx, cur in zip(train, current, strict=True)],
                None if test is None else current_test & test.sets[key][value], chosen)
        del chosen[key]

    rec(0, [idx.all for idx in train], None if test is None else test.all, {})

    def rank(c: Candidate) -> tuple:
        value = c.objective(objective)
        return (value if value is not None else -9e9, -c.complexity, c.train.alerts)

    out.sort(key=rank, reverse=True)
    return out


def choose(found: list[Candidate], objective: str = "pooled") -> Candidate:
    """The one-standard-error rule: of the sets scoring within one standard error of
    the best, the simplest (fewest requirements tighter than the loosest value).

    Taking the very best of thousands of sets rewards whichever extra thresholds
    happened to fit the training folds' noise; the simplest set that is about as
    good carries forward far better.
    """
    best = found[0]
    value, se = best.objective(objective), best.objective_se(objective)
    if value is None or se is None:
        return best
    floor = value - se
    near = [c for c in found if (c.objective(objective) or -9e9) >= floor]
    return min(near, key=lambda c: (c.complexity, -(c.objective(objective) or -9e9), -c.train.alerts))


def permutation_p(index: FoldIndex, chosen: frozenset[int], *, draws: int = 2000, seed: int = 7) -> float | None:
    """Share of random same-size wallet sets (from wallets with alerts) that did at least as well."""
    pool = sorted(index.has)
    k = len(chosen & index.has)
    observed = score_set(index, chosen).mean
    if observed is None or k == 0 or k >= len(pool):
        return None
    rng = random.Random(seed)
    hits = 0
    for _ in range(draws):
        sample = rng.sample(pool, k)
        mean = sum(index.S[i] for i in sample) / sum(index.N[i] for i in sample)
        if mean >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (draws + 1)


# -- the report ---------------------------------------------------------------------------------------


@dataclass
class TestResult:
    name: str
    score: Score
    ci: tuple[float | None, float | None]
    p_random: float | None = None


@dataclass
class Step:
    train_cutoffs: list[int]
    test_cutoff: int
    chosen: Candidate
    feasible: int
    results: list[TestResult]
    transfer: float | None          # Spearman(train LCB, test mean) across feasible requirement sets
    oracle: Candidate | None        # best on the test fold itself: what hindsight would pick


@dataclass
class OptimizeReport:
    folds: list[Fold]
    steps: list[Step]
    final: Candidate | None
    final_per_fold: list[Score]
    spec_per_fold: list[Score]
    baseline_per_fold: list[Score]
    configs_tried: int
    watch: Stopwatch
    verdict: str = ""
    skipped: list[str] = field(default_factory=list)
    objective: str = "pooled"


def _ci(index: FoldIndex, chosen: frozenset[int], samples: int) -> tuple[float | None, float | None]:
    _, lo, hi = cluster_bootstrap_mean_ci(index.groups(chosen), samples=samples)
    return lo, hi


def evaluate(folds: list[Fold], *, min_wallets: int, min_alerts: int, spec: dict[str, Any],
             watch: Stopwatch, samples: int = 3000, objective: str = "pooled") -> OptimizeReport:
    indexes = [FoldIndex(f) for f in folds]
    configs = math.prod(len(v) for v in GRID.values())
    steps: list[Step] = []
    skipped: list[str] = []

    def thin(idxs: list[FoldIndex]) -> str:
        return ", ".join(f"{iso(i.fold.cutoff)[:10]} has {len(i.has)} wallets with alerts" for i in idxs
                         if len(i.has) < min_wallets) or "no set keeps enough wallets and alerts in every fold"

    for k in range(1, len(indexes)):
        train, test = indexes[:k], indexes[k]
        with watch.stage(f"search: train {k} fold(s), test {iso(folds[k].cutoff)[:10]}"):
            found = search(train, min_wallets=min_wallets, min_alerts=min_alerts, test=test, objective=objective)
        if not found:
            skipped.append(f"test on {iso(folds[k].cutoff)[:10]} skipped: {thin(train)} "
                           f"(need {min_wallets} wallets and {min_alerts} alerts per training fold)")
            continue
        chosen = choose(found, objective)
        chosen_set = test.select(chosen.req)
        spec_set = test.select(spec)
        baseline = test.all
        results = [
            TestResult("chosen on earlier folds", score_set(test, chosen_set), _ci(test, chosen_set, samples),
                       permutation_p(test, chosen_set)),
            TestResult("spec thresholds", score_set(test, spec_set), _ci(test, spec_set, samples),
                       permutation_p(test, spec_set)),
            TestResult("every eligible wallet", score_set(test, baseline), _ci(test, baseline, samples)),
        ]
        both = [c for c in found if c.test is not None and c.test.mean is not None
                and c.test.wallets >= min_wallets and c.test.alerts >= min_alerts]
        transfer = (spearman([c.objective(objective) or -9e9 for c in both], [c.test.mean for c in both])
                    if len(both) >= 3 else None)
        oracle = max(both, key=lambda c: c.test.lcb if c.test.lcb is not None else -9e9) if both else None
        steps.append(Step([f.cutoff for f in folds[:k]], folds[k].cutoff, chosen, len(found), results,
                          transfer, oracle))

    with watch.stage("search: all folds (the recommendation)"):
        found_all = search(indexes, min_wallets=min_wallets, min_alerts=min_alerts, objective=objective)
    final = choose(found_all, objective) if found_all else None
    final_per_fold = [score_set(idx, idx.select(final.req)) for idx in indexes] if final else []
    if final is None:
        skipped.append(f"no recommendation: {thin(indexes)}")
    report = OptimizeReport(
        folds, steps, final, final_per_fold,
        [score_set(idx, idx.select(spec)) for idx in indexes],
        [score_set(idx, idx.all) for idx in indexes], configs, watch, skipped=skipped, objective=objective)
    report.verdict = verdict(report)
    return report


def verdict(rep: OptimizeReport) -> str:
    if not rep.steps:
        return ("Inconclusive: no requirement set kept enough wallets in every training fold, so nothing could be "
                "tested. Widen the candidate pools or lower --min-wallets.")
    tests = [s.results[0] for s in rep.steps if s.results[0].score.mean is not None and s.results[0].score.wallets >= 3]
    if not tests:
        return ("Inconclusive: the chosen requirements kept fewer than 3 wallets with alerts on every unseen fold, "
                "so there is nothing to judge them on.")
    beat_random = [t for t in tests if t.p_random is not None and t.p_random < 0.05]
    positive = [t for t in tests if t.ci[0] is not None and t.ci[0] > 0]
    if positive and len(beat_random) == len(tests):
        return ("The requirements chosen on earlier folds made money on later folds with a 95% interval above zero "
                "and beat random wallet sets. That is the evidence needed to trust them.")
    if beat_random:
        return ("The chosen requirements beat random wallet sets in some later folds, but their out-of-sample ROI "
                "interval still includes zero or lies below it. They rank wallets better than chance without "
                "showing that copying them makes money.")
    return ("Requirements chosen on earlier folds did not beat random wallet sets on later folds. The thresholds "
            "that look best in hindsight do not carry forward, so no requirement set here can be trusted to pick "
            "wallets worth copying.")


def save_folds(folds: list[Fold], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = [{"cutoff": iso(f.cutoff), "end": iso(f.end), "candidates": f.candidates, "too_active": f.too_active,
             "wallets": [{"wallet": w.wallet, "metrics": asdict(w.m), "alert_rois": w.rois} for w in f.wallets]}
            for f in folds]
    Path(path).write_text(json.dumps(data, indent=1))


async def run_optimize(app: App, cutoffs: list[int], *, holdout_months: int = 2, clv_sample: int = 50,
                       clv_min_settled: int = 20, min_wallets: int = 10, min_alerts: int = 50,
                       dataset_path: str | None = None, objective: str = "pooled",
                       progress: Callable[[str], None] | None = None) -> OptimizeReport:
    say = progress or (lambda _m: None)
    watch = Stopwatch(app.http)
    folds = await build_folds(app, cutoffs, holdout_months=holdout_months, clv_sample=clv_sample,
                              clv_min_settled=clv_min_settled, say=say, watch=watch)
    if dataset_path:
        save_folds(folds, dataset_path)
    return evaluate(folds, min_wallets=min_wallets, min_alerts=min_alerts, spec=dict(SPEC_REQUIREMENTS),
                    watch=watch, samples=min(app.cfg.stats.bootstrap_samples, 3000), objective=objective)


def load_folds(path: str) -> list[Fold]:
    """Read a dataset written by save_folds, to re-run the search without fetching."""
    from .timeutil import parse_time
    folds = []
    for f in json.loads(Path(path).read_text()):
        fold = Fold(parse_time(f["cutoff"]), parse_time(f["end"]), f["candidates"], f["too_active"])
        for w in f["wallets"]:
            fold.wallets.append(FoldWallet(w["wallet"], WalletMetrics(**w["metrics"]), w["alert_rois"]))
        folds.append(fold)
    return folds
