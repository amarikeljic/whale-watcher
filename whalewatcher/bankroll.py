"""v2.1 Part 2: the $100 wallet.

The bot's strategies stake a fixed $10 on every signal out of a bankroll that never
runs dry. A person with $100 cannot do that: money in an open position is gone until
the market settles, a loss shrinks every later stake, and ten open positions means
the eleventh signal is watched, not traded. This module answers what the same
signals would have done to a small wallet.

It is one deterministic engine. It walks a runner's signals in the order they were
decided and, for each, makes the wallet's own decision with only what was known at
that moment:

  stake   = equity x deploy_fraction / max_open_positions, never more than the cash
  equity  = cash + open positions at the last best bid recorded for them (their
            entry price when none was recorded: historical bids were not kept)
  skip    too_long, no_end_date   the time-to-close cutoff (horizon.py), when on
          no_slot                 max_open_positions are already open
          no_cash                 less cash than the smallest order
          below_minimum           the stake is under min_order_usd, or buys fewer
                                  shares than the market's minimum order
          event_cap               this event would hold more than max_per_event_fraction
  miss    the order would not have filled: price ran past the limit, book too thin

The stake is all-in: it pays for the shares AND the taker fee, so cash never goes
below zero. The on-ramp cost comes off the starting amount once. A position returns
``shares x payout`` to cash when its market is seen to have resolved.

Fill prices. From schema 8 the bot stores the ask levels it saw, so any stake is
priced by walking that book. Older signals carry only the price a $10 order paid:
a smaller stake would have paid that or less, a larger one more, so a larger stake
priced this way is flagged ``optimistic`` and counted.

Minimum order: Polymarket's real minimum is per market and in SHARES, not dollars
(``min_order_size`` on GET /book, 5 on every book sampled 2026-11-12;
https://docs.polymarket.com/trading/place-orders.md). ``min_order_usd`` is our own
floor on top of it.

The same function serves the replay (all of a source's history, with and without
the cutoff) and the live wallet (signals since the run began): nothing is stored
but the run's definition, so a run can never be rewritten, only re-derived. A run
ends when the wallet amount or a bankroll setting changes; its figures are frozen
into ``bankroll_runs.summary_json`` at that moment.
"""

from __future__ import annotations

import bisect
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .config import BankrollConfig, Config, FiltersConfig
from .fees import polymarket_taker_fee, rate_for, taker_fee
from .horizon import close_skip
from .polymarket import MarketInfo
from .precision import (DEFAULT_AMOUNT_DECIMALS, SHARE_STEP, amount_decimals, ceil_cents, exact_ratio, floor_cents,
                        floor_step, round_fills)
from .timeutil import iso
from .version import MODEL_VERSION
from .version import label as model_label

DEFAULT_AMOUNT = 100.0
MAX_AMOUNT = 100_000.0
AMOUNT_KEY = "wallet_amount"
HEADLINE = "flat_hold"          # the strategy whose signals the wallet follows
DEFAULT_MIN_SHARES = 5.0        # used when a signal did not store its market's minimum

NO_SLOT, NO_CASH, BELOW_MINIMUM, EVENT_CAP = "no_slot", "no_cash", "below_minimum", "event_cap"
NO_EDGE = "no_edge"             # automatic stake: the settled signals have not shown an edge
TAKEN, SKIPPED, MISSED = "TAKEN", "SKIPPED", "MISSED"
# A fixed-stake trade that missed for one of these reasons says something about a $10
# order, not about ours: with the book stored we walk it again at our own stake.
BOOK_REASONS = ("price ran", "book too thin", "below the minimum order size")


# -- the wallet amount ------------------------------------------------------------------------


def parse_amount(text: Any) -> float:
    """A positive dollar amount with at most two decimals, up to $100,000. Raises
    ValueError with a message fit to show the person who typed it."""
    raw = str(text).strip().replace(",", "").removeprefix("$").strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("the wallet amount must be a number of dollars, such as 100 or 250.50") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("the wallet amount must be a number of dollars, such as 100 or 250.50")
    if value <= 0:
        raise ValueError("the wallet amount must be more than $0")
    if value > MAX_AMOUNT:
        raise ValueError(f"the wallet amount cannot be more than ${MAX_AMOUNT:,.0f}")
    if round(value, 2) != value:
        raise ValueError("the wallet amount can have at most two decimals")
    return value


def wallet_amount(db: Any) -> float:
    """The amount saved in ``settings``, or $100 when none is (or what is there is not usable)."""
    try:
        row = db.one("SELECT value FROM settings WHERE key = ?", (AMOUNT_KEY,))
        return parse_amount(row["value"]) if row else DEFAULT_AMOUNT
    except Exception:  # noqa: BLE001 - an older database, or a value edited by hand
        return DEFAULT_AMOUNT


def save_amount(db: Any, amount: float, source: str, now: int) -> None:
    db.execute("INSERT INTO settings (key, value, updated_at, source) VALUES (?,?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
               "source = excluded.source", (AMOUNT_KEY, f"{parse_amount(amount):.2f}", now, source))


# -- one run's definition -----------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    amount: float = DEFAULT_AMOUNT
    onramp_cost_pct: float = 1.5
    deploy_fraction: float = 0.8
    max_open_positions: int = 10
    min_order_usd: float = 1.0
    max_per_event_fraction: float = 0.2
    max_days_to_close: float | None = None     # the cutoff this run applies; None and False below = none
    skip_if_no_end_date: bool = False
    stake_mode: str = "fixed"                  # fixed | auto (a run stored before the setting existed was fixed)
    kelly_fraction: float = 0.25
    min_history: int = 100
    confidence_z: float = 1.96
    no_edge_stake: str = "none"                # auto, while no edge is shown: none | smallest
    model_version: int | None = None           # the model whose trades the run follows (None: before tagging)

    @classmethod
    def of(cls, cfg: Config, amount: float, *, cutoff: bool = True, stake_mode: str | None = None,
           stake_fraction: float | None = None, no_edge_stake: str | None = None) -> Params:
        """``stake_fraction`` overrides the fixed stake (as a share of equity), for comparisons."""
        b: BankrollConfig = cfg.bankroll
        deploy = b.deploy_fraction if stake_fraction is None else stake_fraction * b.max_open_positions
        return cls(amount=amount, onramp_cost_pct=cfg.fees.onramp_cost_pct, deploy_fraction=deploy,
                   max_open_positions=b.max_open_positions, min_order_usd=b.min_order_usd,
                   max_per_event_fraction=b.max_per_event_fraction,
                   max_days_to_close=cfg.filters.max_days_to_close if cutoff else None,
                   skip_if_no_end_date=cfg.filters.skip_if_no_end_date if cutoff else False,
                   stake_mode=stake_mode or b.stake_mode, kelly_fraction=b.kelly_fraction,
                   min_history=b.auto_min_history, confidence_z=b.auto_confidence_z,
                   no_edge_stake=no_edge_stake or b.auto_no_edge_stake, model_version=MODEL_VERSION)

    @property
    def stake_fraction(self) -> float:
        """The fixed stake, and the most the automatic stake may ever be, as a share of equity."""
        return self.deploy_fraction / self.max_open_positions

    @property
    def cutoff(self) -> FiltersConfig | None:
        if self.max_days_to_close is None and not self.skip_if_no_end_date:
            return None
        return FiltersConfig(max_days_to_close=self.max_days_to_close, skip_if_no_end_date=self.skip_if_no_end_date)

    @property
    def fingerprint(self) -> str:
        """What makes a run a different run: the amount, the bankroll settings and the
        model version of the trades it follows (a new model version starts a new run)."""
        sizing = (f"auto|{self.kelly_fraction:g}|{self.min_history}|{self.confidence_z:g}|{self.no_edge_stake}"
                  if self.stake_mode == "auto" else "fixed")
        return (f"{self.amount:.2f}|{self.onramp_cost_pct:g}|{self.deploy_fraction:g}|{self.max_open_positions}|"
                f"{self.min_order_usd:g}|{self.max_per_event_fraction:g}|{sizing}|m{self.model_version}")

    def describe(self) -> str:
        before = "no order" if self.no_edge_stake == "none" else "the smallest order"
        stake = (f"automatic stake ({before} until {self.min_history} settled signals show an edge, then "
                 f"{self.kelly_fraction:g} of Kelly, at most {self.stake_fraction:.1%} of equity)"
                 if self.stake_mode == "auto" else
                 f"fixed stake of {self.stake_fraction:.1%} of equity")
        return (f"${self.amount:,.2f}, {stake}, up to {self.max_open_positions} positions, "
                f"{self.max_per_event_fraction:.0%} per event, ${self.min_order_usd:g} minimum, "
                f"{self.onramp_cost_pct:g}% on-ramp")


# -- what the wallet sees of a signal -------------------------------------------------------------


@dataclass
class Signal:
    id: str                          # "<table>:<row id>"
    ts: float                        # when the decision was made
    token_id: str
    condition_id: str
    event_key: str
    base_status: str                 # what the fixed-stake trade did: FILLED | MISSED | SKIPPED
    base_reason: str | None = None
    price: float | None = None       # the fixed-stake fill price (the best ask when it did not trade)
    base_stake: float = 10.0
    limit_price: float | None = None
    asks: list[tuple[float, float]] | None = None    # (price, shares), best first, as stored
    min_shares: float = DEFAULT_MIN_SHARES
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    scheduled_end: int | None = None
    settle_ts: float | None = None   # when the position ended and paid
    payout: float | None = None      # dollars per share at that moment
    wallet: str | None = None
    question: str = ""
    venue_skip: str | None = None    # the market is not on this venue at all (Polymarket US rows): not_on_us, ...
    # Part 4 #7, the venue's precision (precision.py): shares in steps of share_step, the
    # amount at amount_decimals (None: not rounded, Polymarket US documents no rule).
    share_step: float = SHARE_STEP
    amount_decimals: int | None = DEFAULT_AMOUNT_DECIMALS
    mark_key: str | None = None      # where its marks are kept: the token, or us:<slug>:<side> on Polymarket US

    @property
    def marked_as(self) -> str:
        return self.mark_key or self.token_id


# Skips that say the bet does not exist on the venue (polymarket_us.py). A wallet on that
# venue could not have taken the signal at any stake, so it is a skip, never a miss.
VENUE_SKIPS = ("not_on_us", "not_same_bet", "us_excluded")
# The followup switches (Part 4 #10, #11): a signal the bot was told not to copy is not
# copied by the wallet either. Off by default, so no row carries one.
SIGNAL_SKIPS = VENUE_SKIPS + ("hedged", "both_sides_wallet", "sold_back_wallet")


@dataclass
class Entry:
    signal: Signal
    status: str                      # TAKEN | SKIPPED | MISSED
    code: str | None
    reason: str
    equity: float                    # just before the decision
    cash: float
    open_positions: int
    stake: float = 0.0               # all-in: shares plus fee
    price: float | None = None
    contracts: float = 0.0
    fee: float = 0.0
    optimistic: bool = False         # priced from a smaller order's fill, no depth stored
    sizing: str = ""                 # how the stake was chosen (automatic stakes only)
    closed_ts: float | None = None
    proceeds: float | None = None

    @property
    def pnl(self) -> float | None:
        return None if self.proceeds is None else self.proceeds - self.stake


@dataclass
class Point:
    ts: float
    equity: float
    cash: float
    conservative: float | None = None   # open positions at what selling all of each would fetch, after the fee

    @property
    def tied(self) -> float:
        return self.equity - self.cash


@dataclass
class Result:
    params: Params
    label: str = ""
    start_ts: float | None = None
    entries: list[Entry] = field(default_factory=list)
    curve: list[Point] = field(default_factory=list)

    @property
    def conservative_equity(self) -> float:
        """Equity with every open position valued at what selling ALL of it into the
        recorded bids would fetch, after the taker fee (Part 4 #8). Never above equity."""
        last = self.curve[-1] if self.curve else None
        return self.start_cash if last is None else (last.conservative if last.conservative is not None
                                                     else last.equity)

    @property
    def start_cash(self) -> float:
        return round(self.params.amount * (1 - self.params.onramp_cost_pct / 100), 6)

    @property
    def onramp_cost(self) -> float:
        return self.params.amount - self.start_cash

    @property
    def equity(self) -> float:
        return self.curve[-1].equity if self.curve else self.start_cash

    @property
    def cash(self) -> float:
        return self.curve[-1].cash if self.curve else self.start_cash

    @property
    def total_return(self) -> float:
        """Against the amount put in, so the on-ramp cost counts."""
        return self.equity / self.params.amount - 1

    @property
    def taken(self) -> list[Entry]:
        return [e for e in self.entries if e.status == TAKEN]

    @property
    def skipped(self) -> Counter:
        return Counter(e.code for e in self.entries if e.status == SKIPPED)

    @property
    def missed(self) -> int:
        return sum(1 for e in self.entries if e.status == MISSED)

    @property
    def settled(self) -> list[Entry]:
        return [e for e in self.entries if e.status == TAKEN and e.proceeds is not None]

    @property
    def fees(self) -> float:
        return sum(e.fee for e in self.taken)

    @property
    def optimistic(self) -> int:
        return sum(1 for e in self.taken if e.optimistic)

    def drawdown(self) -> tuple[float, float]:
        """(dollars, share of the peak) of the deepest fall of equity from an earlier high."""
        peak, worst, worst_pct = self.start_cash, 0.0, 0.0
        for p in self.curve:
            peak = max(peak, p.equity)
            fall = peak - p.equity
            if fall > worst:
                worst, worst_pct = fall, (fall / peak if peak > 0 else 0.0)
        return worst, worst_pct

    def summary(self) -> dict[str, Any]:
        dd, dd_pct = self.drawdown()
        settled = self.settled
        return {
            "amount": self.params.amount, "equity": round(self.equity, 4), "cash": round(self.cash, 4),
            "conservative_equity": round(self.conservative_equity, 4),
            "total_return": round(self.total_return, 6), "max_drawdown_usd": round(dd, 4),
            "max_drawdown_pct": round(dd_pct, 6), "signals": len(self.entries), "taken": len(self.taken),
            "skipped": dict(self.skipped), "missed": self.missed, "settled": len(settled),
            "wins": sum(1 for e in settled if (e.pnl or 0) > 0), "open": len(self.taken) - len(settled),
            "fees": round(self.fees, 4), "onramp_cost": round(self.onramp_cost, 4), "optimistic": self.optimistic,
            "lowest_equity": round(min([p.equity for p in self.curve] or [self.start_cash]), 4),
            "staked": round(sum(e.stake for e in self.taken), 4), "stake_mode": self.params.stake_mode,
        }


class Marks:
    """Best bids recorded for open positions, per token (or us:<slug>:<side>), for
    marking equity, and the bids behind each mark (Part 4 #8) where they were kept."""

    def __init__(self, rows: Any = ()) -> None:
        by: dict[str, tuple[list[float], list[float], list[Any]]] = defaultdict(lambda: ([], [], []))
        for row in sorted(rows, key=lambda r: (r[0], r[1])):
            token, ts, bid = row[0], row[1], row[2]
            depth = row[3] if len(row) > 3 else None
            by[token][0].append(ts)
            by[token][1].append(bid)
            by[token][2].append(depth)
        self._by = dict(by)

    def _index(self, token: str, ts: float) -> tuple[Any, int] | None:
        series = self._by.get(token)
        if not series:
            return None
        i = bisect.bisect_right(series[0], ts)
        return (series, i - 1) if i else None

    def at(self, token: str, ts: float) -> float | None:
        found = self._index(token, ts)
        return None if found is None else found[0][1][found[1]]

    def depth_at(self, token: str, ts: float) -> list[tuple[float, float]] | None:
        """The bids behind the mark in force at ``ts``, best first, or None (a mark from
        before depth was kept, or no mark)."""
        found = self._index(token, ts)
        if found is None or not found[0][2][found[1]]:
            return None
        try:
            return [(float(p), float(q)) for p, q in json.loads(found[0][2][found[1]])]
        except (ValueError, TypeError):
            return None


# -- pricing one order ----------------------------------------------------------------------------


def _fee_per_share(price: float, rate: float, exponent: float) -> float:
    if rate <= 0 or not 0 < price < 1:
        return 0.0
    return rate * (price * (1 - price)) ** exponent if exponent != 1 else rate * price * (1 - price)


def price_order(sig: Signal, budget: float) -> tuple[float, float, float, bool] | str:
    """(price, shares, fee, optimistic) for an all-in ``budget`` (shares plus fee), or
    the reason the order would not have filled."""
    rate, exp, limit = sig.fee_rate, sig.fee_exponent, sig.limit_price
    if sig.asks:
        remaining, fills = budget, []
        for price, size in sig.asks:
            if limit is not None and price > limit + 1e-12:
                break
            if remaining <= 1e-9:
                break
            shares = min(size, remaining / (price + _fee_per_share(price, rate, exp)))
            if shares <= 0:
                continue
            fills.append((price, shares))
            remaining -= shares * (price + _fee_per_share(price, rate, exp))
        if remaining > 1e-6 or not fills:
            deep = sum(size * (price + _fee_per_share(price, rate, exp)) for price, size in sig.asks)
            if deep + 1e-9 >= budget and limit is not None:
                return f"price ran: ${budget:,.2f} does not fill at or below the limit {limit:.3f}"
            return f"book too thin for ${budget:,.2f}"
        done = round_fills(fills, step=sig.share_step, decimals=sig.amount_decimals)
        if done is None:        # less than one step of shares: the minimum-order check skips it
            return fills[0][0], 0.0, 0.0, False
        fills, contracts, amount = done
        fee = taker_fee(fills, rate, exp)
        return exact_ratio(amount, contracts), contracts, fee, False
    if sig.base_status == "MISSED":
        return sig.base_reason or "the fixed-stake order did not fill"
    price = sig.price
    if price is None or not 0 < price < 1:
        return "no price was recorded for this signal"
    if limit is not None and price > limit + 1e-12:
        return f"price ran: {price:.3f} against the limit {limit:.3f}"
    contracts = floor_step(budget / (price + _fee_per_share(price, rate, exp)), sig.share_step)
    fee = polymarket_taker_fee(contracts, price, rate, exp)
    # A $10 order paid this price. A smaller one would pay it or less. A larger one, or
    # an order where no fill was ever made, may not have found the depth.
    optimistic = budget > sig.base_stake + 1e-9 or sig.base_status != "FILLED"
    return price, contracts, fee, optimistic


# -- the automatic stake ----------------------------------------------------------------------------


class Evidence:
    """What the source's own settled signals say about its edge, as of any moment,
    and the stake that follows from it.

    The record is every signal of the source that a fixed-stake order filled and
    whose result was known by then, whether or not this wallet took it: those
    results are public the moment the market settles. Each contributes its return
    per dollar, fee included: ``payout / (price + fee per share) - 1``.

    The stake, as a share of equity:

      * fewer than ``min_history`` settled signals, or an edge that is not above
        zero after taking ``confidence_z`` standard errors off the mean: no edge
        has been shown, and the Kelly stake for no edge is zero. The signal is
        skipped (``no_edge``). The wallet does not need to bet to learn: the
        record keeps growing from the fixed-stake ledger either way. With
        ``no_edge_stake = smallest`` it places the venue's smallest order instead.
        The defaults (100 signals, 1.96 standard errors) are the project's own
        decision rule for "copying has an edge". A lower bar is passed by luck:
        with 30 signals and one standard error, replays of signals that lose money
        overall sized up on a hot streak and gave it all back.
      * otherwise ``kelly_fraction`` of the Kelly stake for a share bought at
        all-in cost c that pays $1:  f* = c x edge / (1 - c).  A favourite gets a
        larger stake than a long shot for the same edge, because it loses less often.
      * never more than the fixed stake, and never less than the smallest order.

    Nothing after the decision is used: a result that came in later is not evidence yet."""

    def __init__(self, signals: list[Signal], params: Params) -> None:
        self.params = params
        self.history = sorted(
            (s for s in signals if s.base_status == "FILLED" and s.settle_ts is not None and s.payout is not None
             and s.price is not None and 0 < s.price < 1), key=lambda s: (s.settle_ts, s.id))
        self._i, self.n, self._sum, self._sumsq = 0, 0, 0.0, 0.0

    def edge(self, ts: float) -> float | None:
        """The conservative edge per dollar known at ``ts``, or None with too little history."""
        while self._i < len(self.history) and self.history[self._i].settle_ts <= ts:
            s = self.history[self._i]
            r = s.payout / (s.price + _fee_per_share(s.price, s.fee_rate, s.fee_exponent)) - 1
            self.n, self._sum, self._sumsq, self._i = self.n + 1, self._sum + r, self._sumsq + r * r, self._i + 1
        if self.n < max(2, self.params.min_history):
            return None
        mean = self._sum / self.n
        var = max(0.0, self._sumsq - self.n * mean * mean) / (self.n - 1)
        return mean - self.params.confidence_z * (var / self.n) ** 0.5

    def stake(self, sig: Signal, equity: float, cash: float) -> tuple[float, str]:
        """(dollars, how it was chosen). Zero dollars means: no edge shown, place no order."""
        p = self.params
        edge = self.edge(sig.ts)
        ref = sig.asks[0][0] if sig.asks else sig.price
        cost = None if ref is None or not 0 < ref < 1 else ref + _fee_per_share(ref, sig.fee_rate, sig.fee_exponent)
        ceiling = min(equity * p.stake_fraction, cash)
        if edge is None:
            fraction, why = 0.0, f"no edge can be judged from {self.n} settled signals (need {p.min_history})"
        elif edge <= 0 or cost is None or cost >= 1:
            fraction, why = 0.0, f"no edge shown: {edge:+.1%} a dollar after {self.n} settled signals"
        else:
            kelly = cost * edge / (1 - cost)
            fraction = min(p.kelly_fraction * kelly, p.stake_fraction)
            why = (f"edge {edge:+.1%} a dollar after {self.n} settled signals: {p.kelly_fraction:g} of Kelly "
                   f"({kelly:.1%}) is {fraction:.1%} of equity")
        stake = min(equity * fraction, ceiling)
        # The smallest order the venue takes: the market's minimum shares, rounded UP to the
        # cent, so that whole-cent stakes and 0.01-share steps cannot leave it short.
        floor = max(p.min_order_usd, ceil_cents(sig.min_shares * cost)) if cost is not None else p.min_order_usd
        if fraction <= 0 and p.no_edge_stake == "none":
            return 0.0, f"{NO_EDGE}: {why}"
        if stake < floor:
            if floor > ceiling + 1e-9:
                return stake, f"auto: {why}; the smallest order (${floor:,.2f}) is more than this wallet may stake"
            return floor, f"auto: {why}; the smallest order"
        return stake, f"auto: {why}"


# -- the engine -------------------------------------------------------------------------------------


def run(signals: list[Signal], params: Params, *, marks: Marks | None = None, start: float | None = None,
        as_of: float | None = None, label: str = "") -> Result:
    """Replay ``signals`` decided in ``[start, as_of)`` through a wallet. Settlements
    and marks after ``as_of`` are not used: the result is what was known then."""
    marks = marks or Marks()
    todo = sorted((s for s in signals if (start is None or s.ts >= start) and (as_of is None or s.ts < as_of)),
                  key=lambda s: (s.ts, s.id))
    res = Result(params, label, start_ts=start if start is not None else (todo[0].ts if todo else None))
    cash = res.start_cash
    open_: list[Entry] = []
    cutoff = params.cutoff
    evidence = Evidence(signals, params) if params.stake_mode == "auto" else None

    def value(e: Entry, ts: float) -> float:
        bid = marks.at(e.signal.marked_as, ts)
        return e.contracts * (bid if bid is not None else e.price)

    def sale_value(e: Entry, ts: float) -> float:
        """What selling all of it would fetch, after the taker fee: down the recorded
        bids, nothing for what they cannot absorb. Without depth, the best bid (or the
        price paid) less the fee. Never more than value()."""
        sig, depth = e.signal, marks.depth_at(e.signal.marked_as, ts)
        if depth is None:
            px = marks.at(sig.marked_as, ts)
            px = e.price if px is None else px
            fills = [(px, e.contracts)] if px else []
        else:
            fills, left = [], e.contracts
            for price, size in depth:
                if left <= 1e-9:
                    break
                take = min(size, left)
                fills.append((price, take))
                left -= take
        gross = sum(p * q for p, q in fills)
        return min(value(e, ts), max(0.0, gross - taker_fee(fills, sig.fee_rate, sig.fee_exponent)))

    def equity_at(ts: float) -> float:
        return cash + sum(value(e, ts) for e in open_)

    def point(ts: float) -> None:
        res.curve.append(Point(ts, equity_at(ts), cash, cash + sum(sale_value(e, ts) for e in open_)))

    def settle_until(ts: float) -> None:
        nonlocal cash
        due = sorted((e for e in open_ if e.signal.settle_ts is not None and e.signal.payout is not None
                      and e.signal.settle_ts <= ts), key=lambda e: (e.signal.settle_ts, e.signal.id))
        for e in due:
            e.closed_ts, e.proceeds = e.signal.settle_ts, e.contracts * e.signal.payout
            cash += e.proceeds
            open_.remove(e)
            point(e.closed_ts)

    if res.start_ts is not None:
        point(res.start_ts)
    for sig in todo:
        settle_until(sig.ts)
        equity = equity_at(sig.ts)
        entry = Entry(sig, SKIPPED, None, "", equity, cash, len(open_))
        res.entries.append(entry)
        far = close_skip(sig.scheduled_end, sig.ts, cutoff) if cutoff is not None else None
        if far is not None:
            entry.code, entry.reason = far
            continue
        if sig.venue_skip is not None:
            entry.code, entry.reason = sig.venue_skip, sig.base_reason or sig.venue_skip
            continue
        if sig.base_status == "MISSED" and not (sig.asks and (sig.base_reason or "").startswith(BOOK_REASONS)):
            entry.status, entry.reason = MISSED, sig.base_reason or "the order did not fill"
            continue
        if len(open_) >= params.max_open_positions:
            entry.code, entry.reason = NO_SLOT, f"{NO_SLOT}: {len(open_)} positions are open (limit {params.max_open_positions})"
            continue
        if cash < params.min_order_usd:
            entry.code, entry.reason = NO_CASH, f"{NO_CASH}: ${cash:,.2f} free, the smallest order is ${params.min_order_usd:g}"
            continue
        stake = floor_cents(min(equity * params.stake_fraction, cash))     # an order is in whole cents
        if evidence is not None:
            stake, entry.sizing = evidence.stake(sig, equity, cash)
            stake = floor_cents(stake)
            if entry.sizing.startswith(NO_EDGE):
                entry.code, entry.reason = NO_EDGE, entry.sizing
                continue
        if stake < params.min_order_usd:
            entry.code = BELOW_MINIMUM
            entry.reason = f"{BELOW_MINIMUM}: the stake would be ${stake:,.2f}, the smallest order is ${params.min_order_usd:g}"
            continue
        in_event = sum(e.stake for e in open_ if e.signal.event_key == sig.event_key)
        cap = equity * params.max_per_event_fraction
        if in_event + stake > cap + 1e-9:
            entry.code = EVENT_CAP
            entry.reason = f"{EVENT_CAP}: ${in_event:,.2f} is open in this event, a ${stake:,.2f} stake would pass ${cap:,.2f}"
            continue
        priced = price_order(sig, stake)
        if isinstance(priced, str):
            entry.status, entry.reason = MISSED, priced
            continue
        price, contracts, fee, optimistic = priced
        if contracts < sig.min_shares - 1e-9:
            entry.code = BELOW_MINIMUM
            entry.reason = (f"{BELOW_MINIMUM}: ${stake:,.2f} buys {contracts:.2f} shares at {price:.3f}, the market's "
                            f"smallest order is {sig.min_shares:g}")
            continue
        # What the shares cost, which rounding can leave a cent or so under the stake.
        spent = min(stake, price * contracts + fee)
        entry.status, entry.stake, entry.price, entry.contracts, entry.fee = TAKEN, spent, price, contracts, fee
        entry.optimistic = optimistic
        cash -= spent
        open_.append(entry)
        point(sig.ts)
    end = as_of if as_of is not None else float("inf")
    settle_until(end)
    if as_of is not None and (not res.curve or res.curve[-1].ts < as_of):
        point(as_of)
    return res


# -- signals out of the ledgers ------------------------------------------------------------------


def _rate(rec: dict, cfg: Config) -> tuple[float, float]:
    if rec.get("runner") == "us":
        # Polymarket US: one coefficient on every market, exponent 1. A row that did not
        # fill stored no rate, so the published one applies (polymarket_us.py). The
        # wallet's fee is rounded per level, not to the cent as the venue rounds the
        # order: at these stakes that is under half a cent.
        rate = rec.get("fee_rate")
        return (float(rate) if rate is not None else cfg.polymarket_us.fallback_fee_coefficient), 1.0
    exp = rec.get("market_fee_exponent") or 1.0
    if rec.get("fee_rate") is not None:
        return float(rec["fee_rate"]), exp
    market = MarketInfo(condition_id=rec["condition_id"], category=rec.get("category") or "other",
                        fees_enabled=bool(rec.get("market_fees_enabled")), fee_rate=rec.get("market_fee_rate") or 0.0)
    return rate_for(market, int(rec.get("decided_at") or rec.get("alert_ts") or 0), cfg.fees)[0], exp


def signal_of(rec: dict, cfg: Config) -> Signal | None:
    """A ledger row (as ``stats.load_bot_trades`` / ``load_trades`` return it) as the
    wallet sees it. None for a row that is not a decision yet (a pending fill)."""
    bot = "runner" in rec
    status = rec["status"]
    if status == "PENDING":
        return None
    if not bot:
        status = "FILLED" if rec.get("fill_price") is not None and status != "MISSED" else "MISSED"
    ts = rec.get("decided_at") or rec.get("fill_ts") or rec["alert_ts"]
    asks = None
    if rec.get("asks_json"):
        try:
            asks = [(float(p), float(s)) for p, s in json.loads(rec["asks_json"])]
        except (ValueError, TypeError):
            asks = None
    price = rec.get("fill_price") if status == "FILLED" else rec.get("best_ask" if bot else "alert_ask")
    limit = rec.get("limit_price")
    if limit is None and bot and rec.get("whale_price") is not None:
        limit = rec["whale_price"] + cfg.bot.max_slippage
    settle_ts = rec.get("exit_at") or rec.get("closed_at") or rec.get("settled_at")
    payout = rec.get("exit_price") if rec.get("exit_at") else rec.get("payout_per_share")
    if payout is not None and settle_ts is None:
        settle_ts = rec.get("resolved_at") or rec.get("market_resolved_at")     # a signal the bot skipped
    rate, exp = _rate(rec, cfg)
    return Signal(
        id=f"{'bot' if bot else 'paper'}:{rec['id']}", ts=float(ts), token_id=rec["token_id"],
        condition_id=rec["condition_id"], event_key=rec.get("event_key") or rec.get("event_slug") or rec["condition_id"],
        base_status=status, base_reason=rec.get("reason") or rec.get("miss_reason"), price=price,
        base_stake=rec.get("stake_usd") or 10.0, limit_price=limit, asks=asks,
        min_shares=rec.get("min_order_size") or DEFAULT_MIN_SHARES, fee_rate=rate, fee_exponent=exp,
        scheduled_end=rec.get("scheduled_end"), settle_ts=settle_ts if payout is not None else None, payout=payout,
        wallet=rec.get("wallet"), question=rec.get("question") or "",
        venue_skip=rec.get("skip_code") if rec.get("skip_code") in SIGNAL_SKIPS else None,
        share_step=(rec.get("min_order_size") or SHARE_STEP) if rec.get("runner") == "us" else SHARE_STEP,
        mark_key=(f"us:{rec['us_slug']}:{rec['us_side']}" if rec.get("runner") == "us" and rec.get("us_slug")
                  and rec.get("us_side") else None),
        amount_decimals=None if rec.get("runner") == "us" else amount_decimals(rec.get("tick_size")))


def source_key(rec: dict) -> str | None:
    """Which wallet a ledger row feeds. Each is replayed on its own: the bot model per
    runner, the old rule, and each simulated backtest are never mixed."""
    if "runner" in rec:
        return f"bot:{rec['runner']}" if rec["strategy"] == HEADLINE else None
    strategy = str(rec["strategy"])
    if strategy.startswith("backtest:"):
        return f"sim:{strategy[9:]}" if rec.get("venue") == "polymarket" else None
    return "old"


def source_label(key: str) -> str:
    kind, _, name = key.partition(":")
    if kind == "bot":
        return "Polymarket US: same signals, US book" if name == "us" else f"Bot, runner {name}"
    if kind == "sim":
        return f"Simulated: {name}"
    return "Old rule (v1), human delay"


def signals_by_source(records: list[dict], cfg: Config) -> dict[str, list[Signal]]:
    out: dict[str, list[Signal]] = defaultdict(list)
    for rec in records:
        key = source_key(rec)
        if key is None:
            continue
        sig = signal_of(rec, cfg)
        if sig is not None:
            out[key].append(sig)
    return dict(out)


def load_marks(db: Any) -> Marks:
    try:
        return Marks((r["token_id"], r["ts"], r["best_bid"], r["bids_json"]) for r in db.query(
            "SELECT token_id, ts, best_bid, bids_json FROM position_marks"))
    except Exception:  # noqa: BLE001 - a database older than the table or the column
        try:
            return Marks((r["token_id"], r["ts"], r["best_bid"]) for r in db.query(
                "SELECT token_id, ts, best_bid FROM position_marks"))
        except Exception:  # noqa: BLE001
            return Marks()


# -- runs: when a wallet begins and ends ------------------------------------------------------------


def open_run(db: Any, runner: str) -> Any:
    return db.one("SELECT * FROM bankroll_runs WHERE runner = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (runner,))


def run_params(row: Any, cfg: Config) -> Params:
    """The definition a run was started with (its cutoff follows today's config)."""
    stored = json.loads(row["params_json"])
    base = Params.of(cfg, row["amount"])
    known = {k: v for k, v in stored.items() if k in Params.__dataclass_fields__}
    return Params(**{**known, "max_days_to_close": base.max_days_to_close,
                     "skip_if_no_end_date": base.skip_if_no_end_date})


def sync_runs(app: Any, runners: list[str], signals: dict[str, list[Signal]] | None = None) -> list[str]:
    """Make each runner's open run match the wallet amount and the bankroll settings.
    A run whose definition no longer matches is ended (its figures frozen) and a new
    one started. Returns one line per change. Old runs are never touched again."""
    now = int(app.clock())
    params = Params.of(app.cfg, wallet_amount(app.db))
    changes = []
    by_model: dict[int | None, dict[str, list[Signal]]] = {}
    if signals is not None:             # the caller's signals are the current model's
        by_model[params.model_version] = signals
    for runner in runners:
        current = open_run(app.db, runner)
        if current is not None and current["fingerprint"] == params.fingerprint:
            continue
        if current is not None:
            old = run_params(current, app.cfg)
            if old.model_version not in by_model:   # read the ledger only now that a run has to be closed
                from .stats import Scope, load_bot_trades, scope_rows
                scope = Scope("before" if old.model_version is None else str(old.model_version))
                by_model[old.model_version] = signals_by_source(scope_rows(app, load_bot_trades(app), scope).rows,
                                                                app.cfg)
            sigs = by_model[old.model_version].get(f"bot:{runner}", [])
            frozen = run(sigs, old, marks=load_marks(app.db), start=current["started_at"], as_of=now)
            why = (f"wallet amount changed from ${old.amount:,.2f} to ${params.amount:,.2f}"
                   if abs(old.amount - params.amount) > 1e-9 else
                   f"model version changed from {model_label(old.model_version)} to {model_label(params.model_version)}"
                   if old.model_version != params.model_version else "bankroll settings changed")
            app.db.execute("UPDATE bankroll_runs SET ended_at = ?, end_reason = ?, summary_json = ? "
                           "WHERE id = ? AND ended_at IS NULL", (now, why, json.dumps(frozen.summary()), current["id"]))
            changes.append(f"{runner}: ended run #{current['id']} ({why})")
        dump = {k: getattr(params, k) for k in Params.__dataclass_fields__}
        cur = app.db.execute(
            "INSERT INTO bankroll_runs (runner, amount, params_json, fingerprint, started_at) VALUES (?,?,?,?,?)",
            (runner, params.amount, json.dumps(dump), params.fingerprint, now))
        changes.append(f"{runner}: started run #{cur.lastrowid} with {params.describe()}")
    return changes


def run_label(row: Any) -> str:
    end = f"to {iso(row['ended_at'])[:10]}" if row["ended_at"] else "running"
    return f"Run #{row['id']}: ${row['amount']:,.2f} from {iso(row['started_at'])[:10]}, {end}"


# -- what the reports show -----------------------------------------------------------------------------


@dataclass
class View:
    """One source's wallet: the whole history replayed with and without the cutoff,
    and (for a bot runner) the live run since it began."""
    key: str
    label: str
    replay: Result
    replay_no_cutoff: Result
    live: Result | None = None
    run_row: Any = None
    comparison: list[tuple[str, Result]] = field(default_factory=list)

    @property
    def simulated(self) -> bool:
        return self.key.startswith("sim:")


def live_runners(signals: dict[str, list[Signal]]) -> list[str]:
    """Runners that have a wallet: ``single`` always, others once they have signals."""
    return sorted({"single"} | {k[4:] for k in signals if k.startswith("bot:")})


def views(records: list[dict], cfg: Config, db: Any, now: float, *, amount: float | None = None,
          only: str | set[str] | None = None, compare: str | bool = False) -> list[View]:
    """``only``: one source key, or a set of them. ``compare``: True adds the stake-size
    comparison to every view, a source key to that one only."""
    wanted = None if only is None else ({only} if isinstance(only, str) else set(only))
    amount = wallet_amount(db) if amount is None else amount
    signals = signals_by_source(records, cfg)
    marks = load_marks(db)
    with_cutoff, without = Params.of(cfg, amount), Params.of(cfg, amount, cutoff=False)
    keys = sorted(signals, key=lambda k: ({"bot": 0, "old": 1, "sim": 2}[k.partition(":")[0]], k))
    for runner in live_runners(signals):
        if f"bot:{runner}" not in keys:
            keys.insert(0, f"bot:{runner}")
    out = []
    for key in keys:
        if wanted is not None and key not in wanted:
            continue
        sigs = signals.get(key, [])
        view = View(key, source_label(key), run(sigs, with_cutoff, marks=marks, as_of=now, label="with the cutoff"),
                    run(sigs, without, marks=marks, as_of=now, label="without the cutoff"))
        if key.startswith("bot:"):
            try:
                row = open_run(db, key[4:])
            except Exception:  # noqa: BLE001 - a database older than the table
                row = None
            if row is not None:
                view.run_row = row
                view.live = run(sigs, run_params(row, cfg), marks=marks, start=row["started_at"], as_of=now,
                                label=run_label(row))
        if sigs and (compare is True or compare == key):
            view.comparison = stake_comparison(sigs, cfg, amount, marks, now)
        out.append(view)
    return out


SWEEP_FRACTIONS = (0.01, 0.02, 0.04)


def stake_comparison(signals: list[Signal], cfg: Config, amount: float, marks: Marks, now: float,
                     ) -> list[tuple[str, Result]]:
    """The same signals through the automatic stake and through fixed stakes of several
    sizes, all with the cutoff. A description of the past, not a way to choose: the
    row that did best was picked by the same outcomes it is judged on."""
    configured = round(cfg.bankroll.deploy_fraction / cfg.bankroll.max_open_positions, 6)
    auto, rows = cfg.bankroll.stake_mode == "auto", []
    for choice, name in (("none", "Automatic, no order without an edge"),
                         ("smallest", "Automatic, smallest order without an edge")):
        tag = " (in use)" if auto and cfg.bankroll.auto_no_edge_stake == choice else ""
        rows.append((name + tag, run(signals, Params.of(cfg, amount, stake_mode="auto", no_edge_stake=choice),
                                     marks=marks, as_of=now)))
    for f in sorted({*SWEEP_FRACTIONS, configured}):
        tag = " (in use)" if f == configured and cfg.bankroll.stake_mode == "fixed" else ""
        rows.append((f"Fixed {f:.0%} of equity{tag}",
                     run(signals, Params.of(cfg, amount, stake_mode="fixed", stake_fraction=f), marks=marks, as_of=now)))
    return rows


def comparison_lines(rows: list[tuple[str, Result]]) -> list[str]:
    lines = ["    stake sizes compared (replay with the cutoff; choosing the best row afterwards is hindsight):"]
    for name, res in rows:
        s = res.summary()
        skipped = sum(s["skipped"].values())
        lines.append(f"      {name:<50} equity {_usd(s['equity']):>10} ({s['total_return']:+6.1%}) | lowest "
                     f"{_usd(s['lowest_equity']):>9} | max drawdown {s['max_drawdown_pct']:5.1%} | taken "
                     f"{s['taken']:4d}, skipped {skipped:5d} | staked {_usd(s['staked'])}")
    return lines


def past_runs(db: Any) -> list[Any]:
    try:
        return db.query("SELECT * FROM bankroll_runs ORDER BY id DESC")
    except Exception:  # noqa: BLE001
        return []


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def result_line(res: Result) -> str:
    s = res.summary()
    skips = ", ".join(f"{k} {v}" for k, v in sorted(s["skipped"].items(), key=lambda kv: -kv[1]))
    skipped = sum(s["skipped"].values())
    return (f"equity {_usd(s['equity'])} ({s['total_return']:+.1%}), {_usd(s['conservative_equity'])} if every open "
            f"position were sold into the bids | max drawdown {_usd(s['max_drawdown_usd'])} "
            f"({s['max_drawdown_pct']:.1%}) | taken {s['taken']} of {s['signals']} signals, skipped {skipped}"
            f"{f' ({skips})' if skips else ''}, missed {s['missed']} | {s['settled']} settled, {s['open']} open | "
            f"fees {_usd(s['fees'])}")


def view_lines(view: View, threshold: int, *, brief: bool = False) -> list[str]:
    amount = view.replay.params.amount
    tag = " (SIMULATED trades)" if view.simulated else ""
    lines = [f"  {view.label}: ${amount:,.2f} wallet{tag}"]
    if view.live is not None:
        lines.append(f"    live, {view.live.label.lower()}: {result_line(view.live)}")
        if len(view.live.settled) < threshold:
            lines.append(f"    ⚠ only {len(view.live.settled)} settled wallet trades in this run: not enough to "
                         f"draw conclusions (need {threshold})")
    if not view.replay_no_cutoff.entries:
        lines.append("    no signals yet")
        return lines
    lines.append(f"    replay of every signal, with the cutoff:    {result_line(view.replay)}")
    if not brief:
        lines.append(f"    replay of every signal, without the cutoff: {result_line(view.replay_no_cutoff)}")
        for res in (view.replay, view.replay_no_cutoff):
            if res.optimistic:
                lines.append(f"    ⚠ {res.label}: {res.optimistic} of {len(res.taken)} fills are priced from a "
                             "smaller order or the best ask, with no depth stored: optimistic")
    if view.comparison and not brief:
        lines += comparison_lines(view.comparison)
    if view.live is None and len(view.replay.settled) < threshold:
        lines.append(f"    ⚠ only {len(view.replay.settled)} settled wallet trades: not enough to draw conclusions "
                     f"(need {threshold})")
    return lines


def render(records: list[dict], cfg: Config, db: Any, now: float, *, brief: bool = False,
           simulated: bool = True) -> list[str]:
    """The wallet section of ``stats`` and of the daily summary."""
    out: list[str] = []
    for view in views(records, cfg, db, now, compare=not brief):
        if view.simulated and not simulated:
            continue
        out += view_lines(view, cfg.stats.min_settled_for_conclusions, brief=brief)
    if not out:
        return []
    p = Params.of(cfg, wallet_amount(db))
    head = [f"SMALL WALLET: the same signals through {p.describe()} (paper only)",
            "  The fee is inside the stake and a stake is never more than the cash. Open positions are valued "
            "at their last recorded best bid, or at the price paid when none was recorded."]
    return head + out
