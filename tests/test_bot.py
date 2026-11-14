"""v2 Phase 3: the bot model. Book walking, fees, missed trades, mirror exits, bankroll
and exposure limits, the comparison columns, the placebo, and that the old paper trades
stay where they are under their old label."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import DAY, NOW
from whalewatcher.bot import Bot, walk_bids
from whalewatcher.db import SCHEMA_VERSION, Database
from whalewatcher.monitor import Monitor
from whalewatcher.paper import PaperEngine, walk_asks
from whalewatcher.polymarket import Book
from whalewatcher.stats import load_bot_trades, render
from world import FakeMarket, World

WHALE, OTHER = "0xwhale", "0xother"
COND = "0xm"
YES = f"{COND}-t0"


def book_of(asks=(), bids=()) -> Book:
    return Book.from_api({"asks": [{"price": str(p), "size": str(s)} for p, s in asks],
                          "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                          "min_order_size": "5"}, YES)


def setup(app, fake_http, *, asks=((0.44, 1000), (0.45, 1000)), bids=((0.42, 500),), fee_rate=0.0,
          wallets=(WHALE,), game_start=None):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 30 * DAY, question="Will A beat B?", fee_rate=fee_rate,
                                game_start=game_start))
    world.set_book(YES, list(asks), list(bids))
    for i, w in enumerate(wallets, start=1):
        app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                       "VALUES (?, 1, ?, 1, ?, ?)", (w, i, NOW - 3600, NOW - 120))
    bot = Bot(app)
    monitor = Monitor(app, paper=PaperEngine(app), bot=bot)
    return world, monitor, bot


def trades(app, strategy=None):
    sql = "SELECT * FROM bot_trades" + (" WHERE strategy = ?" if strategy else "") + " ORDER BY id"
    return app.db.query(sql, (strategy,) if strategy else ())


def one(app, strategy):
    (t,) = trades(app, strategy)
    return t


# -- book walking ------------------------------------------------------------------------


def test_walking_the_asks_pays_each_level_in_turn():
    book = book_of(asks=[(0.50, 10), (0.52, 100)])
    walk = walk_asks(book, 20.0)                       # $5 at 0.50, then $15 at 0.52
    assert walk.levels == 2 and walk.fills[0] == (0.50, 10)
    assert walk.contracts == pytest.approx(10 + 15 / 0.52) and walk.vwap == pytest.approx(20 / walk.contracts)
    assert walk_asks(book, 20.0, limit_price=0.51) is None, "the second level is above the limit"
    assert walk_asks(book, 5.0, limit_price=0.51).levels == 1
    assert walk_asks(book, 100.0) is None, "the book holds $57 in all"


def test_walking_the_bids_sells_into_each_level_in_turn():
    book = book_of(bids=[(0.60, 10), (0.58, 100)])
    walk = walk_bids(book, 30)                         # 10 shares at 0.60, 20 at 0.58
    assert walk.fills == [(0.60, 10), (0.58, 20)] and walk.vwap == pytest.approx((6 + 11.6) / 30)
    assert walk_bids(book, 30, floor_price=0.59) is None
    assert walk_bids(book, 200) is None, "only 110 shares are bid for"


# -- a signal becomes a bot fill, seconds after the whale ---------------------------------------


async def test_a_whale_buy_is_filled_from_the_book_at_detection(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    stats = await monitor.poll_once()
    assert stats.alerts == 1

    t = one(live_app, "flat_hold")
    assert t["status"] == "FILLED" and t["runner"] == "single" and t["wallet"] == WHALE
    assert (t["fill_price"], t["stake_usd"], t["levels"]) == (0.44, 10.0, 1)
    # Shares come in 0.01 steps (precision.py): 10 / 0.44 = 22.727 buys 22.72 shares for $9.9968.
    assert t["contracts"] == 22.72 and t["cost"] == pytest.approx(0.44 * 22.72)
    assert (t["signal_ts"], t["detected_at"], t["decided_at"]) == (NOW - 3, float(NOW), float(NOW))
    assert (t["whale_price"], t["best_ask"]) == (0.42, 0.44)
    alert = live_app.db.one("SELECT * FROM alerts")
    assert alert["detect_delay"] == 3.0, "the whale's fill to our detector, per alert"
    assert "Paper bot: bought $10 at 0.440, 3s after the whale" in alert["message"]
    assert live_app.db.one("SELECT COUNT(*) n FROM paper_trades")["n"] == 0, "no new old-rule trades"
    assert {r["strategy"]: r["status"] for r in trades(live_app)} == {
        "flat_hold": "FILLED", "flat_mirror": "FILLED", "scaled_hold": "FILLED", "placebo_late": "PENDING"}


async def test_fees_are_charged_per_level_on_the_way_in(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 10), (0.52, 1000)], fee_rate=0.05)
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    t = one(live_app, "flat_hold")
    second = 9.61          # (10 - 5) / 0.52 = 9.615 shares, rounded down to a 0.01 step
    expected = round(10 * 0.05 * 0.5 * 0.5, 5) + round(second * 0.05 * 0.52 * 0.48, 5)
    assert t["levels"] == 2 and t["fees"] == pytest.approx(expected)
    assert t["cost"] == pytest.approx(5 + 0.52 * second + expected), "cost = the shares bought + taker fees"


# -- missed -------------------------------------------------------------------------------


async def test_a_price_that_already_ran_is_a_miss_not_a_chase(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 1000)])
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)     # limit = 0.42 + 0.03
    await monitor.poll_once()
    t = one(live_app, "flat_hold")
    assert t["status"] == "MISSED" and t["reason"].startswith("price ran: filling $10 costs 0.500 a share, limit 0.450")
    assert t["fill_price"] == 0.50 and t["cost"] is None and t["contracts"] is None
    assert "Paper bot: missed (price ran" in live_app.db.one("SELECT message FROM alerts")["message"]
    assert bot.account("flat_hold").open_cost == 0


async def test_a_thin_book_is_a_miss(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.44, 5)])
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    assert (one(live_app, "flat_hold")["status"], one(live_app, "flat_hold")["reason"]) == \
        ("MISSED", "book too thin for $10")


async def test_a_signal_seen_too_late_is_a_miss_for_the_bot(live_app, fake_http, clock):
    """An outage or a slow feed: the alert still goes out, but a fill two minutes after
    the whale is not the strategy being tested."""
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 120, COND, size=1500, price=0.42, usdc=630)       # only the backstop sees it
    await monitor.poll_once()
    assert live_app.db.one("SELECT status FROM alerts")["status"] == "RECORDED"
    t = one(live_app, "flat_hold")
    assert t["status"] == "MISSED" and t["reason"] == "too late: 120s after the whale's fill (limit 30s)"
    assert t["human_due"] is None, "no comparison for a signal nobody could have acted on in time"


# -- bankroll and exposure limits -------------------------------------------------------------


async def buy(world, monitor, clock, cond=COND, *, wallet=WHALE, usdc=630.0, price=0.42):
    clock.advance(400)                                  # a new buy group each time
    world.trade(wallet, clock.now - 3, cond, size=usdc / price, price=price, usdc=usdc, feed=True)
    await monitor.poll_once()


async def test_market_exposure_is_capped_and_the_skip_is_recorded_with_its_reason(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    live_app.cfg.bot.flat_hold.max_market_exposure_usd = 25
    for _ in range(3):
        await buy(world, monitor, clock)
    statuses = [(t["status"], t["reason"]) for t in trades(live_app, "flat_hold")]
    assert statuses[:2] == [("FILLED", None), ("FILLED", None)]
    assert statuses[2] == ("SKIPPED", "market exposure: $19.99 open in this market, limit $25")
    assert bot.exposure("flat_hold", "condition_id", COND) == pytest.approx(2 * 0.44 * 22.72), "two $9.9968 fills"


async def test_event_exposure_counts_every_market_of_the_event(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, wallets=(WHALE, OTHER))
    live_app.cfg.bot.flat_hold.max_event_exposure_usd = 15
    world.markets[COND].event = "big-game"
    world.add_market(FakeMarket("0xn", end_date=NOW + 30 * DAY, event="big-game"))      # same game, another market
    world.set_book("0xn-t0", [(0.44, 1000)], [(0.42, 500)])
    await buy(world, monitor, clock)
    await buy(world, monitor, clock, "0xn", wallet=OTHER)
    first, second = trades(live_app, "flat_hold")
    assert first["status"] == "FILLED" and first["event_key"] == second["event_key"] == "big-game"
    assert (second["status"], second["reason"]) == ("SKIPPED", "event exposure: $10.00 open in this event, limit $15")


async def test_the_bankroll_runs_out_and_each_strategy_has_its_own(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    cfg = live_app.cfg.bot
    cfg.flat_hold.starting_bankroll, cfg.flat_hold.max_market_exposure_usd = 15, 1000
    await buy(world, monitor, clock)
    await buy(world, monitor, clock)
    first, second = trades(live_app, "flat_hold")
    assert (second["status"], second["reason"]) == ("SKIPPED", "bankroll: $5.00 free, stake $10.00")
    acct = bot.account("flat_hold")
    assert (acct.starting, acct.open_cost, acct.cash) == (15, pytest.approx(9.9968), pytest.approx(5.0032))
    mirror = bot.account("flat_mirror")
    assert mirror.cash == pytest.approx(1000 - 2 * 9.9968), "flat_mirror's account is untouched by flat_hold's"
    assert [t["status"] for t in trades(live_app, "flat_mirror")] == ["FILLED", "FILLED"]


async def test_a_daily_loss_pauses_new_entries_until_the_next_utc_day(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    live_app.cfg.bot.flat_hold.daily_loss_limit_usd = 8
    await buy(world, monitor, clock)
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [0.0, 1.0]     # YES loses
    clock.advance(700)
    assert await bot.settle() >= 1 and bot.account("flat_hold").today == pytest.approx(-9.9968)

    world.add_market(FakeMarket("0xn", end_date=NOW + 30 * DAY))
    world.set_book("0xn-t0", [(0.44, 1000)], [(0.42, 500)])
    await buy(world, monitor, clock, "0xn")
    paused = trades(live_app, "flat_hold")[-1]
    assert paused["status"] == "SKIPPED" and paused["reason"].startswith("daily loss limit: lost $10.00 today (limit $8)")

    clock.advance(DAY)                                  # a new UTC day
    world.add_market(FakeMarket("0xo", end_date=NOW + 30 * DAY))
    world.set_book("0xo-t0", [(0.44, 1000)], [(0.42, 500)])
    await buy(world, monitor, clock, "0xo")
    assert trades(live_app, "flat_hold")[-1]["status"] == "FILLED"


async def test_scaled_hold_sizes_with_the_whale_up_to_its_cap(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    assert bot.stake_for("scaled_hold", 500) == 10 and bot.stake_for("scaled_hold", 1500) == 30
    assert bot.stake_for("scaled_hold", 50_000) == 50, "capped at max_stake_usd"
    assert bot.stake_for("flat_hold", 50_000) == 10
    await buy(world, monitor, clock, usdc=1500)
    assert one(live_app, "scaled_hold")["stake_usd"] == 30 and one(live_app, "flat_hold")["stake_usd"] == 10


# -- mirror exits -----------------------------------------------------------------------------


async def test_flat_mirror_sells_into_the_bids_once_the_whale_has_sold_half(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, bids=[(0.50, 10), (0.48, 500)], fee_rate=0.05)
    world.trade(WHALE, NOW - 3, COND, size=1000, price=0.44, usdc=440, feed=True)       # position: 1000 shares
    world.trade(WHALE, NOW - 3, COND, size=500, price=0.44, usdc=220, feed=True)
    await monitor.poll_once()
    held = one(live_app, "flat_mirror")
    assert held["status"] == "FILLED" and held["whale_shares"] == 1500

    clock.advance(600)
    world.trade(WHALE, clock.now - 2, COND, side="SELL", size=600, price=0.50, feed=True)     # 40% sold: hold
    await monitor.poll_once()
    assert one(live_app, "flat_mirror")["exit_due"] == 0 and one(live_app, "flat_mirror")["whale_sold"] == 600

    clock.advance(600)
    world.trade(WHALE, clock.now - 2, COND, side="SELL", size=200, price=0.50, feed=True)     # 53% sold: exit
    await monitor.poll_once()
    t = one(live_app, "flat_mirror")
    rest = t["contracts"] - 10
    proceeds = 10 * 0.50 + rest * 0.48
    fees = round(10 * 0.05 * 0.25, 5) + round(rest * 0.05 * 0.48 * 0.52, 5)
    assert t["result"] == "EXIT" and t["closed_at"] == clock.now and t["exit_at"] == float(clock.now)
    assert t["proceeds"] == pytest.approx(proceeds) and t["exit_fees"] == pytest.approx(fees)
    assert t["exit_price"] == pytest.approx(proceeds / t["contracts"])
    assert t["pnl"] == pytest.approx(proceeds - fees - t["cost"]), "sell fees come off too"
    assert one(live_app, "flat_hold")["closed_at"] is None, "flat_hold holds to resolution"
    assert bot.account("flat_mirror").open_cost == 0


async def test_an_exit_waits_for_the_bids_and_settles_at_resolution_if_they_never_come(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, bids=[(0.50, 3)])
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.44, usdc=660, feed=True)
    await monitor.poll_once()
    clock.advance(600)
    world.trade(WHALE, clock.now - 2, COND, side="SELL", size=1500, price=0.50, feed=True)
    await monitor.poll_once()
    t = one(live_app, "flat_mirror")
    assert t["exit_due"] == 1 and t["closed_at"] is None and t["exit_tried_at"] == float(clock.now), "3 shares bid"

    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    t = one(live_app, "flat_mirror")
    assert t["result"] == "WIN" and t["pnl"] == pytest.approx(t["contracts"] - t["cost"])


async def test_another_wallets_sell_or_a_sell_before_our_entry_does_not_trigger_an_exit(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, wallets=(WHALE, OTHER))
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.44, usdc=660, feed=True)
    await monitor.poll_once()
    clock.advance(600)
    world.trade(OTHER, clock.now - 2, COND, side="SELL", size=5000, price=0.50, feed=True)
    await monitor.poll_once()
    assert bot.on_whale_sell(WHALE, YES, 5000, NOW - 500) == 0, "that sell is older than our signal"
    assert one(live_app, "flat_mirror")["exit_due"] == 0


# -- the comparison columns, and settlement ------------------------------------------------------


async def test_zero_latency_and_the_old_human_delay_rule_ride_along_as_columns(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    t = one(live_app, "flat_hold")
    assert t["zero_price"] == 0.42 and t["zero_contracts"] == pytest.approx(23.80), "10 / 0.42 in 0.01 steps"
    assert t["human_due"] == NOW + 60 and t["human_done"] == 0 and t["human_price"] is None

    world.set_book(YES, [(0.46, 1000)], [(0.44, 500)])          # a minute later the ask has moved
    clock.advance(60)
    await monitor.poll_once()
    t = one(live_app, "flat_hold")
    assert t["human_done"] == 1 and t["human_price"] == pytest.approx(0.46)
    assert t["human_contracts"] == pytest.approx(21.73), "10 / 0.46 = 21.739, in 0.01 steps"

    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    t = one(live_app, "flat_hold")
    assert t["result"] == "WIN" and t["payout_per_share"] == 1.0 and t["closed_at"] == clock.now
    assert t["pnl"] == pytest.approx(22.72 - 0.44 * 22.72)
    assert t["zero_pnl"] == pytest.approx(23.80 - 0.42 * 23.80) and t["human_pnl"] == pytest.approx(21.73 - 0.46 * 21.73)
    assert t["zero_pnl"] > t["pnl"] > t["human_pnl"], "the whale's price beats the bot's, which beats a minute later"
    assert bot.account("flat_hold").realized == pytest.approx(t["pnl"])
    assert one(live_app, "flat_mirror")["zero_pnl"] is None, "comparison columns are for the hold strategies"


async def test_a_missed_signal_still_settles_its_comparisons_and_counts_as_zero(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 1000)])
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    clock.advance(60)
    await monitor.poll_once()                               # old rule: 0.50 vs best ask 0.50 at the signal: fills
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    t = one(live_app, "flat_hold")
    assert t["status"] == "MISSED" and t["pnl"] == 0.0 and t["result"] is None
    assert t["zero_pnl"] == pytest.approx(23.80 - 0.42 * 23.80) and t["human_pnl"] == pytest.approx(10 / 0.50 - 10)


# -- the placebo -------------------------------------------------------------------------------


async def test_the_placebo_enters_the_same_outcome_later_at_whatever_the_book_says(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    p = one(live_app, "placebo_late")
    cfg = live_app.cfg.bot
    assert p["status"] == "PENDING" and cfg.placebo_min_delay_seconds <= p["due_at"] - NOW <= cfg.placebo_max_delay_seconds

    world.set_book(YES, [(0.60, 1000)], [(0.58, 500)])      # far above the whale's 0.42: the placebo has no anchor
    clock.advance(int(p["due_at"] - NOW) - 5)
    await monitor.poll_once()
    assert one(live_app, "placebo_late")["status"] == "PENDING", "not due yet"
    clock.advance(10)
    await monitor.poll_once()
    p = one(live_app, "placebo_late")
    assert p["status"] == "FILLED" and p["fill_price"] == 0.60 and p["decided_at"] == float(clock.now)
    assert p["token_id"] == YES and p["stake_usd"] == one(live_app, "flat_hold")["stake_usd"]
    assert bot.account("placebo_late").open_cost == pytest.approx(0.60 * 16.66), "10 / 0.60 in 0.01 steps"


# -- restarts, and the old paper trades ------------------------------------------------------------


async def test_a_signal_processed_twice_trades_once(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    live_app.db.set_state(monitor.cursor_key, 0)            # a crash before the cursor was saved
    stats = await monitor.poll_once()
    assert stats.alerts == 0 and len(trades(live_app, "flat_hold")) == 1
    assert live_app.db.one("SELECT COUNT(*) n FROM alerts")["n"] == 1


V2_ROWS = """
INSERT INTO alerts (id, wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, whale_shares, whale_usd,
    whale_price, fills, detected_at, status, snapshot_run_id, updated_at, probation)
    VALUES (41, '0xold', '0xc', '0xc-t0', 'NEW', 100, 100, 10, 600, 0.6, 2, 130, 'POSTED', 6, 130, 1);
INSERT INTO paper_trades (id, alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, alert_ts,
    fill_ts, fill_price, contracts, fees, cost, settled_at, pnl, updated_at)
    VALUES (7, 41, 'polymarket', 'hold_to_resolution', 'FILLED', '0xc-t0', '0xc', 10, 2, 130, 190, 0.62, 16.129, 0, 10,
            500, 6.129, 500);
"""


async def test_upgrading_keeps_the_old_paper_trades_under_their_old_label(tmp_path, cfg, fake_http, clock):
    from whalewatcher.app import open_app
    path = tmp_path / "data" / "whalewatcher.sqlite3"
    v2 = Database(str(path))
    v2.conn.executescript(V2_ROWS)
    for table in ("fill_events", "bot_trades"):
        v2.conn.execute(f"DROP TABLE {table}")
    v2.conn.execute("PRAGMA user_version = 2")               # as Phase 2 left it
    v2.close()

    db = Database(str(path))
    assert db.backup_path and db.backup_path.endswith(f".pre-v{SCHEMA_VERSION}")
    old = db.one("SELECT * FROM paper_trades WHERE id = 7")
    assert (old["strategy"], old["status"], old["pnl"], old["fill_price"]) == ("hold_to_resolution", "FILLED", 6.129, 0.62)
    assert db.one("SELECT probation, detect_delay FROM alerts WHERE id = 41")[:] == (1, None)
    assert db.one("SELECT COUNT(*) n FROM bot_trades")["n"] == 0 and db.one("PRAGMA user_version")[0] == SCHEMA_VERSION
    async with open_app(cfg, http=fake_http, db=db, clock=clock) as app:
        text = render(app, with_breakdowns=False)
        assert "LIVE PAPER TRADES" in text and "polymarket / hold_to_resolution" in text
    db.close()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


async def test_stats_put_the_bot_first_and_keep_the_old_rule_as_its_own_section(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    live_app.db.conn.executescript(V2_ROWS)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    clock.advance(60)
    await monitor.poll_once()
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(2000)
    await monitor.poll_once()                               # the placebo is due
    clock.advance(700)
    await bot.settle()

    text = render(live_app)
    assert text.index("BOT PAPER TRADES") < text.index("OLD RULE, LIVE")
    assert "NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: 1 closed bot paper trades (flat_hold, model 1)" in text
    assert "== single / flat_hold (bot fill at detection, held to resolution; the headline) ==" in text
    assert "signals 1 | filled 1 | missed 0 | skipped 0 | waiting 0 | closed 1 | open 0" in text
    assert "paper bankroll $1,012.72 (started $1,000.00)" in text, "22.72 shares paid $1 for $9.9968"
    assert "n=1 closed" in text and "95% CI" in text, "no profit figure without its n and interval"
    assert "same 1 closed signals, same stake, a miss counted as $0: bot +$12.72 | zero latency" in text
    assert "DETECTION DELAY (whale's fill -> our detector saw it), n=1 alerts: median 3.0s" in text
    assert "1 from the global feed, 0 that only the per-wallet backstop saw" in text
    assert "polymarket / hold_to_resolution" in text and "By watchlist status (flat_hold):" in text
    assert len(load_bot_trades(live_app)) == 4
    from whalewatcher.stats import daily_summary
    from whalewatcher.stats import SUMMARY_LIMIT
    assert len(daily_summary(live_app)) <= SUMMARY_LIMIT
