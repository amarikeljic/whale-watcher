"""A stuck main loop ends its process so Docker restarts it. A Docker healthcheck only
marks a container unhealthy outside Swarm; nothing restarts it."""

from __future__ import annotations

import asyncio
import logging
import threading

from whalewatcher import service
from whalewatcher.service import WATCHDOG_EXIT, Watchdog


def test_a_slow_step_is_spared_and_a_stuck_one_ends_the_process(caplog):
    now, exits = [0.0], []
    dog = Watchdog("runner", limit=900, clock=lambda: now[0], exit_=exits.append)
    now[0] = 900
    assert dog.check() and exits == [], "at the limit: still alive"
    dog.beat()
    now[0] = 1800
    assert dog.check(), "each finished step starts the count again"
    with caplog.at_level(logging.CRITICAL):
        now[0] = 1801
        assert not dog.check()
    assert exits == [WATCHDOG_EXIT] and "Stuck: exiting so Docker restarts it" in caplog.text


def test_the_watching_thread_acts_on_its_own():
    fired = threading.Event()
    dog = Watchdog("detector", limit=5, clock=iter([0.0, 10.0, 10.0]).__next__, exit_=lambda code: fired.set())
    dog.start(every=0.01)
    assert fired.wait(2), "the thread noticed without the loop's help"


def test_the_detector_loop_arms_the_watchdog_beats_it_and_disarms_it(live_app, fake_http, clock, monkeypatch):
    calls = []

    class Recorder:
        def __init__(self, name):
            calls.append(("new", name))

        def start(self):
            calls.append("start")
            return self

        def beat(self):
            calls.append("beat")

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(service, "Watchdog", Recorder)
    fake_http.route("/v2/trades", lambda params: [])

    async def run() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        await service.run_detector(live_app, stop, cycles=None)

    asyncio.run(run())
    assert calls[:2] == [("new", "detector"), "start"] and "beat" in calls and calls[-1] == "stop"

    calls.clear()
    asyncio.run(service.run_detector(live_app, asyncio.Event(), cycles=1))
    assert calls == [], "a run of a fixed number of steps (tests, --once) is not watched"
