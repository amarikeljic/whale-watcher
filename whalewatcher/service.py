"""The long-running service and the commands that read its results.

One loop, one process, nothing listening on any port:

    every poll_interval_seconds   paper fills that are due -> poll wallets -> alerts
    every settle_interval_seconds settle resolved paper trades
    nightly at rescore.hour_utc   rescore with data up to today, rotate the monitored set (Phase 6)
    daily at daily_summary_hour   post the stats summary to the stats webhook

Failures degrade instead of crashing: a wallet that fails is skipped for that
cycle, a host that stays down is reported once and again when it recovers, and
SIGTERM (``docker stop``) finishes the current cycle before exiting.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from .app import App, open_app
from .config import Config
from .discord import DiscordWebhook, NullSink, Sink
from .monitor import DATA_HOST, Monitor, rotate_monitored
from .paper import PaperEngine
from .scoring import run_scoring
from .stats import daily_summary, export_csv, render
from .timeutil import from_ts, iso, start_of_day

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "heartbeat"


async def rescore(app: App, *, as_of: int | None = None) -> int | None:
    """Score with data up to today 00:00 UTC and rotate the monitored set to the new top_k."""
    as_of = as_of if as_of is not None else start_of_day(app.clock())
    run = await run_scoring(app, as_of, kind="nightly", historical_pool=False,
                            progress=lambda m: log.info("rescore: %s", m))
    if run.selected:
        added, removed = rotate_monitored(app, run.run_id)
        log.info("rescore run #%s: monitoring %d wallets (+%d, -%d)", run.run_id, len(run.selected),
                 len(added), len(removed))
    else:
        log.warning("rescore run #%s: no wallet passed the filters; keeping the current monitored set",
                    run.run_id)
    return run.run_id


class Service:
    def __init__(self, app: App, sink: Sink, stats_sink: Sink | None = None) -> None:
        self.app = app
        self.sink = sink
        self.stats_sink = stats_sink
        self.paper = PaperEngine(app)
        self.monitor = Monitor(app, sink, paper=self.paper)
        self.stop = asyncio.Event()
        self._outage_reported: str | None = None

    async def run(self, *, cycles: int | None = None) -> None:
        app, cfg = self.app, self.app.cfg
        if not self.monitor.wallets():
            log.info("no monitored wallets yet: scoring now")
            await self._rescore()
            # That was today's rescore; do not run another one after the first poll.
            app.db.set_state("last_rescore_day", from_ts(app.clock()).strftime("%Y-%m-%d"))
        done = 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                stats = await self.monitor.poll_once()
                log.info("poll #%d: %d wallets, %d new fills, %d alerts, %d skipped, %d edits, %d errors, "
                         "%d paper fills, data lag %ss", stats.counter, stats.wallets, stats.new_fills,
                         stats.alerts, stats.skipped, stats.edits, stats.errors, len(stats.paper_filled),
                         stats.data_lag)
                await self._periodic()
            except Exception:
                log.exception("cycle failed; continuing")
            app.db.set_state(HEARTBEAT_KEY, app.clock())
            await self._report_outage()
            done += 1
            if cycles is not None and done >= cycles:
                break
            wait = max(0.0, cfg.alerts.poll_interval_seconds - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
            except TimeoutError:
                pass
        log.info("stopped after %d cycles", done)

    async def _rescore(self) -> None:
        """Rescore, and say so once if that leaves nothing to monitor: silence would
        otherwise look exactly like a broken service."""
        app = self.app
        run_id = await rescore(app)
        if self.monitor.wallets():
            app.db.set_state("empty_notice_run", None)
            return
        if app.db.get_state("empty_notice_run") is not None:
            return
        app.db.set_state("empty_notice_run", run_id)
        run = app.db.one("SELECT n_candidates FROM score_runs WHERE id = ?", (run_id,))
        n = run["n_candidates"] if run else 0
        await (self.stats_sink or self.sink).post(
            f"⚠ Whale Watcher: no wallet passed the scoring filters (snapshot #{run_id}, {n} candidates), "
            "so nothing is being monitored and no alerts will fire. Loosen the filters in config.yaml or "
            "wait for a wallet to qualify; see `python -m whalewatcher score` for why each one failed.")

    async def _periodic(self) -> None:
        app, cfg = self.app, self.app.cfg
        now = app.clock()
        if now - int(app.db.get_state("last_settle_at", 0)) >= cfg.paper.settle_interval_seconds:
            settled = await self.paper.settle()
            app.db.set_state("last_settle_at", now)
            if settled:
                log.info("settled %d paper trades", settled)
        today = from_ts(now).strftime("%Y-%m-%d")
        dt = from_ts(now)
        if (cfg.rescore.enabled and app.db.get_state("last_rescore_day") != today
                and (dt.hour, dt.minute) >= (cfg.rescore.hour_utc, cfg.rescore.minute_utc)):
            app.db.set_state("last_rescore_day", today)
            await self._rescore()
        if (self.stats_sink is not None and app.db.get_state("last_summary_day") != today
                and dt.hour >= cfg.stats.daily_summary_hour_utc):
            app.db.set_state("last_summary_day", today)
            await self.stats_sink.post(f"📊 Whale Watcher daily summary ({today} UTC)\n```\n"
                                       f"{daily_summary(app)}\n```")

    async def _report_outage(self) -> None:
        problem = self.app.http.outage(DATA_HOST)
        target = self.stats_sink or self.sink
        if problem and self._outage_reported is None:
            self._outage_reported = problem
            log.error("outage: %s", problem)
            await target.post(f"⚠ Whale Watcher: Polymarket data is unreachable. {problem}. "
                              "Alerts and paper fills are paused until it answers again.")
        elif problem is None and self._outage_reported is not None:
            self._outage_reported = None
            log.info("outage over")
            await target.post("✅ Whale Watcher: Polymarket data is answering again.")


def _sinks(app: App, *, post: bool) -> tuple[Sink, Sink | None]:
    cfg = app.cfg
    if not post or not cfg.discord_webhook_url:
        sink: Sink = NullSink()
    else:
        sink = DiscordWebhook(app.http, cfg.discord_webhook_url)
    stats_sink = (DiscordWebhook(app.http, cfg.discord_stats_webhook_url)
                  if post and cfg.discord_stats_webhook_url else None)
    return sink, stats_sink


async def run_command(args, cfg: Config) -> int:
    async with open_app(cfg) as app:
        if args.command == "monitor":
            sink, stats_sink = _sinks(app, post=not args.no_post)
            if isinstance(sink, NullSink):
                log.warning("no Discord webhook in use: alerts are stored as UNSENT and still paper traded")
            service = Service(app, sink, stats_sink)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, service.stop.set)
                except (NotImplementedError, RuntimeError):
                    pass
            await service.run(cycles=1 if args.once else args.cycles)
            return 0
        if args.command == "settle":
            n = await PaperEngine(app).settle()
            print(f"settled {n} paper trades")
            return 0
        if args.command == "stats":
            text = render(app)
            print(text)
            if args.post:
                _, stats_sink = _sinks(app, post=True)
                if stats_sink is None:
                    print("no DISCORD_STATS_WEBHOOK_URL set; not posted")
                else:
                    await stats_sink.post(f"📊 Whale Watcher stats\n```\n{daily_summary(app)}\n```")
            return 0
        if args.command == "export":
            n = export_csv(app, args.csv)
            print(f"wrote {n} paper trades to {args.csv}")
            return 0
        if args.command == "health":
            beat = app.db.get_state(HEARTBEAT_KEY)
            limit = 5 * cfg.alerts.poll_interval_seconds
            if beat is None or app.clock() - int(beat) > limit:
                print(f"unhealthy: last heartbeat {iso(beat)}")
                return 1
            print(f"healthy: last heartbeat {iso(beat)}")
            return 0
    raise SystemExit(f"unknown command {args.command}")
