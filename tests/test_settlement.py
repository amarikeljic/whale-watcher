"""Part 4 #9, #12, #13 and #14: the payout time, holding against mirroring when the
whale sold first, how each market resolved, and the history of published fees."""

from __future__ import annotations

import json
import sqlite3

from conftest import DAY, NOW
from dashfix import A, Fixture, small
from test_bot import COND, WHALE, setup
from test_polymarket_us import us_raw
from whalewatcher import dashboard as dash
from whalewatcher.config import Config
from whalewatcher.db import Database
from whalewatcher.horizon import DELAYED, DISPUTED, FIFTY_FIFTY, NORMAL, resolution_type
from whalewatcher.polymarket import uma_history
from whalewatcher.polymarket_us import UsMarket, UsRowA
from whalewatcher.stats import render
from world import FakeMarket, World


def test_the_resolution_type_names_the_most_serious_first():
    late = dict(scheduled_end=NOW - 3 * DAY, resolved_at=NOW)
    assert resolution_type(payouts=[1.0, 0.0], scheduled_end=NOW, resolved_at=NOW + 3600) == NORMAL
    assert resolution_type(payouts=[1.0, 0.0], **late) == DELAYED
    assert resolution_type(payouts=[0.5, 0.5], **late) == FIFTY_FIFTY, "a void is worse than a delay"
    assert resolution_type(payouts=[0.5, 0.5], uma_statuses='["proposed", "disputed"]', **late) == DISPUTED
    assert resolution_type(payouts=[1.0, 0.0], scheduled_end=None, resolved_at=NOW) == NORMAL, "no schedule: not late"


def test_the_uma_history_is_read_as_gamma_sends_it():
    assert uma_history({"umaResolutionStatuses": '["proposed", "Disputed"]'}) == '["proposed", "disputed"]'
    assert uma_history({"umaResolutionStatuses": ["proposed"]}) == '["proposed"]'
    assert uma_history({}) == "[]" and uma_history({"umaResolutionStatuses": None}) == "[]"


async def settled(live_app, fake_http, clock, *, uma=None, payouts=(1.0, 0.0), late=False):
    world, monitor, bot = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await monitor.poll_once()
    m = world.markets[COND]
    m.resolved_at, m.payouts, m.uma_statuses = clock.now + 60, list(payouts), uma
    if late:
        live_app.db.execute("UPDATE bot_trades SET scheduled_end = ?", (clock.now - 2 * DAY,))
    clock.advance(700)
    await bot.settle()
    return bot, live_app.db.one("SELECT * FROM bot_trades WHERE strategy = 'flat_hold'")


async def test_settlement_records_when_we_saw_the_payout_and_how_the_market_resolved(live_app, fake_http, clock):
    _, t = await settled(live_app, fake_http, clock)
    assert t["closed_at"] == t["paid_at"] == clock.now and t["resolved_at"] == NOW + 60
    assert t["resolution_type"] == NORMAL


async def test_a_disputed_result_is_read_fresh_when_the_trade_settles(live_app, fake_http, clock):
    _, t = await settled(live_app, fake_http, clock, uma='["proposed", "disputed", "proposed"]')
    assert t["resolution_type"] == DISPUTED
    row = live_app.db.one("SELECT uma_statuses FROM poly_markets WHERE condition_id = ?", (COND,))
    assert json.loads(row["uma_statuses"]) == ["proposed", "disputed", "proposed"]


async def test_voided_and_late_markets_are_typed(live_app, fake_http, clock):
    _, t = await settled(live_app, fake_http, clock, payouts=(0.5, 0.5))
    assert t["resolution_type"] == FIFTY_FIFTY and t["payout_per_share"] == 0.5


async def test_a_late_market_is_delayed(live_app, fake_http, clock):
    _, t = await settled(live_app, fake_http, clock, late=True)
    assert t["resolution_type"] == DELAYED


async def test_trades_settled_before_types_existed_get_one_later(live_app, fake_http, clock):
    bot, t = await settled(live_app, fake_http, clock)
    live_app.db.execute("UPDATE bot_trades SET resolution_type = NULL")
    assert await bot.fill_resolution_types() >= 1
    assert live_app.db.one("SELECT resolution_type FROM bot_trades WHERE id = ?", (t["id"],))[0] == NORMAL
    assert await bot.fill_resolution_types() == 0, "nothing left to type"


def test_the_upgrade_names_the_payout_time_of_trades_already_closed(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    Fixture(db, NOW).trade(A, 3, pnl=2.0)
    db.execute("UPDATE bot_trades SET paid_at = NULL")
    closed = db.one("SELECT closed_at FROM bot_trades")["closed_at"]
    db.close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 10")          # as a database from before this batch
    raw.commit()
    raw.close()
    again = Database(path)
    assert again.one("SELECT paid_at FROM bot_trades")["paid_at"] == closed and again.schema_version == 11
    again.close()


async def test_a_changed_market_fee_is_kept_in_the_history(live_app, fake_http, caplog):
    world = World(fake_http)
    world.add_market(FakeMarket("0xfee", fee_rate=0.03, end_date=NOW + DAY))
    await live_app.markets.ensure(["0xfee"], refresh_after=0)
    assert live_app.db.query("SELECT * FROM fee_changes") == [], "first sight: nothing changed"
    world.markets["0xfee"].fee_rate = 0.05
    await live_app.markets.ensure(["0xfee"], refresh_after=-1)      # read again (same second in this test)
    (row,) = live_app.db.query("SELECT * FROM fee_changes")
    assert (row["venue"], row["market"], row["field"], row["old_value"], row["new_value"]) == (
        "polymarket", "0xfee", "rate", 0.03, 0.05)
    assert "fee change: market 0xfee" in caplog.text
    assert live_app.db.one("SELECT fee_rate FROM poly_markets WHERE condition_id = '0xfee'")[0] == 0.05


async def test_a_changed_us_fee_coefficient_is_kept_in_the_history(live_app):
    row_a = UsRowA(live_app)
    row_a._fee_seen(UsMarket.from_api(us_raw(fee=0.095)))
    row_a._fee_seen(UsMarket.from_api(us_raw(fee=0.095)))
    assert live_app.db.query("SELECT * FROM fee_changes") == []
    row_a._fee_seen(UsMarket.from_api(us_raw(fee=0.085)))
    (row,) = live_app.db.query("SELECT * FROM fee_changes")
    assert (row["venue"], row["field"], row["old_value"], row["new_value"]) == (
        "polymarket_us", "fee_coefficient", 0.095, 0.085)


async def test_holding_and_mirroring_are_compared_where_the_whale_sold_first(app):
    fx = Fixture(app.db, NOW)
    for days, hold, mirror in ((6, 5.0, 1.0), (5, -10.0, -2.0), (4, 3.0, 3.0)):
        alert_id = fx.trade(A, days, pnl=hold)
        fx.trade(A, days, strategy="flat_mirror", pnl=mirror, cond=f"0xc{fx.n}")
        app.db.execute("UPDATE bot_trades SET alert_id = ?, result = 'EXIT' WHERE id = (SELECT MAX(id) FROM bot_trades)",
                       (alert_id,))
    text = render(app)
    assert "on the 3 signals where the whale sold before resolution: hold -$2.00 | mirror (sell with it) +$2.00" in text
    assert "hold minus mirror, ROI per trade on the 3 both closed" in text


async def test_with_no_whale_exit_the_report_says_so(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 3, pnl=1.0)
    fx.trade(A, 3, strategy="flat_mirror", pnl=1.0)
    assert "the whale has not sold before the market resolved on any copied signal yet" in render(app)


def test_the_health_page_shows_settlement_and_fee_changes(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    fx = small(path, NOW)
    fx.db.execute("UPDATE bot_trades SET resolution_type = 'normal', paid_at = closed_at, "
                  "resolved_at = closed_at - 1200 WHERE closed_at IS NOT NULL")
    fx.db.execute("INSERT INTO fee_changes (seen_at, venue, market, field, old_value, new_value) "
                  "VALUES (?, 'polymarket', '0xc1', 'rate', 0.03, 0.05)", (NOW - DAY,))
    fx.db.close()
    html = dash.render(Config(database_path=path), path, "/health", {"outages": "include"}, now=NOW)[1]
    assert "Settlement" in html and "Payout recorded a median of 20 min after the market resolved" in html
    assert "Fee changes seen" in html and "0.03" in html and "0.05" in html
    edge = dash.render(Config(database_path=path), path, "/edge", {}, now=NOW)[1]
    assert "When the whale sold first" in edge
