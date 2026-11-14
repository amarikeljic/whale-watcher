"""Part 4 #3: no hindsight in choosing wallets, and our own record keeps counting.

The end-to-end check the brief asks for: build the same history twice, once as it
looks today and once with everything after the decision date deleted (markets
that closed later still open, later trades gone, later prices missing). Choosing
wallets for that date must give the same answer from both.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager

import pytest

from conftest import DAY, NOW, FakeHttp
from whalewatcher import watchlist as wl
from whalewatcher.app import open_app
from whalewatcher.config import validate
from whalewatcher.db import Database
from whalewatcher.scoring import candidate_pool, run_scoring
from whalewatcher.timeutil import parse_time
from world import FakeMarket, World

AS_OF = parse_time("2026-11-01T00:00:00Z")      # the decision date, six days before NOW
MONTHS = ["2026-08", "2026-09", "2026-10"]


@asynccontextmanager
async def app_on(cfg, http):
    db = Database(":memory:")
    async with open_app(copy.deepcopy(cfg), http=http, db=db, clock=lambda: NOW) as a:
        yield a
    db.close()


def loosen(cfg) -> None:
    """Small numbers, same shape as production (entry stricter than exit)."""
    s = cfg.scoring
    s.min_avg_stake_usd, s.rank_by, s.min_clv_positions = 0, "roi", 1
    cfg.candidates.historical_min_trade_usd = 50
    cfg.candidates.leaderboard_periods = ["month"]
    cfg.candidates.recent_trades_pages = 1
    e, x = cfg.watchlist.entry, cfg.watchlist.exit
    e.min_settled_markets, e.max_single_market_share, e.min_roi = 3, 100.0, 0.05
    x.min_settled_markets, x.max_single_market_share, x.min_roi = 2, 100.0, 0.0
    validate(cfg)


def history(http: FakeHttp, *, known_at: int | None) -> World:
    """The same history, as it looks today (known_at None) or as known at known_at."""
    world = World(http)
    full = known_at is None

    def market(cond: str, closes: int, win: bool) -> None:
        if full or closes <= known_at:
            world.add_market(FakeMarket(cond, resolved_at=closes, end_date=closes,
                                        payouts=[1.0, 0.0] if win else [0.0, 1.0]))
        else:   # had not closed yet: open, no result
            world.add_market(FakeMarket(cond, end_date=closes))

    def buy(wallet: str, ts: int, cond: str, usdc: float = 100) -> None:
        if full or ts < known_at:
            world.trade(wallet, ts, cond, size=usdc / 0.4, price=0.4, usdc=usdc)

    # A steady wallet: ten bets a month that settled before the decision date.
    for mi, month in enumerate(MONTHS):
        closes = parse_time(f"{month}-15T12:00:00Z")
        for k in range(10):
            cond = f"0xs{mi}{k:02d}"
            market(cond, closes, win=k < 6)
            buy("0xsteady", closes - 5 * DAY, cond)
    # A lucky wallet: a thin settled record, plus bets bought before the date in
    # markets that settled after it, all of which won. Only hindsight sees them win.
    for k in range(4):
        cond = f"0xl{k}"
        market(cond, AS_OF - 20 * DAY, win=k < 2)
        buy("0xlucky", AS_OF - 25 * DAY, cond)
    for k in range(12):
        cond = f"0xlater{k}"
        market(cond, AS_OF + 3 * DAY, win=True)
        buy("0xlucky", AS_OF - 2 * DAY, cond)
        buy("0xpeeker", AS_OF - 2 * DAY, cond, usdc=5000)    # only ever in markets that closed later
        buy("0xcrowd", AS_OF + DAY, cond, usdc=50_000)        # the rush after the date
    # A wallet that started after the date.
    for k in range(10):
        cond = f"0xnew{k}"
        market(cond, AS_OF + 4 * DAY, win=True)
        buy("0xfuture", AS_OF + DAY, cond, usdc=2000)
    world.leaderboard = [{"rank": 1, "user_id": "0xfuture", "pnl": 99_999, "volume": 1, "user_name": "today"}]
    world.price_fn = (lambda token, at: 0.55) if full else (lambda token, at: 0.55 if at <= known_at else None)
    return world


def summary(run) -> list[tuple]:
    return [(m.wallet, m.passed, m.selected, m.rank, m.settled_markets, round(m.roi or 0, 9), m.clv_n)
            for m in run.metrics]


async def test_deleting_everything_after_the_date_leaves_the_pool_and_the_scores_unchanged(cfg):
    loosen(cfg)
    results = []
    for known_at in (None, AS_OF):
        http = FakeHttp()
        history(http, known_at=known_at)
        async with app_on(cfg, http) as app:
            pool, _ = await candidate_pool(app, AS_OF, historical=True)
            run = await run_scoring(app, AS_OF, pool=pool, save=False)
            results.append((pool, summary(run)))
    (pool_today, scores_today), (pool_then, scores_then) = results
    assert pool_today == pool_then
    assert scores_today == scores_then
    assert set(pool_today) == {"0xsteady", "0xlucky"}, "a wallet seen only in markets still open at the date is not"
    lucky = next(s for s in scores_today if s[0] == "0xlucky")
    assert lucky[4] == 4, "bets that settled after the date do not count"


async def test_a_review_for_a_past_date_decides_the_same_without_the_later_data(cfg):
    loosen(cfg)
    reviews = []
    for known_at in (None, AS_OF):
        http = FakeHttp()
        history(http, known_at=known_at)
        async with app_on(cfg, http) as app:
            review = await wl.nightly(app, as_of=AS_OF)
            reviews.append({d.wallet: (d.action, d.detail) for d in review.decisions})
            assert http.count("/v2/leaderboard") == 0, "today's leaderboard says who did well after the date"
            assert http.count("/v2/user-stats") == 0, "lifetime figures as of today"
            assert any("review for a past date" in n for n in review.run.notes)
    assert reviews[0] == reviews[1]
    assert reviews[0]["0xsteady"][0] == wl.ADD and "0xfuture" not in reviews[0] and "0xpeeker" not in reviews[0]


async def test_a_review_for_today_still_reads_the_leaderboard_and_the_market_maker_check(cfg):
    loosen(cfg)
    http = FakeHttp()
    world = history(http, known_at=None)
    world.user_stats = {}
    async with app_on(cfg, http) as app:
        await wl.nightly(app)
    assert http.count("/v2/leaderboard") >= 1 and http.count("/v2/user-stats") >= 1


# -- our own record ------------------------------------------------------------------------


def own(app, wallet: str, n: int, *, clv: float, closed_at: int, runner: str = "single",
        strategy: str = "flat_hold", inplay: bool = False, start: int = 0) -> None:
    """n settled bot copies of this wallet, one event each."""
    for i in range(start, start + n):
        cond = f"0xown{wallet[2:6]}{runner}{strategy}{i}"
        app.db.execute("INSERT INTO poly_markets (condition_id, event_slug, game_start_time) VALUES (?,?,?)",
                       (cond, f"ev-{cond}", closed_at - 7200 if inplay else None))
        app.db.execute(
            "INSERT INTO bot_trades (runner, strategy, wallet, condition_id, token_id, event_key, status, signal_ts, "
            "detected_at, decided_at, stake_usd, fill_price, contracts, closed_at, closing_price, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (runner, strategy, wallet, cond, f"{cond}-t0", f"ev-{cond}", "FILLED", closed_at - 3600,
             closed_at - 3600, closed_at - 3600, 10, 0.50, 20, closed_at, 0.50 + clv, closed_at))


async def test_our_own_record_counts_the_bot_copies_and_only_up_to_the_date(app):
    own(app, "0xaaaa", 3, clv=-0.04, closed_at=NOW - 10 * DAY)
    own(app, "0xaaaa", 2, clv=-0.10, closed_at=NOW - DAY, start=3)
    own(app, "0xaaaa", 5, clv=+0.30, closed_at=NOW - DAY, runner="consensus")           # another runner
    own(app, "0xaaaa", 5, clv=+0.30, closed_at=NOW - DAY, strategy="flat_mirror")       # a comparison strategy
    own(app, "0xaaaa", 5, clv=+0.30, closed_at=NOW - DAY, inplay=True, start=50)        # in-play: no closing line
    events, clv = wl.live_record(app, "0xaaaa")
    assert events == 5 and clv == pytest.approx((3 * -0.04 + 2 * -0.10) / 5)
    assert wl.live_record(app, "0xaaaa", NOW - 5 * DAY) == (3, pytest.approx(-0.04)), "nothing settled later"


async def test_the_own_record_rule_fires_on_bot_copies(app):
    """Before 2026-11-14 the rule read only the old rule's table, which nothing has
    written to since the bot model started, so it could never fire again."""
    x = app.cfg.watchlist.exit
    x.live_min_events = 5
    own(app, "0xaaaa", 5, clv=-0.05, closed_at=NOW - DAY)
    fails = wl.live_failures(app, "0xaaaa", x)
    assert fails and "live CLV -0.050 over 5 settled events" in fails[0]
    assert wl.live_failures(app, "0xaaaa", x, NOW - 2 * DAY) == [], "a review for an earlier date saw none"
