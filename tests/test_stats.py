"""Phase 5: stats on a small fixture must match a hand calculation."""

from __future__ import annotations

import csv

import pytest

from whalewatcher.stats import export_csv, render, summarise

BASE = 1_800_000_000


def seed(db, rows):
    """rows: dicts with alert + paper fields. Inserts alerts and paper_trades."""
    db.execute("INSERT INTO poly_markets (condition_id, question, category, meta_fetched_at) "
               "VALUES ('0xpol', 'Politics?', 'politics', 1), ('0xspo', 'Sports?', 'sports', 1)")
    for i, r in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO alerts (id, wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
            "trigger_fill_ts, whale_shares, whale_usd, whale_price, fills, detected_at, posted_at, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, r["wallet"], r["cond"], f"{r['cond']}-t0", "Yes", "NEW", BASE + i * 1000, BASE + i * 1000,
             BASE + i * 1000, 1000, 500, r["whale"], 1, BASE + i * 1000 + 40, BASE + i * 1000 + 41, "POSTED",
             BASE))
        db.execute(
            "INSERT INTO paper_trades (alert_id, venue, strategy, status, miss_reason, token_id, condition_id, "
            "stake_usd, due_poll, alert_ts, alert_ask, fill_ts, fill_price, contracts, fees, cost, "
            "zero_price, zero_contracts, zero_fees, zero_cost, atask_price, atask_contracts, atask_fees, atask_cost, "
            "settled_at, settle_result, payout_per_share, payout, pnl, zero_pnl, atask_pnl, closing_price, updated_at) "
            "VALUES (?,?,?,?,?,?,?,10,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "polymarket", "hold_to_resolution", r["status"], r.get("miss"), f"{r['cond']}-t0", r["cond"],
             BASE + i * 1000 + 41, r["ask"], BASE + i * 1000 + 101 if r["status"] == "FILLED" else None,
             r.get("fill"), r.get("contracts"), r.get("fees"), r.get("cost"),
             r["whale"], 10 / r["whale"], 0, 10, r["ask"], 10 / r["ask"], 0, 10,
             r.get("settled"), r.get("result"), r.get("pps"), r.get("payout"), r.get("pnl"),
             r.get("zero_pnl"), r.get("atask_pnl"), r.get("closing"), BASE))


FIXTURE = [
    # win: 20 contracts at 0.50, cost 10.00 -> payout 20 -> +10
    dict(wallet="0xa", cond="0xpol", status="FILLED", whale=0.45, ask=0.48, fill=0.50, contracts=20, fees=0,
         cost=10.0, settled=BASE + 9000, result="WIN", pps=1.0, payout=20, pnl=10.0, zero_pnl=12.22, atask_pnl=10.83,
         closing=0.60),
    # loss: -10
    dict(wallet="0xa", cond="0xspo", status="FILLED", whale=0.30, ask=0.31, fill=0.32, contracts=31.25, fees=0,
         cost=10.0, settled=BASE + 9100, result="LOSS", pps=0.0, payout=0, pnl=-10.0, zero_pnl=-10, atask_pnl=-10,
         closing=0.28),
    # 50-50 split: 25 contracts at 0.40 -> payout 12.5 -> +2.5
    dict(wallet="0xb", cond="0xpol", status="FILLED", whale=0.38, ask=0.39, fill=0.40, contracts=25, fees=0,
         cost=10.0, settled=BASE + 9200, result="SPLIT", pps=0.5, payout=12.5, pnl=2.5, zero_pnl=3.16, atask_pnl=2.82,
         closing=0.50),
    # missed, settled market: real pnl 0, shadows count
    dict(wallet="0xb", cond="0xpol", status="MISSED", miss="slippage: x", whale=0.50, ask=0.52, settled=BASE + 9300,
         pnl=0.0, zero_pnl=10.0, atask_pnl=9.23),
    # open: filled, not settled
    dict(wallet="0xb", cond="0xspo", status="FILLED", whale=0.60, ask=0.61, fill=0.62, contracts=16.13, fees=0,
         cost=10.0),
]


def test_summary_matches_hand_calculation(app):
    seed(app.db, FIXTURE)
    from whalewatcher.stats import load_trades
    s = summarise("all", load_trades(app), samples=2000)
    assert (s.alerts, s.filled, s.missed, s.settled, s.open) == (5, 4, 1, 3, 1)
    assert s.total_pnl == pytest.approx(2.5)               # +10 - 10 + 2.5
    assert s.total_cost == pytest.approx(30)
    assert s.roi == pytest.approx(2.5 / 30)
    assert s.win_rate == pytest.approx(2 / 3)              # pnl > 0: the win and the split
    assert s.mean_roi == pytest.approx((1.0 - 1.0 + 0.25) / 3)
    assert s.ci_low < s.mean_roi < s.ci_high and s.ci_includes_zero
    assert s.max_drawdown == pytest.approx(10)             # +10, then -10
    assert s.avg_clv == pytest.approx(((0.60 - 0.50) + (0.28 - 0.32) + (0.50 - 0.40)) / 3)
    assert s.avg_detection_delay == pytest.approx(40)
    assert s.avg_fill_delay == pytest.approx(101)
    assert s.avg_latency_cost == pytest.approx(((0.50 - 0.45) + (0.32 - 0.30) + (0.40 - 0.38) + (0.62 - 0.60)) / 4)
    # shadows over the 4 settled alerts (missed included)
    assert s.shadow_n == 4
    assert s.zero_pnl == pytest.approx(12.22 - 10 + 3.16 + 10.0)
    assert s.atask_pnl == pytest.approx(10.83 - 10 + 2.82 + 9.23)
    assert s.real_pnl_same_set == pytest.approx(2.5)
    assert s.miss_reasons == {"slippage": 1}


def test_output_leads_with_the_small_sample_warning_and_says_ci_includes_zero(app):
    seed(app.db, FIXTURE)
    text = render(app)
    assert text.splitlines()[0].startswith("⚠ NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: 3 settled")
    assert "includes zero" in text
    assert "n=3" in text, "every profit figure carries its sample size"
    for heading in ("By wallet:", "By category:", "By venue:", "By entry price:"):
        assert heading in text


def test_csv_export_has_every_trade(app, tmp_path):
    seed(app.db, FIXTURE)
    path = tmp_path / "out.csv"
    assert export_csv(app, str(path)) == 5
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 5 and rows[0]["wallet"] == "0xa" and rows[0]["pnl"] == "10.0"


def test_empty_database(app):
    assert "No paper trades yet." in render(app)


def test_latency_cost_is_measured_from_the_whale_price_the_shadow_uses_and_includes_misses(app):
    seed(app.db, FIXTURE)
    # Later fills moved the alerts' whale_price after the paper trades opened at zero_price.
    app.db.execute("UPDATE alerts SET whale_price = whale_price + 0.05")
    from whalewatcher.stats import load_trades
    s = summarise("all", load_trades(app), samples=200)
    # whale -> fill over the filled trades, against zero_price (0.45, 0.30, 0.38, 0.60): unchanged
    assert s.avg_latency_cost == pytest.approx(((0.50 - 0.45) + (0.32 - 0.30) + (0.40 - 0.38) + (0.62 - 0.60)) / 4)
    # whale -> alert-time ask over every alert with an ask, the missed one included
    assert s.detection_n == 5
    assert s.avg_detection_cost == pytest.approx(((0.48 - 0.45) + (0.31 - 0.30) + (0.39 - 0.38) + (0.52 - 0.50)
                                                 + (0.61 - 0.60)) / 5)
    assert s.avg_fill_cost == pytest.approx(((0.50 - 0.48) + (0.32 - 0.31) + (0.40 - 0.39) + (0.62 - 0.61)) / 4)


def test_in_play_fills_are_left_out_of_clv(app):
    seed(app.db, FIXTURE)
    # The sports market's game started before the fills (BASE + i*1000 + 101): in-play.
    app.db.execute("UPDATE poly_markets SET game_start_time = ? WHERE condition_id = '0xspo'", (BASE,))
    from whalewatcher.stats import load_trades
    s = summarise("all", load_trades(app), samples=200)
    assert s.clv_inplay_excluded == 1 and s.clv_n == 2, "the settled sports trade was in-play"
    assert s.avg_clv == pytest.approx(((0.60 - 0.50) + (0.50 - 0.40)) / 2)
    assert "1 in-play fills left out" in render(app)
