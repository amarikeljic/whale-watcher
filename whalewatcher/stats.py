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
* The bot headline counts one model version (version.py), the current one by
  default, and leaves out signals inside outage windows unless asked. Rows of
  other versions, "before tagging" included, are shown as their own groups.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app import App
from .horizon import DAY, NO_END_DATE, TOO_LONG, days_to_close, held_days, scheduled_end
from .horizon import bucket as horizon_bucket
from .stats_util import max_drawdown, mean, median, two_way_bootstrap_mean_ci
from .timeutil import iso
from .version import BEFORE_TAGGING, MODEL_VERSION
from .version import label as model_label

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
{where}ORDER BY p.alert_ts, p.id
"""
SIM_PREFIX = "backtest:"         # a simulated replay's strategy name starts with this


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


def load_trades(app: App, *, sims: bool = True) -> list[dict]:
    """Old-rule paper trades, and the simulated replays unless ``sims`` is False. The
    replays are most of the rows (24,800 of 25,000 on the live copy, 2026-11-14) and only
    the Edge page and a replay's own view show them."""
    from .costs import apply_policy
    sql = QUERY.format(where="" if sims else f"WHERE p.strategy NOT LIKE '{SIM_PREFIX}%' ")
    return [horizon_fields(apply_policy(dict(r)), r["fill_ts"] or r["alert_ts"]) for r in app.db.query(sql)]


def sim_strategies(app: App) -> list[tuple[str, str]]:
    """(strategy, venue) of every simulated replay, without loading its rows."""
    return [(r["strategy"], r["venue"]) for r in app.db.query(
        f"SELECT DISTINCT strategy, venue FROM paper_trades WHERE strategy LIKE '{SIM_PREFIX}%'")]


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
US_RUNNER = "us"            # polymarket_us.RUNNER: the same signals priced on Polymarket US
BOT_QUERY = """
SELECT b.*, a.kind, a.outcome, a.whale_usd, a.probation, a.membership_id, a.detect_delay, a.trigger_fill_ts,
       a.snapshot_run_id, a.alert_best_ask, a.alert_best_bid, m.description AS market_rules,
       a.opposite_before_usd, a.opposite_after_usd, a.related_usd, a.sold_back_share, a.followed_at,
       a.book_fetch_seconds,
       m.category, m.question, m.slug, m.event_slug, m.game_start_time,
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


# -- which rows a report counts (v2.1 Part 4 #1 and #15) ---------------------------------------


@dataclass(frozen=True)
class Scope:
    """Which bot rows a report counts. The default is the headline: the current model
    version, with signals inside outage windows left out."""
    model: str = "current"          # current | before | all | a model version number
    include_outages: bool = False

    @classmethod
    def parse(cls, model: str | None, include_outages: bool = False) -> Scope:
        model = (model or "current").strip().lower()
        if model not in ("current", "before", "all") and not model.isdigit():
            raise ValueError(f"model must be current, before, all or a version number, not {model!r}")
        return cls(model, include_outages)

    def wants_model(self, version: int | None) -> bool:
        if self.model == "all":
            return True
        if self.model == "before":
            return version is None
        return version == (MODEL_VERSION if self.model == "current" else int(self.model))

    @property
    def label(self) -> str:
        if self.model == "all":
            return "every model version mixed (for looking only: not a result)"
        if self.model == "before":
            return f"{BEFORE_TAGGING} (rows made before model versions were recorded)"
        n = MODEL_VERSION if self.model == "current" else int(self.model)
        return f"model {n}" + (" (current)" if n == MODEL_VERSION else "")

    @property
    def short(self) -> str:
        if self.model in ("all", "before"):
            return "all model versions" if self.model == "all" else BEFORE_TAGGING
        return f"model {MODEL_VERSION if self.model == 'current' else int(self.model)}"


@dataclass
class Scoped:
    rows: list[dict]
    other_models: dict[int | None, int] = field(default_factory=dict)   # signals of other versions left out
    in_outages: int = 0                                                 # signals left out for an outage
    windows: int = 0
    settings: dict[str, int] = field(default_factory=dict)              # settings hash -> signals counted
    commits: dict[str, int] = field(default_factory=dict)


def outage_windows(app: App) -> list[tuple[int, int]]:
    """(start, end) of every recorded outage; an open one runs to now."""
    try:
        rows = app.db.query("SELECT start_ts, end_ts FROM outage_windows ORDER BY start_ts")
    except Exception:  # noqa: BLE001 - a database older than the table: no windows known
        return []
    now = int(app.clock())
    return [(r["start_ts"], r["end_ts"] or now) for r in rows]


def in_window(ts: float | None, windows: list[tuple[int, int]]) -> bool:
    return ts is not None and any(a <= ts < b for a, b in windows)


def scope_rows(app: App, rows: list[dict], scope: Scope, *, windows: list[tuple[int, int]] | None = None) -> Scoped:
    """The bot rows ``scope`` counts, and what it left out. Rows without a runner (the
    old rule, simulated replays) are separate sources and pass through untouched."""
    windows = outage_windows(app) if windows is None else windows
    out = Scoped([], windows=len(windows))
    other: dict[int | None, int] = defaultdict(int)
    settings: dict[str, int] = defaultdict(int)
    commits: dict[str, int] = defaultdict(int)
    for r in rows:
        if "runner" not in r:
            out.rows.append(r)
            continue
        if not scope.wants_model(r.get("model_version")):
            other[r.get("model_version")] += 1
            continue
        if not scope.include_outages and in_window(r.get("signal_ts"), windows):
            out.in_outages += 1
            continue
        out.rows.append(r)
        if r.get("model_version") is not None:
            settings[r.get("settings_hash") or "unknown"] += 1
            commits[r.get("code_commit") or "unknown"] += 1
    out.other_models, out.settings, out.commits = dict(other), dict(settings), dict(commits)
    return out


def scope_lines(sc: Scoped, scope: Scope) -> list[str]:
    lines = [f"  counting {scope.label}" + (" | code commits: " + ", ".join(
        f"{c} ({n})" for c, n in sorted(sc.commits.items(), key=lambda kv: -kv[1])) if sc.commits else "")]
    if len(sc.settings) > 1:
        lines.append(f"  ⚠ these rows were made under {len(sc.settings)} different settings (config.yaml changed): "
                     + ", ".join(f"{h} ({n})" for h, n in sorted(sc.settings.items(), key=lambda kv: -kv[1]))
                     + ". They are pooled here; a change that alters decisions should also change the model version.")
    if sc.other_models:
        kept = ", ".join(f"{n} signals from {model_label(v)}" for v, n in sorted(
            sc.other_models.items(), key=lambda kv: (kv[0] is None, -(kv[0] or 0))))
        lines.append(f"  kept apart, never added in: {kept} (`stats --model before` or a version number "
                     "shows them)")
    if sc.in_outages:
        lines.append(f"  left out: {sc.in_outages} signals inside {sc.windows} outage window(s) "
                     "(`stats --include-outages` counts them)")
    return lines


# -- the decision rule (v2 Phase 5) -------------------------------------------------------------


@dataclass
class DecisionRule:
    """The owner's rule, set before the results were in: "Copying has an edge if mean CLV
    stays above 0 and the 95% CI for ROI excludes 0 after 100 settled events." Judged on
    the headline (single / flat_hold), one model version, outages left out. An event is
    one game or question: several fills in it count once."""
    events: int
    need: int
    clv: float | None = None
    clv_n: int = 0
    mean: float | None = None
    lo: float | None = None
    hi: float | None = None

    @property
    def text(self) -> str:
        return (f"Copying has an edge if mean CLV stays above 0 and the 95% CI for ROI excludes 0 after "
                f"{self.need} settled events.")

    @property
    def enough(self) -> bool:
        return self.events >= self.need

    # Float noise is not an edge: +0.05 and -0.05 average to 5e-17, which is not "above 0".
    TOLERANCE = 1e-9

    @property
    def clv_above(self) -> bool:
        return self.clv is not None and self.clv > self.TOLERANCE

    @property
    def ci_above(self) -> bool:
        return self.lo is not None and self.lo > self.TOLERANCE

    @property
    def ci_below(self) -> bool:
        return self.hi is not None and self.hi < -self.TOLERANCE

    @property
    def met(self) -> bool:
        return self.enough and self.clv_above and self.ci_above

    @property
    def verdict(self) -> str:
        if not self.enough:
            return f"not decided yet: {self.events} of {self.need} settled events"
        if self.met:
            return f"copying has an edge: both tests pass after {self.events} settled events"
        if self.ci_below:
            return f"copying loses money: the whole interval is below 0 after {self.events} settled events"
        failed = [what for ok, what in ((self.clv_above, "mean CLV is not above 0"),
                                         (self.ci_above, "the interval includes 0")) if not ok]
        return f"no edge shown after {self.events} settled events: " + " and ".join(failed)


def decision_rule(s: Summary, cfg) -> DecisionRule:
    return DecisionRule(events=s.events, need=int(cfg.stats.decision_min_events), clv=s.avg_clv, clv_n=s.clv_n,
                        mean=s.mean_roi, lo=s.ci_low, hi=s.ci_high)


def decision_line(rule: DecisionRule) -> str:
    def yes(ok: bool) -> str:
        return "yes" if ok else "no"
    clv = "-" if rule.clv is None else f"{rule.clv:+.3f}"
    ci = "-" if rule.lo is None else f"[{_pct(rule.lo)}, {_pct(rule.hi)}]"
    return (f"  decision rule: {rule.events} of {rule.need} settled events | mean CLV {clv} (n={rule.clv_n} "
            f"pre-game fills), above 0: {yes(rule.clv_above)} | 95% CI for mean ROI {ci}, above 0: "
            f"{yes(rule.ci_above)} -> {rule.verdict}")


# -- after the fill (Part 4 #5) and the book a second later (Part 4 #6) ---------------------------


def markout_columns(cfg) -> tuple[tuple[str, str], ...]:
    return (("1 min", "markout_1m"), (f"{cfg.bot.markout_seconds // 60} min", "markout_price"),
            ("30 min", "markout_30m"))


def markouts(rows: list[dict], column: str) -> list[float]:
    """Price ``column`` minus our fill price, per share, for every fill that has one."""
    return [t[column] - t["fill_price"] for t in rows if t["status"] == "FILLED" and t.get(column) is not None
            and t.get("fill_price") is not None]


def markout_groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    filled = [t for t in rows if t["status"] == "FILLED"]
    return [("every fill", filled), ("pre-game", [t for t in filled if not in_play(t)]),
            ("in-play", [t for t in filled if in_play(t)])]


def markout_text(rows: list[dict], cfg) -> str:
    def cents(x: float | None) -> str:
        return "-" if x is None else f"{x:+.3f}"
    return ", ".join(f"{name} {cents(mean(markouts(rows, col)))} (n={len(markouts(rows, col))})"
                     for name, col in markout_columns(cfg))


@dataclass
class Recheck:
    """The order book read a second time, ``recheck_seconds`` after the decision."""
    n: int = 0                 # signals read twice
    moved: int = 0             # best ask different on the second read
    up: int = 0
    mean_move: float | None = None
    fills: int = 0             # our fills among them
    lost: int = 0              # fills that would have missed on the second book
    gained: int = 0            # misses that would have filled on it
    extra: float | None = None # what a fill still possible would have paid more, per share
    extra_n: int = 0
    lag: float | None = None   # median seconds from decision to the second read
    fetch: float | None = None  # median seconds the first read of market and book took
    fetch_p90: float | None = None


def recheck_report(rows: list[dict]) -> Recheck:
    done = [t for t in rows if t.get("recheck_status") in ("FILLED", "MISSED")]
    out = Recheck(n=len(done))
    both = [t for t in done if t.get("recheck_best_ask") is not None and t.get("best_ask") is not None]
    moves = [t["recheck_best_ask"] - t["best_ask"] for t in both]
    out.moved = sum(1 for m in moves if abs(m) > 1e-9)
    out.up = sum(1 for m in moves if m > 1e-9)
    out.mean_move = mean(moves)
    filled = [t for t in done if t["status"] == "FILLED"]
    out.fills = len(filled)
    out.lost = sum(1 for t in filled if t["recheck_status"] == "MISSED")
    out.gained = sum(1 for t in done if t["status"] == "MISSED" and t["recheck_status"] == "FILLED")
    extra = [t["recheck_fill_price"] - t["fill_price"] for t in filled
             if t["recheck_status"] == "FILLED" and t.get("recheck_fill_price") is not None]
    out.extra, out.extra_n = mean(extra), len(extra)
    lags = [t["recheck_at"] - t["decided_at"] for t in done if t.get("recheck_at") is not None]
    out.lag = median(lags) if lags else None
    fetch = [t["book_fetch_seconds"] for t in rows if t.get("book_fetch_seconds") is not None]
    out.fetch = median(fetch) if fetch else None
    out.fetch_p90 = _quantile(fetch, 0.9) if fetch else None
    return out


def recheck_lines(rows: list[dict]) -> list[str]:
    r = recheck_report(rows)
    if not r.n:
        return []

    def cents(x: float | None) -> str:
        return "-" if x is None else f"{x:+.3f}"
    fetch = (f" | reading the market and book took median {r.fetch:.2f}s, p90 {r.fetch_p90:.2f}s"
             if r.fetch is not None else "")
    return [f"  order book read again {r.lag:.1f}s after our decision (n={r.n}): best ask moved on {r.moved} "
            f"({r.up} up, {r.moved - r.up} down), mean change {cents(r.mean_move)} a share{fetch}",
            f"  on the second book {r.lost} of {r.fills} fills would have missed and {r.gained} misses would have "
            f"filled; fills still possible would have paid {cents(r.extra)} a share more (n={r.extra_n})"]


def markout_wallet_lines(rows: list[dict], cfg, top: int = 10) -> list[str]:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        if t["status"] == "FILLED" and t.get("wallet"):
            by[t["wallet"]].append(t)
    if not by:
        return []
    ranked = sorted(by.items(), key=lambda kv: -len(kv[1]))[:top]
    lines = [f"  price after our fill by wallet (the {len(ranked)} with most fills, per share):"]
    for w, ts in ranked:
        lines.append(f"    {w[:6]}…{w[-4:]}: {len(ts)} fills | {markout_text(ts, cfg)}")
    return lines


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


NO_MARKET = "market metadata unavailable"       # bot.decide_buy's reason when Gamma has no market
LOST_WINDOW = 7 * DAY


@dataclass
class SettleCheck:
    """Can every signal be traded, and every trade settle?

    Polymarket's new market format (Protocol V2) starts on 2026-11-23, and most sports
    move to it on 2026-12-01. Two failures would be silent without this: a fill whose
    token its market does not list never settles (it stays open, holds a wallet slot
    and never reaches the decision rule), and a market Gamma cannot find turns its
    signals into misses."""
    v2_markets: int = 0
    v2_signals: int = 0          # headline signals on v2 markets
    v2_filled: int = 0
    stuck: list[dict] = field(default_factory=list)    # open fills on a resolved market that does not list the token
    lost: int = 0                # headline signals missed for lack of market data, last 7 days
    signals: int = 0             # all headline signals, last 7 days

    @property
    def ok(self) -> bool:
        return not self.stuck and not self.lost


def settle_check(app: Any, now: int | None = None) -> SettleCheck:
    from .markets import market_from_row
    now = int(app.clock()) if now is None else now
    out = SettleCheck()
    try:
        v2 = app.db.one("SELECT COUNT(*) n FROM poly_markets WHERE version = 'v2'")
    except Exception:  # noqa: BLE001 - a database from before schema 12 has no version column
        return out
    out.v2_markets = v2["n"]
    head = "b.runner = 'single' AND b.strategy = ?"
    row = app.db.one(f"SELECT COUNT(*) n, SUM(b.status = 'FILLED') f FROM bot_trades b JOIN poly_markets m "
                     f"ON m.condition_id = b.condition_id WHERE {head} AND m.version = 'v2'", (BOT_HEADLINE,))
    out.v2_signals, out.v2_filled = row["n"], row["f"] or 0
    row = app.db.one(f"SELECT COUNT(*) n, SUM(b.reason = ?) lost FROM bot_trades b WHERE {head} AND b.signal_ts >= ?",
                     (NO_MARKET, BOT_HEADLINE, now - LOST_WINDOW))
    out.signals, out.lost = row["n"], row["lost"] or 0
    # The US runner settles from the US venue's own results, not by token: left out.
    open_fills = app.db.query("SELECT b.id, b.runner, b.strategy, b.token_id, b.condition_id FROM bot_trades b "
                              "JOIN poly_markets m ON m.condition_id = b.condition_id WHERE b.status = 'FILLED' "
                              "AND b.closed_at IS NULL AND b.runner != ? AND m.resolved = 1 AND m.payouts IS NOT NULL",
                              (US_RUNNER,))
    if open_fills:
        ids = sorted({r["condition_id"] for r in open_fills})
        marks = ",".join("?" * len(ids))
        markets = {r["condition_id"]: market_from_row(r)
                   for r in app.db.query(f"SELECT * FROM poly_markets WHERE condition_id IN ({marks})", ids)}
        out.stuck = [dict(r, slug=markets[r["condition_id"]].slug) for r in open_fills
                     if markets[r["condition_id"]].outcome_index_of(r["token_id"]) is None]
    return out


def settle_check_lines(app: Any, *, brief: bool = False) -> list[str]:
    c = settle_check(app)
    if brief and c.ok:
        return []
    out = [(f"CAN EVERY TRADE SETTLE? (Polymarket's new market format starts 2026-11-23, most sports 2026-12-01) "
            f"new-format markets seen: {c.v2_markets}, headline signals on them: {c.v2_signals} ({c.v2_filled} filled) "
            f"| open fills that cannot settle: {len(c.stuck)} | signals missed for lack of market data, last 7 days: "
            f"{c.lost} of {c.signals}")]
    if c.stuck:
        sample = ", ".join(f"#{r['id']} {r['runner']}/{r['strategy']} on {r['slug'] or r['condition_id'][:12]}"
                           for r in c.stuck[:3])
        out.append(f"  ! {len(c.stuck)} open fills cannot settle: their market has resolved but does not list "
                   f"their token, so they will stay open. A bug to fix. First: {sample}")
    if c.lost:
        out.append(f"  ! {c.lost} signals in the last 7 days were missed because Gamma had no data for their "
                   "market. More than a stray one or two means the market lookup is failing.")
    return out


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


def exit_comparison(by: dict[tuple[str, str], list[dict]], runner: str, samples: int) -> list[str]:
    """Part 4 #12: holding against mirroring, on only the signals where the whale sold
    before the market resolved (where the two can differ)."""
    mirror = by.get((runner, "flat_mirror"), [])
    exited = {t["alert_id"] for t in mirror if t.get("result") == "EXIT"}
    if not mirror:
        return []
    if not exited:
        return [f"  {runner}: the whale has not sold before the market resolved on any copied signal yet, so holding "
                "and mirroring cannot differ"]
    hold = [t for t in by.get((runner, BOT_HEADLINE), []) if t["alert_id"] in exited]
    mir = [t for t in mirror if t["alert_id"] in exited]
    n, m, lo, hi = _paired(hold, mir, samples)
    hold_pnl = sum(t["pnl"] or 0.0 for t in hold if t["status"] == "FILLED" and t["closed_at"] is not None)
    mir_pnl = sum(t["pnl"] or 0.0 for t in mir if t["status"] == "FILLED" and t["closed_at"] is not None)
    ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
    return [f"  {runner}: on the {len(exited)} signals where the whale sold before resolution: hold "
            f"{_money(hold_pnl)} | mirror (sell with it) {_money(mir_pnl)} | hold minus mirror, ROI per trade on the "
            f"{n} both closed: {_pct(m)}, 95% CI {ci}"]


def limit_comparison(app: App, by: dict[tuple[str, str], list[dict]], runner: str, samples: int) -> list[str]:
    """The owner's question 8: does chasing the price up to 3 cents above the whale beat
    stopping at 1? Same signals, same stake, a miss counted as $0, both settled."""
    loose, tight = by.get((runner, BOT_HEADLINE), []), by.get((runner, "tight_hold"), [])
    if not tight:
        return []
    done = {t["alert_id"]: t for t in tight if t["status"] in ("FILLED", "MISSED") and t["closed_at"] is not None}
    pairs = [(t, done[t["alert_id"]]) for t in loose if t["alert_id"] in done
             and t["status"] in ("FILLED", "MISSED") and t["closed_at"] is not None]
    a, b = app.cfg.bot.max_slippage * 100, (app.cfg.bot.tight_hold.max_slippage or 0) * 100
    if not pairs:
        return [f"  {runner}: chase limit {a:.0f}c against {b:.0f}c: no signal both have settled yet"]
    diffs = [(x["pnl"] or 0.0) - (y["pnl"] or 0.0) for x, y in pairs]
    m, lo, hi = two_way_bootstrap_mean_ci(diffs, [x["wallet"] for x, _ in pairs], [event_key(x) for x, _ in pairs],
                                          samples=samples)
    ci = "-" if lo is None else f"[{_money(lo)}, {_money(hi)}]"
    return [f"  {runner}: chase limit on the same {len(pairs)} settled signals (a miss is $0): up to {a:.0f}c "
            f"{_money(sum(x['pnl'] or 0.0 for x, _ in pairs))} ({sum(x['status'] == 'FILLED' for x, _ in pairs)} "
            f"filled) | up to {b:.0f}c {_money(sum(y['pnl'] or 0.0 for _, y in pairs))} "
            f"({sum(y['status'] == 'FILLED' for _, y in pairs)} filled) | {a:.0f}c minus {b:.0f}c per signal "
            f"{_money(m)}, 95% CI {ci}"]


def settlement_lines(rows: list[dict], samples: int) -> list[str]:
    """Part 4 #9 and #13: how long the payout took to be recorded, and P&L by how the
    market resolved."""
    from .horizon import RESOLUTION_TYPES
    closed = [t for t in rows if "runner" in t and t["status"] == "FILLED" and t.get("closed_at") is not None
              and t.get("result") != "EXIT"]
    if not closed:
        return []
    lags = [t["paid_at"] - t["resolved_at"] for t in closed if t.get("paid_at") and t.get("resolved_at")]
    lines = []
    if lags:
        lines.append(f"  payout recorded after the market resolved: median {_secs(median(lags))}, p90 "
                     f"{_secs(_quantile(lags, 0.9))} (n={len(lags)}). The wallet counts the money back at the payout.")
    head = [t for t in closed if t["strategy"] == BOT_HEADLINE and t["runner"] != US_RUNNER]
    typed = [t for t in head if t.get("resolution_type")]
    if typed:
        parts = []
        for kind in RESOLUTION_TYPES:
            group = [t for t in typed if t["resolution_type"] == kind]
            if group:
                s = summarise(kind, group, samples=samples)
                parts.append(f"{kind} n={s.settled}, net {_money(s.total_pnl)}")
        untyped = len(head) - len(typed)
        lines.append("  how the markets resolved (flat_hold): " + " | ".join(parts)
                     + (f" | not typed yet {untyped}" if untyped else ""))
    return lines


def _bot_strategy_lines(app: App, runner: str, strategy: str, rows: list[dict], threshold: int, samples: int,
                        *, brief: bool, group: str | None = None) -> list[str]:
    from .bot import Bot
    traded = [t for t in rows if t["status"] != "SKIPPED"]
    skipped = [t for t in rows if t["status"] == "SKIPPED"]
    s = summarise(f"{runner} / {strategy}", traded, samples=samples)
    acct = Bot(app, runner).account(strategy)
    if brief and strategy != BOT_HEADLINE:
        head = _brief_summary(s, threshold)
        if group is None:
            head[0] += f" | bankroll ${acct.equity:,.2f}"
        return head
    tight = app.cfg.bot.tight_hold.max_slippage
    note = {"flat_hold": "bot fill at detection, held to resolution; the headline",
            "tight_hold": f"the same, but it gives up {tight * 100:.0f}c above the whale's price instead of "
                          f"{app.cfg.bot.max_slippage * 100:.0f}c" if tight is not None else "a second chase limit",
            "flat_mirror": "bot fill, sold when the whale sells",
            "scaled_hold": "stake grows with the whale's size, capped",
            "placebo_late": "the control: same outcome at a random later time"}.get(strategy, "")
    if runner == US_RUNNER:
        note = "the same signals on the Polymarket US book, at US fees: row A"
    lines = [f"== {runner} / {strategy} ({note})" + (f", {group}" if group else "") + " =="]
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
        lines.append("  skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items())))
    if runner != US_RUNNER and group is None:   # row A prices every signal: it has no bankroll and no limits
        lines.append(f"  paper bankroll ${acct.equity:,.2f} (started ${acct.starting:,.2f}) | open at cost "
                     f"${acct.open_cost:,.2f} | free ${acct.cash:,.2f} | closed since 00:00 UTC {_money(acct.today)} "
                     "(the bot's operating account: every model version, as its risk limits see it)")
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
    if runner == "single" and strategy == BOT_HEADLINE and group is None:
        lines.append(decision_line(decision_rule(s, app.cfg)))
    if brief:
        return lines

    def cents(x: float | None) -> str:
        return "-" if x is None else f"{x:+.3f}"

    marks = [t["markout_price"] - t["fill_price"] for t in traded
             if t["status"] == "FILLED" and t["markout_price"] is not None]
    inplay = f", {s.clv_inplay_excluded} in-play fills have no closing line" if s.clv_inplay_excluded else ""
    if runner == US_RUNNER:
        lines.append("  CLV: not measured on Polymarket US (its price history is display prices, not trades)")
    else:
        lines.append(f"  CLV: closing line {cents(s.avg_clv)} (n={s.clv_n}{inplay}) | price "
                     f"{app.cfg.bot.markout_seconds // 60} min after our fill {cents(mean(marks))} "
                     f"(n={len(marks)}, every fill)")
        if strategy == BOT_HEADLINE:
            for name, group in markout_groups(traded)[1:]:
                if group:
                    lines.append(f"  price after our fill, {name} ({len(group)} fills): {markout_text(group, app.cfg)}")
            lines += recheck_lines(rows)
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


def bot_lines(app: App, *, threshold: int, samples: int, brief: bool, with_breakdowns: bool,
              scope: Scope = Scope(), everything: list[dict] | None = None) -> list[str]:
    from .config import BOT_STRATEGIES
    everything = load_bot_trades(app) if everything is None else everything
    if not everything:
        return []
    windows = outage_windows(app)
    sc = scope_rows(app, everything, scope, windows=windows)
    trades = sc.rows
    lines = ["BOT PAPER TRADES: a bot that buys the moment a whale fill is detected (paper only)"]
    lines += scope_lines(sc, scope)
    if not trades:
        lines.append(f"  no signals from {scope.label} yet.")
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by[(t["runner"], t["strategy"])].append(t)
    order = {name: i for i, name in enumerate(BOT_STRATEGIES)}
    # The US venue (row A) comes last: it is read against the international runner.
    runners = sorted({r for r, _ in by}, key=lambda r: (r == US_RUNNER, r))
    for runner in runners:
        for strategy in sorted({s for r, s in by if r == runner}, key=lambda s: order.get(s, 99)):
            lines += _bot_strategy_lines(app, runner, strategy, by[(runner, strategy)], threshold, samples,
                                         brief=brief)
        if runner == US_RUNNER:
            lines += us_lines(by.get((US_RUNNER, BOT_HEADLINE), []), by.get(("single", BOT_HEADLINE), []), samples)
            continue
        n, m, lo, hi = _paired(by.get((runner, BOT_HEADLINE), []), by.get((runner, "placebo_late"), []), samples)
        if n:
            ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
            verdict = ("copying beats the control" if lo is not None and lo > 0 else
                       "the control beats copying" if hi is not None and hi < 0 else
                       "no difference shown: the whale's timing has not been shown to matter")
            lines.append(f"  {runner}: flat_hold minus placebo_late, ROI per trade on the {n} signals both filled: "
                         f"{_pct(m)}, 95% CI {ci} -> {verdict}")
        lines += cutoff_lines(app, runner, by.get((runner, BOT_HEADLINE), []))
        lines += exit_comparison(by, runner, samples)
        lines += limit_comparison(app, by, runner, samples)
        if not brief:
            lines += markout_wallet_lines(by.get((runner, BOT_HEADLINE), []), app.cfg)
            lines += settlement_lines([t for t in trades if t.get("runner") == runner], samples)
    lines += wallet_lines(app, trades, brief=brief)
    if not brief:
        lines += followup_lines(app, trades, samples)
    lines += detection_lines(app)
    lines += settle_check_lines(app)
    if with_breakdowns:
        headline = [t for t in trades if t["strategy"] == BOT_HEADLINE and t["status"] != "SKIPPED"
                    and t["runner"] != US_RUNNER]
        if headline:
            lines += _breakdown_lines(headline, f" ({BOT_HEADLINE})",
                                      ("watchlist status", "month", "category", "days to close when entered",
                                       "holding time", "entry price", "wallet"))
    if scope.model == "current":
        lines += _other_model_lines(app, everything, sc, scope, threshold, samples, windows)
    return lines


def followup_lines(app: App, rows: list[dict], samples: int) -> list[str]:
    """Part 4 #10, #11: the whale's other-side buys and quick sales around each alert,
    and how the copies did by class. ``rows``: the bot rows in scope."""
    from . import followup as fu
    cfg = app.cfg.followup
    alerts = app.db.query("SELECT whale_usd, opposite_before_usd, opposite_after_usd, related_usd, "
                          "sold_back_share, followed_at FROM alerts WHERE followed_at IS NOT NULL")
    if not alerts:
        return []
    classes: dict[str, int] = defaultdict(int)
    known_early = related = sold = 0
    for a in alerts:
        d = dict(a)
        cls = fu.hedge_class(d, cfg.hedge_ratio)
        classes[cls] += 1
        known_early += cls == fu.HEDGED and (d["opposite_before_usd"] or 0) >= cfg.hedge_ratio * d["whale_usd"]
        related += (d["related_usd"] or 0) > 0
        sold += (d["sold_back_share"] or 0) >= cfg.sold_back_fraction
    lines = ["", "WHALE FOLLOW-UP: what the whale did around each alert (from the fills the detector stored)",
             f"  {len(alerts)} alerts followed up: clean {classes[fu.CLEAN]} | partial {classes[fu.PARTIAL]} "
             f"(bought some of the other side) | hedged {classes[fu.HEDGED]} (other side at least "
             f"{cfg.hedge_ratio:.0%} of the alert) | bought another market of the same event on {related}",
             f"  known when we acted: on {known_early} of the {classes[fu.HEDGED]} hedged alerts the other-side buy "
             "came before the alert. A live rule can only see those; the rest came in the hour after.",
             f"  sold back {cfg.sold_back_fraction:.0%} or more within 30 minutes: {sold} alerts"]
    head = [t for t in rows if "runner" in t and t["runner"] == "single" and t["strategy"] == BOT_HEADLINE
            and t["status"] != "SKIPPED"]
    parts = []
    for cls in (fu.CLEAN, fu.PARTIAL, fu.HEDGED):
        group = [t for t in head if fu.hedge_class(t, cfg.hedge_ratio) == cls]
        s = summarise(cls, group, samples=samples)
        parts.append(f"{cls} n={s.settled} closed, net {_money(s.total_pnl)}, mean ROI {_pct(s.mean_roi)} "
                     f"95% CI {_ci_text(s)}" if s.settled else f"{cls} n=0 closed")
    lines.append(f"  single / {BOT_HEADLINE} by class: " + " | ".join(parts))
    wallets = fu.by_wallet(app.db, cfg)
    both = sorted((w for w in wallets.values() if w.both_sides_flag(cfg)), key=lambda w: -w.both_sides / w.alerts)
    back = sorted((w for w in wallets.values() if w.sold_back_flag(cfg)), key=lambda w: -w.sold_back / w.alerts)
    lines.append(f"  wallets that usually buy both sides ({cfg.min_alerts}+ alerts, {cfg.both_sides_share:.0%} or "
                 "more): " + (", ".join(f"{w.wallet[:6]}…{w.wallet[-4:]} ({w.both_sides} of {w.alerts})"
                                       for w in both) or "none"))
    lines.append(f"  wallets that often sell back ({cfg.min_alerts}+ alerts, {cfg.sold_back_alert_share:.0%} or "
                 "more): " + (", ".join(f"{w.wallet[:6]}…{w.wallet[-4:]} ({w.sold_back} of {w.alerts})"
                                       for w in back) or "none"))
    on = [n for n in ("skip_hedged_alerts", "skip_both_sides_wallets", "skip_sold_back_wallets") if getattr(cfg, n)]
    lines.append("  switches: " + (", ".join(on) + " ON" if on else
                                   "all off (followup.skip_* in config.yaml): nothing is skipped for these yet"))
    return lines


def _other_model_lines(app: App, everything: list[dict], sc: Scoped, scope: Scope, threshold: int, samples: int,
                       windows: list[tuple[int, int]]) -> list[str]:
    """Each other model version's headline, briefly, as its own group."""
    lines: list[str] = []
    for version in sorted(sc.other_models, key=lambda v: (v is None, -(v or 0))):
        group = model_label(version)
        rows = scope_rows(app, everything, Scope("before" if version is None else str(version),
                                                 scope.include_outages), windows=windows).rows
        rows = [t for t in rows if "runner" in t and t["strategy"] == BOT_HEADLINE]
        if not rows:
            continue
        lines += ["", f"BOT PAPER TRADES, {group.upper()}: kept apart, never added to the headline above"]
        for runner in sorted({t["runner"] for t in rows}, key=lambda r: (r == US_RUNNER, r)):
            lines += _bot_strategy_lines(app, runner, BOT_HEADLINE, [t for t in rows if t["runner"] == runner],
                                         threshold, samples, brief=True, group=group)
    return lines


@dataclass
class UsComparison:
    """Row A beside the baseline, on the same signals."""
    signals: int = 0
    on_us: int = 0                       # the same bet exists there and the sides lined up
    reasons: dict[str, int] = field(default_factory=dict)
    gaps: list[float] = field(default_factory=list)       # US best ask minus international best ask
    both_filled: int = 0
    fee_us: float = 0.0                  # on the signals both filled
    fee_intl: float = 0.0
    paired: tuple[int, float | None, float | None, float | None] = (0, None, None, None)
    no_sports: Summary | None = None
    fill_gaps: list[tuple[int, float]] = field(default_factory=list)   # (signal time, US fill - intl fill)
    side: dict[str, dict[str, float | None]] = field(default_factory=dict)   # "intl" / "us" -> rates and averages


def venue_figures(rows: list[dict], spread: Callable[[dict], float | None]) -> dict[str, float | None]:
    """Missed rate, average spread when we looked, and average slippage (fill minus best
    ask, a share) over the signals a venue tried to fill."""
    tried = [r for r in rows if r["status"] in ("FILLED", "MISSED")]
    filled = [r for r in tried if r["status"] == "FILLED"]
    spreads = [x for x in (spread(r) for r in tried) if x is not None]
    slips = [r["fill_price"] - r["best_ask"] for r in filled
             if r.get("fill_price") is not None and r.get("best_ask") is not None]
    return {"tried": len(tried), "missed_rate": (len(tried) - len(filled)) / len(tried) if tried else None,
            "spread": mean(spreads) if spreads else None, "slippage": mean(slips) if slips else None}


def us_comparison(us_rows: list[dict], base_rows: list[dict], samples: int) -> UsComparison:
    c = UsComparison(signals=len(us_rows))
    base = {t["alert_id"]: t for t in base_rows}
    for t in us_rows:
        if t.get("us_slug"):
            c.on_us += 1
        code = t.get("skip_code")
        if code:
            why = (t.get("reason") or code).split(":", 1)[-1].strip() if code == "not_on_us" else ""
            if "its sides do not line up" in why:
                why = "the market exists but its sides do not line up"
            key = f"{code}: {why}" if why else code
            c.reasons[key] = c.reasons.get(key, 0) + 1
        if t.get("price_gap") is not None:
            c.gaps.append(t["price_gap"])
        other = base.get(t["alert_id"])
        if t["status"] == "FILLED" and other is not None and other["status"] == "FILLED":
            c.both_filled += 1
            c.fee_us += t.get("fees") or 0.0
            c.fee_intl += other.get("fees") or 0.0
    c.paired = _paired(us_rows, base_rows, samples)
    for t in us_rows:
        other = base.get(t["alert_id"])
        # What each venue's walk paid, or would have paid when the price ran past the limit.
        if t.get("fill_price") is not None and other is not None and other.get("fill_price") is not None:
            c.fill_gaps.append((int(t["alert_ts"]), t["fill_price"] - other["fill_price"]))
    priced = {t["alert_id"] for t in us_rows}
    c.side = {
        "intl": venue_figures([b for b in base_rows if b["alert_id"] in priced],
                              lambda r: (r["alert_best_ask"] - r["alert_best_bid"])
                              if r.get("alert_best_ask") is not None and r.get("alert_best_bid") is not None else None),
        "us": venue_figures(us_rows, lambda r: (r["best_ask"] - r["us_best_bid"])
                            if r.get("best_ask") is not None and r.get("us_best_bid") is not None else None),
    }
    c.side["us"]["not_on_us_rate"] = (sum(1 for t in us_rows if t.get("skip_code") == "not_on_us") / len(us_rows)
                                      if us_rows else None)
    rest = [t for t in us_rows if t["status"] != "SKIPPED" and (t.get("category") or "") != "sports"]
    c.no_sports = summarise("us, sports excluded", rest, samples=samples) if rest else None
    return c


def us_lines(us_rows: list[dict], base_rows: list[dict], samples: int) -> list[str]:
    """v2.1 Part 3b: how the same signals fare on Polymarket US, against the baseline."""
    if not us_rows:
        return []
    c = us_comparison(us_rows, base_rows, samples)
    share = c.on_us / c.signals if c.signals else 0.0
    lines = [f"  {US_RUNNER}: the same bet exists on Polymarket US for {c.on_us} of {c.signals} signals ({share:.0%})"]
    if c.reasons:
        lines.append("    not priced there: " + ", ".join(
            f"{k} {v}" for k, v in sorted(c.reasons.items(), key=lambda kv: -kv[1])))
    if c.gaps:
        dearer = sum(1 for g in c.gaps if g > 1e-9)
        lines.append(f"    US best ask minus international best ask, n={len(c.gaps)}: mean "
                     f"{(round(mean(c.gaps), 3) or 0.0):+.3f}, median {(round(median(c.gaps), 3) or 0.0):+.3f}; "
                     f"the US price was higher on {dearer} ({dearer / len(c.gaps):.0%})")
    if c.fill_gaps:
        gaps = [g for _, g in c.fill_gaps]
        lines.append(f"    US fill minus international fill (or what each would have paid), n={len(gaps)}: "
                     f"mean {(round(mean(gaps), 3) or 0.0):+.3f} a share")
    if c.both_filled:
        lines.append(f"    fees on the {c.both_filled} signals both venues filled: US ${c.fee_us:,.2f}, "
                     f"international ${c.fee_intl:,.2f}")

    def fig(x: float | None, kind: str) -> str:
        return "-" if x is None else f"{x:.0%}" if kind == "rate" else f"{(round(x, 3) or 0.0):+.3f}"

    i, u = c.side.get("intl", {}), c.side.get("us", {})
    lines.append(f"    international vs US: missed {fig(i.get('missed_rate'), 'rate')} vs {fig(u.get('missed_rate'), 'rate')}"
                 f" | spread {fig(i.get('spread'), 'cents')} vs {fig(u.get('spread'), 'cents')}"
                 f" | slippage {fig(i.get('slippage'), 'cents')} vs {fig(u.get('slippage'), 'cents')} a share"
                 f" | not on US {fig(u.get('not_on_us_rate'), 'rate')}")
    n, m, lo, hi = c.paired
    if n:
        ci = "-" if lo is None else f"[{_pct(lo)}, {_pct(hi)}]"
        lines.append(f"    US minus international, ROI per trade on the {n} signals both filled and closed: "
                     f"{_pct(m)}, 95% CI {ci}")
    else:
        lines.append("    no signal has been filled and closed on both venues yet, so no comparison of returns")
    s = c.no_sports
    if s is not None and s.settled:
        lines.append(f"    sports excluded: net {_money(s.total_pnl)} on ${s.total_cost:,.2f}, n={s.settled} closed, "
                     f"mean ROI {_pct(s.mean_roi)}, 95% CI {_ci_text(s)}")
    else:
        lines.append(f"    sports excluded: {0 if s is None else s.filled} fills, none closed"
                     " (almost everything that exists on both venues is sports)")
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


def render(app: App, *, with_breakdowns: bool = True, brief_backtest: bool = False, scope: Scope = Scope()) -> str:
    trades = load_trades(app)
    threshold = app.cfg.stats.min_settled_for_conclusions
    samples = app.cfg.stats.bootstrap_samples
    live = [t for t in trades if not is_backtest(t)]
    simulated = [t for t in trades if is_backtest(t)]
    lines: list[str] = []
    everything = load_bot_trades(app)
    bot = bot_lines(app, threshold=threshold, samples=samples, brief=brief_backtest, with_breakdowns=with_breakdowns,
                    scope=scope, everything=everything)
    if bot:
        counted = scope_rows(app, everything, scope).rows
        headline = sum(1 for t in counted if t["strategy"] == BOT_HEADLINE and t["status"] == "FILLED"
                       and t["closed_at"] is not None and t["runner"] != US_RUNNER)
        label = f"closed bot paper trades (flat_hold, {scope.short})"
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
    if not brief_backtest:
        from . import experiments
        try:
            lines += experiments.lines(app, scope_rows(app, everything, scope).rows)
        except Exception as exc:  # noqa: BLE001 - a report must not fail on its newest section
            lines.append(f"  (the experiments list could not be worked out: {type(exc).__name__}: {exc})")
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


SUMMARY_WINDOW = DAY       # the daily summary's "last 24 hours" column (see daily_summary)
# A guard, not a target. It was 1,900 while the summary was posted as a chat message; it now
# goes to the log and `stats --brief`, and two runners plus a nightly review came to 3,500 on real data.
SUMMARY_LIMIT = 6000


def _period(rows: list[dict], since: float | None, samples: int) -> dict[str, Any]:
    """Signals made since ``since``, and trades CLOSED since then (None: all of them)."""
    from .costs import breakdown
    signals = [t for t in rows if since is None or (t["signal_ts"] or 0) >= since]
    closed = [t for t in rows if t["status"] == "FILLED" and t["closed_at"] is not None
              and (since is None or t["closed_at"] >= since)]
    c = breakdown(closed)
    return {"filled": sum(t["status"] == "FILLED" for t in signals),
            "missed": sum(t["status"] == "MISSED" for t in signals),
            "skipped": sum(t["status"] == "SKIPPED" for t in signals),
            "s": summarise("period", closed, samples=samples), "fees": c.fees, "slippage": c.slippage}


def _side_by_side(day: dict[str, Any], ever: dict[str, Any]) -> list[str]:
    """The last 24 hours and all-time, each with its n and interval (no profit figure without them)."""
    def cells(p: dict[str, Any]) -> list[str]:
        s = p["s"]
        return [f"{p['filled']} / {p['missed']} / {p['skipped']}", str(s.settled),
                _money(s.total_pnl) if s.settled else "-",
                f"{_pct(s.mean_roi)} {_ci_text(s)}" if s.settled else "-",
                f"{s.win_rate:.0%}" if s.win_rate is not None else "-",
                f"${p['fees']:,.2f} / ${p['slippage']:,.2f}"]
    labels = ["signals: filled / missed / skipped", "closed trades (n)", "net P&L", "mean ROI per trade, 95% CI",
              "win rate", "fees / slippage"]
    lines = [f"  {'':<36}{'last 24 hours':<26}all-time"]
    for label, a, b in zip(labels, cells(day), cells(ever)):
        lines.append(f"  {label:<36}{a:<26}{b}")
    return lines


@dataclass
class ReviewChanges:
    """What the latest nightly review changed on the watchlist (v2 Phase 5)."""
    at: int
    members: int
    added: list[tuple[str, str]]       # (wallet, why)
    removed: list[tuple[str, str]]
    failed: int
    turned_away: int


def last_review(app: App) -> ReviewChanges | None:
    """None when no review has run, or the database is from before the watchlist."""
    from . import watchlist as wl
    try:
        last = app.db.one("SELECT run_id FROM watchlist_events WHERE run_id IS NOT NULL ORDER BY id DESC LIMIT 1")
    except Exception:  # noqa: BLE001 - a database from before the watchlist
        return None
    if last is None:
        return None
    events = app.db.query("SELECT * FROM watchlist_events WHERE run_id = ? ORDER BY id", (last["run_id"],))
    members = app.db.one("SELECT COUNT(*) n FROM watchlist_membership WHERE removed_at IS NULL")["n"]
    by: dict[str, list[Any]] = defaultdict(list)
    for e in events:
        by[e["action"]].append(e)
    return ReviewChanges(at=max(e["at"] for e in events), members=members,
                         added=[(e["wallet"], (e["detail"] or "").strip()) for e in by[wl.ADD]],
                         removed=[(e["wallet"], (e["detail"] or "").strip()) for e in by[wl.REMOVE]],
                         failed=len(by[wl.FAIL]), turned_away=len(by[wl.REJECT]))


def watchlist_lines(app: App, *, show: int = 5) -> list[str]:
    """What the latest nightly review changed (v2 Phase 5), from watchlist_events."""
    try:
        app.db.one("SELECT 1 FROM watchlist_events LIMIT 1")
    except Exception:  # noqa: BLE001 - a database from before the watchlist
        return []
    r = last_review(app)
    if r is None:
        return ["watchlist: no nightly review has run yet"]
    lines = [f"watchlist, nightly review of {iso(r.at)[:16]} UTC: {r.members} members now | added {len(r.added)}, "
             f"removed {len(r.removed)}, failed a review {r.failed}, turned away {r.turned_away}"]
    for sign, items in (("+", r.added), ("-", r.removed)):
        for wallet, why in items[:show]:
            lines.append(f"  {sign} {wallet[:6]}…{wallet[-4:]}: {why[:110]}{'…' if len(why) > 110 else ''}")
        if len(items) > show:
            lines.append(f"  … and {len(items) - show} more (`watchlist --last`)")
    return lines


def daily_summary(app: App, scope: Scope = Scope()) -> str:
    """The short version: what `stats --brief` prints and what the runner writes to
    its log once a day. Per runner, the last 24 hours beside all-time (v2 Phase 5),
    each figure with its n and interval: signals, closed trades, net P&L, the costs.
    Then the decision rule, what the time-to-close cutoff skipped, the small wallet,
    and what the latest nightly review changed on the watchlist.

    The first column is the 24 hours up to the summary, not the calendar day, so that
    each trade is in exactly one daily summary whatever hour it is written. The full report is
    ``stats`` and the dashboard."""
    from . import bankroll
    everything = load_bot_trades(app)
    if not everything:        # only old-rule or simulated trades: the old short report
        text = render(app, with_breakdowns=False, brief_backtest=True, scope=scope)
        return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 10] + "\n…"
    since = app.clock() - SUMMARY_WINDOW
    windows = outage_windows(app)
    sc = scope_rows(app, everything, scope, windows=windows)
    bots = sc.rows
    threshold, samples = app.cfg.stats.min_settled_for_conclusions, min(app.cfg.stats.bootstrap_samples, 2000)
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in bots:
        by[(t["runner"], t["strategy"])].append(t)
    lines: list[str] = scope_lines(sc, scope)
    for runner in sorted({r for r, _ in by}):
        rows = by.get((runner, BOT_HEADLINE), [])
        traded = [t for t in rows if t["status"] != "SKIPPED"]
        s = summarise(runner, traded, samples=samples)
        lines.append(f"{runner} / {BOT_HEADLINE} (paper, {scope.short})")
        if s.settled < threshold:
            lines.append(f"  ⚠ only {s.settled} closed trades (need {threshold}): everything here is noise so far")
        lines += _side_by_side(_period(rows, since, samples), _period(rows, None, samples))
        if not s.settled:
            lines.append("  no closed trades yet, so no P&L")
        if runner == "single":
            lines.append(decision_line(decision_rule(s, app.cfg)))
        c = cutoff_report(app, rows)
        limit = "off" if c.limit is None else f"{c.limit:g}-day"
        lines.append(f"  open {s.open} | time-to-close cutoff ({limit}) skipped {c.too_long + c.no_end_date} "
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
    if scope.model == "current":
        for version in sorted(sc.other_models, key=lambda v: (v is None, -(v or 0))):
            rows = [t for t in scope_rows(app, everything, Scope("before" if version is None else str(version),
                                                                 scope.include_outages), windows=windows).rows
                    if "runner" in t and t["runner"] == "single" and t["strategy"] == BOT_HEADLINE]
            s = summarise("single", [t for t in rows if t["status"] != "SKIPPED"], samples=samples)
            if rows:
                lines.append(f"single / {BOT_HEADLINE}, {model_label(version)} (kept apart): net {_money(s.total_pnl)}, "
                             f"n={s.settled} closed, 95% CI {_ci_text(s)}")
    lines += watchlist_lines(app)
    lines += detection_lines(app)[:1]
    lines += settle_check_lines(app, brief=True)
    lines.append("Full report: `python -m whalewatcher stats`, or the dashboard.")
    text = "\n".join(lines)
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 10] + "\n…"
