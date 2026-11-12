"""v2 Phase 4: the dashboard. A small web app that READS the database and shows it.

  * It never writes: the database is opened read-only and query-only, every route
    is a GET, and there is no form that changes anything.
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
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from . import dashview as v
from .config import Config
from .db import SCHEMA_VERSION, Database
from .stats import (bucket, event_key, in_play, load_bot_trades, load_trades, summarise)
from .stats_util import mean, median
from .timeutil import iso, now_ts, parse_as_of, start_of_day

log = logging.getLogger(__name__)

DAY = 86_400
SAMPLES = 2000            # bootstrap draws per interval: enough for a dashboard, light on a home server
PAGE_ROWS = 100
LABELS = {
    "single/flat_hold": "Bot: flat stake, hold",
    "single/flat_mirror": "Bot: flat stake, mirror exits",
    "single/scaled_hold": "Bot: scaled stake, hold",
    "single/placebo_late": "Control: same outcome, later",
    "old/hold_to_resolution": "Old rule: human delay",
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
        t.update(key=f"{t['runner']}/{t['strategy']}", market_venue="polymarket", simulated=False)
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
    try:
        rows = app.db.query("SELECT start_ts, end_ts FROM outage_windows ORDER BY start_ts")
    except Exception:  # noqa: BLE001 - a database older than the table: no windows known
        return []
    now = app.clock()
    return [(r["start_ts"], r["end_ts"] or now) for r in rows]


@dataclass
class Filters:
    preset: str = "all"
    start: int | None = None
    end: int | None = None           # exclusive
    group: str = "day"
    strategy: str = DEFAULT_STRATEGY
    venue: str = "all"
    wallet: str = "all"
    no_outages: bool = False
    since: str = ""
    until: str = ""

    @classmethod
    def parse(cls, q: Any, now: int, strategies: list[str]) -> Filters:
        f = cls()
        f.group = "week" if q.get("group") == "week" else "day"
        f.venue = q.get("venue") or "all"
        f.wallet = (q.get("wallet") or "all").lower()
        f.no_outages = q.get("outages") == "exclude"
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
        if self.no_outages:
            p["outages"] = "exclude"
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
    return (f'<form class="filters" method="get" action="{c.path}">'
            f'<label>Range<span class="presets">{presets}'
            f'<a href="{c.path}?{f.qs(range="custom")}" class="{"on" if f.preset == "custom" else ""}">Custom</a></span></label>'
            f'{hidden}{custom}'
            f'<label>Strategy{select("strategy", [(k, label_of(k)) for k in c.strategies], f.strategy)}</label>'
            f'<label>Group by{select("group", [("day", "Day"), ("week", "Week")], f.group)}</label>'
            f'<label>Venue{select("venue", [("all", "All venues"), ("polymarket", "Polymarket")], f.venue)}</label>'
            f'<label>Wallet{select("wallet", wallets, f.wallet)}</label>'
            f'<label class="check"><input type="checkbox" name="outages" value="exclude"'
            f'{" checked" if f.no_outages else ""}>Exclude outage windows</label></form>')


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
    skipped = len(rows) - len(tr)
    open_cost = sum(r["cost"] or 0.0 for r in tr if r["status"] == "FILLED" and r["settled_at"] is None)
    n = f"n={s.settled} closed"
    tiles = "".join([
        v.tile("Total P&L", v.money(s.total_pnl) if s.settled else "-", n, v.tone(s.total_pnl) if s.settled else ""),
        v.tile("ROI", v.pct(s.roi), f"on ${s.total_cost:,.0f} staked, {n}", v.tone(s.roi)),
        v.tile("Win rate", "-" if s.win_rate is None else f"{s.win_rate:.0%}", n),
        v.tile("Max drawdown", f"${s.max_drawdown:,.2f}" if s.settled else "-", n),
        v.tile("Open exposure", f"${open_cost:,.2f}", f"{s.open} open positions, at cost"),
        v.tile("Signals", str(len(rows)), f"{s.settled} closed · {s.open} open · {s.missed} missed · {skipped} skipped"),
    ])
    kind, note = ci_note(s, c.threshold)
    body = size_banner(c, s.settled) + f'<div class="tiles">{tiles}</div>' + v.banner(note, kind)

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
    body += '<div class="grid">' + v.card(
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
    return body + "</div>"


def histogram(values: list[float], lo: float = -0.2, hi: float = 0.2, step: float = 0.02) -> tuple[list[dict], int]:
    n = round((hi - lo) / step)
    counts = [0] * n
    for x in values:
        counts[min(n - 1, max(0, int((x - lo) / step)))] += 1
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


def breakdown_table(c: Ctx, rows: list[dict], key: Any, first: str, *, limit: int = 0, order: str = "n") -> tuple[str, list]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(key(r))].append(r)
    stats = [(g, summarise(g, rs, samples=600)) for g, rs in groups.items()]
    stats.sort(key=(lambda x: x[0]) if order == "label" else (lambda x: (-x[1].settled, -x[1].alerts, x[0])))
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
    return Ctx(app, records, outages(app), f, strategies or [DEFAULT_STRATEGY], live_wallets, now,
               cfg.stats.min_settled_for_conclusions, path)


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
                     "/breakdowns": ("Breakdowns", breakdowns), "/health": ("Health", health)}
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


def make_app(cfg: Config, db_path: str) -> web.Application:
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

    app = web.Application()
    app.router.add_get("/assets/app.css", css)
    app.router.add_get("/assets/app.js", js)
    app.router.add_get("/healthz", healthz)
    for route in ("/", "/edge", "/wallets", "/trades", "/trades.csv", "/breakdowns", "/health", "/wallet/{address}"):
        app.router.add_get(route, handle)       # GET only: nothing here can change anything
    return app


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def serve(cfg: Config, *, host: str, port: int, in_container: bool) -> None:
    if host not in LOOPBACK and not in_container:
        raise SystemExit(
            f"refusing to listen on {host}: the dashboard is for the tailnet only. Run it on 127.0.0.1 and expose "
            "it with `tailscale serve`. (--in-container allows it inside Docker, where compose must publish the "
            "port on 127.0.0.1 only.)")
    log.info("dashboard on http://%s:%d (read-only; database %s)", host, port, cfg.database_path)
    web.run_app(make_app(cfg, cfg.database_path), host=host, port=port, print=None, access_log=None)
