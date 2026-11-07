"""The fresh-wallet test: who counts as fresh, which markets are scanned, the copy, and that its
rows replace only its own strategy in the overview."""

from __future__ import annotations

import pytest

from conftest import DAY
from whalewatcher.backtest import SimTrade, write_trades
from whalewatcher.fresh import STRATEGY, candidates, describe, run_fresh_wallet_test
from whalewatcher.stats import render
from world import FakeMarket, World

T0 = 1_790_000_000          # 2026-09-21
START, END = T0 - 30 * DAY, T0 + 30 * DAY


def test_only_wallets_that_could_still_be_fresh_are_looked_up():
    fills = {"0xm": [("old", 0, "SELL", 900.0), ("old", 20 * DAY, "BUY", 900.0),   # 20 days after its earliest fill
                     ("new", 0, "SELL", 600.0), ("new", 3 * DAY, "BUY", 900.0),    # 3 days after
                     ("seller", 0, "SELL", 900.0),                                # never buys
                     ("early", 0, "SELL", 900.0), ("early", DAY, "BUY", 900.0)]}  # fresh, but before the window
    assert candidates(fills, max_age=14 * DAY, start=2 * DAY, end=30 * DAY) == {"new"}


async def test_a_fresh_wallets_large_buy_in_a_non_sports_market_is_copied(app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xgeo", resolved_at=T0 + 5 * DAY, payouts=[1.0, 0.0], tags=["geopolitics"]))
    world.add_market(FakeMarket("0xspt", resolved_at=T0 + 5 * DAY, payouts=[1.0, 0.0], tags=["sports"]))
    world.add_market(FakeMarket("0xold", resolved_at=START - 5 * DAY, payouts=[1.0, 0.0], tags=["politics"]))
    world.trade("0xnew", T0 - 2 * DAY, "0xelsewhere", size=10, price=0.5)            # its first trade
    world.trade("0xnew", T0, "0xgeo", size=600 / 0.55, price=0.55)                   # $600
    world.trade("0xnew", T0 + 60, "0xgeo", size=500 / 0.56, price=0.56)              # $500: the group reaches $1,100
    world.trade("0xnew", T0, "0xspt", size=4000, price=0.5)                          # sports: never scanned
    world.trade("0xvet", T0 - 60 * DAY, "0xelsewhere", size=10, price=0.5)           # trading for months
    world.trade("0xvet", T0, "0xgeo", size=4000, price=0.5)
    world.trade("0xsmall", T0 - DAY, "0xelsewhere", size=10, price=0.5)
    world.trade("0xsmall", T0, "0xgeo", size=600, price=0.5)                         # $300: below the signal
    world.clob_history = lambda token, s, e: [(s + 30, 0.57)]

    rep = await run_fresh_wallet_test(app, START, END)
    assert rep.markets == 1, "only the non-sports market that resolved inside the window"
    assert rep.fresh_wallets == 1 and [e.wallet for e in rep.events] == ["0xnew"]
    (e,) = rep.events
    assert e.trigger_ts == T0 + 60 and e.whale_usd == pytest.approx(1100)
    (t,) = rep.trades
    assert t.strategy == STRATEGY and t.status == "FILLED" and t.fill_price == pytest.approx(0.58)
    assert t.settled["result"] == "WIN" and t.settled["pnl"] == pytest.approx(10 / 0.58 - 10)
    text = describe(rep, samples=500)
    assert "n=1" in text and "FAILS: too few copies" in text, "one copy is no evidence either way"
    assert "fresh-wallet (SIMULATED)" in render(app)

    trades_calls = fake_http.count("/v2/trades")
    await run_fresh_wallet_test(app, START, END)
    assert fake_http.count("/v2/trades") == trades_calls, "market fills come from the cache"
    assert app.db.one("SELECT COUNT(*) AS n FROM paper_trades WHERE strategy = ?", (STRATEGY,))["n"] == 1


async def test_each_backtest_replaces_only_its_own_rows(app, fake_http):
    from whalewatcher.backtest import AlertEvent
    e = AlertEvent("0xw", "0xc", "0xc-t0", "Yes", 0, T0, T0, T0, 0.5, 600, 1200, 1)
    replay = SimTrade(e, "backtest:spec", "MISSED", miss_reason="no price printed within 15 minutes")
    fresh = SimTrade(e, STRATEGY, "MISSED", miss_reason="no price printed within 15 minutes")
    write_trades(app, [replay])
    write_trades(app, [fresh], strategies=(STRATEGY,), state_key="backtest_run:fresh-wallet")
    write_trades(app, [replay])                     # re-running the replay keeps the fresh-wallet rows

    def strategies():
        return sorted(r["strategy"] for r in app.db.query("SELECT strategy FROM paper_trades"))

    assert strategies() == ["backtest:fresh-wallet", "backtest:spec"]
    write_trades(app, [], strategies=(STRATEGY,), state_key="backtest_run:fresh-wallet")
    assert strategies() == ["backtest:spec"]
    assert app.db.one("SELECT COUNT(*) AS n FROM alerts WHERE status = 'BACKTEST'")["n"] == 1, "orphans removed"
