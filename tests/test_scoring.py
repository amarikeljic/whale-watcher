"""Scoring: hand-checked metrics, filters, ranking, and the leakage guarantees."""

from __future__ import annotations

import pytest

from conftest import DAY, act, market
from whalewatcher.scoring import (
    WalletMetrics,
    analyse,
    apply_filters,
    candidate_pool,
    compute_clv,
    load_wallet_rows,
    rank_wallets,
    run_scoring,
)
from whalewatcher.timeutil import parse_time
from world import FakeMarket, World

AS_OF = parse_time("2026-11-01T00:00:00Z")
MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
WINS = [6, 6, 6, 4, 6, 6]   # per month, out of 10 markets


def fixture_wallet(wallet="0xw"):
    """60 markets, 10 resolving on the 15th of each month; $100 at 0.40 five days before.

    Hand calculation: a win is 250 * 1 - 100 = +150, a loss -100.
    6 wins: 900 - 400 = +500; 4 wins: 600 - 600 = 0 (not profitable).
    total = 5 * 500 = 2500, cost 6000, roi 0.41667, win rate 34/60, max share 150/2500.
    """
    rows, markets = [], {}
    for mi, month in enumerate(MONTHS):
        resolved = parse_time(f"{month}-15T12:00:00Z")
        for k in range(10):
            cond = f"0x{mi}{k:02d}"
            win = k < WINS[mi]
            markets[cond] = market(cond, resolved_at=resolved, payouts=[1.0, 0.0] if win else [0.0, 1.0])
            rows.append(act(resolved - 5 * DAY, cond=cond, size=250, price=0.4, usdc=100, wallet=wallet))
    return rows, markets


def test_metrics_match_the_hand_calculation(cfg):
    rows, markets = fixture_wallet()
    m = analyse("0xw", rows, markets, AS_OF, cfg.scoring).metrics
    assert m.settled_markets == 60
    assert m.total_pnl == pytest.approx(2500)
    assert m.total_cost == pytest.approx(6000)
    assert m.roi == pytest.approx(2500 / 6000)
    assert m.profitable_months == 5 and m.months_in_window == 6
    assert m.max_single_market_share == pytest.approx(150 / 2500)
    assert m.avg_stake_usd == pytest.approx(100)
    assert m.win_rate == pytest.approx(34 / 60)
    assert m.two_sided_ratio == 0
    assert m.days_since_last_trade == pytest.approx((AS_OF - parse_time("2026-10-10T12:00:00Z")) / DAY)
    assert apply_filters(m, cfg.scoring) == []


def test_future_rows_and_future_resolutions_change_nothing(cfg):
    rows, markets = fixture_wallet()
    before = analyse("0xw", rows, markets, AS_OF, cfg.scoring).metrics
    # A huge winner that resolves two days AFTER as_of, with its payout already known today...
    markets["0xlate"] = market("0xlate", resolved_at=AS_OF + 2 * DAY, payouts=[1.0, 0.0])
    rows.append(act(AS_OF - 3 * DAY, cond="0xlate", size=100_000, price=0.01, usdc=1000))
    # ...and trades placed after as_of in markets that are already settled.
    rows.append(act(AS_OF + DAY, cond="0x000", size=5000, price=0.5))
    after = analyse("0xw", rows, markets, AS_OF, cfg.scoring).metrics
    for field in ("settled_markets", "total_pnl", "total_cost", "roi", "profitable_months", "win_rate",
                  "max_single_market_share"):
        assert getattr(after, field) == getattr(before, field), field


def test_markets_resolved_before_the_window_do_not_count(cfg):
    rows, markets = fixture_wallet()
    markets["0xold"] = market("0xold", resolved_at=parse_time("2026-04-20T00:00:00Z"))
    rows.append(act(parse_time("2026-04-10T00:00:00Z"), cond="0xold", size=1000, price=0.1))
    assert analyse("0xw", rows, markets, AS_OF, cfg.scoring).metrics.settled_markets == 60


def test_unreliable_markets_are_excluded_and_counted(cfg):
    rows, markets = fixture_wallet()
    rows.append(act(parse_time("2026-06-14T00:00:00Z"), "CONVERSION", cond="0x100", size=20, usdc=160, index=None))
    m = analyse("0xw", rows, markets, AS_OF, cfg.scoring).metrics
    assert m.settled_markets == 59 and m.unreliable_markets == 1


def test_each_filter_fires(cfg):
    s = cfg.scoring
    base = dict(settled_markets=60, total_pnl=2500, total_cost=6000, roi=0.4, profitable_months=5,
                months_in_window=6, max_single_market_share=0.1, avg_stake_usd=100, days_since_last_trade=3,
                two_sided_ratio=0.1)
    assert apply_filters(WalletMetrics("w", AS_OF, **base), s) == []
    for change, word in [({"settled_markets": 49}, "settled_markets"), ({"profitable_months": 3}, "profitable_months"),
                         ({"max_single_market_share": 0.31}, "max_single_market_share"),
                         ({"avg_stake_usd": 99}, "avg_stake_usd"), ({"days_since_last_trade": 31}, "days_since"),
                         ({"two_sided_ratio": 0.41}, "two_sided"), ({"roi": 0.04}, "roi")]:
        reasons = apply_filters(WalletMetrics("w", AS_OF, **{**base, **change}), s)
        assert len(reasons) == 1 and word in reasons[0], (change, reasons)
    assert "too_active" in apply_filters(WalletMetrics("w", AS_OF, truncated=True), s)[0]


def test_ranking_by_clv_needs_enough_positions_and_marks_top_k(cfg):
    cfg.scoring.top_k = 1
    a = WalletMetrics("a", AS_OF, passed=True, clv=0.02, clv_n=40, roi=0.1)
    b = WalletMetrics("b", AS_OF, passed=True, clv=0.05, clv_n=40, roi=0.1)
    c = WalletMetrics("c", AS_OF, passed=True, clv=0.09, clv_n=3, roi=0.5)
    ordered = rank_wallets([a, b, c], cfg.scoring)
    assert [m.wallet for m in ordered[:2]] == ["b", "a"]
    assert b.selected and not a.selected and not c.passed
    assert "clv_positions" in c.fail_reasons[0]


async def test_clv_uses_the_pre_game_price_for_sports_and_close_otherwise(app, fake_http):
    world = World(fake_http)
    start = parse_time("2026-10-10T23:00:00Z")
    resolved = start + 4 * 3600
    markets = {
        "0xs": market("0xs", resolved_at=resolved, game_start=start, category="sports"),
        "0xp": market("0xp", resolved_at=resolved, closed_time=resolved),
    }
    rows = [act(start - 2 * DAY, cond="0xs", size=100, price=0.40),
            act(start - 2 * DAY, cond="0xp", size=100, price=0.40)]
    asked = []

    def price(token, at):
        asked.append((token, at))
        return {"0xs-t0": 0.55, "0xp-t0": 0.999}[token]

    world.price_fn = price
    a = analyse("0xw", rows, markets, AS_OF, app.cfg.scoring)
    clv, n, markout, mn = await compute_clv(a.clv_positions, app.prices, AS_OF, 24)
    assert ("0xs-t0", start - 1) in asked, "sports close = last price before the start"
    assert ("0xp-t0", resolved - 1) in asked
    assert n == 2 and clv == pytest.approx(((0.55 - 0.40) + (0.999 - 0.40)) / 2)
    assert mn == 2


async def test_markouts_never_look_past_as_of(app, fake_http):
    world = World(fake_http)
    resolved = AS_OF - 3600
    markets = {"0xp": market("0xp", resolved_at=resolved)}
    rows = [act(AS_OF - 7200, cond="0xp", size=100, price=0.4)]   # 24h later is after as_of
    world.price_fn = lambda token, at: 0.6
    a = analyse("0xw", rows, markets, AS_OF, app.cfg.scoring)
    _, _, markout, mn = await compute_clv(a.clv_positions, app.prices, AS_OF, 24)
    assert markout is None and mn == 0


async def test_wallet_rows_are_cached_and_only_new_rows_fetched(app, fake_http):
    world = World(fake_http)
    world.trade("0xw", AS_OF - 10 * DAY, "0xc1")
    rows, truncated = await load_wallet_rows(app, "0xw", AS_OF - 100 * DAY, AS_OF, max_rows=20000)
    assert len(rows) == 1 and not truncated
    calls = fake_http.count("/v2/activity")
    rows, _ = await load_wallet_rows(app, "0xw", AS_OF - 100 * DAY, AS_OF, max_rows=20000)
    assert fake_http.count("/v2/activity") == calls, "covered range is served from the database"
    world.trade("0xw", AS_OF + DAY, "0xc2")
    rows, _ = await load_wallet_rows(app, "0xw", AS_OF - 100 * DAY, AS_OF + 2 * DAY, max_rows=20000)
    assert len(rows) == 2
    start = fake_http.calls[-1][2]["start"]
    assert start >= AS_OF - 3600, "only the new stretch is fetched"


async def test_hyperactive_wallets_are_cut_off_after_one_page(app, fake_http):
    world = World(fake_http)
    for i in range(1500):   # 1500 fills in the last hour of a year-long window
        world.trade("0xbot", AS_OF - i, "0xc1", size=1, price=0.5)
    rows, truncated = await load_wallet_rows(app, "0xbot", AS_OF - 365 * DAY, AS_OF, max_rows=20000)
    assert truncated and rows == []
    assert fake_http.count("/v2/activity") == 1


async def test_historical_pool_never_reads_todays_leaderboard(app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xm1", resolved_at=AS_OF - 5 * DAY))
    world.trade("0xearly", AS_OF - 10 * DAY, "0xm1", size=5000, price=0.5)
    world.trade("0xlate", AS_OF + 10 * DAY, "0xm1", size=5000, price=0.5)
    pool, _ = await candidate_pool(app, AS_OF, historical=True)
    assert "0xearly" in pool and "0xlate" not in pool, "only trades before as_of choose candidates"
    assert fake_http.count("/v2/leaderboard") == 0


async def test_live_pool_warns_when_as_of_is_in_the_past(app, fake_http):
    World(fake_http).leaderboard = []
    _, notes = await candidate_pool(app, AS_OF - 30 * DAY, historical=False)
    assert any("leaderboards describe today" in n for n in notes)


async def test_end_to_end_score_run_stores_a_snapshot(app, fake_http):
    world = World(fake_http)
    rows, markets = fixture_wallet("0xgood")
    for cond, m in markets.items():
        world.add_market(FakeMarket(cond, resolved_at=m.resolved_at, payouts=m.payouts))
    for r in rows:
        world.trade("0xgood", r.ts, r.condition_id, size=r.size, price=r.price, usdc=r.usdc_size)
    world.price_fn = lambda token, at: 0.55
    run = await run_scoring(app, AS_OF, pool={"0xgood": {"test"}})
    assert run.run_id is not None
    good = run.metrics[0]
    assert good.passed and good.selected and good.rank == 1
    assert good.clv == pytest.approx(0.15) and good.clv_n == 60
    stored = app.db.one("SELECT * FROM wallet_score_snapshots WHERE run_id = ?", (run.run_id,))
    assert stored["as_of"] == AS_OF and stored["selected"] == 1 and stored["settled_markets"] == 60
    assert app.db.one("SELECT as_of FROM score_runs WHERE id = ?", (run.run_id,))["as_of"] == AS_OF


async def test_the_pool_cap_keeps_the_biggest_traders_not_the_first_addresses(app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xm1", resolved_at=AS_OF - 5 * DAY))
    world.trade("0x0000small", AS_OF - 10 * DAY, "0xm1", size=2000, price=0.5)     # $1,000
    world.trade("0xffffbig", AS_OF - 10 * DAY, "0xm1", size=200_000, price=0.5)    # $100,000
    app.cfg.candidates.max_candidates = 1
    pool, notes = await candidate_pool(app, AS_OF, historical=True)
    assert list(pool) == ["0xffffbig"]
    assert any("capped at 1 of 2" in n for n in notes)


async def test_a_multi_page_wallet_is_fetched_once_page_by_page(app, fake_http):
    world = World(fake_http)
    for i in range(2500):   # 2,500 fills spread over 100 days: three pages, an ordinary active trader
        world.trade("0xbusy", AS_OF - i * 3456, "0xc1", size=1, price=0.5)
    rows, truncated = await load_wallet_rows(app, "0xbusy", AS_OF - 365 * DAY, AS_OF, max_rows=20000)
    assert len(rows) == 2500 and not truncated
    assert fake_http.count("/v2/activity") == 3, "the first page is not fetched twice"


def test_clv_sample_is_capped_and_the_same_every_run():
    from whalewatcher.scoring import ClvPosition, sample_positions
    positions = [ClvPosition(f"0x{i:03d}", f"t{i}", 0.5, 1, 0.5, 10) for i in range(400)]
    a = sample_positions(positions, 150, "0xwallet")
    b = sample_positions(list(reversed(positions)), 150, "0xwallet")
    assert len(a) == 150 and a == b, "deterministic regardless of input order"
    assert sample_positions(positions, 150, "0xother") != a, "different wallets, different samples"
    assert sample_positions(positions, 0, "x") is positions and sample_positions(positions[:10], 150, "x") == positions[:10]


async def test_clv_lookups_respect_the_sample_and_skip_unwanted_metrics(app, fake_http):
    from whalewatcher.scoring import ClvPosition
    world = World(fake_http)
    world.price_fn = lambda token, at: 0.6
    positions = [ClvPosition(f"0x{i:03d}", f"t{i}", 0.5, AS_OF - 30 * DAY, 0.5, AS_OF - DAY) for i in range(300)]
    clv, n, markout, mn = await compute_clv(positions, app.prices, AS_OF, 24, sample=50, seed="w",
                                            want_markout=False)
    assert n == 50 and markout is None and mn == 0
    assert fake_http.count("/v2/prices-history") == 50, "one lookup per sampled position, no markout lookups"
    assert clv == pytest.approx(0.1)
