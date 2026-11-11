"""v2 Phase 3: the detector. Two routes, one dedupe key, tracked wallets only, and
runners that all read the same published fills."""

from __future__ import annotations

from conftest import NOW
from whalewatcher.detector import Detector
from whalewatcher.discord import MemorySink
from whalewatcher.http import HttpError
from whalewatcher.monitor import Monitor
from whalewatcher.service import DETECTOR_HEARTBEAT_KEY, Service, run_detector
from world import FakeMarket, World

WHALE, MAKER, STRANGER = "0xwhale", "0xmaker", "0xstranger"
COND = "0xm"


def setup(app, fake_http, wallets=(WHALE,)):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 30 * 86400))
    world.set_book(f"{COND}-t0", [(0.44, 1000)], [(0.42, 500)])
    for i, w in enumerate(wallets, start=1):
        app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                       "VALUES (?, 1, ?, 1, ?, ?)", (w, i, NOW - 3600, NOW - 120))
    return world, Detector(app)


def events(app):
    return app.db.query("SELECT * FROM fill_events ORDER BY id")


async def test_the_feed_publishes_only_tracked_wallets_with_the_time_they_were_seen(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    world.trade(STRANGER, NOW - 3, COND, size=9000, price=0.42, usdc=3780, feed=True)
    stats = await detector.poll_once(backstop=False)
    assert (stats.feed_rows, stats.published) == (2, 1)
    (e,) = events(live_app)
    assert (e["wallet"], e["side"], e["size"], e["price"], e["ts"]) == (WHALE, "BUY", 1500, 0.42, NOW - 3)
    assert e["source"] == "feed" and e["detected_at"] == float(NOW)
    assert live_app.db.one("SELECT COUNT(*) n FROM wallet_trades")["n"] == 1, "strangers' trades are not stored"
    feed_calls = [p for _, path, p in fake_http.calls if path == "/v2/trades"]
    assert feed_calls[0]["taker_only"] == "false" and feed_calls[0]["limit"] == 1000, "maker fills included"


async def test_every_feed_request_is_a_url_never_asked_before(live_app, fake_http, clock):
    """An identical URL is answered from a 300 s CDN cache, ten seconds stale and more."""
    world, detector = setup(live_app, fake_http)
    for _ in range(3):
        await detector.poll_once(backstop=False)
    nonces = [p["_t"] for _, path, p in fake_http.calls if path == "/v2/trades"]
    assert len(nonces) == 3 and len(set(nonces)) == 3


async def test_both_routes_seeing_a_fill_publish_it_once(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    first = await detector.poll_once(backstop=True)             # feed, then the backstop reads the same fill
    again = await detector.poll_once(backstop=True)
    assert (first.published, first.from_backstop, again.published) == (1, 0, 0)
    assert len(events(live_app)) == 1 and events(live_app)[0]["source"] == "feed"


async def test_the_backstop_catches_what_the_feed_never_showed(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 40, COND, size=1500, price=0.42, usdc=630)       # not on the feed (dropped, or we were down)
    stats = await detector.poll_once(backstop=True)
    assert (stats.published, stats.from_backstop) == (1, 1)
    assert events(live_app)[0]["source"] == "backstop"
    assert live_app.db.one("SELECT last_seen_ts FROM monitored_wallets")["last_seen_ts"] == NOW - 40


async def test_the_backstop_runs_when_due_not_on_every_feed_poll(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 40, COND, size=1500, price=0.42, usdc=630)       # only the backstop can see it
    first = await detector.poll_once()                          # first step: a pass is due, and starts...
    assert first.published == 0, "...in the background: the feed poll does not wait for it"
    await detector.backstop_task
    second = await detector.poll_once()
    assert (second.published, second.from_backstop) == (1, 1), "what the pass found is reported on a later step"
    assert fake_http.count("/v2/activity") == 1
    clock.advance(live_app.cfg.alerts.poll_interval_seconds)
    await detector.poll_once()
    await detector.backstop_task
    assert fake_http.count("/v2/activity") == 2 and fake_http.count("/v2/trades") == 3


async def test_identical_rows_are_numbered_per_wallet_so_both_routes_agree(live_app, fake_http, clock):
    """One transaction can fill two makers for the same size at the same price, and one
    maker twice. The feed shows them together; each wallet's own activity shows only its
    rows. The key must come out the same either way."""
    world, detector = setup(live_app, fake_http, wallets=(WHALE, MAKER))
    for wallet in (WHALE, MAKER, MAKER):                       # same tx, token, side, size, price
        world.trade(wallet, NOW - 3, COND, size=40, price=0.57, tx="0xsame", feed=True)
    stats = await detector.poll_once(backstop=True)
    assert (stats.published, stats.from_backstop) == (3, 0), "three real fills; the backstop found nothing new"
    assert sorted(e["wallet"] for e in events(live_app)) == [MAKER, MAKER, WHALE]
    assert len({e["fill_key"] for e in events(live_app)}) == 3


async def test_a_page_that_does_not_reach_back_to_the_last_one_is_a_gap_and_calls_the_backstop(
        live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    await detector.poll_once(backstop=True)
    world.trade(WHALE, NOW + 5, COND, size=1500, price=0.42, usdc=630)                    # fell in the gap
    for i in range(1000):                                      # a burst: a full page, all newer than that
        world.trade(STRANGER, NOW + 20 + i // 100, COND, size=1, price=0.5, feed=True, activity=False)
    detector._newest_seen = NOW                                # where the previous page ended
    clock.advance(30)
    stats = await detector.poll_once()
    assert stats.gap and detector.gaps == 1
    await detector.backstop_task                                # started at once, not a minute later
    found = await detector.poll_once(backstop=False)
    assert len(events(live_app)) == 1 and events(live_app)[0]["source"] == "backstop"
    assert found.published == 0, "a pass's finds are reported by the live loop's own calls"


async def test_a_failing_feed_degrades_to_the_backstop(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630)

    def down(_params):
        raise HttpError("503", status=503, answered=True)

    fake_http.route("/v2/trades", down)
    stats = await detector.poll_once(backstop=True)
    assert stats.errors == 1 and stats.published == 1 and stats.from_backstop == 1


async def test_two_runners_read_the_same_fills_each_from_its_own_cursor(live_app, fake_http, clock):
    world, detector = setup(live_app, fake_http)

    class Second(Monitor):
        RUNNER = "second"

    a = Monitor(live_app, MemorySink(), embedded=False)
    b = Second(live_app, MemorySink(), embedded=False)
    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    assert (await a.poll_once()).new_fills == 0, "nothing published yet, and a runner does not detect"
    await detector.poll_once(backstop=False)
    assert (await a.poll_once()).new_fills == 1
    assert (await a.poll_once()).new_fills == 0, "its cursor moved on"
    assert (await b.poll_once()).new_fills == 1, "the other runner still gets it, with the same detection time"
    assert fake_http.count("/v2/trades") == 1, "the API was asked once for both"


async def test_the_detector_process_writes_a_heartbeat_and_the_runner_reports_it_missing(live_app, fake_http, clock):
    import asyncio
    world, _ = setup(live_app, fake_http)
    sink = MemorySink()
    svc = Service(live_app, sink, with_detector=False)
    await svc._report_outage()
    await svc._report_outage()
    assert [t for _, t in sink.posts if "detector is not running" in t] != [] and len(sink.posts) == 1

    world.trade(WHALE, NOW - 3, COND, size=1500, price=0.42, usdc=630, feed=True)
    await run_detector(live_app, asyncio.Event(), cycles=1)
    assert live_app.db.get_state(DETECTOR_HEARTBEAT_KEY) == clock.now and len(events(live_app)) == 1
    await svc._report_outage()
    assert "detector is running again" in sink.posts[-1][1]
    stats = await svc.monitor.poll_once()
    assert stats.alerts == 1, "the runner acted on what the detector process published"
    assert fake_http.count("/v2/activity") == 1, "only the detector polled; the runner made no detection calls"
