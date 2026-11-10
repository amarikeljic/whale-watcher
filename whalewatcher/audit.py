"""Audit settled live paper trades, their shadow fills, and the stats built on them.

    python -m whalewatcher audit [--trades 5] [--random] [--id ID ...]

For each settled live paper trade it rebuilds, from the stored fills and prices:
  the whale's side (the outcome bought) and what it paid; the whale's price three ways:
  the VWAP of the fill prices, USDC per share (which includes the whale's taker fees),
  and the zero_price stored with the paper trade; the alert-time best ask; the paper fill;
  and for each method (zero-latency, alert-time ask, real fill) its contracts, fees, cost
  and P&L, recomputed by hand next to what is stored.

Then it checks every settled live trade for token mix-ups, P&L that does not follow from
its own price, contracts and payout, fees that do not match the fee model, missed trades
with a position or without shadows, a whale price that includes the whale's fees, and CLV
measured against a price from before the fill. Finally it splits the zero-latency vs
alert-time gap into the trades that make it.

Read-only: it never writes to the database.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .app import App
from .fees import fee_model
from .polymarket import MarketInfo
from .stats_util import mean

VENUE = "polymarket"
TOL = 0.02      # dollars: stored vs hand-computed P&L
PRICE_TOL = 0.002


@dataclass
class Check:
    trade: dict[str, Any]
    market: MarketInfo | None
    fills: list[dict[str, Any]]
    exec_vwap: float | None = None       # fill prices, fees excluded
    usdc_vwap: float | None = None       # USDC per share, the whale's taker fees included
    pps: float | None = None             # payout per share of the outcome the trade holds
    methods: dict[str, dict[str, float | None]] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    @property
    def gap(self) -> float:
        t = self.trade
        return (t["atask_pnl"] or 0.0) - (t["zero_pnl"] or 0.0)


def _settled_live(app: App) -> list[dict[str, Any]]:
    return [dict(r) for r in app.db.query(
        "SELECT p.*, a.wallet, a.outcome, a.token_id AS alert_token, a.whale_price AS alert_whale_price, "
        "a.whale_shares, a.whale_usd, a.fills AS alert_fill_count, a.trigger_fill_ts, a.first_fill_ts, "
        "a.alert_best_ask, a.alert_best_bid, a.detected_at "
        "FROM paper_trades p JOIN alerts a ON a.id = p.alert_id "
        "WHERE p.strategy NOT LIKE 'backtest:%' AND p.settled_at IS NOT NULL ORDER BY p.id")]


def _fills(app: App, alert_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in app.db.query(
        "SELECT w.* FROM alert_fills f JOIN wallet_trades w ON w.fill_key = f.fill_key "
        "WHERE f.alert_id = ? ORDER BY w.ts, w.id", (alert_id,))]


def check(app: App, t: dict[str, Any]) -> Check:
    market = app.markets.get(t["condition_id"])
    c = Check(t, market, _fills(app, t["alert_id"]))
    stake = t["stake_usd"]
    fees_of = fee_model(VENUE)

    # Which outcome the trade holds, and what it paid.
    index = market.outcome_index_of(t["token_id"]) if market else None
    c.pps = market.payout_for(index) if market else None
    if t["token_id"] != t["alert_token"]:
        c.flags.append("paper trade's token differs from the alert's token")
    tokens = {f["token_id"] for f in c.fills}
    if tokens and tokens != {t["token_id"]}:
        c.flags.append(f"the alert's fills are on another token: {sorted(tokens)}")
    if market and index is None:
        c.flags.append("the token is not one of the market's outcomes")
    if c.pps is not None and t["payout_per_share"] is not None and abs(c.pps - t["payout_per_share"]) > 1e-6:
        c.flags.append(f"stored payout {t['payout_per_share']} differs from the market's {c.pps}")
    pps = t["payout_per_share"] if t["payout_per_share"] is not None else c.pps

    # The whale's price, from the fills up to the one that triggered the alert.
    upto = [f for f in c.fills if t["trigger_fill_ts"] is None or f["ts"] <= t["trigger_fill_ts"]]
    shares = sum(f["size"] or 0 for f in upto)
    if shares > 0:
        c.exec_vwap = sum((f["price"] or 0) * (f["size"] or 0) for f in upto) / shares
        c.usdc_vwap = sum(f["usdc_size"] or 0 for f in upto) / shares
        zp = t["zero_price"]
        if zp is not None and abs(zp - c.exec_vwap) > PRICE_TOL:
            if abs(zp - c.usdc_vwap) <= PRICE_TOL:
                c.flags.append(f"zero_price {zp:.4f} is USDC per share (whale's fee included); fill-price VWAP "
                               f"{c.exec_vwap:.4f}: the zero-latency shadow pays a fee twice")
            else:
                c.flags.append(f"zero_price {zp:.4f} matches neither fill-price VWAP {c.exec_vwap:.4f} nor "
                               f"USDC per share {c.usdc_vwap:.4f}")
    elif t["alert_fill_count"]:
        c.flags.append("the alert's fills are not in wallet_trades, so its price cannot be rebuilt")

    # Each method, by hand.
    def shadow(name: str, price, contracts, fees, cost, pnl) -> None:
        row: dict[str, float | None] = {"price": price, "contracts": contracts, "fees": fees, "cost": cost,
                                        "pnl": pnl, "pnl_hand": None}
        if price is not None and market is not None and 0 < price < 1:
            n = stake / price
            f = fees_of([(price, n)], market)
            row.update(contracts_hand=n, fees_hand=f, cost_hand=stake + f)
            if pps is not None:
                row["pnl_hand"] = n * pps - (stake + f)
            if contracts is not None and abs(contracts - n) > 1e-6 * max(1, n):
                c.flags.append(f"{name}: contracts {contracts:.4f} != stake / price {n:.4f}")
            if fees is not None and abs(fees - f) > 1e-4:
                c.flags.append(f"{name}: fees {fees:.5f} != fee model {f:.5f}")
        if row["pnl_hand"] is not None and pnl is not None and abs(row["pnl_hand"] - pnl) > TOL:
            c.flags.append(f"{name}: stored P&L {pnl:+.2f} != {row['pnl_hand']:+.2f} by hand")
        c.methods[name] = row

    shadow("zero-latency", t["zero_price"], t["zero_contracts"], t["zero_fees"], t["zero_cost"], t["zero_pnl"])
    shadow("alert-time ask", t["atask_price"], t["atask_contracts"], t["atask_fees"], t["atask_cost"], t["atask_pnl"])
    if t["atask_price"] != t["alert_best_ask"]:
        c.flags.append(f"atask_price {t['atask_price']} differs from the alert's best ask {t['alert_best_ask']}")

    real: dict[str, float | None] = {"price": t["fill_price"], "contracts": t["contracts"], "fees": t["fees"],
                                     "cost": t["cost"], "pnl": t["pnl"], "pnl_hand": None}
    if t["status"] == "FILLED":
        if None in (t["fill_price"], t["contracts"], t["cost"]):
            c.flags.append("FILLED without price, contracts or cost")
        else:
            if abs(t["fill_price"] * t["contracts"] + (t["fees"] or 0) - t["cost"]) > 1e-4:
                c.flags.append("real fill: cost != price x contracts + fees")
            if market is not None:
                f = fees_of([(t["fill_price"], t["contracts"])], market)
                if t["fees"] is not None and abs(t["fees"] - f) > 0.01:   # per-level rounding allowed
                    c.flags.append(f"real fill: fees {t['fees']:.5f} far from the fee model {f:.5f}")
            if pps is not None:
                real["pnl_hand"] = t["contracts"] * pps - t["cost"]
                if t["pnl"] is not None and abs(real["pnl_hand"] - t["pnl"]) > TOL:
                    c.flags.append(f"real fill: stored P&L {t['pnl']:+.2f} != {real['pnl_hand']:+.2f} by hand")
    elif t["status"] == "MISSED":
        real["pnl_hand"] = 0.0
        if t["contracts"] is not None:
            c.flags.append("MISSED trade holds contracts")
        if t["pnl"] not in (0, 0.0, None):
            c.flags.append(f"MISSED trade has P&L {t['pnl']}")
        if t["zero_pnl"] is None or t["atask_pnl"] is None:
            c.flags.append("MISSED trade without both shadows: it drops out of the shadow comparison")
    c.methods["real fill"] = real

    # CLV against a price from before the fill.
    if (t["closing_price"] is not None and t["fill_ts"] and market and market.game_start_time
            and t["fill_ts"] >= market.game_start_time):
        c.flags.append("CLV compares an in-play fill with the pre-game price (from before the fill)")
    return c


@dataclass
class AuditReport:
    checks: list[Check]
    shown: list[Check]


def run_audit(app: App, *, n: int = 5, pick: str = "gap", ids: list[int] | None = None,
              seed: int = 7) -> AuditReport:
    checks = [check(app, t) for t in _settled_live(app)]
    if ids:
        shown = [c for c in checks if c.trade["id"] in set(ids)]
    elif pick == "random":
        shown = random.Random(seed).sample(checks, min(n, len(checks)))
    else:   # the trades that make the zero-latency vs alert-time gap
        shown = sorted(checks, key=lambda c: -abs(c.gap))[:n]
    return AuditReport(checks, shown)


def monitored_wallets(app: App) -> list[dict[str, Any]]:
    """Each monitored wallet with the snapshot that selected it and the filters that run used."""
    import json
    out = []
    for r in app.db.query(
            "SELECT w.wallet, w.run_id, w.added_at, s.roi, s.settled_markets, s.max_single_market_share, "
            "s.profitable_months, s.passed, s.selected, s.fail_reasons, r.as_of, r.config_json "
            "FROM monitored_wallets w LEFT JOIN wallet_score_snapshots s ON s.run_id = w.run_id AND s.wallet = w.wallet "
            "LEFT JOIN score_runs r ON r.id = w.run_id WHERE w.active = 1 ORDER BY s.roi"):
        d = dict(r)
        try:
            used = json.loads(d.pop("config_json") or "{}")     # score_runs store the scoring section
            d["min_roi_used"] = used.get("min_roi", (used.get("scoring") or {}).get("min_roi"))
        except ValueError:
            d["min_roi_used"] = None
        out.append(d)
    return out


def _f(x: float | None, spec: str = ".3f") -> str:
    return "-" if x is None else format(x, spec)


def render(rep: AuditReport, wallets: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    if wallets is not None:
        lines += _render_wallets(wallets)
    checks = rep.checks
    if not checks:
        return "\n".join(lines + ["No settled live paper trades in this database yet."])
    lines.append(f"{len(checks)} settled live paper trades. Showing {len(rep.shown)}:")
    lines.append("")
    lines.append(f"  {'trade':>6s}  {'side bought':18s} {'paid':>5s}  {'whale px':>8s} {'+fee':>6s} {'stored':>7s}"
                 f"  {'alert ask':>9s} {'fill':>6s}  {'P&L zero':>9s} {'atask':>8s} {'real':>8s}  status")
    for c in rep.shown:
        t = c.trade
        lines.append(
            f"  {t['id']:>6d}  {(t['outcome'] or '?')[:18]:18s} {_f(c.pps, '.2f'):>5s}  {_f(c.exec_vwap):>8s} "
            f"{_f(c.usdc_vwap):>6s} {_f(t['zero_price']):>7s}  {_f(t['alert_best_ask']):>9s} {_f(t['fill_price']):>6s}"
            f"  {_f(t['zero_pnl'], '+.2f'):>9s} {_f(t['atask_pnl'], '+.2f'):>8s} {_f(t['pnl'], '+.2f'):>8s}  {t['status']}")
    for c in rep.shown:
        t = c.trade
        q = c.market.question if c.market else t["condition_id"]
        lines.append("")
        lines.append(f"  trade {t['id']} (alert {t['alert_id']}, wallet {t['wallet']}): bought \"{t['outcome']}\" "
                     f"on \"{q[:70]}\"; paid {_f(c.pps, '.2f')} per share; {t['status']}"
                     + (f" ({t['miss_reason']})" if t["miss_reason"] else ""))
        lines.append(f"    whale: {len(c.fills)} fills; up to the trigger, fill-price VWAP {_f(c.exec_vwap, '.4f')}, "
                     f"USDC per share {_f(c.usdc_vwap, '.4f')}; stored zero_price {_f(t['zero_price'], '.4f')}")
        lines.append(f"    book at the alert: best ask {_f(t['alert_best_ask'])}, best bid {_f(t['alert_best_bid'])}; "
                     f"paper fill {_f(t['fill_price'])}")
        lines.append(f"    {'method':15s} {'price':>7s} {'contracts':>10s} {'fees':>7s} {'cost':>7s} "
                     f"{'P&L stored':>11s} {'by hand':>8s}")
        for name, m in c.methods.items():
            lines.append(f"    {name:15s} {_f(m['price']):>7s} {_f(m['contracts'], '.2f'):>10s} {_f(m['fees'], '.4f'):>7s} "
                         f"{_f(m['cost'], '.2f'):>7s} {_f(m['pnl'], '+.2f'):>11s} {_f(m['pnl_hand'], '+.2f'):>8s}")
        for flag in c.flags:
            lines.append(f"    ! {flag}")

    lines.append("")
    lines.append(f"Checks over all {len(checks)} settled live trades:")
    kinds = Counter(f.split(":")[0].split(" ")[0] if f.startswith(("zero-latency", "alert-time", "real fill"))
                    else f.split(" (")[0].split(";")[0][:60] for c in checks for f in c.flags)
    flagged = sum(1 for c in checks if c.flags)
    lines.append(f"  trades with at least one flag: {flagged}")
    for k, v in kinds.most_common():
        lines.append(f"    {v:4d}  {k}")

    shadow = [c for c in checks if c.trade["zero_pnl"] is not None and c.trade["atask_pnl"] is not None]
    if shadow:
        zero = sum(c.trade["zero_pnl"] for c in shadow)
        atask = sum(c.trade["atask_pnl"] for c in shadow)
        real = sum(c.trade["pnl"] or 0.0 for c in shadow)
        lines.append("")
        lines.append(f"Shadow comparison over {len(shadow)} settled alerts (missed included): zero-latency "
                     f"{zero:+.2f} | alert-time ask {atask:+.2f} | real fill {real:+.2f}")
        cheaper = [c for c in shadow if c.trade["atask_price"] is not None and c.trade["zero_price"] is not None
                   and c.trade["atask_price"] < c.trade["zero_price"] - 1e-9]
        lines.append(f"  alert-time ask below the whale's price on {len(cheaper)} of {len(shadow)} alerts; "
                     f"they account for {sum(c.gap for c in cheaper):+.2f} of the {atask - zero:+.2f} gap")
        for label, keep in (("won", lambda c: (c.trade["payout_per_share"] or 0) >= 0.999),
                            ("lost", lambda c: (c.trade["payout_per_share"] or 0) <= 0.001)):
            g = [c for c in shadow if keep(c)]
            lines.append(f"  on {label} alerts ({len(g)}): gap {sum(c.gap for c in g):+.2f}")
        top = sorted(shadow, key=lambda c: -abs(c.gap))[:5]
        lines.append("  largest contributors: " + ", ".join(f"trade {c.trade['id']} {c.gap:+.2f}" for c in top)
                     + f" (together {sum(c.gap for c in top):+.2f})")

    filled = [c.trade for c in checks if c.trade["status"] == "FILLED" and c.trade["fill_price"] is not None]
    old = mean([t["fill_price"] - t["alert_whale_price"] for t in filled if t["alert_whale_price"] is not None])
    new = mean([t["fill_price"] - t["zero_price"] for t in filled if t["zero_price"] is not None])
    det = mean([c.trade["alert_best_ask"] - c.trade["zero_price"] for c in checks
                if c.trade["alert_best_ask"] is not None and c.trade["zero_price"] is not None])
    lines.append("")
    lines.append(f"Latency cost per share: as the old stats computed it (fill - the alert's latest whale price, "
                 f"filled only) {_f(old, '+.4f')} | fill - whale price at the alert {_f(new, '+.4f')} | "
                 f"alert-time ask - whale price, missed included {_f(det, '+.4f')}")
    inplay = [c for c in checks if any(f.startswith("CLV compares") for f in c.flags)]
    pre = [c.trade for c in checks if c.trade["closing_price"] is not None and c not in inplay
           and c.trade["fill_price"] is not None]
    lines.append(f"CLV: {len(inplay)} in-play fills were compared with the pre-game price (mean "
                 f"{_f(mean([c.trade['closing_price'] - c.trade['fill_price'] for c in inplay]), '+.3f')}); "
                 f"{len(pre)} other fills average {_f(mean([t['closing_price'] - t['fill_price'] for t in pre]), '+.3f')}")
    return "\n".join(lines)


def _render_wallets(wallets: list[dict[str, Any]]) -> list[str]:
    lines = [f"Monitored wallets ({len(wallets)}), with the snapshot that selected each and the min_roi that run used:",
             f"  {'wallet':44s} {'run':>4s} {'as_of':10s} {'ROI':>7s} {'settled':>7s} {'max share':>9s} "
             f"{'passed':>6s} {'min_roi used':>12s}"]
    from .timeutil import iso
    for w in wallets:
        lines.append(f"  {w['wallet']:44s} {w['run_id']:>4d} {(iso(w['as_of'])[:10] if w['as_of'] else '-'):10s} "
                     f"{_f(w['roi'], '+.1%'):>7s} {w['settled_markets'] if w['settled_markets'] is not None else '-':>7} "
                     f"{_f(w['max_single_market_share'], '.2f'):>9s} {('yes' if w['passed'] else 'no'):>6s} "
                     f"{_f(w['min_roi_used'], '+.2f'):>12s}")
    low = [w for w in wallets if w["roi"] is not None and w["min_roi_used"] is not None and w["roi"] < w["min_roi_used"]]
    if low:
        lines.append(f"  ! {len(low)} monitored wallet(s) have a snapshot ROI below the min_roi of the run that selected them")
    lines.append("  (ROI here is the wallet's own ROI on settled markets in the lookback, the number the filter tests. "
                 "The stats' per-wallet ROI is the ROI of our copies of it, a different number.)")
    lines.append("")
    return lines
