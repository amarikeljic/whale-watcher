"""Part 4 #10 and #11: what the whale did around each alert, the wallet flags, the
report, and the three switches (off unless turned on)."""

from __future__ import annotations

import pytest

from conftest import DAY, NOW
from test_bot import COND, WHALE, YES, setup, trades
from whalewatcher import followup as fu
from whalewatcher.stats import render

NO = f"{COND}-t1"
HOUR = 3600


def fill(app, key: str, ts: int, *, token: str, side: str = "BUY", usd: float = 0.0, shares: float = 0.0,
         cond: str = COND, wallet: str = WHALE) -> None:
    app.db.execute(
        "INSERT INTO fill_events (fill_key, wallet, ts, condition_id, token_id, outcome, outcome_index, side, size, "
        "usdc_size, price, detected_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'feed')",
        (key, wallet, ts, cond, token, "No" if token.endswith("t1") else "Yes", 1 if token.endswith("t1") else 0,
         side, shares, usd, (usd / shares) if shares else 0.5, ts + 2))


def alert(app, *, at: int, usd: float = 1000.0, shares: float = 2000.0, wallet: str = WHALE, cond: str = COND,
          token: str = YES) -> int:
    cur = app.db.execute(
        "INSERT INTO alerts (wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, trigger_fill_ts, "
        "whale_shares, whale_usd, whale_price, fills, detected_at, status, updated_at) "
        "VALUES (?,?,?,'NEW',?,?,?,?,?,0.5,1,?,'RECORDED',?)",
        (wallet, cond, token, at, at, at, shares, usd, at + 3, at))
    return int(cur.lastrowid)


def row(app, alert_id: int):
    return dict(app.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,)))


async def test_the_follow_up_adds_the_other_side_both_ways_the_event_and_quick_sales(live_app, clock):
    app, at = live_app, NOW - 3 * HOUR
    app.db.execute("INSERT INTO poly_markets (condition_id, event_slug) VALUES (?, 'game'), ('0xdraw', 'game')",
                   (COND,))
    fill(app, "a", at - 2 * HOUR, token=NO, usd=300, shares=600)          # other side, before
    fill(app, "b", at - 2 * DAY, token=NO, usd=5000, shares=9000)         # other side, too long before
    fill(app, "c", at + 30 * 60, token=NO, usd=400, shares=800)           # other side, in the hour after
    fill(app, "d", at + 2 * HOUR, token=NO, usd=7000, shares=9000)        # other side, too late to count
    fill(app, "e", at + 10 * 60, token=YES, side="SELL", shares=1000)     # half sold back in 10 minutes
    fill(app, "f", at + 5 * 60, token="0xdraw-t0", usd=200, shares=400, cond="0xdraw")   # the draw, same game
    a = alert(app, at=at)
    assert fu.at_alert_time(app.db, row(app, a)) == pytest.approx(300.0), "what the bot could know"
    assert fu.update(app) == 1
    r = row(app, a)
    assert (r["opposite_before_usd"], r["opposite_after_usd"], r["related_usd"], r["sold_back_share"]) == (
        pytest.approx(300), pytest.approx(400), pytest.approx(200), pytest.approx(0.5))
    assert r["followed_at"] == clock.now
    assert fu.hedge_class(r, 0.5) == fu.HEDGED, "700 of 1,000 on the other side"
    assert fu.update(app) == 0, "done once"


async def test_an_alert_is_followed_up_only_once_its_hour_has_passed(live_app, clock):
    a = alert(live_app, at=NOW - 30 * 60)
    assert fu.update(live_app) == 0 and row(live_app, a)["followed_at"] is None
    clock.advance(HOUR)
    assert fu.update(live_app) == 1


def test_the_classes(live_app):
    base = {"followed_at": 1, "whale_usd": 1000.0}
    assert fu.hedge_class({**base, "opposite_before_usd": 0, "opposite_after_usd": 0}, 0.5) == fu.CLEAN
    assert fu.hedge_class({**base, "opposite_before_usd": 100, "opposite_after_usd": 0}, 0.5) == fu.PARTIAL
    assert fu.hedge_class({**base, "opposite_before_usd": 0, "opposite_after_usd": 500}, 0.5) == fu.HEDGED
    assert fu.hedge_class({**base, "followed_at": None}, 0.5) is None, "not until the hour has passed"


async def test_wallet_flags_need_enough_alerts_and_count_only_what_is_followed_up(live_app, clock):
    app, cfg = live_app, live_app.cfg.followup
    for k in range(5):
        at = NOW - (10 + k) * HOUR
        alert(app, at=at)
        if k < 3:                                            # both sides on 3 of 5
            fill(app, f"o{k}", at + 60, token=NO, usd=50, shares=100)
        if k == 0:                                           # sold back on 1 of 5 (20%)
            fill(app, f"s{k}", at + 60, token=YES, side="SELL", shares=1500)
    four = fu.by_wallet(app.db, cfg)
    assert four == {}, "nothing is followed up yet"
    fu.update(app)
    w = fu.by_wallet(app.db, cfg)[WHALE]
    assert (w.alerts, w.both_sides, w.hedged, w.sold_back) == (5, 3, 0, 1)
    assert w.both_sides_flag(cfg) and w.sold_back_flag(cfg)
    cfg.min_alerts = 6
    assert not w.both_sides_flag(cfg), "five alerts are not enough at 6"
    assert fu.by_wallet(app.db, cfg, until=NOW - 1) == {}, "followed up after that date: not known then"


async def test_the_report_lists_classes_flags_and_says_the_switches_are_off(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    fill(live_app, "h", NOW - 600, token=NO, usd=400, shares=700)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    assert all(t["status"] != "SKIPPED" for t in trades(live_app)), "switches off: copied as usual"
    (a,) = live_app.db.query("SELECT * FROM alerts WHERE status = 'RECORDED'")
    assert a["opposite_before_usd"] == pytest.approx(400), "measured when the alert fired"
    clock.advance(2 * HOUR)
    fu.update(live_app)
    text = render(live_app)
    assert "WHALE FOLLOW-UP" in text and "1 alerts followed up: clean 0 | partial 0" in text and "hedged 1" in text
    assert "known when we acted: on 1 of the 1 hedged alerts" in text
    assert "switches: all off" in text


async def test_the_hedged_switch_skips_an_alert_the_whale_had_already_hedged(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    fill(live_app, "h", NOW - 600, token=NO, usd=400, shares=700)        # $400 of No, ten minutes before
    live_app.cfg.followup.skip_hedged_alerts = True
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    rows = trades(live_app)
    assert rows and all(t["status"] == "SKIPPED" and t["skip_code"] == fu.SKIP_HEDGED for t in rows), \
        "every strategy, the placebo too: a control may not enter what the strategy may not"
    assert "bought $400 of the other side in the 24 hours before" in rows[0]["reason"]


async def test_the_wallet_switches_use_only_alerts_followed_up_before_the_signal(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http)
    for k in range(5):                                                   # five old alerts, all with both sides
        at = NOW - (30 + k) * HOUR
        alert(live_app, at=at, token=f"0xold{k}-t0", cond=f"0xold{k}")
        fill(live_app, f"o{k}", at + 60, token=f"0xold{k}-t1", usd=50, shares=100, cond=f"0xold{k}")
    live_app.cfg.followup.skip_both_sides_wallets = True
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    assert all(t["status"] != "SKIPPED" for t in trades(live_app)), "not followed up yet: nothing is known"
    fu.update(live_app)
    clock.advance(60)
    world.trade(WHALE, clock.now - 3, COND, size=1500, price=0.42, usdc=630, feed=True, tx="0xsecond")
    live_app.db.execute("UPDATE alerts SET first_fill_ts = first_fill_ts - 7200 WHERE condition_id = ?", (COND,))
    await monitor.poll_once()
    skipped = [t for t in trades(live_app) if t["status"] == "SKIPPED"]
    assert skipped and all(t["skip_code"] == fu.SKIP_BOTH_SIDES for t in skipped)
    assert "bought both sides on 5 of its 5 followed-up alerts" in skipped[0]["reason"]
