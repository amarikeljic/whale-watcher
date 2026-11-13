"""Backtest: alert groups like the monitor, the paper engine's fill rule on historical prices,
walk-forward planning that never sees the period it trades, idempotent writes, and simulated
trades kept apart from live ones in stats."""

from __future__ import annotations

import random

import pytest

from conftest import DAY, NOW, act, market
from whalewatcher.backtest import AlertEvent, alert_events, plan_periods, run_backtest, simulate, top_wallets
from whalewatcher.config import ScoringConfig
from whalewatcher.fees import polymarket_taker_fee
from whalewatcher.monitor import Monitor
from whalewatcher.optimize import SPEC_REQUIREMENTS, Fold, FoldIndex, FoldWallet, choose, search
from whalewatcher.scoring import WalletMetrics
from whalewatcher.stats import render
from whalewatcher.stats_util import bootstrap_mean_ci, two_way_bootstrap_mean_ci
from world import FakeMarket, World

T0 = NOW - 40 * DAY
FILL = dict(strategy="backtest:test", stake=10.0, delay=90, ask_premium=0.01, max_slippage=0.03)


def event(whale_price: float, *, trigger: int = T0, index: int = 0, cond: str = "0xa") -> AlertEvent:
    return AlertEvent(wallet="0xw", condition_id=cond, token_id=f"{cond}-t{index}", outcome="Yes", outcome_index=index,
                      first_fill_ts=trigger - 50, trigger_ts=trigger, last_fill_ts=trigger, whale_price=whale_price,
                      whale_usd=600.0, whale_shares=600 / whale_price, fills=2)


# -- alert groups ------------------------------------------------------------------------------


def test_alert_events_group_per_token_like_the_monitor():
    m = market("0xa", resolved_at=T0 + 20 * DAY)
    rows = [
        act(T0 - 100, size=2000, price=0.45, cond="0xa"),                     # before the period
        act(T0, size=500, price=0.40, cond="0xa"),                            # $200
        act(T0 + 100, size=1000, price=0.42, cond="0xa"),                     # $420: crosses $500 here
        act(T0 + 250, size=200, price=0.43, cond="0xa"),                      # $86, same group (<= 300 s)
        act(T0 + 1000, size=500, price=0.60, cond="0xa"),                     # alone, $300: no alert
        act(T0 + 2000, size=2000, price=0.30, cond="0xa", index=1),           # the other outcome: own group
        act(T0 + 2100, side="SELL", size=5000, price=0.5, cond="0xa"),        # sells never alert
        act(T0 + 6 * DAY, size=5000, price=0.5, cond="0xa"),                  # after the period
    ]
    evs = alert_events("0xw", rows, {"0xa": m}, T0, T0 + 5 * DAY, window=300, min_usd=500)
    assert [(e.token_id, e.first_fill_ts, e.trigger_ts, e.fills) for e in evs] == [
        ("0xa-t0", T0, T0 + 100, 3), ("0xa-t1", T0 + 2000, T0 + 2000, 1)]
    first = evs[0]
    assert first.whale_price == pytest.approx(620 / 1500), "VWAP up to the trigger, what the alert would show"
    assert first.whale_usd == pytest.approx(706) and first.whale_shares == pytest.approx(1700)
    assert first.outcome == "Yes" and evs[1].outcome == "No" and evs[1].wallet == "0xw"


# -- the fill rule -----------------------------------------------------------------------------


def test_a_delayed_copy_fills_at_the_later_price_plus_fee_and_settles():
    m = market("0xa", resolved_at=T0 + 5 * DAY, payouts=[1.0, 0.0], fee_rate=0.05)
    e = event(0.40)
    t = simulate(e, m, (T0 + 120, 0.42), **FILL)
    assert t.status == "FILLED" and t.fill_ts == T0 + 120 and t.fill_price == pytest.approx(0.43)
    contracts = 10 / 0.43
    fee = polymarket_taker_fee(contracts, 0.43, 0.05)
    assert fee > 0 and t.fees == pytest.approx(fee) and t.contracts == pytest.approx(contracts)
    assert t.cost == pytest.approx(10 + fee)
    assert t.settled["result"] == "WIN" and t.settled["pnl"] == pytest.approx(contracts - 10 - fee)
    assert t.zero[0] == 0.40 and t.atask[0] == pytest.approx(0.41), "shadows: whale price, alert-time ask"
    assert t.settled["zero_pnl"] > t.settled["atask_pnl"] > t.settled["pnl"], "latency costs money"


def test_misses_follow_the_paper_engine():
    open_market = market("0xa")
    e = event(0.40)
    t = simulate(e, open_market, (T0 + 100, 0.44), **FILL)
    assert t.status == "MISSED" and t.miss_reason.startswith("slippage"), "0.45 is 0.04 above the 0.41 alert ask"
    assert t.settled is None and t.contracts is None
    assert simulate(e, open_market, (T0 + 100, 0.43), **FILL).status == "FILLED", "0.03 above is still allowed"
    assert simulate(e, open_market, None, **FILL).miss_reason.startswith("no price")
    closed = market("0xa", resolved_at=T0 + 60)        # closes 60 s after the trigger; the fill is due at +90
    t = simulate(e, closed, (T0 + 100, 0.40), **FILL)
    assert t.miss_reason == "market closed before the fill"
    assert t.settled["pnl"] == 0.0 and t.settled["zero_pnl"] is not None, "a miss earns 0; its shadows settle"


def test_a_void_pays_half_per_share_not_a_refund():
    m = market("0xa", resolved_at=T0 + DAY, payouts=[0.5, 0.5])
    t = simulate(event(0.40), m, (T0 + 95, 0.40), **FILL)
    assert t.settled["result"] == "SPLIT" and t.settled["payout_per_share"] == 0.5
    assert t.settled["pnl"] == pytest.approx(10 / 0.41 * 0.5 - 10)


# -- who gets copied -----------------------------------------------------------------------------


def fold_wallet(i: int, *, share: float, rois: list[float], roi: float = 0.1, settled: int = 40) -> FoldWallet:
    m = WalletMetrics(f"0x{i:03x}", 0, settled_markets=settled, total_pnl=roi * 1000, total_cost=1000, roi=roi,
                      profitable_months=3, months_in_window=6, max_single_market_share=share, avg_stake_usd=200,
                      days_since_last_trade=2, two_sided_ratio=0.1, win_rate=0.55)
    return FoldWallet(m.wallet, m, rois)


def make_fold(cutoff: int, rng: random.Random, *, signal: bool) -> Fold:
    wallets = []
    for i in range(40):
        concentrated = i % 2 == 1
        if signal:
            rois = [rng.gauss(-0.3 if concentrated else 0.3, 0.2) for _ in range(6)]
        else:
            rois = [rng.gauss(0, 0.5) for _ in range(6)]
        wallets.append(fold_wallet(i, share=0.9 if concentrated else 0.1, rois=rois))
    return Fold(cutoff, cutoff + 60 * DAY, 40, 0, wallets)


def test_walk_forward_requirements_never_see_the_period_they_trade():
    sc = ScoringConfig(top_k=100)
    first = make_fold(T0 - 120 * DAY, random.Random(1), signal=True)
    second_a = make_fold(T0 - 60 * DAY, random.Random(2), signal=False)
    second_b = make_fold(T0 - 60 * DAY, random.Random(3), signal=True)
    plans_a = plan_periods([first, second_a], sc, min_wallets=10, min_alerts=50, objective="pooled")
    plans_b = plan_periods([second_b, first], sc, min_wallets=10, min_alerts=50, objective="pooled")

    def walk_forward(plans):
        return [(p.fold.cutoff, p.requirements, p.wallets) for p in plans if p.strategy == "backtest:walk-forward"]

    wf_a = walk_forward(plans_a)
    assert [c for c, _, _ in wf_a] == [second_a.cutoff], "nothing earlier than the first period, so no rule there"
    expected = choose(search([FoldIndex(first)], min_wallets=10, min_alerts=50), "pooled").req
    assert wf_a[0][1] == expected, "chosen on the earlier period alone"
    assert wf_a[0][1]["max_single_market_share"] is not None, "the earlier period's signal was concentration"
    assert walk_forward(plans_b) == wf_a, \
        "the traded period's own outcomes change nothing about who it copies"
    assert {p.strategy for p in plans_a if p.fold is first} == {"backtest:spec"}


def test_copied_wallets_are_ranked_and_cut_like_scoring():
    sc = ScoringConfig(top_k=2, rank_by="roi")
    wallets = [fold_wallet(1, share=0.1, rois=[], roi=0.30, settled=60),
               fold_wallet(2, share=0.1, rois=[], roi=0.50, settled=60),
               fold_wallet(3, share=0.1, rois=[], roi=0.10, settled=60),
               fold_wallet(4, share=0.1, rois=[], roi=0.90, settled=20)]       # fails min_settled_markets 50
    req = dict(SPEC_REQUIREMENTS, min_profitable_months=0)
    passed, chosen = top_wallets(wallets, req, sc)
    assert passed == 3 and chosen == ["0x002", "0x001"]


# -- end to end -----------------------------------------------------------------------------------


W = "0xwhale"


def spec_fold(start: int) -> Fold:
    m = WalletMetrics(W, start, settled_markets=80, total_pnl=900, total_cost=6000, roi=0.15, profitable_months=5,
                      months_in_window=6, max_single_market_share=0.1, avg_stake_usd=300, days_since_last_trade=1,
                      two_sided_ratio=0.1, win_rate=0.6, clv=0.02, clv_n=40)   # rankable under the default (clv)
    return Fold(start, start + 30 * DAY, 1, 0, [FoldWallet(W, m, [])])


def seed_live_trade(app) -> None:
    app.db.execute(
        "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
        "trigger_fill_ts, whale_shares, whale_usd, whale_price, fills, detected_at, posted_at, status, updated_at) "
        "VALUES ('0xlive', '0xa', '0xa-t0', 'Yes', 'NEW', ?, ?, ?, 1000, 500, 0.5, 1, ?, ?, 'POSTED', ?)",
        (NOW - 900, NOW - 900, NOW - 900, NOW - 860, NOW - 860, NOW))
    app.db.execute(
        "INSERT INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
        "alert_ts, fill_ts, fill_price, contracts, fees, cost, settled_at, settle_result, payout_per_share, payout, "
        "pnl, updated_at) VALUES (last_insert_rowid(), 'polymarket', 'hold_to_resolution', 'FILLED', '0xa-t0', "
        "'0xa', 10, 1, ?, ?, 0.5, 20, 0, 10, ?, 'LOSS', 0, 0, -10, ?)", (NOW - 860, NOW - 800, NOW - 10, NOW))


async def test_backtest_writes_simulated_trades_once_and_stats_keeps_them_apart(app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xa", resolved_at=T0 + 20 * DAY, payouts=[1.0, 0.0]))
    world.add_market(FakeMarket("0xb", end_date=NOW + 30 * DAY))                       # still open
    world.add_market(FakeMarket("0xc", closed_time=T0 + 4 * DAY + 60))               # closes before the fill
    world.trade(W, T0 + DAY, "0xa", size=1500, price=0.40)                           # $600: copied, wins
    world.trade(W, T0 + 2 * DAY, "0xb", size=1400, price=0.50)                       # $700: copied, open
    world.trade(W, T0 + 3 * DAY, "0xa", size=1000, price=0.97)                       # outside 0.05-0.95
    world.trade(W, T0 + 4 * DAY, "0xc", size=1600, price=0.50)                       # $800: missed, closed
    world.trade(W, T0 + 5 * DAY, "0xa", side="SELL", size=1000, price=0.6)           # sells never copy
    world.clob_history = lambda token, s, e: [(s - 30, 0.10), (s + 30, 0.41 if token == "0xa-t0" else 0.50)]

    report = await run_backtest(app, [spec_fold(T0)])
    by_cond = {t.event.condition_id: t for t in report.trades}
    assert set(by_cond) == {"0xa", "0xb", "0xc"} and len(report.trades) == 3
    assert report.skipped[("backtest:spec", "price outside 0.05-0.95")] == 1
    win = by_cond["0xa"]
    assert win.status == "FILLED" and win.fill_price == pytest.approx(0.42), "first price at or after the fill time"
    assert win.settled["result"] == "WIN" and win.settled["pnl"] == pytest.approx(10 / 0.42 - 10)
    assert by_cond["0xb"].status == "FILLED" and by_cond["0xb"].settled is None
    assert by_cond["0xc"].miss_reason == "market closed before the fill"

    def counts():
        return (app.db.one("SELECT COUNT(*) AS n FROM paper_trades WHERE strategy = 'backtest:spec'")["n"],
                app.db.one("SELECT COUNT(*) AS n FROM alerts WHERE status = 'BACKTEST'")["n"])

    assert counts() == (3, 3)
    lookups = fake_http.count("/prices-history")
    await run_backtest(app, [spec_fold(T0)])
    assert counts() == (3, 3), "a second run replaces the first"
    assert fake_http.count("/prices-history") == lookups, "fill prices come from the cache the second time"

    seed_live_trade(app)
    text = render(app)
    lines = text.splitlines()
    assert lines[0].startswith("⚠ NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: 1 settled live paper trades")
    live_at, sim_at = text.index("LIVE PAPER TRADES"), text.index("SIMULATED PAPER TRADES")
    assert live_at < sim_at
    live_part, sim_part = text[live_at:sim_at], text[sim_at:]
    assert "polymarket / hold_to_resolution" in live_part and "alerts 1 |" in live_part
    assert "total P&L -$10.00" in live_part and "backtest" not in live_part.split("By month")[0]
    assert "backtest:spec (SIMULATED)" in sim_part and "alerts 3 |" in sim_part
    assert "⚠ only 1 settled trades" in sim_part and "n=1" in sim_part
    assert "90s after the triggering fill" in sim_part, "the overview states the replay's assumptions"


async def test_the_live_monitor_never_joins_a_backtest_alert(live_app, fake_http, clock):
    world = World(fake_http)
    world.add_market(FakeMarket("0xm", end_date=NOW + 30 * DAY))
    world.set_book("0xm-t0", [(0.44, 5000)], [(0.42, 500)])
    live_app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                        "VALUES (?, 1, 1, 1, ?, ?)", (W, NOW - 3600, NOW - 120))
    live_app.db.execute(
        "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
        "trigger_fill_ts, whale_shares, whale_usd, whale_price, fills, detected_at, status, updated_at) "
        "VALUES (?, '0xm', '0xm-t0', 'Yes', 'NEW', ?, ?, ?, 1500, 630, 0.42, 1, ?, 'BACKTEST', ?)",
        (W, NOW - 30, NOW - 30, NOW - 30, NOW - 30, NOW))
    world.trade(W, NOW - 20, "0xm", size=1500, price=0.42)
    await Monitor(live_app).poll_once()
    rows = live_app.db.query("SELECT status, fills FROM alerts ORDER BY id")
    assert [(r["status"], r["fills"]) for r in rows] == [("BACKTEST", 1), ("RECORDED", 1)]


# -- the interval ---------------------------------------------------------------------------------


def test_repeated_trades_on_one_event_do_not_narrow_the_interval():
    values = [1.0] * 50 + [-1.0] * 10              # 50 copies of one bet, 10 separate losing bets
    wallets = ["a"] * 60
    events = ["e0"] * 50 + [f"e{i}" for i in range(1, 11)]
    _, lo_iid, hi_iid = bootstrap_mean_ci(values, samples=2000)
    m, lo, hi = two_way_bootstrap_mean_ci(values, wallets, events, samples=2000)
    assert m == pytest.approx(40 / 60)
    assert hi - lo > 2 * (hi_iid - lo_iid), "11 independent events, not 60"
    assert lo < 0 < hi, "one lucky bet repeated 50 times is not evidence"
    assert two_way_bootstrap_mean_ci([1.0, 1.0, -1.0], ["a"] * 3, ["e"] * 3) == (pytest.approx(1 / 3), None, None)
