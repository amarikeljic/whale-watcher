"""v2.1 Part 1: what every paper trade cost, shown instead of only netted.

The paper model has always charged the taker fee (``cost = price x shares + fee``,
P&L = payout - cost), and mirror exits pay it on the sell as well. What it did not
do is SHOW the costs. This module:

  * recalculates every existing fill into new columns, leaving the original
    ``fees`` / ``cost`` / ``pnl`` untouched so the two can be compared;
  * splits a P&L into gross, fees, slippage and net.

Definitions, per closed trade:

  net       what the account made: payout (or sale proceeds) minus everything paid.
  fees      taker fee on the buy, plus on the sell for a mirror exit. No rebates.
  slippage  dollars paid beyond the best ask because the stake did not fit in the
            top level of the book (and received below the best bid on an exit).
  gross     net + fees + slippage: the trade at the top of the book, free of fees.

The bid-ask spread is not a separate line: the bot buys at the ask, so the spread
is already inside the gross figure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .app import App
from .fees import polymarket_taker_fee, rate_for, table_rate

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Costs:
    n: int = 0
    gross: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    net: float = 0.0


def trade_costs(t: Any) -> tuple[float, float, float, float] | None:
    """(gross, fees, slippage, net) of one closed, filled trade; None otherwise."""
    closed = t["closed_at"] if "closed_at" in t.keys() else t["settled_at"]
    if t["status"] != "FILLED" or closed is None or t["pnl"] is None:
        return None
    fees = (t["fees"] or 0.0) + ((t["exit_fees"] or 0.0) if "exit_fees" in t.keys() else 0.0)
    slip = t["slippage_cost"] or 0.0
    return t["pnl"] + fees + slip, fees, slip, t["pnl"]


def breakdown(rows: list[Any]) -> Costs:
    c = Costs()
    for t in rows:
        parts = trade_costs(t)
        if parts is None:
            continue
        c.n += 1
        c.gross += parts[0]
        c.fees += parts[1]
        c.slippage += parts[2]
        c.net += parts[3]
    return c


def _one_level_fee(shares: float | None, price: float | None, rate: float, exponent: float) -> float:
    if not shares or price is None:
        return 0.0
    return polymarket_taker_fee(shares, price, rate, exponent)


POLICY_KEY = "cost_policy"


def policy(app: App) -> str:
    """What decides a recalculated fee: change either and every trade is costed again."""
    f = app.cfg.fees
    return json.dumps({"rate_source": f.rate_source, "schedules": f.schedules}, sort_keys=True)


def recalculate(app: App) -> dict[str, int]:
    """Cost every fill under the CURRENT fee policy into the ``*_recalc`` columns.

    ``fees`` / ``cost`` / ``pnl`` (what the trade was charged when it was made) are
    never touched, so the two can always be compared. ``fee_rate`` / ``fee_source``
    record that original charge. Reports and the dashboard use the recalculated
    figures, so one fee policy applies to every trade however old.

    Runs for rows that lack the columns, and for every row when the policy changed."""
    current = policy(app)
    everything = app.db.get_state(POLICY_KEY) != current
    done = {"bot_trades": 0, "paper_trades": 0}
    for table, closed_col, ts_col in (("bot_trades", "closed_at", "signal_ts"), ("paper_trades", "settled_at", "alert_ts")):
        where = "" if everything else (f" AND (fees_recalc IS NULL OR ({closed_col} IS NOT NULL "
                                       "AND net_pnl_recalc IS NULL))")
        rows = app.db.query(f"SELECT * FROM {table} WHERE status = 'FILLED'{where}")
        markets = app.markets.get_many({r["condition_id"] for r in rows})
        with app.db.tx() as conn:
            for t in rows:
                market = markets.get(t["condition_id"])
                if market is None or t["fill_price"] is None or not t["contracts"]:
                    continue
                exponent = market.fee_exponent or 1.0
                stored = t["fees"] or 0.0
                charged, charged_by = t["fee_rate"], t["fee_source"]
                if charged is None:         # a trade from before the rate was recorded: infer it from its fee
                    base = t["contracts"] * t["fill_price"] * (1 - t["fill_price"])
                    charged = round(stored / base, 4) if base > 0 else 0.0
                    charged_by = "market"
                rate, _ = rate_for(market, t[ts_col], app.cfg.fees)
                if charged_by == "us":
                    rate = charged      # Polymarket US has its own fee: the international policy does not apply
                slip = t["slippage_cost"]
                leaving = table == "bot_trades" and t["exit_price"] is not None
                if leaving:
                    stored += t["exit_fees"] or 0.0
                if table == "bot_trades" and slip is None and t["best_ask"] is not None:
                    slip = max(0.0, (t["fill_price"] - t["best_ask"]) * t["contracts"])
                if charged > 0:
                    # The fee is proportional to the rate, so the fee actually charged (per
                    # level, to the cent) scales exactly: no approximation, and no change at
                    # all when the policy rate is the rate that was charged.
                    fee = stored * rate / charged
                else:
                    fee = _one_level_fee(t["contracts"], t["fill_price"], rate, exponent)
                    if leaving:
                        fee += _one_level_fee(t["contracts"], t["exit_price"], rate, exponent)
                gross = net = None
                if t[closed_col] is not None and t["pnl"] is not None:
                    gross = t["pnl"] + stored + (slip or 0.0)
                    net = t["pnl"] + stored - fee
                conn.execute(
                    f"UPDATE {table} SET fee_rate = ?, fee_source = ?, slippage_cost = ?, fees_recalc = ?, "
                    "gross_pnl = ?, net_pnl_recalc = ? WHERE id = ?",
                    (charged, charged_by, slip, round(fee, 5), gross, net, t["id"]))
                done[table] += 1
    app.db.set_state(POLICY_KEY, current)
    if any(done.values()):
        log.info("costs recalculated for %d bot trades and %d old-rule trades (fees.rate_source = %s)",
                 done["bot_trades"], done["paper_trades"], app.cfg.fees.rate_source)
    return done


def apply_policy(t: dict) -> dict:
    """For reports: a trade as the current fee policy costs it. The stored values move
    to ``*_charged``; ``pnl``, ``fees`` and ``cost`` become the recalculated ones."""
    if t.get("fees_recalc") is None or t.get("status") != "FILLED":
        return t
    stored = (t.get("fees") or 0.0) + (t.get("exit_fees") or 0.0)
    extra = t["fees_recalc"] - stored
    entry = (t.get("contracts") or 0.0) * (t.get("fill_price") or 0.0) * (1 - (t.get("fill_price") or 0.0))
    leave = (t.get("contracts") or 0.0) * t["exit_price"] * (1 - t["exit_price"]) if t.get("exit_price") is not None else 0.0
    entry_share = entry / (entry + leave) if entry + leave > 0 else 1.0
    t["pnl_charged"], t["fees_charged"], t["cost_charged"] = t.get("pnl"), stored, t.get("cost")
    if t.get("cost") is not None:
        t["cost"] = t["cost"] - (t.get("fees") or 0.0) + t["fees_recalc"] * entry_share
    t["fees"], t["exit_fees"] = t["fees_recalc"] * entry_share, t["fees_recalc"] * (1 - entry_share)
    if t.get("net_pnl_recalc") is not None:
        t["pnl"] = t["net_pnl_recalc"]
    elif t.get("pnl") is not None:
        t["pnl"] = t["pnl"] - extra
    return t


# -- the audit: gross, fee and net side by side ---------------------------------------------------


def audit_rows(app: App, *, strategy: str = "flat_hold", n: int = 5) -> dict[str, Any]:
    rows = app.db.query(
        "SELECT b.*, m.category, m.question, m.fee_rate AS market_rate, m.fees_enabled FROM bot_trades b "
        "LEFT JOIN poly_markets m ON m.condition_id = b.condition_id "
        "WHERE b.status = 'FILLED' AND b.closed_at IS NOT NULL AND b.strategy = ? ORDER BY b.closed_at DESC, b.id DESC",
        (strategy,))
    kind = "bot"
    if not rows:        # nothing closed under the bot model yet: audit the old-rule trades instead
        kind = "old rule"
        rows = app.db.query(
            "SELECT p.*, p.settled_at AS closed_at, p.alert_ts AS signal_ts, m.category, m.question, "
            "m.fee_rate AS market_rate, m.fees_enabled FROM paper_trades p "
            "LEFT JOIN poly_markets m ON m.condition_id = p.condition_id WHERE p.status = 'FILLED' "
            "AND p.settled_at IS NOT NULL AND p.strategy NOT LIKE 'backtest:%' ORDER BY p.settled_at DESC, p.id DESC")
    out = []
    for t in rows:
        parts = trade_costs(t)
        if parts is None:
            continue
        gross, fees, slip, net = parts
        table = table_rate(t["category"], t["signal_ts"], app.cfg.fees)
        # An independent check of the fee that was charged: the formula at the average
        # fill price (and the exit price). Per-level rounding can move it a fraction of a cent.
        by_hand = (t["contracts"] or 0) * (t["fee_rate"] or 0) * t["fill_price"] * (1 - t["fill_price"])
        if "exit_price" in t.keys() and t["exit_price"] is not None:
            by_hand += t["contracts"] * (t["fee_rate"] or 0) * t["exit_price"] * (1 - t["exit_price"])
        out.append({"id": t["id"], "question": t["question"] or t["condition_id"][:16], "category": t["category"],
                    "price": t["fill_price"], "shares": t["contracts"], "rate": t["fee_rate"], "source": t["fee_source"],
                    "table_rate": table, "gross": gross, "fees": fees, "slippage": slip, "net": net,
                    "fees_recalc": t["fees_recalc"], "net_recalc": t["net_pnl_recalc"],
                    "by_hand": by_hand, "differs": abs(by_hand - fees) > 0.01,
                    "policy": app.cfg.fees.rate_source,
                    "rate_differs": table is not None and t["fee_rate"] is not None and abs(table - t["fee_rate"]) > 1e-9})
    return {"kind": kind, "strategy": strategy, "rows": out, "shown": out[:n], "total": breakdown(rows)}


def render_audit(rep: dict[str, Any]) -> str:
    rows, c = rep["rows"], rep["total"]
    lines = [f"Cost audit ({rep['kind']} trades" + (f", {rep['strategy']}" if rep["kind"] == "bot" else "") + ")",
             "  fee = shares x rate x price x (1 - price), charged on every buy and on mirror-exit sells. No rebates."]
    if not rows:
        return "\n".join(lines + ["  No closed trades yet."])
    lines.append("")
    lines.append(f"  {'trade':>6} {'category':<11} {'price':>6} {'shares':>8} {'rate':>6} {'gross P&L':>10} {'fee':>8} "
                 f"{'slippage':>9} {'net P&L':>9}   fee check")
    for r in rep["shown"]:
        check = "charged = formula" if not r["differs"] else (
            f"CHARGED {r['fees']:.4f} BUT THE FORMULA GIVES {r['by_hand']:.4f}")
        if r["rate_differs"]:
            check += f"; table says {r['table_rate']:g}, market charges {r['rate']:g}"
        lines.append(f"  {r['id']:>6} {(r['category'] or '?')[:11]:<11} {r['price']:>6.3f} {r['shares']:>8.2f} "
                     f"{(r['rate'] or 0):>6.3f} {r['gross']:>+10.2f} {-r['fees']:>+8.4f} {-r['slippage']:>+9.4f} "
                     f"{r['net']:>+9.2f}   {check}")
        lines.append(f"         {r['question'][:70]}")
        expect = r["shares"] * (r["rate"] or 0) * r["price"] * (1 - r["price"])
        lines.append(f"         by hand: {r['shares']:.2f} x {(r['rate'] or 0):g} x {r['price']:.3f} x "
                     f"{1 - r['price']:.3f} = {expect:.4f}" + ("" if abs(r["by_hand"] - expect) < 1e-9 else
                                                              f" on the buy, {r['by_hand']:.4f} with the sell"))
    lines.append("")
    lines.append(f"  All {c.n} closed trades: gross {c.gross:+,.2f} | fees -{c.fees:,.2f} | slippage -{c.slippage:,.2f} "
                 f"| net {c.net:+,.2f}")
    bad = sum(1 for r in rows if r["differs"])
    off_table = sum(1 for r in rows if r["rate_differs"])
    recalc = sum(r["fees_recalc"] or 0.0 for r in rows)
    lines.append(f"  The table above is what each trade was CHARGED when it was made. Recalculated under today's "
                 f"policy (fees.rate_source = {rows[0]['policy']}): fees -{recalc:,.2f} | net "
                 f"{c.gross - recalc - c.slippage:+,.2f}. Reports use the recalculated figures.")
    lines.append(f"  The fee charged differs from the formula by more than a cent on {bad} of {len(rows)} trades.")
    lines.append(f"  The rate charged differs from the config table on {off_table} of {len(rows)} trades.")
    return "\n".join(lines)
