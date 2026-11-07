"""Phase 1: score wallets using only data up to ``as_of``, filter, rank, snapshot.

Leakage rules, enforced here rather than trusted to callers:

* Activity rows after ``as_of`` are ignored (``build_ledgers``).
* A market counts as settled only if it resolved at or before ``as_of``. Its
  payout is known today, but it is not used for an ``as_of`` before it.
* CLV reads prices at or before the market's close reference, which is at or
  before its resolution, so at or before ``as_of``; markouts that would land
  after ``as_of`` are dropped.
* Leaderboards describe today. For a past ``as_of`` the candidate pool comes
  from large trades placed before ``as_of`` instead (``historical`` pool),
  because choosing candidates from today's leaderboard is itself selection on
  the period being evaluated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field

from .app import App, PriceLookup
from .config import ScoringConfig
from .ledger import MarketLedger, build_ledgers, entry_vwap
from .polymarket import Activity, MarketInfo, assign_fill_keys
from .timeutil import add_months, iso, month_key, months_in_window
from .timing import Progress, Stopwatch

log = logging.getLogger(__name__)

DAY = 86_400
ACTIVITY_TYPES = ("TRADE", "SPLIT", "MERGE", "REDEEM", "CONVERSION")


@dataclass
class WalletMetrics:
    wallet: str
    as_of: int
    settled_markets: int = 0
    total_pnl: float = 0.0
    total_cost: float = 0.0
    roi: float | None = None
    profitable_months: int = 0
    months_in_window: int = 0
    max_single_market_share: float | None = None
    avg_stake_usd: float | None = None
    days_since_last_trade: float | None = None
    two_sided_ratio: float | None = None
    clv: float | None = None
    clv_n: int = 0
    clv_markout: float | None = None
    clv_markout_n: int = 0
    win_rate: float | None = None
    unreliable_markets: int = 0
    activity_rows: int = 0
    truncated: bool = False
    passed: bool = False
    fail_reasons: list[str] = field(default_factory=list)
    rank: int | None = None
    selected: bool = False

    def rank_value(self, key: str) -> float | None:
        return getattr(self, key)


@dataclass(slots=True)
class SettledMarket:
    condition_id: str
    pnl: float
    cost: float
    resolved_at: int
    first_acq_ts: int | None


@dataclass(slots=True)
class ClvPosition:
    condition_id: str
    token_id: str
    entry_price: float
    first_buy_ts: int
    first_buy_price: float
    close_ref_ts: int


@dataclass
class WalletAnalysis:
    metrics: WalletMetrics
    settled: list[SettledMarket]
    clv_positions: list[ClvPosition]
    incomplete: list[str]   # settled-in-window markets whose history looks cut off


def _token_index(markets: dict[str, MarketInfo]) -> Callable[[str, str], int | None]:
    def lookup(condition_id: str, token_id: str) -> int | None:
        m = markets.get(condition_id)
        return m.outcome_index_of(token_id) if m is not None else None
    return lookup


def _outcome_count(markets: dict[str, MarketInfo]) -> Callable[[str], int]:
    def count(condition_id: str) -> int:
        m = markets.get(condition_id)
        return len(m.outcomes) if m is not None and m.outcomes else 2
    return count


def analyse(
    wallet: str,
    rows: Iterable[Activity],
    markets: dict[str, MarketInfo],
    as_of: int,
    cfg: ScoringConfig,
    *,
    window_start: int | None = None,
    entry_after: int | None = None,
) -> WalletAnalysis:
    """All the cheap metrics (everything but CLV), from rows and market data.

    ``window_start``..``as_of`` bounds resolution time. ``entry_after`` keeps
    only positions opened at or after it (the holdout uses this, so a
    position opened during selection cannot count toward the holdout).
    """
    rows = list(rows)
    if window_start is None:
        window_start = add_months(as_of, -cfg.lookback_months)
    months = months_in_window(window_start, as_of)
    ledgers = build_ledgers(rows, as_of, token_index=_token_index(markets),
                            outcome_count=_outcome_count(markets))
    m = WalletMetrics(wallet=wallet, as_of=as_of, months_in_window=len(months),
                      activity_rows=sum(1 for r in rows if r.ts <= as_of))

    settled: list[SettledMarket] = []
    clv_positions: list[ClvPosition] = []
    incomplete: list[str] = []
    for cid, led in ledgers.items():
        market = markets.get(cid)
        if market is None or not market.resolved_by(as_of) or market.payouts is None:
            continue
        if not window_start <= market.resolved_at < as_of:  # type: ignore[operator]
            continue
        if entry_after is not None and (led.first_acq_ts is None or led.first_acq_ts < entry_after):
            continue
        if not led.reliable:
            m.unreliable_markets += 1
            if led.incomplete and not led.has_conversion:
                incomplete.append(cid)
            continue
        settled.append(SettledMarket(cid, led.pnl(market.payouts), led.cost, market.resolved_at,
                                     led.first_acq_ts))
        clv_positions.extend(_clv_positions(led, market, as_of))

    m.settled_markets = len(settled)
    m.total_pnl = sum(s.pnl for s in settled)
    m.total_cost = sum(s.cost for s in settled)
    if m.total_cost > 0:
        m.roi = m.total_pnl / m.total_cost
        m.avg_stake_usd = m.total_cost / len(settled)
    if settled:
        m.win_rate = sum(1 for s in settled if s.pnl > 0) / len(settled)
        by_month: dict[str, float] = defaultdict(float)
        for s in settled:
            by_month[month_key(s.resolved_at)] += s.pnl
        m.profitable_months = sum(1 for key in months if by_month.get(key, 0.0) > 0)
        if m.total_pnl > 0:
            m.max_single_market_share = max(s.pnl for s in settled) / m.total_pnl

    trade_ts = [r.ts for r in rows if r.type == "TRADE" and r.ts <= as_of]
    if trade_ts:
        m.days_since_last_trade = (as_of - max(trade_ts)) / DAY

    active = [led for led in ledgers.values()
              if led.last_acq_ts is not None and led.last_acq_ts >= window_start]
    if active:
        m.two_sided_ratio = sum(1 for led in active if led.two_sided) / len(active)
    return WalletAnalysis(m, settled, clv_positions, incomplete)


def _clv_positions(led: MarketLedger, market: MarketInfo, as_of: int) -> list[ClvPosition]:
    close_ref = market.close_reference_ts()
    if close_ref is None or close_ref > as_of:
        return []
    out = []
    for index in sorted(led.outcomes_bought):
        buys = [b for b in led.buys if b.outcome_index == index]
        entry = entry_vwap(buys, before=close_ref)
        if entry is None:
            continue
        pre_close = sorted((b for b in buys if b.ts < close_ref), key=lambda b: b.ts)
        token = market.token_ids[index] if index < len(market.token_ids) else pre_close[0].token_id
        out.append(ClvPosition(market.condition_id, token, entry[0], pre_close[0].ts,
                               pre_close[0].price, close_ref))
    return out


def sample_positions(positions: list[ClvPosition], limit: int, seed: str) -> list[ClvPosition]:
    """At most ``limit`` positions, the same ones every run for the same wallet (0 = all)."""
    if limit <= 0 or len(positions) <= limit:
        return positions
    ordered = sorted(positions, key=lambda p: (p.condition_id, p.token_id))
    rng = random.Random(int(hashlib.sha1(seed.encode()).hexdigest()[:12], 16))
    return rng.sample(ordered, limit)


async def compute_clv(positions: list[ClvPosition], prices: PriceLookup, as_of: int,
                      markout_hours: float, *, sample: int = 0, seed: str = "",
                      want_clv: bool = True, want_markout: bool = True,
                      ) -> tuple[float | None, int, float | None, int]:
    """(clv, n, markout, n) averaged equally across positions.

    clv: closing price (last price before the close reference) minus entry VWAP.
    markout: price ``markout_hours`` after the first buy minus that buy's price,
    only where that instant is before both the close reference and ``as_of``.
    Each costs one price lookup per position, so ``sample`` caps the positions
    used (a seeded sample) and ``want_*`` skips a metric nobody will read.
    """
    horizon = int(markout_hours * 3600)
    positions = sample_positions(positions, sample, seed)

    async def one(pos: ClvPosition) -> tuple[float | None, float | None]:
        clv = markout = None
        if want_clv:
            close = await prices.at(pos.token_id, pos.close_ref_ts - 1)
            clv = close.price - pos.entry_price if close is not None else None
        target = pos.first_buy_ts + horizon
        if want_markout and target < pos.close_ref_ts and target <= as_of:
            point = await prices.at(pos.token_id, target)
            if point is not None:
                markout = point.price - pos.first_buy_price
        return clv, markout

    results = await asyncio.gather(*(one(p) for p in positions))
    clvs = [c for c, _ in results if c is not None]
    marks = [mk for _, mk in results if mk is not None]
    return (sum(clvs) / len(clvs) if clvs else None, len(clvs),
            sum(marks) / len(marks) if marks else None, len(marks))


def apply_filters(m: WalletMetrics, cfg: ScoringConfig) -> list[str]:
    """Reasons the wallet fails, empty if it passes. CLV is checked when ranking."""
    if m.truncated:
        return [f"too_active(>{cfg.max_activity_rows} rows: bot or market maker)"]
    reasons = []
    if m.settled_markets < cfg.min_settled_markets:
        reasons.append(f"settled_markets {m.settled_markets} < {cfg.min_settled_markets}")
    if m.profitable_months < cfg.min_profitable_months:
        reasons.append(f"profitable_months {m.profitable_months} < {cfg.min_profitable_months}")
    if m.max_single_market_share is None or m.max_single_market_share > cfg.max_single_market_share:
        shown = "n/a" if m.max_single_market_share is None else f"{m.max_single_market_share:.2f}"
        reasons.append(f"max_single_market_share {shown} > {cfg.max_single_market_share}")
    if m.avg_stake_usd is None or m.avg_stake_usd < cfg.min_avg_stake_usd:
        reasons.append(f"avg_stake_usd < {cfg.min_avg_stake_usd:g}")
    if m.days_since_last_trade is None or m.days_since_last_trade > cfg.max_days_since_last_trade:
        reasons.append(f"days_since_last_trade > {cfg.max_days_since_last_trade:g}")
    if m.two_sided_ratio is not None and m.two_sided_ratio > cfg.max_two_sided_ratio:
        reasons.append(f"two_sided_ratio {m.two_sided_ratio:.2f} > {cfg.max_two_sided_ratio}")
    if m.roi is None or m.roi < cfg.min_roi:
        reasons.append(f"roi < {cfg.min_roi:g}")
    measured = m.settled_markets + m.unreliable_markets
    if measured and m.unreliable_markets / measured > cfg.max_unreliable_market_share:
        reasons.append(f"unreliable_markets {m.unreliable_markets}/{measured} (conversions/transfers)")
    return reasons


def rank_wallets(metrics: list[WalletMetrics], cfg: ScoringConfig) -> list[WalletMetrics]:
    """Rank the wallets that passed; marks the top_k as selected. Returns all, ordered."""
    key = cfg.rank_by
    eligible = []
    for m in metrics:
        if not m.passed:
            continue
        if key == "clv" and m.clv_n < cfg.min_clv_positions:
            m.passed = False
            m.fail_reasons.append(f"clv_positions {m.clv_n} < {cfg.min_clv_positions}")
            continue
        if key == "clv_markout" and m.clv_markout_n < cfg.min_clv_positions:
            m.passed = False
            m.fail_reasons.append(f"markout_positions {m.clv_markout_n} < {cfg.min_clv_positions}")
            continue
        if m.rank_value(key) is None:
            m.passed = False
            m.fail_reasons.append(f"no {key}")
            continue
        eligible.append(m)
    eligible.sort(key=lambda m: (m.rank_value(key), m.roi or 0.0), reverse=True)
    for i, m in enumerate(eligible, start=1):
        m.rank = i
        m.selected = i <= cfg.top_k
    rest = [m for m in metrics if m.rank is None]
    rest.sort(key=lambda m: (m.total_pnl or 0.0), reverse=True)
    return eligible + rest


# -- fetching -----------------------------------------------------------------------


def _store_rows(app: App, rows: list[Activity]) -> None:
    now = app.clock()
    with app.db.tx() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO wallet_trades (wallet, ts, type, condition_id, token_id, outcome, "
            "outcome_index, side, size, usdc_size, price, tx_hash, fill_key, seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r.wallet, r.ts, r.type, r.condition_id, r.token_id, r.outcome, r.outcome_index, r.side,
              r.size, r.usdc_size, r.price, r.tx_hash, r.fill_key, now)
             for r in rows],
        )


def rows_from_db(app: App, wallet: str, end: int) -> list[Activity]:
    out = []
    for r in app.db.query("SELECT * FROM wallet_trades WHERE wallet = ? AND ts <= ? ORDER BY ts, id",
                          (wallet, end)):
        out.append(Activity(
            wallet=r["wallet"], ts=r["ts"], type=r["type"], condition_id=r["condition_id"],
            token_id=r["token_id"] or "", outcome=r["outcome"] or "", outcome_index=r["outcome_index"],
            side=r["side"] or "", size=r["size"] or 0.0, usdc_size=r["usdc_size"] or 0.0,
            price=r["price"] or 0.0, tx_hash=r["tx_hash"] or "", fill_key=r["fill_key"]))
    return out


async def load_wallet_rows(app: App, wallet: str, start: int, end: int, *,
                           max_rows: int) -> tuple[list[Activity], bool]:
    """Activity rows for [start, end] (plus any older rows already stored), and whether
    the wallet was too active to fetch.

    Only the stretches the database lacks are fetched, before and/or after what
    it already holds. "Too active" is a rate, ``max_rows`` over the length of the
    requested window, judged on each stretch: a wider window scales the cap
    instead of tripping it, and a hyperactive wallet is still cut off after one page.
    """
    cov = app.db.one("SELECT start_ts, end_ts, truncated FROM wallet_fetches WHERE wallet = ?", (wallet,))
    if cov is not None and cov["truncated"] and cov["start_ts"] <= start and cov["end_ts"] >= end:
        return [], True
    if cov is None or cov["truncated"]:
        gaps = [(start, end)]
        new_start, new_end = start, end
    else:
        gaps = []
        if start < cov["start_ts"]:
            gaps.append((start, cov["start_ts"] + 3600))
        if end > cov["end_ts"]:
            gaps.append((cov["end_ts"] - 3600, end))
        if not gaps:
            return rows_from_db(app, wallet, end), False
        new_start, new_end = min(start, cov["start_ts"]), max(end, cov["end_ts"])
    rate = max_rows / max(1, end - start)   # rows per second the cap allows

    fetched: list[Activity] = []
    for gap_start, gap_end in gaps:
        gap_rows = max(1000, int(rate * (gap_end - gap_start)))

        def too_busy(first_page: list[dict], gap_start: int = gap_start, gap_end: int = gap_end,
                     gap_rows: int = gap_rows) -> bool:
            # Rows are newest first. If one page covers only a sliver of the stretch,
            # the stretch holds far more rows than any person trading by hand.
            if not first_page:
                return False
            oldest = min(int(r.get("timestamp") or gap_end) for r in first_page)
            span = max(1, gap_end - oldest)
            return len(first_page) * (gap_end - gap_start) / span > gap_rows

        rows, truncated = await app.data.activity(wallet, types=ACTIVITY_TYPES, start=gap_start, end=gap_end,
                                                  max_rows=gap_rows, stop_after_first=too_busy)
        if truncated:  # a capped wallet is never scored, so its rows are not worth the disk
            _record_fetch(app, wallet, start, end, 0, truncated=True)
            return [], True
        fetched.extend(r for r in rows if r.type in ACTIVITY_TYPES)
    assign_fill_keys(fetched)
    _store_rows(app, fetched)
    _record_fetch(app, wallet, new_start, new_end, len(fetched), truncated=False)
    return rows_from_db(app, wallet, end), False


def _record_fetch(app: App, wallet: str, start: int, end: int, rows: int, *, truncated: bool) -> None:
    app.db.execute("DELETE FROM wallet_fetches WHERE wallet = ?", (wallet,))
    app.db.execute(
        "INSERT INTO wallet_fetches (wallet, start_ts, end_ts, rows, truncated, fetched_at) VALUES (?,?,?,?,?,?)",
        (wallet, start, end, rows, int(truncated), app.clock()))


async def complete_history(app: App, wallet: str, condition_ids: list[str], end: int,
                           *, limit: int = 200) -> None:
    """Fetch the full history of specific markets (positions opened before the window)."""
    for i in range(0, min(len(condition_ids), limit), 20):
        group = condition_ids[i:i + 20]
        rows, _ = await app.data.activity(wallet, types=ACTIVITY_TYPES, start=1, end=end,
                                          conditions=group, max_rows=5000)
        rows = [r for r in rows if r.type in ACTIVITY_TYPES]
        assign_fill_keys(rows)
        _store_rows(app, rows)


# -- candidate pool ---------------------------------------------------------------------


async def candidate_pool(app: App, as_of: int, *, historical: bool) -> tuple[dict[str, set[str]], list[str]]:
    """Wallets worth scoring, each with where it came from, plus warnings."""
    cfg = app.cfg.candidates
    pool: dict[str, set[str]] = defaultdict(set)
    # How much each wallet moved in the trades that brought it in: the cap below
    # keeps the biggest traders, not whichever addresses sort first.
    weight: dict[str, float] = defaultdict(float)
    notes: list[str] = []
    if not historical:
        if as_of < app.clock() - 2 * DAY:
            notes.append("leaderboards describe today, not as_of: this pool leans on performance after "
                         "as_of. Use --historical-pool for a leak-free pool.")
        for period in cfg.leaderboard_periods:
            for category in cfg.leaderboard_categories:
                for e in await app.data.leaderboard(period, category=category, limit=cfg.leaderboard_limit):
                    pool[e.wallet].add(f"leaderboard:{period}:{category}")
                    weight[e.wallet] += abs(e.pnl)
        trades = await app.data.trades(min_usd=cfg.recent_trades_min_usd, max_pages=cfg.recent_trades_pages)
        for t in trades:
            if t.ts <= as_of and t.wallet:
                pool[t.wallet].add("recent_large_trades")
                weight[t.wallet] += t.usdc_size
    else:
        markets = await app.gamma.top_markets(end_min=_gamma_date(as_of - 90 * DAY),
                                              end_max=_gamma_date(as_of + 14 * DAY),
                                              limit=cfg.historical_markets)
        conds = [str(m.get("conditionId") or "").lower() for m in markets if m.get("conditionId")]
        for i in range(0, len(conds), 20):
            trades = await app.data.trades(conditions=conds[i:i + 20], min_usd=cfg.historical_min_trade_usd,
                                           max_pages=3)
            for t in trades:
                if t.ts < as_of and t.wallet:
                    pool[t.wallet].add("historical_large_trades")
                    weight[t.wallet] += t.usdc_size
    ranked = sorted(pool, key=lambda w: (-len(pool[w]), -weight[w], w))[: cfg.max_candidates]
    if len(pool) > len(ranked):
        notes.append(f"candidate pool capped at {len(ranked)} of {len(pool)} wallets")
    return {w: pool[w] for w in ranked}, notes


def _gamma_date(ts: int) -> str:
    return iso(ts).replace(" ", "T")


# -- the run ---------------------------------------------------------------------------------


@dataclass
class ScoreRun:
    run_id: int | None
    as_of: int
    metrics: list[WalletMetrics]
    notes: list[str]
    sources: dict[str, int]
    analyses: dict[str, WalletAnalysis] = field(default_factory=dict)
    watch: Stopwatch | None = None

    @property
    def selected(self) -> list[WalletMetrics]:
        return [m for m in self.metrics if m.selected]


async def run_scoring(
    app: App,
    as_of: int,
    *,
    kind: str = "score",
    pool: dict[str, set[str]] | None = None,
    historical_pool: bool | None = None,
    extra_notes: Iterable[str] = (),
    save: bool = True,
    progress: Callable[[str], None] | None = None,
    watch: Stopwatch | None = None,
    stage_prefix: str = "",
) -> ScoreRun:
    cfg = app.cfg.scoring
    say = progress or (lambda _msg: None)
    watch = watch or Stopwatch(app.http)
    notes = list(extra_notes)
    if pool is None:
        with watch.stage(f"{stage_prefix}candidate pool"):
            historical = historical_pool if historical_pool is not None else as_of < app.clock() - 2 * DAY
            pool, pool_notes = await candidate_pool(app, as_of, historical=historical)
        notes.extend(pool_notes)
    say(f"candidate pool: {len(pool)} wallets")
    _remember_wallets(app, pool)

    window_start = add_months(as_of, -cfg.lookback_months)
    fetch_start = window_start - cfg.entry_lookback_days * DAY
    sem = asyncio.Semaphore(8)
    ticker = Progress(say, "wallet activity", len(pool))

    async def fetch(wallet: str) -> tuple[str, list[Activity], bool]:
        async with sem:
            try:
                rows, truncated = await load_wallet_rows(app, wallet, fetch_start, as_of,
                                                         max_rows=cfg.max_activity_rows)
            except Exception as exc:  # one wallet failing must not sink the run
                log.warning("fetch failed for %s: %s", wallet, exc)
                rows, truncated = [], False
            ticker.tick()
            return wallet, rows, truncated

    with watch.stage(f"{stage_prefix}wallet activity"):
        fetched = await asyncio.gather(*(fetch(w) for w in pool))
    conditions = {r.condition_id for _, rows, _ in fetched for r in rows}
    say(f"loading {len(conditions)} markets")
    with watch.stage(f"{stage_prefix}markets"):
        markets = await app.markets.ensure(conditions)

    rows_by_wallet = {w: rows for w, rows, truncated in fetched if not truncated}
    analyses: dict[str, WalletAnalysis] = {w: analyse(w, rows, markets, as_of, cfg)
                                           for w, rows in rows_by_wallet.items()}
    # Positions opened before the fetched window show up as incomplete markets;
    # fetch those markets' full histories for every wallet at once, then redo them.
    incomplete = {w: a.incomplete for w, a in analyses.items() if a.incomplete}
    if incomplete:
        say(f"completing older history for {len(incomplete)} wallets")
        with watch.stage(f"{stage_prefix}older history"):
            async def complete(wallet: str, conds: list[str]) -> None:
                async with sem:
                    try:
                        await complete_history(app, wallet, conds, as_of)
                    except Exception as exc:
                        log.warning("history completion failed for %s: %s", wallet, exc)

            await asyncio.gather(*(complete(w, c) for w, c in incomplete.items()))
            for w in incomplete:
                rows_by_wallet[w] = rows_from_db(app, w, as_of)
            extra = {r.condition_id for w in incomplete for r in rows_by_wallet[w]} - set(markets)
            if extra:
                markets.update(await app.markets.ensure(extra))
            for w in incomplete:
                analyses[w] = analyse(w, rows_by_wallet[w], markets, as_of, cfg)

    metrics: list[WalletMetrics] = []
    for wallet, _rows, truncated in fetched:
        if truncated:
            m = WalletMetrics(wallet=wallet, as_of=as_of, truncated=True)
            m.fail_reasons = apply_filters(m, cfg)
            metrics.append(m)
            continue
        a = analyses[wallet]
        a.metrics.fail_reasons = apply_filters(a.metrics, cfg)
        a.metrics.passed = not a.metrics.fail_reasons
        metrics.append(a.metrics)

    survivors = [m for m in metrics if m.passed]
    say(f"{len(survivors)} wallets pass the filters; computing CLV")
    clv_ticker = Progress(say, "CLV", len(survivors))

    async def clv(m: WalletMetrics) -> None:
        async with sem:
            m.clv, m.clv_n, m.clv_markout, m.clv_markout_n = await compute_clv(
                analyses[m.wallet].clv_positions, app.prices, as_of, cfg.markout_hours,
                sample=cfg.clv_sample_positions, seed=m.wallet)
        clv_ticker.tick()

    with watch.stage(f"{stage_prefix}CLV ({len(survivors)} wallets)"):
        await asyncio.gather(*(clv(m) for m in survivors))
    ordered = rank_wallets(metrics, cfg)
    sources: dict[str, int] = defaultdict(int)
    for tags in pool.values():
        for tag in tags:
            sources[tag] += 1
    run = ScoreRun(None, as_of, ordered, notes, dict(sources), analyses, watch)
    if save:
        run.run_id = save_run(app, run, kind)
    return run


def _remember_wallets(app: App, pool: dict[str, set[str]]) -> None:
    now = app.clock()
    for wallet, tags in pool.items():
        row = app.db.one("SELECT sources FROM wallets WHERE address = ?", (wallet,))
        merged = sorted(set(json.loads(row["sources"] or "[]")) | tags) if row else sorted(tags)
        app.db.execute(
            "INSERT INTO wallets (address, sources, first_seen, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET sources = excluded.sources, updated_at = excluded.updated_at",
            (wallet, json.dumps(merged), now, now))


SNAPSHOT_FIELDS = (
    "settled_markets", "total_pnl", "total_cost", "roi", "profitable_months", "months_in_window",
    "max_single_market_share", "avg_stake_usd", "days_since_last_trade", "two_sided_ratio", "clv",
    "clv_n", "clv_markout", "clv_markout_n", "win_rate", "unreliable_markets", "activity_rows",
)


def save_run(app: App, run: ScoreRun, kind: str) -> int:
    cfg = app.cfg
    with app.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO score_runs (as_of, kind, created_at, config_json, sources_json, n_candidates, "
            "n_passed, notes) VALUES (?,?,?,?,?,?,?,?)",
            (run.as_of, kind, app.clock(), json.dumps(asdict(cfg.scoring)), json.dumps(run.sources),
             len(run.metrics), sum(1 for m in run.metrics if m.passed), json.dumps(run.notes)))
        run_id = int(cur.lastrowid)
        conn.executemany(
            f"INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, {', '.join(SNAPSHOT_FIELDS)}, "
            "passed, fail_reasons, rank, selected) VALUES "
            f"({', '.join('?' * (3 + len(SNAPSHOT_FIELDS) + 4))})",
            [(run_id, m.wallet, run.as_of, *(getattr(m, f) for f in SNAPSHOT_FIELDS), int(m.passed),
              json.dumps(m.fail_reasons), m.rank, int(m.selected)) for m in run.metrics],
        )
    return run_id


def load_run(app: App, run_id: int) -> list[WalletMetrics]:
    out = []
    for r in app.db.query("SELECT * FROM wallet_score_snapshots WHERE run_id = ? "
                          "ORDER BY rank IS NULL, rank, total_pnl DESC", (run_id,)):
        m = WalletMetrics(wallet=r["wallet"], as_of=r["as_of"])
        for f in SNAPSHOT_FIELDS:
            setattr(m, f, r[f])
        m.passed, m.rank, m.selected = bool(r["passed"]), r["rank"], bool(r["selected"])
        m.fail_reasons = json.loads(r["fail_reasons"] or "[]")
        out.append(m)
    return out
