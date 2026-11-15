"""Part 4 #5 and #6: the price 1, 5 and 30 minutes after each fill, and the order book
read a second time after each decision. Measurements only: neither changes a decision."""

from __future__ import annotations

import pytest

from conftest import NOW
from dashfix import A, small
from test_bot import COND, WHALE, YES, setup, trades
from whalewatcher import dashboard as dash
from whalewatcher.config import Config
from whalewatcher.stats import recheck_report, render


async def filled(live_app, fake_http, clock, **kw):
    world, monitor, bot = setup(live_app, fake_http, **kw)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    head = [t for t in trades(live_app, "flat_hold")]
    assert head and head[0]["status"] == "FILLED"
    return world, monitor, bot, head[0]


async def test_the_price_is_recorded_1_5_and_30_minutes_after_each_fill(live_app, fake_http, clock):
    world, _, bot, t = await filled(live_app, fake_http, clock)
    start = t["decided_at"]
    world.price_fn = lambda token, at: 0.50 if at <= start + 60 else (0.55 if at <= start + 300 else 0.60)
    clock.advance(1800 + 121)
    await bot._markouts(clock.now)
    row = live_app.db.one("SELECT * FROM bot_trades WHERE id = ?", (t["id"],))
    assert (row["markout_1m"], row["markout_price"], row["markout_30m"]) == (0.50, 0.55, 0.60)
    assert row["markout_1m_done"] == row["markout_done"] == row["markout_30m_done"] == 1
    calls = fake_http.count("/v2/prices-history")
    await bot._markouts(clock.now + 60)
    assert fake_http.count("/v2/prices-history") == calls, "each is read once"


async def test_a_price_from_before_our_fill_is_not_a_markout(live_app, fake_http, clock):
    world, _, bot, t = await filled(live_app, fake_http, clock)
    fake_http.route("/v2/prices-history", lambda p: {"data": [{"timestamp": int(p["as_of"]) - 4000, "price": 0.5,
                                                                "resolution_seconds": 60}],
                                                       "pagination": {"next_cursor": None}})
    clock.advance(200)
    await bot._markouts(clock.now)
    row = live_app.db.one("SELECT markout_1m, markout_1m_done FROM bot_trades WHERE id = ?", (t["id"],))
    assert row["markout_1m"] is None and row["markout_1m_done"] == 1, "a quiet market's old price says nothing"


async def test_the_30_minute_price_waits_for_its_time(live_app, fake_http, clock):
    world, _, bot, t = await filled(live_app, fake_http, clock)
    clock.advance(300 + 121)
    await bot._markouts(clock.now)
    row = live_app.db.one("SELECT * FROM bot_trades WHERE id = ?", (t["id"],))
    assert row["markout_done"] == 1 and row["markout_30m_done"] == 0 and row["markout_30m"] is None


async def test_the_book_is_read_again_and_what_the_order_would_have_done_is_recorded(live_app, fake_http, clock):
    world, monitor, bot, t = await filled(live_app, fake_http, clock)
    market = live_app.markets.get(COND)
    world.set_book(YES, [(0.47, 1000)], [(0.42, 500)])      # the ask ran past the limit (0.42 + 0.03)
    live_app.cfg.bot.recheck_seconds = 0
    await bot._recheck(t["alert_id"], market)
    row = live_app.db.one("SELECT * FROM bot_trades WHERE id = ?", (t["id"],))
    assert row["recheck_status"] == "MISSED" and row["recheck_best_ask"] == 0.47 and row["recheck_fill_price"] is None
    assert row["status"] == "FILLED" and row["fill_price"] == pytest.approx(0.44), "the decision itself is untouched"
    r = recheck_report([dict(row)])
    assert (r.n, r.moved, r.up, r.fills, r.lost) == (1, 1, 1, 1, 1) and r.mean_move == pytest.approx(0.03)


async def test_the_second_read_runs_in_the_background_after_the_decision(live_app, fake_http, clock):
    live_app.cfg.bot.recheck_book, live_app.cfg.bot.recheck_seconds = True, 0
    world, monitor, bot, t = await filled(live_app, fake_http, clock)
    await bot.drain()
    row = live_app.db.one("SELECT * FROM bot_trades WHERE id = ?", (t["id"],))
    assert row["recheck_status"] == "FILLED" and row["recheck_fill_price"] == pytest.approx(0.44)
    alert = live_app.db.one("SELECT book_fetch_seconds FROM alerts WHERE id = ?", (t["alert_id"],))
    assert alert["book_fetch_seconds"] is not None and alert["book_fetch_seconds"] >= 0
    text = render(live_app)
    assert "order book read again" in text and "best ask moved on 0 (0 up, 0 down)" in text
    assert "price after our fill, pre-game (1 fills)" in text
    assert "price after our fill by wallet" in text


async def test_a_failed_second_read_is_recorded_and_changes_nothing(live_app, fake_http, clock):
    world, monitor, bot, t = await filled(live_app, fake_http, clock)

    def broken(_params):
        raise RuntimeError("book down")
    fake_http.route("/book", broken)
    live_app.cfg.bot.recheck_seconds = 0
    await bot._recheck(t["alert_id"], live_app.markets.get(COND))
    row = live_app.db.one("SELECT * FROM bot_trades WHERE id = ?", (t["id"],))
    assert row["recheck_status"] == "ERROR" and row["status"] == "FILLED"
    assert recheck_report([dict(row)]).n == 0, "an error is not a measurement"


def test_the_edge_page_shows_the_prices_after_the_fill_and_the_second_read(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    fx = small(path, NOW)
    fx.db.execute("UPDATE bot_trades SET markout_1m = fill_price + 0.01, markout_30m = fill_price - 0.02 "
                  "WHERE status = 'FILLED' AND wallet = ?", (A,))
    fx.db.execute("UPDATE bot_trades SET recheck_at = decided_at + 1.1, recheck_best_ask = best_ask + 0.01, "
                  "recheck_fill_price = fill_price + 0.01, recheck_status = 'FILLED' WHERE status = 'FILLED' "
                  "AND strategy = 'flat_hold'")
    fx.db.close()
    html = dash.render(Config(database_path=path), path, "/edge", {"outages": "include"}, now=NOW)[1]
    assert "Price after our fill" in html and "+0.010 (n=4)" in html and "-0.020 (n=4)" in html
    assert "The order book, a second later" in html and "median 1.1s after the decision" in html


# -- the two cuts the replay points to: had the game started, and had the price already moved -----------


def test_game_state_reads_only_what_is_known_at_the_signal():
    from whalewatcher.stats import IN_PLAY, NOT_SPORTS, PRE_GAME, game_state
    assert game_state({"game_start_time": 1000, "signal_ts": 1000}) == IN_PLAY
    assert game_state({"game_start_time": 1000, "signal_ts": 999}) == PRE_GAME
    assert game_state({"game_start_time": None, "category": "sports", "signal_ts": 5}) == PRE_GAME
    assert game_state({"game_start_time": None, "category": "politics", "alert_ts": 5}) == NOT_SPORTS
    assert game_state({"game_start_time": 1000, "trigger_fill_ts": 1500}) == IN_PLAY, "an old-rule row has no signal_ts"


def test_the_price_move_at_arrival_sorts_signals_into_what_each_limit_buys():
    from whalewatcher.stats import ARRIVAL_ORDER, arrival_move

    def move(ask: float, whale: float = 0.50) -> str:
        return arrival_move({"best_ask": ask, "whale_price": whale})

    assert move(0.49) == move(0.50) == "at or below whale price"
    assert (move(0.51), move(0.52), move(0.53)) == ("up to 1c above", "1 to 2c above", "2 to 3c above")
    assert move(0.531) == move(0.60) == "3 to 10c above", "past the headline's 3 cents, inside chase_hold's 10"
    assert move(0.61) == "over 10c above"
    assert arrival_move({"best_ask": None, "alert_ask": 0.52, "whale_price": 0.5}) == "1 to 2c above"
    assert arrival_move({"best_ask": None, "whale_price": 0.5}) == "no ask recorded"
    assert set(ARRIVAL_ORDER) >= {move(x) for x in (0.4, 0.51, 0.52, 0.53, 0.6, 0.9)}


def test_the_breakdowns_page_and_the_report_carry_both_cuts(tmp_path):
    from conftest import NOW
    from dashfix import small

    from whalewatcher import dashboard as dash
    from whalewatcher.config import Config
    path = str(tmp_path / "ww.sqlite3")
    small(path, NOW).db.close()
    html = dash.render(Config(database_path=path), path, "/breakdowns", {"outages": "include"}, now=NOW)[1]
    assert "By game state" in html and "By price move when we arrived" in html
    assert "1 to 2c above" in html, "the fixture's fills sit 2 cents above the whale"
