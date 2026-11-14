"""v2 Phase 4: the dashboard. A small web app that READS the database and shows it.

  * It reads. The database is opened read-only and query-only and every page is a
    GET. There is ONE exception (v2.1): the wallet amount form, which writes one
    row of the ``settings`` table through ``SettingsWriter``, a connection that
    SQLite itself restricts to that table. No trade, alert or run can be changed.
  * It is meant for the tailnet only. Run directly it binds 127.0.0.1. In Docker it
    listens inside its own container network and compose publishes that port on the
    host's 127.0.0.1 only; the owner exposes it with `tailscale serve`.
  * No login, no cookies, no third-party assets, no outbound requests.

Every number comes from the same code as `python -m whalewatcher stats`
(``stats.summarise``), so the dashboard and the command line cannot disagree, and
every number is shown with its n.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from . import bankroll
from . import dashview as v
from .config import Config
from .costs import breakdown
from .db import SCHEMA_VERSION, Database
from .horizon import BUCKETS as CLOSE_BUCKETS
from .horizon import UNKNOWN as NO_END
from .horizon import bucket as horizon_bucket
from .stats import (Scope, Scoped, bucket, cutoff_report, event_key, in_play, load_bot_trades, load_trades,
                    outage_windows, scope_rows, summarise, us_comparison)
from .stats_util import mean, median
from .timeutil import iso, now_ts, parse_as_of, start_of_day
from .version import MODEL_VERSION
from .version import label as model_label

log = logging.getLogger(__name__)

DAY = 86_400
SAMPLES = 2000            # bootstrap draws per interval: enough for a dashboard, light on a home server
PAGE_ROWS = 100
US_KEY = "us/flat_hold"          # row A: the runner `us` (polymarket_us.py)
BASE_KEY = "single/flat_hold"
LABELS = {
    "single/flat_hold": "Bot: flat stake, hold",
    "single/flat_mirror": "Bot: flat stake, mirror exits",
    "single/scaled_hold": "Bot: scaled stake, hold",
    "single/placebo_late": "Control: same outcome, later",
    "old/hold_to_resolution": "Old rule: human delay",
    US_KEY: "Polymarket US: same signals, US book",
}
SERIES = {"single/flat_hold": "s1", "single/flat_mirror": "s2", "single/scaled_hold": "s3",
          "single/placebo_late": "s4", "old/hold_to_resolution": "s5"}
DEFAULT_STRATEGY = "single/flat_hold"


def label_of(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    kind, _, rest = key.partition("/")
    return f"Simulated: {rest}" if kind == "sim" else key


# -- data -----------------------------------------------------------------------------------


def load_records(app: Any) -> list[dict]:
    """Every paper trade, bot and old rule and simulated, as one list with one set of names."""
    out = []
    for t in load_bot_trades(app):
        t.update(key=f"{t['runner']}/{t['strategy']}", simulated=False,
                 market_venue="polymarket_us" if t["runner"] == "us" else "polymarket")
        out.append(t)
    for t in load_trades(app):
        sim = str(t["strategy"]).startswith("backtest:")
        t.update(key=f"sim/{t['strategy'][9:]}/{t['venue']}" if sim else f"old/{t['strategy']}",
                 market_venue=t["venue"], simulated=sim, reason=t.get("miss_reason"), markout_price=None,
                 human_pnl=None, detect_delay=None, decided_at=t.get("fill_ts"), signal_ts=t.get("trigger_fill_ts"),
                 best_ask=t.get("alert_ask"), probation=t.get("probation"))
        out.append(t)
    return out


def outages(app: Any) -> list[tuple[int, int]]:
    return outage_windows(app)


# Query value -> the band of horizon.bucket() it selects.
CLOSE_FILTERS = {"all": None, "lt1": CLOSE_BUCKETS[0], "1-7": CLOSE_BUCKETS[1], "7-14": CLOSE_BUCKETS[2],
                 "gt14": CLOSE_BUCKETS[3], "none": NO_END}


@dataclass
class Filters:
    preset: str = "all"
    start: int | None = None
    end: int | None = None           # exclusive
    group: str = "day"
    strategy: str = DEFAULT_STRATEGY
    venue: str = "all"
    wallet: str = "all"
    no_outages: bool = True          # signals inside outage windows are left out unless asked (Part 4 #15)
    model: str = "current"           # which model version's bot rows count (Part 4 #1)
    close: str = "all"               # days to the scheduled close when the trade was entered
    since: str = ""
    until: str = ""

    @classmethod
    def parse(cls, q: Any, now: int, strategies: list[str]) -> Filters:
        f = cls()
        f.group = "week" if q.get("group") == "week" else "day"
        f.venue = q.get("venue") or "all"
        f.wallet = (q.get("wallet") or "all").lower()
        f.no_outages = q.get("outages") != "include"
        want_model = (q.get("model") or "current").lower()
        f.model = want_model if want_model in ("current", "before", "all") or want_model.isdigit() else "current"
        f.close = q.get("close") if q.get("close") in CLOSE_FILTERS else "all"
        want = q.get("strategy") or ""
        f.strategy = want if want in strategies else (
            DEFAULT_STRATEGY if DEFAULT_STRATEGY in strategies or not strategies else strategies[0])
        f.preset = q.get("range") if q.get("range") in ("7d", "30d", "90d", "all", "custom") else "all"
        if f.preset in ("7d", "30d", "90d"):
            f.start, f.end = start_of_day(now) - (int(f.preset[:-1]) - 1) * DAY, None
        elif f.preset == "custom":
            f.since, f.until = q.get("from") or "", q.get("to") or ""
            try:
                f.start = parse_as_of(f.since) if f.since else None
                f.end = parse_as_of(f.until) + DAY if f.until else None
            except ValueError:
                f.preset, f.start, f.end, f.since, f.until = "all", None, None, "", ""
        return f

    def qs(self, **change: Any) -> str:
        p: dict[str, Any] = {}
        if self.preset != "all":
            p["range"] = self.preset
        if self.preset == "custom":
            p.update({"from": self.since, "to": self.until})
        if self.group != "day":
            p["group"] = self.group
        if self.strategy != DEFAULT_STRATEGY:
            p["strategy"] = self.strategy
        if self.venue != "all":
            p["venue"] = self.venue
        if self.wallet != "all":
            p["wallet"] = self.wallet
        if not self.no_outages:
            p["outages"] = "include"
        if self.model != "current":
            p["model"] = self.model
        if self.close != "all":
            p["close"] = self.close
        p.update(change)
        if p.get("range") != "custom":
            p.pop("from", None)
            p.pop("to", None)
        return urlencode({k: val for k, val in p.items() if val not in (None, "")})

    def period(self, ts: int) -> int:
        d = start_of_day(int(ts))
        return d - time.gmtime(d).tm_wday * DAY if self.group == "week" else d

    def period_label(self, ts: int) -> str:
        text = time.strftime("%b %-d", time.gmtime(ts))
        return f"wk of {text}" if self.group == "week" else text


@dataclass
class Ctx:
    app: Any
    records: list[dict]
    windows: list[tuple[int, int]]
    f: Filters
    strategies: list[str]
    wallets: list[str]
    now: int
    threshold: int
    path: str = "/"
    notes: list[str] = field(default_factory=list)
    scoped: Scoped | None = None
    models: list[int | None] = field(default_factory=list)   # model versions present in the bot rows

    def scope(self, *, strategy: bool = True, wallet: bool = True) -> list[dict]:
        f = self.f
        out = []
        for r in self.records:
            if strategy and r["key"] != f.strategy:
                continue
            if not strategy and r["simulated"]:
                continue
            if f.venue != "all" and r["market_venue"] != f.venue:
                continue
            if wallet and f.wallet != "all" and r["wallet"] != f.wallet:
                continue
            ts = r["alert_ts"]
            if (f.start is not None and ts < f.start) or (f.end is not None and ts >= f.end):
                continue
            if f.no_outages and any(a <= ts < b for a, b in self.windows):
                continue
            if f.close != "all" and horizon_bucket(r.get("days_to_close")) != CLOSE_FILTERS[f.close]:
                continue
            out.append(r)
        return out


def traded(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["status"] != "SKIPPED"]


def closed(rows: list[dict]) -> list[dict]:
    return sorted((r for r in rows if r["status"] == "FILLED" and r["settled_at"] is not None),
                  key=lambda r: (r["settled_at"], r["id"]))


def clv_values(rows: list[dict]) -> list[float]:
    return [r["closing_price"] - r["fill_price"] for r in closed(rows)
            if r["closing_price"] is not None and not in_play(r)]


def markout_values(rows: list[dict]) -> list[float]:
    return [r["markout_price"] - r["fill_price"] for r in rows
            if r["status"] == "FILLED" and r.get("markout_price") is not None]


def ci_text(s: Any) -> str:
    return "-" if s.ci_low is None else f"[{v.pct(s.ci_low)}, {v.pct(s.ci_high)}]"


def ci_note(s: Any, threshold: int) -> tuple[str, str]:
    """Plain words for what the interval does and does not show."""
    if not s.settled:
        return "info", "No closed trades in this range yet, so there is no result to read."
    if s.ci_low is None:
        return "info", (f"Only {s.settled} closed trade(s) from one wallet and one event: no interval can be "
                        "computed, so this is an anecdote, not a result.")
    lead = f"Mean return per trade is {v.pct(s.mean_roi)} with a 95% interval of {ci_text(s)} (n={s.settled}). "
    if s.ci_includes_zero:
        return "warn", lead + ("The interval includes zero: on this evidence the strategy cannot be told apart "
                               "from breaking even.")
    if s.ci_low > 0:
        return "good", lead + "The whole interval is above zero." + (
            "" if s.settled >= threshold else " With this few trades, treat that as provisional.")
    return "bad", lead + "The whole interval is below zero: on this evidence the strategy loses money."


# -- the filter row -----------------------------------------------------------------------------


def filters_html(c: Ctx) -> str:
    f = c.f
    presets = "".join(f'<a href="{c.path}?{f.qs(range=p)}" class="{"on" if f.preset == p else ""}">{t}</a>'
                      for p, t in (("7d", "7d"), ("30d", "30d"), ("90d", "90d"), ("all", "All")))

    def select(name: str, options: list[tuple[str, str]], current: str) -> str:
        opts = "".join(f'<option value="{v.esc(val)}"{" selected" if val == current else ""}>{v.esc(text)}</option>'
                       for val, text in options)
        return f'<select name="{name}">{opts}</select>'

    hidden = "" if f.preset in ("all", "custom") else f'<input type="hidden" name="range" value="{f.preset}">'
    custom = ""
    if f.preset == "custom":
        custom = (f'<input type="hidden" name="range" value="custom">'
                  f'<label>From<input type="date" name="from" value="{v.esc(f.since)}"></label>'
                  f'<label>To<input type="date" name="to" value="{v.esc(f.until)}"></label>'
                  '<button type="submit">Apply</button>')
    wallets = [("all", "All wallets")] + [(w, f"{w[:6]}…{w[-4:]}") for w in c.wallets]
    closes = [(k, "Any" if band is None else band.capitalize()) for k, band in CLOSE_FILTERS.items()]
    models = [("current", f"Model {MODEL_VERSION} (current)")]
    models += [(str(m), f"Model {m}") for m in c.models if m is not None and m != MODEL_VERSION]
    models += [("before", "Before tagging"), ("all", "All, mixed (not a result)")]
    return (f'<form class="filters" method="get" action="{c.path}">'
            f'<label>Range<span class="presets">{presets}'
            f'<a href="{c.path}?{f.qs(range="custom")}" class="{"on" if f.preset == "custom" else ""}">Custom</a></span></label>'
            f'{hidden}{custom}'
            f'<label>Strategy{select("strategy", [(k, label_of(k)) for k in c.strategies], f.strategy)}</label>'
            f'<label>Group by{select("group", [("day", "Day"), ("week", "Week")], f.group)}</label>'
            f'<label>Venue{select("venue", [("all", "All venues"), ("polymarket", "Polymarket"), ("polymarket_us", "Polymarket US")], f.venue)}</label>'
            f'<label>Wallet{select("wallet", wallets, f.wallet)}</label>'
            f'<label>Days to close{select("close", closes, f.close)}</label>'
            f'<label>Version{select("model", models, f.model)}</label>'
            f'<label class="check"><input type="checkbox" name="outages" value="include"'
            f'{"" if f.no_outages else " checked"}>Include outage windows</label></form>' + scope_note(c))


def scope_note(c: Ctx) -> str:
    """What the bot numbers on this page count, and what they leave out."""
    sc, f = c.scoped, c.f
    if sc is None:
        return ""
    scope = Scope(f.model, not f.no_outages)
    parts = [f"Bot numbers count {scope.label}."]
    if sc.other_models and f.model != "all":
        kept = ", ".join(f"{n} signals from {model_label(m)}" for m, n in sorted(
            sc.other_models.items(), key=lambda kv: (kv[0] is None, -(kv[0] or 0))))
        parts.append(f"Kept apart: {kept} (pick them under Version).")
    if sc.in_outages:
        parts.append(f"Left out: {sc.in_outages} signals inside outage windows (tick the box to count them).")
    if len(sc.settings) > 1:
        parts.append(f"These rows were made under {len(sc.settings)} different settings.")
    cls = "card-note warn" if f.model == "all" else "card-note"
    return f'<p class="{cls}">{v.esc(" ".join(parts))}</p>'


def size_banner(c: Ctx, settled: int) -> str:
    if settled >= c.threshold:
        return ""
    return v.banner(f"Not enough data: {settled} closed trades in this range (need at least {c.threshold}). "
                    "Every figure below is noise until then.")


# -- pages -----------------------------------------------------------------------------------------


def overview(c: Ctx) -> str:
    rows = c.scope()
    tr = traded(rows)
    s = summarise(label_of(c.f.strategy), tr, samples=SAMPLES)
    costs = breakdown(tr)
    skipped = len(rows) - len(tr)
    open_cost = sum(r["cost"] or 0.0 for r in tr if r["status"] == "FILLED" and r["settled_at"] is None)
    n = f"n={s.settled} closed"
    tiles = "".join([
        v.tile("Total P&L", v.money(s.total_pnl) if s.settled else "-", n, v.tone(s.total_pnl) if s.settled else ""),
        v.tile("ROI", v.pct(s.roi), f"on ${s.total_cost:,.0f} staked, {n}", v.tone(s.roi)),
        v.tile("Win rate", "-" if s.win_rate is None else f"{s.win_rate:.0%}", n),
        v.tile("Max drawdown", f"${s.max_drawdown:,.2f}" if s.settled else "-", n),
        v.tile("Open exposure", f"${open_cost:,.2f}", f"{s.open} open positions, at cost"),
        v.tile("Costs paid", f"${costs.fees + costs.slippage:,.2f}",
               f"fees ${costs.fees:,.2f} + slippage ${costs.slippage:,.2f}, n={costs.n} closed"),
        v.tile("Signals", str(len(rows)), f"{s.settled} closed · {s.open} open · {s.missed} missed · {skipped} skipped"),
    ])
    kind, note = ci_note(s, c.threshold)
    body = size_banner(c, s.settled) + f'<div class="tiles">{tiles}</div>' + v.banner(note, kind)
    scale = max(abs(costs.gross), abs(costs.net), costs.fees, costs.slippage, 0.01)
    cost_card = v.card("Where the money went", v.table(["", "Amount", ""], [
        ["Gross P&L (top of the book, no fees)", (v.money(costs.gross), costs.gross), (v.hbar(costs.gross, scale), 0)],
        ["Taker fees (buys, and sells on mirror exits)", (v.money(-costs.fees), -costs.fees), (v.hbar(-costs.fees, scale), 0)],
        ["Slippage (paid beyond the best ask)", (v.money(-costs.slippage), -costs.slippage), (v.hbar(-costs.slippage, scale), 0)],
        ["Net P&L", (v.money(costs.net), costs.net, v.tone(costs.net)), (v.hbar(costs.net, scale), 0)],
    ], sortable=False) if costs.n else '<p class="empty">No closed trades in this range yet.</p>',
        note=f"n={costs.n} closed trades. Every other figure on this dashboard is net of these costs. No rebates "
             "are counted.", wide=True)

    # Cumulative P&L, one line per live strategy, over the same range and filters.
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in closed(traded(c.scope(strategy=False))):
        by_key[r["key"]].append(r)
    keys = [k for k in SERIES if k in by_key]
    series, table_rows, periods = [], [], sorted({c.f.period(r["settled_at"]) for rs in by_key.values() for r in rs})
    values: dict[str, dict[int, float]] = {}
    for k in keys:
        total, at = 0.0, {}
        for r in by_key[k]:
            total += r["pnl"]
            at[c.f.period(r["settled_at"])] = total
        values[k] = at
        series.append({"name": label_of(k), "cls": SERIES[k], "points": [[p, round(val, 2)] for p, val in sorted(at.items())]})
    for p in periods:
        row: list[Any] = [c.f.period_label(p)]
        for k in keys:
            known = [val for q, val in values[k].items() if q <= p]
            row.append(v.money(values[k].get(p, known[-1] if known else None)) if known else "-")
        table_rows.append(row)
    cum = v.chart("line", {"series": series, "bands": c.windows, "y": "usd", "zero": True},
                  table_headers=["Period"] + [label_of(k) for k in keys], table_rows=table_rows,
                  legend=[(label_of(k), SERIES[k]) for k in keys])
    counts = ", ".join(f"{label_of(k)} n={len(by_key[k])}" for k in keys)
    body += '<div class="grid">' + cost_card + v.card(
        "Cumulative P&L by strategy", cum, wide=True,
        note=f"Closed trades only, by the date they closed. Shaded bands are outage windows. {counts}")

    per: dict[int, list[float]] = defaultdict(list)
    for r in closed(tr):
        per[c.f.period(r["settled_at"])].append(r["pnl"])
    bars = [{"label": c.f.period_label(p), "v": round(sum(vals), 2), "n": len(vals)} for p, vals in sorted(per.items())]
    body += v.card(
        f"P&L per {c.f.group}: {label_of(c.f.strategy)}",
        v.chart("bars", {"bars": bars, "y": "usd", "signed": True},
                table_headers=["Period", "P&L", "Closed"],
                table_rows=[[b["label"], v.money(b["v"]), str(b["n"])] for b in bars],
                legend=[("Gain", "pos"), ("Loss", "neg")]),
        note=f"n={s.settled} closed trades", wide=True)
    return body + overview_wallets(c) + "</div>"


def overview_wallets(c: Ctx) -> str:
    """Part 2's wallet chart, one per runner, with its tiles and the sample-size warning.
    Not scoped by the filters: a wallet is one path through all of its signals."""
    app = c.app
    try:
        found = bankroll.views(c.records, app.cfg, app.db, c.now, only={"bot:single", "bot:us"})
    except Exception as exc:  # noqa: BLE001 - the overview must still render
        log.warning("wallet cards could not be worked out: %s", exc)
        return ""
    out = ""
    for view in found:
        res = view.live if view.live is not None else view.replay
        amount = res.params.amount
        shown = f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"
        which = (view.live.label if view.live is not None else
                 "Replay of every signal: the live run starts when the runner next starts")
        out += v.card(f"{shown} wallet: {view.label}",
                      wallet_warnings(res, c.threshold) + wallet_tiles(res) + wallet_chart(res),
                      note=f"{which}. Not affected by the filters above. The amount, stake sizes and the replay with "
                           "and without the cutoff are on the Sim wallet page.", wide=True)
    return out


def histogram(values: list[float], lo: float = -0.2, hi: float = 0.2, step: float = 0.02) -> tuple[list[dict], int]:
    n = round((hi - lo) / step)
    counts = [0] * n
    for x in values:
        # Prices move in whole ticks, so most values sit exactly on a bucket edge, and
        # in floating point 0.58 - 0.57 is 0.00999...: without the rounding a one-cent
        # gap lands in the bucket below about one time in eight.
        counts[min(n - 1, max(0, math.floor(round((x - lo) / step, 9))))] += 1
    bars = [{"label": f"{lo + i * step:+.2f}", "v": cnt, "n": cnt} for i, cnt in enumerate(counts)]
    return bars, n // 2


def edge(c: Ctx) -> str:
    rows = traded(c.scope())
    s = summarise("x", rows, samples=SAMPLES)
    clv, marks = clv_values(rows), markout_values(rows)
    body = size_banner(c, s.settled)
    tiles = "".join([
        v.tile("Closing-line value", v.cents(mean(clv)), f"n={len(clv)} pre-game fills", v.tone(mean(clv))),
        v.tile("Price 5 min after our fill", v.cents(mean(marks)), f"n={len(marks)}, every fill", v.tone(mean(marks))),
        v.tile("Whale to our fill", v.cents(s.avg_latency_cost), "per share, filled trades"),
        v.tile("Whale to best ask", v.cents(s.avg_detection_cost), f"per share, n={s.detection_n}, misses included"),
    ])
    body += f'<div class="tiles">{tiles}</div><div class="grid">'

    bars, ref = histogram(clv)
    body += v.card("Closing-line value, distribution",
                   v.chart("bars", {"bars": bars, "y": "num", "signed": False, "cls": "s1", "ref": ref},
                           table_headers=["CLV from", "Fills"], table_rows=[[b["label"], str(b["v"])] for b in bars])
                   if clv else '<p class="empty">No pre-game fills with a closing price yet.</p>',
                   note=f"Closing price minus our fill, in dollars a share. Right of the line is good. n={len(clv)}. "
                        f"{s.clv_inplay_excluded} in-play fills have no closing line and are left out.")
    bars, ref = histogram(marks)
    body += v.card("Price 5 minutes after our fill, distribution",
                   v.chart("bars", {"bars": bars, "y": "num", "signed": False, "cls": "s3", "ref": ref},
                           table_headers=["Move from", "Fills"], table_rows=[[b["label"], str(b["v"])] for b in bars])
                   if marks else '<p class="empty">No fills old enough to have a 5-minute price yet.</p>',
                   note=f"Works for in-play fills too. n={len(marks)}.")

    by_p: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"clv": [], "mark": []})
    for r in rows:
        if r["status"] != "FILLED" or r["fill_price"] is None:
            continue
        p = c.f.period(r["alert_ts"])
        if r["settled_at"] is not None and r["closing_price"] is not None and not in_play(r):
            by_p[p]["clv"].append(r["closing_price"] - r["fill_price"])
        if r.get("markout_price") is not None:
            by_p[p]["mark"].append(r["markout_price"] - r["fill_price"])
    ps = sorted(by_p)
    trend = v.chart(
        "line",
        {"series": [{"name": "Closing-line value", "cls": "s1",
                     "points": [[p, round(mean(by_p[p]["clv"]), 4), len(by_p[p]["clv"])] for p in ps if by_p[p]["clv"]]},
                    {"name": "5 minutes after fill", "cls": "s3",
                     "points": [[p, round(mean(by_p[p]["mark"]), 4), len(by_p[p]["mark"])] for p in ps if by_p[p]["mark"]]}],
         "bands": c.windows, "y": "cents", "zero": True},
        table_headers=["Period", "Mean CLV", "n", "Mean 5-min move", "n"],
        table_rows=[[c.f.period_label(p), v.cents(mean(by_p[p]["clv"])), str(len(by_p[p]["clv"])),
                     v.cents(mean(by_p[p]["mark"])), str(len(by_p[p]["mark"]))] for p in ps],
        legend=[("Closing-line value", "s1"), ("5 minutes after fill", "s3")])
    body += v.card(f"CLV over time, mean per {c.f.group}", trend, wide=True,
                   note="By the date of the signal. A mean over a handful of fills swings wildly: check n.")

    done = [r for r in rows if r["settled_at"] is not None and r.get("zero_pnl") is not None
            and (r.get("human_pnl") is not None or r.get("atask_pnl") is not None)]
    if done:
        ours = sum(r["pnl"] or 0.0 for r in done)
        zero = sum(r["zero_pnl"] for r in done)
        is_bot = done[0].get("human_pnl") is not None
        third = sum((r["human_pnl"] if is_bot else r["atask_pnl"]) for r in done)
        scale = max(abs(ours), abs(zero), abs(third), 0.01)
        cmp_rows = [["Zero latency (the whale's own price)", (v.money(zero), zero), (v.hbar(zero, scale), zero)],
                    [label_of(c.f.strategy), (v.money(ours), ours), (v.hbar(ours, scale), ours)],
                    ["Old rule: a minute later" if is_bot else "At the alert-time ask", (v.money(third), third),
                     (v.hbar(third, scale), third)]]
        shadow = v.table(["Fill", "P&L", ""], cmp_rows, sortable=False)
    else:
        shadow = '<p class="empty">No closed signals with comparison fills yet.</p>'
    body += v.card("Same signals, three fills", shadow,
                   note=f"n={len(done)} closed signals, same stake each, a missed fill counted as $0. The gap between "
                        "rows is what latency costs.")

    delays = [r["decided_at"] - r["signal_ts"] for r in rows if r.get("decided_at") and r.get("signal_ts")
              and r["status"] in ("FILLED", "MISSED") and 0 <= r["decided_at"] - r["signal_ts"] <= 600]
    seen = [r["detect_delay"] for r in rows if r.get("detect_delay") is not None and r["detect_delay"] <= 600]
    kv = [("Whale's fill to our detector", f"median {median(seen):.1f}s (n={len(seen)})" if seen else "-"),
          ("Whale's fill to our decision", f"median {median(delays):.1f}s (n={len(delays)})" if delays else "-"),
          ("Price paid above the whale", f"{v.cents(s.avg_latency_cost)} a share on fills"),
          ("Missed", f"{s.missed} of {s.alerts}: " + (", ".join(f"{k} {n}" for k, n in sorted(s.miss_reasons.items())) or "none"))]
    body += v.card("Latency cost", '<dl class="kv">' + "".join(
        f"<dt>{v.esc(k)}</dt><dd>{v.esc(val)}</dd>" for k, val in kv) + "</dl>")
    return body + "</div>"


def memberships(app: Any) -> dict[str, Any]:
    try:
        rows = app.db.query("SELECT * FROM watchlist_membership ORDER BY added_at, id")
    except Exception:  # noqa: BLE001 - a database from before the watchlist
        return {}
    return {r["wallet"]: r for r in rows}       # the latest stay per wallet


def wallet_status(m: Any, now: int) -> tuple[str, float | None, str]:
    if m is None:
        return "not on the list", None, ""
    if m["removed_at"] is not None:
        return "removed", (m["removed_at"] - m["added_at"]) / DAY, "bad"
    days = (now - m["added_at"]) / DAY
    if m["fail_streak"]:
        return f"failing {m['fail_streak']}", days, "bad"
    return ("probation", days, "") if now < m["probation_until"] else ("established", days, "good")


def wallets(c: Ctx) -> str:
    rows = traded(c.scope(wallet=False))
    members = memberships(c.app)
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["wallet"]].append(r)
    scale = max([abs(sum(x["pnl"] or 0.0 for x in closed(rs))) for rs in by.values()] + [0.01])
    out = []
    for w, rs in by.items():
        s = summarise(w, rs, samples=300)
        status, days, cls = wallet_status(members.get(w), c.now)
        clv = clv_values(rs)
        out.append([
            v.link(f"/wallet/{w}?{c.f.qs()}", f"{w[:6]}…{w[-4:]}"),
            (v.Raw(f'<span class="pill {cls}">{v.esc(status)}</span>'), status),
            ("-" if days is None else f"{days:.0f}", -1 if days is None else days),
            (str(len(rs)), len(rs)), (str(s.settled), s.settled),
            ("-" if s.win_rate is None else f"{s.win_rate:.0%}", -1 if s.win_rate is None else s.win_rate),
            (v.money(s.total_pnl) if s.settled else "-", s.total_pnl, v.tone(s.total_pnl) if s.settled else ""),
            (v.hbar(s.total_pnl, scale), s.total_pnl),
            (v.pct(s.roi), -9 if s.roi is None else s.roi),
            (f"{v.cents(mean(clv))} (n={len(clv)})", -9 if not clv else mean(clv)),
        ])
    out.sort(key=lambda r: -r[6][1])
    total = summarise("x", rows, samples=1)
    body = size_banner(c, total.settled) + '<div class="grid">' + v.card(
        "Wallets", v.table(
            ["Wallet", "Status", "Days on list", "Signals", "Closed", "Win rate", "P&L", "", "ROI",
             ("CLV", "Closing price minus our fill, pre-game fills")], out, numeric_from=2),
        note=f"{label_of(c.f.strategy)}. P&L is what OUR paper trades on the wallet's signals made, not the "
             "wallet's own profit. A wallet with a handful of closed trades tells you nothing yet.", wide=True)
    try:
        hist = c.app.db.query("SELECT * FROM watchlist_membership ORDER BY added_at DESC, id DESC")
    except Exception:  # noqa: BLE001
        hist = []
    events = []
    for m in hist:
        events.append((m["added_at"], "Added", m["wallet"], m["add_reason"]))
        if m["removed_at"] is not None:
            events.append((m["removed_at"], "Removed", m["wallet"], m["remove_reason"]))
    events.sort(reverse=True)
    body += v.card("Watchlist timeline", v.table(
        ["When (UTC)", "Change", "Wallet", "Why"],
        [[iso(ts)[:16], (v.Raw(f'<span class="pill {"good" if what == "Added" else "bad"}">{what}</span>'), what),
          v.link(f"/wallet/{w}?{c.f.qs()}", f"{w[:6]}…{w[-4:]}"), (why or "", None, "wrap")]
         for ts, what, w, why in events[:60]], numeric_from=9, empty="No watchlist changes recorded yet."),
        note=f"{len(events)} changes in all; the newest 60 are shown. History is never deleted.", wide=True)
    return body + "</div>"


def trade_cells(r: dict, c: Ctx, *, with_wallet: bool = True) -> list[Any]:
    clv = (r["closing_price"] - r["fill_price"]) if (r.get("closing_price") is not None and r.get("fill_price") is not None
                                                    and r["status"] == "FILLED" and not in_play(r)) else None
    status = "CLOSED" if r["status"] == "FILLED" and r["settled_at"] is not None else (
        "OPEN" if r["status"] == "FILLED" else r["status"])
    cells: list[Any] = [(iso(r["alert_ts"])[:16], r["alert_ts"])]
    if with_wallet:
        cells.append(v.link(f"/wallet/{r['wallet']}?{c.f.qs()}", f"{r['wallet'][:6]}…{r['wallet'][-4:]}"))
    cells += [
        (f"{r.get('question') or r['condition_id'][:14]} · {r.get('outcome') or ''}", None, "wrap"),
        (v.Raw(f'<span class="pill">{v.esc(status)}</span>'), status),
        (r.get("reason") or "", None, "wrap"),
        ("-" if r.get("whale_price") is None else f"{r['whale_price']:.3f}", r.get("whale_price") or 0),
        ("-" if r.get("fill_price") is None else f"{r['fill_price']:.3f}", r.get("fill_price") or 0),
        ("-" if r.get("cost") is None else f"${r['cost']:,.2f}", r.get("cost") or 0),
        (v.money(r["pnl"]) if status == "CLOSED" else "-", r["pnl"] if status == "CLOSED" else 0,
         v.tone(r["pnl"]) if status == "CLOSED" else ""),
        (v.cents(clv), -9 if clv is None else clv),
        ("-" if r.get("detect_delay") is None else f"{r['detect_delay']:.1f}s", r.get("detect_delay") or 0),
    ]
    return cells


TRADE_HEADERS = ["Signal (UTC)", "Wallet", "Market · outcome", "Status", "Reason", "Whale price", "Our fill", "Cost",
                 "P&L", "CLV", "Detected after"]


def trades(c: Ctx, page: int) -> str:
    rows = sorted(c.scope(), key=lambda r: (-r["alert_ts"], -r["id"]))
    pages = max(1, -(-len(rows) // PAGE_ROWS))
    page = min(max(1, page), pages)
    shown = rows[(page - 1) * PAGE_ROWS: page * PAGE_ROWS]
    pager = (f'<div class="pager"><span>{len(rows)} rows · page {page} of {pages}</span>'
             + (v.link(f"/trades?{c.f.qs(page=page - 1)}", "← Newer") if page > 1 else "")
             + (v.link(f"/trades?{c.f.qs(page=page + 1)}", "Older →") if page < pages else "")
             + v.link(f"/trades.csv?{c.f.qs()}", "Download CSV") + "</div>")
    s = summarise("x", traded(rows), samples=1)
    return size_banner(c, s.settled) + v.card(
        f"Every signal and paper trade: {label_of(c.f.strategy)}",
        v.table(TRADE_HEADERS, [trade_cells(r, c) for r in shown], numeric_from=5) + pager,
        note="Newest first. MISSED means the order book could not fill it; SKIPPED means a risk limit refused it.")


CSV_FIELDS = ["id", "key", "status", "reason", "wallet", "category", "question", "outcome", "condition_id", "token_id",
              "alert_ts", "signal_ts", "detect_delay", "decided_at", "stake_usd", "whale_price", "best_ask", "fill_price",
              "contracts", "fees", "cost", "settled_at", "pnl", "closing_price", "markout_price", "zero_pnl",
              "human_pnl", "probation"]


def trades_csv(c: Ctx) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(sorted(c.scope(), key=lambda r: (r["alert_ts"], r["id"])))
    return buf.getvalue()


def breakdown_table(c: Ctx, rows: list[dict], key: Any, first: str, *, limit: int = 0,
                    order: Any = "n") -> tuple[str, list]:
    """``order``: "n" (most closed trades first), "label", or a function of the label."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(key(r))].append(r)
    stats = [(g, summarise(g, rs, samples=600)) for g, rs in groups.items()]
    stats.sort(key=(lambda x: order(x[0])) if callable(order) else (lambda x: x[0]) if order == "label" else
               (lambda x: (-x[1].settled, -x[1].alerts, x[0])))
    more = max(0, len(stats) - limit) if limit else 0
    if limit:
        stats = stats[:limit]
    scale = max([abs(s.total_pnl) for _, s in stats] + [0.01])
    out = [[(g, None, "wrap" if len(g) > 28 else ""), (str(s.alerts), s.alerts), (str(s.settled), s.settled),
            ("-" if s.win_rate is None else f"{s.win_rate:.0%}", -1 if s.win_rate is None else s.win_rate),
            (v.money(s.total_pnl) if s.settled else "-", s.total_pnl, v.tone(s.total_pnl) if s.settled else ""),
            (v.hbar(s.total_pnl, scale), s.total_pnl), (v.pct(s.roi), -9 if s.roi is None else s.roi),
            (f"{v.pct(s.mean_roi)} {ci_text(s)}" if s.settled else "-", -9 if s.mean_roi is None else s.mean_roi)]
           for g, s in stats]
    html_ = v.table([first, "Signals", "Closed", "Win rate", "P&L", "", "ROI", "Mean ROI, 95% interval"], out)
    if more:
        html_ += f'<p class="card-note">{more} more groups with fewer trades are not shown.</p>'
    return html_, stats


def cutoff_card(c: Ctx) -> str:
    """What the time-to-close cutoff did in this view, and schedule against reality."""
    rep = cutoff_report(c.app, c.scope())
    fl = c.app.cfg.filters
    limit = "off" if rep.limit is None else f"{rep.limit:g} days"

    def share(k: int, n: int) -> str:
        return "-" if not n else f"{k} of {n} ({k / n:.0%})"

    tiles = (v.tile("Cutoff", limit, "no end date: " + ("skipped" if fl.skip_if_no_end_date else "allowed"))
             + v.tile("Skipped as too long", str(rep.too_long), "signals")
             + v.tile("Skipped, no end date", str(rep.no_end_date), "signals")
             + v.tile("Held past the limit", share(rep.held_over, rep.closed), "closed trades")
             + v.tile("Resolved a day or more late", share(rep.late, rep.resolved), "against the scheduled end"))
    note = ("The cutoff reads the market's scheduled end. Resolution can come later, so a trade inside the limit "
            "can still be held past it.")
    if rep.would_skip:
        note += (f" {rep.would_skip} filled trades in this view were entered in markets the cutoff now refuses "
                 f"(closed P&L {v.money(rep.would_skip_pnl)}). They stay in every total.")
    return v.card("Time-to-close cutoff", f'<div class="tiles">{tiles}</div>', note=note, wide=True)


def breakdowns(c: Ctx) -> str:
    rows = traded(c.scope())
    total = summarise("x", rows, samples=1)
    body = size_banner(c, total.settled) + '<div class="grid">'
    note = "Slicing a small sample makes smaller samples: read n before any P&L."
    body += v.card("By category", breakdown_table(c, rows, lambda r: r.get("category") or "unknown", "Category")[0],
                   note=note, wide=True)
    body += v.card("By entry price", breakdown_table(
        c, rows, lambda r: bucket(r["fill_price"] if r.get("fill_price") is not None else r.get("alert_ask")),
        "Price", order="label")[0], note="Our fill price (the best ask for a miss).", wide=True)
    body += cutoff_card(c)
    order = {b: i for i, b in enumerate((*CLOSE_BUCKETS, NO_END, "still open"))}
    for title, key, first, note_ in (
            ("By holding time", lambda r: "still open" if r.get("held_days") is None else horizon_bucket(r["held_days"]),
             "Held for", "From our fill to the end of the position: the market's real resolution, or a mirror exit. "
                         "Missed signals have no position and sit under still open."),
            ("By days to close when entered", lambda r: horizon_bucket(r.get("days_to_close")), "Scheduled close",
             "The market's scheduled end minus the moment we decided. This is what the cutoff reads.")):
        html_, stats = breakdown_table(c, rows, key, first, order=lambda g: order.get(g, 99))
        bars = [{"label": g, "v": round(s.total_pnl, 2), "n": s.settled} for g, s in stats]
        body += v.card(title, v.chart(
            "bars", {"bars": bars, "y": "usd", "signed": True}, table_headers=[first, "P&L", "Closed"],
            table_rows=[[b["label"], v.money(b["v"]), str(b["n"])] for b in bars],
            legend=[("Gain", "pos"), ("Loss", "neg")]) + html_, note=note_, wide=True)
    hour_html, hour_stats = breakdown_table(c, rows, lambda r: f"{time.gmtime(r['alert_ts']).tm_hour:02d}:00",
                                            "Hour (UTC)", order="label")
    bars = [{"label": g[:2], "v": round(s.total_pnl, 2), "n": s.settled} for g, s in hour_stats]
    body += v.card("By hour of day (UTC)", v.chart(
        "bars", {"bars": bars, "y": "usd", "signed": True}, table_headers=["Hour", "P&L", "Closed"],
        table_rows=[[b["label"] + ":00", v.money(b["v"]), str(b["n"])] for b in bars],
        legend=[("Gain", "pos"), ("Loss", "neg")]) + hour_html, note="Hour of the signal.", wide=True)
    body += v.card("By event", breakdown_table(
        c, rows, lambda r: r.get("event_slug") or event_key(r), "Event", limit=20)[0],
        note="The 20 events with the most closed trades. Several markets of one game count as one event.", wide=True)
    return body + "</div>"


# -- Polymarket US, row A (v2.1 Part 3b) ----------------------------------------------------------------


RULES_SHOWN = 12          # matched markets whose two sets of rules are shown
RULES_CHARS = 600


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return "-" if not text else text if len(text) <= RULES_CHARS else text[:RULES_CHARS].rsplit(" ", 1)[0] + " …"


def rules_card(us_rows: list[dict]) -> str:
    """Both sites' resolution rules for the most recent matched markets, side by side."""
    seen: dict[str, dict] = {}
    for r in sorted(us_rows, key=lambda r: -r["alert_ts"]):
        if r.get("us_slug") and r.get("us_rules") and r["us_slug"] not in seen:
            seen[r["us_slug"]] = r
    rows = []
    for r in list(seen.values())[:RULES_SHOWN]:
        intl_end = r.get("scheduled_end") or r.get("market_end")
        deadlines = (f"Intl {iso(int(intl_end))[:10] if intl_end else '-'} · "
                     f"US {iso(int(r['us_end_date']))[:10] if r.get('us_end_date') else '-'}")
        rows.append([((r.get("question") or r["us_slug"])[:80], None, "wrap"), (_clip(r.get("market_rules")), None, "wrap"),
                     (_clip(r.get("us_rules")), None, "wrap"), (deadlines, None, "wrap")])
    return v.card("Resolution rules side by side", v.table(
        ["Market", "International rules", "Polymarket US rules", "End dates"], rows,
        empty="No matched market has its US rules stored yet. They are stored from this version on.",
        sortable=False, numeric_from=9),
        note="The same bet on both sites, as each site words it, for the most recent matched markets. The two always "
             "word it differently, so every matched market is shown: read the postponement and cancellation lines, "
             "where a real difference would be. Sports end dates differ by design: the US venue sets about 14 days "
             "after the game.", wide=True)


def us_page(c: Ctx) -> str:
    """The same signals on Polymarket US, beside the international baseline. Scoped by
    the date range and the outage switch only: it is always these two rows."""
    f = c.f

    def ranged(key: str) -> list[dict]:
        out = []
        for r in c.records:
            ts = r["alert_ts"]
            if r["key"] != key or (f.start is not None and ts < f.start) or (f.end is not None and ts >= f.end):
                continue
            if f.no_outages and any(a <= ts < b for a, b in c.windows):
                continue
            out.append(r)
        return out

    us_rows, base_rows = ranged(US_KEY), ranged(BASE_KEY)
    intro = v.banner(
        "Row A. Nobody's trades are visible on Polymarket US, so there is nothing to copy there. This page shows what "
        "acting on the SAME signals would have cost on the Polymarket US order book, at US fees, read from its public "
        "market data. Nothing is ordered anywhere. Of the filters above, only the date range and the outage "
        "switch apply here.", "info")
    if not us_rows:
        return intro + v.card("Polymarket US", '<p class="empty">No signal has been priced on Polymarket US in this '
                                               "range yet. It starts with the next signal the runner makes.</p>",
                              wide=True)
    cmp = us_comparison(us_rows, base_rows, samples=1500)
    us_sum = summarise("us", [r for r in us_rows if r["status"] != "SKIPPED"], samples=1500)
    alerted = {r["alert_id"] for r in us_rows}
    same = [r for r in base_rows if r["alert_id"] in alerted and r["status"] != "SKIPPED"]
    base_sum = summarise("base", same, samples=1500)
    n, m, lo, hi = cmp.paired
    fill_mean = mean([g for _, g in cmp.fill_gaps]) if cmp.fill_gaps else None
    tiles = (v.tile("Signals priced", str(cmp.signals), "made since row A began")
             + v.tile("On Polymarket US", f"{cmp.on_us} ({cmp.on_us / cmp.signals:.0%})" if cmp.signals else "-",
                      "the same bet exists there")
             + v.tile("Filled there", str(us_sum.filled), f"{us_sum.missed} did not fill · {us_sum.settled} closed")
             + v.tile("Mean fill-price gap", "-" if fill_mean is None else v.cents(fill_mean),
                      f"US fill minus international, a share, n={len(cmp.fill_gaps)}")
             + v.tile("Fees, both filled", f"${cmp.fee_us:,.2f} vs ${cmp.fee_intl:,.2f}",
                      f"US vs international, n={cmp.both_filled}")
             + v.tile("ROI, US minus intl", "-" if not n else v.pct(m),
                      "no signal closed on both yet" if not n else
                      f"n={n}, 95% interval " + ("-" if lo is None else f"[{v.pct(lo)}, {v.pct(hi)}]"),
                      v.tone(m) if n else ""))
    body = intro + size_banner(c, us_sum.settled) + f'<div class="tiles">{tiles}</div><div class="grid">'

    bars, ref = histogram(cmp.gaps, lo=-0.05, hi=0.05, step=0.01)
    body += v.card("Price gap between the venues", v.chart(
        "bars", {"bars": bars, "y": "num", "signed": False, "cls": "s1", "ref": ref},
        table_headers=["US ask minus international ask", "Signals"],
        table_rows=[[b["label"], str(b["n"])] for b in bars]) if cmp.gaps else '<p class="empty">No gap measured yet.</p>',
        note="Best ask on Polymarket US minus the best ask the bot saw on the international book, for the same "
             "outcome, read moments apart. Right of the line the US price was higher. The two ends collect "
             "everything beyond 5 cents.", wide=True)

    per: dict[int, list[float]] = defaultdict(list)
    for ts, gap in cmp.fill_gaps:
        per[f.period(ts)].append(gap)
    points = [[p, round(sum(g) / len(g), 4), len(g)] for p, g in sorted(per.items())]
    body += v.card("Fill-price gap over time", v.chart(
        "line", {"series": [{"name": "US fill minus international fill", "cls": "s1", "points": points}],
                 "y": "cents", "zero": True},
        table_headers=["Period", "Mean gap a share", "Signals"],
        table_rows=[[f.period_label(p), v.cents(g), str(k)] for p, g, k in points])
        if points else '<p class="empty">No signal has a fill price on both venues yet.</p>',
        note="For each signal, what the walk paid on Polymarket US minus what it paid on the international book, "
             "or would have paid where the price ran past the limit. Each point is the mean of one "
             f"{f.group}; the tooltip shows its n. Above the zero line, acting where you legally can cost more. "
             f"Overall: {'-' if fill_mean is None else v.cents(fill_mean)} a share, n={len(cmp.fill_gaps)}.",
        wide=True)

    def rate(x: Any) -> tuple[str, float]:
        return ("-", -9.0) if x is None else (f"{x:.0%}", x)

    def per_share(x: Any) -> tuple[str, float]:
        return ("-", -9.0) if x is None else (v.cents(x), x)

    def row(name: str, s: Any, fig: dict) -> list:
        return [name, (str(s.alerts), s.alerts), (str(s.filled), s.filled), rate(fig.get("missed_rate")),
                rate(fig.get("not_on_us_rate")) if "not_on_us_rate" in fig else ("-", -9.0),
                (str(s.settled), s.settled),
                rate(s.win_rate) if s.settled else ("-", -9.0),
                (v.money(s.total_pnl) if s.settled else "-", s.total_pnl, v.tone(s.total_pnl) if s.settled else ""),
                (v.pct(s.roi), -9 if s.roi is None else s.roi),
                (f"{v.pct(s.mean_roi)} {ci_text(s)}" if s.settled else "-", -9 if s.mean_roi is None else s.mean_roi),
                per_share(fig.get("spread")), per_share(fig.get("slippage"))]

    body += v.card("The same signals on both venues", v.table(
        ["Venue", "Signals", "Filled", "Missed", "Not on US", "Closed", "Win rate", "Net P&L", "ROI",
         "Mean ROI (95%)", "Spread", "Slippage"],
        [row("International (the baseline)", base_sum, cmp.side.get("intl", {})),
         row("Polymarket US (row A)", us_sum, cmp.side.get("us", {}))],
        sortable=False),
        note="Never combined. Signals the cutoff or a missing US market skipped are not attempts. Missed rate: "
             "attempts that did not fill. Spread: best ask minus best bid when we looked. Slippage: fill price minus "
             "the best ask, a share. Fees on the signals both venues filled are in the tiles above. The US fee "
             "coefficient is 0.095 on every market.", wide=True)

    body += v.card("Why a signal was not priced on Polymarket US", v.table(
        ["Reason", "Signals"], [[(k, None, "wrap"), (str(n_), n_)] for k, n_ in
                                sorted(cmp.reasons.items(), key=lambda kv: -kv[1])],
        empty="Every signal was priced there.", numeric_from=1),
        note="A market is matched by an exact rule on its name, or not at all. not_same_bet: crypto up or down "
             "covers the same window on both venues but settles on a different price index.", wide=True)

    body += rules_card(us_rows)

    traded_us = [r for r in us_rows if r["status"] != "SKIPPED"]
    s = cmp.no_sports
    sports_line = ("Sports excluded: no fill has closed (almost everything on both venues is sports)."
                   if s is None or not s.settled else
                   f"Sports excluded: net {v.money(s.total_pnl)} on ${s.total_cost:,.2f}, n={s.settled} closed, "
                   f"mean ROI {v.pct(s.mean_roi)} {ci_text(s)}.")
    body += v.card("Polymarket US by category", breakdown_table(
        c, traded_us, lambda r: r.get("category") or "unknown", "Category")[0],
        note=sports_line + " Set polymarket_us.exclude_categories to leave a category out there.", wide=True)

    rows = []
    for r in sorted(us_rows, key=lambda r: -r["alert_ts"])[:40]:
        rows.append([iso(int(r["alert_ts"]))[:16].replace("T", " "), ((r.get("question") or "")[:50], None, "wrap"),
                     r["status"].capitalize(), ((r.get("reason") or r.get("us_side") or "")[:70], None, "wrap"),
                     "-" if r.get("intl_best_ask") is None else f"{r['intl_best_ask']:.3f}",
                     "-" if r.get("best_ask") is None else f"{r['best_ask']:.3f}",
                     ("-" if r.get("price_gap") is None else v.cents(r["price_gap"]), r.get("price_gap") or 0),
                     "-" if r["status"] != "FILLED" else f"${r.get('fees') or 0:,.2f}",
                     ("-" if r.get("pnl") is None or r.get("settled_at") is None else v.money(r["pnl"]),
                      r.get("pnl") or 0, v.tone(r["pnl"]) if r.get("settled_at") is not None else "")])
    body += v.card("Latest signals on Polymarket US", v.table(
        ["When (UTC)", "Market", "Result", "Side or reason", "Intl ask", "US ask", "Gap", "US fee", "P&L"], rows,
        numeric_from=4), note="The 40 most recent, newest first.", wide=True)
    return body + "</div>"


# -- the simulated small wallet (v2.1 Part 2) ---------------------------------------------------------

SKIP_MEANING = {
    "no_slot": "All positions were open, so the signal could only be watched.",
    "no_cash": "Less cash was free than the smallest order.",
    "below_minimum": "The stake was under the smallest order, in dollars or in shares.",
    "event_cap": "The event already held its share of the wallet.",
    "no_edge": "Automatic stake: the settled signals have not shown an edge, so the right stake is nothing.",
    "too_long": "The market was scheduled to end beyond the time-to-close cutoff.",
    "no_end_date": "The market had no scheduled end date.",
    "not_on_us": "Polymarket US: the same bet is not listed there (or no rule can line it up exactly).",
    "not_same_bet": "Polymarket US: the same window is listed but settles on a different price index.",
    "us_excluded": "Polymarket US: the category is excluded there (polymarket_us.exclude_categories).",
}
CURVE_POINTS = 700      # more than this many equity points are thinned before they are sent to the page
RUG_BINS = 140


def _thin(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    kept = [points[int(i * step)] for i in range(limit)]
    return kept + ([points[-1]] if kept[-1] is not points[-1] else [])


def wallet_chart(res: Any) -> str:
    curve = _thin(res.curve, CURVE_POINTS)
    if len(curve) < 2:
        return '<p class="empty">No wallet activity yet.</p>'
    t0, t1 = curve[0].ts, curve[-1].ts
    width = max(1.0, (t1 - t0) / RUG_BINS)
    bins: dict[int, int] = defaultdict(int)
    for e in res.entries:
        # The markers the brief asked for: trades the wallet wanted but could not take.
        if e.status == bankroll.SKIPPED and e.code in (bankroll.NO_CASH, bankroll.NO_SLOT):
            bins[int((e.signal.ts - t0) // width)] += 1
    data = {"points": [[int(p.ts), round(p.equity, 2), round(p.cash, 2)] for p in curve], "start": res.params.amount,
            "skips": [[int(t0 + (k + 0.5) * width), n] for k, n in sorted(bins.items())]}
    rows = [[iso(int(p.ts))[:16].replace("T", " "), v.money(p.equity, signed=False), v.money(p.cash, signed=False),
             v.money(p.tied, signed=False)] for p in curve[-200:]]
    legend = [("Equity", "s1"), ("Tied up in positions", "s2 area"), ("Free cash", "s3 area"),
              ("Skipped: no cash or no free slot", "rug")]
    return v.chart("wallet", data, table_headers=["When (UTC)", "Equity", "Free cash", "Tied up"], table_rows=rows,
                   legend=legend)


def wallet_tiles(res: Any) -> str:
    s = res.summary()
    skipped = sum(s["skipped"].values())
    return ('<div class="tiles">'
            + v.tile("Equity", v.money(s["equity"], signed=False), f"cash {v.money(s['cash'], signed=False)} free")
            + v.tile("Total return", v.pct(s["total_return"]), f"on ${s['amount']:,.2f} put in",
                     v.tone(s["total_return"]))
            + v.tile("Max drawdown", v.money(s["max_drawdown_usd"], signed=False),
                     f"{s['max_drawdown_pct']:.1%} below the high before it")
            + v.tile("Taken / skipped", f"{s['taken']} / {skipped}", f"of {s['signals']} signals · {s['missed']} did not fill")
            + v.tile("Total fees", v.money(s["fees"], signed=False),
                     f"plus {v.money(s['onramp_cost'], signed=False)} on-ramp")
            + v.tile("Settled", str(s["settled"]), f"{s['wins']} won · {s['open']} open") + "</div>")


def wallet_warnings(res: Any, threshold: int) -> str:
    out = ""
    settled = len(res.settled)
    if settled < threshold:
        out += v.banner(f"Not enough data: {settled} settled wallet trades (need at least {threshold}). A wallet's path "
                        "depends on the order of a few wins and losses, so this curve says little yet.")
    if res.optimistic:
        out += v.banner(f"{res.optimistic} of {len(res.taken)} fills are priced from a smaller order or from the best "
                        "ask, because no order book was stored for them. A real order of this size may have paid "
                        "more, or not filled.")
    return out


def wallet_form(amount: float, saved: str, error: str) -> str:
    note = ""
    if error:
        note = v.banner(f"Not saved: {error}.", "bad")
    elif saved:
        note = v.banner("Saved. The runner ends the current wallet run and starts a new one within a minute. "
                        "The replay below already uses the new amount.", "good")
    form = ('<form class="setting" method="post" action="/settings/wallet-amount">'
            '<label>Wallet amount, in dollars'
            f'<input type="text" name="amount" inputmode="decimal" autocomplete="off" value="{amount:.2f}" '
            'aria-describedby="amount-help"></label><button type="submit">Save</button></form>'
            f'<p class="card-note" id="amount-help">Above $0, at most ${bankroll.MAX_AMOUNT:,.0f}. Changing it starts '
            "a new simulated wallet run. Earlier runs are kept below and never rewritten. This is the only thing "
            "this site can change.</p>")
    return note + v.card("Wallet amount", form, wide=True)


def bankroll_page(c: Ctx, query: Any) -> tuple[str, str]:
    """(title, body). Not scoped by the filter row: a wallet is one path through all its signals."""
    app = c.app
    amount = bankroll.wallet_amount(app.db)
    all_views = bankroll.views(c.records, app.cfg, app.db, c.now, amount=amount)
    keys = [w.key for w in all_views]
    src = query.get("src") if query.get("src") in keys else (keys[0] if keys else "")
    view = next((w for w in all_views if w.key == src), None)
    title = f"${amount:,.2f} wallet"
    body = wallet_form(amount, query.get("saved") or "", (query.get("error") or "")[:200]) + scope_note(c)
    params = bankroll.Params.of(app.cfg, amount)
    body += (f'<p class="card-note">{v.esc(params.describe())}. The fee is inside the stake and a stake is never '
             "more than the cash. Open positions are valued at their last recorded best bid, or at the price paid "
             "when none was recorded.</p>")
    if view is None:
        return title, body + v.banner("No signals yet.", "info")
    body += '<nav class="tabs" aria-label="Signal source">' + "".join(
        f'<a href="/bankroll?{urlencode({"src": w.key})}" class="{"on" if w.key == src else ""}">{v.esc(w.label)}</a>'
        for w in all_views) + "</nav>"
    if view.simulated:
        body += v.banner("These signals come from a simulated backtest, not from live paper trading.", "info")
    body += '<div class="grid">'
    if view.key.startswith("bot:"):
        if view.live is None:
            body += v.card("Live wallet", '<p class="empty">The runner has not started a wallet run yet. It does so '
                                          "within a minute of starting.</p>", wide=True)
        else:
            row = view.run_row
            stale = abs(row["amount"] - amount) > 1e-9
            note = (f"{bankroll.run_label(row)}. Only signals since the run began count."
                    + (" The amount was changed: the runner will end this run shortly." if stale else ""))
            body += v.card("Live wallet", wallet_warnings(view.live, c.threshold) + wallet_tiles(view.live)
                           + wallet_chart(view.live), note=note, wide=True)
    replay, bare = view.replay, view.replay_no_cutoff
    if not bare.entries:
        return title, body + v.card("Replay of every signal", '<p class="empty">No signals yet.</p>', wide=True) + "</div>"
    fl = app.cfg.filters
    cutoff = "no cutoff set" if fl.max_days_to_close is None else f"{fl.max_days_to_close:g}-day cutoff"
    body += v.card("Replay of every signal", wallet_warnings(replay, c.threshold) + wallet_tiles(replay)
                   + wallet_chart(replay),
                   note=f"All {len(replay.entries)} signals of this source, in the order they were decided, through "
                        f"today's wallet settings, with the {cutoff}.", wide=True)

    def series(res: Any) -> list:
        return [[int(p.ts), round(p.equity, 2)] for p in _thin(res.curve, CURVE_POINTS)]

    def cell(res: Any, key: str) -> str:
        s = res.summary()
        return {"equity": v.money(s["equity"], signed=False), "ret": v.pct(s["total_return"]),
                "dd": f"{v.money(s['max_drawdown_usd'], signed=False)} ({s['max_drawdown_pct']:.1%})",
                "taken": str(s["taken"]), "skipped": str(sum(s["skipped"].values())),
                "fees": v.money(s["fees"], signed=False), "settled": str(s["settled"])}[key]

    compare = v.chart("line", {"series": [{"name": "With the cutoff", "cls": "s1", "points": series(replay)},
                                          {"name": "Without the cutoff", "cls": "s2", "points": series(bare)}],
                               "y": "usd"},
                      table_headers=["", "With the cutoff", "Without the cutoff"],
                      table_rows=[[name, cell(replay, k), cell(bare, k)] for name, k in (
                          ("Equity", "equity"), ("Total return", "ret"), ("Max drawdown", "dd"), ("Taken", "taken"),
                          ("Skipped", "skipped"), ("Settled", "settled"), ("Fees", "fees"))],
                      legend=[("With the cutoff", "s1"), ("Without the cutoff", "s2")])
    body += v.card("With and without the time-to-close cutoff", compare + v.table(
        ["", "With the cutoff", "Without the cutoff"],
        [[name, cell(replay, k), cell(bare, k)] for name, k in (
            ("Equity", "equity"), ("Total return", "ret"), ("Max drawdown", "dd"), ("Taken", "taken"),
            ("Skipped", "skipped"), ("Settled", "settled"), ("Fees", "fees"))], sortable=False),
        note="One path each. The two wallets take different trades from the first skip on, so a gap between the "
             "lines is mostly the luck of which trades each happened to hold.", wide=True)

    compare_rows = []
    for name, res in (bankroll.stake_comparison(bankroll.signals_by_source(c.records, app.cfg).get(view.key, []),
                                                app.cfg, amount, bankroll.load_marks(app.db), c.now)):
        s = res.summary()
        compare_rows.append([name, (v.money(s["equity"], signed=False), s["equity"]),
                             (v.pct(s["total_return"]), s["total_return"], v.tone(s["total_return"])),
                             (v.money(s["lowest_equity"], signed=False), s["lowest_equity"]),
                             (f"{s['max_drawdown_pct']:.1%}", s["max_drawdown_pct"]), (str(s["taken"]), s["taken"]),
                             (str(sum(s["skipped"].values())), sum(s["skipped"].values())),
                             (v.money(s["staked"], signed=False), s["staked"])])
    body += v.card("Stake sizes compared", v.table(
        ["Stake", "Final equity", "Return", "Lowest equity", "Max drawdown", "Taken", "Skipped", "Total staked"],
        compare_rows, numeric_from=1),
        note="The same signals, with the cutoff, through the automatic stake and through fixed stakes. The automatic "
             "stake bets a fraction of the Kelly stake once the settled signals have shown an edge (100 settled, 95% "
             "interval above zero), and until then either nothing or the smallest order the venue takes. This "
             "describes the past: choosing the best row afterwards is hindsight.",
        wide=True)

    skips = replay.skipped
    body += v.card("Why signals were skipped", v.table(
        ["Reason", "What it means", "Signals"],
        [[code, (SKIP_MEANING.get(code, ""), None, "wrap"), (str(n), n)] for code, n in skips.most_common()],
        empty="Nothing was skipped.", numeric_from=2), note="In the replay with the cutoff.", wide=True)

    recent = replay.entries[-40:][::-1]
    rows = []
    for e in recent:
        what = {"TAKEN": "Taken", "SKIPPED": "Skipped", "MISSED": "Did not fill"}[e.status]
        pnl = e.pnl
        rows.append([iso(int(e.signal.ts))[:16].replace("T", " "), ((e.signal.question or e.signal.event_key)[:60], None, "wrap"),
                     what, (e.reason if e.status != "TAKEN" else f"at {e.price:.3f}", None, "wrap"),
                     v.money(e.stake, signed=False) if e.status == "TAKEN" else "-",
                     v.money(e.equity, signed=False),
                     ("-" if pnl is None else v.money(pnl), pnl or 0, v.tone(pnl) if pnl is not None else "")])
    body += v.card("Latest decisions", v.table(
        ["When (UTC)", "Market", "Decision", "Why / price", "Stake", "Equity before", "P&L"], rows, numeric_from=4),
        note="The 40 most recent signals in the replay with the cutoff, newest first.", wide=True)

    runs = bankroll.past_runs(app.db)
    run_rows = []
    for r in runs:
        s = json.loads(r["summary_json"] or "{}")
        run_rows.append([f"#{r['id']}", r["runner"], v.money(r["amount"], signed=False), iso(r["started_at"])[:10],
                         iso(r["ended_at"])[:10] if r["ended_at"] else "running",
                         (r["end_reason"] or "", None, "wrap"),
                         v.money(s["equity"], signed=False) if s else "-",
                         v.pct(s["total_return"]) if s else "-", str(s.get("taken", "-")) if s else "-"])
    body += v.card("Wallet runs", v.table(
        ["Run", "Runner", "Amount", "Started", "Ended", "Why it ended", "Final equity", "Return", "Taken"], run_rows,
        empty="No run has started yet.", numeric_from=6),
        note="A run ends when the amount or a bankroll setting changes. Its figures are frozen then.", wide=True)
    return title, body + "</div>"


def state(app: Any, key: str) -> Any:
    try:
        return app.db.get_state(key)
    except Exception:  # noqa: BLE001
        return None


def ago(now: int, ts: Any) -> str:
    if ts is None:
        return "never"
    d = now - int(ts)
    return f"{d}s ago" if d < 120 else f"{d // 60} min ago" if d < 7200 else f"{d // 3600} h ago" if d < 2 * DAY else f"{d // DAY} days ago"


def health(c: Ctx) -> str:
    app, f, now = c.app, c.f, c.now
    lo, hi = f.start or 0, f.end or now + DAY
    alerts = app.db.query("SELECT trigger_fill_ts, detect_delay FROM alerts WHERE detect_delay IS NOT NULL "
                          "AND status != 'BACKTEST' AND trigger_fill_ts >= ? AND trigger_fill_ts < ?", (lo, hi))
    quick = [a for a in alerts if a["detect_delay"] <= 600]
    late = len(alerts) - len(quick)
    by: dict[int, list[float]] = defaultdict(list)
    for a in quick:
        by[f.period(a["trigger_fill_ts"])].append(a["detect_delay"])

    def q90(xs: list[float]) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(0.9 * len(xs)))]

    ps = sorted(by)
    everything = [a["detect_delay"] for a in quick]
    beat, det = state(app, "heartbeat"), state(app, "detector_heartbeat")
    review = app.db.one("SELECT id, as_of, created_at FROM score_runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    members = app.db.one("SELECT COUNT(*) n FROM watchlist_membership WHERE removed_at IS NULL")["n"]

    def light(ts: Any, limit: int) -> str:
        return "good" if ts is not None and now - int(ts) <= limit else "bad"

    tiles = "".join([
        v.tile("Runner", ago(now, beat), "last successful step", "up" if light(beat, 300) == "good" else "down"),
        v.tile("Detector", ago(now, det), "last successful poll", "up" if light(det, 300) == "good" else "down"),
        v.tile("Detection delay", f"{median(everything):.1f}s" if everything else "-",
               f"median, n={len(everything)} alerts · target under 5s"),
        v.tile("Seen late", str(late), "alerts first seen over 10 min late"),
        v.tile("Last nightly review", ago(now, review["created_at"]) if review else "never",
               f"snapshot #{review['id']}" if review else ""),
        v.tile("Watchlist", str(members), "wallets watched now"),
    ])
    body = f'<div class="tiles">{tiles}</div><div class="grid">'
    body += v.card(f"Detection delay per {f.group}", v.chart(
        "line",
        {"series": [{"name": "Median", "cls": "s1", "points": [[p, round(median(by[p]), 2), len(by[p])] for p in ps]},
                    {"name": "90th percentile", "cls": "s2", "points": [[p, round(q90(by[p]), 2), len(by[p])] for p in ps]}],
         "bands": c.windows, "y": "sec", "zero": True},
        table_headers=["Period", "Median", "90th percentile", "Alerts"],
        table_rows=[[f.period_label(p), f"{median(by[p]):.1f}s", f"{q90(by[p]):.1f}s", str(len(by[p]))] for p in ps],
        legend=[("Median", "s1"), ("90th percentile", "s2")]),
        note="Seconds from the whale's fill to our detector seeing it. Alerts seen over 10 minutes late (restarts, "
             "outages) are counted in the tile above, not averaged in here.", wide=True)

    fills = app.db.query("SELECT detected_at, source FROM fill_events WHERE detected_at >= ? AND detected_at < ?", (lo, hi))
    per: dict[int, dict[str, int]] = defaultdict(lambda: {"feed": 0, "backstop": 0})
    for e in fills:
        per[f.period(int(e["detected_at"]))][e["source"] if e["source"] in ("feed", "backstop") else "feed"] += 1
    bars = [{"label": f.period_label(p), "v": per[p]["feed"] + per[p]["backstop"], "n": per[p]["backstop"]} for p in sorted(per)]
    body += v.card(f"Whale fills published per {f.group}", v.chart(
        "bars", {"bars": bars, "y": "num", "signed": False, "cls": "s1"},
        table_headers=["Period", "Fills", "Only the backstop saw"],
        table_rows=[[b["label"], str(b["v"]), str(b["n"])] for b in bars]),
        note="A day with far fewer fills than its neighbours, or many that only the backstop saw, means the feed "
             "had trouble. (n in the tooltip = backstop-only fills.)")

    rows = app.db.query("SELECT * FROM outage_windows ORDER BY start_ts DESC") if c.windows else []
    body += v.card("Outage windows", v.table(
        ["From (UTC)", "To (UTC)", "Cause", "Recorded by", "Hours"],
        [[iso(r["start_ts"])[:16], iso(r["end_ts"])[:16] if r["end_ts"] else "still going",
          (r["cause"] or "", None, "wrap"), r["source"], f"{((r['end_ts'] or now) - r['start_ts']) / 3600:.1f}"]
         for r in rows], sortable=False, numeric_from=4, empty="No outage has been recorded."),
        note="Shaded on every time chart. Tick \"Exclude outage windows\" to leave their signals out of the numbers.")
    body += v.card("What this page cannot show", '<p class="card-note">Individual failed requests are written to the '
                   "container logs (<code>docker compose logs detector</code>), not to the database, so they are not "
                   "charted here. The heartbeats above stop when either process stops succeeding.</p>")
    return body + "</div>"


def wallet_page(c: Ctx, wallet: str) -> str:
    c.f.wallet = wallet
    rows = c.scope()
    s = summarise(wallet, traded(rows), samples=SAMPLES)
    m = memberships(c.app).get(wallet)
    status, days, _ = wallet_status(m, c.now)
    clv = clv_values(rows)
    tiles = "".join([
        v.tile("Status", status, "-" if days is None else f"{days:.0f} days on the list"),
        v.tile("P&L of our copies", v.money(s.total_pnl) if s.settled else "-", f"n={s.settled} closed",
               v.tone(s.total_pnl) if s.settled else ""),
        v.tile("ROI", v.pct(s.roi), f"mean per trade {v.pct(s.mean_roi)} {ci_text(s)}", v.tone(s.roi)),
        v.tile("Win rate", "-" if s.win_rate is None else f"{s.win_rate:.0%}", f"n={s.settled} closed"),
        v.tile("CLV", v.cents(mean(clv)), f"n={len(clv)} pre-game fills"),
        v.tile("Signals", str(len(rows)), f"{s.missed} missed · {s.open} open"),
    ])
    body = (f'<p class="card-note"><a href="/wallets?{c.f.qs(wallet="all")}">← All wallets</a> · '
            f'<code>{v.esc(wallet)}</code> · {v.esc(label_of(c.f.strategy))}</p>'
            + size_banner(c, s.settled) + f'<div class="tiles">{tiles}</div><div class="grid">')
    snaps = c.app.db.query(
        "SELECT s.*, r.kind, r.created_at FROM wallet_score_snapshots s JOIN score_runs r ON r.id = s.run_id "
        "WHERE s.wallet = ? AND r.kind IN ('nightly', 'score') ORDER BY s.as_of, s.run_id", (wallet,))
    pts = [[x["as_of"], round(x["roi"], 4)] for x in snaps if x["roi"] is not None]
    body += v.card("Score history: the wallet's own ROI at each nightly review", v.chart(
        "line", {"series": [{"name": "ROI", "cls": "s1", "points": pts}], "bands": c.windows, "y": "pct", "zero": True},
        table_headers=["As of", "ROI", "Settled markets", "Largest market share", "Passed entry rules", "Snapshot"],
        table_rows=[[iso(x["as_of"])[:10], v.pct(x["roi"]), str(x["settled_markets"] or 0),
                     "-" if x["max_single_market_share"] is None else f"{x['max_single_market_share']:.2f}",
                     "yes" if x["passed"] else "no", f"#{x['run_id']}"] for x in snaps],
        empty="This wallet has not been scored yet."),
        note="Its own trading over the lookback, as known at each date. Not our P&L.", wide=True)
    stays = c.app.db.query("SELECT * FROM watchlist_membership WHERE wallet = ? ORDER BY added_at", (wallet,))
    body += v.card("Time on the watchlist", v.table(
        ["Added (UTC)", "Removed (UTC)", "Why added", "Why removed"],
        [[iso(x["added_at"])[:16], iso(x["removed_at"])[:16] if x["removed_at"] else "still on the list",
          (x["add_reason"], None, "wrap"), (x["remove_reason"] or "", None, "wrap")] for x in stays],
        sortable=False, numeric_from=9, empty="Never on the watchlist."), wide=True)
    ordered = sorted(rows, key=lambda r: (-r["alert_ts"], -r["id"]))[:PAGE_ROWS]
    body += v.card("Its signals and our paper trades", v.table(
        [h for h in TRADE_HEADERS if h != "Wallet"], [trade_cells(r, c, with_wallet=False) for r in ordered],
        numeric_from=4), note=f"Newest {len(ordered)} of {len(rows)}.", wide=True)
    return body + "</div>"


# -- the server ---------------------------------------------------------------------------------------


def build_ctx(cfg: Config, db: Database, query: Any, path: str, now: int | None = None) -> Ctx:
    now = now_ts() if now is None else now
    app = SimpleNamespace(db=db, cfg=cfg, clock=lambda: now, wall=lambda: float(now))
    records = load_records(app)
    present = {r["key"] for r in records}
    strategies = [k for k in LABELS if k in present] + sorted(k for k in present if k not in LABELS)
    f = Filters.parse(query, now, strategies)
    live_wallets = sorted({r["wallet"] for r in records if not r["simulated"] and r["wallet"]})
    models = sorted({r.get("model_version") for r in records if "runner" in r}, key=lambda m: (m is None, m or 0))
    windows = outages(app)
    # Every page, the wallet pages included, sees only the bot rows of the chosen model
    # version, without the signals inside outage windows unless asked.
    sc = scope_rows(app, records, Scope(f.model, not f.no_outages), windows=windows)
    return Ctx(app, sc.rows, windows, f, strategies or [DEFAULT_STRATEGY], live_wallets, now,
               cfg.stats.min_settled_for_conclusions, path, scoped=sc, models=models)


def render(cfg: Config, db_path: str, path: str, query: Any, *, now: int | None = None) -> tuple[str, str]:
    """(content type, body) for one GET. Opens the database read-only for this request only."""
    try:
        db = Database.readonly(db_path)
    except Exception as exc:  # noqa: BLE001 - no file yet, or not readable
        return "text/html", v.page("Not ready", path, "", v.banner(
            f"The database cannot be opened yet ({type(exc).__name__}). It appears once the service has started.",
            "bad"))
    try:
        if db.schema_version < SCHEMA_VERSION:
            return "text/html", v.page("Not ready", path, "", v.banner(
                f"The database is at schema v{db.schema_version} and this dashboard needs v{SCHEMA_VERSION}. "
                "Start the updated whale-watcher service once; it backs up and upgrades the database.", "bad"))
        c = build_ctx(cfg, db, query, path, now)
        if path == "/trades.csv":
            return "text/csv", trades_csv(c)
        if path.startswith("/wallet/"):
            wallet = path.rsplit("/", 1)[1].lower()
            title, body, nav = f"Wallet {wallet[:8]}", wallet_page(c, wallet), "/wallets"
            c.path = path
        else:
            pages = {"/": ("Overview", overview), "/edge": ("Edge", edge), "/wallets": ("Wallets", wallets),
                     "/breakdowns": ("Breakdowns", breakdowns), "/health": ("Health", health),
                     "/us": ("Polymarket US", us_page)}
            if path == "/bankroll":
                title, body = bankroll_page(c, query)
                updated = f"Read {iso(c.now)[11:16]} UTC."
                return "text/html", v.page(title, path, "", body, updated=updated)
            if path == "/trades":
                try:
                    page_no = int(query.get("page") or 1)
                except ValueError:
                    page_no = 1
                title, body, nav = "Trades", trades(c, page_no), path
            elif path in pages:
                title, fn = pages[path]
                body, nav = fn(c), path
            else:
                raise web.HTTPNotFound()
        updated = f"Read {iso(c.now)[11:16]} UTC."
        return "text/html", v.page(title, nav, filters_html(c), body, qs=c.f.qs(), updated=updated)
    finally:
        db.close()


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


class SettingsWriter:
    """THE ONLY WRITE THE DASHBOARD CAN MAKE: the ``settings`` table, and in it only
    the wallet amount. Every other connection in this module is read-only. This one
    is opened for a single statement and SQLite itself is told to refuse anything
    that is not an insert or update of ``settings``."""

    ALLOWED = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_READ}

    @classmethod
    def _authorize(cls, action: int, arg1: Any, _arg2: Any, _db: Any, _source: Any) -> int:
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SELECT):
            return sqlite3.SQLITE_OK
        if action in cls.ALLOWED and arg1 == "settings":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    @classmethod
    def connect(cls, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=10)
        conn.set_authorizer(cls._authorize)
        return conn

    @classmethod
    def save_wallet_amount(cls, db_path: str, amount: float, now: int) -> None:
        value = f"{bankroll.parse_amount(amount):.2f}"
        conn = cls.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at, source) VALUES (?,?,?,'dashboard') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                    "source = excluded.source", (bankroll.AMOUNT_KEY, value, now))
        finally:
            conn.close()


def same_site(request: web.Request) -> bool:
    """A form on another website must not be able to change the amount. Browsers say
    where a POST came from; one that does not say is refused."""
    if request.headers.get("Sec-Fetch-Site") == "same-origin":
        return True
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    host = urlsplit(origin).netloc.lower()
    return bool(host) and host in {(request.headers.get("Host") or "").lower(),
                                   (request.headers.get("X-Forwarded-Host") or "").lower()} - {""}


def make_app(cfg: Config, db_path: str) -> web.Application:
    async def save_amount(request: web.Request) -> web.Response:
        if not same_site(request):
            raise web.HTTPForbidden(text="This form can only be sent from the dashboard itself.")
        form = await request.post()
        try:
            amount = bankroll.parse_amount(form.get("amount", ""))
            SettingsWriter.save_wallet_amount(db_path, amount, now_ts())
            target = "/bankroll?" + urlencode({"saved": f"{amount:.2f}"})
        except ValueError as exc:
            target = "/bankroll?" + urlencode({"error": str(exc)})
        except sqlite3.Error as exc:
            log.error("could not save the wallet amount: %s", exc)
            target = "/bankroll?" + urlencode({"error": "the database could not be written just now"})
        raise web.HTTPSeeOther(target, headers=SECURITY_HEADERS)

    async def handle(request: web.Request) -> web.Response:
        kind, body = render(cfg, db_path, request.path, request.query)
        headers = dict(SECURITY_HEADERS)
        if kind == "text/csv":
            headers["Content-Disposition"] = 'attachment; filename="whale-watcher-trades.csv"'
        return web.Response(text=body, content_type=kind, charset="utf-8", headers=headers)

    async def css(_request: web.Request) -> web.Response:
        return web.Response(text=v.CSS, content_type="text/css", charset="utf-8",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=300"})

    async def js(_request: web.Request) -> web.Response:
        return web.Response(text=v.JS, content_type="application/javascript", charset="utf-8",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=300"})

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(client_max_size=4096)     # the one form is a few bytes
    app.router.add_post("/settings/wallet-amount", save_amount)   # the only route that is not a GET
    app.router.add_get("/assets/app.css", css)
    app.router.add_get("/assets/app.js", js)
    app.router.add_get("/healthz", healthz)
    for route in ("/", "/edge", "/wallets", "/trades", "/trades.csv", "/breakdowns", "/health", "/bankroll", "/us",
                  "/wallet/{address}"):
        app.router.add_get(route, handle)       # GET only: none of these can change anything
    return app


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def serve(cfg: Config, *, host: str, port: int, in_container: bool) -> None:
    if host not in LOOPBACK and not in_container:
        raise SystemExit(
            f"refusing to listen on {host}: the dashboard is for the tailnet only. Run it on 127.0.0.1 and expose "
            "it with `tailscale serve`. (--in-container allows it inside Docker, where compose must publish the "
            "port on 127.0.0.1 only.)")
    log.info("dashboard on http://%s:%d (reads %s; can change only the simulated wallet amount)", host, port,
             cfg.database_path)
    web.run_app(make_app(cfg, cfg.database_path), host=host, port=port, print=None, access_log=None)
