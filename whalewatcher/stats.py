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
from .stats_util import max_drawdown, mean, two_way_bootstrap_mean_ci
from .timeutil import iso

PRICE_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
BACKTEST_PREFIX = "backtest:"
BACKTEST_STATE_KEY = "backtest_run"
BREAKDOWN_ROWS = 15

QUERY = """
SELECT p.*, a.wallet, a.kind, a.outcome, a.whale_price, a.whale_usd, a.first_fill_ts, a.last_fill_ts,
       a.trigger_fill_ts,
       a.detected_at, a.posted_at, a.status AS alert_status, a.snapshot_run_id,
       m.category, m.question, m.slug, m.event_slug
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
    avg_detection_delay: float | None = None
    avg_fill_delay: float | None = None
    avg_latency_cost: float | None = None
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
    return [dict(r) for r in app.db.query(QUERY)]


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
    clvs = [t["closing_price"] - t["fill_price"] for t in settled if t["closing_price"] is not None]
    s.avg_clv = mean(clvs)
    # Measured from the fill that made the group alert-worthy, so the time a whale
    # spends building a position is not counted as our latency.
    s.avg_detection_delay = mean([t["detected_at"] - t["trigger_fill_ts"] for t in trades
                                  if t["detected_at"] and t["trigger_fill_ts"]])
    s.avg_fill_delay = mean([t["fill_ts"] - t["trigger_fill_ts"] for t in trades
                             if t["status"] == "FILLED" and t["fill_ts"] and t["trigger_fill_ts"]])
    s.avg_latency_cost = mean([t["fill_price"] - t["whale_price"] for t in trades
                               if t["status"] == "FILLED" and t["fill_price"] is not None])
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
    "venue": lambda t: t["venue"],
    "entry price": lambda t: bucket(t["fill_price"] if t["fill_price"] is not None else t["alert_ask"]),
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
    live_settled = sum(1 for t in live if t["status"] == "FILLED" and t["settled_at"] is not None)
    if live_settled < threshold:
        lines.append(f"⚠ NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: {live_settled} settled live paper trades "
                     f"(need at least {threshold}). Every live number below is noise until then.")
        lines.append("")
    if not trades:
        lines.append("No paper trades yet.")
        return "\n".join(lines)

    lines.append("LIVE PAPER TRADES")
    if not live:
        lines.append("  No live paper trades yet.")
    for (venue, strategy), group in _per_strategy(live):
        lines += _render_summary(summarise(f"{venue} / {strategy}", group, samples=samples), threshold)
    if with_breakdowns and live:
        lines += _breakdown_lines(live, "", BREAKDOWN_KEYS)

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
    lines.append(f"  avg CLV of paper fills {'-' if s.avg_clv is None else f'{s.avg_clv:+.3f}'} | "
                 f"detection delay {_secs(s.avg_detection_delay)} | fill delay {_secs(s.avg_fill_delay)} "
                 f"(from the fill that triggered the alert) | latency cost "
                 f"{'-' if s.avg_latency_cost is None else f'{s.avg_latency_cost:+.3f}'} per share")
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


def daily_summary(app: App) -> str:
    """The short version for the stats webhook: live in full, each backtest strategy in one line."""
    text = render(app, with_breakdowns=False, brief_backtest=True)
    return text if len(text) <= 1900 else text[:1890] + "\n…"
