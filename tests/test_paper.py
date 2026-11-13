"""Phase 3: delayed fills from the real book, MISSED rules, shadow fills, settlement P&L by hand."""

from __future__ import annotations

import pytest

from conftest import NOW
from whalewatcher.fees import polymarket_taker_fee
from whalewatcher.monitor import Monitor
from whalewatcher.paper import PaperEngine, settle_values, walk_asks
from whalewatcher.polymarket import Book, Level
from world import FakeMarket, World

WHALE, COND = "0xwhale", "0xm"
YES = f"{COND}-t0"


def book(*asks):
    return Book("t", NOW, bids=[], asks=[Level(p, s) for p, s in asks])


def test_walk_spends_the_stake_across_levels():
    w = walk_asks(book((0.40, 10), (0.42, 100)), 10.0)
    # 10 shares at 0.40 = $4, then $6 / 0.42 = 14.2857 shares
    assert w.fills == [(0.40, 10), (0.42, pytest.approx(6 / 0.42))]
    assert w.contracts == pytest.approx(10 + 6 / 0.42)
    assert w.vwap == pytest.approx(10 / (10 + 6 / 0.42))
    assert w.levels == 2


def test_walk_fails_when_the_book_is_too_thin():
    assert walk_asks(book((0.40, 5)), 10.0) is None


def world_with_alert(app, fake_http, *, fee_rate=0.05, asks=((0.44, 1000),)):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 86400 * 30, fee_rate=fee_rate))
    world.set_book(YES, list(asks))
    app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                   "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    return world, Monitor(app, paper=PaperEngine(app))


async def test_fill_happens_on_the_next_poll_at_the_book_price_then(live_app, fake_http, clock):
    world, monitor = world_with_alert(live_app, fake_http)
    await monitor.poll_once()                       # alert at 0.44
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "PENDING" and t["alert_ask"] == 0.44
    world.set_book(YES, [(0.46, 30), (0.47, 1000)])  # the price moved while I reacted
    clock.advance(60)
    await monitor.poll_once()
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "FILLED" and t["fill_ts"] == NOW + 60
    contracts = 30 + (10 - 30 * 0.46) / 0.47 if 30 * 0.46 < 10 else 10 / 0.46
    assert t["contracts"] == pytest.approx(contracts)
    assert t["fill_price"] == pytest.approx(10 / contracts)
    expected_fees = polymarket_taker_fee(min(30, 10 / 0.46), 0.46, 0.05) + (
        polymarket_taker_fee((10 - 30 * 0.46) / 0.47, 0.47, 0.05) if 30 * 0.46 < 10 else 0)
    assert t["fees"] == pytest.approx(expected_fees, abs=1e-5)
    assert t["cost"] == pytest.approx(10 + t["fees"])
    # shadow fills: whale's own price and the alert-time ask
    assert t["zero_price"] == pytest.approx(0.42) and t["atask_price"] == 0.44


async def test_slippage_beyond_the_limit_is_missed(live_app, fake_http, clock):
    world, monitor = world_with_alert(live_app, fake_http)
    await monitor.poll_once()
    world.set_book(YES, [(0.48, 1000)])             # 0.04 above the alert ask, limit is 0.03
    clock.advance(60)
    await monitor.poll_once()
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "MISSED" and t["miss_reason"].startswith("slippage")


async def test_a_market_that_closed_before_the_fill_is_missed(live_app, fake_http, clock):
    world, monitor = world_with_alert(live_app, fake_http)
    await monitor.poll_once()
    world.markets[COND].accepting_orders = False
    clock.advance(60)
    await monitor.poll_once()
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "MISSED" and "closed" in t["miss_reason"]


async def test_every_posted_alert_gets_exactly_one_filled_or_missed_record(live_app, fake_http, clock):
    world, monitor = world_with_alert(live_app, fake_http)
    world.trade(WHALE, NOW - 10, COND, index=1, size=1200, price=0.56, usdc=672)   # a NO alert too
    world.set_book(f"{COND}-t1", [(0.57, 1000)])
    await monitor.poll_once()
    for _ in range(3):
        clock.advance(60)
        await monitor.poll_once()
    posted = live_app.db.query("SELECT id FROM alerts WHERE status = 'RECORDED'")
    trades = live_app.db.query("SELECT alert_id, status FROM paper_trades")
    assert len(posted) == 2 and len(trades) == 2
    assert sorted(t["alert_id"] for t in trades) == sorted(a["id"] for a in posted)
    assert {t["status"] for t in trades} <= {"FILLED", "MISSED"}


def trade(status="FILLED", **kw):
    base = dict(status=status, contracts=22.0, cost=10.10, zero_contracts=23.8, zero_cost=10.11,
                atask_contracts=22.7, atask_cost=10.11)
    base.update(kw)
    return base


def test_settlement_win_loss_split_and_missed_by_hand():
    win = settle_values(trade(), 1.0)
    assert win["result"] == "WIN" and win["payout"] == pytest.approx(22.0) and win["pnl"] == pytest.approx(11.90)
    assert win["zero_pnl"] == pytest.approx(23.8 - 10.11) and win["atask_pnl"] == pytest.approx(22.7 - 10.11)
    loss = settle_values(trade(), 0.0)
    assert loss["result"] == "LOSS" and loss["pnl"] == pytest.approx(-10.10)
    # Polymarket voids pay 0.50 a share ("50-50"), not a refund.
    split = settle_values(trade(), 0.5)
    assert split["result"] == "SPLIT" and split["pnl"] == pytest.approx(11.0 - 10.10)
    # A venue that refunds a void returns the cost: zero P&L.
    refund = settle_values(trade(), 0.0, refunded=True)
    assert refund["result"] == "REFUND" and refund["pnl"] == 0.0 and refund["payout"] == pytest.approx(10.10)
    # MISSED: no position, so 0, but the shadows still settle.
    missed = settle_values(trade("MISSED", contracts=None, cost=None), 1.0)
    assert missed["pnl"] == 0.0 and missed["result"] is None and missed["zero_pnl"] == pytest.approx(13.69)


async def test_settle_job_pays_out_resolved_markets(live_app, fake_http, clock):
    world, monitor = world_with_alert(live_app, fake_http)
    await monitor.poll_once()
    clock.advance(60)
    await monitor.poll_once()
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "FILLED"
    world.markets[COND].resolved_at = NOW + 3600
    world.markets[COND].payouts = [1.0, 0.0]
    world.price_fn = lambda token, at: 0.80
    clock.advance(7200)
    live_app.db.execute("UPDATE poly_markets SET meta_fetched_at = 0, resolution_checked_at = NULL")
    assert await PaperEngine(live_app).settle() == 1
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["status"] == "FILLED", "status stays FILLED; settlement fields mark it settled"
    assert t["settle_result"] == "WIN" and t["pnl"] == pytest.approx(t["contracts"] - t["cost"])
    assert t["closing_price"] == 0.80


async def test_an_in_play_fill_gets_no_closing_price(live_app, fake_http, clock):
    """For sports the closing price is the last pre-game price; a fill after the start has none."""
    world, monitor = world_with_alert(live_app, fake_http)
    world.markets[COND].game_start = NOW - 1800          # the game started before the alert
    await monitor.poll_once()
    clock.advance(60)
    await monitor.poll_once()
    assert live_app.db.one("SELECT status FROM paper_trades")["status"] == "FILLED"
    world.markets[COND].resolved_at = NOW + 3600
    world.markets[COND].payouts = [1.0, 0.0]
    clock.advance(7200)
    live_app.db.execute("UPDATE poly_markets SET meta_fetched_at = 0, resolution_checked_at = NULL")
    lookups = fake_http.count("/v2/prices-history")
    assert await PaperEngine(live_app).settle() == 1
    t = live_app.db.one("SELECT * FROM paper_trades")
    assert t["settle_result"] == "WIN" and t["closing_price"] is None
    assert fake_http.count("/v2/prices-history") == lookups, "no lookup for a price that means nothing"
