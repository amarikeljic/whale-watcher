"""v2 Phase 3: the detector. One process that watches trades and publishes each
fill of a tracked wallet exactly once, with the moment it was first seen.

Two routes, one dedupe key (the idea of redundant circuits on one key is from
AndreySchurko/garnet-polymarket; the code is ours):

  feed      GET /v2/trades with no filter: every trade on Polymarket, newest first,
            once a second. Measured 2026-11-10 over 600 polls: a new fill appears a
            median 3.4 s after its block (p90 4.9 s), a 1,000-row page covers 4 to
            45 s, so consecutive polls always overlap.
  backstop  GET /v2/activity per tracked wallet, once a minute. Slow, but it cannot
            miss a fill: it catches what the feed dropped (an outage, a burst that
            outran the page) and what happened while this process was down.

Both routes serve identical rows for a fill (checked on 39 wallets, 2026-11-11),
so the same ``fill_key`` comes out of either and a fill is published once.

Publishing means one row in ``fill_events``. Runners read that table, so the
APIs are asked once however many runners there are, and every runner sees the
same fill at the same detection time. Read-only: no wallet, no key, no signing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass

from .app import App
from .polymarket import Activity, assign_fill_keys

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "detector_heartbeat"
FEED_PAGE = 1000


@dataclass
class DetectorStats:
    feed_rows: int = 0          # rows read from the feed this poll
    published: int = 0          # new fills of tracked wallets
    from_backstop: int = 0      # ...of which only the backstop saw
    gap: bool = False           # the feed page did not reach back to the previous poll
    errors: int = 0


class Detector:
    def __init__(self, app: App) -> None:
        self.app = app
        self._calls = 0
        self._newest_seen: int | None = None    # newest block time on the previous feed page
        self._last_backstop = 0.0
        self.backstop_task: asyncio.Task | None = None
        self.backstop_totals = DetectorStats()   # what background backstop passes found, until read
        self.gaps = 0

    def tracked(self) -> set[str]:
        """Every wallet some runner cares about: today the watchlist (the consensus
        pool joins it in Phase 3b)."""
        return {r["wallet"] for r in self.app.db.query("SELECT wallet FROM monitored_wallets WHERE active = 1")}

    # -- publishing ------------------------------------------------------------------

    def publish(self, rows: list[Activity], source: str) -> int:
        """Store the rows and publish the ones never seen before. Returns how many."""
        if not rows:
            return 0
        by_wallet: dict[str, list[Activity]] = defaultdict(list)
        for r in rows:
            by_wallet[r.wallet].append(r)
        for group in by_wallet.values():
            # Keys number identical rows per wallet, as a per-wallet fetch does: two
            # makers filled for the same size at the same price in one transaction
            # must not be mistaken for one wallet's two fills.
            assign_fill_keys(group)
        app = self.app
        now, seen_at = app.clock(), app.wall()
        new = 0
        insert = ("INSERT OR IGNORE INTO wallet_trades (wallet, ts, type, condition_id, token_id, outcome, "
                  "outcome_index, side, size, usdc_size, price, tx_hash, fill_key, seen_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

        def store(conn, r: Activity) -> bool:
            return conn.execute(insert, (r.wallet, r.ts, r.type, r.condition_id, r.token_id, r.outcome,
                                         r.outcome_index, r.side, r.size, r.usdc_size, r.price, r.tx_hash,
                                         r.fill_key, now)).rowcount == 1

        with app.db.tx() as conn:
            for r in rows:
                if not store(conn, r):
                    # Already stored: published before, or history read by scoring. Unless
                    # the key belongs to ANOTHER wallet: the key leaves the wallet out, so
                    # two tracked wallets filled identically in one transaction collide.
                    # The first keeps the plain key (every stored key stays valid); the
                    # other gets a key salted with its address, the same on every re-read.
                    owner = conn.execute("SELECT wallet FROM wallet_trades WHERE fill_key = ?",
                                         (r.fill_key,)).fetchone()
                    if owner is None or owner["wallet"] == r.wallet:
                        continue
                    r.fill_key = hashlib.sha1(f"{r.wallet}|{r.fill_key}".encode()).hexdigest()
                    if not store(conn, r):
                        continue
                conn.execute(
                    "INSERT OR IGNORE INTO fill_events (fill_key, wallet, ts, condition_id, token_id, outcome, "
                    "outcome_index, side, size, usdc_size, price, tx_hash, detected_at, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r.fill_key, r.wallet, r.ts, r.condition_id, r.token_id, r.outcome, r.outcome_index, r.side,
                     r.size, r.usdc_size, r.price, r.tx_hash, seen_at, source))
                new += 1
        return new

    # -- the feed ----------------------------------------------------------------------

    async def poll_feed(self, stats: DetectorStats) -> None:
        """One page of the global feed, maker rows included: 46% of fills of $500 or
        more are maker fills, which the default (taker_only) would hide."""
        tracked = self.tracked()
        self._calls += 1
        # A unique parameter per request: an identical URL is answered from a 300 s
        # CDN cache (`cache-control: public, max-age=300`), ten seconds stale and more.
        rows, _ = await self.app.data.trade_feed(limit=FEED_PAGE, nonce=f"{int(self.app.wall() * 1000)}{self._calls}")
        stats.feed_rows = len(rows)
        if not rows:
            return
        newest, oldest = max(r.ts for r in rows), min(r.ts for r in rows)
        if self._newest_seen is not None and len(rows) >= FEED_PAGE and oldest > self._newest_seen:
            # The page did not reach back to where the last one ended. The backstop
            # will pick up whatever fell in between; say so, it should be rare.
            stats.gap = True
            self.gaps += 1
            self._last_backstop = 0.0
            log.warning("feed gap: page covers %ds back to %d, previous page ended at %d; backstop will run now",
                        newest - oldest, oldest, self._newest_seen)
        self._newest_seen = newest
        stats.published += self.publish([r for r in rows if r.wallet in tracked and r.type == "TRADE"], "feed")

    # -- the backstop ------------------------------------------------------------------

    async def poll_backstop(self, stats: DetectorStats) -> None:
        app = self.app
        cfg = app.cfg.alerts
        overlap = max(cfg.dedupe_window_seconds, 3 * cfg.poll_interval_seconds)
        for w in app.db.query("SELECT * FROM monitored_wallets WHERE active = 1 ORDER BY rank"):
            now = app.clock()
            start = (w["last_seen_ts"] or now - 2 * cfg.poll_interval_seconds) - overlap
            try:
                rows, _ = await app.data.activity(w["wallet"], types=("TRADE",), start=start, end=now + 60,
                                                  max_rows=3000)
            except Exception as exc:  # noqa: BLE001 - one wallet failing must not stop the rest
                stats.errors += 1
                log.warning("backstop failed for %s: %s", w["wallet"], exc)
                continue
            found = self.publish(rows, "backstop")
            stats.published += found
            stats.from_backstop += found
            newest = max([r.ts for r in rows], default=None)
            app.db.execute(
                "UPDATE monitored_wallets SET last_polled_at = ?, last_seen_ts = MAX(COALESCE(last_seen_ts, 0), ?) "
                "WHERE wallet = ?", (now, newest or (w["last_seen_ts"] or now - 2 * cfg.poll_interval_seconds),
                                     w["wallet"]))
        self._last_backstop = app.wall()

    # -- one step -----------------------------------------------------------------------

    async def poll_once(self, *, backstop: bool | None = None) -> DetectorStats:
        """One feed poll, plus the backstop.

        ``backstop=None`` (the live loop): when a pass is due it starts in the
        background, because a pass over every wallet takes many seconds and the feed
        must keep being read every second meanwhile. What it finds is added to the
        stats of a later call. ``True`` runs a pass now and waits for it; ``False``
        runs none."""
        stats = DetectorStats()
        try:
            await self.poll_feed(stats)
        except Exception as exc:  # noqa: BLE001 - degrade to the backstop, keep going
            stats.errors += 1
            log.warning("feed poll failed: %s", exc)
        if backstop is True:
            await self.poll_backstop(stats)
        elif backstop is None:
            running = self.backstop_task is not None and not self.backstop_task.done()
            due = self.app.wall() - self._last_backstop >= self.app.cfg.alerts.poll_interval_seconds
            if due and not running:
                self._last_backstop = self.app.wall()
                self.backstop_task = asyncio.create_task(self._backstop_in_background())
            found, self.backstop_totals = self.backstop_totals, DetectorStats()
            stats.published += found.published
            stats.from_backstop += found.from_backstop
            stats.errors += found.errors
        return stats

    async def _backstop_in_background(self) -> None:
        try:
            await self.poll_backstop(self.backstop_totals)
        except Exception:
            log.exception("backstop pass failed; the next one is due in a minute")
