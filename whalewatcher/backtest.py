"""Replay the paper-trading strategy over past periods: walk-forward, delayed fills, fees, real payouts.

The question: had this system been running, choosing wallets the way it does with
only the data available at the time, what would its paper trades have made?

Periods     the optimize folds (back-to-back two-month holdouts). In each period the
            copied wallets are picked at the period's start from that period's leak-free
            candidate pool, with metrics computed from data before it, then ranked by
            ``scoring.rank_by`` and cut to ``scoring.top_k`` like the live service.
            Two rules pick them:
              walk-forward  the requirements the optimize search chooses on the EARLIER
                            periods only, so a period's own results never influence which
                            wallets it copies (none for the first period: nothing earlier).
              spec          the spec's original thresholds, fixed in advance.
            The live service re-picks nightly. Here the set is picked once per period, which
            is the main way this replay differs from what the service would have done.
Alerts      every buy group a copied wallet placed in the period that the monitor would
            have alerted on: at least ``alerts.min_whale_trade_usd`` within the dedupe
            window, grouped per wallet and token exactly like monitor.py. The live monitor
            checks the price band and the market's state against the order book when it
            alerts. There is no historical book, so the alert-time ask is approximated by
            the whale's price plus ``ask_premium``.
Fills       ``delay`` seconds after the fill that triggered the alert (90 s: the dry run's
            median, about 33 s to detect plus one poll), at the first CLOB minute price at
            or after that moment plus ``ask_premium`` (one cent, for the spread), plus the
            market's taker fee. MISSED when the market closed before the fill, no price
            was printed in the following 15 minutes, or the fill would be more than
            ``paper.max_slippage`` above the alert-time ask. The live engine walks the real
            book. A minute price plus a cent understates the cost of thin books.
Settlement  the market's actual payout per share (0.50 on a 50-50 void). Markets that have
            not resolved stay open, and the service's settle loop settles them later, so
            the numbers fill in over time like live trades do.

Trades are written as strategy ``backtest:<rule>`` with alerts of status BACKTEST. ``stats``
shows them in their own, clearly labelled sections and never mixes them into the live
numbers. Each run replaces the previous backtest rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .app import App
from .config import ScoringConfig
from .fees import fee_model
from .kalshi import KalshiMarket, KalshiStore, Quote, match, poly_game, quote_at, taker_fee
from .stats_util import two_way_bootstrap_mean_ci
from .optimize import LOOSEST, SPEC_REQUIREMENTS, Fold, FoldIndex, FoldWallet, choose, passes_all, search
from .paper import result_label
from .polymarket import Activity, MarketInfo
from .scoring import load_wallet_rows
from .stats import BACKTEST_PREFIX as STRATEGY_PREFIX
from .stats import BACKTEST_STATE_KEY
from .timeutil import iso
from .timing import Progress, Stopwatch

log = logging.getLogger(__name__)

VENUE = "polymarket"
PRICE_SOURCE = "clob-history-1m"
DETECTION_LAG = 33        # seconds from the triggering fill to the monitor seeing it (dry-run median)
PRICE_HORIZON = 900       # a fill needs a printed price within this many seconds


@dataclass(slots=True)
class AlertEvent:
    wallet: str
    condition_id: str
    token_id: str
    outcome: str
    outcome_index: int | None
    first_fill_ts: int
    trigger_ts: int          # the fill that took the group over the threshold
    last_fill_ts: int
    whale_price: float       # VWAP of the group's fills up to and including the trigger
    whale_usd: float         # the whole group
    whale_shares: float
    fills: int


def _groups(buys: list[Activity], window: int) -> Iterator[list[Activity]]:
    """A group starts at a fill and takes every later fill within ``window`` seconds of it."""
    group: list[Activity] = []
    for b in buys:
        if group and b.ts - group[0].ts > window:
            yield group
            group = []
        group.append(b)
    if group:
        yield group


def _trigger(group: list[Activity], min_usd: float) -> tuple[Activity, float] | None:
    """The fill that took the group to ``min_usd`` (USDC spent, fees included, as the monitor
    counts it), and the VWAP of the fill prices up to it (fees excluded, as in monitor.py)."""
    usd = shares = notional = 0.0
    for b in group:
        usd += b.usdc_size
        shares += b.size
        notional += b.price * b.size
        if usd >= min_usd and shares > 0:
            return b, notional / shares
    return None


def alert_events(wallet: str, rows: list[Activity], markets: dict[str, MarketInfo], start: int, end: int, *,
                 window: int, min_usd: float) -> list[AlertEvent]:
    """Every buy group triggered in [start, end) that reached ``min_usd``, grouped like monitor.py."""
    by_token: dict[tuple[str, str], list[Activity]] = defaultdict(list)
    for r in rows:
        if r.type == "TRADE" and r.side == "BUY" and r.size > 0 and start <= r.ts < end and r.token_id:
            by_token[(r.condition_id, r.token_id)].append(r)
    out: list[AlertEvent] = []
    for (cid, token), buys in by_token.items():
        m = markets.get(cid)
        if m is None:
            continue
        buys.sort(key=lambda r: (r.ts, r.fill_key or ""))
        index = m.outcome_index_of(token)
        if index is None:
            index = buys[0].outcome_index
        outcome = m.outcomes[index] if index is not None and index < len(m.outcomes) else buys[0].outcome
        for group in _groups(buys, window):
            hit = _trigger(group, min_usd)
            if hit is None:
                continue
            trigger, vwap = hit
            out.append(AlertEvent(
                wallet=wallet, condition_id=cid, token_id=token, outcome=outcome, outcome_index=index,
                first_fill_ts=group[0].ts, trigger_ts=trigger.ts, last_fill_ts=group[-1].ts, whale_price=vwap,
                whale_usd=sum(b.usdc_size for b in group), whale_shares=sum(b.size for b in group),
                fills=len(group)))
    out.sort(key=lambda e: (e.trigger_ts, e.token_id))
    return out


# -- one simulated paper trade ------------------------------------------------------------------


Shadow = tuple[float, float, float, float]   # price, contracts, fees, cost


@dataclass
class SimTrade:
    event: AlertEvent
    strategy: str
    status: str                    # FILLED | MISSED
    venue: str = VENUE
    contract: str | None = None    # Kalshi: "<ticker>:<yes|no>"; Polymarket: the token id
    miss_reason: str | None = None
    fill_ts: int | None = None
    fill_price: float | None = None
    contracts: float | None = None
    fees: float | None = None
    cost: float | None = None
    zero: Shadow | None = None     # zero latency, at the whale's price
    atask: Shadow | None = None    # at the (approximated) alert-time ask
    settled: dict[str, Any] | None = None


def _position(price: float, stake: float, market: MarketInfo) -> Shadow | None:
    if not 0 < price < 1:
        return None
    contracts = stake / price
    fees = fee_model(VENUE)([(price, contracts)], market)
    return price, contracts, fees, price * contracts + fees


def closed_before(market: MarketInfo, ts: int) -> bool:
    """Whether trading had stopped by ``ts``, judged from what is known about the market now."""
    for t in (market.closed_time, market.resolved_at):
        if t is not None and t <= ts:
            return True
    return False


def simulate(event: AlertEvent, market: MarketInfo, price: tuple[int, float] | None, *, strategy: str,
             stake: float, delay: int, ask_premium: float, max_slippage: float) -> SimTrade:
    """The paper engine's fill rule, replayed with a historical minute price instead of a live book."""
    t = SimTrade(event, strategy, "MISSED")
    alert_ask = min(0.999, event.whale_price + ask_premium)
    t.zero = _position(event.whale_price, stake, market)
    t.atask = _position(alert_ask, stake, market)
    fill_at = event.trigger_ts + delay
    t.fill_ts = fill_at
    if closed_before(market, fill_at):
        t.miss_reason = "market closed before the fill"
    elif price is None:
        t.miss_reason = f"no price printed within {PRICE_HORIZON // 60} minutes"
    else:
        ask = min(0.999, price[1] + ask_premium)
        t.fill_price = ask
        if ask - alert_ask > max_slippage + 1e-9:
            t.miss_reason = f"slippage: fill {ask:.3f} vs alert ask {alert_ask:.3f} (max {max_slippage:g})"
        else:
            pos = _position(ask, stake, market)
            assert pos is not None
            t.status, t.fill_ts = "FILLED", price[0]
            _, t.contracts, t.fees, t.cost = pos
    pps = market.payout_for(event.outcome_index) if market.resolved and market.payouts is not None else None
    if pps is not None:
        filled = t.status == "FILLED"

        def pnl(pos: Shadow | None) -> float | None:
            return None if pos is None else pos[1] * pps - pos[3]

        t.settled = {
            "settled_at": market.resolved_at, "payout_per_share": pps,
            "result": result_label(pps) if filled else None,
            "payout": t.contracts * pps if filled else None,
            "pnl": (t.contracts * pps - t.cost) if filled else 0.0,
            "zero_pnl": pnl(t.zero), "atask_pnl": pnl(t.atask),
        }
    return t


async def price_after(app: App, token_id: str, at: int, *, horizon: int = PRICE_HORIZON) -> tuple[int, float] | None:
    """The first CLOB minute price at or after ``at``, within ``horizon`` seconds. Cached in SQLite.

    https://docs.polymarket.com/api-spec/clob-openapi.yaml GET /prices-history (verified
    2026-11-07): ``market`` is the token id, ``startTs``/``endTs`` unix seconds, ``fidelity``
    minutes (default 1); the response is ``{"history": [{"t": int, "p": float}]}``. Limit
    1,000 req/10s (https://docs.polymarket.com/api-reference/rate-limits.md). Minute points
    were still served for markets five months old, where the Data API's own history had
    thinned to 30-minute buckets or nothing.
    """
    row = app.db.one("SELECT price, observed_ts FROM price_snapshots WHERE token_id = ? AND at_ts = ? AND source = ?",
                     (token_id, at, PRICE_SOURCE))
    if row is not None:
        return None if row["price"] is None else (int(row["observed_ts"]), float(row["price"]))
    payload = await app.http.get_json(f"{app.cfg.api.clob_api}/prices-history",
                                      {"market": token_id, "startTs": at, "endTs": at + horizon, "fidelity": 1})
    history = (payload.get("history") or []) if isinstance(payload, dict) else []
    points = sorted((int(p["t"]), float(p["p"])) for p in history if "t" in p and "p" in p)
    found = next(((ts, p) for ts, p in points if at <= ts <= at + horizon and 0 < p < 1), None)
    app.db.execute(
        "INSERT OR REPLACE INTO price_snapshots (token_id, at_ts, price, observed_ts, resolution_seconds, source, "
        "fetched_at) VALUES (?,?,?,?,?,?,?)",
        (token_id, at, None if found is None else found[1], None if found is None else found[0], 60, PRICE_SOURCE,
         app.clock()))
    return found


# -- the same copy on Kalshi --------------------------------------------------------------------------


def _kalshi_position(price: float | None, stake: float, multiplier: float) -> Shadow | None:
    if price is None or not 0 < price < 1:
        return None
    contracts = stake / price
    fees = taker_fee(contracts, price, multiplier)
    return price, contracts, fees, price * contracts + fees


def simulate_kalshi(event: AlertEvent, market: KalshiMarket, side: str, quotes: list[Quote], *, strategy: str,
                    stake: float, delay: int, max_slippage: float, fee_multiplier: float) -> SimTrade:
    """Copy a Polymarket alert by buying the matching Kalshi contract at Kalshi's own ask.

    Same rule as the Polymarket replay: the alert-time ask is Kalshi's ask when the
    alert is detected (trigger + 33 s), the fill is at Kalshi's ask ``delay`` seconds
    after the trigger, MISSED if that is more than ``max_slippage`` above the alert-time
    ask or the market closed first. Quotes are real minute candles (best bid and ask),
    so no spread has to be assumed. Taker fee per Kalshi's schedule; settlement at
    Kalshi's own result, which can differ from Polymarket's (fair-price settlements).
    """
    t = SimTrade(event, strategy, "MISSED", venue="kalshi", contract=f"{market.ticker}:{side}")
    q0 = quote_at(quotes, event.trigger_ts)
    qa = quote_at(quotes, event.trigger_ts + DETECTION_LAG)
    alert_ask = qa.ask(side) if qa else None
    t.zero = _kalshi_position(q0.ask(side) if q0 else None, stake, fee_multiplier)
    t.atask = _kalshi_position(alert_ask, stake, fee_multiplier)
    fill_at = event.trigger_ts + delay
    t.fill_ts = fill_at
    q = quote_at(quotes, fill_at)
    ask = q.ask(side) if q else None
    if not market.open_at(fill_at):
        t.miss_reason = "market closed before the fill"
    elif ask is None:
        t.miss_reason = "no Kalshi offer at the fill"
    else:
        t.fill_price = ask
        if alert_ask is not None and ask - alert_ask > max_slippage + 1e-9:
            t.miss_reason = f"slippage: fill {ask:.3f} vs alert ask {alert_ask:.3f} (max {max_slippage:g})"
        else:
            pos = _kalshi_position(ask, stake, fee_multiplier)
            assert pos is not None
            t.status, t.fill_ts = "FILLED", q.ts
            _, t.contracts, t.fees, t.cost = pos
    pps = market.payout(side)
    if pps is not None:
        filled = t.status == "FILLED"

        def pnl(pos: Shadow | None) -> float | None:
            return None if pos is None else pos[1] * pps - pos[3]

        t.settled = {
            "settled_at": market.settled_at or market.close_time, "payout_per_share": pps,
            "result": result_label(pps) if filled else None,
            "payout": t.contracts * pps if filled else None,
            "pnl": (t.contracts * pps - t.cost) if filled else 0.0,
            "zero_pnl": pnl(t.zero), "atask_pnl": pnl(t.atask),
        }
    return t


# -- who is copied in each period -----------------------------------------------------------------


@dataclass
class PeriodPlan:
    strategy: str
    fold: Fold
    requirements: dict[str, Any]
    passed: int
    wallets: list[str]
    note: str


def top_wallets(wallets: list[FoldWallet], req: dict[str, Any], sc: ScoringConfig) -> tuple[int, list[str]]:
    """Wallets passing ``req``, ranked by ``rank_by`` and cut to ``top_k`` as scoring.rank_wallets does."""
    passed = [w for w in wallets if passes_all(w.m, req)]
    key = sc.rank_by
    ranked = []
    for w in passed:
        if key == "clv" and w.m.clv_n < sc.min_clv_positions:
            continue
        if key == "clv_markout" and w.m.clv_markout_n < sc.min_clv_positions:
            continue
        if w.m.rank_value(key) is None:
            continue
        ranked.append(w)
    ranked.sort(key=lambda w: (w.m.rank_value(key), w.m.roi or 0.0, w.wallet), reverse=True)
    return len(passed), [w.wallet for w in ranked[:sc.top_k]]


def plan_periods(folds: list[Fold], sc: ScoringConfig, *, min_wallets: int, min_alerts: int,
                 objective: str) -> list[PeriodPlan]:
    """Which wallets each rule copies in each period, choosing with earlier periods only."""
    folds = sorted(folds, key=lambda f: f.cutoff)
    indexes = [FoldIndex(f) for f in folds]
    plans: list[PeriodPlan] = []
    for k, fold in enumerate(folds):
        if k:
            found = search(indexes[:k], min_wallets=min_wallets, min_alerts=min_alerts, objective=objective)
            trained_on = ", ".join(iso(f.cutoff)[:10] for f in folds[:k])
            if found:
                req = choose(found, objective).req
                passed, chosen = top_wallets(fold.wallets, req, sc)
                plans.append(PeriodPlan(f"{STRATEGY_PREFIX}walk-forward", fold, req, passed, chosen,
                                        f"requirements chosen on the period(s) starting {trained_on}"))
            else:
                log.warning("walk-forward: nothing chosen for %s (earlier folds too thin)", iso(fold.cutoff)[:10])
        passed, chosen = top_wallets(fold.wallets, SPEC_REQUIREMENTS, sc)
        plans.append(PeriodPlan(f"{STRATEGY_PREFIX}spec", fold, dict(SPEC_REQUIREMENTS), passed, chosen,
                                "the spec's original thresholds, fixed in advance"))
    return plans


# -- the run ------------------------------------------------------------------------------------


@dataclass
class BacktestReport:
    plans: list[PeriodPlan]
    trades: list[SimTrade] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    delay: int = 90
    ask_premium: float = 0.01
    watch: Stopwatch | None = None
    comparison: list[str] = field(default_factory=list)


async def run_backtest(app: App, folds: list[Fold], *, delay: int = 90, ask_premium: float = 0.01,
                       min_wallets: int = 10, min_alerts: int = 50, objective: str = "pooled", write: bool = True,
                       kalshi: bool = False, progress: Callable[[str], None] | None = None,
                       watch: Stopwatch | None = None) -> BacktestReport:
    say = progress or (lambda _m: None)
    al, pp = app.cfg.alerts, app.cfg.paper
    watch = watch or Stopwatch(app.http)
    plans = plan_periods(folds, app.cfg.scoring, min_wallets=min_wallets, min_alerts=min_alerts, objective=objective)
    for p in plans:
        say(f"{p.strategy} from {iso(p.fold.cutoff)[:10]}: copying {len(p.wallets)} of {p.passed} passing wallets")

    periods = {(w, p.fold.cutoff, p.fold.end) for p in plans for w in p.wallets}
    rows: dict[tuple[str, int], list[Activity]] = {}
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "wallet activity", len(periods))

    async def load(w: str, start: int, end: int) -> None:
        # No activity cap here: the live monitor alerts on every fill of a wallet it copies.
        async with sem:
            try:
                got, _ = await load_wallet_rows(app, w, start, end, max_rows=10**9)
            except Exception as exc:
                log.warning("activity for %s failed: %s", w, exc)
                got = []
        rows[(w, start)] = got
        ticker.tick()

    with watch.stage("wallet activity"):
        await asyncio.gather(*(load(w, s, e) for w, s, e in periods))
    with watch.stage("markets"):
        markets = await app.markets.ensure({r.condition_id for rs in rows.values() for r in rs
                                            if r.type == "TRADE" and r.side == "BUY"})

    events: dict[tuple[str, int], list[AlertEvent]] = {
        (w, s): alert_events(w, rows[(w, s)], markets, s, e, window=al.dedupe_window_seconds,
                             min_usd=al.min_whale_trade_usd)
        for w, s, e in periods}

    report = BacktestReport(plans, delay=delay, ask_premium=ask_premium, watch=watch)
    jobs: list[tuple[PeriodPlan, AlertEvent]] = []
    for plan in plans:
        for w in plan.wallets:
            for e in events[(w, plan.fold.cutoff)]:
                m = markets[e.condition_id]
                ask = e.whale_price + ask_premium
                if closed_before(m, e.trigger_ts + DETECTION_LAG):
                    report.skipped[(plan.strategy, "market closed")] += 1
                elif not al.min_price <= ask <= al.max_price:
                    report.skipped[(plan.strategy, f"price outside {al.min_price:g}-{al.max_price:g}")] += 1
                else:
                    jobs.append((plan, e))
    lookups = sorted({(e.token_id, e.trigger_ts + delay) for _, e in jobs
                      if not closed_before(markets[e.condition_id], e.trigger_ts + delay)})
    say(f"{len(jobs)} paper trades to simulate; {len(lookups)} fill prices to look up")
    prices: dict[tuple[str, int], tuple[int, float] | None] = {}
    ticker = Progress(say, "fill prices", len(lookups))

    async def lookup(token: str, at: int) -> None:
        async with sem:
            try:
                prices[(token, at)] = await price_after(app, token, at)
            except Exception as exc:
                log.warning("price lookup failed for %s at %s: %s", token, at, exc)
                prices[(token, at)] = None
        ticker.tick()

    with watch.stage("fill prices (CLOB minute history)"):
        await asyncio.gather(*(lookup(t, a) for t, a in lookups))

    for plan, e in jobs:
        report.trades.append(simulate(e, markets[e.condition_id], prices.get((e.token_id, e.trigger_ts + delay)),
                                      strategy=plan.strategy, stake=pp.stake_usd, delay=delay,
                                      ask_premium=ask_premium, max_slippage=pp.max_slippage))
    if kalshi:
        await _kalshi_copies(app, report, jobs, markets, delay=delay, min_close_ts=min(f.cutoff for f in folds) - 7 * 86_400,
                             say=say, watch=watch)
        report.comparison = same_alert_comparison(report.trades, markets, samples=app.cfg.stats.bootstrap_samples)
    if write:
        with watch.stage("write paper trades"):
            write_trades(app, report.trades, run={
                "delay": delay, "ask_premium": ask_premium, "objective": objective, "kalshi": kalshi,
                "periods": [{"strategy": p.strategy, "start": iso(p.fold.cutoff), "end": iso(p.fold.end),
                             "wallets": len(p.wallets), "passed": p.passed,
                             "requirements": {k: v for k, v in p.requirements.items() if v != LOOSEST[k]}}
                            for p in plans]})
    return report


async def _kalshi_copies(app: App, report: BacktestReport, jobs: list[tuple[PeriodPlan, AlertEvent]],
                         markets: dict[str, MarketInfo], *, delay: int, min_close_ts: int,
                         say: Callable[[str], None], watch: Stopwatch) -> None:
    """The Kalshi twin of every Polymarket copy whose game Kalshi also lists."""
    al, pp = app.cfg.alerts, app.cfg.paper
    store = KalshiStore(app)
    games = {cid: poly_game(markets[cid]) for cid in {e.condition_id for _, e in jobs}}
    with watch.stage("kalshi markets"):
        index = await store.index({g.league for g in games.values() if g}, min_close_ts=min_close_ts)
    legs = {cid: match(markets[cid], index) for cid, g in games.items() if g}
    todo = []
    for plan, e in jobs:
        label = f"{plan.strategy} on kalshi"
        leg = (legs.get(e.condition_id) or {}).get(e.outcome_index) if e.outcome_index is not None else None
        if leg is None:
            why = "no matching Kalshi game" if games.get(e.condition_id) else "not a game-winner market"
            report.skipped[(label, why)] += 1
            continue
        km = index.by_ticker[leg.ticker]
        if not km.open_at(e.trigger_ts + DETECTION_LAG):
            report.skipped[(label, "Kalshi market not open at alert time")] += 1
            continue
        todo.append((plan, e, km, leg.side))
    need: dict[str, set[int]] = defaultdict(set)
    for _, e, km, _ in todo:
        need[km.ticker].update((e.trigger_ts, e.trigger_ts + DETECTION_LAG, e.trigger_ts + delay))
    quotes: dict[str, list[Quote]] = {}
    ticker = Progress(say, "Kalshi quotes", len(need))
    sem = asyncio.Semaphore(4)

    async def load(tk: str) -> None:
        async with sem:
            try:
                quotes[tk] = await store.quotes(index.by_ticker[tk], need[tk])
            except Exception as exc:
                log.warning("Kalshi quotes for %s failed: %s", tk, exc)
                quotes[tk] = []
        ticker.tick()

    with watch.stage("kalshi quotes (minute candles)"):
        await asyncio.gather(*(load(tk) for tk in need))
    for plan, e, km, side in todo:
        label = f"{plan.strategy} on kalshi"
        qa = quote_at(quotes.get(km.ticker) or [], e.trigger_ts + DETECTION_LAG)
        ask = qa.ask(side) if qa else None
        if ask is None:
            report.skipped[(label, "no Kalshi offer at alert time")] += 1
        elif not al.min_price <= ask <= al.max_price:
            report.skipped[(label, f"price outside {al.min_price:g}-{al.max_price:g}")] += 1
        else:
            report.trades.append(simulate_kalshi(e, km, side, quotes[km.ticker], strategy=plan.strategy,
                                                 stake=pp.stake_usd, delay=delay, max_slippage=pp.max_slippage,
                                                 fee_multiplier=store.fee_multiplier(km.series)))


def same_alert_comparison(trades: list[SimTrade], markets: dict[str, MarketInfo], *, samples: int) -> list[str]:
    """Each venue's mean ROI per settled copy, on exactly the alerts copied on both."""
    def key(t: SimTrade) -> tuple:
        e = t.event
        return t.strategy, e.wallet, e.token_id, e.first_fill_ts

    poly = {key(t): t for t in trades if t.venue == VENUE}
    lines = []
    for strategy in sorted({t.strategy for t in trades if t.venue == "kalshi"}):
        pairs = [(poly[key(t)], t) for t in trades if t.venue == "kalshi" and t.strategy == strategy and key(t) in poly]
        lines.append(f"  {strategy}: {len(pairs)} alerts copied on both venues")
        for name, pick in (("Polymarket", 0), ("Kalshi", 1)):
            st = [p[pick] for p in pairs if p[pick].status == "FILLED" and p[pick].settled]
            if not st:
                lines.append(f"    {name:10s} no settled copies")
                continue
            ev = [markets[t.event.condition_id].event_slug or t.event.condition_id for t in st]
            m, lo, hi = two_way_bootstrap_mean_ci([t.settled["pnl"] / t.cost for t in st], [t.event.wallet for t in st],
                                                  ev, samples=samples)
            ci = "no CI" if lo is None else f"95% CI [{lo:+.1%}, {hi:+.1%}]"
            lines.append(f"    {name:10s} mean ROI per settled copy {m:+.1%}, {ci}, n={len(st)} "
                         f"({len({t.event.wallet for t in st})} wallets, {len(set(ev))} events)")
    return lines


REPLAY_STRATEGIES = (f"{STRATEGY_PREFIX}walk-forward", f"{STRATEGY_PREFIX}spec")


def write_trades(app: App, trades: list[SimTrade], *, strategies: tuple[str, ...] = REPLAY_STRATEGIES,
                 run: dict[str, Any] | None = None, state_key: str = BACKTEST_STATE_KEY) -> None:
    """Replace the previous rows of ``strategies`` (and only those) with these, in one transaction."""
    now = app.clock()
    with app.db.tx() as conn:
        conn.executemany("DELETE FROM paper_trades WHERE strategy = ?", [(s,) for s in strategies])
        orphans = "SELECT id FROM alerts WHERE status = 'BACKTEST' AND id NOT IN (SELECT alert_id FROM paper_trades)"
        conn.execute(f"DELETE FROM alert_fills WHERE alert_id IN ({orphans})")
        conn.execute(f"DELETE FROM alerts WHERE id IN ({orphans})")
        conn.execute("INSERT OR REPLACE INTO service_state (key, value) VALUES (?, ?)",
                     (state_key, json.dumps({**(run or {}), "written_at": iso(now), "trades": len(trades)})))
        alert_ids: dict[tuple[str, str, int], int] = {}
        for t in trades:
            e = t.event
            key = (e.wallet, e.token_id, e.first_fill_ts)
            if key not in alert_ids:
                detected = e.trigger_ts + DETECTION_LAG
                cur = conn.execute(
                    "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
                    "trigger_fill_ts, whale_shares, whale_usd, whale_price, fills, detected_at, posted_at, status, "
                    "alert_best_ask, message, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e.wallet, e.condition_id, e.token_id, e.outcome, "NEW", e.first_fill_ts, e.last_fill_ts,
                     e.trigger_ts, e.whale_shares, e.whale_usd, e.whale_price, e.fills, detected, None,
                     "BACKTEST", t.atask[0] if t.atask else None, "simulated by `backtest`", now))
                alert_ids[key] = int(cur.lastrowid)
            s = t.settled or {}
            zero, atask = t.zero or (None,) * 4, t.atask or (None,) * 4
            conn.execute(
                "INSERT INTO paper_trades (alert_id, venue, strategy, status, miss_reason, token_id, condition_id, "
                "stake_usd, due_poll, alert_ts, alert_ask, fill_ts, fill_price, contracts, fees, cost, levels_walked, "
                "zero_price, zero_contracts, zero_fees, zero_cost, atask_price, atask_contracts, atask_fees, "
                "atask_cost, settled_at, settle_result, payout_per_share, payout, pnl, zero_pnl, atask_pnl, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert_ids[key], t.venue, t.strategy, t.status, t.miss_reason,
                 e.token_id if t.venue == VENUE else f"kalshi:{t.contract}", e.condition_id,
                 app.cfg.paper.stake_usd, 0, e.trigger_ts + DETECTION_LAG, atask[0], t.fill_ts, t.fill_price,
                 t.contracts, t.fees, t.cost, None, *zero, *atask, s.get("settled_at"), s.get("result"),
                 s.get("payout_per_share"), s.get("payout"), s.get("pnl"), s.get("zero_pnl"), s.get("atask_pnl"),
                 now))


def describe(report: BacktestReport) -> str:
    lines = [f"Backtest: copy fills {report.delay}s after the triggering fill at the CLOB minute price "
             f"+ {report.ask_premium:g}, plus fees. Wallets picked at each period's start with earlier data only.",
             ""]
    for p in report.plans:
        req = ", ".join(f"{k}={'off' if v is None else f'{v:g}'}" for k, v in p.requirements.items()
                        if v != LOOSEST[k]) or "the loosest grid values"
        lines.append(f"  {p.strategy:22s} {iso(p.fold.cutoff)[:10]} to {iso(p.fold.end)[:10]}: "
                     f"{len(p.wallets):3d} wallets copied ({p.passed} passed)  [{req}]")
        lines.append(f"  {'':22s} {p.note}")
    by: dict[str, Counter] = defaultdict(Counter)
    for t in report.trades:
        label = t.strategy if t.venue == VENUE else f"{t.strategy} on {t.venue}"
        by[label]["paper trades"] += 1
        by[label][t.status.lower()] += 1
        if t.status == "MISSED":
            by[label]["missed: " + (t.miss_reason or "").split(":")[0]] += 1
        elif t.settled:
            by[label]["settled"] += 1
    for (strategy, reason), n in report.skipped.items():
        by[strategy][f"no trade: {reason}"] += n
    lines.append("")
    for strategy, c in sorted(by.items()):
        lines.append(f"  {strategy}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    if report.comparison:
        lines.append("")
        lines.append("Same alerts, both venues (the copies Kalshi could take, and their Polymarket twins):")
        lines += report.comparison
    lines.append("")
    lines.append("The results are in `python -m whalewatcher stats`, labelled SIMULATED, apart from live trades.")
    return "\n".join(lines)
