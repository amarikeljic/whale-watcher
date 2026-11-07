"""The fresh-wallet test: copy large buys by brand-new wallets in non-sports markets.

Copying wallets with long profitable histories fails because their edge is speed: it is
priced in within a minute (README, "Would copying have made money?"). The tracker can
never pick a brand-new wallet that bets big in a non-sports market, the pattern public
insider trackers watch, where information may take hours or days to come out. This tests
that pattern, under a rule fixed on 2026-11-07 before any result was seen:

Markets   non-sports markets that resolved inside [start, end). Found by volume and dates
          only, never by outcome: Gamma's 2,000 highest-volume closed markets per week of
          end date (a listing stops at 2,000 rows), plus closed markets whose end date is
          later (they resolved early, as "by Dec 31?" markets do when the event happens).
Signal    a buy group (per wallet and token, fills within 300 s of the first, as in
          monitor.py) reaching $1,000, by a wallet whose first Polymarket trade was at most
          14 days before the fill that took the group to $1,000.
Copy      90 s after that fill, at the CLOB minute price plus one cent plus the market's
          taker fee; MISSED above 3 cents of slippage or if the market had closed; held to
          resolution (backtest.simulate).
Verdict   it counts only if the 95% CI of the mean ROI per settled copy, wallets and
          events resampled together, lies above zero.

Detection reads every fill of at least ``detect_usd`` in those markets, both sides of each
fill. A wallet whose earliest such fill is more than 14 days before a candidate buy cannot be
fresh at that buy, so only the rest are checked against their real first trade. A fresh
wallet's activity over its first 14 days is then read in full, so its groups are rebuilt from
every fill. A wallet that only ever bought in fills under ``detect_usd`` is not found, which
can only make the test miss insiders, not invent them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from .app import App
from .backtest import DETECTION_LAG, AlertEvent, SimTrade, alert_events, closed_before, price_after, simulate, write_trades
from .polymarket import MarketInfo
from .scoring import ACTIVITY_TYPES
from .stats_util import two_way_bootstrap_mean_ci
from .timeutil import iso
from .timing import Progress, Stopwatch

log = logging.getLogger(__name__)

STRATEGY = "backtest:fresh-wallet"
STATE_KEY = "backtest_run:fresh-wallet"
DAY = 86_400
WEEK = 7 * DAY
GAMMA_ROWS = 2000           # Gamma refuses offsets past this ("use /markets/keyset")


@dataclass
class FreshReport:
    start: int
    end: int
    markets: int = 0
    fills: int = 0
    truncated_markets: int = 0
    wallets_checked: int = 0
    fresh_wallets: int = 0
    events: list[AlertEvent] = field(default_factory=list)
    trades: list[SimTrade] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    categories: dict[str, str] = field(default_factory=dict)     # condition_id -> category
    event_slugs: dict[str, str] = field(default_factory=dict)
    watch: Stopwatch | None = None


async def universe(app: App, start: int, end: int, *, say: Callable[[str], None]) -> dict[str, MarketInfo]:
    """Non-sports markets resolved in [start, end), chosen by volume and dates only."""
    def day(ts: int) -> str:
        return iso(ts)[:10] + "T00:00:00Z"

    windows: list[tuple[int, int]] = []
    t = start - (start % DAY) - 3 * DAY
    while t < end:
        windows.append((t, t + WEEK))
        t += WEEK
    later = end
    while later < end + 400 * DAY:      # resolved early, with an end date after the window
        windows.append((later, later + 60 * DAY))
        later += 60 * DAY
    raw: dict[str, dict] = {}

    async def window(a: int, b: int) -> None:
        for offset in range(0, GAMMA_ROWS, 100):
            rows = await app.gamma.top_markets(end_min=day(a), end_max=day(b), offset=offset)
            for r in rows:
                if r.get("conditionId"):
                    raw[str(r["conditionId"])] = r
            if len(rows) < 100:
                return

    await asyncio.gather(*(window(a, b) for a, b in windows))
    listed = {cid: MarketInfo.from_gamma(r) for cid, r in raw.items()}
    nonsports = {cid for cid, m in listed.items() if m.category != "sports" and not m.is_sports}
    say(f"{len(listed)} closed markets listed, {len(nonsports)} not sports; reading their resolutions")
    markets = await app.markets.ensure(nonsports)
    return {cid: m for cid, m in markets.items()
            if m.category != "sports" and m.resolved and m.payouts is not None and m.resolved_at is not None
            and start <= m.resolved_at < end}


async def large_fills(app: App, markets: dict[str, MarketInfo], *, min_usd: float, max_pages: int = 100,
                      say: Callable[[str], None]) -> tuple[dict[str, list[tuple[str, int, str, float]]], int]:
    """Every fill of at least ``min_usd`` in each market (both sides), cached in SQLite.

    Returns {condition_id: [(wallet, ts, side, usdc)]} and the number of markets whose
    history was longer than ``max_pages`` pages (the oldest fills are then missing).
    """
    db = app.db
    done = {r["condition_id"]: r for r in db.query("SELECT * FROM market_fill_fetches")}
    todo = [cid for cid in markets if cid not in done or done[cid]["min_usd"] > min_usd]
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "market fills", len(todo))

    async def fetch(cid: str) -> None:
        async with sem:
            try:
                rows = await app.data.trades(conditions=[cid], min_usd=min_usd, taker_only=False,
                                             max_pages=max_pages)
            except Exception as exc:
                log.warning("fills for %s failed: %s", cid, exc)
                ticker.tick()
                return
        with db.tx() as conn:
            conn.execute("DELETE FROM market_fills WHERE condition_id = ?", (cid,))
            conn.executemany("INSERT INTO market_fills (condition_id, wallet, ts, side, usdc) VALUES (?,?,?,?,?)",
                             [(cid, r.wallet, r.ts, r.side, r.usdc_size) for r in rows if r.wallet])
            conn.execute("INSERT OR REPLACE INTO market_fill_fetches (condition_id, min_usd, rows, truncated, "
                         "fetched_at) VALUES (?,?,?,?,?)",
                         (cid, min_usd, len(rows), int(len(rows) >= max_pages * 1000), app.clock()))
        ticker.tick()

    await asyncio.gather(*(fetch(cid) for cid in todo))
    out: dict[str, list[tuple[str, int, str, float]]] = defaultdict(list)
    for r in db.query("SELECT condition_id, wallet, ts, side, usdc FROM market_fills WHERE usdc >= ?", (min_usd,)):
        if r["condition_id"] in markets:
            out[r["condition_id"]].append((r["wallet"], r["ts"], r["side"], r["usdc"]))
    truncated = db.one("SELECT COUNT(*) AS n FROM market_fill_fetches WHERE truncated = 1")["n"]
    return out, int(truncated)


def candidates(fills: dict[str, list[tuple[str, int, str, float]]], *, max_age: int,
               start: int, end: int) -> set[str]:
    """Wallets with a buy inside [start, end) no more than ``max_age`` after their earliest
    visible fill.

    The first trade is never later than the earliest fill we can see, so a wallet whose
    earliest visible fill is older than ``max_age`` before every one of its buys in the
    window cannot be fresh at any of them, and is not worth a lookup.
    """
    earliest: dict[str, int] = {}
    buys: dict[str, list[int]] = defaultdict(list)
    for rows in fills.values():
        for wallet, ts, side, _ in rows:
            earliest[wallet] = min(earliest.get(wallet, ts), ts)
            if side == "BUY" and start <= ts < end:
                buys[wallet].append(ts)
    return {w for w, ts in buys.items() if any(t - earliest[w] <= max_age for t in ts)}


async def first_trades(app: App, wallets: set[str], *, say: Callable[[str], None]) -> dict[str, int | None]:
    """Each wallet's first Polymarket trade (GET /v2/activity, type TRADE, ascending, one row).
    Cached: a first trade never changes once it exists."""
    db, now = app.db, app.clock()
    known = {r["wallet"]: r for r in db.query("SELECT * FROM wallet_first_trades")}
    out = {w: known[w]["first_ts"] for w in wallets
           if w in known and (known[w]["first_ts"] is not None or now - known[w]["checked_at"] < DAY)}
    todo = sorted(wallets - set(out))
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "first trades", len(todo))

    async def one(w: str) -> None:
        async with sem:
            try:
                rows, _ = await app.data.activity(w, types=("TRADE",), direction="ASC", page_size=1, max_rows=1)
            except Exception as exc:
                log.warning("first trade of %s failed: %s", w, exc)
                ticker.tick()
                return
        first = min((r.ts for r in rows), default=None)
        out[w] = first
        db.execute("INSERT OR REPLACE INTO wallet_first_trades (wallet, first_ts, checked_at) VALUES (?,?,?)",
                   (w, first, now))
        ticker.tick()

    await asyncio.gather(*(one(w) for w in todo))
    return out


async def fresh_events(app: App, first: dict[str, int | None], fills: dict[str, list[tuple[str, int, str, float]]],
                       markets: dict[str, MarketInfo], *, start: int, end: int, max_age: int, min_usd: float,
                       window: int, max_rows: int, say: Callable[[str], None]) -> tuple[list[AlertEvent], int]:
    """Buy groups reaching ``min_usd`` within each fresh wallet's first ``max_age``."""
    # Only wallets that bought large in the window while still fresh are worth reading in full.
    fresh: dict[str, int] = {}
    for rows in fills.values():
        for w, ts, side, _ in rows:
            f = first.get(w)
            if side == "BUY" and f is not None and 0 <= ts - f <= max_age and start <= ts < end:
                fresh[w] = f
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "fresh wallets' activity", len(fresh))
    events: list[AlertEvent] = []

    async def one(w: str, f: int) -> None:
        async with sem:
            try:
                rows, _ = await app.data.activity(w, types=ACTIVITY_TYPES, start=f, end=f + max_age + window,
                                                  direction="ASC", max_rows=max_rows)
            except Exception as exc:
                log.warning("activity of %s failed: %s", w, exc)
                ticker.tick()
                return
        rows = [r for r in rows if r.ts <= f + max_age + window]
        for e in alert_events(w, rows, markets, max(start, f), min(end, f + max_age + 1), window=window,
                              min_usd=min_usd):
            if e.trigger_ts - f <= max_age:
                events.append(e)
        ticker.tick()

    await asyncio.gather(*(one(w, f) for w, f in fresh.items()))
    events.sort(key=lambda e: (e.trigger_ts, e.wallet, e.token_id))
    return events, len(fresh)


async def run_fresh_wallet_test(app: App, start: int, end: int, *, max_age_days: int = 14, min_usd: float = 1000.0,
                                detect_usd: float = 500.0, delay: int = 90, ask_premium: float = 0.01,
                                write: bool = True, progress: Callable[[str], None] | None = None) -> FreshReport:
    say = progress or (lambda _m: None)
    al, pp = app.cfg.alerts, app.cfg.paper
    max_age = int(timedelta(days=max_age_days).total_seconds())
    watch = Stopwatch(app.http)
    report = FreshReport(start, end, watch=watch)
    with watch.stage("markets (non-sports, resolved in the window)"):
        markets = await universe(app, start, end, say=say)
    report.markets = len(markets)
    say(f"{len(markets)} non-sports markets resolved in the window")
    with watch.stage(f"fills of at least ${detect_usd:,.0f}"):
        fills, report.truncated_markets = await large_fills(app, markets, min_usd=detect_usd, say=say)
    report.fills = sum(len(v) for v in fills.values())
    cands = candidates(fills, max_age=max_age, start=start, end=end)
    report.wallets_checked = len(cands)
    say(f"{report.fills} fills; {len(cands)} wallets could be fresh at one of their buys")
    with watch.stage("first trades"):
        first = await first_trades(app, cands, say=say)
    with watch.stage("fresh wallets' activity"):
        events, report.fresh_wallets = await fresh_events(
            app, first, fills, markets, start=start, end=end, max_age=max_age, min_usd=min_usd,
            window=al.dedupe_window_seconds, max_rows=5000, say=say)
    report.events = events
    jobs = []
    for e in events:
        m = markets[e.condition_id]
        if closed_before(m, e.trigger_ts + DETECTION_LAG):
            report.skipped["market closed"] += 1
        elif not al.min_price <= e.whale_price + ask_premium <= al.max_price:
            report.skipped[f"price outside {al.min_price:g}-{al.max_price:g}"] += 1
        else:
            jobs.append(e)
    say(f"{len(events)} alert-worthy fresh-wallet buys; {len(jobs)} to copy")
    prices: dict[tuple[str, int], tuple[int, float] | None] = {}
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "fill prices", len(jobs))

    async def look(e: AlertEvent) -> None:
        async with sem:
            try:
                prices[(e.token_id, e.trigger_ts)] = await price_after(app, e.token_id, e.trigger_ts + delay)
            except Exception as exc:
                log.warning("price lookup failed for %s: %s", e.token_id, exc)
                prices[(e.token_id, e.trigger_ts)] = None
        ticker.tick()

    with watch.stage("fill prices (CLOB minute history)"):
        await asyncio.gather(*(look(e) for e in jobs))
    for e in jobs:
        report.trades.append(simulate(e, markets[e.condition_id], prices.get((e.token_id, e.trigger_ts)),
                                      strategy=STRATEGY, stake=pp.stake_usd, delay=delay, ask_premium=ask_premium,
                                      max_slippage=pp.max_slippage))
    report.categories = {cid: m.category for cid, m in markets.items()}
    report.event_slugs = {cid: (m.event_slug or cid) for cid, m in markets.items()}
    if write:
        write_trades(app, report.trades, strategies=(STRATEGY,), state_key=STATE_KEY, run={
            "rule": "fresh wallet (first trade <= 14 days before) buy group >= $1,000 in a non-sports market",
            "start": iso(start), "end": iso(end), "max_age_days": max_age_days, "min_usd": min_usd,
            "detect_usd": detect_usd, "delay": delay, "ask_premium": ask_premium})
    return report


def describe(rep: FreshReport, *, samples: int = 10_000) -> str:
    def ci(trades: list[SimTrade], value) -> str:
        if not trades:
            return "no settled copies"
        m, lo, hi = two_way_bootstrap_mean_ci([value(t) for t in trades], [t.event.wallet for t in trades],
                                              [rep.event_slugs.get(t.event.condition_id, t.event.condition_id)
                                               for t in trades], samples=samples)
        return f"{m:+.1%}" + ("" if lo is None else f", 95% CI [{lo:+.1%}, {hi:+.1%}]") + f", n={len(trades)}"

    filled = [t for t in rep.trades if t.status == "FILLED" and t.settled]
    shadow = [t for t in rep.trades if t.settled and t.zero]
    lines = [
        f"Fresh-wallet test, {iso(rep.start)[:10]} to {iso(rep.end)[:10]} (rule fixed in advance; see whalewatcher/fresh.py)",
        f"  {rep.markets} non-sports markets resolved in the window, {rep.fills} large fills"
        + (f" ({rep.truncated_markets} markets' oldest fills cut off)" if rep.truncated_markets else ""),
        f"  {rep.wallets_checked} wallets checked against their first trade, {rep.fresh_wallets} fresh at a large buy",
        f"  {len(rep.events)} alert-worthy buy groups, {len(rep.trades)} copied "
        f"({sum(t.status == 'FILLED' for t in rep.trades)} filled, {sum(t.status == 'MISSED' for t in rep.trades)} missed)"
        + "".join(f"; no copy: {k} {v}" for k, v in sorted(rep.skipped.items())),
        "",
        f"  Copied 90 s later (the test): mean ROI per settled copy {ci(filled, lambda t: t.settled['pnl'] / t.cost)} "
        f"({len({t.event.wallet for t in filled})} wallets, "
        f"{len({rep.event_slugs.get(t.event.condition_id) for t in filled})} events)",
        f"  At the fresh wallet's own price (shadow): {ci(shadow, lambda t: t.settled['zero_pnl'] / t.zero[3])}",
    ]
    if filled:
        m, lo, hi = two_way_bootstrap_mean_ci([t.settled["pnl"] / t.cost for t in filled],
                                              [t.event.wallet for t in filled],
                                              [rep.event_slugs.get(t.event.condition_id) for t in filled],
                                              samples=samples)
        verdict = ("PASSES: the interval is above zero." if lo is not None and lo > 0 else
                   "FAILS: the interval includes zero." if lo is not None and hi >= 0 else
                   "FAILS: the interval is below zero." if lo is not None else "FAILS: too few copies for an interval.")
        lines.append(f"  -> {verdict}")
    by: dict[str, list[SimTrade]] = defaultdict(list)
    for t in filled:
        by[rep.categories.get(t.event.condition_id, "?")].append(t)
    if by:
        lines.append("")
        lines.append("  By category (descriptive only: the verdict is on the total):")
        for cat, ts in sorted(by.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"    {cat:12s} {ci(ts, lambda t: t.settled['pnl'] / t.cost)}")
    if rep.watch:
        lines.append("")
        lines.append("Where the time went:\n" + rep.watch.table())
    return "\n".join(lines)
