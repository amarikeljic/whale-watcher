"""Part 4 #15: the service records its own outage windows, and results leave them out."""

from __future__ import annotations

import asyncio

from conftest import NOW
from whalewatcher import outages
from whalewatcher.detector import HEARTBEAT_KEY as DETECTOR_HEARTBEAT_KEY
from whalewatcher.service import HEARTBEAT_KEY, Service, run_detector
from world import World


def windows(app) -> list[tuple]:
    return [(r["kind"], r["start_ts"], r["end_ts"], r["source"])
            for r in app.db.query("SELECT * FROM outage_windows ORDER BY id")]


class Flaky:
    """Wraps FakeHttp and reports a data outage on demand."""

    def __init__(self, inner):
        self.inner, self.problem, self.failing = inner, None, None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def outage(self, host, **_):
        return self.problem

    def failing_for(self, host):
        return self.failing


async def test_a_runner_that_was_away_records_the_gap_and_a_quick_restart_does_not(live_app, fake_http, clock):
    World(fake_http)
    Service(live_app)._record_restart()
    assert windows(live_app) == [], "the first start ever has nothing to have missed"
    live_app.db.set_state(HEARTBEAT_KEY, clock.now - 600)
    svc = Service(live_app)
    svc._record_restart()
    assert windows(live_app) == [("runner", clock.now - 600, clock.now, "detected")]
    assert any("the runner was not running for 10 min" in n for n in svc.notices)
    live_app.db.set_state(HEARTBEAT_KEY, clock.now - 10)       # a quick redeploy: no signal can have been late
    Service(live_app)._record_restart()
    assert len(windows(live_app)) == 1


async def test_the_runner_records_its_gap_when_it_starts(live_app, fake_http, clock):
    World(fake_http)
    live_app.db.set_state(HEARTBEAT_KEY, clock.now - 3600)
    await Service(live_app).run(cycles=1)
    assert windows(live_app)[0][:3] == ("runner", clock.now - 3600, clock.now)


async def test_a_stale_detector_opens_one_window_and_its_return_closes_it(live_app, fake_http, clock):
    World(fake_http)
    svc = Service(live_app, with_detector=False)
    live_app.db.set_state(DETECTOR_HEARTBEAT_KEY, clock.now - 300)
    await svc._report_outage()
    await svc._report_outage()
    assert windows(live_app) == [("detector", clock.now - 300, None, "detected")], "one window, still open"
    clock.advance(600)
    live_app.db.set_state(DETECTOR_HEARTBEAT_KEY, clock.now)
    await svc._report_outage()
    assert windows(live_app) == [("detector", NOW - 300, NOW + 600, "detected")]


async def test_a_detector_starting_again_closes_the_open_window_or_records_its_own_gap(live_app, fake_http, clock):
    World(fake_http)
    outages.open_window(live_app.db, outages.DETECTOR, clock.now - 900, "the detector's heartbeat stopped")
    live_app.db.set_state(DETECTOR_HEARTBEAT_KEY, clock.now - 900)
    await run_detector(live_app, asyncio.Event(), cycles=1)
    assert windows(live_app) == [("detector", NOW - 900, NOW, "detected")], "closed, not doubled"
    clock.advance(7200)        # stopped for two hours, and no runner noticed
    await run_detector(live_app, asyncio.Event(), cycles=1)
    assert windows(live_app)[1] == ("detector", NOW, NOW + 7200, "detected")


async def test_a_data_outage_runs_from_the_first_failure_to_the_first_answer(live_app, fake_http, clock):
    World(fake_http)
    svc = Service(live_app)
    flaky = Flaky(fake_http)
    live_app.http = flaky
    flaky.problem, flaky.failing = "data-api.polymarket.com: 9 failed attempts over 15 min", 900.0
    await svc._report_outage()
    await svc._report_outage()
    assert windows(live_app) == [("data", NOW - 900, None, "detected")]
    clock.advance(300)
    flaky.problem = flaky.failing = None
    await svc._report_outage()
    assert windows(live_app) == [("data", NOW - 900, NOW + 300, "detected")]


async def test_a_restart_ends_a_data_window_that_a_stopped_runner_left_open(live_app, fake_http, clock):
    World(fake_http)
    outages.open_window(live_app.db, outages.DATA, clock.now - 2000, "down")
    live_app.db.set_state(HEARTBEAT_KEY, clock.now - 1000)
    Service(live_app)._record_restart()
    assert windows(live_app) == [("data", NOW - 2000, NOW - 1000, "detected"),
                                 ("runner", NOW - 1000, NOW, "detected")]


def test_a_window_never_runs_backwards(live_app):
    outages.open_window(live_app.db, outages.DETECTOR, NOW, "x")
    outages.close_window(live_app.db, outages.DETECTOR, NOW - 50)    # another process's clock
    assert windows(live_app) == [("detector", NOW, NOW, "detected")]
