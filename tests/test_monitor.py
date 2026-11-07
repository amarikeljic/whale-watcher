"""Phase 2: detection, grouping, dedupe, skip rules, timestamps, and the hand-off to paper trading."""

from __future__ import annotations

import pytest

from conftest import NOW
from whalewatcher.discord import MemorySink, NullSink
from whalewatcher.monitor import Monitor, rotate_monitored
from whalewatcher.paper import PaperEngine
from world import FakeMarket, World

WHALE = "0xwhale"
COND = "0xm"
YES, NO = f"{COND}-t0", f"{COND}-t1"


def setup(app, fake_http, *, sink=None, book_asks=((0.44, 1000), (0.45, 1000))):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 30 * 86400, question="Will A beat B?"))
    world.set_book(YES, list(book_asks), [(0.42, 500)])
    world.set_book(NO, [(0.58, 1000)], [(0.55, 500)])
    app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                   "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    sink = sink if sink is not None else MemorySink()
    monitor = Monitor(app, sink, paper=PaperEngine(app))
    return world, monitor, sink


def alerts(app, status=None):
    sql = "SELECT * FROM alerts WHERE status != 'BELOW_THRESHOLD'"
    if status:
        sql += f" AND status = '{status}'"
    return app.db.query(sql + " ORDER BY id")


async def test_a_large_buy_posts_one_alert_with_all_timestamps(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    stats = await monitor.poll_once()
    assert stats.alerts == 1 and len(sink.posts) == 1
    (a,) = alerts(live_app)
    assert a["status"] == "POSTED" and a["kind"] == "NEW"
    assert a["first_fill_ts"] == NOW - 20, "whale trade time"
    assert a["detected_at"] == NOW and a["posted_at"] == NOW, "detection and post time"
    assert a["alert_best_ask"] == 0.44
    text = sink.posts[0][1]
    assert "bought YES" in text and "Whale price: 0.420" in text and "Current best ask: 0.440" in text
    assert "$630" in text and "Will A beat B?" in text and "polymarket.com/event/" in text


async def test_small_fills_add_up_inside_the_window_into_one_alert(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 50, COND, size=500, price=0.42, usdc=210)
    await monitor.poll_once()
    assert alerts(live_app) == [] and sink.posts == [], "$210 is below the $500 threshold"
    clock.advance(60)
    world.trade(WHALE, NOW + 30, COND, size=1000, price=0.43, usdc=430)
    await monitor.poll_once()
    (a,) = alerts(live_app)
    assert a["fills"] == 2 and a["whale_usd"] == pytest.approx(640)
    assert a["whale_price"] == pytest.approx(640 / 1500)
    assert a["first_fill_ts"] == NOW - 50 and a["trigger_fill_ts"] == NOW + 30
    assert len(sink.posts) == 1


async def test_later_fills_in_the_window_edit_the_message_instead_of_posting(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    await monitor.poll_once()
    clock.advance(60)
    world.trade(WHALE, NOW + 40, COND, size=2000, price=0.43, usdc=860)
    await monitor.poll_once()
    assert len(sink.posts) == 1 and len(sink.edits) == 1
    assert "$1,490 (2 fills)" in sink.edits[0][1]
    assert len(alerts(live_app)) == 1


async def test_overlapping_polls_never_duplicate(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    for _ in range(4):
        await monitor.poll_once()
        clock.advance(60)
    assert len(sink.posts) == 1 and sink.edits == [] and len(alerts(live_app)) == 1
    assert live_app.db.one("SELECT COUNT(*) n FROM paper_trades")["n"] == 1


async def test_a_buy_after_the_window_is_a_new_alert(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    await monitor.poll_once()
    clock.advance(400)
    world.trade(WHALE, NOW + 390, COND, size=1500, price=0.43, usdc=645)
    await monitor.poll_once()
    kinds = [a["kind"] for a in alerts(live_app)]
    assert len(sink.posts) == 2 and kinds == ["NEW", "ADD"], "the second buy adds to a held position"


async def test_yes_and_no_are_separate_groups_and_sells_never_alert(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 30, COND, index=0, size=1500, price=0.42, usdc=630)
    world.trade(WHALE, NOW - 25, COND, index=1, size=1000, price=0.56, usdc=560)
    world.trade(WHALE, NOW - 20, COND, index=0, side="SELL", size=5000, price=0.42, usdc=2100)
    await monitor.poll_once()
    assert sorted(a["token_id"] for a in alerts(live_app)) == [YES, NO]


async def test_extreme_prices_are_skipped_but_stored_with_the_reason(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http, book_asks=((0.97, 5000),))
    world.trade(WHALE, NOW - 20, COND, size=1000, price=0.96, usdc=960)
    stats = await monitor.poll_once()
    (a,) = alerts(live_app)
    assert a["status"] == "SKIPPED" and "outside 0.05-0.95" in a["skip_reason"]
    assert a["detected_at"] == NOW and a["posted_at"] is None
    assert sink.posts == [] and stats.skipped == 1
    assert live_app.db.one("SELECT COUNT(*) n FROM paper_trades")["n"] == 0, "skipped alerts are not traded"


async def test_closed_markets_are_skipped(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.markets[COND].accepting_orders = False
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    await monitor.poll_once()
    assert alerts(live_app)[0]["skip_reason"] == "market closed" and sink.posts == []


async def test_fills_that_surface_late_are_skipped_as_stale(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    live_app.db.execute("UPDATE monitored_wallets SET last_seen_ts = ?", (NOW - 2500,))
    world.trade(WHALE, NOW - 2000, COND, size=1500, price=0.40, usdc=600)   # 33 minutes old
    await monitor.poll_once()
    (a,) = alerts(live_app)
    assert a["status"] == "SKIPPED" and a["skip_reason"].startswith("stale") and sink.posts == []


async def test_without_a_webhook_alerts_are_unsent_and_still_paper_traded(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, sink=NullSink())
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    await monitor.poll_once()
    (a,) = alerts(live_app)
    assert a["status"] == "UNSENT" and a["posted_at"] is None
    assert live_app.db.one("SELECT status FROM paper_trades")["status"] == "PENDING"


class FailingSink(MemorySink):
    async def post(self, content):
        return None


async def test_a_failed_post_is_recorded_and_not_traded(live_app, fake_http, clock):
    world, monitor, _ = setup(live_app, fake_http, sink=FailingSink())
    world.trade(WHALE, NOW - 20, COND, size=1500, price=0.42, usdc=630)
    await monitor.poll_once()
    assert alerts(live_app)[0]["status"] == "FAILED"
    assert live_app.db.one("SELECT COUNT(*) n FROM paper_trades")["n"] == 0


async def test_rotation_follows_the_new_snapshot(live_app):
    db = live_app.db
    db.execute("INSERT INTO score_runs (id, as_of, kind, created_at, config_json, sources_json, n_candidates, n_passed) "
               "VALUES (7, 0, 'nightly', 0, '{}', '{}', 3, 2)")
    for w, rank, sel in (("0xa", 1, 1), ("0xb", 2, 1), ("0xc", 3, 0)):
        db.execute("INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, passed, rank, selected) "
                   "VALUES (7, ?, 0, 1, ?, ?)", (w, rank, sel))
    db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at) VALUES ('0xold', 1, 1, 1, 0)")
    added, removed = rotate_monitored(live_app, 7)
    assert added == ["0xa", "0xb"] and removed == ["0xold"]
    active = [r["wallet"] for r in db.query("SELECT wallet FROM monitored_wallets WHERE active = 1 ORDER BY rank")]
    assert active == ["0xa", "0xb"] and db.get_state("monitored_run_id") == 7


async def test_score_line_says_when_a_wallet_could_not_be_scored(live_app, fake_http, clock):
    from whalewatcher.monitor import score_line
    db = live_app.db
    db.execute("INSERT INTO score_runs (id, as_of, kind, created_at, config_json, sources_json, n_candidates, n_passed) "
               "VALUES (9, 0, 'nightly', 0, '{}', '{}', 2, 1)")
    db.execute("INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, settled_markets, passed, fail_reasons) "
               "VALUES (9, '0xbot', 0, 0, 0, '[\"too_active(>20000 rows: bot or market maker)\"]')")
    db.execute("INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, settled_markets, roi, clv, profitable_months, "
               "months_in_window, passed, fail_reasons) VALUES (9, '0xok', 0, 71, 0.18, 0.06, 5, 6, 1, '[]')")
    assert "not scored (too active to score)" in score_line(live_app, "0xbot", 9)
    assert score_line(live_app, "0xok", 9).startswith("Wallet score: CLV +0.06 | ROI 18% | 71 settled | 5/6")


async def test_groups_that_never_reach_the_threshold_are_pruned_after_their_window(live_app, fake_http, clock):
    world, monitor, sink = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 20, COND, size=100, price=0.42, usdc=42)
    await monitor.poll_once()
    assert live_app.db.one("SELECT COUNT(*) n FROM alerts WHERE status = 'BELOW_THRESHOLD'")["n"] == 1
    clock.advance(700)
    await monitor.poll_once()
    assert live_app.db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0
    assert live_app.db.one("SELECT COUNT(*) n FROM wallet_trades")["n"] == 1, "the fill itself is kept"
