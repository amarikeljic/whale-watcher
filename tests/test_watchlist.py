"""v2 Phase 2: the persistent watchlist. Add, keep, remove after N failed reviews,
re-add, the size cap, probation on alerts, and history that cannot be deleted."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import DAY, NOW
from whalewatcher import watchlist as wl
from whalewatcher.config import Config, ConfigError, validate
from whalewatcher.db import SCHEMA_VERSION, Database
from whalewatcher.discord import MemorySink
from whalewatcher.http import HttpError
from whalewatcher.service import Service
from world import FakeMarket, World

GOOD, BAND, BAD = "0xaaaa", "0xbbbb", "0xcccc"


def setup(cfg):
    """Small numbers, same shape as production: entry asks for more than exit."""
    s = cfg.scoring
    s.min_avg_stake_usd, s.rank_by, s.min_clv_positions = 0, "roi", 1
    cfg.candidates.leaderboard_periods = ["month"]
    cfg.candidates.recent_trades_pages = 1
    e, x = cfg.watchlist.entry, cfg.watchlist.exit
    e.min_settled_markets, e.max_single_market_share, e.min_roi = 3, 100.0, 0.05
    x.min_settled_markets, x.max_single_market_share, x.min_roi = 2, 100.0, 0.0
    validate(cfg)


def bets(world: World, wallet: str, *, wins: int, losses: int, start: int, step: int = 3600,
         hold: int = 1800) -> None:
    """Settled $100 bets at 0.40: a win makes +$150, a loss -$100."""
    for i in range(wins + losses):
        cond = f"0x{wallet[2:6]}{start}{i}"
        opened = start + i * step
        world.add_market(FakeMarket(cond, resolved_at=opened + hold, payouts=[1.0, 0.0]))
        world.trade(wallet, opened, cond, index=0 if i < wins else 1, size=250, price=0.4, usdc=100)


def board(world: World, *wallets: str) -> None:
    world.leaderboard = [{"rank": i, "user_id": w, "pnl": 1000 - i, "volume": 1, "user_name": w}
                         for i, w in enumerate(wallets, start=1)]


def actions(review: wl.Review) -> dict[str, str]:
    return {d.wallet: d.action for d in review.decisions}


def rows(app, wallet: str):
    return app.db.query("SELECT * FROM watchlist_membership WHERE wallet = ? ORDER BY id", (wallet,))


def monitored(app) -> list[str]:
    return sorted(r["wallet"] for r in app.db.query("SELECT wallet FROM monitored_wallets WHERE active = 1"))


async def next_night(app, clock) -> wl.Review:
    clock.advance(DAY)
    return await wl.nightly(app)


# -- add -------------------------------------------------------------------------------


async def test_a_wallet_that_meets_the_entry_rules_is_added_with_its_reason_and_snapshot(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD, BAD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    bets(world, BAD, wins=0, losses=4, start=NOW - 20 * DAY)
    review = await wl.nightly(live_app)

    assert actions(review) == {GOOD: wl.ADD}                # a wallet that fails entry is simply not on the list
    (row,) = rows(live_app, GOOD)
    assert row["added_at"] == clock.now and row["removed_at"] is None
    assert row["added_run_id"] == review.run_id and "passed the entry rules" in row["add_reason"]
    assert row["probation_until"] == clock.now + live_app.cfg.watchlist.probation_days * DAY
    assert monitored(live_app) == [GOOD] and live_app.db.get_state("monitored_run_id") == review.run_id
    assert rows(live_app, BAD) == []
    # The snapshot was scored with the entry rules, and says who is on the list after it.
    snap = {r["wallet"]: r for r in live_app.db.query(
        "SELECT * FROM wallet_score_snapshots WHERE run_id = ?", (review.run_id,))}
    assert snap[GOOD]["selected"] == 1 and snap[BAD]["selected"] == 0 and snap[BAD]["passed"] == 0
    event = live_app.db.one("SELECT * FROM watchlist_events WHERE run_id = ?", (review.run_id,))
    assert (event["wallet"], event["action"], event["membership_id"]) == (GOOD, "ADD", row["id"])


# -- keep (hysteresis) -------------------------------------------------------------------


async def test_between_the_exit_and_entry_lines_a_wallet_is_not_added_but_a_member_is_kept(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD, BAND)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    bets(world, BAND, wins=5, losses=7, start=NOW - 20 * DAY)      # ROI +4.2%: above exit 0%, below entry 5%
    first = await wl.nightly(live_app)
    assert actions(first) == {GOOD: wl.ADD}, "4.2% is under the 5% entry line"

    # GOOD cools to the same +4.2%: now under the entry line, still over the exit line.
    bets(world, GOOD, wins=1, losses=7, start=first.as_of + 60, step=600, hold=300)
    second = await next_night(live_app, clock)
    assert actions(second) == {GOOD: wl.KEEP}
    (row,) = rows(live_app, GOOD)
    assert row["fail_streak"] == 0 and row["removed_at"] is None and row["last_review"] == "[]"
    assert row["last_review_run_id"] == second.run_id
    assert rows(live_app, BAND) == [] and monitored(live_app) == [GOOD]


# -- remove after N failed reviews ------------------------------------------------------------


async def test_a_member_is_removed_only_after_failing_reviews_in_a_row(live_app, fake_http, clock):
    setup(live_app.cfg)
    assert live_app.cfg.watchlist.exit.fail_reviews_in_a_row == 3
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    first = await wl.nightly(live_app)
    bets(world, GOOD, wins=0, losses=12, start=first.as_of + 60, step=600, hold=300)   # ROI goes negative

    one = await next_night(live_app, clock)
    assert actions(one) == {GOOD: wl.FAIL} and rows(live_app, GOOD)[0]["fail_streak"] == 1
    two = await next_night(live_app, clock)
    assert actions(two) == {GOOD: wl.FAIL} and rows(live_app, GOOD)[0]["fail_streak"] == 2
    assert monitored(live_app) == [GOOD], "still watched while it is only failing"
    three = await next_night(live_app, clock)
    assert actions(three) == {GOOD: wl.REMOVE}

    (row,) = rows(live_app, GOOD)
    assert row["removed_at"] == clock.now and row["removed_run_id"] == three.run_id
    assert "roi" in row["remove_reason"] and "failed 3 reviews in a row" in row["remove_reason"]
    assert monitored(live_app) == []
    assert three.before == 1 and three.after == 0


async def test_a_passing_review_resets_the_count(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    first = await wl.nightly(live_app)
    bets(world, GOOD, wins=0, losses=7, start=first.as_of + 60, step=600, hold=300)    # -$100 overall
    assert actions(await next_night(live_app, clock)) == {GOOD: wl.FAIL}
    assert actions(await next_night(live_app, clock)) == {GOOD: wl.FAIL}
    bets(world, GOOD, wins=4, losses=0, start=clock.now - DAY // 2 + 60, step=600, hold=300)   # back in profit
    assert actions(await next_night(live_app, clock)) == {GOOD: wl.KEEP}
    assert rows(live_app, GOOD)[0]["fail_streak"] == 0
    bets(world, GOOD, wins=0, losses=12, start=clock.now - DAY // 2 + 60, step=600, hold=300)
    assert actions(await next_night(live_app, clock)) == {GOOD: wl.FAIL}
    assert rows(live_app, GOOD)[0]["fail_streak"] == 1, "the count started again from one"


async def test_a_second_review_on_the_same_day_does_not_count_the_failure_twice(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    first = await wl.nightly(live_app)
    bets(world, GOOD, wins=0, losses=12, start=first.as_of + 60, step=600, hold=300)
    await next_night(live_app, clock)
    clock.advance(3600)
    again = await wl.nightly(live_app)          # e.g. `watchlist --review` by hand, same data day
    assert actions(again) == {GOOD: wl.FAIL} and rows(live_app, GOOD)[0]["fail_streak"] == 1


async def test_a_wallet_that_could_not_be_read_is_not_judged(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    await wl.nightly(live_app)
    served = fake_http.routes[("GET", "/v2/activity")]

    def down(params):
        if params["user"] == GOOD:
            raise HttpError("503", status=503, answered=True)
        return served(params)

    fake_http.route("/v2/activity", down)
    review = await next_night(live_app, clock)
    assert actions(review) == {GOOD: wl.SKIP}
    (row,) = rows(live_app, GOOD)
    assert row["fail_streak"] == 0 and row["removed_at"] is None and monitored(live_app) == [GOOD]


# -- re-add -------------------------------------------------------------------------------


async def test_a_removed_wallet_that_qualifies_again_gets_a_new_row(live_app, fake_http, clock):
    setup(live_app.cfg)
    live_app.cfg.watchlist.exit.fail_reviews_in_a_row = 1
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    first = await wl.nightly(live_app)
    bets(world, GOOD, wins=0, losses=7, start=first.as_of + 60, step=600, hold=300)
    removed = await next_night(live_app, clock)
    assert actions(removed) == {GOOD: wl.REMOVE}
    assert actions(await next_night(live_app, clock)) == {}, "still losing: not let back in"

    bets(world, GOOD, wins=8, losses=0, start=clock.now - DAY // 2 + 60, step=600, hold=300)
    back = await next_night(live_app, clock)
    assert actions(back) == {GOOD: wl.ADD}
    old, new = rows(live_app, GOOD)
    assert old["removed_at"] is not None and old["removed_run_id"] == removed.run_id, "the first stay is untouched"
    assert new["removed_at"] is None and new["added_run_id"] == back.run_id and new["id"] != old["id"]
    assert new["probation_until"] == clock.now + live_app.cfg.watchlist.probation_days * DAY, "probation starts over"
    assert wl.membership_at(live_app, GOOD, old["added_at"] + 1)["id"] == old["id"]
    assert wl.membership_at(live_app, GOOD, old["removed_at"] + 1) is None
    assert wl.membership_at(live_app, GOOD)["id"] == new["id"]
    text = wl.render_history(live_app)
    assert text.count("ADDED") == 2 and text.count("REMOVED") == 1 and "2 stays in total, 1 open now" in text


# -- the size cap -------------------------------------------------------------------------


async def test_the_list_never_grows_past_max_size_and_nobody_is_pushed_out(live_app, fake_http, clock):
    setup(live_app.cfg)
    live_app.cfg.watchlist.max_size = 2
    world = World(fake_http)
    board(world, GOOD, BAND, BAD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)       # ROI +150%
    bets(world, BAND, wins=4, losses=1, start=NOW - 20 * DAY)       # ROI +100%
    bets(world, BAD, wins=4, losses=2, start=NOW - 20 * DAY)        # ROI +67%, third by rank
    first = await wl.nightly(live_app)
    assert actions(first) == {GOOD: wl.ADD, BAND: wl.ADD, BAD: wl.WAIT}
    assert "full (2)" in first.of(wl.WAIT)[0].detail and first.after == 2

    # BAD becomes the best wallet of all: it still waits, because members only leave by failing.
    bets(world, BAD, wins=30, losses=0, start=first.as_of + 60, step=600, hold=300)
    second = await next_night(live_app, clock)
    assert actions(second) == {GOOD: wl.KEEP, BAND: wl.KEEP, BAD: wl.WAIT}
    assert monitored(live_app) == sorted([GOOD, BAND]) and rows(live_app, BAD) == []


# -- market makers and bots -----------------------------------------------------------------


async def test_a_market_maker_or_bot_is_turned_away_and_an_unreadable_check_waits(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD, BAND, BAD)
    for w in (GOOD, BAND, BAD):
        bets(world, w, wins=4, losses=0, start=NOW - 20 * DAY)
    world.user_stats[GOOD] = {"trades": 100, "all_time_pnl": {
        "trade_count": 900, "position_pnl": 50_000.0, "maker_rebate": 40_000.0, "reward_income": 10_000.0}}
    world.user_stats[BAND] = {"trades": 10, "all_time_pnl": {"trade_count": 5000, "position_pnl": 9000.0}}
    review = await wl.nightly(live_app)
    assert actions(review) == {GOOD: wl.REJECT, BAND: wl.REJECT, BAD: wl.ADD}      # BAD has no stats: unknown, allowed
    detail = {d.wallet: d.detail for d in review.decisions}
    assert "likely market maker: 50%" in detail[GOOD] and "likely bot: 500 fills per market" in detail[BAND]

    def broken(_params):
        raise HttpError("500", status=500, answered=True)

    fake_http.route("/v2/user-stats", broken)
    world.user_stats.clear()
    again = await next_night(live_app, clock)
    assert actions(again) == {GOOD: wl.WAIT, BAND: wl.WAIT, BAD: wl.KEEP}, "a failed check is not a pass"


def test_the_screen_uses_looser_limits_for_members_and_ignores_taker_rebates():
    stats = {"trades": 10, "all_time_pnl": {"trade_count": 1500, "position_pnl": 6000.0, "maker_rebate": 3000.0,
                                            "reward_income": 1000.0, "taker_rebate": 99_999.0}}
    screen = wl.screen_from_stats(stats)
    assert screen.rebate_share == pytest.approx(0.4) and screen.fills_per_market == 150
    c = Config().watchlist
    assert len(wl.screen_failures(screen, c.entry.max_rebate_share, c.entry.max_fills_per_market)) == 2
    assert wl.screen_failures(screen, c.exit.max_rebate_share, c.exit.max_fills_per_market) == []
    assert wl.screen_from_stats(None) == wl.Screen()


# -- our own record of the wallet ----------------------------------------------------------


def paper(app, wallet: str, n: int, *, clv: float, strategy: str = "hold_to_resolution", start_id: int = 0,
          inplay: bool = False) -> None:
    for i in range(start_id, start_id + n):
        cond = f"0xlive{wallet[2:6]}{i}"
        app.db.execute("INSERT INTO poly_markets (condition_id, event_slug, game_start_time) VALUES (?,?,?)",
                       (cond, f"ev-{cond}", NOW - 500 if inplay else None))
        cur = app.db.execute(
            "INSERT INTO alerts (wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, whale_shares, "
            "whale_usd, whale_price, fills, detected_at, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, cond, f"{cond}-t0", "NEW", NOW, NOW, 10, 5, 0.5, 1, NOW, "POSTED", NOW))
        app.db.execute(
            "INSERT INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
            "alert_ts, fill_ts, fill_price, closing_price, settled_at, pnl, cost, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cur.lastrowid, "polymarket", strategy, "FILLED", f"{cond}-t0", cond, 10, 1, NOW, NOW, 0.50,
             0.50 + clv, NOW, 0.0, 10.0, NOW))


async def test_live_clv_only_counts_once_there_are_enough_events(live_app, fake_http, clock):
    setup(live_app.cfg)
    x = live_app.cfg.watchlist.exit
    x.live_min_events = 5
    world = World(fake_http)
    board(world, GOOD, BAND)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    bets(world, BAND, wins=4, losses=0, start=NOW - 20 * DAY)
    await wl.nightly(live_app)

    paper(live_app, GOOD, 4, clv=-0.05)
    paper(live_app, GOOD, 50, clv=+0.30, strategy="backtest:walk-forward", start_id=100)   # simulated: never counts
    paper(live_app, GOOD, 50, clv=+0.30, start_id=200, inplay=True)                        # in-play: no closing line
    assert wl.live_record(live_app, GOOD) == (4, pytest.approx(-0.05))
    assert actions(await next_night(live_app, clock)) == {GOOD: wl.KEEP, BAND: wl.KEEP}, "4 events is noise"

    paper(live_app, GOOD, 1, clv=-0.05, start_id=4)
    review = await next_night(live_app, clock)
    assert actions(review) == {GOOD: wl.FAIL, BAND: wl.KEEP}
    assert "live CLV -0.050 over 5 settled events" in review.of(wl.FAIL)[0].detail


async def test_a_wallet_with_a_bad_live_record_is_not_let_back_in(live_app, fake_http, clock):
    setup(live_app.cfg)
    live_app.cfg.watchlist.exit.live_min_events = 5
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    paper(live_app, GOOD, 5, clv=-0.05)
    review = await wl.nightly(live_app)
    assert actions(review) == {GOOD: wl.REJECT} and "live CLV" in review.decisions[0].detail


# -- alerts remember what was true when they fired ---------------------------------------------


async def test_an_alert_records_its_membership_probation_and_snapshot(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    review = await wl.nightly(live_app)
    member = rows(live_app, GOOD)[0]
    svc = Service(live_app, MemorySink())

    async def alert_now(cond: str):
        world.add_market(FakeMarket(cond, end_date=clock.now + 30 * DAY))
        world.set_book(f"{cond}-t0", [(0.50, 5000)])
        live_app.db.execute("UPDATE monitored_wallets SET last_seen_ts = ?", (clock.now - 120,))
        world.trade(GOOD, clock.now - 10, cond, size=2000, price=0.49, usdc=980)
        await svc.monitor.poll_once()
        return live_app.db.one("SELECT * FROM alerts WHERE condition_id = ?", (cond,))

    early = await alert_now("0xlive1")
    assert (early["membership_id"], early["probation"], early["snapshot_run_id"]) == (member["id"], 1, review.run_id)
    assert "on probation" in early["message"]

    clock.advance(live_app.cfg.watchlist.probation_days * DAY + 60)
    late = await alert_now("0xlive2")
    assert (late["membership_id"], late["probation"]) == (member["id"], 0) and "on probation" not in late["message"]
    # The earlier alert keeps what was true when it fired.
    assert live_app.db.one("SELECT probation FROM alerts WHERE id = ?", (early["id"],))["probation"] == 1

    from whalewatcher.stats import BREAKDOWN_KEYS, load_bot_trades
    status = {t["condition_id"]: BREAKDOWN_KEYS["watchlist status"](t) for t in load_bot_trades(live_app)
              if t["strategy"] == "flat_hold"}
    assert status == {"0xlive1": "probation", "0xlive2": "established"}


# -- history is permanent, and the upgrade keeps every row ----------------------------------------


async def test_watchlist_history_cannot_be_deleted(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    await wl.nightly(live_app)
    for table in ("watchlist_membership", "watchlist_events"):
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            live_app.db.execute(f"DELETE FROM {table}")
        assert live_app.db.one(f"SELECT COUNT(*) n FROM {table}")["n"] == 1


V1_SCHEMA = """
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, condition_id TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT,
    kind TEXT NOT NULL, first_fill_ts INTEGER NOT NULL, last_fill_ts INTEGER NOT NULL, trigger_fill_ts INTEGER,
    whale_shares REAL NOT NULL, whale_usd REAL NOT NULL, whale_price REAL NOT NULL, fills INTEGER NOT NULL,
    detected_at INTEGER NOT NULL, posted_at INTEGER, status TEXT NOT NULL, skip_reason TEXT, alert_best_ask REAL,
    alert_best_bid REAL, alert_book_ts INTEGER, snapshot_run_id INTEGER, discord_message_id TEXT, message TEXT,
    updated_at INTEGER NOT NULL);
CREATE TABLE monitored_wallets (
    wallet TEXT PRIMARY KEY, run_id INTEGER NOT NULL, rank INTEGER, active INTEGER NOT NULL DEFAULT 1,
    added_at INTEGER NOT NULL, removed_at INTEGER, last_polled_at INTEGER, last_seen_ts INTEGER);
INSERT INTO alerts (id, wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, whale_shares, whale_usd,
    whale_price, fills, detected_at, status, snapshot_run_id, updated_at)
    VALUES (41, '0xold', '0xc', '0xc-t0', 'NEW', 100, 100, 10, 600, 0.6, 2, 130, 'POSTED', 6, 130);
INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) VALUES
    ('0xkeep', 6, 1, 1, 1000, 5555), ('0xgone', 5, 2, 0, 900, 4444);
PRAGMA user_version = 1;
"""


async def test_upgrading_a_v1_database_backs_it_up_first_and_keeps_every_row(tmp_path, cfg, fake_http, clock):
    from whalewatcher.app import open_app
    path = tmp_path / "data" / "whalewatcher.sqlite3"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(V1_SCHEMA)
    old.close()

    db = Database(str(path))
    assert db.backup_path and db.backup_path.endswith(f".pre-v{SCHEMA_VERSION}")
    assert db.backup_path.startswith(str(tmp_path / "data" / "backups" / "whalewatcher.sqlite3."))
    copy = sqlite3.connect(db.backup_path)       # the backup is the database exactly as it was
    assert copy.execute("SELECT wallet, whale_usd FROM alerts").fetchall() == [("0xold", 600.0)]
    assert "probation" not in [r[1] for r in copy.execute("PRAGMA table_info(alerts)")]
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
    copy.close()

    alert = db.one("SELECT * FROM alerts WHERE id = 41")
    assert (alert["wallet"], alert["whale_usd"], alert["snapshot_run_id"], alert["status"]) == ("0xold", 600.0, 6, "POSTED")
    assert alert["probation"] is None and alert["membership_id"] is None, "old alerts are not relabelled"
    assert db.one("PRAGMA user_version")[0] == SCHEMA_VERSION

    async with open_app(cfg, http=fake_http, db=db, clock=clock) as app:
        assert wl.seed_from_monitored(app) == 1 and wl.seed_from_monitored(app) == 0
        wl.sync_monitored(app, None)
        (member,) = app.db.query("SELECT * FROM watchlist_membership")
        assert (member["wallet"], member["added_at"], member["added_run_id"]) == ("0xkeep", 1000, 6)
        assert "carried over" in member["add_reason"]
        kept = app.db.one("SELECT * FROM monitored_wallets WHERE wallet = '0xkeep'")
        assert (kept["active"], kept["last_seen_ts"], kept["run_id"]) == (1, 5555, 6), "polling state survives"
    db.close()

    again = Database(str(path))                  # already v2: no second backup
    assert again.backup_path is None and len(list((tmp_path / "data" / "backups").iterdir())) == 1
    again.close()


async def test_a_carried_over_wallet_must_meet_the_entry_rules_at_its_first_review(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD, BAND, BAD)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    bets(world, BAND, wins=5, losses=7, start=NOW - 20 * DAY)      # +4.2%: between the exit and entry lines
    bets(world, BAD, wins=0, losses=4, start=NOW - 20 * DAY)       # fails the exit rules too
    for w in (GOOD, BAND, BAD):                                    # what v1 left behind
        live_app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at) "
                            "VALUES (?, 1, 1, 1, ?)", (w, NOW - 3 * DAY))
    review = await wl.nightly(live_app)
    assert actions(review) == {GOOD: wl.KEEP, BAND: wl.REMOVE, BAD: wl.FAIL}
    (band,) = rows(live_app, BAND)
    assert band["removed_at"] == clock.now and "does not meet the v2 entry rules: roi < 0.05" in band["remove_reason"]
    assert band["added_at"] == NOW - 3 * DAY, "its v1 stay is on record"
    assert monitored(live_app) == sorted([GOOD, BAD]), "a wallet failing the exit rules gets its three nights"

    # From the second review on, a carried-over wallet has the same protection as any member.
    bets(world, GOOD, wins=1, losses=7, start=review.as_of + 60, step=600, hold=300)     # GOOD drops to +4.2%
    assert actions(await next_night(live_app, clock))[GOOD] == wl.KEEP


def test_a_brand_new_database_needs_no_backup(tmp_path):
    db = Database(str(tmp_path / "new.sqlite3"))
    assert db.backup_path is None and not (tmp_path / "backups").exists()
    db.close()


# -- display and config ----------------------------------------------------------------------


async def test_the_cli_views_show_members_the_last_review_and_why(live_app, fake_http, clock):
    setup(live_app.cfg)
    world = World(fake_http)
    board(world, GOOD, BAND)
    bets(world, GOOD, wins=4, losses=0, start=NOW - 20 * DAY)
    bets(world, BAND, wins=4, losses=1, start=NOW - 20 * DAY)
    assert "empty" in wl.render_current(live_app) and "No watchlist review" in wl.render_last_review(live_app)
    first = await wl.nightly(live_app)
    bets(world, BAND, wins=0, losses=12, start=first.as_of + 60, step=600, hold=300)
    review = await next_night(live_app, clock)

    current = wl.render_current(live_app)
    assert "Watchlist: 2 of 40 wallets" in current
    assert "probation to" in current and "failing 1/3" in current and "Failing an exit rule" in current
    live = wl.render_review(review.run_id, review.as_of, review.before, review.after, review.max_size,
                            review.decisions)
    assert live == wl.render_last_review(live_app), "the stored events rebuild the same report"
    assert "on the list before: 2   added: 0   kept: 1   failing: 1   removed: 0   on the list now: 2 of 40" in live
    assert "KEPT (1)" in live and "FAILING (still on the list) (1)" in live and "failed review 1 of 3" in live


def test_entry_must_be_at_least_as_strict_as_exit():
    cfg = Config()
    validate(cfg)
    cfg.watchlist.exit.min_roi = cfg.watchlist.entry.min_roi + 0.01
    with pytest.raises(ConfigError, match="watchlist.entry.min_roi must be at least as strict"):
        validate(cfg)


def test_watchlist_config_reads_nested_sections_and_rejects_typos(tmp_path):
    from whalewatcher.config import load
    good = tmp_path / "a.yaml"
    good.write_text("watchlist:\n  max_size: 25\n  entry:\n    min_roi: 0.1\n  exit:\n    fail_reviews_in_a_row: 5\n")
    cfg = load(good, env={})
    w = cfg.watchlist
    assert (w.max_size, w.entry.min_roi, w.exit.fail_reviews_in_a_row, w.entry.min_settled_markets) == (25, 0.1, 5, 30)
    bad = tmp_path / "b.yaml"
    bad.write_text("watchlist:\n  entry:\n    min_roy: 0.1\n")
    with pytest.raises(ConfigError, match="unknown key.*watchlist.entry: min_roy"):
        load(bad, env={})
