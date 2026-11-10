"""The audit rebuilds each settled paper trade by hand and flags what does not add up."""

from __future__ import annotations

import pytest

from conftest import NOW
from whalewatcher.audit import render, run_audit

COND = "0xm"


def seed(app, *, zero_price, fills=((1000, 0.40, 405.0), (1000, 0.42, 425.25)), token="0xm-t0", status="FILLED",
         pnl_tamper=0.0, contracts=None, game_start=None, closing=None, ask=0.43, payout=1.0):
    app.db.execute(
        "INSERT OR IGNORE INTO poly_markets (condition_id, question, category, outcomes, token_ids, closed, resolved, "
        "payouts, game_start_time, fees_enabled, fee_rate, fee_exponent, meta_fetched_at) "
        "VALUES (?, 'Will A win?', 'sports', '[\"Yes\",\"No\"]', '[\"0xm-t0\",\"0xm-t1\"]', 1, 1, ?, ?, 0, 0, 1, 1)",
        (COND, f"[{payout}, {1 - payout}]", game_start))
    cur = app.db.execute(
        "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, trigger_fill_ts, "
        "whale_shares, whale_usd, whale_price, fills, detected_at, posted_at, status, alert_best_ask, alert_best_bid, "
        "updated_at) VALUES ('0xw', ?, '0xm-t0', 'Yes', 'NEW', ?, ?, ?, 2000, 830.25, ?, 2, ?, ?, 'UNSENT', ?, ?, ?)",
        (COND, NOW - 100, NOW - 50, NOW - 50, zero_price, NOW, NOW, ask, ask - 0.01, NOW))
    alert = int(cur.lastrowid)
    for k, (size, price, usdc) in enumerate(fills):
        key = f"k{alert}-{k}"
        app.db.execute(
            "INSERT INTO wallet_trades (wallet, ts, type, condition_id, token_id, outcome, outcome_index, side, size, "
            "usdc_size, price, fill_key, seen_at) VALUES ('0xw', ?, 'TRADE', ?, '0xm-t0', 'Yes', 0, 'BUY', ?, ?, ?, ?, ?)",
            (NOW - 100 + 50 * k, COND, size, usdc, price, key, NOW))
        app.db.execute("INSERT INTO alert_fills (alert_id, fill_key) VALUES (?, ?)", (alert, key))
    stake = 10.0
    zc, ac = stake / zero_price, stake / ask
    filled = status == "FILLED"
    fill, n = 0.44, (contracts if contracts is not None else (stake / 0.44 if filled else None))
    cost = fill * n if n is not None else None
    app.db.execute(
        "INSERT INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
        "alert_ts, alert_ask, fill_ts, fill_price, contracts, fees, cost, zero_price, zero_contracts, zero_fees, "
        "zero_cost, atask_price, atask_contracts, atask_fees, atask_cost, settled_at, settle_result, payout_per_share, "
        "payout, pnl, zero_pnl, atask_pnl, closing_price, updated_at) "
        "VALUES (?, 'polymarket', 'hold_to_resolution', ?, ?, ?, 10, 1, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, 10, ?, ?, 0, 10, "
        "?, 'WIN', ?, ?, ?, ?, ?, ?, ?)",
        (alert, status, token, COND, NOW, ask, NOW + 60, fill if filled or contracts else None, n, cost,
         zero_price, zc, ask, ac, NOW + 3600, payout, n * payout if n else None,
         ((n * payout - cost) if filled else 0.0) + pnl_tamper, zc * payout - 10, ac * payout - 10, closing, NOW))
    return alert


def flags(app):
    return [f for c in run_audit(app).checks for f in c.flags]


async def test_a_consistent_trade_raises_no_flag(app):
    seed(app, zero_price=0.41)
    rep = run_audit(app)
    (c,) = rep.checks
    assert c.flags == []
    assert c.exec_vwap == pytest.approx(0.41) and c.usdc_vwap == pytest.approx(830.25 / 2000)
    assert c.methods["zero-latency"]["pnl_hand"] == pytest.approx(10 / 0.41 - 10)
    assert c.methods["real fill"]["pnl_hand"] == pytest.approx(10 / 0.44 - 10)


async def test_a_whale_price_with_the_whales_fee_in_it_is_flagged(app):
    seed(app, zero_price=830.25 / 2000)
    assert any("USDC per share" in f and "pays a fee twice" in f for f in flags(app))


async def test_wrong_pnl_tokens_and_missed_positions_are_flagged(app):
    seed(app, zero_price=0.41, pnl_tamper=3.0)
    seed(app, zero_price=0.41, token="0xm-t1")
    seed(app, zero_price=0.41, status="MISSED", contracts=5.0)
    found = flags(app)
    assert any(f.startswith("real fill: stored P&L") for f in found)
    assert any("differs from the alert's token" in f for f in found)
    assert any(f == "MISSED trade holds contracts" for f in found)


async def test_clv_against_a_pre_game_price_for_an_in_play_fill_is_flagged(app):
    seed(app, zero_price=0.41, game_start=NOW - 3600, closing=0.35)
    assert any(f.startswith("CLV compares an in-play fill") for f in flags(app))


async def test_the_gap_between_zero_latency_and_alert_time_is_taken_apart(app):
    seed(app, zero_price=0.41, ask=0.39)        # the ask was below the whale's price and the trade won
    seed(app, zero_price=0.41, ask=0.45)
    text = render(run_audit(app))
    assert "alert-time ask below the whale's price on 1 of 2 alerts" in text
    assert "Showing 2" in text and "largest contributors" in text


async def test_monitored_wallets_show_the_min_roi_their_run_used(app):
    import json
    from whalewatcher.audit import monitored_wallets
    app.db.execute("INSERT INTO score_runs (id, as_of, kind, created_at, config_json, sources_json, n_candidates, n_passed) "
                   "VALUES (6, ?, 'nightly', ?, ?, '{}', 10, 2)", (NOW, NOW, json.dumps({"min_roi": -1.0})))
    for wallet, roi in (("0xlow", 0.01), ("0xhigh", 0.2)):
        app.db.execute("INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, settled_markets, roi, passed, selected) "
                       "VALUES (6, ?, ?, 40, ?, 1, 1)", (wallet, NOW, roi))
        app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at) VALUES (?, 6, 1, 1, ?)",
                       (wallet, NOW))
    ws = monitored_wallets(app)
    assert [(w["wallet"], w["min_roi_used"]) for w in ws] == [("0xlow", -1.0), ("0xhigh", -1.0)]
    text = render(run_audit(app), ws)
    assert "+1.0%" in text and "-1.00" in text, "a 1% wallet, selected by a run whose min_roi was off"
