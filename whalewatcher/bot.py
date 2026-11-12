"""v2 Phase 3: the bot model. Paper trading as a bot would do it: act the moment a
whale fill is detected, against the order book as it is at that instant.

PAPER ONLY. Nothing here places an order, signs anything or holds a key. A "fill"
is arithmetic on a public order book snapshot.

Strategies, all run on the same signals and each with its own paper account
(bankroll, exposure, ledger, P&L):

  flat_hold     fixed stake, held to resolution. The headline.
  flat_mirror   fixed stake, sold into the bids once the whale has sold
                ``mirror_exit_fraction`` of what it held when we entered.
  scaled_hold   stake grows with the size of the whale's buy, up to a cap.
  placebo_late  the control: same outcome, same stake, same costs, entered at a
                random later moment that has nothing to do with the whale's timing.
                If copying carries information, flat_hold must beat it on the
                signals both filled. (Another study's copy edge shrank by more than
                half against exactly this kind of control.)

Fill rule: a fill-or-kill limit order. Walk the asks, best first, for the stake,
taking no level above ``whale price + max_slippage``. Fees are charged per level
(``fees.py``). Every signal leaves one row per strategy, whatever happened:

  FILLED   the book absorbed the stake inside the limit
  MISSED   it could not: price already ran, book too thin, market closed, or we
           were too late to call it "seconds after the whale"
  SKIPPED  a risk limit said no: bankroll, market or event exposure, daily loss

Comparison columns on the hold strategies, same stake, never in the headline:
``zero_*`` (the whale's own price, as if we had no latency at all) and ``human_*``
(the old rule: a book walk ``human_delay_seconds`` later).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from .app import App
from .config import BOT_STRATEGIES, StrategyConfig
from .fees import rate_for, taker_fee
from .paper import VENUE, Walk, result_label, shadow_fill, walk_asks
from .polymarket import Book, MarketInfo
from .timeutil import start_of_day

log = logging.getLogger(__name__)

HOLD = ("flat_hold", "scaled_hold")      # strategies that carry the comparison columns
EXIT_RETRY_SECONDS = 5.0
MARKOUT_SETTLE_SECONDS = 120             # let the price series catch up before reading it


def walk_bids(book: Book, shares: float, *, floor_price: float | None = None) -> Walk | None:
    """Sell ``shares`` into the bids, best first. None if the book (down to
    ``floor_price``, when given) cannot absorb all of them."""
    remaining = shares
    fills: list[tuple[float, float]] = []
    for level in book.bids:
        if floor_price is not None and level.price < floor_price - 1e-12:
            break
        if remaining <= 1e-9:
            break
        take = min(level.size, remaining)
        if take <= 0:
            continue
        fills.append((level.price, take))
        remaining -= take
    if remaining > 1e-6 or not fills:
        return None
    proceeds = sum(p * s for p, s in fills)
    return Walk(proceeds / shares, shares, fills, len(fills))


def slippage(walk: Walk | None, best_ask: float | None) -> float | None:
    """Dollars paid beyond the best ask: what walking past the top of the book cost."""
    if walk is None or best_ask is None:
        return None
    return max(0.0, (walk.vwap - best_ask) * walk.contracts)


@dataclass(slots=True)
class Account:
    strategy: str
    starting: float
    open_cost: float        # cost of positions not yet closed
    realized: float         # P&L of closed positions, all time
    today: float            # ...closed since 00:00 UTC

    @property
    def cash(self) -> float:
        return self.starting + self.realized - self.open_cost

    @property
    def equity(self) -> float:
        """Bankroll with open positions counted at cost."""
        return self.starting + self.realized


@dataclass(slots=True)
class Decision:
    status: str
    reason: str | None = None
    walk: Walk | None = None
    fees: float = 0.0
    would_pay: float | None = None      # what an unlimited walk would have cost, on a miss
    fee_rate: float | None = None       # the taker rate charged, and where it came from
    fee_source: str | None = None


class Bot:
    def __init__(self, app: App, runner: str = "single") -> None:
        self.app = app
        self.runner = runner
        self.cfg = app.cfg.bot
        self._last_markouts = 0.0

    def strategy_cfg(self, strategy: str) -> StrategyConfig:
        return getattr(self.cfg, strategy)

    def enabled(self) -> list[str]:
        return [s for s in BOT_STRATEGIES if self.strategy_cfg(s).enabled]

    # -- the account -------------------------------------------------------------------

    def account(self, strategy: str) -> Account:
        today = start_of_day(self.app.clock())
        r = self.app.db.one(
            "SELECT COALESCE(SUM(CASE WHEN closed_at IS NULL THEN cost END), 0) AS open_cost, "
            "COALESCE(SUM(CASE WHEN closed_at IS NOT NULL THEN pnl END), 0) AS realized, "
            "COALESCE(SUM(CASE WHEN closed_at >= ? THEN pnl END), 0) AS today "
            "FROM bot_trades WHERE runner = ? AND strategy = ? AND status = 'FILLED'",
            (today, self.runner, strategy))
        return Account(strategy, self.strategy_cfg(strategy).starting_bankroll, r["open_cost"], r["realized"],
                       r["today"])

    def exposure(self, strategy: str, column: str, value: str) -> float:
        assert column in ("condition_id", "event_key")
        return self.app.db.one(
            f"SELECT COALESCE(SUM(cost), 0) AS c FROM bot_trades WHERE runner = ? AND strategy = ? "
            f"AND status = 'FILLED' AND closed_at IS NULL AND {column} = ?", (self.runner, strategy, value))["c"]

    def stake_for(self, strategy: str, whale_usd: float) -> float:
        c = self.strategy_cfg(strategy)
        if strategy == "scaled_hold":
            return round(min(c.max_stake_usd, max(c.stake_usd, c.stake_usd * whale_usd / self.cfg.scale_whale_usd)), 2)
        return min(c.stake_usd, c.max_stake_usd)

    def limit_reason(self, strategy: str, stake: float, condition_id: str, event_key: str) -> str | None:
        """The risk limit that forbids this entry, or None."""
        c = self.strategy_cfg(strategy)
        acct = self.account(strategy)
        if acct.today <= -c.daily_loss_limit_usd:
            return (f"daily loss limit: lost ${-acct.today:,.2f} today (limit ${c.daily_loss_limit_usd:,.0f}); "
                    "new entries paused until 00:00 UTC")
        if acct.cash < stake:
            return f"bankroll: ${acct.cash:,.2f} free, stake ${stake:,.2f}"
        in_market = self.exposure(strategy, "condition_id", condition_id)
        if in_market + stake > c.max_market_exposure_usd + 1e-9:
            return (f"market exposure: ${in_market:,.2f} open in this market, limit "
                    f"${c.max_market_exposure_usd:,.0f}")
        in_event = self.exposure(strategy, "event_key", event_key)
        if in_event + stake > c.max_event_exposure_usd + 1e-9:
            return f"event exposure: ${in_event:,.2f} open in this event, limit ${c.max_event_exposure_usd:,.0f}"
        return None

    # -- the buy decision ------------------------------------------------------------------

    def decide_buy(self, book: Book | None, stake: float, market: MarketInfo | None, *,
                   limit_price: float | None) -> Decision:
        """Can a fill-or-kill limit order for ``stake`` fill on this book?"""
        if market is None:
            return Decision("MISSED", "market metadata unavailable")
        if market.closed or market.resolved or market.accepting_orders is False:
            return Decision("MISSED", "market closed")
        if book is None or not book.asks:
            return Decision("MISSED", "no asks on the book")
        walk = walk_asks(book, stake, limit_price=limit_price)
        if walk is None:
            free = walk_asks(book, stake)
            if free is None:
                return Decision("MISSED", f"book too thin for ${stake:g}")
            return Decision("MISSED", f"price ran: filling ${stake:g} costs {free.vwap:.3f} a share, "
                                      f"limit {limit_price:.3f}", would_pay=free.vwap)
        if book.min_order_size and walk.contracts < book.min_order_size - 1e-9:
            return Decision("MISSED", f"below the minimum order size ({walk.contracts:.1f} of "
                                      f"{book.min_order_size:g} shares)")
        rate, source = rate_for(market, self.app.clock())
        return Decision("FILLED", None, walk, taker_fee(walk.fills, rate, market.fee_exponent or 1.0),
                        fee_rate=rate, fee_source=source)

    # -- entering on a signal ----------------------------------------------------------------

    def enter(self, alert: Any, market: MarketInfo | None, book: Book | None, *, detected_at: float,
              decided_at: float) -> dict[str, str]:
        """One row per enabled strategy for this signal. Returns strategy -> status.
        Idempotent: a signal seen twice (a restart mid-way) changes nothing."""
        results: dict[str, str] = {}
        signal_ts = alert["trigger_fill_ts"] or alert["last_fill_ts"]
        event_key = (market.event_slug if market else None) or alert["condition_id"]
        whale_price = alert["whale_price"]
        age = decided_at - signal_ts
        for strategy in self.enabled():
            stake = self.stake_for(strategy, alert["whale_usd"])
            if strategy == "placebo_late":
                rng = random.Random(f"{self.runner}:{alert['id']}")     # reproducible, unrelated to the whale
                due = decided_at + rng.uniform(self.cfg.placebo_min_delay_seconds, self.cfg.placebo_max_delay_seconds)
                self._insert(strategy, alert, event_key, "PENDING", None, signal_ts, detected_at, decided_at,
                             stake, whale_price, book, None, due_at=due)
                results[strategy] = "PENDING"
                continue
            if age > self.cfg.max_signal_age_seconds:
                decision = Decision("MISSED", f"too late: {age:.0f}s after the whale's fill "
                                              f"(limit {self.cfg.max_signal_age_seconds:g}s)")
            else:
                blocked = self.limit_reason(strategy, stake, alert["condition_id"], event_key)
                decision = (Decision("SKIPPED", blocked) if blocked else
                            self.decide_buy(book, stake, market, limit_price=whale_price + self.cfg.max_slippage))
            compare = strategy in HOLD and decision.status != "SKIPPED" and market is not None
            zero = shadow_fill(whale_price, stake, market) if compare else None
            human_due = (decided_at + self.cfg.human_delay_seconds
                         if compare and age <= self.cfg.max_signal_age_seconds else None)
            self._insert(strategy, alert, event_key, decision.status, decision.reason, signal_ts, detected_at,
                         decided_at, stake, whale_price, book, decision, zero=zero, human_due=human_due)
            results[strategy] = decision.status
        return results

    def _insert(self, strategy: str, alert: Any, event_key: str, status: str, reason: str | None, signal_ts: int,
                detected_at: float, decided_at: float, stake: float, whale_price: float, book: Book | None,
                decision: Decision | None, *, due_at: float | None = None,
                zero: tuple[float, float, float, float] | None = None, human_due: float | None = None) -> None:
        walk = decision.walk if decision else None
        fees = decision.fees if decision and walk else None
        self.app.db.execute(
            "INSERT OR IGNORE INTO bot_trades (runner, strategy, alert_id, wallet, condition_id, token_id, event_key, "
            "status, reason, signal_ts, detected_at, decided_at, due_at, stake_usd, whale_price, best_ask, "
            "fill_price, contracts, fees, cost, levels, zero_price, zero_contracts, zero_cost, human_due, "
            "human_done, whale_shares, updated_at, fee_rate, fee_source, slippage_cost) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.runner, strategy, alert["id"], alert["wallet"], alert["condition_id"], alert["token_id"], event_key,
             status, reason, signal_ts, detected_at, decided_at, due_at, stake, whale_price,
             book.best_ask if book else None,
             walk.vwap if walk else (decision.would_pay if decision else None),
             walk.contracts if walk else None, fees,
             (walk.vwap * walk.contracts + fees) if walk else None, walk.levels if walk else None,
             zero[0] if zero else None, zero[1] if zero else None, zero[3] if zero else None,
             human_due, 0 if human_due is not None else 1, alert["whale_shares"], self.app.clock(),
             decision.fee_rate if decision and walk else None, decision.fee_source if decision and walk else None,
             slippage(walk, book.best_ask if book else None)))

    def set_whale_position(self, alert_id: int, shares: float | None) -> None:
        """What the whale held in the outcome when we entered: the base for mirror exits."""
        if shares and shares > 0:
            self.app.db.execute("UPDATE bot_trades SET whale_shares = ? WHERE runner = ? AND alert_id = ?",
                                (shares, self.runner, alert_id))

    # -- mirror exits -----------------------------------------------------------------------

    def on_whale_sell(self, wallet: str, token_id: str, shares: float, ts: int) -> int:
        """The whale sold. Marks the mirror positions that should now be sold; returns how many."""
        db = self.app.db
        db.execute(
            "UPDATE bot_trades SET whale_sold = whale_sold + ? WHERE runner = ? AND strategy = 'flat_mirror' "
            "AND status = 'FILLED' AND closed_at IS NULL AND wallet = ? AND token_id = ? AND signal_ts <= ?",
            (shares, self.runner, wallet, token_id, ts))
        cur = db.execute(
            "UPDATE bot_trades SET exit_due = 1 WHERE runner = ? AND strategy = 'flat_mirror' AND status = 'FILLED' "
            "AND closed_at IS NULL AND exit_due = 0 AND wallet = ? AND token_id = ? "
            "AND whale_sold >= ? * COALESCE(whale_shares, 0) - 1e-9 AND whale_sold > 0",
            (self.runner, wallet, token_id, self.cfg.mirror_exit_fraction))
        return cur.rowcount

    async def _exits(self, now: float) -> int:
        app = self.app
        rows = app.db.query(
            "SELECT * FROM bot_trades WHERE runner = ? AND exit_due = 1 AND closed_at IS NULL AND status = 'FILLED' "
            "AND (exit_tried_at IS NULL OR exit_tried_at <= ?)", (self.runner, now - EXIT_RETRY_SECONDS))
        done = 0
        books: dict[str, Book | None] = {}
        for t in rows:
            if t["token_id"] not in books:
                try:
                    books[t["token_id"]] = await app.clob.book(t["token_id"])
                except Exception as exc:  # noqa: BLE001 - try again on the next tick
                    log.warning("exit book for %s failed: %s", t["token_id"], exc)
                    continue
            book = books[t["token_id"]]
            walk = walk_bids(book, t["contracts"]) if book is not None else None
            if walk is None:
                # No book (the market closed) or bids too thin for the whole position:
                # keep trying; if it resolves first it settles like a held position.
                app.db.execute("UPDATE bot_trades SET exit_tried_at = ? WHERE id = ?", (now, t["id"]))
                continue
            market = app.markets.get(t["condition_id"])
            # A mirror exit sells into the bids: it is a taker trade and pays the fee too.
            rate = rate_for(market, app.clock())[0] if market is not None else 0.0
            fees = taker_fee(walk.fills, rate, (market.fee_exponent or 1.0) if market else 1.0)
            proceeds = walk.vwap * walk.contracts
            below_best = max(0.0, (book.best_bid - walk.vwap) * walk.contracts) if book.best_bid is not None else 0.0
            app.db.execute(
                "UPDATE bot_trades SET exit_at = ?, exit_price = ?, exit_fees = ?, proceeds = ?, closed_at = ?, "
                "result = 'EXIT', pnl = ?, exit_tried_at = ?, updated_at = ?, exit_best_bid = ?, "
                "slippage_cost = COALESCE(slippage_cost, 0) + ? WHERE id = ? AND closed_at IS NULL",
                (now, walk.vwap, fees, proceeds, app.clock(), proceeds - fees - t["cost"], now, app.clock(),
                 book.best_bid, below_best, t["id"]))
            done += 1
        return done

    # -- things that happen later ---------------------------------------------------------------

    async def tick(self) -> dict[str, int]:
        """Placebo entries that are due, human-delay comparisons, exits, markouts."""
        now = self.app.wall()
        out = {"placebo": await self._placebos(now), "human": await self._humans(now),
               "exits": await self._exits(now), "markouts": 0}
        if now - self._last_markouts >= 60:
            self._last_markouts = now
            out["markouts"] = await self._markouts(now)
        return out

    async def _placebos(self, now: float) -> int:
        app = self.app
        rows = app.db.query("SELECT * FROM bot_trades WHERE runner = ? AND status = 'PENDING' AND due_at <= ?",
                            (self.runner, now))
        for t in rows:
            try:
                market = (await app.markets.ensure([t["condition_id"]], refresh_after=300)).get(t["condition_id"])
                blocked = self.limit_reason(t["strategy"], t["stake_usd"], t["condition_id"], t["event_key"])
                if blocked:
                    decision, book = Decision("SKIPPED", blocked), None
                else:
                    book = await app.clob.book(t["token_id"])
                    # No whale price to anchor to at a random moment: the limit only guards depth.
                    limit = book.best_ask + self.cfg.max_slippage if book and book.best_ask is not None else None
                    decision = self.decide_buy(book, t["stake_usd"], market, limit_price=limit)
            except Exception as exc:  # noqa: BLE001 - leave it PENDING; the next tick tries again
                log.warning("placebo entry %s failed: %s", t["id"], exc)
                continue
            walk = decision.walk
            app.db.execute(
                "UPDATE bot_trades SET status = ?, reason = ?, decided_at = ?, best_ask = ?, fill_price = ?, "
                "contracts = ?, fees = ?, cost = ?, levels = ?, updated_at = ?, fee_rate = ?, fee_source = ?, "
                "slippage_cost = ? WHERE id = ? AND status = 'PENDING'",
                (decision.status, decision.reason, now, book.best_ask if book else None,
                 walk.vwap if walk else decision.would_pay, walk.contracts if walk else None,
                 decision.fees if walk else None, (walk.vwap * walk.contracts + decision.fees) if walk else None,
                 walk.levels if walk else None, app.clock(), decision.fee_rate if walk else None,
                 decision.fee_source if walk else None, slippage(walk, book.best_ask if book else None), t["id"]))
        return len(rows)

    async def _humans(self, now: float) -> int:
        """The old rule as a comparison: the same stake, walked ``human_delay_seconds``
        after the signal, missed if the fill is more than paper.max_slippage above the
        best ask at signal time."""
        app = self.app
        rows = app.db.query("SELECT * FROM bot_trades WHERE runner = ? AND human_done = 0 AND human_due <= ?",
                            (self.runner, now))
        books: dict[str, Book | None] = {}
        for t in rows:
            try:
                if t["token_id"] not in books:
                    books[t["token_id"]] = await app.clob.book(t["token_id"])
                market = (await app.markets.ensure([t["condition_id"]], refresh_after=300)).get(t["condition_id"])
            except Exception as exc:  # noqa: BLE001 - try again on the next tick
                log.warning("human-delay comparison %s failed: %s", t["id"], exc)
                continue
            book = books[t["token_id"]]
            price = contracts = cost = None
            open_market = market is not None and not (market.closed or market.resolved or market.accepting_orders is False)
            walk = walk_asks(book, t["stake_usd"]) if book is not None and open_market else None
            if walk is not None and not (t["best_ask"] is not None
                                         and walk.vwap - t["best_ask"] > app.cfg.paper.max_slippage + 1e-9):
                fees = taker_fee(walk.fills, rate_for(market, app.clock())[0], market.fee_exponent or 1.0)
                price, contracts, cost = walk.vwap, walk.contracts, walk.vwap * walk.contracts + fees
            app.db.execute("UPDATE bot_trades SET human_done = 1, human_price = ?, human_contracts = ?, human_cost = ? "
                           "WHERE id = ?", (price, contracts, cost, t["id"]))
        return len(rows)

    async def _markouts(self, now: float) -> int:
        app = self.app
        horizon = self.cfg.markout_seconds
        rows = app.db.query(
            "SELECT id, token_id, decided_at FROM bot_trades WHERE runner = ? AND status = 'FILLED' "
            "AND markout_done = 0 AND decided_at <= ? LIMIT 40", (self.runner, now - horizon - MARKOUT_SETTLE_SECONDS))
        for t in rows:
            try:
                point = await app.prices.at(t["token_id"], int(t["decided_at"]) + horizon)
            except Exception as exc:  # noqa: BLE001 - try again in a minute
                log.warning("markout for %s failed: %s", t["id"], exc)
                continue
            app.db.execute("UPDATE bot_trades SET markout_done = 1, markout_price = ? WHERE id = ?",
                           (point.price if point is not None else None, t["id"]))
        return len(rows)

    # -- settlement -------------------------------------------------------------------------------

    async def settle(self) -> int:
        """Close every position (and settle every comparison) whose market has resolved."""
        app = self.app
        rows = app.db.query("SELECT * FROM bot_trades WHERE runner = ? AND closed_at IS NULL "
                            "AND status IN ('FILLED', 'MISSED')", (self.runner,))
        if not rows:
            return 0
        markets = await app.markets.ensure({r["condition_id"] for r in rows},
                                           refresh_after=app.cfg.paper.settle_interval_seconds)
        settled = 0
        for t in rows:
            market = markets.get(t["condition_id"])
            if market is None or not market.resolved or market.payouts is None:
                continue
            pps = market.payout_for(market.outcome_index_of(t["token_id"]))
            if pps is None:
                continue
            filled = t["status"] == "FILLED"
            closing = None
            ref = market.close_reference_ts()
            in_play = bool(market.game_start_time and t["decided_at"] >= market.game_start_time)
            if filled and ref is not None and not in_play:
                point = await app.prices.at(t["token_id"], ref - 1)
                closing = point.price if point is not None else None

            def shadow(contracts: float | None, cost: float | None, pps: float = pps) -> float | None:
                return None if contracts is None or cost is None else contracts * pps - cost

            human = shadow(t["human_contracts"], t["human_cost"])
            if human is None and t["human_due"] is not None and t["human_done"]:
                human = 0.0     # the old rule would have missed this one
            app.db.execute(
                "UPDATE bot_trades SET closed_at = ?, result = ?, payout_per_share = ?, pnl = ?, zero_pnl = ?, "
                "human_pnl = ?, closing_price = ?, updated_at = ? WHERE id = ? AND closed_at IS NULL",
                (app.clock(), result_label(pps) if filled else None, pps,
                 (t["contracts"] * pps - t["cost"]) if filled else 0.0,
                 shadow(t["zero_contracts"], t["zero_cost"]), human, closing, app.clock(), t["id"]))
            settled += 1
        return settled
