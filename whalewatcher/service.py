"""The long-running processes and the commands that read their results.

Nothing listens on any port. Two processes share the database (v2 Phase 3):

  detector   watches trades and publishes each tracked wallet's fills (detector.py)
  monitor    the `single` runner: reads published fills -> alerts -> paper bot, plus
             the housekeeping:

    every settle_interval_seconds settle resolved paper trades
    nightly at rescore.hour_utc   rescore with data up to today, review the watchlist (watchlist.py)
    daily at daily_summary_hour   write the short summary to the log (also: `stats --brief`)

They are separate containers so that one crashing, restarting or pausing never
stops the other: the detector keeps publishing while a runner is down, and the
runner picks up from its cursor when it returns. A plain ``monitor`` with no
detector container still works: it runs the detector inside its own process.

Failures degrade instead of crashing: a wallet that fails is skipped for that
cycle, a host that stays down is reported once and again when it recovers, and
SIGTERM (``docker stop``) finishes the current cycle before exiting.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections import deque

from . import bankroll
from .app import App, open_app
from .bot import Bot
from .config import Config
from .costs import recalculate
from .detector import HEARTBEAT_KEY as DETECTOR_HEARTBEAT_KEY
from .detector import Detector
from .monitor import DATA_HOST, Monitor
from .paper import PaperEngine
from .polymarket_us import UsRowA
from .stats import daily_summary, export_bot_csv, export_csv, render
from .timeutil import from_ts, iso
from .watchlist import nightly, seed_from_monitored, sync_monitored

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "heartbeat"
WALLET_CHECK_EVERY = 30.0      # seconds between looks at the wallet amount (one small query)
HEARTBEAT_EVERY = 5.0          # seconds between heartbeat writes: the loops tick faster than that
DETECTOR_STALE_SECONDS = 90


async def rescore(app: App, *, as_of: int | None = None) -> int | None:
    """The nightly job: score with data up to today 00:00 UTC, then review the watchlist
    (add wallets that meet the entry rules, remove ones that kept failing the exit rules)."""
    review = await nightly(app, as_of=as_of, progress=lambda m: log.info("rescore: %s", m))
    if not review.after:
        log.warning("rescore run #%s: the watchlist is empty (no wallet meets the entry rules)", review.run_id)
    return review.run_id


class Service:
    """The `single` runner and the housekeeping. ``with_detector`` also runs the
    detector in this process; False means a separate detector process publishes."""

    def __init__(self, app: App, *, with_detector: bool = True, review_in_subprocess: bool = False,
                 config_path: str | None = None) -> None:
        self.app = app
        # Things the owner should know (an outage, a stopped detector, an empty
        # watchlist). They go to the log, which `docker logs` shows, and the Health
        # page shows the same state. Nothing is sent anywhere. The most recent are
        # kept here for the tests.
        self.notices: deque[str] = deque(maxlen=50)
        self.with_detector = with_detector
        # The nightly review scores hundreds of wallets: minutes of work, some of it
        # pure computation that would freeze this loop, and a bot that is frozen
        # misses every signal in that window. The live service runs it as a child
        # process and keeps trading; tests run it in-process.
        self.detector_grace = 0.0 if not review_in_subprocess else 600.0    # live service only
        self.review_in_subprocess = review_in_subprocess
        self.config_path = config_path
        self._review_task: asyncio.Task | None = None
        self.paper = PaperEngine(app)            # settles the pre-v3 paper trades; opens no new ones
        self.bot = Bot(app, Monitor.RUNNER)
        # Row A: the same signals priced on Polymarket US, in the background.
        self.us = UsRowA(app) if app.cfg.polymarket_us.enabled and app.us is not None else None
        self.monitor = Monitor(app, paper=self.paper, bot=self.bot, embedded=with_detector, us=self.us)
        self.stop = asyncio.Event()
        self._outage_reported: str | None = None
        self._detector_down = False
        self._started = time.monotonic()
        self._beat_at = 0.0
        self._wallet_checked = -WALLET_CHECK_EVERY      # look on the first cycle
        self._quiet_since = 0.0

    async def run(self, *, cycles: int | None = None) -> None:
        app, cfg = self.app, self.app.cfg
        if seed_from_monitored(app):          # first start after the v2 upgrade
            sync_monitored(app, None)
        recalculate(app)                      # v2.1: cost columns for trades made before they existed
        if not self.monitor.wallets():
            log.info("no monitored wallets yet: scoring now")
            await self._rescore()
            # That was today's rescore; do not run another one after the first poll.
            app.db.set_state("last_rescore_day", from_ts(app.clock()).strftime("%Y-%m-%d"))
        interval = cfg.bot.feed_poll_seconds if self.with_detector else cfg.bot.runner_poll_seconds
        log.info("runner `single` started: %s, a step every %gs",
                 "detector in this process" if self.with_detector else "reading the detector process's fills",
                 interval)
        done = 0
        totals = {"fills": 0, "alerts": 0, "skipped": 0, "errors": 0}
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                # backstop=None: the per-wallet safety net runs when it is due, not every step
                stats = await self.monitor.poll_once(backstop=None)
                totals["fills"] += stats.new_fills
                totals["alerts"] += stats.alerts
                totals["skipped"] += stats.skipped
                totals["errors"] += stats.errors
                # A step every second: log one line when something happened, and a
                # summary once a minute otherwise, instead of 86,400 lines a day.
                if stats.alerts or stats.skipped or stats.errors or time.monotonic() - self._quiet_since >= 60:
                    self._quiet_since = time.monotonic()
                    log.info("step #%d: %d wallets | since last line: %d new fills, %d alerts, %d skipped, "
                             "%d errors | data lag %ss", stats.counter, stats.wallets, totals["fills"],
                             totals["alerts"], totals["skipped"], totals["errors"], stats.data_lag)
                    totals = dict.fromkeys(totals, 0)
                await self._periodic()
            except Exception:
                log.exception("cycle failed; continuing")
            if time.monotonic() - self._beat_at >= HEARTBEAT_EVERY or cycles is not None:
                self._beat_at = time.monotonic()
                try:
                    app.db.set_state(HEARTBEAT_KEY, app.clock())
                    if self.with_detector:
                        app.db.set_state(DETECTOR_HEARTBEAT_KEY, app.clock())
                    await self._report_outage()
                except Exception:
                    # A busy database or a failing notice must never end the runner: a
                    # missed heartbeat shows on the Health page, a dead process trades nothing.
                    log.exception("heartbeat failed; continuing")
            done += 1
            if cycles is not None and done >= cycles:
                break
            wait = max(0.0, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except TimeoutError:
                pass
        if self.us is not None:
            await self.us.drain()             # let any Polymarket US pricing in flight finish and be recorded
        log.info("stopped after %d cycles", done)

    async def _rescore(self) -> None:
        """Rescore, and say so once if that leaves nothing to monitor: silence would
        otherwise look exactly like a broken service."""
        app = self.app
        run_id = await rescore(app)
        await self._after_review(run_id)

    async def _after_review(self, run_id: int | None) -> None:
        app = self.app
        if self.monitor.wallets():
            app.db.set_state("empty_notice_run", None)
            return
        if app.db.get_state("empty_notice_run") is not None:
            return
        app.db.set_state("empty_notice_run", run_id)
        run = app.db.one("SELECT n_candidates FROM score_runs WHERE id = ?", (run_id,))
        n = run["n_candidates"] if run else 0
        self.notice(
            f"no wallet meets the watchlist entry rules (snapshot #{run_id}, {n} candidates), "
            "so nothing is being monitored and no alerts will fire. Loosen watchlist.entry in config.yaml or "
            "wait for a wallet to qualify; see `python -m whalewatcher watchlist --last` for why.")

    async def _review_in_child(self) -> None:
        """`python -m whalewatcher watchlist --review` as a child process. Its report goes
        to this container's log; the list it leaves in the database is picked up on the
        next step, because the runner reads the watched wallets every step."""
        cmd = [sys.executable, "-m", "whalewatcher"]
        if self.config_path:
            cmd += ["--config", self.config_path]
        cmd += ["watchlist", "--review"]
        log.info("nightly review started in a child process")
        try:
            proc = await asyncio.create_subprocess_exec(*cmd)
            code = await proc.wait()
        except Exception:
            log.exception("nightly review could not be started")
            return
        if code != 0:
            log.error("nightly review exited with code %s; the watchlist is unchanged", code)
            return
        log.info("nightly review finished")
        await self._after_review(self.app.db.get_state("monitored_run_id"))

    def _sync_wallet(self) -> None:
        """The wallet amount is the one thing the dashboard can change. Notice it within
        half a minute: end the old wallet run, start the new one."""
        if time.monotonic() - self._wallet_checked < WALLET_CHECK_EVERY:
            return
        self._wallet_checked = time.monotonic()
        try:
            runners = [self.bot.runner] + (["us"] if self.us is not None else [])
            for change in bankroll.sync_runs(self.app, runners):
                log.info("wallet: %s", change)
        except Exception:
            log.exception("could not bring the wallet run up to date; trying again shortly")

    async def _periodic(self) -> None:
        app, cfg = self.app, self.app.cfg
        now = app.clock()
        self._sync_wallet()
        if now - int(app.db.get_state("last_settle_at", 0)) >= cfg.paper.settle_interval_seconds:
            settled = await self.paper.settle()
            closed = await self.bot.settle()
            if self.us is not None:
                try:
                    closed += await self.us.settle()
                except Exception as exc:  # noqa: BLE001 - the comparison row never stops the runner
                    log.warning("could not settle the Polymarket US rows: %s", exc)
            recalculate(app)                 # cost columns for anything filled or closed since
            app.db.set_state("last_settle_at", now)
            if settled or closed:
                log.info("settled %d old-rule paper trades and %d bot trades", settled, closed)
        today = from_ts(now).strftime("%Y-%m-%d")
        dt = from_ts(now)
        if (cfg.rescore.enabled and app.db.get_state("last_rescore_day") != today
                and (dt.hour, dt.minute) >= (cfg.rescore.hour_utc, cfg.rescore.minute_utc)):
            app.db.set_state("last_rescore_day", today)
            if self.review_in_subprocess:
                if self._review_task is None or self._review_task.done():
                    self._review_task = asyncio.create_task(self._review_in_child())
            else:
                await self._rescore()
        if app.db.get_state("last_summary_day") != today and dt.hour >= cfg.stats.daily_summary_hour_utc:
            app.db.set_state("last_summary_day", today)
            log.info("daily summary (%s UTC):\n%s", today, daily_summary(app))

    def notice(self, text: str, *, good: bool = False) -> None:
        self.notices.append(text)
        (log.info if good else log.error)("%s", text)

    async def _report_outage(self) -> None:
        problem = self.app.http.outage(DATA_HOST)
        if problem and self._outage_reported is None:
            self._outage_reported = problem
            self.notice(f"Polymarket data is unreachable. {problem}. Alerts and paper fills are paused until it "
                        "answers again.")
        elif problem is None and self._outage_reported is not None:
            self._outage_reported = None
            self.notice("Polymarket data is answering again.", good=True)
        # The detector is a separate process: if it stops, nothing is detected and
        # silence would look exactly like a quiet market.
        if self.with_detector:
            return
        beat = self.app.db.get_state(DETECTOR_HEARTBEAT_KEY)
        stale = beat is None or self.app.clock() - int(beat) > DETECTOR_STALE_SECONDS
        # Both containers start together and the detector may still be backing up and
        # migrating the database: no alarm until it has had time to write a heartbeat.
        warming_up = beat is None and time.monotonic() - self._started < self.detector_grace
        if stale and not self._detector_down and not warming_up:
            self._detector_down = True
            self.notice(f"the detector is not running (last heartbeat {iso(beat)}). No whale fills are being "
                        "detected until it is back.")
        elif not stale and self._detector_down:
            self._detector_down = False
            self.notice("the detector is running again.", good=True)


async def run_detector(app: App, stop: asyncio.Event, *, cycles: int | None = None) -> None:
    """The detector process: one feed poll every feed_poll_seconds, the per-wallet
    backstop every alerts.poll_interval_seconds, a heartbeat every few seconds."""
    detector = Detector(app)
    interval = app.cfg.bot.feed_poll_seconds
    log.info("detector started: global trade feed every %gs, per-wallet backstop every %ds",
             interval, app.cfg.alerts.poll_interval_seconds)
    done = published = backstop = errors = 0
    beat_at = line_at = 0.0
    while not stop.is_set():
        started = time.monotonic()
        try:
            stats = await detector.poll_once()
            published += stats.published
            backstop += stats.from_backstop
            errors += stats.errors
        except Exception:
            log.exception("detector step failed; continuing")
        try:
            if time.monotonic() - beat_at >= HEARTBEAT_EVERY or cycles is not None:
                beat_at = time.monotonic()
                app.db.set_state(DETECTOR_HEARTBEAT_KEY, app.clock())
            if time.monotonic() - line_at >= 60:
                line_at = time.monotonic()
                log.info("detector: %d wallets tracked | last minute: %d fills published (%d only the backstop "
                         "saw), %d errors, %d feed gaps so far", len(detector.tracked()), published, backstop,
                         errors, detector.gaps)
                published = backstop = errors = 0
        except Exception:
            log.exception("detector heartbeat failed; continuing")     # a busy database must not end the detector
        done += 1
        if cycles is not None and done >= cycles:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, interval - (time.monotonic() - started)))
        except TimeoutError:
            pass
    log.info("detector stopped after %d steps", done)


async def run_command(args, cfg: Config) -> int:
    async with open_app(cfg) as app:
        if args.command in ("monitor", "detector"):
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except (NotImplementedError, RuntimeError):
                    pass
            if args.command == "detector":
                await run_detector(app, stop, cycles=args.cycles)
                return 0
            service = Service(app, with_detector=not args.no_detector, review_in_subprocess=True,
                              config_path=args.config)
            service.stop = stop
            await service.run(cycles=1 if args.once else args.cycles)
            return 0
        if args.command == "settle":
            n = await PaperEngine(app).settle()
            m = await Bot(app, Monitor.RUNNER).settle()
            print(f"settled {n} old-rule paper trades and {m} bot trades")
            return 0
        if args.command == "stats":
            from .stats import Scope
            scope = Scope.parse(args.model, args.include_outages)     # checked by cli.main before opening
            print(daily_summary(app, scope) if args.brief else render(app, scope=scope))
            return 0
        if args.command == "export":
            n = export_csv(app, args.csv)
            print(f"wrote {n} old-rule paper trades to {args.csv}")
            bot_path = str(args.csv).removesuffix(".csv") + "_bot.csv"
            m = export_bot_csv(app, bot_path)
            print(f"wrote {m} bot trades to {bot_path}")
            return 0
        if args.command == "health":
            beat = app.db.get_state(DETECTOR_HEARTBEAT_KEY if args.detector else HEARTBEAT_KEY)
            limit = 5 * cfg.alerts.poll_interval_seconds
            if beat is None or app.clock() - int(beat) > limit:
                print(f"unhealthy: last heartbeat {iso(beat)}")
                return 1
            print(f"healthy: last heartbeat {iso(beat)}")
            return 0
    raise SystemExit(f"unknown command {args.command}")
