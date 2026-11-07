"""The requirement search: same filter semantics as scoring, honest objective, finds real
signal, and does not invent signal from noise."""

from __future__ import annotations

import math
import random

import pytest

from conftest import DAY, act, market
from whalewatcher.ledger import Buy
from whalewatcher.optimize import (
    GRID,
    SPEC_REQUIREMENTS,
    Fold,
    FoldIndex,
    FoldWallet,
    alert_prices,
    copy_roi,
    evaluate,
    holdout_alert_rois,
    passes_all,
    permutation_p,
    score_from_sums,
    score_set,
)
from whalewatcher.config import ScoringConfig
from whalewatcher.scoring import WalletMetrics, apply_filters
from whalewatcher.timing import Stopwatch

AS_OF = 1_800_000_000


def metrics(**kw) -> WalletMetrics:
    base = dict(settled_markets=60, total_pnl=2500, total_cost=6000, roi=0.4, profitable_months=5,
                months_in_window=6, max_single_market_share=0.1, avg_stake_usd=100, days_since_last_trade=3,
                two_sided_ratio=0.1, win_rate=0.55)
    base.update(kw)
    return WalletMetrics("w", AS_OF, **base)


def test_the_spec_requirements_filter_exactly_like_scoring():
    rng = random.Random(3)
    spec, scoring = SPEC_REQUIREMENTS, ScoringConfig()   # the dataclass defaults are the spec's values
    for _ in range(500):
        m = metrics(settled_markets=rng.choice([10, 49, 50, 80]), profitable_months=rng.randint(0, 6),
                    roi=rng.choice([None, -0.1, 0.05, 0.3]), max_single_market_share=rng.choice([None, 0.1, 0.3, 0.9]),
                    avg_stake_usd=rng.choice([None, 50, 100, 900]), days_since_last_trade=rng.choice([None, 2, 30, 45]),
                    two_sided_ratio=rng.choice([None, 0.1, 0.4, 0.7]))
        assert passes_all(m, spec) == (apply_filters(m, scoring) == []), m


def b(ts, usd, price):
    return Buy(ts, 0, "t", usd / price, price, usd)


def test_alert_groups_follow_the_monitor_rules():
    buys = [b(0, 200, 0.40), b(100, 400, 0.42),        # one group, $600: alerts
            b(1000, 300, 0.50),                         # alone, $300: no alert
            b(2000, 450, 0.60), b(2299, 100, 0.62)]     # one group (within 300 s of its first fill), $550
    prices = alert_prices(buys, window=300, min_usd=500)
    assert prices == [pytest.approx(600 / (200 / 0.40 + 400 / 0.42)), pytest.approx(550 / (450 / 0.6 + 100 / 0.62))]


def test_copy_roi_pays_the_taker_fee():
    m = market(fee_rate=0.05)
    assert copy_roi(0.40, 1.0, m) == pytest.approx(1 / (0.40 + 0.05 * 0.24) - 1)
    assert copy_roi(0.40, 0.0, m) == pytest.approx(-1.0)
    assert copy_roi(0.40, 0.5, market()) == pytest.approx(0.25), "a 50-50 void pays half, fee-free market"


def test_holdout_outcomes_count_only_alert_worthy_buys_on_markets_settled_inside_the_holdout():
    start, end = AS_OF, AS_OF + 60 * DAY
    markets = {
        "0xin": market("0xin", resolved_at=start + 10 * DAY, payouts=[1.0, 0.0]),
        "0xlate": market("0xlate", resolved_at=end + DAY, payouts=[1.0, 0.0]),
        "0xband": market("0xband", resolved_at=start + 10 * DAY, payouts=[1.0, 0.0]),
    }
    rows = [act(start - DAY, cond="0xin", size=2000, price=0.40, usdc=800),      # before the holdout
            act(start + DAY, cond="0xin", size=2000, price=0.40, usdc=800),      # counts
            act(start + DAY, cond="0xlate", size=2000, price=0.40, usdc=800),    # resolves after the holdout
            act(start + DAY, cond="0xband", size=1000, price=0.97, usdc=970),    # outside the price band
            act(start + 2 * DAY, cond="0xin", size=100, price=0.40, usdc=40)]    # too small to alert
    rois = holdout_alert_rois(rows, markets, start, end, window=300, min_usd=500, min_price=0.05, max_price=0.95)
    assert rois == [pytest.approx(1 / 0.40 - 1)]


def test_cluster_robust_score_matches_a_direct_computation():
    groups = [[0.5, -1.0, 0.2], [-1.0], [0.1, 0.1, 0.1, 0.3], [2.0, -1.0]]
    S = [sum(g) for g in groups]
    N = [len(g) for g in groups]
    sums = (len(groups), sum(N), sum(S), sum(s * s for s in S), sum(s * n for s, n in zip(S, N, strict=True)),
            sum(n * n for n in N))
    sc = score_from_sums(*sums)
    mean = sum(S) / sum(N)
    se = math.sqrt(len(groups) / (len(groups) - 1) * sum((s - mean * n) ** 2 for s, n in zip(S, N, strict=True))) / sum(N)
    assert sc.mean == pytest.approx(mean) and sc.se == pytest.approx(se)
    assert sc.lcb == pytest.approx(mean - 1.96 * se)
    assert score_from_sums(1, 3, 1.0, 1.0, 3.0, 9.0).se is None, "one wallet: no spread to estimate"


LOOSE = {k: None for k in GRID} | {"min_settled_markets": 10, "min_profitable_months": 0, "min_avg_stake_usd": 0,
                                   "max_days_since_last_trade": 30}


def synthetic_fold(cutoff: int, rng: random.Random, *, signal: bool, n: int = 120) -> Fold:
    """Wallets with varied metrics. With ``signal``, wallets whose selection ROI is
    at least 0.1 really are better in the holdout; otherwise outcomes are noise."""
    fold = Fold(cutoff, cutoff + 60 * DAY, n, 0)
    for i in range(n):
        roi = rng.uniform(-0.3, 0.4)
        m = WalletMetrics(f"0x{i:03d}", cutoff, settled_markets=rng.randint(10, 150), total_pnl=100,
                          total_cost=1000, roi=roi, profitable_months=rng.randint(0, 6), months_in_window=6,
                          max_single_market_share=rng.uniform(0.05, 1.2), avg_stake_usd=rng.uniform(20, 3000),
                          days_since_last_trade=rng.uniform(0, 40), two_sided_ratio=rng.uniform(0, 0.6),
                          win_rate=rng.uniform(0.3, 0.8), clv=rng.uniform(-0.05, 0.05), clv_n=20)
        edge = (0.25 if roi >= 0.1 else -0.25) if signal else 0.0
        rois = [edge + rng.gauss(0, 0.5) for _ in range(rng.randint(3, 15))]
        fold.wallets.append(FoldWallet(m.wallet, m, rois))
    return fold


def test_search_finds_a_planted_signal_and_the_unseen_fold_confirms_it():
    rng = random.Random(11)
    folds = [synthetic_fold(AS_OF + k * 60 * DAY, rng, signal=True) for k in range(3)]
    rep = evaluate(folds, min_wallets=10, min_alerts=50, spec=LOOSE, watch=Stopwatch(None), samples=500)
    for step in rep.steps:
        assert step.chosen.req["min_roi"] in (0.1, 0.2), step.chosen.req
        assert step.chosen.complexity <= 2, "the one-standard-error rule keeps the set simple"
        chosen = step.results[0]
        assert chosen.score.mean > 0.1 and chosen.ci[0] > 0
        assert chosen.p_random is not None and chosen.p_random < 0.01
        assert step.transfer is not None and step.transfer > 0.3
    assert rep.final.req["min_roi"] in (0.1, 0.2)
    assert "made money on later folds" in rep.verdict


def test_search_on_noise_does_not_claim_an_edge_out_of_sample():
    rng = random.Random(5)
    folds = [synthetic_fold(AS_OF + k * 60 * DAY, rng, signal=False) for k in range(3)]
    rep = evaluate(folds, min_wallets=10, min_alerts=50, spec=LOOSE, watch=Stopwatch(None), samples=500)
    assert rep.steps, "a search ran"
    for step in rep.steps:
        chosen = step.results[0]
        assert chosen.p_random is None or chosen.p_random >= 0.05, "noise must not beat random out of sample"
        assert step.chosen.train.mean > chosen.score.mean, "the training folds flatter the chosen set"
    assert "made money on later folds" not in rep.verdict


def test_permutation_p_value():
    rng = random.Random(2)
    fold = synthetic_fold(AS_OF, rng, signal=True)
    idx = FoldIndex(fold)
    best = frozenset(i for i, w in enumerate(fold.wallets) if w.m.roi >= 0.1)
    worst = frozenset(i for i, w in enumerate(fold.wallets) if w.m.roi < 0.1)
    assert permutation_p(idx, best) < 0.01
    assert permutation_p(idx, worst) > 0.9
    assert permutation_p(idx, idx.all) is None, "all wallets is not a selection"
    assert score_set(idx, best).mean > score_set(idx, idx.all).mean


def test_an_empty_fold_is_reported_not_judged():
    rng = random.Random(4)
    good = synthetic_fold(AS_OF, rng, signal=True)
    empty = Fold(AS_OF + 60 * DAY, AS_OF + 120 * DAY, 400, 0,
                 [FoldWallet(f"0xe{i}", WalletMetrics(f"0xe{i}", AS_OF, settled_markets=0), []) for i in range(400)])
    rep = evaluate([good, empty, synthetic_fold(AS_OF + 120 * DAY, rng, signal=True)], min_wallets=10,
                   min_alerts=50, spec=LOOSE, watch=Stopwatch(None), samples=200)
    assert rep.verdict.startswith("Inconclusive"), rep.verdict
    assert any("has 0 wallets with alerts" in note for note in rep.skipped)


def regime_fold(cutoff: int, rng: random.Random, b_edge: float) -> Fold:
    """Low-concentration wallets earn +5% in every period; high win-rate wallets earn
    ``b_edge``, which flips sign in one period."""
    fold = Fold(cutoff, cutoff + 60 * DAY, 300, 0)
    for i in range(300):
        share = rng.uniform(0.05, 1.2)
        # The two effects live in different wallets: low-concentration wallets never
        # have the high win rate, so the steady rule is not contaminated by the other.
        win = rng.uniform(0.3, 0.59) if share <= 0.2 else rng.uniform(0.3, 0.8)
        m = WalletMetrics(f"0x{i:03d}", cutoff, settled_markets=60, total_pnl=100, total_cost=1000, roi=0.1,
                          profitable_months=4, months_in_window=6, max_single_market_share=share,
                          avg_stake_usd=500, days_since_last_trade=2, two_sided_ratio=0.1, win_rate=win)
        edge = (0.05 if share <= 0.2 else -0.05) + (b_edge if win >= 0.6 else 0.0)
        fold.wallets.append(FoldWallet(m.wallet, m, [edge + rng.gauss(0, 0.2) for _ in range(10)]))
    return fold


def test_the_worst_period_objective_prefers_the_rule_that_never_loses():
    rng = random.Random(9)
    folds = [regime_fold(AS_OF + k * 60 * DAY, rng, b) for k, b in enumerate((0.40, -0.30, 0.40))]
    pooled = evaluate(folds, min_wallets=10, min_alerts=50, spec=LOOSE, watch=Stopwatch(None), samples=200,
                      objective="pooled")
    worst = evaluate(folds, min_wallets=10, min_alerts=50, spec=LOOSE, watch=Stopwatch(None), samples=200,
                     objective="worst")
    assert pooled.final.req["min_win_rate"] == 0.6, "pooled chases the high average"
    assert worst.final.req["min_win_rate"] is None and worst.final.req["max_single_market_share"] == 0.2
    assert min(s.mean for s in worst.final_per_fold) > 0
