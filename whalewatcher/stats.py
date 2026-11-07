"""Phase 5: would copying these alerts make money after realistic delay and fees?

Rules this module keeps:
* Below ``min_settled_for_conclusions`` settled trades, the output LEADS with a
  warning that there is not enough data.
* No profit figure is shown without its sample size and a 95% bootstrap CI.
  When the CI includes zero, the output says so in words.
* Headline numbers use the real, delayed fill. Shadow fills (zero latency,
  alert-time ask) are only ever shown as a comparison.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .app import App
from .stats_util import bootstrap_mean_ci, max_drawdown, mean

PRICE_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]

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
        s.total_pnl = sum(t["pnl"] for t in settled)
        s.total_cost = sum(t["cost"] for t in settled)
        s.roi = s.total_pnl / s.total_cost if s.total_cost else None
        s.win_rate = sum(1 for t in settled if t["pnl"] > 0) / len(settled)
        s.mean_roi, s.ci_low, s.ci_high = bootstrap_mean_ci([t["pnl"] / t["cost"] for t in settled], samples=samples)
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


def breakdowns(trades: list[dict], *, samples: int = 2000) -> dict[str, list[Summary]]:
    keys = {
        "wallet": lambda t: t["wallet"],
        "category": lambda t: t["category"] or "unknown",
        "venue": lambda t: t["venue"],
        "entry price": lambda t: bucket(t["fill_price"] if t["fill_price"] is not None else t["alert_ask"]),
    }
    out = {}
    for name, key in keys.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            groups[key(t)].append(t)
        out[name] = sorted((summarise(k, v, samples=samples) for k, v in groups.items()),
                           key=lambda s: (-s.settled, s.label))
    return out


def _money(x: float | None) -> str:
    return "-" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.1%}"


def _secs(x: float | None) -> str:
    return "-" if x is None else f"{x:,.0f}s"


def render(app: App, *, with_breakdowns: bool = True) -> str:
    trades = load_trades(app)
    threshold = app.cfg.stats.min_settled_for_conclusions
    lines: list[str] = []
    per_strategy: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        per_strategy[(t["venue"], t["strategy"])].append(t)
    total_settled = sum(1 for t in trades if t["status"] == "FILLED" and t["settled_at"] is not None)
    if total_settled < threshold:
        lines.append(f"⚠ NOT ENOUGH DATA TO DRAW CONCLUSIONS YET: {total_settled} settled paper trades "
                     f"(need at least {threshold}). Every number below is noise until then.")
        lines.append("")
    if not trades:
        lines.append("No paper trades yet.")
        return "\n".join(lines)
    for (venue, strategy), group in sorted(per_strategy.items()):
        s = summarise(f"{venue} / {strategy}", group, samples=app.cfg.stats.bootstrap_samples)
        lines += _render_summary(s)
    if with_breakdowns:
        for name, rows in breakdowns(trades).items():
            lines.append("")
            lines.append(f"By {name}:")
            lines.append(f"  {'group':24s} {'alerts':>6s} {'settled':>7s} {'missed':>6s} {'pnl':>11s} "
                         f"{'roi':>7s} {'mean roi [95% CI]':>28s}")
            for s in rows:
                ci = "-" if s.ci_low is None else f"{_pct(s.mean_roi)} [{_pct(s.ci_low)}, {_pct(s.ci_high)}]"
                label = s.label if len(s.label) <= 24 else f"{s.label[:6]}…{s.label[-3:]}"
                lines.append(f"  {label:24s} {s.alerts:6d} {s.settled:7d} {s.missed:6d} "
                             f"{_money(s.total_pnl) if s.settled else '-':>11s} {_pct(s.roi):>7s} {ci:>28s}")
    lines.append("")
    lines.append("Research and education only. Not financial advice.")
    return "\n".join(lines)


def _render_summary(s: Summary) -> list[str]:
    lines = [f"== {s.label} (strategy headline uses the real, delayed fill) =="]
    lines.append(f"  alerts {s.alerts} | filled {s.filled} | missed {s.missed} | pending {s.pending} | "
                 f"settled {s.settled} | open {s.open}")
    if s.missed:
        lines.append("  missed because: " + ", ".join(f"{k} {v}" for k, v in sorted(s.miss_reasons.items())))
    if s.settled:
        ci = f"[{_pct(s.ci_low)}, {_pct(s.ci_high)}]" if s.ci_low is not None else "(n=1, no CI)"
        lines.append(f"  total P&L {_money(s.total_pnl)} on ${s.total_cost:,.2f} staked (n={s.settled} settled) | "
                     f"ROI {_pct(s.roi)} | win rate {s.win_rate:.0%}")
        lines.append(f"  mean ROI per trade {_pct(s.mean_roi)}, 95% CI {ci}, n={s.settled}")
        if s.ci_includes_zero:
            lines.append("  -> The confidence interval includes zero: no evidence yet that copying these alerts "
                         "makes money.")
        elif s.ci_low is not None and s.ci_low > 0:
            lines.append("  -> The confidence interval is above zero.")
        elif s.ci_high is not None and s.ci_high < 0:
            lines.append("  -> The confidence interval is below zero: copying these alerts has lost money.")
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
    """The short version for the stats webhook."""
    text = render(app, with_breakdowns=False)
    return text if len(text) <= 1900 else text[:1890] + "\n…"
