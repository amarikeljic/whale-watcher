"""The loop: the nightly rescore reviews the watchlist, alerts link to the snapshot current
when they fired, outages are reported once, and the loop degrades. The watchlist rules
themselves are tested in test_watchlist.py."""

from __future__ import annotations

from conftest import DAY, NOW
from whalewatcher.service import HEARTBEAT_KEY, Service, rescore
from world import FakeMarket, World


def loosen(cfg):
    s = cfg.scoring
    s.min_settled_markets, s.min_profitable_months, s.max_single_market_share = 3, 1, 1.0
    s.min_avg_stake_usd, s.min_clv_positions, s.top_k, s.rank_by, s.min_roi = 10, 1, 1, "roi", 0.0
    cfg.candidates.leaderboard_periods = ["month"]
    cfg.candidates.recent_trades_pages = 1
    e, x = cfg.watchlist.entry, cfg.watchlist.exit
    e.min_settled_markets, e.max_single_market_share, e.min_roi = 3, 1.0, 0.0
    x.min_settled_markets, x.max_single_market_share, x.min_roi = 3, 1.0, 0.0
    x.fail_reviews_in_a_row = 1          # these tests are about the loop, not about hysteresis


def history(world: World, wallet: str, *, win: bool, start: int, n: int = 4, step: int = DAY,
            hold: int = DAY) -> None:
    """n settled markets opened every ``step`` from ``start``: all wins or all losses."""
    for k in range(n):
        cond = f"0x{wallet[2:6]}{start}{k}"
        opened = start + k * step
        world.add_market(FakeMarket(cond, resolved_at=opened + hold, payouts=[1.0, 0.0]))
        world.trade(wallet, opened, cond, index=0 if win else 1, size=250, price=0.4, usdc=100)


async def test_rescore_updates_the_watchlist_and_alerts_link_to_the_snapshot_in_force(live_app, fake_http, clock):
    loosen(live_app.cfg)
    world = World(fake_http)
    world.leaderboard = [{"rank": 1, "user_id": "0xaaaa", "pnl": 1000, "volume": 1, "user_name": "a"},
                         {"rank": 2, "user_id": "0xbbbb", "pnl": 900, "volume": 1, "user_name": "b"}]
    history(world, "0xaaaa", win=True, start=NOW - 20 * DAY)
    history(world, "0xbbbb", win=False, start=NOW - 20 * DAY)
    first = await rescore(live_app)            # as_of = today 00:00
    active = [r["wallet"] for r in live_app.db.query("SELECT wallet FROM monitored_wallets WHERE active = 1")]
    assert active == ["0xaaaa"]

    # During the following day 0xbbbb turns it around and 0xaaaa collapses.
    first_as_of = live_app.db.one("SELECT as_of FROM score_runs WHERE id = ?", (first,))["as_of"]
    history(world, "0xaaaa", win=False, start=first_as_of + 60, n=12, step=1800, hold=900)
    history(world, "0xbbbb", win=True, start=first_as_of + 60, n=12, step=1800, hold=900)
    clock.advance(DAY)
    second = await rescore(live_app)           # as_of = the next day 00:00
    assert second != first
    active = [r["wallet"] for r in live_app.db.query("SELECT wallet FROM monitored_wallets WHERE active = 1")]
    assert active == ["0xbbbb"]

    # An alert fired now carries the snapshot in force now.
    world.add_market(FakeMarket("0xlive", end_date=clock.now + 30 * DAY))
    world.set_book("0xlive-t0", [(0.50, 5000)])
    live_app.db.execute("UPDATE monitored_wallets SET last_seen_ts = ?", (clock.now - 120,))
    world.trade("0xbbbb", clock.now - 10, "0xlive", size=2000, price=0.49, usdc=980)
    svc = Service(live_app)
    await svc.monitor.poll_once()
    alert = live_app.db.one("SELECT * FROM alerts WHERE status = 'RECORDED'")
    assert alert["snapshot_run_id"] == second
    assert f"(snapshot #{second})" in alert["message"]


async def test_startup_rescore_counts_as_todays_rescore(live_app, fake_http, clock):
    loosen(live_app.cfg)
    world = World(fake_http)
    world.leaderboard = [{"rank": 1, "user_id": "0xaaaa", "pnl": 1000, "volume": 1, "user_name": "a"}]
    history(world, "0xaaaa", win=True, start=NOW - 20 * DAY)
    svc = Service(live_app)
    await svc.run(cycles=2)
    runs = live_app.db.one("SELECT COUNT(*) n FROM score_runs WHERE kind = 'nightly'")["n"]
    assert runs == 1, "no second rescore straight after the first poll"
    assert live_app.db.get_state(HEARTBEAT_KEY) == clock.now


class FlakyHttp:
    """Wraps FakeHttp and reports an outage on demand."""

    def __init__(self, inner):
        self.inner, self.problem = inner, None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def outage(self, host, **_):
        return self.problem


async def test_outages_are_reported_once_and_recovery_once(live_app, fake_http):
    World(fake_http)
    svc = Service(live_app)
    flaky = FlakyHttp(fake_http)
    live_app.http = flaky
    flaky.problem = "data-api.polymarket.com: 9 failed attempts over 12 min"
    await svc._report_outage()
    await svc._report_outage()
    flaky.problem = None
    await svc._report_outage()
    texts = list(svc.notices)
    assert len(texts) == 2 and "unreachable" in texts[0] and "answering again" in texts[1]


async def test_a_failing_wallet_does_not_stop_the_cycle(live_app, fake_http, clock):
    world = World(fake_http)
    for w in ("0xgood", "0xbad"):
        live_app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                            "VALUES (?, 1, 1, 1, ?, ?)", (w, NOW, NOW - 120))
    original = world._activity

    def activity(params):
        if params["user"] == "0xbad":
            raise RuntimeError("upstream hiccup")
        return original(params)

    fake_http.route("/v2/activity", activity)
    svc = Service(live_app)
    stats = await svc.monitor.poll_once()
    assert stats.errors == 1 and stats.wallets == 2


async def test_an_empty_monitored_set_is_announced_once(live_app, fake_http, clock):
    loosen(live_app.cfg)
    live_app.cfg.watchlist.entry.min_settled_markets = 999   # nobody can join
    world = World(fake_http)
    world.leaderboard = [{"rank": 1, "user_id": "0xaaaa", "pnl": 1000, "volume": 1, "user_name": "a"}]
    history(world, "0xaaaa", win=True, start=NOW - 20 * DAY)
    svc = Service(live_app)
    await svc.run(cycles=1)
    await svc._rescore()          # the next night: still nobody
    notices = [t for t in svc.notices if "no wallet meets the watchlist entry rules" in t]
    assert len(notices) == 1 and "nothing is being monitored" in notices[0]


async def test_a_busy_database_at_heartbeat_time_does_not_end_the_runner(live_app, fake_http, clock, monkeypatch):
    """Seen live on 2026-11-13: the nightly review (a child process) held the database
    longer than the busy timeout and the heartbeat write, outside any handler, killed the runner."""
    import sqlite3
    World(fake_http)
    live_app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                        "VALUES ('0xw', 1, 1, 1, ?, ?)", (NOW - 3600, NOW - 120))
    svc = Service(live_app)
    real, calls = live_app.db.set_state, []

    def locked(key, value):
        calls.append(key)
        if key == "heartbeat":
            raise sqlite3.OperationalError("database is locked")
        return real(key, value)

    monkeypatch.setattr(live_app.db, "set_state", locked)
    await svc.run(cycles=2)                                 # returns: it was not killed
    assert calls.count("heartbeat") == 2, "and it tried again on the next cycle"


def test_writers_wait_a_minute_for_a_busy_database_and_slow_statements_are_logged(tmp_path, caplog, monkeypatch):
    from whalewatcher import db as dbmod
    d = dbmod.Database(str(tmp_path / "x.sqlite3"))
    assert d.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
    monkeypatch.setattr(dbmod, "SLOW_STATEMENT_SECONDS", -1.0)
    with caplog.at_level("WARNING"):
        d.execute("SELECT 1")
    assert "a database statement took" in caplog.text and "SELECT 1" in caplog.text
    d.close()
