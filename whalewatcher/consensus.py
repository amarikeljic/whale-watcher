"""v2 Phase 3b: the consensus strategy.

Hypothesis: when several INDEPENDENT top traders take the same side of a market,
that side wins more often than any one whale's trade. This module tests it; it
does not assume it.

  the pool      top traders by 30-day profit, minus market makers and bots
                (transactions a day, two-sided ratio). Separate from the watchlist.
  entities      wallets that are probably one person or fund are merged, and
                consensus counts entities, not wallets. Two tests, both on the last
                30 days only: nearly the same set of markets, or repeatedly buying
                the same outcome within seconds of each other.
                NOT done: "funded from the same address". That needs a chain node
                and this project uses free public APIs only. It is the strongest of
                the three tests in the brief, so some twins will get through.
  the signal    at least N entities hold a net position of min_position_usd or more
                in the same outcome, their entries are within window_hours of each
                other, and no pool entity holds another outcome of that market. The
                signal fires on the fill that completes it.
  the backtest  replays six months. On every day the pool is rebuilt from data
                available that day only: a wallet's profit is what it realised on
                markets that resolved in the 30 days before, and a wallet cannot be
                in the pool before the date a leak-free candidate search first found
                it. A pool taken from today's leaderboard and applied to the past
                would be hindsight.

Two honest gaps between the backtest pool and the live one, stated wherever the
numbers are printed: (1) history has no leaderboard, so the backtest ranks on
REALISED profit of markets that resolved in the window, while the live board is
marked-to-market equity change; (2) the backtest only knows the wallets our
candidate search found (large trades in high-volume markets), not every trader.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .app import App
from .backtest import AlertEvent, SimTrade, closed_before, price_after, simulate
from .config import ConsensusConfig, ConsensusPoolConfig
from .horizon import close_skip, scheduled_end
from .polymarket import Activity, MarketInfo
from .scoring import DAY, analyse, candidate_pool, load_wallet_rows
from .stats_util import cluster_bootstrap_mean_ci, max_drawdown, mean
from .timeutil import add_months, iso, start_of_day

log = logging.getLogger(__name__)

WINDOW = 30 * DAY
COTRADE_SECONDS = 10
MIN_MARKETS_FOR_JACCARD = 5
# The four pools the brief asks to compare: (label, minimum 30-day profit, top N).
VARIANTS: tuple[tuple[str, float, int], ...] = (
    ("100k", 100_000.0, 0), ("250k", 250_000.0, 0), ("500k", 500_000.0, 0), ("top50", 0.0, 50))


# -- one wallet's history, indexed for "as of day d" questions -----------------------------


@dataclass
class WalletHistory:
    wallet: str
    discovered: int                                   # first date a leak-free search found it
    trades: list[Activity] = field(default_factory=list)          # TRADE rows, oldest first
    settled_ts: list[int] = field(default_factory=list)           # resolution times, sorted
    settled_cum: list[float] = field(default_factory=list)        # running sum of market P&L
    tx_ts: list[int] = field(default_factory=list)                # one entry per transaction, sorted
    market_buys: dict[str, list[int]] = field(default_factory=dict)   # market -> its buy times, sorted
    two_sided_since: dict[str, int] = field(default_factory=dict)     # market -> when a second outcome was bought

    @classmethod
    def build(cls, wallet: str, discovered: int, rows: Iterable[Activity],
              settled: Iterable[tuple[int, float]]) -> WalletHistory:
        h = cls(wallet, discovered)
        h.trades = sorted((r for r in rows if r.type == "TRADE" and r.token_id and r.size > 0),
                          key=lambda r: (r.ts, r.fill_key or ""))
        total = 0.0
        for ts, pnl in sorted(settled):
            total += pnl
            h.settled_ts.append(ts)
            h.settled_cum.append(total)
        h.tx_ts = sorted({r.tx_hash: r.ts for r in h.trades}.values())
        first_by_outcome: dict[str, dict[str, int]] = defaultdict(dict)
        for r in h.trades:
            if r.side != "BUY":
                continue
            h.market_buys.setdefault(r.condition_id, []).append(r.ts)
            first_by_outcome[r.condition_id].setdefault(r.token_id, r.ts)
        for cond, firsts in first_by_outcome.items():
            if len(firsts) >= 2:
                h.two_sided_since[cond] = sorted(firsts.values())[1]
        return h

    def profit(self, day: int) -> float:
        """Realised P&L of markets that resolved in the 30 days before ``day``."""
        def upto(ts: int) -> float:
            i = bisect.bisect_left(self.settled_ts, ts)
            return self.settled_cum[i - 1] if i else 0.0
        return upto(day) - upto(day - WINDOW)

    def trades_per_day(self, day: int) -> float:
        return (bisect.bisect_left(self.tx_ts, day) - bisect.bisect_left(self.tx_ts, day - WINDOW)) / 30.0

    def markets(self, day: int) -> set[str]:
        """Markets with a buy in the 30 days before ``day``."""
        out = set()
        for cond, times in self.market_buys.items():
            i = bisect.bisect_left(times, day - WINDOW)
            if i < len(times) and times[i] < day:
                out.add(cond)
        return out

    def two_sided_ratio(self, day: int) -> float | None:
        active = self.markets(day)
        if not active:
            return None
        return sum(1 for c in active if self.two_sided_since.get(c, day) < day) / len(active)

    def cotrade_keys(self, day: int) -> set[tuple[str, int]]:
        lo = bisect.bisect_left(self.trades, day - WINDOW, key=lambda r: r.ts)
        hi = bisect.bisect_left(self.trades, day, key=lambda r: r.ts)
        return {(r.token_id, r.ts // COTRADE_SECONDS) for r in self.trades[lo:hi] if r.side == "BUY"}


# -- the pool ------------------------------------------------------------------------------


@dataclass(slots=True)
class PoolRow:
    wallet: str
    profit: float
    trades_per_day: float
    two_sided: float | None
    reasons: list[str]

    @property
    def passed(self) -> bool:
        return not self.reasons


def activity_reasons(trades_per_day: float, two_sided: float | None, rules: ConsensusPoolConfig) -> list[str]:
    out = []
    if trades_per_day < rules.min_trades_per_day:
        out.append(f"{trades_per_day:.1f} trades a day < {rules.min_trades_per_day:g}")
    if trades_per_day > rules.max_trades_per_day:
        out.append(f"{trades_per_day:.0f} trades a day > {rules.max_trades_per_day:g} (market maker or bot)")
    if two_sided is not None and two_sided > rules.max_two_sided_ratio:
        out.append(f"buys both sides in {two_sided:.0%} of its markets (limit {rules.max_two_sided_ratio:.0%})")
    return out


def select(rows: list[PoolRow], min_profit: float, top_n: int) -> list[PoolRow]:
    """The pool for one definition: activity rules first, then the profit bar or the top N."""
    ok = sorted((r for r in rows if r.passed and r.profit > 0), key=lambda r: -r.profit)
    return ok[:top_n] if top_n > 0 else [r for r in ok if r.profit >= min_profit]


def pool_rows(histories: dict[str, WalletHistory], day: int, rules: ConsensusPoolConfig, *,
              floor: float) -> list[PoolRow]:
    """Every wallet known by ``day`` with at least ``floor`` profit, judged on the 30 days before it."""
    out = []
    for h in histories.values():
        if h.discovered > day:
            continue
        profit = h.profit(day)
        if profit < floor:
            continue
        tpd = h.trades_per_day(day)
        two = h.two_sided_ratio(day) if rules.min_trades_per_day <= tpd <= rules.max_trades_per_day else None
        out.append(PoolRow(h.wallet, profit, tpd, two, activity_reasons(tpd, two, rules)))
    return out


# -- entities -----------------------------------------------------------------------------------


def entities(wallets: list[str], markets: dict[str, set[str]], cotrades: dict[str, set[tuple[str, int]]],
             rules: ConsensusPoolConfig) -> dict[str, str]:
    """wallet -> entity id (the alphabetically first wallet of its group)."""
    parent = {w: w for w in wallets}

    def find(w: str) -> str:
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    for i, a in enumerate(wallets):
        for b in wallets[i + 1:]:
            ma, mb = markets.get(a, set()), markets.get(b, set())
            same = False
            if len(ma) >= MIN_MARKETS_FOR_JACCARD and len(mb) >= MIN_MARKETS_FOR_JACCARD:
                same = len(ma & mb) / len(ma | mb) >= rules.entity_min_jaccard
            if not same:
                ca, cb = cotrades.get(a, set()), cotrades.get(b, set())
                both = len(ca & cb)
                same = (both >= rules.entity_min_cotrades and min(len(ca), len(cb)) > 0
                        and both / min(len(ca), len(cb)) >= rules.entity_min_cotrade_share)
            if same:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {w: find(w) for w in wallets}


# -- the signal engine (the same code replays history and, later, runs live) ---------------------


@dataclass(slots=True)
class Entry:
    entity: str
    ts: int
    price: float
    usd: float


@dataclass
class Signal:
    condition_id: str
    token_id: str
    outcome_index: int | None
    ts: int                       # block time of the fill that completed the consensus
    wallet: str                   # ...and who made it
    price: float                  # ...and its price
    entries: list[Entry]          # the agreeing entities, oldest first
    opposing: int

    @property
    def first(self) -> Entry:
        return self.entries[0]


class Engine:
    """Tracks every known wallet's net position and fires when the pool agrees.

    Positions and entry times are kept for every wallet all the time, in or out of
    the pool: a wallet that joins the pool already holding a month-old position has
    a month-old entry, not a fresh one. Pool membership is only asked at the moment
    a signal is checked."""

    def __init__(self, rules: ConsensusConfig) -> None:
        self.rules = rules
        self.shares: dict[tuple[str, str], float] = defaultdict(float)     # (wallet, token) -> net shares
        self.cost: dict[tuple[str, str], float] = defaultdict(float)       # ...and what they cost
        self.entries: dict[tuple[str, str], Entry] = {}    # (wallet, token) -> when it reached min_position_usd
        self.tokens: dict[str, set[str]] = defaultdict(set)                # market -> outcome tokens seen
        self.holders: dict[str, set[str]] = defaultdict(set)               # token -> wallets that bought it
        self.fired: set[str] = set()                                       # one signal per outcome token
        self.blocked_by_opposition = 0
        self.new_entries: list[tuple[Entry, Activity]] = []    # entries made by pool wallets, for the baseline

    def on_fill(self, fill: Activity, entity_of: Callable[[str], str | None]) -> Signal | None:
        """Apply one fill. ``entity_of`` maps a wallet to its entity if it is in the pool
        NOW (at this fill), else None. Returns a signal if this fill completed one."""
        key = (fill.wallet, fill.token_id)
        self.tokens[fill.condition_id].add(fill.token_id)
        if fill.side == "BUY":
            self.shares[key] += fill.size
            self.cost[key] += fill.price * fill.size
            self.holders[fill.token_id].add(fill.wallet)
        elif fill.side == "SELL" and self.shares[key] > 0:
            keep = max(0.0, self.shares[key] - fill.size) / self.shares[key]
            self.shares[key] *= keep
            self.cost[key] *= keep
        else:
            return None
        usd = self.cost[key]
        if usd < self.rules.min_position_usd:
            self.entries.pop(key, None)           # never reached it, or sold down: not agreeing
            return None
        if fill.side != "BUY":
            return None
        in_pool = entity_of(fill.wallet) is not None
        if key not in self.entries:
            entry = Entry(fill.wallet, fill.ts, usd / self.shares[key], usd)
            self.entries[key] = entry
            if in_pool:
                self.new_entries.append((entry, fill))
        else:
            self.entries[key].usd = usd
        if not in_pool or fill.token_id in self.fired:
            return None
        return self._check(fill, entity_of)

    def side(self, token: str, entity_of: Callable[[str], str | None]) -> dict[str, Entry]:
        """Pool entities holding min_position_usd or more of ``token``: entity -> its entry
        (the earliest of its wallets' entries, with their positions added up)."""
        out: dict[str, Entry] = {}
        for w in self.holders[token]:
            e, ent = self.entries.get((w, token)), entity_of(w)
            if e is None or ent is None:
                continue
            cur = out.get(ent)
            if cur is None:
                out[ent] = Entry(ent, e.ts, e.price, e.usd)
            else:
                cur.usd += e.usd
                if e.ts < cur.ts:
                    cur.ts, cur.price = e.ts, e.price
        return out

    def _check(self, fill: Activity, entity_of: Callable[[str], str | None]) -> Signal | None:
        r = self.rules
        fresh = fill.ts - r.window_hours * 3600
        agreeing = sorted((e for e in self.side(fill.token_id, entity_of).values() if e.ts >= fresh),
                          key=lambda e: (e.ts, e.entity))
        if len(agreeing) < r.min_agreeing_entities:
            return None
        opposing: set[str] = set()
        for other in self.tokens[fill.condition_id] - {fill.token_id}:
            opposing |= set(self.side(other, entity_of))
        if len(opposing) > r.max_opposing_entities:
            self.blocked_by_opposition += 1
            return None
        self.fired.add(fill.token_id)
        return Signal(fill.condition_id, fill.token_id, fill.outcome_index, fill.ts, fill.wallet, fill.price,
                      agreeing, len(opposing))


# -- the backtest --------------------------------------------------------------------------------------


@dataclass
class GroupStats:
    label: str
    trades: int = 0
    filled: int = 0
    missed: int = 0
    settled: int = 0
    events: int = 0
    pnl: float = 0.0
    cost: float = 0.0
    roi: float | None = None
    mean_roi: float | None = None
    ci: tuple[float | None, float | None] = (None, None)
    win_rate: float | None = None
    drawdown: float = 0.0
    clv: float | None = None
    clv_n: int = 0


@dataclass
class VariantResult:
    label: str
    pool_sizes: list[int] = field(default_factory=list)       # wallets in the pool, per day
    entity_counts: list[int] = field(default_factory=list)    # entities, per weekly regrouping
    merged: int = 0                                           # most wallets merged into others in any week
    signals: list[Signal] = field(default_factory=list)
    blocked: int = 0
    entries: int = 0
    consensus: GroupStats | None = None
    single: GroupStats | None = None
    random: GroupStats | None = None
    paired: tuple[int, float | None, float | None, float | None] = (0, None, None, None)
    runup: float | None = None
    hours_to_complete: float | None = None


@dataclass
class ConsensusReport:
    start: int
    end: int
    wallets: int
    too_active: int
    rules: ConsensusConfig
    pool_rules: ConsensusPoolConfig
    stake: float = 10.0
    max_slippage: float = 0.03
    variants: list[VariantResult] = field(default_factory=list)
    max_days_to_close: float | None = None
    cutoff_skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))   # "<group>:<pool>" -> n


def _event(market: MarketInfo | None, condition_id: str) -> str:
    return (market.event_slug if market else None) or condition_id


def group_stats(label: str, trades: list[SimTrade], markets: dict[str, MarketInfo], samples: int) -> GroupStats:
    g = GroupStats(label, trades=len(trades))
    g.filled = sum(1 for t in trades if t.status == "FILLED")
    g.missed = g.trades - g.filled
    done = sorted((t for t in trades if t.status == "FILLED" and t.settled is not None),
                  key=lambda t: (t.settled["settled_at"], t.event.token_id))        # type: ignore[index]
    g.settled = len(done)
    if not done:
        return g
    by_event: dict[str, list[float]] = defaultdict(list)
    for t in done:
        by_event[_event(markets.get(t.event.condition_id), t.event.condition_id)].append(t.settled["pnl"] / t.cost)
    g.events = len(by_event)
    g.pnl = sum(t.settled["pnl"] for t in done)
    g.cost = sum(t.cost for t in done)
    g.roi = g.pnl / g.cost if g.cost else None
    # Trades in one event rise and fall together, so whole events are resampled.
    g.mean_roi, lo, hi = cluster_bootstrap_mean_ci(list(by_event.values()), samples=samples)
    g.ci = (lo, hi)
    g.win_rate = sum(1 for t in done if t.settled["pnl"] > 0) / len(done)
    g.drawdown = max_drawdown([t.settled["pnl"] for t in done])
    return g


async def load_histories(app: App, cutoffs: list[int], end: int, *, say: Callable[[str], None],
                         ) -> tuple[dict[str, WalletHistory], dict[str, MarketInfo], int]:
    """Every wallet a leak-free candidate search found at each cutoff, with its history."""
    sc = app.cfg.scoring
    discovered: dict[str, int] = {}
    for c in cutoffs:
        pool, _ = await candidate_pool(app, c, historical=True)
        say(f"candidates found with data before {iso(c)[:10]}: {len(pool)}")
        for w in pool:
            discovered.setdefault(w, c)

    def window(c: int) -> tuple[int, int]:      # the same window `backtest` fetched, so the cache is reused
        return add_months(c, -sc.lookback_months) - sc.entry_lookback_days * DAY, end

    rate = sc.max_activity_rows / max(1, cutoffs[0] - window(cutoffs[0])[0])
    sem = asyncio.Semaphore(8)
    done = 0

    async def fetch(w: str) -> tuple[str, list[Activity] | None]:
        nonlocal done
        s, e = window(discovered[w])
        async with sem:
            try:
                rows, truncated = await load_wallet_rows(app, w, s, e, max_rows=int(rate * (e - s)))
            except Exception as exc:  # noqa: BLE001 - one wallet failing must not sink the run
                log.warning("fetch failed for %s: %s", w, exc)
                rows, truncated = [], True
        done += 1
        if done % 100 == 0:
            say(f"wallet activity: {done}/{len(discovered)}")
        return w, None if truncated else rows

    fetched = await asyncio.gather(*(fetch(w) for w in discovered))
    rows_by_wallet = {w: rows for w, rows in fetched if rows is not None}
    too_active = len(discovered) - len(rows_by_wallet)
    say(f"loading markets for {len(rows_by_wallet)} wallets ({too_active} too active to read)")
    markets = await app.markets.ensure({r.condition_id for rows in rows_by_wallet.values() for r in rows})
    first = cutoffs[0] - WINDOW
    histories = {}
    for w, rows in rows_by_wallet.items():
        a = analyse(w, rows, markets, end, sc, window_start=first)
        histories[w] = WalletHistory.build(w, discovered[w], rows, [(s.resolved_at, s.pnl) for s in a.settled])
    return histories, markets, too_active


def replay(histories: dict[str, WalletHistory], start: int, end: int, rules: ConsensusConfig,
           pool_rules: ConsensusPoolConfig, min_profit: float, top_n: int, label: str,
           ) -> tuple[VariantResult, list[tuple[Entry, Activity]]]:
    """Walk the period day by day: rebuild the pool each day, regroup entities each week,
    feed every fill of every known wallet through the engine."""
    res = VariantResult(label)
    engine = Engine(rules)
    fills = sorted((r for h in histories.values() for r in h.trades if r.ts < end),
                   key=lambda r: (r.ts, r.fill_key or ""))
    entity_map: dict[str, str] = {}
    members: set[str] = set()
    floor = min_profit if top_n == 0 else 0.0
    i, day = 0, start_of_day(start)
    while day < end:
        if day >= start:
            pool = select(pool_rows(histories, day, pool_rules, floor=floor), min_profit, top_n)
            members = {r.wallet for r in pool}
            res.pool_sizes.append(len(members))
            unknown = members - set(entity_map)
            if unknown or (day - start_of_day(start)) % (7 * DAY) == 0:
                ordered = sorted(members)
                entity_map = entities(ordered, {w: histories[w].markets(day) for w in ordered},
                                      {w: histories[w].cotrade_keys(day) for w in ordered}, pool_rules)
                if (day - start_of_day(start)) % (7 * DAY) == 0:
                    groups = len(set(entity_map.values()))
                    res.entity_counts.append(groups)
                    res.merged = max(res.merged, len(ordered) - groups)

        def entity_of(w: str, members: set[str] = members, entity_map: dict[str, str] = entity_map) -> str | None:
            return entity_map.get(w, w) if w in members else None

        while i < len(fills) and fills[i].ts < day + DAY:
            f = fills[i]
            i += 1
            signal = engine.on_fill(f, entity_of if f.ts >= start else (lambda _w: None))
            if signal is not None:
                res.signals.append(signal)
        day += DAY
    res.blocked = engine.blocked_by_opposition
    res.entries = len(engine.new_entries)
    return res, engine.new_entries


def _as_event(condition_id: str, token_id: str, index: int | None, ts: int, wallet: str, price: float,
              usd: float, market: MarketInfo | None, n: int = 1) -> AlertEvent:
    if index is None and market is not None:
        index = market.outcome_index_of(token_id)
    outcome = market.outcomes[index] if market and index is not None and index < len(market.outcomes) else ""
    return AlertEvent(wallet=wallet, condition_id=condition_id, token_id=token_id, outcome=outcome,
                      outcome_index=index, first_fill_ts=ts, trigger_ts=ts, last_fill_ts=ts, whale_price=price,
                      whale_usd=usd, whale_shares=0.0, fills=n)


async def run_backtest(app: App, cutoffs: list[int], end: int, *, samples: int = 4000, max_single: int = 1500,
                       random_draws: int = 3, progress: Callable[[str], None] | None = None) -> ConsensusReport:
    say = progress or (lambda _m: None)
    cfg, rules, pool_rules, al = app.cfg, app.cfg.consensus, app.cfg.consensus_pool, app.cfg.alerts
    cutoffs = sorted(cutoffs)
    start = cutoffs[0]
    histories, markets, too_active = await load_histories(app, cutoffs, end, say=say)
    report = ConsensusReport(start, end, len(histories), too_active, rules, pool_rules, cfg.paper.stake_usd,
                             cfg.bot.max_slippage, max_days_to_close=cfg.filters.max_days_to_close)
    stake, delay = cfg.paper.stake_usd, rules.backtest_delay_seconds
    sem = asyncio.Semaphore(16)

    # For the random-market baseline: what traded when, and at what price.
    bucket = 6 * 3600
    seen: dict[int, dict[str, tuple[str, float, int | None]]] = defaultdict(dict)
    for h in histories.values():
        for r in h.trades:
            seen[r.ts // bucket][r.token_id] = (r.condition_id, r.price, r.outcome_index)

    async def sim(event: AlertEvent, strategy: str, *, anchored: bool = True) -> SimTrade | None:
        market = markets.get(event.condition_id)
        if market is None:
            return None
        # The time-to-close cutoff, the same for the signal and for both baselines. The
        # end date is the one stored today: Gamma keeps no history of it.
        if close_skip(scheduled_end(market.end_date, market.game_start_time), event.trigger_ts + delay,
                      cfg.filters) is not None:
            report.cutoff_skipped[strategy] += 1
            return None
        async with sem:
            price = None if closed_before(market, event.trigger_ts + delay) else \
                await price_after(app, event.token_id, event.trigger_ts + delay)
        if not anchored and price is not None:
            event.whale_price = price[1]        # no whale to anchor to: take the market as it was
        return simulate(event, market, price, strategy=strategy, stake=stake, delay=delay,
                        ask_premium=rules.ask_premium, max_slippage=cfg.bot.max_slippage)

    for label, min_profit, top_n in VARIANTS:
        say(f"replaying the {label} pool")
        res, entries = replay(histories, start, end, rules, pool_rules, min_profit, top_n, label)
        in_band = [s for s in res.signals if al.min_price <= s.price <= al.max_price]
        say(f"  {label}: {len(res.signals)} signals ({len(in_band)} inside the price band); pricing them")
        events = [_as_event(s.condition_id, s.token_id, s.outcome_index, s.ts, s.wallet, s.price,
                            sum(e.usd for e in s.entries), markets.get(s.condition_id), len(s.entries))
                  for s in in_band]
        trades = [t for t in await asyncio.gather(*(sim(e, f"consensus:{label}") for e in events)) if t is not None]
        res.consensus = group_stats(f"consensus ({label})", trades, markets, samples)
        await _clv(app, res.consensus, trades, markets)
        filled = {t.event.token_id: t for t in trades if t.status == "FILLED"}
        by_token = {s.token_id: s for s in in_band}
        ups = [t.fill_price - by_token[tok].first.price for tok, t in filled.items()]
        res.runup = mean(ups)
        res.hours_to_complete = mean([(s.ts - s.first.ts) / 3600 for s in in_band])

        # Baseline 1: copy every single pool entity's entry, same fill rule.
        singles = [(e, f) for e, f in entries if al.min_price <= f.price <= al.max_price]
        rng = random.Random(f"single:{label}")
        if len(singles) > max_single:
            singles = rng.sample(singles, max_single)
        single_trades = [t for t in await asyncio.gather(*(
            sim(_as_event(f.condition_id, f.token_id, f.outcome_index, f.ts, f.wallet, f.price, e.usd,
                          markets.get(f.condition_id)), f"single:{label}") for e, f in singles)) if t is not None]
        res.single = group_stats(f"any single pool entity ({label})", single_trades, markets, samples)

        # Baseline 2: at the same moment, a random other market trading at a similar price.
        rng = random.Random(f"random:{label}")
        jobs: list[tuple[str, AlertEvent]] = []
        for s in in_band:
            own = _event(markets.get(s.condition_id), s.condition_id)
            options = []
            for b in (s.ts // bucket, s.ts // bucket - 1):
                for token, (cond, price, index) in seen.get(b, {}).items():
                    m = markets.get(cond)
                    if (m is None or abs(price - s.price) > 0.05 or _event(m, cond) == own
                            or closed_before(m, s.ts + delay) or m.outcome_index_of(token) is None):
                        continue
                    options.append((token, cond, price, index))
            options.sort()
            for token, cond, price, index in rng.sample(options, min(random_draws, len(options))):
                jobs.append((s.token_id, _as_event(cond, token, index, s.ts, "random", price, 0.0, markets.get(cond))))
        drawn = await asyncio.gather(*(sim(e, f"random:{label}", anchored=False) for _, e in jobs))
        random_trades = [t for t in drawn if t is not None]
        res.random = group_stats(f"random market, similar price, same moment ({label})", random_trades, markets,
                                 samples)
        # Paired: each signal against the mean of its own random draws.
        control: dict[str, list[float]] = defaultdict(list)
        for (tok, _), t in zip(jobs, drawn, strict=True):
            if t is not None and t.status == "FILLED" and t.settled is not None:
                control[tok].append(t.settled["pnl"] / t.cost)
        diffs: dict[str, list[float]] = defaultdict(list)
        for tok, t in filled.items():
            if t.settled is not None and control.get(tok):
                diffs[_event(markets.get(t.event.condition_id), t.event.condition_id)].append(
                    t.settled["pnl"] / t.cost - sum(control[tok]) / len(control[tok]))
        m, lo, hi = cluster_bootstrap_mean_ci(list(diffs.values()), samples=samples)
        res.paired = (sum(len(v) for v in diffs.values()), m, lo, hi)
        report.variants.append(res)
        _store_signals(app, f"backtest:{label}", in_band, {t.event.token_id: t for t in trades}, markets)
    return report


async def _clv(app: App, g: GroupStats, trades: list[SimTrade], markets: dict[str, MarketInfo]) -> None:
    """Closing line minus our fill, for fills made before the game or the close."""
    values = []
    for t in trades:
        market = markets.get(t.event.condition_id)
        ref = market.close_reference_ts() if market else None
        if t.status != "FILLED" or ref is None or t.fill_ts is None or t.fill_ts >= ref:
            continue
        point = await app.prices.at(t.event.token_id, ref - 1)
        if point is not None:
            values.append(point.price - t.fill_price)
    g.clv, g.clv_n = mean(values), len(values)


def _store_signals(app: App, source: str, signals: list[Signal], trades: dict[str, SimTrade],
                   markets: dict[str, MarketInfo]) -> None:
    """Every signal with who agreed, when, at what price, and how far the price had moved."""
    now = app.clock()
    with app.db.tx() as conn:
        conn.execute("DELETE FROM consensus_signals WHERE source = ?", (source,))
        for s in signals:
            t = trades.get(s.token_id)
            settled = (t.settled or {}) if t else {}
            market = markets.get(s.condition_id)
            index = s.outcome_index if s.outcome_index is not None else (
                market.outcome_index_of(s.token_id) if market else None)
            outcome = market.outcomes[index] if market and index is not None and index < len(market.outcomes) else None
            conn.execute(
                "INSERT INTO consensus_signals (source, condition_id, token_id, outcome, signal_ts, trigger_wallet, "
                "trigger_price, entities, opposing, first_entry_ts, first_entry_price, runup, details, status, "
                "miss_reason, fill_ts, fill_price, cost, pnl, payout_per_share, settled_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, s.condition_id, s.token_id, outcome, s.ts, s.wallet, s.price, len(s.entries), s.opposing,
                 s.first.ts, s.first.price, s.price - s.first.price,
                 json.dumps([{"entity": e.entity, "entry_ts": e.ts, "entry_price": round(e.price, 4),
                              "usd": round(e.usd, 2)} for e in s.entries]),
                 t.status if t else "UNPRICED", t.miss_reason if t else None, t.fill_ts if t else None,
                 t.fill_price if t else None, t.cost if t else None, settled.get("pnl"),
                 settled.get("payout_per_share"), settled.get("settled_at"), now))


# -- reporting -------------------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.1%}"


def _group_line(g: GroupStats | None) -> str:
    if g is None or not g.trades:
        return "nothing to trade"
    head = f"{g.trades} signals, {g.filled} filled, {g.missed} missed, {g.settled} settled"
    if not g.settled:
        return head + ": no settled trades, so no P&L"
    ci = "-" if g.ci[0] is None else f"[{_pct(g.ci[0])}, {_pct(g.ci[1])}]"
    return (f"{head} in {g.events} events | ROI {_pct(g.roi)} (${g.pnl:+,.2f} on ${g.cost:,.0f}) | mean per trade "
            f"{_pct(g.mean_roi)}, 95% CI {ci} | win rate {g.win_rate:.0%} | max drawdown ${g.drawdown:,.2f}")


def describe(rep: ConsensusReport) -> str:
    r, p = rep.rules, rep.pool_rules
    lines = [
        f"Consensus backtest, {iso(rep.start)[:10]} to {iso(rep.end)[:10]}",
        f"  signal: {r.min_agreeing_entities}+ entities each holding ${r.min_position_usd:,.0f}+ of one outcome, "
        f"entries within {r.window_hours:g}h, at most {r.max_opposing_entities} pool entities on another outcome",
        f"  pool, rebuilt every day from the 30 days before it: {p.min_trades_per_day:g}-{p.max_trades_per_day:g} "
        f"trades a day, buys both sides in at most {p.max_two_sided_ratio:.0%} of its markets, plus the profit bar below",
        f"  fill: {r.backtest_delay_seconds}s after the completing fill at the CLOB minute price + "
        f"{r.ask_premium:g}, limit = that fill's price + {rep.max_slippage:g}, fees, ${rep.stake:g}, held to resolution",
        f"  universe: {rep.wallets} wallets found by leak-free candidate searches ({rep.too_active} more were too "
        "active to read). Profit here is REALISED on markets that resolved in the window; the live leaderboard "
        "is marked-to-market. This is a sample of traders, not all of them.",
        "",
    ]
    for v in rep.variants:
        sizes = sorted(v.pool_sizes) or [0]
        ents = sorted(v.entity_counts) or [0]
        lines.append(f"== pool {v.label} ==" if v.label != "top50" else
                     "== pool top50 (the 50 most profitable wallets that pass the activity rules) ==")
        lines.append(f"  wallets in the pool per day: median {sizes[len(sizes) // 2]} (min {sizes[0]}, max {sizes[-1]}) | "
                     f"entities per week: median {ents[len(ents) // 2]} | most wallets merged into another in "
                     f"any week: {v.merged}")
        lines.append(f"  entity entries of ${r.min_position_usd:,.0f}+: {v.entries} | signals: {len(v.signals)} | "
                     f"would-be signals blocked by an opposing pool entity: {v.blocked}")
        cut = rep.cutoff_skipped
        if any(cut.get(f"{g}:{v.label}") for g in ("consensus", "single", "random")):
            limit = "no end date" if rep.max_days_to_close is None else f"over {rep.max_days_to_close:g} days to close"
            lines.append(f"  not traded, time-to-close cutoff ({limit} or no end date): "
                         f"{cut.get(f'consensus:{v.label}', 0)} signals, {cut.get(f'single:{v.label}', 0)} "
                         f"single entries, {cut.get(f'random:{v.label}', 0)} random draws")
        lines.append(f"  consensus:  {_group_line(v.consensus)}")
        if v.consensus and v.consensus.settled:
            c = v.consensus
            clv = "-" if c.clv is None else f"{c.clv:+.3f}"
            run = "-" if v.runup is None else f"{v.runup:+.3f}"
            hrs = "-" if v.hours_to_complete is None else f"{v.hours_to_complete:.1f}h"
            lines.append(f"              CLV {clv} (n={c.clv_n} pre-game fills) | price already moved {run} a share "
                         f"between the first entity's entry and our fill | first entry to signal: {hrs} on average")
        lines.append(f"  baseline 1: {_group_line(v.single)}")
        lines.append(f"  baseline 2: {_group_line(v.random)}")
        n, m, lo, hi = v.paired
        if n:
            ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
            verdict = ("consensus beats a random market" if lo is not None and lo > 0 else
                       "a random market beats consensus" if hi is not None and hi < 0 else
                       "no difference shown")
            lines.append(f"  consensus minus its own random-market draws, ROI per trade on {n} settled signals: "
                         f"{_pct(m)}, 95% CI {ci} -> {verdict}")
        lines.append("")
    lines.append(verdict_line(rep))
    return "\n".join(lines)


def verdict_line(rep: ConsensusReport) -> str:
    good = [v.label for v in rep.variants
            if v.consensus and v.consensus.ci[0] is not None and v.consensus.ci[0] > 0]
    thin = [v.label for v in rep.variants if not v.consensus or v.consensus.settled < 30]
    if good:
        return ("Verdict: PASSES for pool " + ", ".join(good) + ": the whole 95% interval is above zero. One "
                "backtest on minute prices is not proof; the live paper run is the test.")
    if len(thin) == len(rep.variants):
        return ("Verdict: NOT ENOUGH SIGNALS. No pool produced 30 settled signals, so nothing can be concluded "
                "either way. Loosening the signal rules would produce more, at the price of weaker agreement.")
    return ("Verdict: FAILS. No pool's 95% interval is above zero: on this evidence, agreement among these "
            "traders is not a signal you can profit from after costs.")


# -- today's pool, from the live leaderboard ------------------------------------------------------------


@dataclass
class LivePool:
    as_of: int
    rows: list[PoolRow]
    names: dict[str, str]
    ranks: dict[str, int]
    entity_of: dict[str, str]
    counts: dict[str, dict[str, int]]       # definition -> {board, passed, entities}
    run_id: int | None = None


LIVE_ROW_CAP = 30_000       # fills in 30 days beyond which a wallet is not read: 1,000 a day is a machine


async def live_pool(app: App, *, progress: Callable[[str], None] | None = None, save: bool = True) -> LivePool:
    """Today's consensus pool. Source: GET /v2/leaderboard?time_period=month&sort_by=PNL
    (spec https://data-api.polymarket.com/v2/openapi.json), which is polymarket.com's
    own board, the international platform; the US platform has a separate API that
    this project never calls. Board profit is marked-to-market and has been seen to
    disagree with Polymarket's per-wallet P&L series (docs/v2-research.md), so the
    board only nominates wallets; the activity rules and entity merging do the rest."""
    say = progress or (lambda _m: None)
    rules = app.cfg.consensus_pool
    day = start_of_day(app.clock())
    floor = min([m for _, m, n in VARIANTS if n == 0] + [rules.min_profit_30d_usd])
    board = [e for e in await app.data.leaderboard("month", category="overall", limit=1000) if e.pnl > 0]
    need = max([n for _, _, n in VARIANTS] + [rules.top_n])
    wanted = [e for i, e in enumerate(board) if e.pnl >= floor or i < need]
    say(f"leaderboard: {len(board)} wallets with a profit, reading the activity of {len(wanted)}")
    sem = asyncio.Semaphore(8)
    done = 0

    async def one(entry: Any) -> tuple[PoolRow, WalletHistory | None]:
        nonlocal done
        async with sem:
            try:
                rows, truncated = await load_wallet_rows(app, entry.wallet, day - WINDOW, app.clock(),
                                                         max_rows=LIVE_ROW_CAP)
            except Exception as exc:  # noqa: BLE001 - one wallet failing must not sink the pool
                log.warning("activity failed for %s: %s", entry.wallet, exc)
                rows, truncated = [], True
        done += 1
        if done % 50 == 0:
            say(f"activity: {done}/{len(wanted)}")
        if truncated:
            return PoolRow(entry.wallet, entry.pnl, float("inf"), None,
                           [f"too active to read (over {LIVE_ROW_CAP:,} fills in 30 days: market maker or bot)"]), None
        h = WalletHistory.build(entry.wallet, 0, rows, [])
        tpd, two = h.trades_per_day(day + DAY), h.two_sided_ratio(day + DAY)
        return PoolRow(entry.wallet, entry.pnl, tpd, two, activity_reasons(tpd, two, rules)), h

    results = await asyncio.gather(*(one(e) for e in wanted))
    rows = [r for r, _ in results]
    histories = {r.wallet: h for r, h in results if h is not None}
    ok = sorted(r.wallet for r in rows if r.passed)
    entity_of = entities(ok, {w: histories[w].markets(day + DAY) for w in ok},
                         {w: histories[w].cotrade_keys(day + DAY) for w in ok}, rules)
    ranks = {e.wallet: i for i, e in enumerate(board, start=1)}
    counts: dict[str, dict[str, int]] = {}
    definitions = list(VARIANTS) + [("configured", rules.min_profit_30d_usd, rules.top_n)]
    for label, min_profit, top_n in definitions:
        on_board = [r for r in rows if (ranks[r.wallet] <= top_n if top_n else r.profit >= min_profit)]
        # For "top N" this row answers "how many of the board's top N pass the rules";
        # the pool itself (select) then fills up to N from the next ranks that pass.
        chosen = [r for r in on_board if r.passed] if label != "configured" else select(rows, min_profit, top_n)
        counts[label] = {"board": len(on_board), "passed": len(chosen),
                         "entities": len({entity_of[r.wallet] for r in chosen})}
    pool = LivePool(day, rows, {e.wallet: e.name for e in board}, ranks, entity_of, counts)
    if save:
        with app.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO consensus_pool_runs (as_of, created_at, rules_json, counts_json) VALUES (?,?,?,?)",
                (day, app.clock(), json.dumps(rules.__dict__), json.dumps(counts)))
            pool.run_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO consensus_pool (run_id, wallet, name, entity, rank, profit_30d, trades_per_day, "
                "two_sided_ratio, passed, reasons) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(pool.run_id, r.wallet, pool.names.get(r.wallet), entity_of.get(r.wallet), ranks.get(r.wallet),
                  r.profit, None if r.trades_per_day == float("inf") else r.trades_per_day, r.two_sided,
                  int(r.passed), json.dumps(r.reasons)) for r in rows])
    return pool


def describe_pool(pool: LivePool, rules: ConsensusPoolConfig) -> str:
    lines = [f"Consensus pool as of {iso(pool.as_of)[:10]} (polymarket.com leaderboard, 30-day profit, marked to market)",
             f"  activity rules: {rules.min_trades_per_day:g}-{rules.max_trades_per_day:g} trades a day, buys both "
             f"sides in at most {rules.max_two_sided_ratio:.0%} of its markets",
             "",
             f"  {'definition':<22}{'on the board':>13}{'pass the rules':>16}{'entities':>10}"]
    names = {"100k": "$100k+ profit", "250k": "$250k+ profit", "500k": "$500k+ profit", "top50": "top 50 by profit"}
    for label, c in pool.counts.items():
        if label == "configured":
            continue
        lines.append(f"  {names.get(label, label):<22}{c['board']:>13}{c['passed']:>16}{c['entities']:>10}")
    c = pool.counts["configured"]
    bar = f"top {rules.top_n}" if rules.top_n else f"${rules.min_profit_30d_usd:,.0f}+"
    lines.append(f"  config.yaml now ({bar}): {c['passed']} wallets, {c['entities']} entities")
    why: dict[str, int] = defaultdict(int)
    for r in pool.rows:
        for reason in r.reasons:
            key = ("too active to read" if reason.startswith("too active") else
                   "too many trades a day" if "> " in reason and "trades a day" in reason else
                   "too few trades a day" if "trades a day" in reason else "buys both sides")
            why[key] += 1
    if why:
        lines.append("")
        lines.append(f"  dropped by the activity rules, of {len(pool.rows)} wallets read: "
                     + ", ".join(f"{k} {v}" for k, v in sorted(why.items(), key=lambda kv: -kv[1])))
    groups: dict[str, list[str]] = defaultdict(list)
    for w, e in pool.entity_of.items():
        groups[e].append(w)
    twins = {e: ws for e, ws in groups.items() if len(ws) > 1}
    lines.append("")
    lines.append(f"  wallets judged to be the same entity as another: {sum(len(ws) - 1 for ws in twins.values())} "
                 f"in {len(twins)} groups (same markets, or buying the same outcome within "
                 f"{COTRADE_SECONDS}s of each other)")
    for ws in sorted(twins.values(), key=len, reverse=True)[:8]:
        lines.append("    " + " = ".join(f"{pool.names.get(w) or w[:8]} (#{pool.ranks.get(w)})" for w in sorted(ws)))
    lines.append("  Not checked: wallets funded from the same address. That needs a chain node (free APIs only).")
    return "\n".join(lines)
