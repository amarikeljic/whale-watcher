"""Per-market accounting for one wallet, rebuilt from activity rows.

Realized P&L on a resolved market is

    cash in - cash out + (shares still held at resolution x payout per share)

with cash flows from TRADE buys and sells, SPLIT (pay $1, get one of each
outcome), MERGE (return one of each, get $1) and REDEEM (shares in, payout out).
Redemptions after ``as_of`` are never fetched, and the shares they would have
burned are valued at the payout instead, which gives the same number: that is
what makes the result independent of when a winner bothered to redeem.

Some activity cannot be accounted for from the rows the API serves:

* CONVERSION (neg-risk): NO shares across markets turn into YES shares of
  others plus cash, and the rows do not say which shares moved where.
* Shares that arrive by transfer, or entries older than the fetched window.
  These show up as a balance going negative.

Markets with either are marked unreliable and left out of every metric, and
the share of such markets is itself a filter: a wallet whose results cannot be
measured cannot be scored. Fees: ``usdc_size`` is used as reported; the rows do
not say whether a fill was maker (no fee) or taker, so wallet P&L can be off by
the taker fee (at most ~1.75% of notional at p=0.5). Paper trades, where it
matters, compute fees exactly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .polymarket import Activity

# Within one block, acquisitions are applied before disposals so a buy-then-sell
# in the same second does not read as a negative balance.
_ORDER = {("SPLIT", ""): 0, ("TRADE", "BUY"): 1, ("CONVERSION", ""): 2, ("TRADE", "SELL"): 3,
          ("MERGE", ""): 4, ("REDEEM", ""): 5}

NEGATIVE_TOLERANCE = 0.01   # shares; rounding in the API's six-decimal sizes


@dataclass(slots=True)
class Buy:
    ts: int
    outcome_index: int
    token_id: str
    size: float
    price: float
    usdc: float


@dataclass(slots=True)
class MarketLedger:
    condition_id: str
    n_outcomes: int = 2
    cash: float = 0.0
    cost: float = 0.0
    tokens: dict[int, float] = field(default_factory=dict)
    buys: list[Buy] = field(default_factory=list)
    outcomes_bought: set[int] = field(default_factory=set)
    has_split: bool = False
    has_conversion: bool = False
    incomplete: bool = False
    unmapped: bool = False
    first_ts: int | None = None
    first_acq_ts: int | None = None   # first buy or split: when the position was opened
    last_acq_ts: int | None = None
    last_trade_ts: int | None = None
    rows: int = 0

    def _acquired(self, ts: int) -> None:
        self.first_acq_ts = ts if self.first_acq_ts is None else min(self.first_acq_ts, ts)
        self.last_acq_ts = ts if self.last_acq_ts is None else max(self.last_acq_ts, ts)

    @property
    def two_sided(self) -> bool:
        return self.has_split or len(self.outcomes_bought) >= 2

    @property
    def reliable(self) -> bool:
        return not (self.has_conversion or self.incomplete or self.unmapped) and self.cost > 0

    def held_value(self, payouts: list[float]) -> float:
        return sum(shares * payouts[i] for i, shares in self.tokens.items() if i < len(payouts))

    def pnl(self, payouts: list[float]) -> float:
        return self.cash + self.held_value(payouts)

    def _add(self, index: int, shares: float) -> None:
        self.tokens[index] = self.tokens.get(index, 0.0) + shares

    def _remove(self, index: int, shares: float) -> None:
        balance = self.tokens.get(index, 0.0) - shares
        if balance < -NEGATIVE_TOLERANCE:
            self.incomplete = True
            balance = 0.0
        self.tokens[index] = max(0.0, balance)


TokenIndex = Callable[[str, str], int | None]
OutcomeCount = Callable[[str], int]


def build_ledgers(
    rows: Iterable[Activity],
    as_of: int,
    *,
    token_index: TokenIndex | None = None,
    outcome_count: OutcomeCount | None = None,
) -> dict[str, MarketLedger]:
    """One ledger per market from every row at or before ``as_of`` (later rows are ignored)."""
    usable = [r for r in rows if r.ts <= as_of and r.condition_id]
    usable.sort(key=lambda r: (r.ts, _ORDER.get((r.type, r.side if r.type == "TRADE" else ""), 9)))
    ledgers: dict[str, MarketLedger] = {}
    for r in usable:
        led = ledgers.get(r.condition_id)
        if led is None:
            n = outcome_count(r.condition_id) if outcome_count else 2
            led = ledgers[r.condition_id] = MarketLedger(r.condition_id, n_outcomes=max(2, n or 2))
        led.rows += 1
        led.first_ts = r.ts if led.first_ts is None else min(led.first_ts, r.ts)

        index = None
        if r.token_id and token_index is not None:
            index = token_index(r.condition_id, r.token_id)
        if index is None:
            index = r.outcome_index

        if r.type == "TRADE":
            led.last_trade_ts = r.ts
            if index is None:
                led.unmapped = True
                continue
            if r.side == "BUY":
                led.cash -= r.usdc_size
                led.cost += r.usdc_size
                led._acquired(r.ts)
                led._add(index, r.size)
                led.outcomes_bought.add(index)
                led.buys.append(Buy(r.ts, index, r.token_id, r.size, r.price, r.usdc_size))
            elif r.side == "SELL":
                led.cash += r.usdc_size
                led._remove(index, r.size)
        elif r.type == "SPLIT":
            led.has_split = True
            led.cash -= r.usdc_size
            led.cost += r.usdc_size
            led._acquired(r.ts)
            for i in range(led.n_outcomes):
                led._add(i, r.size)
        elif r.type == "MERGE":
            led.cash += r.usdc_size
            for i in range(led.n_outcomes):
                led._remove(i, r.size)
        elif r.type == "REDEEM":
            led.cash += r.usdc_size
            if index is None:
                # A combined (pre-Aug-2026) redemption burns the whole position.
                led.tokens = {i: 0.0 for i in led.tokens}
            else:
                led._remove(index, r.size)
        elif r.type == "CONVERSION":
            led.has_conversion = True
        # Anything else (rewards, rebates, yield) is not market P&L.
    return ledgers


def entry_vwap(buys: list[Buy], *, before: int | None = None) -> tuple[float, float, float] | None:
    """(vwap, shares, usd) of buys strictly before ``before``, or None if there are none."""
    chosen = [b for b in buys if before is None or b.ts < before]
    shares = sum(b.size for b in chosen)
    usd = sum(b.usdc for b in chosen)
    if shares <= 0:
        return None
    return usd / shares, shares, usd
