"""v2.1 Part 4 #2: every variation this system tracks, what it is for, and a forward test
for anything other than the headline that starts to look good.

With dozens of variations, each judged at a 95% interval, a few will look good by luck
alone. So:

* Only ONE result can answer "does copying make money": the primary, ``single /
  flat_hold``, against the decision rule (at least ``bankroll.auto_min_history``
  settled signals, 100, with a 95% interval for the return per trade above zero).
* Anything else that looks good (its interval above zero) is FLAGGED the first time it
  does, with the date (``experiment_flags``, written by the runner). From then on only
  signals after that date count for it: the forward test. It counts as shown only once
  its signals after the flag meet the decision rule on their own. What it did before the
  flag is how it was found, so it cannot also be the proof.
* Simulated replays and stake-size comparisons are listed, never flagged: their data is
  fixed, so there is no "after" to test on.

Roles: primary, control (placebo_late: the same outcome at a random later time),
comparison (a fixed reference: the old rule, zero latency, the US venue), exploratory
(other strategies, which could be picked after the fact), simulated (replays).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .stats_util import two_way_bootstrap_mean_ci
from .timeutil import iso

log = logging.getLogger(__name__)

PRIMARY, CONTROL, COMPARISON, EXPLORATORY, SIMULATED = "primary", "control", "comparison", "exploratory", "simulated"
ROLE_OF = {"flat_hold": PRIMARY, "placebo_late": CONTROL, "flat_mirror": EXPLORATORY, "scaled_hold": EXPLORATORY}
SAMPLES = 2000
# Draws x rows per interval at most. A replay with 13,000 trades gets 200 draws instead of
# 2,000: with that many trades the interval barely moves between runs, and the dashboard
# page stays quick. Small samples, where the draws matter, keep all 2,000.
WORK = 2_000_000
MIN_SAMPLES = 200


@dataclass
class Experiment:
    key: str
    name: str
    role: str
    started: int | None = None      # its first signal
    n: int = 0                      # settled
    mean: float | None = None       # return per trade
    lo: float | None = None
    hi: float | None = None
    flagged_at: int | None = None   # when it first looked good (non-primary, live only)
    after_n: int = 0                # settled among signals after the flag
    after_mean: float | None = None
    after_lo: float | None = None
    after_hi: float | None = None
    status: str = ""

    @property
    def looks_good(self) -> bool:
        return self.lo is not None and self.lo > 0


def decision_n(cfg: Any) -> int:
    """The decision rule's sample size (config.py: the same bar the automatic stake uses)."""
    return int(cfg.bankroll.auto_min_history)


def _interval(rows: list[dict], roi) -> tuple[int, float | None, float | None, float | None]:
    vals, wallets, events = [], [], []
    for r in rows:
        x = roi(r)
        if x is None:
            continue
        vals.append(x)
        wallets.append(r.get("wallet") or "")
        events.append(r.get("event_slug") or r.get("event_key") or r.get("condition_id") or "")
    if not vals:
        return 0, None, None, None
    samples = max(MIN_SAMPLES, min(SAMPLES, WORK // len(vals)))
    m, lo, hi = two_way_bootstrap_mean_ci(vals, wallets, events, samples=samples)
    return len(vals), m, lo, hi


def _closed_roi(r: dict) -> float | None:
    if r["status"] != "FILLED" or r.get("closed_at") is None or not r.get("cost"):
        return None
    return (r["pnl"] or 0.0) / r["cost"]


def _column_roi(pnl: str, cost: str):
    def roi(r: dict) -> float | None:
        if r.get("closed_at") is None or r.get(pnl) is None or not r.get(cost):
            return None
        return r[pnl] / r[cost]
    return roi


def build(app: Any, rows: list[dict] | None = None) -> list[Experiment]:
    """Every live variation over the bot rows in scope (the current model version,
    outages left out), plus the old rule, the simulated replays and the stake sizes."""
    from .stats import Scope, load_bot_trades, load_trades, scope_rows
    if rows is None:
        rows = scope_rows(app, load_bot_trades(app), Scope()).rows
    flags = {r["key"]: r for r in _flags(app)}
    out: list[Experiment] = []

    def add(key: str, name: str, role: str, group: list[dict], roi, *, live: bool = True) -> None:
        e = Experiment(key, name, role, started=min((int(r["signal_ts"] if "runner" in r else r["alert_ts"])
                                                     for r in group), default=None))
        e.n, e.mean, e.lo, e.hi = _interval(group, roi)
        flag = flags.get(key)
        if flag is not None:
            e.flagged_at = flag["flagged_at"]
            after = [r for r in group if (r["signal_ts"] if "runner" in r else r["alert_ts"]) > e.flagged_at]
            e.after_n, e.after_mean, e.after_lo, e.after_hi = _interval(after, roi)
        e.status = _status(e, app.cfg, live=live)
        out.append(e)

    bot = [r for r in rows if "runner" in r]
    for runner in sorted({r["runner"] for r in bot}, key=lambda x: (x != "single", x)):
        for strategy in sorted({r["strategy"] for r in bot if r["runner"] == runner},
                               key=lambda s: list(ROLE_OF).index(s) if s in ROLE_OF else 9):
            group = [r for r in bot if r["runner"] == runner and r["strategy"] == strategy]
            role = ROLE_OF.get(strategy, EXPLORATORY) if runner == "single" else (
                COMPARISON if runner == "us" else EXPLORATORY)
            name = {"us": "Polymarket US: the same signals on the US book"}.get(runner, f"{runner} / {strategy}")
            add(f"bot:{runner}/{strategy}", name, role, group, _closed_roi)
    head = [r for r in bot if r["runner"] == "single" and r["strategy"] == "flat_hold"]
    if head:
        add("column:zero", "single / flat_hold at the whale's own price (zero latency)", COMPARISON, head,
            _column_roi("zero_pnl", "zero_cost"))
        add("column:human", "single / flat_hold a minute later (the old rule's delay)", COMPARISON, head,
            _column_roi("human_pnl", "human_cost"))
    # The old rule and the replays: from the rows given when they include them (the
    # dashboard's records do), else read here.
    paper = [r for r in rows if "runner" not in r] or load_trades(app)
    old = [t for t in paper if not str(t["strategy"]).startswith("backtest:")]
    if old:
        add("old:live", "Old rule, live (closed to new alerts)", COMPARISON, old, _paper_roi, live=False)
    sims = sorted({(t["venue"], t["strategy"]) for t in paper if str(t["strategy"]).startswith("backtest:")})
    for venue, strategy in sims:
        group = [t for t in paper if t["venue"] == venue and t["strategy"] == strategy]
        add(f"sim:{venue}/{strategy}", f"Simulated: {strategy[9:]} on {venue}", SIMULATED, group, _paper_roi,
            live=False)
    return out


def _paper_roi(t: dict) -> float | None:
    if t["status"] != "FILLED" or t.get("settled_at") is None or not t.get("cost"):
        return None
    return (t["pnl"] or 0.0) / t["cost"]


def _status(e: Experiment, cfg: Any, *, live: bool) -> str:
    need = decision_n(cfg)
    if e.role == PRIMARY:
        if e.n >= need and e.looks_good:
            return f"meets the decision rule: {e.n} settled, interval above zero"
        return f"the headline: {e.n} of {need} settled needed" + (", interval above zero so far" if e.looks_good else "")
    if not live:
        return "fixed data: listed, never tested forward"
    if e.flagged_at is None:
        return "interval includes zero or below" if e.n else "no settled trades yet"
    since = iso(e.flagged_at)[:10]
    if e.after_n >= need and e.after_lo is not None and e.after_lo > 0:
        return f"looked good on {since}; since then {e.after_n} settled with the interval above zero: shown forward"
    return f"looked good on {since}; forward test: {e.after_n} of {need} settled since then"


def _flags(app: Any) -> list[Any]:
    try:
        return app.db.query("SELECT * FROM experiment_flags")
    except Exception:  # noqa: BLE001 - a database older than the table
        return []


def flag_new(app: Any, experiments: list[Experiment] | None = None) -> list[str]:
    """Record the date each non-primary live variation first looks good. Returns notices.
    Run by the runner once a day. A flag is never moved or removed."""
    now = int(app.clock())
    out = []
    for e in experiments if experiments is not None else build(app):
        if e.role in (PRIMARY, SIMULATED) or e.key.startswith(("old:", "sim:")) or e.flagged_at is not None:
            continue
        if not e.looks_good:
            continue
        app.db.execute("INSERT OR IGNORE INTO experiment_flags (key, flagged_at, n_at_flag, mean_at_flag, lo_at_flag, "
                       "hi_at_flag) VALUES (?,?,?,?,?,?)", (e.key, now, e.n, e.mean, e.lo, e.hi))
        out.append(f"{e.name} looks good ({e.n} settled, interval above zero): only signals after "
                   f"{iso(now)[:16]} count for it from now on")
    for text in out:
        log.info("experiments: %s", text)
    return out


def luck_note(experiments: list[Experiment]) -> str:
    tested = [e for e in experiments if e.n]
    expect = 0.025 * len(tested)
    return (f"{len(experiments)} variations listed, {len(tested)} with settled trades. If none of them had any edge, "
            f"about {expect:.1f} would still show an interval above zero by luck (2.5% each), before counting the "
            "breakdown slices (category, price, hour, wallet), each of which is another chance. That is why only the "
            "primary can answer the question, and anything else must pass on later signals.")


def lines(app: Any, rows: list[dict] | None = None) -> list[str]:
    exps = build(app, rows)
    if not exps:
        return []

    def pct(x: float | None) -> str:
        return "-" if x is None else f"{x:+.1%}"
    out = ["", "EXPERIMENTS: every variation tracked, its role, and the forward test (Part 4 #2)"]
    for e in exps:
        ci = "-" if e.lo is None else f"[{pct(e.lo)}, {pct(e.hi)}]"
        since = iso(e.started)[:10] if e.started else "-"
        out.append(f"  {e.role:<11} {e.name}: since {since}, n={e.n}, mean {pct(e.mean)} 95% CI {ci} | {e.status}")
    out.append("  " + luck_note(exps))
    return out
