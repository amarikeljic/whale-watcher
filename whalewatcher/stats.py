"""Phase 5: would copying these alerts make money after realistic delay and fees?

Rules this module keeps:
* Below ``min_settled_for_conclusions`` settled trades, the output LEADS with a
  warning that there is not enough data (about the live trades), and every
  strategy below it carries its own warning.
* No profit figure is shown without its sample size and a 95% bootstrap CI.
  When the CI includes zero, the output says so in words. The CI resamples
  wallets and events together (stats_util.two_way_bootstrap_mean_ci): one
  wallet's repeated buys, or several wallets on one event, are not
  independent results and must not narrow it.
* Headline numbers use the real, delayed fill. Shadow fills (zero latency,
  alert-time ask) are only ever shown as a comparison.
* Simulated trades written by ``backtest`` (strategy ``backtest:*``) are shown
  in their own section, labelled SIMULATED, and never mixed into the live
  numbers or the live warning.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .app import App
from .horizon import DAY, NO_END_DATE, TOO_LONG, days_to_close, held_days, scheduled_end
from .horizon import bucket as horizon_bucket
from .stats_util import max_drawdown, mean, median, two_way_bootstrap_mean_ci
from .timeutil import iso

PRICE_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
BACKTEST_PREFIX = "backtest:"
BACKTEST_STATE_KEY = "backtest_run"
BREAKDOWN_ROWS = 15

QUERY = """
SELECT p.*, a.wallet, a.kind, a.outcome, a.whale_price, a.whale_usd, a.first_fill_ts, a.last_fill_ts,
       a.trigger_fill_ts,
       a.detected_at, a.posted_at, a.status AS alert_status, a.snapshot_run_id, a.probation, a.membership_id,
       m.category, m.question, m.slug, m.event_slug, m.game_start_time,
       m.end_date AS market_end, m.resolved_at AS market_resolved_at, m.fee_rate AS market_fee_rate,
       m.fees_enabled AS market_fees_enabled, m.fee_exponent AS market_fee_exponent
FROM paper_trades p
JOIN alerts a ON a.id = p.alert_id
LEFT JOIN poly_markets m ON m.condition_id = p.condition_id
ORDER BY p.alert_ts, p.id
"""


@dataclass
class Summary:
    label: str
    alerts: int = 0
    filled: int = 0
    missed: int = 0
    pending: int = 0
    settled: int = 0
    open: int = 0
    wallets: int = 0               # among settled trades
    events: int = 0                # among settled trades
    total_pnl: float = 0.0
    total_cost: float = 0.0
    roi: float | None = None
    win_rate: float | None = None
    mean_roi: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    avg_clv: float | None = None
    clv_n: int = 0
    clv_inplay_excluded: int = 0       # fills after the game started: no closing price to compare with
    avg_detection_delay: float | None = None
    avg_fill_delay: float | None = None
    avg_latency_cost: float | None = None      # fill - whale price at the alert, filled trades only
    avg_detection_cost: float | None = None    # alert-time ask - whale price, every alert with an ask
    detection_n: int = 0
    avg_fill_cost: float | None = None         # fill - alert-time ask, filled trades
    max_drawdown: float = 0.0
    zero_pnl: float | None = None
    atask_pnl: float | None = None
    real_pnl_same_set: float | None = None
    shadow_n: int = 0
    miss_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def ci_includes_zero(self) -> bool:
        return self.ci_low is not None and self.ci_high is not None and self.ci_low <= 0 <= self.ci_high


def load_trades(app: App) -> list[dict]:
    from .costs import apply_policy
    return [horizon_fields(apply_policy(dict(r)), r["fill_ts"] or r["alert_ts"]) for r in app.db.query(QUERY)]


def horizon_fields(d: dict, entered: float | None) -> dict:
    """Time-to-close, as scheduled and as it turned out (v2.1 Part 3).

    ``days_to_close``: scheduled end minus the entry, in days. Stored on the row since
    schema 7; for an older row it is worked out from the market's end date as stored
    today. ``held_days``: entry to the real end of the position (a mirror exit, else
    the market's resolution). Only a closed trade has one."""
    end = (d.get("scheduled_end") if d.get("scheduled_end") is not None else
           scheduled_end(d.get("market_end"), d.get("game_start_time")))
    days = d.get("days_to_close")
    if days is None:
        days = days_to_close(end, entered) if entered is not None else None
    resolved = d.get("resolved_at") if d.get("resolved_at") is not None else d.get("market_resolved_at")
    done = d.get("exit_at") or (resolved if d.get("closed_at") or d.get("settled_at") else None)
    d.update(scheduled_end=end, days_to_close=days, resolved_ts=resolved,
             held_days=held_days(entered, done) if d["status"] == "FILLED" else None,
             late_days=(resolved - end) / DAY if resolved is not None and end is not None else None)
    return d


# -- the bot model (v2 Phase 3) -------------------------------------------------------

BOT_HEADLINE = "flat_hold"
BOT_QUERY = """
SELECT b.*, a.kind, a.outcome, a.whale_usd, a.probation, a.membership_id, a.detect_delay, a.trigger_fill_ts,
       a.snapshot_run_id, m.category, m.question, m.slug, m.event_slug, m.game_start_time,
       m.end_date AS market_end, m.resolved_at AS market_resolved_at, m.fee_rate AS market_fee_rate,
       m.fees_enabled AS market_fees_enabled, m.fee_exponent AS market_fee_exponent
FROM bot_trades b
LEFT JOIN alerts a ON a.id = b.alert_id
LEFT JOIN poly_markets m ON m.condition_id = b.condition_id
ORDER BY b.signal_ts, b.id
"""


def load_bot_trades(app: App) -> list[dict]:
    """Bot ledger rows, with the names ``summarise`` and the breakdowns read."""
    from .costs import apply_policy
    out = []
    for r in app.db.query(BOT_QUERY):
        d = apply_policy(dict(r))
        d.update(venue=f"bot:{d['runner']}", settled_at=d["closed_at"], miss_reason=d["reason"],
                 alert_ts=d["signal_ts"], alert_ask=d["best_ask"], atask_pnl=None,
                 fill_ts=d["decided_at"] if d["status"] == "FILLED" else None,
                 # the whale's price is the zero-latency reference on every strategy
                 zero_price=d["zero_price"] if d["zero_price"] is not None else d["whale_price"])
        out.append(horizon_fields(d, d["decided_at"]))
    return out


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def detection_lines(app: App) -> list[str]:
    """How long after the whale's fill we saw it: the distribution, not just a mean."""
    limit = app.cfg.alerts.max_alert_age_seconds
    rows = [r["detect_delay"] for r in app.db.query(
        "SELECT detect_delay FROM alerts WHERE detect_delay IS NOT NULL AND status != 'BACKTEST'")]
    # A fill first seen more than max_alert_age late was not a slow detection: the
    # detector was not running (a restart, an outage). Counted, not averaged in.
    delays = [d for d in rows if d <= limit]
    late = len(rows) - len(delays)
    if not delays:
        return []
    fast = sum(1 for d in delays if d <= 5) / len(delays)
    src = {r["source"]: r["n"] for r in app.db.query("SELECT source, COUNT(*) n FROM fill_events GROUP BY source")}
    return [f"DETECTION DELAY (whale's fill -> our detector saw it), n={len(delays)} alerts: "
            f"median {median(delays):.1f}s | p90 {_quantile(delays, 0.9):.1f}s | p99 {_quantile(delays, 0.99):.1f}s | "
            f"max {max(delays):.0f}s | {fast:.0%} within 5s (target: median under 5s)",
            f"  fills published: {src.get('feed', 0)} from the global feed, {src.get('backstop', 0)} that only the "
            "per-wallet backstop saw"
            + (f" | {late} more alerts were first seen over {limit // 60} min late (detector restarts or outages)"
               if late else "")]


def cost_line(rows: list[dict]) -> str:
    """Gross, costs and net of the closed trades. Every headline figure is already net."""
    from .costs import breakdown
    c = breakdown(rows)
    return (f"  costs (n={c.n} closed): gross {_money(c.gross)} | fees -${c.fees:,.2f} | slippage "
            f"-${c.slippage:,.2f} | net {_money(c.net)}   (P&L, ROI and the interval above are all net)")


def _paired(a: list[dict], b: list[dict], samples: int) -> tuple[int, float | None, float | None, float | None]:
    """Mean P&L difference (a - b) on the signals both strategies filled and closed."""
    other = {t["alert_id"]: t for t in b if t["status"] == "FILLED" and t["closed_at"] is not None}
    pairs = [(t, other[t["alert_id"]]) for t in a
             if t["status"] == "FILLED" and t["closed_at"] is not None and t["alert_id"] in other]
    if not pairs:
        return 0, None, None, None
    diffs = [x["pnl"] / x["cost"] - y["pnl"] / y["cost"] for x, y in pairs]
    m, lo, hi = two_way_bootstrap_mean_ci(diffs, [x["wallet"] for x, _ in pairs], [event_key(x) for x, _ in pairs],
                                          samples=samples)
    return len(pairs), m, lo, hi


def _bot_strategy_lines(app: App, runner: str, strategy: str, rows: list[dict], threshold: int, samples: int,
                        *, brief: bool) -> list[str]:
    from .bot import Bot
    traded = [t for t in rows if t["status"] != "SKIPPED"]
    skipped = [t for t in rows if t["status"] == "SKIPPED"]
    s = summarise(f"{runner} / {strategy}", traded, samples=samples)
    acct = Bot(app, runner).account(strategy)
    if brief and strategy != BOT_HEADLINE:
        head = _brief_summary(s, threshold)
        head[0] += f" | bankroll ${acct.equity:,.2f}"
        return head
    note = {"flat_hold": "bot fill at detection, held to resolution; the headline",
            "flat_mirror": "bot fill, sold when the whale sells",
            "scaled_hold": "stake grows with the whale's size, capped",
            "placebo_late": "the control: same outcome at a random later time"}.get(strategy, "")
    lines = [f"== {runner} / {strategy} ({note}) =="]
    if s.settled < threshold:
        lines.append(f"  ⚠ only {s.settled} closed trades: not enough to draw conclusions (need {threshold})")
    lines.append(f"  signals {len(rows)} | filled {s.filled} | missed {s.missed} | skipped {len(skipped)} | "
                 f"waiting {s.pending} | closed {s.settled} | open {s.open}")
    if s.missed:
        lines.append("  missed because: " + ", ".join(f"{k} {v}" for k, v in sorted(s.miss_reasons.items())))
    if skipped:
        why: dict[str, int] = defaultdict(int)
        for t in skipped:
            why[(t["reason"] or "unknown").split(":")[0]] += 1
        lines.append("  skipped by a risk limit: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())))
    lines.append(f"  paper bankroll ${acct.equity:,.2f} (started ${acct.starting:,.2f}) | open at cost "
                 f"${acct.open_cost:,.2f} | free ${acct.cash:,.2f} | closed today {_money(acct.today)}")
    if s.settled:
        lines.append(f"  total P&L {_money(s.total_pnl)} on ${s.total_cost:,.2f} staked (n={s.settled} closed) | "
                     f"ROI {_pct(s.roi)} | win rate {s.win_rate:.0%}")
        lines.append(f"  mean ROI per trade {_pct(s.mean_roi)}, 95% CI {_ci_text(s)}, n={s.settled} "
                     f"({s.wallets} wallets, {s.events} events, resampled together)")
        verdict = _verdict(s)
        if verdict:
            lines.append(f"  -> {verdict}")
        lines.append(f"  max drawdown {_money(-s.max_drawdown) if s.max_drawdown else '$0.00'}")
        lines.append(cost_line(traded))
    else:
        lines.append("  no closed trades yet, so no P&L.")
    if brief:
        return lines

    def cents(x: float | None) -> str:
        return "-" if x is None else f"{x:+.3f}"

    marks = [t["markout_price"] - t["fill_price"] for t in traded
             if t["status"] == "FILLED" and t["markout_price"] is not None]
    inplay = f", {s.clv_inplay_excluded} in-play fills have no closing line" if s.clv_inplay_excluded else ""
    lines.append(f"  CLV: closing line {cents(s.avg_clv)} (n={s.clv_n}{inplay}) | price "
                 f"{app.cfg.bot.markout_seconds // 60} min after our fill {cents(mean(marks))} (n={len(marks)}, every fill)")
    # Signals we reached too late (a restart, an outage) are already counted as misses
    # above; averaging minutes-old backlog into this line would hide how fast the bot is.
    limit = app.cfg.bot.max_signal_age_seconds
    decide = [t["decided_at"] - t["signal_ts"] for t in traded if t["status"] in ("FILLED", "MISSED")
              and strategy != "placebo_late" and t["decided_at"] - t["signal_ts"] <= limit]
    if decide:
        lines.append(f"  whale's fill -> our decision: median {median(decide):.1f}s | p90 {_quantile(decide, 0.9):.1f}s "
                     f"(n={len(decide)} signals acted on within {limit:g}s)")
    lines.append(f"  price vs the whale, per share: whale -> best ask when we looked {cents(s.avg_detection_cost)} "
                 f"(n={s.detection_n}, missed included) | whale -> our fill {cents(s.avg_latency_cost)} (filled only)")
    done = [t for t in traded if t["closed_at"] is not None and t["zero_pnl"] is not None and t["human_pnl"] is not None]
    if done:
        lines.append(f"  same {len(done)} closed signals, same stake, a miss counted as $0: bot "
                     f"{_money(sum(t['pnl'] or 0.0 for t in done))} | zero latency (the whale's price) "
                     f"{_money(sum(t['zero_pnl'] for t in done))} | old rule ({app.cfg.bot.human_delay_seconds}s later) "
                     f"{_money(sum(t['human_pnl'] for t in done))}")
    return lines


def bot_lines(app: App, *, threshold: int, samples: int, brief: bool, with_breakdowns: bool) -> list[str]:
    from .config import BOT_STRATEGIES
    trades = load_bot_trades(app)
    if not trades:
        return []
    lines = ["BOT PAPER TRADES: a bot that buys the moment a whale fill is detected (paper only)"]
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by[(t["runner"], t["strategy"])].append(t)
    order = {name: i for i, name in enumerate(BOT_STRATEGIES)}
    for (runner, strategy), rows in sorted(by.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], 99))):
        lines += _bot_strategy_lines(app, runner, strategy, rows, threshold, samples, brief=brief)
    for runner in sorted({r for r, _ in by}):
        n, m, lo, hi = _paired(by.get((runner, BOT_HEADLINE), []), by.get((runner, "placebo_late"), []), samples)
        if n:
            ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
            verdict = ("copying beats the control" if lo is not None and lo > 0 else
                       "the control beats copying" if hi is not None and hi < 0 else
                       "no difference shown: the whale's timing has not been shown to matter")
            lines.append(f"  {runner}: flat_hold minus placebo_late, ROI per trade on the {n} signals both filled: "
                         f"{_pct(m)}, 95% CI {ci} -> {verdict}")
    for runner in sorted({r for r, _ in by}):
        lines += cutoff_lines(app, runner, by.get((runner, BOT_HEADLINE), []))
    lines += wallet_lines(app, trades, brief=brief)
    lines += detection_lines(app)
    if with_breakdowns:
        headline = [t for t in trades if t["strategy"] == BOT_HEADLINE and t["status"] != "SKIPPED"]
        if headline:
            lines += _breakdown_lines(headline, f" ({BOT_HEADLINE})",
                                      ("watchlist status", "month", "category", "days to close when entered",
                                       "holding time", "entry price", "wallet"))
    return lines


def wallet_lines(app: App, bot_trades: list[dict], *, brief: bool) -> list[str]:
    """v2.1 Part 2: the small wallet, per runner. The daily summary gets the bot
    runners only; the full report adds the old rule and each simulated backtest."""
    from . import bankroll
    records = bot_trades if brief else bot_trades + load_trades(app)
    try:
        lines = bankroll.render(records, app.cfg, app.db, app.clock(), brief=brief, simulated=not brief)
    except Exception as exc:  # noqa: BLE001 - a report must not fail on its newest section
        return [f"  (the small-wallet section could not be worked out: {type(exc).__name__}: {exc})"]
    return ([""] + lines) if lines else []


@dataclass
class Cutoff:
    """What the time-to-close cutoff did, and how the schedule compared with reality."""
    limit: float | None
    too_long: int = 0
    no_end_date: int = 0
    closed: int = 0            # filled trades that are closed and have a known holding time
    held_over: int = 0         # ...held longer than the limit
    resolved: int = 0          # filled trades whose market resolved and had a scheduled end
    late: int = 0              # ...resolved more than a day after that end
    would_skip: int = 0        # filled before the cutoff existed, in a market it would now refuse
    would_skip_pnl: float = 0.0


def cutoff_report(app: App, rows: list[dict], *, today: int | None = None) -> Cutoff:
    """``today``: count only signals from this timestamp on (the daily summary)."""
    limit = app.cfg.filters.max_days_to_close
    c = Cutoff(limit)
    for t in rows:
        if today is not None and t["alert_ts"] < today:
            continue
        code = t.get("skip_code")
        c.too_long += code == TOO_LONG
        c.no_end_date += code == NO_END_DATE
        if t["status"] != "FILLED":
            continue
        if t.get("held_days") is not None:
            c.closed += 1
            c.held_over += limit is not None and t["held_days"] > limit
        if t.get("late_days") is not None:
            c.resolved += 1
            c.late += t["late_days"] > 1
        days = t.get("days_to_close")
        if (days is None and app.cfg.filters.skip_if_no_end_date) or (
                days is not None and limit is not None and days > limit):
            c.would_skip += 1
            c.would_skip_pnl += t["pnl"] if t["settled_at"] is not None and t["pnl"] is not None else 0.0
    return c


def cutoff_lines(app: App, runner: str, rows: list[dict]) -> list[str]:
    c = cutoff_report(app, rows)
    limit = "off" if c.limit is None else f"{c.limit:g} days"
    lines = [f"  {runner}: time-to-close cutoff ({limit}): {c.too_long + c.no_end_date} signals skipped "
             f"({TOO_LONG} {c.too_long}, {NO_END_DATE} {c.no_end_date})"]
    if c.closed and c.limit is not None:
        lines.append(f"    of {c.closed} closed {BOT_HEADLINE} trades, {c.held_over} ({c.held_over / c.closed:.0%}) "
                     f"were held longer than {c.limit:g} days")
    if c.resolved:
        lines.append(f"    of {c.resolved} whose market resolved, {c.late} ({c.late / c.resolved:.0%}) resolved more "
                     "than a day after the scheduled end")
    if c.would_skip:
        lines.append(f"    {c.would_skip} filled trades were entered in markets the cutoff now refuses "
                     f"(P&L of the closed ones {_money(c.would_skip_pnl)}); they stay in the record")
    return lines


def in_play(t: dict) -> bool:
    """Whether the paper fill came after the game started (sports markets only)."""
    start, filled = t.get("game_start_time"), t.get("fill_ts")
    return bool(start and filled and filled >= start)


def is_backtest(t: dict) -> bool:
    return str(t["strategy"]).startswith(BACKTEST_PREFIX)


def event_key(t: dict) -> str:
    return t.get("event_slug") or t["condition_id"]


def summarise(label: str, trades: list[dict], *, samples: int = 10_000) -> Summary:
    s = Summary(label)
    s.alerts = len(trades)
    s.filled = sum(1 for t in trades if t["status"] == "FILLED")
    s.missed = sum(1 for t in trades if t["status"] == "MISSED")
    s.pending = sum(1 for t in trades if t["status"] == "PENDING")
    settled = [t for t in trades if t["status"] == "FILLED" and t["settled_at"] is not None]
    s.settled = len(settled)
    s.open = s.filled - s.settled
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        if t["status"] == "MISSED":
            reasons[(t["miss_reason"] or "unknown").split(":")[0]] += 1
    s.miss_reasons = dict(reasons)

    if settled:
        s.wallets = len({t["wallet"] for t in settled})
        s.events = len({event_key(t) for t in settled})
        s.total_pnl = sum(t["pnl"] for t in settled)
        s.total_cost = sum(t["cost"] for t in settled)
        s.roi = s.total_pnl / s.total_cost if s.total_cost else None
        s.win_rate = sum(1 for t in settled if t["pnl"] > 0) / len(settled)
        s.mean_roi, s.ci_low, s.ci_high = two_way_bootstrap_mean_ci(
            [t["pnl"] / t["cost"] for t in settled], [t["wallet"] for t in settled],
            [event_key(t) for t in settled], samples=samples)
        s.max_drawdown = max_drawdown([t["pnl"] for t in sorted(settled, key=lambda t: (t["settled_at"], t["id"]))])
    # A sports market's closing price is the last price before the game starts. For a fill
    # made after the start that price is older than the fill, so there is nothing to compare.
    inplay = [t for t in settled if in_play(t)]
    clvs = [t["closing_price"] - t["fill_price"] for t in settled if t["closing_price"] is not None and not in_play(t)]
    s.avg_clv, s.clv_n, s.clv_inplay_excluded = mean(clvs), len(clvs), len(inplay)
    # Measured from the fill that made the group alert-worthy, so the time a whale
    # spends building a position is not counted as our latency.
    s.avg_detection_delay = mean([t["detected_at"] - t["trigger_fill_ts"] for t in trades
                                  if t["detected_at"] and t["trigger_fill_ts"]])
    s.avg_fill_delay = mean([t["fill_ts"] - t["trigger_fill_ts"] for t in trades
                             if t["status"] == "FILLED" and t["fill_ts"] and t["trigger_fill_ts"]])
    # All against the whale's price when the paper trade opened (zero_price), the same price
    # the zero-latency shadow uses: the alert's whale_price keeps moving as later fills arrive.
    s.avg_latency_cost = mean([t["fill_price"] - t["zero_price"] for t in trades
                               if t["status"] == "FILLED" and t["fill_price"] is not None
                               and t["zero_price"] is not None])
    detection = [t["alert_ask"] - t["zero_price"] for t in trades
                 if t["alert_ask"] is not None and t["zero_price"] is not None]
    s.avg_detection_cost, s.detection_n = mean(detection), len(detection)
    s.avg_fill_cost = mean([t["fill_price"] - t["alert_ask"] for t in trades
                            if t["status"] == "FILLED" and t["fill_price"] is not None and t["alert_ask"] is not None])
    # Shadow comparison over every alert whose market has settled, MISSED included:
    # zero-latency and alert-time fills were always possible; the real strategy
    # earned 0 on the trades it missed.
    shadow = [t for t in trades if t["settled_at"] is not None and t["zero_pnl"] is not None
              and t["atask_pnl"] is not None]
    s.shadow_n = len(shadow)
    if shadow:
        s.zero_pnl = sum(t["zero_pnl"] for t in shadow)
        s.atask_pnl = sum(t["atask_pnl"] for t in shadow)
        s.real_pnl_same_set = sum(t["pnl"] or 0.0 for t in shadow)
    return s


def bucket(price: float | None) -> str:
    if price is None:
        return "unknown"
    for lo, hi in PRICE_BUCKETS:
        if lo <= price < hi:
            return f"{lo:.1f}-{hi:.1f}"
    return "1.0"


BREAKDOWN_KEYS: dict[str, Callable[[dict], str]] = {
    "month": lambda t: iso(t["alert_ts"])[:7],
    "wallet": lambda t: t["wallet"],
    "category": lambda t: t["category"] or "unknown",
    "days to close when entered": lambda t: horizon_bucket(t.get("days_to_close")),
    "holding time": lambda t: horizon_bucket(t.get("held_days")) if t.get("held_days") is not None else "still open",
    "venue": lambda t: t["venue"],
    "entry price": lambda t: bucket(t["fill_price"] if t["fill_price"] is not None else t["alert_ask"]),
    # Frozen on the alert when it fired (monitor._trigger), never recomputed.
    "watchlist status": lambda t: {None: "before the watchlist", 1: "probation", 0: "established"}[t.get("probation")],
}


def breakdowns(trades: list[dict], *, samples: int = 2000,
               keys: tuple[str, ...] = tuple(BREAKDOWN_KEYS)) -> dict[str, list[Summary]]:
    out = {}
    for name in keys:
        key = BREAKDOWN_KEYS[name]
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            groups[key(t)].append(t)
        rows = [summarise(k, v, samples=samples) for k, v in groups.items()]
        rows.sort(key=(lambda s: s.label) if name == "month" else (lambda s: (-s.settled, s.label)))
        out[name] = rows
    return out


def _money(x: float | None) -> str:
    return "-" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.1%}"


def _secs(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f}s"


def _ci_text(s: Summary) -> str:
    return "no CI (too few wallets and events)" if s.ci_low is None else f"[{_pct(s.ci_low)}, {_pct(s.ci_high)}]"


def _verdict(s: Summary) -> str | None:
    if s.ci_includes_zero:
        return "The confidence interval includes zero: no evidence yet that copying these alerts makes money."
    if s.ci_low is not None and s.ci_low > 0:
        return "The confidence interval is above zero."
    if s.ci_high is not None and s.ci_high < 0:
        return "The confidence interval is below zero: copying these alerts has lost money."
    return None


def _per_strategy(trades: list[dict]) -> list[tuple[tuple[str, str], list[dict]]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["venue"], t["strategy"])].append(t)
    return sorted(groups.items())


def backtest_run(app: App) -> dict | None:
    row = app.db.one("SELECT value FROM service_state WHERE key = ?", (BACKTEST_STATE_KEY,))
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def _backtest_header(app: App) -> list[str]:
    run = backtest_run(app)
    lines = ["SIMULATED PAPER TRADES: a backtest replay of past periods, not live trading"]
    if run:
        lines.append(f"  written {run.get('written_at', '?')} by `python -m whalewatcher backtest`: copy fills "
                     f"{run.get('delay', '?')}s after the triggering fill at the CLOB minute price "
                     f"+ {run.get('ask_premium', '?')}, plus fees, held to resolution.")
    lines.append("  Wallets were picked at each period's start with earlier data only, then held for the period "
                 "(live re-picks nightly). Minute prices, not real order books. Treat as an estimate, not a "
                 "track record.")
    return lines


def render(app: App, *, with_breakdowns: bool = True, brief_backtest: bool = False) -> str:
    trades = load_trades(app)
    threshold = app.cfg.stats.min_settled_for_conclusions
    samples = app.cfg.stats.bootstrap_samples
    live = [t for t in trades if not is_backtest(t)]
    simulated = [t for t in trades if is_backtest(t)]
    lines: list[str] = []
    bot = bot_lines(app, threshold=threshold, samples=samples, brief=brief_backtest, with_breakdowns=with_breakdowns)
    if bot:
        headline = app.db.one("SELECT COUNT(*) n FROM bot_trades WHERE strategy = ? AND status = 'FILLED' "
                              "AND closed_at IS NOT NULL", (BOT_HEADLINE,))["n"]
        label = "closed bot paper trades (flat_hold)"
    else:
        headline = sum(1 for t in live if t["status"] == "FILLED" and t["settled_at"] is not None)
        label = "settled live paper trades"
    if headline < threshold:
        lines.append(f"⚠ NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: {headline} {label} "
                     f"(need at least {threshold}). Every live number below is noise until then.")
        lines.append("")
    if not trades and not bot:
        lines.append("No paper trades yet.")
        return "\n".join(lines)

    if bot:
        lines += bot
        lines.append("")
        lines.append("OLD RULE, LIVE: one paper fill a poll (about a minute) after the alert. Closed to new alerts "
                     "since the bot model started; kept for comparison.")
    else:
        lines.append("LIVE PAPER TRADES")
    if not live:
        lines.append("  No live paper trades under the old rule." if bot else "  No live paper trades yet.")
    for (venue, strategy), group in _per_strategy(live):
        summary = summarise(f"{venue} / {strategy}", group, samples=samples)
        lines += _brief_summary(summary, threshold) if bot and brief_backtest else _render_summary(summary, threshold)
        if summary.settled and not (bot and brief_backtest):
            lines.append(cost_line(group))
    if with_breakdowns and live:
        lines += _breakdown_lines(live, " (old rule)" if bot else "", BREAKDOWN_KEYS)

    if simulated:
        lines.append("")
        lines += _backtest_header(app)
        for (venue, strategy), group in _per_strategy(simulated):
            s = summarise(f"{venue} / {strategy} (SIMULATED)", group, samples=samples)
            lines += _brief_summary(s, threshold) if brief_backtest else _render_summary(s, threshold)
            if with_breakdowns:
                lines += _breakdown_lines(group, f" ({venue} / {strategy}, SIMULATED)",
                                          ("month", "category", "entry price", "wallet"))
    lines.append("")
    lines.append("Research and education only. Not financial advice.")
    return "\n".join(lines)


def _breakdown_lines(trades: list[dict], suffix: str, keys) -> list[str]:
    lines: list[str] = []
    for name, rows in breakdowns(trades, keys=tuple(keys)).items():
        lines.append("")
        lines.append(f"By {name}{suffix}:")
        lines.append(f"  {'group':24s} {'alerts':>6s} {'settled':>7s} {'missed':>6s} {'pnl':>11s} "
                     f"{'roi':>7s} {'mean roi [95% CI]':>28s}")
        for s in rows[:BREAKDOWN_ROWS]:
            ci = "-" if s.ci_low is None else f"{_pct(s.mean_roi)} [{_pct(s.ci_low)}, {_pct(s.ci_high)}]"
            label = s.label if len(s.label) <= 24 else f"{s.label[:6]}…{s.label[-3:]}"
            lines.append(f"  {label:24s} {s.alerts:6d} {s.settled:7d} {s.missed:6d} "
                         f"{_money(s.total_pnl) if s.settled else '-':>11s} {_pct(s.roi):>7s} {ci:>28s}")
        if len(rows) > BREAKDOWN_ROWS:
            rest = rows[BREAKDOWN_ROWS:]
            lines.append(f"  … {len(rest)} more ({sum(s.settled for s in rest)} settled trades)")
    return lines


def _render_summary(s: Summary, threshold: int) -> list[str]:
    lines = [f"== {s.label} (strategy headline uses the real, delayed fill) =="]
    if s.settled < threshold:
        lines.append(f"  ⚠ only {s.settled} settled trades: not enough to draw conclusions (need {threshold})")
    lines.append(f"  alerts {s.alerts} | filled {s.filled} | missed {s.missed} | pending {s.pending} | "
                 f"settled {s.settled} | open {s.open}")
    if s.missed:
        lines.append("  missed because: " + ", ".join(f"{k} {v}" for k, v in sorted(s.miss_reasons.items())))
    if s.settled:
        lines.append(f"  total P&L {_money(s.total_pnl)} on ${s.total_cost:,.2f} staked (n={s.settled} settled) | "
                     f"ROI {_pct(s.roi)} | win rate {s.win_rate:.0%}")
        lines.append(f"  mean ROI per trade {_pct(s.mean_roi)}, 95% CI {_ci_text(s)}, n={s.settled} "
                     f"({s.wallets} wallets, {s.events} events, resampled together)")
        verdict = _verdict(s)
        if verdict:
            lines.append(f"  -> {verdict}")
        lines.append(f"  max drawdown {_money(-s.max_drawdown) if s.max_drawdown else '$0.00'}")
    else:
        lines.append("  no settled trades yet, so no P&L.")
    def cents(x: float | None) -> str:
        return "-" if x is None else f"{x:+.3f}"

    inplay = f", {s.clv_inplay_excluded} in-play fills left out" if s.clv_inplay_excluded else ""
    lines.append(f"  avg CLV of paper fills {cents(s.avg_clv)} (n={s.clv_n}{inplay}) | "
                 f"detection delay {_secs(s.avg_detection_delay)} | fill delay {_secs(s.avg_fill_delay)} "
                 f"(from the fill that triggered the alert)")
    lines.append(f"  price paid vs the whale, per share: whale -> alert-time ask {cents(s.avg_detection_cost)} "
                 f"(n={s.detection_n}, missed included) | alert-time ask -> fill {cents(s.avg_fill_cost)} | "
                 f"whale -> fill {cents(s.avg_latency_cost)} (filled trades only)")
    if s.shadow_n:
        lines.append(f"  shadow comparison over {s.shadow_n} settled alerts (missed included): zero-latency "
                     f"{_money(s.zero_pnl)} | alert-time ask {_money(s.atask_pnl)} | real delayed fill "
                     f"{_money(s.real_pnl_same_set)}")
    return lines


def _brief_summary(s: Summary, threshold: int) -> list[str]:
    if not s.settled:
        return [f"  {s.label}: {s.alerts} paper trades, none settled yet, so no P&L."]
    warn = f" ⚠ only {s.settled} settled (need {threshold})" if s.settled < threshold else ""
    verdict = _verdict(s)
    return [f"  {s.label}: ROI {_pct(s.roi)} ({_money(s.total_pnl)} on ${s.total_cost:,.0f}) | mean per trade "
            f"{_pct(s.mean_roi)}, 95% CI {_ci_text(s)}, n={s.settled} ({s.wallets} wallets, {s.events} events)"
            f"{warn}" + (f"\n    -> {verdict}" if verdict else "")]


CSV_FIELDS = [
    "id", "alert_id", "venue", "strategy", "status", "miss_reason", "wallet", "kind", "category", "question",
    "outcome", "condition_id", "token_id", "first_fill_ts", "trigger_fill_ts", "detected_at", "posted_at", "alert_ts", "fill_ts",
    "whale_price", "whale_usd", "alert_ask", "fill_price", "contracts", "fees", "cost", "levels_walked",
    "settled_at", "settle_result", "payout_per_share", "payout", "pnl", "closing_price",
    "zero_price", "zero_cost", "zero_pnl", "atask_price", "atask_cost", "atask_pnl", "snapshot_run_id",
    "membership_id", "probation",
]


def export_csv(app: App, path: str) -> int:
    trades = load_trades(app)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow(t)
    return len(trades)


BOT_CSV_FIELDS = [
    "id", "runner", "strategy", "status", "reason", "alert_id", "wallet", "category", "question", "outcome",
    "condition_id", "token_id", "event_key", "signal_ts", "detected_at", "decided_at", "detect_delay", "stake_usd",
    "whale_price", "best_ask", "fill_price", "contracts", "fees", "cost", "levels", "zero_price", "zero_cost",
    "zero_pnl", "human_price", "human_cost", "human_pnl", "whale_shares", "whale_sold", "exit_at", "exit_price",
    "exit_fees", "proceeds", "closed_at", "result", "payout_per_share", "pnl", "closing_price", "markout_price",
    "probation", "membership_id", "snapshot_run_id",
]


def export_bot_csv(app: App, path: str) -> int:
    trades = load_bot_trades(app)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=BOT_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
    return len(trades)


def daily_summary(app: App) -> str:
    """The short version: what `stats --brief` prints and what the runner writes to
    its log once a day. Per runner: net P&L with its n and interval, where the money
    went, what the time-to-close cutoff skipped, and the small wallet. Kept under
    2,000 characters so it stays one screen. The full report is ``stats`` and the
    dashboard."""
    from . import bankroll
    bots = load_bot_trades(app)
    if not bots:        # only old-rule or simulated trades: the old short report
        text = render(app, with_breakdowns=False, brief_backtest=True)
        return text if len(text) <= 1900 else text[:1890] + "\n…"
    threshold, samples = app.cfg.stats.min_settled_for_conclusions, min(app.cfg.stats.bootstrap_samples, 2000)
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in bots:
        by[(t["runner"], t["strategy"])].append(t)
    lines: list[str] = []
    for runner in sorted({r for r, _ in by}):
        rows = by.get((runner, BOT_HEADLINE), [])
        traded = [t for t in rows if t["status"] != "SKIPPED"]
        s = summarise(runner, traded, samples=samples)
        lines.append(f"{runner} / {BOT_HEADLINE} (paper)")
        if s.settled < threshold:
            lines.append(f"  ⚠ only {s.settled} closed trades (need {threshold}): everything here is noise so far")
        if s.settled:
            lines.append(f"  net {_money(s.total_pnl)} on ${s.total_cost:,.2f} staked, n={s.settled} closed | ROI "
                         f"{_pct(s.roi)} | mean per trade {_pct(s.mean_roi)}, 95% CI {_ci_text(s)} | win rate "
                         f"{s.win_rate:.0%}")
            lines.append(cost_line(traded).split("   (")[0])
        else:
            lines.append("  no closed trades yet, so no P&L")
        c = cutoff_report(app, rows)
        limit = "off" if c.limit is None else f"{c.limit:g}-day"
        lines.append(f"  signals {len(rows)}: filled {s.filled}, missed {s.missed}, skipped {len(rows) - len(traded)} "
                     f"| open {s.open} | time-to-close cutoff ({limit}) skipped {c.too_long + c.no_end_date} "
                     f"({TOO_LONG} {c.too_long}, {NO_END_DATE} {c.no_end_date})")
        try:
            for view in bankroll.views(bots, app.cfg, app.db, app.clock(), only=f"bot:{runner}"):
                amount = view.replay.params.amount
                if view.live is not None:
                    lines.append(f"  ${amount:,.2f} wallet, {view.live.label.lower()}: {bankroll.result_line(view.live)}")
                lines.append(f"  ${amount:,.2f} wallet, replay of every signal: {bankroll.result_line(view.replay)}")
        except Exception as exc:  # noqa: BLE001 - the summary must still go out
            lines.append(f"  (the wallet could not be worked out: {type(exc).__name__})")
        n, m, lo, hi = _paired(rows, by.get((runner, "placebo_late"), []), samples)
        if n:
            ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
            lines.append(f"  against the control (same outcome, later), n={n}: {_pct(m)} ROI per trade, 95% CI {ci}")
    lines += detection_lines(app)[:1]
    lines.append("Full report: `python -m whalewatcher stats`, or the dashboard.")
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1890] + "\n…"
