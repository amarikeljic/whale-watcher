"""Phase 3: paper trade every alert at the price available on the next poll.

Fill rule (config ``paper``):
1. At alert time, record the best ask (``alert_ask``).
2. ``reaction_polls`` polls later, fetch the live book and walk the asks for
   ``stake_usd`` of notional. The fill price is the VWAP of that walk, not the
   midpoint or the last trade.
3. Add the venue's taker fees (``fees.fee_model``).
4. MISSED if the market has closed, the book cannot fill the stake, or the
   VWAP is more than ``max_slippage`` above ``alert_ask``. Missed trades stay in
   the stats: in real life they would have been missed too.

Shadow fills, never part of the headline numbers: zero-latency (the whale's own
VWAP) and alert-time (the best ask when the alert fired). Same stake, same fees.

Settlement pays each share the venue's actual payout: 1 or 0, or 0.50 on a
Polymarket "50-50" resolution (draws, no contests, cancelled fights). Polymarket
does not refund a voided market; a REFUND result is kept for venues that do.
``pnl = payout - cost`` with ``cost = fill_price * contracts + fees``.

Status is PENDING until the fill poll, then FILLED or MISSED for good;
settlement fills in ``settled_at`` / ``settle_result`` / ``pnl`` and leaves the
status alone, so every alert has exactly one FILLED-or-MISSED record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .app import App
from .fees import fee_model
from .polymarket import Book, MarketInfo

log = logging.getLogger(__name__)

VENUE = "polymarket"
STRATEGY = "hold_to_resolution"


@dataclass(slots=True)
class Walk:
    vwap: float
    contracts: float
    fills: list[tuple[float, float]]   # (price, shares) per level
    levels: int


def walk_asks(book: Book, stake_usd: float, *, limit_price: float | None = None) -> Walk | None:
    """Spend ``stake_usd`` against the asks, best first. None if the book (up to
    ``limit_price``, when given) cannot absorb the whole stake."""
    remaining = stake_usd
    fills: list[tuple[float, float]] = []
    for level in book.asks:
        if limit_price is not None and level.price > limit_price + 1e-12:
            break
        if remaining <= 1e-9:
            break
        shares = min(level.size, remaining / level.price)
        if shares <= 0:
            continue
        fills.append((level.price, shares))
        remaining -= shares * level.price
    if remaining > 1e-6 or not fills:
        return None
    contracts = sum(s for _, s in fills)
    return Walk(stake_usd / contracts, contracts, fills, len(fills))


def shadow_fill(price: float | None, stake: float, market: MarketInfo) -> tuple[float, float, float, float] | None:
    """(price, contracts, fees, cost) for a hypothetical fill at one price."""
    if price is None or not 0 < price < 1:
        return None
    contracts = stake / price
    fees = fee_model(VENUE)([(price, contracts)], market)
    return price, contracts, fees, stake + fees


class PaperEngine:
    def __init__(self, app: App) -> None:
        self.app = app
        self.cfg = app.cfg.paper

    # -- opening ---------------------------------------------------------------------

    def open(self, alert_id: int, *, poll_counter: int, market: MarketInfo) -> int:
        """Create the PENDING paper trade for an alert (once; idempotent)."""
        a = self.app.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        stake = self.cfg.stake_usd
        zero = shadow_fill(a["whale_price"], stake, market)
        atask = shadow_fill(a["alert_best_ask"], stake, market)
        alert_ts = a["posted_at"] or a["detected_at"]
        now = self.app.clock()
        cur = self.app.db.execute(
            "INSERT OR IGNORE INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, "
            "stake_usd, due_poll, alert_ts, alert_ask, zero_price, zero_contracts, zero_fees, zero_cost, "
            "atask_price, atask_contracts, atask_fees, atask_cost, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (alert_id, VENUE, STRATEGY, "PENDING", a["token_id"], a["condition_id"], stake,
             poll_counter + self.cfg.reaction_polls, alert_ts, a["alert_best_ask"],
             *(zero or (None,) * 4), *(atask or (None,) * 4), now))
        return int(cur.lastrowid or 0)

    # -- filling ----------------------------------------------------------------------

    async def process_due(self, poll_counter: int) -> list[int]:
        """Fill every PENDING trade whose poll has come. Returns their ids."""
        due = self.app.db.query("SELECT * FROM paper_trades WHERE status = 'PENDING' AND due_poll <= ?",
                                (poll_counter,))
        done = []
        for t in due:
            try:
                await self._fill(t)
                done.append(t["id"])
            except Exception as exc:  # leave it PENDING; the next poll tries again
                log.warning("paper fill for trade %s failed: %s", t["id"], exc)
        return done

    async def _fill(self, t) -> None:
        app = self.app
        now = app.clock()
        market = (await app.markets.ensure([t["condition_id"]], refresh_after=0)).get(t["condition_id"])
        if market is None:
            return self._miss(t, "market metadata unavailable", now)
        if market.closed or market.resolved or market.accepting_orders is False:
            return self._miss(t, "market closed before the fill", now)
        book = await app.clob.book(t["token_id"])
        if book is None or not book.asks:
            return self._miss(t, "no asks on the book at fill time", now)
        walk = walk_asks(book, t["stake_usd"])
        if walk is None:
            return self._miss(t, f"book too thin for ${t['stake_usd']:g}", now)
        if t["alert_ask"] is not None and walk.vwap - t["alert_ask"] > self.cfg.max_slippage + 1e-9:
            return self._miss(
                t, f"slippage: fill {walk.vwap:.3f} vs alert ask {t['alert_ask']:.3f} "
                   f"(max {self.cfg.max_slippage:g})", now, price=walk.vwap)
        fees = fee_model(VENUE)(walk.fills, market)
        cost = walk.vwap * walk.contracts + fees
        app.db.execute(
            "UPDATE paper_trades SET status = 'FILLED', fill_ts = ?, fill_price = ?, contracts = ?, fees = ?, "
            "cost = ?, levels_walked = ?, updated_at = ? WHERE id = ? AND status = 'PENDING'",
            (now, walk.vwap, walk.contracts, fees, cost, walk.levels, now, t["id"]))

    def _miss(self, t, reason: str, now: int, *, price: float | None = None) -> None:
        self.app.db.execute(
            "UPDATE paper_trades SET status = 'MISSED', miss_reason = ?, fill_ts = ?, fill_price = ?, "
            "updated_at = ? WHERE id = ? AND status = 'PENDING'", (reason, now, price, now, t["id"]))

    # -- settling ------------------------------------------------------------------------

    async def settle(self) -> int:
        """Settle every unsettled FILLED trade (and the shadows of MISSED ones) whose market resolved."""
        app = self.app
        rows = app.db.query("SELECT * FROM paper_trades WHERE settled_at IS NULL AND status IN ('FILLED', 'MISSED')")
        if not rows:
            return 0
        markets = await app.markets.ensure({r["condition_id"] for r in rows}, refresh_after=self.cfg.settle_interval_seconds)
        settled = 0
        for t in rows:
            market = markets.get(t["condition_id"])
            if market is None or not market.resolved or market.payouts is None:
                continue
            index = market.outcome_index_of(t["token_id"])
            pps = market.payout_for(index)
            if pps is None:
                continue
            closing = None
            ref = market.close_reference_ts()
            if ref is not None:
                point = await app.prices.at(t["token_id"], ref - 1)
                closing = point.price if point is not None else None
            values = settle_values(t, pps)
            app.db.execute(
                "UPDATE paper_trades SET settled_at = ?, settle_result = ?, payout_per_share = ?, payout = ?, "
                "pnl = ?, zero_pnl = ?, atask_pnl = ?, closing_price = ?, updated_at = ? WHERE id = ?",
                (app.clock(), values["result"], pps, values["payout"], values["pnl"], values["zero_pnl"],
                 values["atask_pnl"], closing, app.clock(), t["id"]))
            settled += 1
        return settled


def result_label(payout_per_share: float, *, refunded: bool = False) -> str:
    if refunded:
        return "REFUND"
    if payout_per_share >= 0.999:
        return "WIN"
    if payout_per_share <= 0.001:
        return "LOSS"
    return "SPLIT"


def settle_values(t, payout_per_share: float, *, refunded: bool = False) -> dict:
    """P&L for one paper trade and its shadows. A MISSED trade has no position: pnl 0.

    ``refunded`` is for venues that void and return the cost; Polymarket never
    does (it pays a fraction per share instead), so it is False there.
    """
    def pnl(contracts, cost):
        if contracts is None or cost is None:
            return None
        return 0.0 if refunded else contracts * payout_per_share - cost

    filled = t["status"] == "FILLED"
    payout = (t["cost"] if refunded else t["contracts"] * payout_per_share) if filled else None
    return {
        "result": result_label(payout_per_share, refunded=refunded) if filled else None,
        "payout": payout,
        "pnl": (payout - t["cost"]) if filled else 0.0,
        "zero_pnl": pnl(t["zero_contracts"], t["zero_cost"]),
        "atask_pnl": pnl(t["atask_contracts"], t["atask_cost"]),
    }
