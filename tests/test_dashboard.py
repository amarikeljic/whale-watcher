"""v2 Phase 4: the dashboard shows the right numbers, with their n, and cannot write."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import NOW
from dashfix import A, B, small
from whalewatcher import bankroll
from whalewatcher import dashboard as dash
from whalewatcher.config import Config
from whalewatcher.db import Database


@pytest.fixture
def site(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    fx = small(path, NOW)
    fx.db.close()
    cfg = Config(database_path=path)

    def get(url: str, **query) -> str:
        return dash.render(cfg, path, url, query, now=NOW)[1]

    get.cfg, get.path = cfg, path
    return get


def test_overview_shows_the_hand_checked_totals_each_with_its_n(site):
    html = site("/")
    assert "+$2.50" in html and "+5.0%" in html and "60%" in html, "P&L, ROI and win rate of the five closed trades"
    assert "n=5 closed" in html and "on $50 staked" in html
    assert "9" in html and "5 closed · 1 open · 1 missed · 2 skipped" in html
    assert "$10.00" in html and "1 open positions, at cost" in html
    assert "Not enough data: 5 closed trades in this range (need at least 30)" in html
    assert "The interval includes zero" in html and "95% interval" in html
    assert "Where the money went" in html and "Costs paid" in html and "Every other figure on this dashboard is net" in html
    assert "Cumulative P&amp;L by strategy" in html and "Control: same outcome, later n=1" in html
    assert "Old rule: human delay n=1" in html, "the old paper trades are their own line"


def test_excluding_the_outage_window_drops_the_trade_made_inside_it(site):
    html = site("/", outages="exclude")
    assert "+$12.50" in html and "n=4 closed" in html and "75%" in html
    assert "+31.2%" in html, "12.50 on 40"


def test_the_date_range_and_wallet_filters_scope_every_number(site):
    assert "n=3 closed" in site("/", wallet=A) and "+$8.50" in site("/", wallet=A)
    assert "n=2 closed" in site("/", wallet=B) and "-$6.00" in site("/", wallet=B)
    last = site("/", range="7d")
    assert "n=5 closed" in last
    assert "n=0 closed" in site("/", range="custom", **{"from": "2020-01-01", "to": "2020-01-02"})
    assert "wk of" in site("/", group="week")


def test_another_strategy_is_a_different_account(site):
    html = site("/", strategy="single/placebo_late")
    assert "-$3.00" in html and "n=1 closed" in html
    old = site("/", strategy="old/hold_to_resolution")
    assert "+$2.00" in old and "n=1 closed" in old


def test_edge_page_clv_and_the_three_fill_comparison(site):
    html = site("/edge")
    assert "+0.010" in html and "n=2 pre-game fills" in html, "CLV: (+0.05, -0.03) / 2"
    assert "+0.007" in html and "n=3, every fill" in html, "5-min move: (+0.02, -0.01, +0.01) / 3"
    assert "n=3 closed signals" in html
    assert "+$11.00" in html and "+$8.50" in html and "-$1.00" in html, "zero latency, the bot, the old rule"
    assert "median 4.0s" in html


def test_wallets_page_and_a_wallet_page(site):
    html = site("/wallets")
    assert "established" in html and "removed" in html and "0xaaaa…0001" in html
    assert "Watchlist timeline" in html and "failed 3 reviews in a row" in html
    page = site(f"/wallet/{A}")
    assert "+$8.50" in page and "n=3 closed" in page and "40 days on the list" in page
    assert "Score history" in page and "+14.0%" in page and "#2" in page
    assert "price ran: filling $10 costs 0.560" in page


def test_trades_table_and_its_csv_export(site):
    html = site("/trades")
    assert "9 rows · page 1 of 1" in html and "SKIPPED" in html and "event exposure: $50.00" in html
    assert "too_long: closes in 40.0 days (limit 14)" in html
    assert "Download CSV" in html
    csv_text = site("/trades.csv")
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("id,key,status,reason,wallet") and len(lines) == 10
    assert sum("single/flat_hold" in line for line in lines) == 9
    assert len(site("/trades.csv", wallet=B).strip().splitlines()) == 3


def test_breakdowns_by_category_price_hour_and_event(site):
    html = site("/breakdowns")
    assert "By category" in html and "politics" in html and "+$10.00" in html, "politics: +6 and +4"
    assert "By entry price" in html and "0.4-0.6" in html
    assert "By hour of day (UTC)" in html and "11:00" in html and "By event" in html


def test_breakdowns_show_the_cutoff_and_the_holding_time_bands(site):
    html = site("/breakdowns")
    assert "Time-to-close cutoff" in html and "Skipped as too long" in html and "14 days" in html
    assert "1 of 5 (20%)" in html, "the +4 trade resolved two days after its scheduled end"
    assert "0 of 5 (0%)" in html, "none of the five was held longer than 14 days"
    assert "1 filled trades in this view were entered in markets the cutoff now refuses" in html
    assert "By holding time" in html and "By days to close when entered" in html
    assert "Under 1 day" in html and "1 to 7 days" in html and "still open" in html


def test_the_days_to_close_filter_scopes_every_number(site):
    week = site("/", close="1-7")
    assert "n=2 closed" in week and "+$10.00" in week, "+6 and +4"
    day = site("/", close="lt1")
    assert "n=3 closed" in day and "-$7.50" in day, "+12.50, -10 and -10"
    assert "n=0 closed" in site("/", close="gt14"), "the open trade and the skipped signal"
    assert "n=5 closed" in site("/", close="nonsense"), "an unknown value is ignored"
    assert 'name="close"' in week and 'value="1-7" selected' in week


# -- the simulated small wallet -------------------------------------------------------------------


def test_the_wallet_page_replays_the_fixture_to_the_hand_checked_equity(site):
    """$100, 1.5% on-ramp, 8% of equity a trade, every fill at 0.50, fee inside the stake
    (table rates: sports 0.05, politics 0.04, crypto 0.07; a share costs 0.5125 / 0.51 / 0.5175).
    An open position is worth its shares at the price paid, so the fee is gone at once.

      start                                                        cash  98.5000
      6 days ago  sports, wins     stake 7.8800  15.3756 shares -> cash 105.9956
      5 days ago  sports, loses    stake 8.4796                 -> cash  97.5160
      4 days ago  politics         stake 7.8013  15.2966 shares    equity 97.3630 (fee 0.1530 gone)
      4 days ago  politics         stake 7.7890  15.2726 shares    equity 97.2103
      3 days ago  crypto, loses    stake 7.7768                 -> equity 89.4334 (the low)
      2 days ago  30 days to close: skipped (too_long). Another did not fill.
      1 day ago   both politics win, +30.5692                   -> cash 104.7181
      1 day ago   a signal the $10 strategy skipped: taken at the best ask, stake 8.3774, 16.3462 shares
      1 day ago   40 days to close: skipped (too_long)
      now         cash 96.3406 + 16.3462 shares at 0.50 = equity 104.5137
    """
    html = site("/bankroll")
    assert "$100.00 wallet" in html and "Replay of every signal" in html
    assert "$104.51" in html and "cash $96.34 free" in html and "+4.5%" in html
    assert "$16.56" in html and "15.6% below the high before it" in html, "from 105.9956 down to 89.4334"
    assert "6 / 2" in html and "of 9 signals · 1 did not fill" in html
    assert "3 won · 1 open" in html
    assert "Not enough data: 5 settled wallet trades (need at least 30)" in html
    assert "1 of 6 fills are priced from a smaller order or from the best ask" in html
    assert "too_long" in html and "The market was scheduled to end beyond the time-to-close cutoff." in html
    assert 'data-kind="wallet"' in html and "Tied up in positions" in html and "Skipped signals" in html
    assert "With and without the time-to-close cutoff" in html and "Old rule (v1), human delay" in html
    assert "The runner has not started a wallet run yet" in html
    assert 'action="/settings/wallet-amount"' in html and 'value="100.00"' in html


def test_the_wallet_page_follows_the_saved_amount_and_shows_runs(site):
    db = Database(site.path)
    app = SimpleNamespace(db=db, cfg=site.cfg, clock=lambda: NOW - 3 * 86_400)
    bankroll.sync_runs(app, ["single"])                         # a run that began three days ago
    bankroll.save_amount(db, 1000, "cli", NOW - 60)
    db.close()
    html = site("/bankroll")
    assert "$1,000.00 wallet" in html and "$1,045.14" in html, "ten times the wallet, the same path"
    assert "6 of 6 fills are priced from a smaller order" in html, "$80 stakes priced at what $10 paid"
    assert "Live wallet" in html and "Run #1: $100.00 from" in html
    assert "The amount was changed: the runner will end this run shortly." in html
    assert "Wallet runs" in html and "running" in html
    old = site("/bankroll", src="old")
    assert "Old rule (v1), human delay" in old and "Live wallet" not in old.split("Replay of every signal")[0].split("</nav>")[-1]
    assert "Saved. The runner ends" in site("/bankroll", saved="250.00")
    assert "Not saved: the wallet amount must be more than $0." in site("/bankroll", error="the wallet amount must be more than $0")
    assert "&lt;script&gt;" in site("/bankroll", error="<script>alert(1)</script>"), "an error message is escaped"


async def test_the_amount_form_is_the_only_thing_the_site_can_write(site):
    client = TestClient(TestServer(dash.make_app(site.cfg, site.path)))
    await client.start_server()
    same = {"Origin": f"http://{client.host}:{client.port}"}

    async def post(amount, headers=same):
        return await client.post("/settings/wallet-amount", data={"amount": amount}, headers=headers,
                                 allow_redirects=False)

    def saved():
        db = Database.readonly(site.path)
        try:
            return bankroll.wallet_amount(db), db.one("SELECT COUNT(*) n FROM settings")["n"]
        finally:
            db.close()

    try:
        ok = await post("250")
        assert ok.status == 303 and ok.headers["Location"] == "/bankroll?saved=250.00"
        assert saved() == (250.0, 1)
        page = await client.get("/bankroll?saved=250.00")
        assert "$250.00 wallet" in await page.text()

        for bad, why in (("0", "more+than"), ("-1", "more+than"), ("abc", "must+be+a+number"), ("", "must+be+a+number"),
                         ("100000.01", "cannot+be+more+than"), ("1.234", "two+decimals")):
            r = await post(bad)
            assert r.status == 303 and r.headers["Location"].startswith("/bankroll?error=") and why in r.headers["Location"]
        assert saved() == (250.0, 1), "a refused amount changes nothing"

        assert (await post("999", {"Origin": "https://evil.example"})).status == 403, "another site's form"
        assert (await post("999", {})).status == 403, "no word on where the form came from"
        assert (await post("300", {"Sec-Fetch-Site": "same-origin"})).status == 303
        assert (await post("999", {"Sec-Fetch-Site": "cross-site"})).status == 403
        assert saved() == (300.0, 1)

        for route in ("/", "/bankroll", "/trades", "/settings/other"):
            assert (await client.post(route, headers=same)).status in (404, 405), f"{route} accepts no write"
        assert (await client.get("/settings/wallet-amount")).status == 405
        big = await client.post("/settings/wallet-amount", data={"amount": "1" * 10_000}, headers=same,
                                allow_redirects=False)
        assert big.status == 413, "an oversized form is refused before it is read"
    finally:
        await client.close()


def test_the_settings_writer_cannot_touch_any_other_table(site):
    dash.SettingsWriter.save_wallet_amount(site.path, 400, NOW)
    conn = dash.SettingsWriter.connect(site.path)
    try:
        for sql in ("DELETE FROM bot_trades", "UPDATE bot_trades SET pnl = 999", "DELETE FROM settings",
                    "INSERT INTO service_state (key, value) VALUES ('x', '1')", "DROP TABLE alerts",
                    "UPDATE bankroll_runs SET amount = 1", "SELECT * FROM alerts", "CREATE TABLE x (y)",
                    "PRAGMA writable_schema = 1", "ATTACH DATABASE ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
                conn.execute(sql).fetchall()
        conn.rollback()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        dash.SettingsWriter.save_wallet_amount(site.path, -5, NOW)
    db = Database.readonly(site.path)
    assert bankroll.wallet_amount(db) == 400.0 and db.one("SELECT COUNT(*) n FROM bot_trades")["n"] == 10
    assert db.one("SELECT source FROM settings")["source"] == "dashboard"
    db.close()


def test_health_page_shows_delays_heartbeats_and_the_outage(site):
    html = site("/health")
    assert "4s ago" in html and "3s ago" in html, "runner and detector heartbeats"
    assert "ISP outage" in html and "2.0" in html
    assert "3.0s" in html and "n=10 alerts" in html, "ten alerts: seven at 3 s, two at 4 s, one at 5 s"
    assert "snapshot #2" in html


def test_trader_supplied_text_is_escaped(site):
    db = sqlite3.connect(site.path)
    db.execute("UPDATE poly_markets SET question = '<script>alert(1)</script>'")
    db.commit()
    db.close()
    html = site("/trades")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_dashboard_cannot_write(site):
    db = Database.readonly(site.path)
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|attempt to write"):
        db.execute("DELETE FROM bot_trades")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO service_state (key, value) VALUES ('x', '1')")
    assert db.one("SELECT COUNT(*) n FROM bot_trades")["n"] == 10
    db.close()


def test_an_old_or_missing_database_gets_a_plain_message(tmp_path):
    cfg = Config(database_path=str(tmp_path / "none.sqlite3"))
    assert "cannot be opened yet" in dash.render(cfg, cfg.database_path, "/", {}, now=NOW)[1]
    old = sqlite3.connect(tmp_path / "old.sqlite3")
    old.execute("CREATE TABLE alerts (id INTEGER)")
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()
    assert "needs v" in dash.render(cfg, str(tmp_path / "old.sqlite3"), "/", {}, now=NOW)[1]


async def test_the_server_answers_get_only_and_sends_a_strict_policy(site):
    client = TestClient(TestServer(dash.make_app(site.cfg, site.path)))
    await client.start_server()
    try:
        page = await client.get("/")
        assert page.status == 200 and "Whale Watcher" in await page.text()
        assert "default-src 'none'" in page.headers["Content-Security-Policy"]
        assert page.headers["Cache-Control"] == "no-store"
        assert (await client.post("/")).status == 405, "nothing accepts a write"
        assert (await client.get("/assets/app.css")).status == 200 and (await client.get("/assets/app.js")).status == 200
        assert (await client.get("/healthz")).status == 200 and (await client.get("/nope")).status == 404
        export = await client.get("/trades.csv")
        assert export.headers["Content-Type"].startswith("text/csv") and "attachment" in export.headers["Content-Disposition"]
    finally:
        await client.close()


def test_it_refuses_to_listen_beyond_loopback_unless_told_it_is_in_a_container():
    with pytest.raises(SystemExit, match="tailnet only"):
        dash.serve(Config(), host="0.0.0.0", port=8787, in_container=False)
