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
  SKIPPED  a rule said no before the book was looked at: the time-to-close cutoff
           (``too_long``, ``no_end_date``; see ``horizon.py``) or a risk limit
           (bankroll, market or event exposure, daily loss)

Comparison columns on the hold strategies, same stake, never in the headline:
``zero_*`` (the whale's own price, as if we had no latency at all) and ``human_*``
(the old rule: a book walk ``human_delay_seconds`` later).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

from . import followup
from .app import App
from .bankroll import HEADLINE
from .config import BOT_STRATEGIES, StrategyConfig
from .fees import rate_for, taker_fee
from .horizon import close_skip, days_to_close, resolution_type, scheduled_end
from .paper import Walk, result_label, rounded, shadow_fill, walk_asks
from .precision import SHARE_STEP, amount_decimals, floor_step
from .polymarket import Book, MarketInfo
from .timeutil import start_of_day

log = logging.getLogger(__name__)

HOLD = ("flat_hold", "scaled_hold")      # strategies that carry the comparison columns
EXIT_RETRY_SECONDS = 5.0
BOOK_LEVELS_KEPT = 25           # ask levels stored with each signal
SKIPPED_SETTLE_SECONDS = 3600   # how often skipped signals' markets are checked for a result
MARKOUT_SETTLE_SECONDS = 120             # let the price series catch up before reading it
# Part 4 #5: (seconds after our fill, column, its done flag). None: bot.markout_seconds.
MARKOUTS = ((60, "markout_1m", "markout_1m_done"), (None, "markout_price", "markout_done"),
            (1800, "markout_30m", "markout_30m_done"))


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
        self._last_marks = float("-inf")             # mark on the first tick
        self._tasks: set[asyncio.Task] = set()       # second book reads in flight (Part 4 #6)
        self._last_skipped_settle = float("-inf")    # and look at skipped signals on the first settle

    def strategy_cfg(self, strategy: str) -> StrategyConfig:
        return getattr(self.cfg, strategy)

    def enabled(self) -> list[str]:
        return [s for s in BOT_STRATEGIES if self.strategy_cfg(s).enabled]

    def limit_for(self, strategy: str, whale_price: float) -> float:
        """The fill-or-kill limit: the whale's price plus the strategy's own chase limit,
        or the bot's (tight_hold tests a second one)."""
        own = self.strategy_cfg(strategy).max_slippage
        return whale_price + (self.cfg.max_slippage if own is None else own)

    # -- the account -------------------------------------------------------------------

    def account(self, strategy: str) -> Account:
        today = start_of_day(self.app.clock())
        r = self.app.db.one(
            "SELECT COALESCE(SUM(CASE WHEN closed_at IS NULL THEN cost END), 0) AS open_cost, "
            "COALESCE(SUM(CASE WHEN closed_at IS NOT NULL THEN COALESCE(net_pnl_recalc, pnl) END), 0) AS realized, "
            "COALESCE(SUM(CASE WHEN closed_at >= ? THEN COALESCE(net_pnl_recalc, pnl) END), 0) AS today "
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
            return (f"daily loss limit: lost ${-acct.today:,.2f} since 00:00 UTC (limit ${c.daily_loss_limit_usd:,.0f}); "
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
        # In the amounts Polymarket accepts: shares in 0.01 steps, the amount at its precision.
        walk = rounded(walk, decimals=amount_decimals(book.tick_size))
        if walk is None:
            return Decision("MISSED", f"below the minimum order size (less than {SHARE_STEP:g} share)")
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
        # The time-to-close cutoff comes before anything else and applies to every
        # strategy, the placebo included: a control that may enter markets the real
        # strategies may not is not a control.
        end = scheduled_end(market.end_date, market.game_start_time) if market else None
        horizon = (end, days_to_close(end, decided_at))
        too_far = close_skip(end, decided_at, self.app.cfg.filters) if market is not None else None
        if too_far is None:     # Part 4 #10, #11: off unless a followup switch is on
            too_far = followup.skip(self.app, alert)
        for strategy in self.enabled():
            stake = self.stake_for(strategy, alert["whale_usd"])
            if too_far is not None:
                self._insert(strategy, alert, event_key, "SKIPPED", too_far[1], signal_ts, detected_at, decided_at,
                             stake, whale_price, book, None, skip_code=too_far[0], horizon=horizon)
                results[strategy] = "SKIPPED"
                continue
            if strategy == "placebo_late":
                rng = random.Random(f"{self.runner}:{alert['id']}")     # reproducible, unrelated to the whale
                due = decided_at + rng.uniform(self.cfg.placebo_min_delay_seconds, self.cfg.placebo_max_delay_seconds)
                self._insert(strategy, alert, event_key, "PENDING", None, signal_ts, detected_at, decided_at,
                             stake, whale_price, book, None, due_at=due, horizon=horizon)
                results[strategy] = "PENDING"
                continue
            if age > self.cfg.max_signal_age_seconds:
                decision = Decision("MISSED", f"too late: {age:.0f}s after the whale's fill "
                                              f"(limit {self.cfg.max_signal_age_seconds:g}s)")
            else:
                blocked = self.limit_reason(strategy, stake, alert["condition_id"], event_key)
                decision = (Decision("SKIPPED", blocked) if blocked else
                            self.decide_buy(book, stake, market, limit_price=self.limit_for(strategy, whale_price)))
            compare = strategy in HOLD and decision.status != "SKIPPED" and market is not None
            zero = shadow_fill(whale_price, stake, market) if compare else None
            human_due = (decided_at + self.cfg.human_delay_seconds
                         if compare and age <= self.cfg.max_signal_age_seconds else None)
            self._insert(strategy, alert, event_key, decision.status, decision.reason, signal_ts, detected_at,
                         decided_at, stake, whale_price, book, decision, zero=zero, human_due=human_due,
                         horizon=horizon)
            results[strategy] = decision.status
        return results

    def _insert(self, strategy: str, alert: Any, event_key: str, status: str, reason: str | None, signal_ts: int,
                detected_at: float, decided_at: float, stake: float, whale_price: float, book: Book | None,
                decision: Decision | None, *, due_at: float | None = None,
                zero: tuple[float, float, float, float] | None = None, human_due: float | None = None,
                skip_code: str | None = None,
                horizon: tuple[int | None, float | None] = (None, None)) -> None:
        walk = decision.walk if decision else None
        fees = decision.fees if decision and walk else None
        self.app.db.execute(
            "INSERT OR IGNORE INTO bot_trades (runner, strategy, alert_id, wallet, condition_id, token_id, event_key, "
            "status, reason, signal_ts, detected_at, decided_at, due_at, stake_usd, whale_price, best_ask, "
            "fill_price, contracts, fees, cost, levels, zero_price, zero_contracts, zero_cost, human_due, "
            "human_done, whale_shares, updated_at, fee_rate, fee_source, slippage_cost, skip_code, "
            "scheduled_end, days_to_close, asks_json, limit_price, min_order_size, tick_size) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.runner, strategy, alert["id"], alert["wallet"], alert["condition_id"], alert["token_id"], event_key,
             status, reason, signal_ts, detected_at, decided_at, due_at, stake, whale_price,
             book.best_ask if book else None,
             walk.vwap if walk else (decision.would_pay if decision else None),
             walk.contracts if walk else None, fees,
             (walk.vwap * walk.contracts + fees) if walk else None, walk.levels if walk else None,
             zero[0] if zero else None, zero[1] if zero else None, zero[3] if zero else None,
             human_due, 0 if human_due is not None else 1, alert["whale_shares"], self.app.clock(),
             decision.fee_rate if decision and walk else None, decision.fee_source if decision and walk else None,
             slippage(walk, book.best_ask if book else None), skip_code, horizon[0], horizon[1],
             # The asks as they were, so an order of any size can be priced later (bankroll.py).
             json.dumps([[lv.price, lv.size] for lv in book.asks[:BOOK_LEVELS_KEPT]]) if book and book.asks else None,
             self.limit_for(strategy, whale_price) if strategy != "placebo_late" and whale_price is not None else None,
             book.min_order_size if book else None, book.tick_size if book else None))

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
            # Only whole 0.01-share steps can be sold; a fraction below that (under a cent) is dropped.
            walk = (rounded(walk_bids(book, floor_step(t["contracts"], SHARE_STEP)),
                            decimals=amount_decimals(book.tick_size)) if book is not None else None)
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
        if now - self._last_marks >= self.app.cfg.bankroll.mark_interval_seconds:
            self._last_marks = now
            out["marks"] = await self.mark_positions()
        return out

    async def mark_positions(self) -> int:
        """Record the best bid of every open position, so the wallet's equity can be
        marked to what the position would sell for. A book with no bids records
        nothing: the last mark stands (a market about to resolve often has none)."""
        app = self.app
        tokens = [r["token_id"] for r in app.db.query(
            "SELECT DISTINCT token_id FROM bot_trades WHERE runner = ? AND status = 'FILLED' AND closed_at IS NULL",
            (self.runner,))]
        now, done = int(app.clock()), 0
        for token in tokens:
            try:
                book = await app.clob.book(token)
            except Exception as exc:  # noqa: BLE001 - a mark is not worth stopping for
                log.debug("no mark for %s: %s", token, exc)
                continue
            if book is None or book.best_bid is None:
                continue
            app.db.execute("INSERT OR IGNORE INTO position_marks (token_id, ts, best_bid, bids_json) VALUES (?,?,?,?)",
                           (token, now, book.best_bid,
                            json.dumps([[lv.price, lv.size] for lv in book.bids[:BOOK_LEVELS_KEPT]])))
            done += 1
        return done

    async def settle_skipped(self) -> int:
        """A signal the fixed-stake strategy skipped still has an outcome, and the wallet
        may have taken it (its limits are its own). Record that outcome on the row:
        ``payout_per_share`` and ``resolved_at`` only. The row stays SKIPPED, with no
        P&L, and counts in no total. Checked hourly: these can be months away."""
        app = self.app
        if app.wall() - self._last_skipped_settle < SKIPPED_SETTLE_SECONDS:
            return 0
        self._last_skipped_settle = app.wall()
        rows = app.db.query("SELECT id, condition_id, token_id FROM bot_trades WHERE runner = ? AND strategy = ? "
                            "AND status = 'SKIPPED' AND payout_per_share IS NULL", (self.runner, HEADLINE))
        if not rows:
            return 0
        # The pass itself is hourly; within it, metadata as fresh as any settlement uses.
        markets = await app.markets.ensure({r["condition_id"] for r in rows},
                                           refresh_after=app.cfg.paper.settle_interval_seconds)
        done = 0
        for t in rows:
            market = markets.get(t["condition_id"])
            if market is None or not market.resolved or market.payouts is None:
                continue
            pps = market.payout_for(market.outcome_index_of(t["token_id"]))
            if pps is None:
                continue
            app.db.execute("UPDATE bot_trades SET payout_per_share = ?, resolved_at = ?, updated_at = ? "
                           "WHERE id = ? AND status = 'SKIPPED' AND payout_per_share IS NULL",
                           (pps, market.resolved_at, app.clock(), t["id"]))
            done += 1
        return done

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
            walk = (rounded(walk_asks(book, t["stake_usd"]), decimals=amount_decimals(book.tick_size))
                    if book is not None and open_market else None)
            if walk is not None and not (t["best_ask"] is not None
                                         and walk.vwap - t["best_ask"] > app.cfg.paper.max_slippage + 1e-9):
                fees = taker_fee(walk.fills, rate_for(market, app.clock())[0], market.fee_exponent or 1.0)
                price, contracts, cost = walk.vwap, walk.contracts, walk.vwap * walk.contracts + fees
            app.db.execute("UPDATE bot_trades SET human_done = 1, human_price = ?, human_contracts = ?, human_cost = ? "
                           "WHERE id = ?", (price, contracts, cost, t["id"]))
        return len(rows)

    async def _markouts(self, now: float) -> int:
        """The price 1, 5 and 30 minutes after each fill (Part 4 #5). A point from before
        our fill (a quiet market's last trade) is no markout: recorded as none."""
        app, done = self.app, 0
        for seconds, column, flag in MARKOUTS:
            horizon = self.cfg.markout_seconds if seconds is None else seconds
            rows = app.db.query(
                f"SELECT id, token_id, decided_at FROM bot_trades WHERE runner = ? AND status = 'FILLED' "
                f"AND {flag} = 0 AND decided_at <= ? LIMIT 40", (self.runner, now - horizon - MARKOUT_SETTLE_SECONDS))
            for t in rows:
                try:
                    point = await app.prices.at(t["token_id"], int(t["decided_at"]) + horizon)
                except Exception as exc:  # noqa: BLE001 - try again in a minute
                    log.warning("markout for %s failed: %s", t["id"], exc)
                    continue
                price = point.price if point is not None and point.ts > t["decided_at"] - 60 else None
                app.db.execute(f"UPDATE bot_trades SET {flag} = 1, {column} = ? WHERE id = ?", (price, t["id"]))
            done += len(rows)
        return done

    # -- the book, read again (Part 4 #6) --------------------------------------------------------

    def schedule_recheck(self, alert: Any, market: MarketInfo | None) -> None:
        """Read the book again ``recheck_seconds`` after the decision and record what the
        headline order would have done on it. In the background: it never delays a
        decision, and it changes none."""
        if not self.cfg.recheck_book or market is None:
            return
        task = asyncio.create_task(self._recheck(int(alert["id"]), market))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _recheck(self, alert_id: int, market: MarketInfo) -> None:
        await asyncio.sleep(self.cfg.recheck_seconds)
        db = self.app.db
        row = db.one("SELECT * FROM bot_trades WHERE runner = ? AND strategy = ? AND alert_id = ? "
                     "AND status IN ('FILLED', 'MISSED')", (self.runner, HEADLINE, alert_id))
        if row is None:          # skipped by a rule before the book mattered
            return
        try:
            book = await self.app.clob.book(row["token_id"])
        except Exception as exc:  # noqa: BLE001 - a measurement, never in the way
            log.warning("second book read for alert %s failed: %s", alert_id, exc)
            db.execute("UPDATE bot_trades SET recheck_at = ?, recheck_status = 'ERROR' WHERE id = ?",
                       (self.app.wall(), row["id"]))
            return
        at = self.app.wall()
        decision = self.decide_buy(book, row["stake_usd"], market, limit_price=row["limit_price"])
        db.execute("UPDATE bot_trades SET recheck_at = ?, recheck_best_ask = ?, recheck_fill_price = ?, "
                   "recheck_status = ? WHERE id = ?",
                   (at, book.best_ask if book else None, decision.walk.vwap if decision.walk else None,
                    decision.status, row["id"]))

    # -- settlement -------------------------------------------------------------------------------

    async def settle(self) -> int:
        """Close every position (and settle every comparison) whose market has resolved."""
        app = self.app
        try:
            await self.settle_skipped()
        except Exception as exc:  # noqa: BLE001 - the wallet's extra, never in the way of real settlement
            log.warning("could not check skipped signals for results: %s", exc)
        rows = app.db.query("SELECT * FROM bot_trades WHERE runner = ? AND closed_at IS NULL "
                            "AND status IN ('FILLED', 'MISSED')", (self.runner,))
        if not rows:
            return 0
        markets = await app.markets.ensure({r["condition_id"] for r in rows},
                                           refresh_after=app.cfg.paper.settle_interval_seconds)
        # The UMA history, read fresh for the markets settling now: a dispute can come
        # after our last read of the market (Part 4 #13).
        due = {r["condition_id"] for r in rows if (m := markets.get(r["condition_id"])) is not None and m.resolved}
        try:
            uma = await app.markets.refresh_uma(due) if due else {}
        except Exception as exc:  # noqa: BLE001 - settle anyway; the type is filled in later
            log.warning("could not read the UMA history of %d markets: %s", len(due), exc)
            uma = {}
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
            kind = (resolution_type(payouts=market.payouts, scheduled_end=t["scheduled_end"],
                                    resolved_at=market.resolved_at, uma_statuses=uma[t["condition_id"]])
                    if t["condition_id"] in uma else None)
            app.db.execute(
                "UPDATE bot_trades SET closed_at = ?, result = ?, payout_per_share = ?, pnl = ?, zero_pnl = ?, "
                "human_pnl = ?, closing_price = ?, updated_at = ?, resolved_at = ?, paid_at = ?, resolution_type = ? "
                "WHERE id = ? AND closed_at IS NULL",
                (app.clock(), result_label(pps) if filled else None, pps,
                 (t["contracts"] * pps - t["cost"]) if filled else 0.0,
                 shadow(t["zero_contracts"], t["zero_cost"]), human, closing, app.clock(), market.resolved_at,
                 app.clock(), kind, t["id"]))
            settled += 1
        await self.fill_resolution_types()
        return settled

    async def fill_resolution_types(self, limit: int = 100) -> int:
        """Part 4 #13: the resolution type of trades settled without one (before it existed,
        or when the UMA history could not be read), oldest first."""
        app = self.app
        rows = app.db.query(
            "SELECT b.id, b.condition_id, b.scheduled_end, b.resolved_at, m.payouts FROM bot_trades b "
            "LEFT JOIN poly_markets m ON m.condition_id = b.condition_id WHERE b.runner = ? AND b.closed_at IS NOT NULL "
            "AND b.resolution_type IS NULL AND COALESCE(b.result, '') != 'EXIT' AND b.payout_per_share IS NOT NULL "
            "ORDER BY b.id LIMIT ?", (self.runner, limit))
        if not rows:
            return 0
        try:
            uma = await app.markets.refresh_uma({r["condition_id"] for r in rows})
        except Exception as exc:  # noqa: BLE001 - try again at the next settle
            log.warning("could not read the UMA history to type %d settled trades: %s", len(rows), exc)
            return 0
        done = 0
        for r in rows:
            if r["condition_id"] not in uma:
                continue
            kind = resolution_type(payouts=json.loads(r["payouts"]) if r["payouts"] else None,
                                   scheduled_end=r["scheduled_end"], resolved_at=r["resolved_at"],
                                   uma_statuses=uma[r["condition_id"]])
            app.db.execute("UPDATE bot_trades SET resolution_type = ? WHERE id = ?", (kind, r["id"]))
            done += 1
        return done
