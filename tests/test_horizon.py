"""v2.1 Part 3: the time-to-close cutoff. Under, over and exactly at the limit, a
market with no end date, and a market that resolves later than it was scheduled to."""

from __future__ import annotations

import pytest

from conftest import DAY, NOW
from test_bot import COND, WHALE, YES, one, trades
from whalewatcher.bot import Bot
from whalewatcher.config import ConfigError, FiltersConfig
from whalewatcher.config import load as load_config
from whalewatcher.horizon import BUCKETS, NO_END_DATE, TOO_LONG, bucket, close_skip, days_to_close, scheduled_end
from whalewatcher.monitor import Monitor
from whalewatcher.paper import PaperEngine
from whalewatcher.stats import cutoff_report, load_bot_trades, render
from world import FakeMarket, World

RULE = FiltersConfig(max_days_to_close=14, skip_if_no_end_date=True)


# -- the rule itself ---------------------------------------------------------------------


def test_under_the_limit_is_allowed():
    assert close_skip(NOW + 3 * DAY, NOW, RULE) is None
    assert days_to_close(NOW + 3 * DAY, NOW) == 3.0


def test_over_the_limit_is_skipped_and_the_reason_carries_the_days():
    code, reason = close_skip(NOW + 21 * DAY + DAY // 4, NOW, RULE)
    assert code == TOO_LONG and reason == "too_long: closes in 21.2 days (limit 14)"
    assert close_skip(NOW + 14 * DAY + 1, NOW, RULE)[0] == TOO_LONG, "one second over is over"


def test_exactly_at_the_limit_is_allowed():
    assert close_skip(NOW + 14 * DAY, NOW, RULE) is None


def test_no_end_date_is_skipped_unless_the_setting_says_otherwise():
    assert close_skip(None, NOW, RULE) == (NO_END_DATE, "no_end_date: the market has no scheduled end date")
    assert close_skip(None, NOW, FiltersConfig(max_days_to_close=14, skip_if_no_end_date=False)) is None


def test_a_market_past_its_scheduled_end_is_zero_days_away_not_negative():
    assert days_to_close(NOW - 3600, NOW) == 0.0, "a game in play: Gamma's endDate is the kickoff"
    assert close_skip(NOW - 3600, NOW, RULE) is None


def test_a_game_market_is_scheduled_by_its_start_when_the_end_date_trails_it():
    game, end = NOW + DAY, NOW + 20 * DAY
    assert scheduled_end(end, game) == game, "1 sports market in 10 carries an end date days after the game"
    assert scheduled_end(end, None) == end and scheduled_end(None, game) == game and scheduled_end(None, None) is None
    assert close_skip(scheduled_end(end, game), NOW, RULE) is None, "tomorrow's game is not a 20-day position"


def test_the_limit_can_be_switched_off():
    assert close_skip(NOW + 400 * DAY, NOW, FiltersConfig(max_days_to_close=None)) is None


def test_holding_time_bands():
    assert [bucket(d) for d in (0, 0.99, 1, 6.99, 7, 14, 14.01, 90)] == [
        BUCKETS[0], BUCKETS[0], BUCKETS[1], BUCKETS[1], BUCKETS[2], BUCKETS[2], BUCKETS[3], BUCKETS[3]]
    assert bucket(None) == "no end date"


# -- configuration -------------------------------------------------------------------------


def test_the_shipped_config_sets_14_days_and_skips_markets_with_no_end_date():
    cfg = load_config("config.yaml", env={})
    assert cfg.filters.max_days_to_close == 14 and cfg.filters.skip_if_no_end_date is True


def test_null_switches_the_limit_off_and_nonsense_is_refused(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("filters:\n  max_days_to_close: null\n")
    assert load_config(str(p), env={}).filters.max_days_to_close is None
    p.write_text("filters:\n  max_days_to_close: 0\n")
    with pytest.raises(ConfigError, match="max_days_to_close"):
        load_config(str(p), env={})
    p.write_text("filters:\n  max_days: 3\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(str(p), env={})


# -- the bot ---------------------------------------------------------------------------------


def setup(app, fake_http, end_date):
    app.cfg.filters.max_days_to_close, app.cfg.filters.skip_if_no_end_date = 14.0, True
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=end_date, question="Will A beat B?"))
    world.set_book(YES, [(0.44, 1000), (0.45, 1000)], [(0.42, 500)])
    app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                   "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    bot = Bot(app)
    return world, Monitor(app, paper=PaperEngine(app), bot=bot), bot


async def signal(world, monitor):
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    return await monitor.poll_once()


async def test_a_market_ending_too_far_away_is_skipped_by_every_strategy(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, NOW + 21 * DAY)
    assert (await signal(world, monitor)).alerts == 1, "the alert is still posted: only the paper trade is skipped"
    rows = trades(live_app)
    assert {r["strategy"]: r["status"] for r in rows} == {
        "flat_hold": "SKIPPED", "flat_mirror": "SKIPPED", "scaled_hold": "SKIPPED", "placebo_late": "SKIPPED"}, \
        "the placebo faces the same rule as the strategies it is a control for"
    t = rows[0]
    assert t["skip_code"] == "too_long" and t["reason"] == "too_long: closes in 21.0 days (limit 14)"
    assert t["scheduled_end"] == NOW + 21 * DAY and t["days_to_close"] == 21.0
    assert t["fill_price"] is None and t["cost"] is None and t["zero_price"] is None, "no fill, no comparison"


async def test_a_market_ending_exactly_at_the_limit_is_traded(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, NOW + 14 * DAY)
    await signal(world, monitor)
    t = one(live_app, "flat_hold")
    assert t["status"] == "FILLED" and t["skip_code"] is None and t["days_to_close"] == 14.0


async def test_a_market_ending_soon_is_traded_and_its_schedule_recorded(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, NOW + 3 * DAY)
    await signal(world, monitor)
    t = one(live_app, "flat_hold")
    assert t["status"] == "FILLED" and (t["scheduled_end"], t["days_to_close"]) == (NOW + 3 * DAY, 3.0)
    assert one(live_app, "placebo_late")["status"] == "PENDING"


async def test_a_market_with_no_end_date_is_skipped(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, None)
    await signal(world, monitor)
    t = one(live_app, "flat_hold")
    assert t["status"] == "SKIPPED" and t["skip_code"] == "no_end_date"
    assert t["scheduled_end"] is None and t["days_to_close"] is None


async def test_no_end_date_is_traded_when_the_setting_allows_it(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, None)
    live_app.cfg.filters.skip_if_no_end_date = False
    await signal(world, monitor)
    assert one(live_app, "flat_hold")["status"] == "FILLED"


# -- resolving later than scheduled ------------------------------------------------------------


async def test_a_market_that_resolves_later_than_scheduled_is_recorded_and_reported(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, NOW + 2 * DAY)      # scheduled: 2 days away, allowed
    await signal(world, monitor)
    assert one(live_app, "flat_hold")["resolved_at"] is None

    resolved = NOW + 20 * DAY                                            # reality: 20 days
    world.markets[COND].resolved_at, world.markets[COND].payouts = resolved, [1.0, 0.0]
    clock.advance(21 * DAY)
    await bot.settle()
    t = one(live_app, "flat_hold")
    assert t["resolved_at"] == resolved and t["result"] == "WIN", "the real resolution time, kept on the trade"

    (row,) = [r for r in load_bot_trades(live_app) if r["strategy"] == "flat_hold"]
    assert row["days_to_close"] == 2.0 and row["held_days"] == pytest.approx(20.0)
    assert row["late_days"] == pytest.approx(18.0)
    assert (bucket(row["days_to_close"]), bucket(row["held_days"])) == ("1 to 7 days", "over 14 days")

    rep = cutoff_report(live_app, [row])
    assert (rep.closed, rep.held_over, rep.resolved, rep.late) == (1, 1, 1, 1)
    assert rep.would_skip == 0, "it was inside the limit when entered"
    text = render(live_app)
    assert "of 1 closed flat_hold trades, 1 (100%) were held longer than 14 days" in text
    assert "of 1 whose market resolved, 1 (100%) resolved more than a day after the scheduled end" in text


async def test_the_summary_counts_what_the_cutoff_skipped(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, NOW + 40 * DAY)
    await signal(world, monitor)
    text = render(live_app)
    assert "single: time-to-close cutoff (14 days): 1 signals skipped (too_long 1, no_end_date 0)" in text
    rep = cutoff_report(live_app, [r for r in load_bot_trades(live_app) if r["strategy"] == "flat_hold"])
    assert (rep.too_long, rep.no_end_date) == (1, 0)


async def test_trades_made_before_the_cutoff_stay_and_are_counted_as_ones_it_would_refuse(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, NOW + 60 * DAY)
    live_app.cfg.filters.max_days_to_close = None                        # the rule did not exist yet
    await signal(world, monitor)
    assert one(live_app, "flat_hold")["status"] == "FILLED"
    live_app.cfg.filters.max_days_to_close = 14.0                        # now it does
    rep = cutoff_report(live_app, [r for r in load_bot_trades(live_app) if r["strategy"] == "flat_hold"])
    assert rep.would_skip == 1 and rep.too_long == 0
    assert live_app.db.one("SELECT COUNT(*) n FROM bot_trades WHERE status = 'FILLED'")["n"] == 3, "nothing rewritten"


# -- the backtests ---------------------------------------------------------------------------


async def test_the_walk_forward_backtest_applies_the_cutoff(app, fake_http):
    from test_backtest import T0, W, spec_fold
    from whalewatcher.backtest import run_backtest
    app.cfg.filters.max_days_to_close, app.cfg.filters.skip_if_no_end_date = 14.0, True
    world = World(fake_http)
    world.add_market(FakeMarket("0xa", end_date=T0 + 10 * DAY, resolved_at=T0 + 20 * DAY, payouts=[1.0, 0.0]))
    world.add_market(FakeMarket("0xb", end_date=T0 + 60 * DAY))                       # 58 days away
    world.add_market(FakeMarket("0xn", resolved_at=T0 + 20 * DAY, payouts=[1.0, 0.0]))  # no end date at all
    world.trade(W, T0 + DAY, "0xa", size=1500, price=0.40)                            # 9 days away: traded
    world.trade(W, T0 + 2 * DAY, "0xb", size=1400, price=0.50)
    world.trade(W, T0 + 3 * DAY, "0xn", size=1400, price=0.50)
    world.clob_history = lambda token, s, e: [(s - 30, 0.10), (s + 30, 0.41)]

    report = await run_backtest(app, [spec_fold(T0)])
    assert [t.event.condition_id for t in report.trades] == ["0xa"]
    assert report.skipped[("backtest:spec", "time-to-close cutoff (too_long)")] == 1
    assert report.skipped[("backtest:spec", "time-to-close cutoff (no_end_date)")] == 1
    (row,) = [r for r in __import__("whalewatcher.stats", fromlist=["x"]).load_trades(app)
              if r["strategy"] == "backtest:spec"]
    assert row["days_to_close"] == pytest.approx(9.0, abs=0.01), "scheduled: 9 days from the simulated fill"
    assert row["held_days"] == pytest.approx(19.0, abs=0.01) and row["late_days"] == pytest.approx(10.0), \
        "it resolved 10 days after its scheduled end"


def test_the_consensus_backtest_report_says_what_the_cutoff_removed():
    from test_consensus import POOL, RULES, T0
    from whalewatcher import consensus as cs
    g = cs.GroupStats("x", trades=40, filled=35, missed=5, settled=32, events=20, pnl=12.5, cost=320.0, roi=0.039,
                      mean_roi=0.04, ci=(-0.05, 0.13), win_rate=0.55, drawdown=30.0)
    v = cs.VariantResult("100k", pool_sizes=[5], entity_counts=[5], consensus=g, single=g, random=g,
                         paired=(30, -0.01, -0.08, 0.06))
    rep = cs.ConsensusReport(T0, T0 + 180 * DAY, 700, 400, RULES, POOL, variants=[v], max_days_to_close=14)
    assert "time-to-close cutoff" not in cs.describe(rep), "nothing removed, nothing said"
    rep.cutoff_skipped.update({"consensus:100k": 7, "single:100k": 90, "random:100k": 12})
    assert ("not traded, time-to-close cutoff (over 14 days to close or no end date): 7 signals, 90 single entries, "
            "12 random draws") in cs.describe(rep)
