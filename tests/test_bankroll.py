"""v2.1 Part 2: the small wallet. Stake sizing, compounding after a win and a loss,
every skip reason, fees coming out of cash, settlement returning cash, and a replay
of a small fixture checked against a hand calculation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from whalewatcher import bankroll as br
from whalewatcher.bankroll import Marks, Params, Signal, run

# No on-ramp cost unless a test is about it: the arithmetic stays readable.
PLAIN = Params(amount=100.0, onramp_cost_pct=0.0)


def sig(n: int, ts: float, price: float = 0.50, *, event: str | None = None, settle: float | None = None,
        payout: float | None = None, status: str = "FILLED", **kw) -> Signal:
    return Signal(id=f"bot:{n}", ts=ts, token_id=f"tok{n}", condition_id=f"c{n}", event_key=event or f"e{n}",
                  base_status=status, price=price, settle_ts=settle, payout=payout, **kw)


# -- stake sizing ---------------------------------------------------------------------------------


def test_the_stake_is_equity_times_the_deployed_share_over_the_positions():
    res = run([sig(1, 100)], PLAIN)
    (e,) = res.entries
    assert e.status == "TAKEN" and e.stake == pytest.approx(8.0), "100 x 0.8 / 10"
    assert e.contracts == pytest.approx(16.0) and e.price == 0.50
    assert res.cash == pytest.approx(92.0) and res.equity == pytest.approx(100.0), "the position is worth what it cost"


def test_the_on_ramp_cost_comes_off_the_starting_amount_once():
    res = run([sig(1, 100)], Params(amount=100.0, onramp_cost_pct=1.5))
    assert res.start_cash == 98.5 and res.onramp_cost == pytest.approx(1.5)
    assert res.entries[0].stake == pytest.approx(7.88), "98.50 x 0.08"
    assert res.total_return == pytest.approx(-0.015), "measured against the $100 put in"


def test_the_stake_is_capped_by_the_cash():
    marks = Marks([("tok1", 150, 0.75)])                                # the first position triples
    p = Params(amount=100.0, onramp_cost_pct=0.0, deploy_fraction=1.0, max_open_positions=4,
               max_per_event_fraction=1.0)
    res = run([sig(1, 100, 0.25), sig(2, 200), sig(3, 300)], p, marks=marks)
    a, b, c = res.entries
    assert a.stake == pytest.approx(25.0) and b.equity == pytest.approx(150.0), "75 cash + 100 shares at 0.75"
    assert b.stake == pytest.approx(37.5) and c.equity == pytest.approx(150.0)
    assert c.stake == pytest.approx(37.5) and res.cash == pytest.approx(0.0), "a quarter of 150, and all that is left"


# -- compounding, settlement, and the hand-checked replay -----------------------------------------------


def fixture() -> list[Signal]:
    return [sig(1, 100, 0.50, settle=1000, payout=1.0),      # wins
            sig(2, 200, 0.40, settle=1500, payout=0.0),      # loses
            sig(3, 1100, 0.50, settle=2000, payout=1.0),     # entered after the win
            sig(4, 1600, 0.50)]                              # entered after the loss, still open


def test_a_replay_matches_the_hand_calculation():
    """$100, 1.5% on-ramp, no fees, stake = 8% of equity, rounded down to the cent.

      start                      cash 98.50                 equity  98.50
      t=100  buy #1 at 0.50      stake 7.88   15.76 shares  cash 90.62
      t=200  buy #2 at 0.40      stake 7.88   19.70 shares  cash 82.74
      t=1000 #1 wins             +15.76                     cash 98.50   equity 106.38
      t=1100 buy #3 at 0.50      stake 8.51   (8% of 106.38 = 8.5104, to the cent) 17.02 shares  cash 89.99
      t=1500 #2 loses            +0                         cash 89.99   equity  98.50
      t=1600 buy #4 at 0.50      stake 7.88   (8% of 98.50) cash 82.11
      t=2000 #3 wins             +17.02                     cash 99.13   equity 107.01
    """
    res = run(fixture(), Params(amount=100.0, onramp_cost_pct=1.5), as_of=3000)
    one, two, three, four = res.entries
    assert [e.status for e in res.entries] == ["TAKEN"] * 4
    assert (one.stake, one.contracts) == (pytest.approx(7.88), pytest.approx(15.76))
    assert (two.stake, two.contracts) == (pytest.approx(7.88), pytest.approx(19.70))
    assert three.equity == pytest.approx(106.38) and three.stake == pytest.approx(8.51), "a win makes the next stake bigger"
    assert four.equity == pytest.approx(98.50) and four.stake == pytest.approx(7.88), "a loss makes it smaller"
    assert (one.proceeds, two.proceeds, three.proceeds, four.proceeds) == (
        pytest.approx(15.76), 0.0, pytest.approx(17.02), None)
    assert (one.pnl, two.pnl, three.pnl) == (pytest.approx(7.88), pytest.approx(-7.88), pytest.approx(8.51))
    assert res.cash == pytest.approx(99.13) and res.equity == pytest.approx(107.01)
    assert res.total_return == pytest.approx(0.0701)
    dd, dd_pct = res.drawdown()
    assert dd == pytest.approx(7.88) and dd_pct == pytest.approx(7.88 / 106.38), "from 106.38 to 98.50 when #2 lost"
    s = res.summary()
    assert (s["taken"], s["settled"], s["wins"], s["open"], s["missed"], s["skipped"]) == (4, 3, 2, 1, 0, {})
    assert [round(p.equity, 4) for p in res.curve] == [98.5, 98.5, 98.5, 106.38, 106.38, 98.5, 98.5, 107.01, 107.01]
    assert res.curve[-1].tied == pytest.approx(7.88), "the open position is the money tied up"


def test_settlement_returns_shares_times_payout_to_cash():
    res = run([sig(1, 100, 0.50, settle=500, payout=1.0)], PLAIN, as_of=600)
    assert res.cash == pytest.approx(108.0) and res.equity == pytest.approx(108.0), "92 + 16 shares x $1"
    res = run([sig(1, 100, 0.50, settle=500, payout=0.5)], PLAIN, as_of=600)
    assert res.cash == pytest.approx(100.0), "a void pays 50 cents a share"


def test_nothing_after_as_of_is_used_and_the_past_never_changes():
    early = run(fixture(), Params(amount=100.0, onramp_cost_pct=1.5), as_of=1050)
    assert [e.signal.id for e in early.entries] == ["bot:1", "bot:2"]
    assert early.equity == pytest.approx(106.38) and early.entries[1].proceeds is None, "#2 had not settled yet"
    full = run(fixture(), Params(amount=100.0, onramp_cost_pct=1.5), as_of=3000)
    assert [(e.stake, e.equity) for e in full.entries[:2]] == [(e.stake, e.equity) for e in early.entries]
    unknown = run([sig(1, 100, 0.50, settle=5000, payout=1.0)], PLAIN, as_of=600)
    assert unknown.cash == pytest.approx(92.0), "a result that came in after as_of is the future"


def test_a_run_counts_only_signals_from_its_start():
    res = run(fixture(), PLAIN, start=1050, as_of=3000)
    assert [e.signal.id for e in res.entries] == ["bot:3", "bot:4"] and res.entries[0].stake == pytest.approx(8.0)


# -- fees ---------------------------------------------------------------------------------------------


def test_the_fee_comes_out_of_the_stake_so_cash_falls_by_what_was_spent():
    """8.00 all-in at 0.50 with a 0.05 rate: a share costs 0.50 + 0.05 x 0.25 = 0.5125,
    so 15.6098 shares, 15.60 in 0.01 steps (precision.py). They cost 7.80 and the fee is
    15.60 x 0.0125 = 0.195: 7.995 spent, half a cent of the stake left in cash."""
    res = run([sig(1, 100, 0.50, settle=500, payout=1.0, fee_rate=0.05)], PLAIN, as_of=600)
    (e,) = res.entries
    assert e.stake == pytest.approx(7.995) and e.contracts == pytest.approx(15.60)
    assert e.fee == pytest.approx(0.195, abs=1e-5) and res.fees == e.fee
    assert res.curve[1].cash == pytest.approx(92.005), "cash fell by what was spent, fee included"
    assert e.pnl == pytest.approx(15.60 - 7.995), "the fee is why this is 7.61 and not 8.00"
    free = run([sig(1, 100, 0.50, settle=500, payout=1.0)], PLAIN, as_of=600)
    assert free.equity - res.equity == pytest.approx((92.0 + 16.0) - (92.005 + 15.60)), "the fee bought fewer shares"


# -- every skip reason --------------------------------------------------------------------------------


def test_no_slot_when_the_positions_are_all_open():
    p = Params(amount=100.0, onramp_cost_pct=0.0, max_open_positions=2, max_per_event_fraction=0.5)
    res = run([sig(1, 100), sig(2, 200), sig(3, 300, settle=350, payout=1.0), sig(4, 400)], p)
    assert [e.status for e in res.entries] == ["TAKEN", "TAKEN", "SKIPPED", "SKIPPED"]
    assert res.entries[2].code == "no_slot" and res.entries[2].reason == "no_slot: 2 positions are open (limit 2)"
    freed = run([sig(1, 100, settle=250, payout=0.0), sig(2, 200), sig(3, 300)], p)
    assert freed.entries[2].status == "TAKEN", "a settled position gives its slot back"


def test_no_cash_when_less_than_the_smallest_order_is_free():
    marks = Marks([("tok1", 150, 0.75)])
    p = Params(amount=100.0, onramp_cost_pct=0.0, deploy_fraction=1.0, max_open_positions=4,
               max_per_event_fraction=1.0)
    res = run([sig(1, 100, 0.25), sig(2, 200), sig(3, 300), sig(4, 400)], p, marks=marks)
    last = res.entries[3]
    assert last.open_positions == 3 and last.status == "SKIPPED" and last.code == "no_cash"
    assert last.reason == "no_cash: $0.00 free, the smallest order is $1"


def test_below_minimum_in_dollars():
    p = Params(amount=90.0, onramp_cost_pct=0.0, deploy_fraction=1.0, max_open_positions=3, min_order_usd=25.0,
               max_per_event_fraction=1.0)
    res = run([sig(1, 100, settle=150, payout=0.0), sig(2, 200)], p)       # 30 staked and lost: 60 left
    e = res.entries[1]
    assert e.code == "below_minimum" and e.reason == "below_minimum: the stake would be $20.00, the smallest order is $25"


def test_below_minimum_in_shares_the_markets_own_rule():
    res = run([sig(1, 100, 0.90)], Params(amount=50.0, onramp_cost_pct=0.0))      # $4 buys 4.44 shares
    (e,) = res.entries
    assert e.status == "SKIPPED" and e.code == "below_minimum"
    assert e.reason == "below_minimum: $4.00 buys 4.44 shares at 0.900, the market's smallest order is 5"
    assert run([sig(1, 100, 0.90, min_shares=1.0)], Params(amount=50.0, onramp_cost_pct=0.0)).entries[0].status == "TAKEN"


def test_event_cap_when_one_event_would_hold_too_much():
    res = run([sig(1, 100, event="game"), sig(2, 200, event="game"), sig(3, 300, event="game"), sig(4, 400)], PLAIN)
    assert [e.status for e in res.entries] == ["TAKEN", "TAKEN", "SKIPPED", "TAKEN"]
    third = res.entries[2]
    assert third.code == "event_cap", "8 + 8 open, a third 8 would pass 20% of 100"
    assert third.reason == "event_cap: $16.00 is open in this event, a $8.00 stake would pass $20.00"


def test_the_time_to_close_cutoff_applies_only_to_a_run_that_has_it():
    day = 86_400
    far, none_, near = sig(1, 100, scheduled_end=100 + 30 * day), sig(2, 200), sig(3, 300, scheduled_end=300 + day)
    with_cutoff = run([far, none_, near], Params(amount=100.0, onramp_cost_pct=0.0, max_days_to_close=14,
                                                 skip_if_no_end_date=True))
    assert [(e.status, e.code) for e in with_cutoff.entries] == [
        ("SKIPPED", "too_long"), ("SKIPPED", "no_end_date"), ("TAKEN", None)]
    assert with_cutoff.entries[0].reason == "too_long: closes in 30.0 days (limit 14)"
    assert with_cutoff.skipped == {"too_long": 1, "no_end_date": 1}
    assert [e.status for e in run([far, none_, near], PLAIN).entries] == ["TAKEN"] * 3


def test_an_order_that_did_not_fill_is_a_miss_not_a_skip():
    res = run([sig(1, 100, status="MISSED", base_reason="price ran: filling $10 costs 0.560 a share, limit 0.510"),
               sig(2, 200, None, status="SKIPPED")], PLAIN)
    assert [(e.status, e.code) for e in res.entries] == [("MISSED", None), ("MISSED", None)]
    assert res.entries[1].reason == "no price was recorded for this signal"
    assert res.missed == 2 and res.cash == 100.0


# -- pricing an order of any size from the stored book -------------------------------------------------


def test_with_the_book_stored_the_stake_is_walked_through_its_levels():
    book = [(0.50, 10.0), (0.52, 100.0)]
    res = run([sig(1, 100, 0.50, asks=book, limit_price=0.53)], PLAIN)
    (e,) = res.entries
    assert e.contracts == pytest.approx(15.76), "$5 at 0.50, then the other $3 at 0.52: 15.769, in 0.01 steps"
    assert e.price == pytest.approx((5 + 0.52 * 5.76) / 15.76) and e.optimistic is False
    ran = run([sig(1, 100, 0.50, asks=book, limit_price=0.51)], PLAIN).entries[0]
    assert ran.status == "MISSED" and ran.reason.startswith("price ran: $8.00 does not fill at or below the limit 0.510")
    thin = run([sig(1, 100, 0.50, asks=[(0.50, 4.0)], limit_price=0.53)], PLAIN).entries[0]
    assert thin.status == "MISSED" and thin.reason == "book too thin for $8.00"


def test_a_big_wallet_misses_what_a_small_one_fills():
    book = [(0.50, 30.0), (0.51, 30.0)]                                  # $30.30 of depth inside the limit
    small = run([sig(1, 100, 0.50, asks=book, limit_price=0.53)], PLAIN).entries[0]
    big = run([sig(1, 100, 0.50, asks=book, limit_price=0.53)], Params(amount=10_000.0, onramp_cost_pct=0.0)).entries[0]
    assert small.status == "TAKEN" and big.status == "MISSED" and big.reason == "book too thin for $800.00"


def test_a_missed_ten_dollar_order_is_walked_again_at_our_own_stake_when_the_book_was_stored():
    s = sig(1, 100, None, status="MISSED", base_reason="book too thin for $10", asks=[(0.50, 17.0)], limit_price=0.53)
    assert run([s], PLAIN).entries[0].status == "TAKEN", "$8.50 of depth: too thin for $10, enough for $8"
    late = sig(2, 100, None, status="MISSED", base_reason="too late: 45s after the whale's fill (limit 30s)",
               asks=[(0.50, 100.0)], limit_price=0.53)
    assert run([late], PLAIN).entries[0].status == "MISSED", "being late has nothing to do with the stake"


def test_without_the_book_a_larger_stake_is_flagged_as_optimistic():
    small = run([sig(1, 100, 0.50)], PLAIN)
    big = run([sig(1, 100, 0.50)], Params(amount=1000.0, onramp_cost_pct=0.0))
    assert small.optimistic == 0, "$8 pays no more than the $10 order did"
    assert big.optimistic == 1 and big.entries[0].optimistic, "$80 priced at what $10 paid"
    never = run([sig(1, 100, 0.50, status="SKIPPED")], PLAIN)
    assert never.optimistic == 1, "priced at the best ask: no order ever tested the depth"


# -- the amount, the settings table, and runs ---------------------------------------------------------


def test_the_amount_must_be_a_positive_dollar_amount_up_to_100k():
    assert br.parse_amount("250") == 250.0 and br.parse_amount(" $1,250.50 ") == 1250.5
    assert br.parse_amount(100_000) == 100_000.0 and br.parse_amount("0.01") == 0.01
    for bad, why in (("0", "more than \\$0"), ("-5", "more than \\$0"), ("abc", "must be a number"),
                     ("", "must be a number"), ("nan", "must be a number"), ("inf", "must be a number"),
                     ("100000.01", "cannot be more than \\$100,000"), ("1.234", "at most two decimals"),
                     ("1e9", "cannot be more than")):
        with pytest.raises(ValueError, match=why):
            br.parse_amount(bad)


def test_the_amount_defaults_to_100_and_is_kept_in_settings(live_app):
    app = live_app
    assert br.wallet_amount(app.db) == 100.0
    br.save_amount(app.db, 250, "cli", 5)
    assert br.wallet_amount(app.db) == 250.0
    row = app.db.one("SELECT * FROM settings")
    assert (row["key"], row["value"], row["source"], row["updated_at"]) == ("wallet_amount", "250.00", "cli", 5)
    app.db.execute("UPDATE settings SET value = 'garbage'")
    assert br.wallet_amount(app.db) == 100.0, "a value nobody could have saved falls back to the default"
    with pytest.raises(ValueError):
        br.save_amount(app.db, 0, "cli", 6)


def test_changing_the_amount_ends_the_run_and_starts_a_new_one_and_old_runs_are_frozen(live_app, clock):
    app = live_app
    assert br.sync_runs(app, ["single"]) == [
        "single: started run #1 with $100.00, fixed stake of 8.0% of equity, up to 10 positions, 20% per event, "
        "$1 minimum, 1.5% on-ramp"]
    assert br.sync_runs(app, ["single"]) == [], "nothing changed, nothing happens"
    first = br.open_run(app.db, "single")
    signals = {"bot:single": [sig(1, first["started_at"] + 10, 0.50, settle=first["started_at"] + 20, payout=1.0)]}

    clock.advance(3600)
    br.save_amount(app.db, 250, "dashboard", clock.now)
    changes = br.sync_runs(app, ["single"], signals)
    assert changes[0] == "single: ended run #1 (wallet amount changed from $100.00 to $250.00)"
    assert changes[1].startswith("single: started run #2 with $250.00")
    old = app.db.one("SELECT * FROM bankroll_runs WHERE id = 1")
    frozen = json.loads(old["summary_json"])
    assert old["ended_at"] == clock.now and frozen["taken"] == 1 and frozen["equity"] == pytest.approx(98.5 + 7.88)
    assert br.open_run(app.db, "single")["amount"] == 250.0

    with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
        app.db.execute("UPDATE bankroll_runs SET amount = 1 WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        app.db.execute("DELETE FROM bankroll_runs WHERE id = 1")
    assert app.db.one("SELECT COUNT(*) n FROM bankroll_runs")["n"] == 2


def test_changing_a_bankroll_setting_also_starts_a_new_run(live_app, clock):
    app = live_app
    br.sync_runs(app, ["single"])
    app.cfg.bankroll.max_open_positions = 5
    changes = br.sync_runs(app, ["single"])
    assert changes[0] == "single: ended run #1 (bankroll settings changed)" and "up to 5 positions" in changes[1]
    app.cfg.filters.max_days_to_close = 7
    assert br.sync_runs(app, ["single"]) == [], "the cutoff is not a bankroll setting"


def test_a_stake_that_could_never_fit_under_the_event_cap_is_refused_as_configuration(tmp_path):
    from whalewatcher.config import ConfigError, load
    p = tmp_path / "c.yaml"
    p.write_text("bankroll:\n  max_open_positions: 2\n")           # every stake would be 40% of equity, cap 20%
    with pytest.raises(ConfigError, match="no trade could ever be taken"):
        load(str(p), env={})
    p.write_text("bankroll:\n  deploy_fraction: 0.5\n  max_open_positions: 5\n  max_per_event_fraction: 0.1\n")
    cfg = load(str(p), env={})
    assert (cfg.bankroll.deploy_fraction, cfg.bankroll.max_open_positions) == (0.5, 5)
    shipped = load("config.yaml", env={}).bankroll
    assert (shipped.deploy_fraction, shipped.max_open_positions, shipped.min_order_usd,
            shipped.max_per_event_fraction) == (0.8, 10, 1.0, 0.2)


# -- wired into the bot: what a signal stores, marks, skipped signals' results --------------------------


async def whale_buy(live_app, fake_http, **kw):
    from test_bot import COND, WHALE, setup
    world, monitor, bot = setup(live_app, fake_http, **kw)
    world.trade(WHALE, live_app.clock() - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    return world, monitor, bot


def wallet_of(app, *, cutoff: bool = True, **kw):
    from whalewatcher.stats import load_bot_trades
    signals = br.signals_by_source(load_bot_trades(app), app.cfg)["bot:single"]
    return run(signals, Params.of(app.cfg, 100.0, cutoff=cutoff), marks=br.load_marks(app.db),
               as_of=app.clock() + 1, **kw)


async def test_a_signal_stores_the_asks_the_limit_and_the_markets_minimum(live_app, fake_http, clock):
    from test_bot import one
    await whale_buy(live_app, fake_http, asks=((0.44, 12), (0.45, 1000), (0.60, 50)))
    t = one(live_app, "flat_hold")
    assert json.loads(t["asks_json"]) == [[0.44, 12.0], [0.45, 1000.0], [0.6, 50.0]], "best first, as seen"
    assert t["limit_price"] == pytest.approx(0.45) and t["min_order_size"] == 5.0
    assert one(live_app, "placebo_late")["limit_price"] is None, "the placebo has no whale price to anchor to"


async def test_the_wallet_prices_its_own_stake_from_the_stored_book(live_app, fake_http, clock):
    await whale_buy(live_app, fake_http, asks=((0.44, 12), (0.45, 1000)))
    res = wallet_of(live_app)
    (e,) = res.entries
    # 12 shares at 0.44, the rest of $7.88 at 0.45: 17.778 shares, 17.77 in 0.01 steps, for $7.8765
    assert e.status == "TAKEN" and e.stake == pytest.approx(12 * 0.44 + 5.77 * 0.45) and e.optimistic is False
    assert e.contracts == pytest.approx(17.77)
    big = run(br.signals_by_source(__import__("whalewatcher.stats", fromlist=["x"]).load_bot_trades(live_app),
                                   live_app.cfg)["bot:single"], Params.of(live_app.cfg, 100_000.0))
    assert big.entries[0].status == "MISSED" and big.entries[0].reason == "book too thin for $7,880.00", \
        "$455 of depth inside the limit: a $100,000 wallet cannot copy this"


async def test_open_positions_are_marked_at_the_best_bid_every_few_minutes(live_app, fake_http, clock):
    from test_bot import YES
    world, _, bot = await whale_buy(live_app, fake_http)
    assert live_app.db.one("SELECT COUNT(*) n FROM position_marks")["n"] == 1, "marked on the first tick after the fill"
    assert (await bot.tick()).get("marks") is None, "and not again until the interval has passed"
    at_cost = wallet_of(live_app)
    shares, spent = at_cost.entries[0].contracts, at_cost.entries[0].stake
    assert at_cost.equity == pytest.approx(98.5 - spent + shares * 0.42), "worth what it would sell for: the bid, 0.42"
    clock.advance(301)
    world.set_book(YES, [(0.52, 1000)], [(0.50, 500)])
    assert (await bot.tick())["marks"] == 1
    assert wallet_of(live_app).equity == pytest.approx(98.5 - spent + shares * 0.50)
    world.set_book(YES, [(0.99, 10)], [])
    clock.advance(301)
    assert (await bot.tick())["marks"] == 0, "no bids: nothing recorded, the last mark stands"
    assert live_app.db.one("SELECT COUNT(*) n FROM position_marks")["n"] == 2


async def test_a_skipped_signal_gets_its_result_so_the_wallet_can_have_taken_it(live_app, fake_http, clock):
    from test_bot import COND, one
    live_app.cfg.filters.max_days_to_close = 14.0                    # setup()'s market ends in 30 days
    world, _, bot = await whale_buy(live_app, fake_http)
    assert one(live_app, "flat_hold")["skip_code"] == "too_long"
    assert [e.code for e in wallet_of(live_app).entries] == ["too_long"]
    open_ = wallet_of(live_app, cutoff=False)
    assert open_.entries[0].status == "TAKEN" and open_.entries[0].optimistic is False, "the book was stored"

    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    t = one(live_app, "flat_hold")
    assert (t["status"], t["payout_per_share"], t["resolved_at"]) == ("SKIPPED", 1.0, clock.now - 640)
    assert t["closed_at"] is None and t["pnl"] is None, "still a skipped row: in no total"
    won = wallet_of(live_app, cutoff=False)
    assert won.entries[0].proceeds == pytest.approx(won.entries[0].contracts) and won.equity > 98.5
    assert await bot.settle_skipped() == 0, "checked hourly, and there is nothing left to check"


async def test_the_runner_notices_a_new_amount_and_the_reports_show_the_wallet(live_app, fake_http, clock):
    from whalewatcher.service import Service
    from whalewatcher.stats import daily_summary, render
    await whale_buy(live_app, fake_http)
    svc = Service(live_app)
    svc._sync_wallet()
    assert br.open_run(live_app.db, "single")["amount"] == 100.0
    br.save_amount(live_app.db, 250, "dashboard", clock.now)
    svc._sync_wallet()
    assert br.open_run(live_app.db, "single")["amount"] == 100.0, "looked at twice a minute, not every cycle"
    svc._wallet_checked = -1e9
    clock.advance(60)
    svc._sync_wallet()
    assert br.open_run(live_app.db, "single")["amount"] == 250.0
    assert live_app.db.one("SELECT COUNT(*) n FROM bankroll_runs")["n"] == 2

    text = render(live_app)
    assert "SMALL WALLET: the same signals through $250.00, fixed stake of 8.0% of equity, up to 10 positions" in text
    assert "Bot, runner single: $250.00 wallet" in text and "live, run #2: $250.00 from" in text
    assert "replay of every signal, with the cutoff:    equity $" in text and "without the cutoff" in text
    assert "taken 1 of 1 signals" in text and "⚠ only 0 settled wallet trades in this run" in text
    brief = daily_summary(live_app)
    assert "$250.00 wallet, run #2: $250.00 from" in brief and "$250.00 wallet, replay of every signal: equity $" in brief
    assert "time-to-close cutoff (off) skipped 0 (too_long 0, no_end_date 0)" in brief
    from whalewatcher.stats import SUMMARY_LIMIT
    assert "no closed trades yet, so no P&L" in brief and len(brief) < SUMMARY_LIMIT and "without the cutoff" not in brief


def test_the_wallet_command_sets_and_shows_the_amount(tmp_path, monkeypatch, capsys):
    from whalewatcher.cli import build_parser, main
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(f"database_path: {tmp_path / 'w.sqlite3'}\n")
    assert build_parser().parse_args(["wallet", "set", "250"]).amount == "250"
    assert main(["wallet", "show"]) == 0
    assert "wallet amount: $100.00 (the default: never set)" in capsys.readouterr().out
    assert main(["wallet", "set", "250"]) == 0
    out = capsys.readouterr().out
    assert "wallet amount: $100.00 -> $250.00" in out and "single: started run #1 with $250.00" in out
    assert main(["wallet", "set", "400.50"]) == 0
    out = capsys.readouterr().out
    assert "single: ended run #1 (wallet amount changed from $250.00 to $400.50)" in out
    assert "single: started run #2 with $400.50" in out and "An ended run is frozen and never rewritten" in out
    for bad in ("0", "abc", "1000000"):
        assert main(["wallet", "set", bad]) == 2
    assert "cannot be more than $100,000" in capsys.readouterr().err
    assert main(["wallet", "set"]) == 2 and "needs an amount" in capsys.readouterr().err
    assert main(["wallet", "show"]) == 0
    assert "wallet amount: $400.50 (set from the cli on " in capsys.readouterr().out


# -- the automatic stake ---------------------------------------------------------------------------------

T = 10_000.0                         # the wallet in these tests starts here, after the history


def history(wins: int, losses: int, price: float = 0.50) -> list[Signal]:
    """Signals that settled before T: what the source has shown so far."""
    return [sig(1000 + i, 100 + i, price, settle=200 + i, payout=1.0 if i < wins else 0.0)
            for i in range(wins + losses)]


def auto(**kw) -> Params:
    base = dict(amount=100.0, onramp_cost_pct=0.0, stake_mode="auto", kelly_fraction=0.1, min_history=30,
                confidence_z=1.0, no_edge_stake="none")
    return Params(**{**base, **kw})


def test_with_no_edge_shown_the_automatic_stake_is_nothing():
    res = run([sig(1, T + 1)], auto(), start=T)
    (e,) = res.entries
    assert e.status == "SKIPPED" and e.code == "no_edge" and res.cash == 100.0
    assert e.reason == "no_edge: no edge can be judged from 0 settled signals (need 30)"
    losing = run(history(10, 30) + [sig(1, T + 1)], auto(), start=T).entries[0]
    assert losing.code == "no_edge" and losing.reason.startswith("no_edge: no edge shown: -6"), \
        "10 wins in 40 at 50 cents: a loss of half of every dollar, less a standard error"
    assert losing.reason.endswith("a dollar after 40 settled signals")


def test_or_the_smallest_order_the_venue_takes_when_told_to_probe():
    res = run([sig(1, T + 1, 0.50)], auto(no_edge_stake="smallest"), start=T)
    (e,) = res.entries
    assert e.status == "TAKEN" and e.stake == pytest.approx(2.50), "5 shares at 50 cents, rounded up to the cent"
    assert e.contracts == pytest.approx(5.0) and e.sizing.endswith("; the smallest order")
    tiny = run([sig(1, T + 1, 0.90)], auto(no_edge_stake="smallest", amount=50.0), start=T).entries[0]
    assert tiny.code == "below_minimum", "5 shares at 90 cents is $4.50: more than 8% of a $50 wallet"


def test_a_shown_edge_is_staked_at_a_fraction_of_kelly():
    """30 wins and 10 losses at 50 cents: returns of +1 (30 times) and -1 (10 times).
    Mean +0.5, sample variance (30 x 0.25 + 10 x 2.25) / 39 = 0.76923, standard error
    0.87706 / sqrt(40) = 0.138675. One standard error off: an edge of +0.361325 a dollar.
    Kelly for a share at cost c paying $1 is c x edge / (1 - c)."""
    past = history(30, 10)
    even = run(past + [sig(1, T + 1, 0.50)], auto(), start=T).entries[0]
    assert even.status == "TAKEN" and even.stake == pytest.approx(3.61), \
        "at 50 cents Kelly is the edge itself: 36.1%, and a tenth of that is 3.61% of equity ($3.613, to the cent)"
    assert "edge +36.1% a dollar after 40 settled signals: 0.1 of Kelly (36.1%) is 3.6% of equity" in even.sizing
    favourite = run(past + [sig(1, T + 1, 0.80)], auto(), start=T).entries[0]
    assert favourite.stake == pytest.approx(8.0), "0.8 x 0.3613 / 0.2 = 145%: capped at the fixed 8%"
    longshot = run(past + [sig(1, T + 1, 0.20)], auto(), start=T).entries[0]
    assert longshot.stake == pytest.approx(1.00), \
        "0.2 x 0.3613 / 0.8 = 9.0% Kelly, 0.9% of equity is 90 cents: raised to the smallest order, 5 x 0.20"


def test_the_bar_for_an_edge_is_the_number_of_signals_and_the_standard_errors():
    past = history(30, 10)
    assert run(past + [sig(1, T + 1)], auto(min_history=41), start=T).entries[0].code == "no_edge"
    strict = run(past + [sig(1, T + 1)], auto(confidence_z=1.96, kelly_fraction=0.25), start=T).entries[0]
    assert "edge +22.8% a dollar" in strict.sizing, "0.5 - 1.96 x 0.138675"
    assert strict.stake == pytest.approx(5.70), "100 x 0.25 x (0.5 - 1.96 x 0.138675) = 5.7047, to the cent"
    assert run(history(22, 18) + [sig(1, T + 1)], auto(confidence_z=1.96), start=T).entries[0].code == "no_edge", \
        "55% winners in 40 is not distinguishable from a coin"


def test_a_result_that_came_in_after_the_decision_is_not_evidence():
    late = [sig(1000 + i, 100 + i, 0.50, settle=T + 500 + i, payout=1.0) for i in range(40)]    # all win, later
    first = run(late + [sig(1, T + 1)], auto(), start=T).entries[0]
    assert first.code == "no_edge" and "from 0 settled signals" in first.reason
    after = run(late + [sig(1, T + 1), sig(2, T + 1000)], auto(), start=T, as_of=T + 2000).entries
    assert [e.code for e in after] == ["no_edge", None] and after[1].status == "TAKEN", \
        "once they have settled they count, for decisions made after that"


def test_fees_and_missed_orders_are_part_of_the_evidence_the_right_way():
    paid = [sig(1000 + i, 100 + i, 0.50, settle=200 + i, payout=1.0 if i < 30 else 0.0, fee_rate=0.05)
            for i in range(40)]
    e = run(paid + [sig(1, T + 1, 0.50, fee_rate=0.05)], auto(), start=T).entries[0]
    assert "edge +32.8% a dollar" in e.sizing, \
        "a winner returns 1 / 0.5125 - 1 = +95.1%, not +100%: mean +46.3%, less a standard error of 13.5%"
    missed = [sig(2000 + i, 100 + i, 0.50, settle=200 + i, payout=1.0, status="MISSED") for i in range(40)]
    assert run(missed + [sig(1, T + 1)], auto(), start=T).entries[0].code == "no_edge", \
        "an order that never filled made no money, whatever the market did"


def test_the_fixed_stake_is_untouched_and_the_two_are_different_runs():
    fixed = Params(amount=100.0, onramp_cost_pct=0.0)
    assert fixed.stake_mode == "fixed" and run([sig(1, 100)], fixed).entries[0].stake == pytest.approx(8.0)
    assert fixed.fingerprint != auto().fingerprint
    assert auto().fingerprint != auto(no_edge_stake="smallest").fingerprint != auto(min_history=50).fingerprint
    assert "fixed stake of 8.0% of equity" in fixed.describe()
    assert "automatic stake (no order until 30 settled signals show an edge, then 0.1 of Kelly" in auto().describe()


def test_the_shipped_setting_is_the_automatic_stake_at_the_projects_own_decision_rule(tmp_path):
    from whalewatcher.config import ConfigError, load
    b = load("config.yaml", env={}).bankroll
    assert (b.stake_mode, b.kelly_fraction, b.auto_min_history, b.auto_confidence_z, b.auto_no_edge_stake) == (
        "auto", 0.25, 100, 1.96, "none")
    p = Params.of(load("config.yaml", env={}), 100.0)
    assert (p.stake_mode, p.min_history, p.confidence_z, p.no_edge_stake) == ("auto", 100, 1.96, "none")
    f = tmp_path / "c.yaml"
    for bad, why in (("stake_mode: big", "auto or fixed"), ("kelly_fraction: 0", "kelly_fraction"),
                     ("auto_no_edge_stake: some", "none or smallest"), ("auto_min_history: 1", "auto_min_history"),
                     ("auto_confidence_z: -1", "auto_confidence_z")):
        f.write_text(f"bankroll:\n  {bad}\n")
        with pytest.raises(ConfigError, match=why):
            load(str(f), env={})


def test_stake_sizes_are_compared_on_the_same_signals(cfg):
    signals = history(30, 10) + [sig(1, T + 1, 0.50, settle=T + 5, payout=1.0), sig(2, T + 10, 0.50)]
    cfg.fees.onramp_cost_pct = 0.0
    rows = br.stake_comparison(signals, cfg, 100.0, Marks(), T + 100)
    names = [name for name, _ in rows]
    assert names == ["Automatic, no order without an edge", "Automatic, smallest order without an edge",
                     "Fixed 1% of equity", "Fixed 2% of equity", "Fixed 4% of equity", "Fixed 8% of equity (in use)"]
    by = dict(rows)
    assert by["Fixed 8% of equity (in use)"].entries[0].stake == pytest.approx(8.0)
    assert by["Fixed 2% of equity"].entries[0].code == "below_minimum", "2% of $100 buys 4 shares: under the 5 minimum"
    assert by["Fixed 4% of equity"].entries[0].stake == pytest.approx(4.0)
    assert by["Fixed 1% of equity"].entries[0].code == "below_minimum"
    assert by["Automatic, no order without an edge"].params.no_edge_stake == "none"
    text = "\n".join(br.comparison_lines(rows))
    assert "choosing the best row afterwards is hindsight" in text and "Fixed 4% of equity" in text
    cfg.bankroll.stake_mode, cfg.bankroll.auto_no_edge_stake = "auto", "smallest"
    assert br.stake_comparison(signals, cfg, 100.0, Marks(), T + 100)[1][0].endswith("(in use)")
