"""Fee models, one pluggable function per venue.

A fee model takes the fills of one order (price, shares per level crossed) and
the market, and returns the fee in USD. Kalshi will plug in here when Kalshi
paper trading comes back; see docs/API_NOTES.md for its verified fee fields.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .polymarket import MarketInfo
from .timeutil import parse_as_of

Fill = tuple[float, float]  # (price, shares)
FeeModel = Callable[..., float]   # (fills, market[, ts]) -> USD

_FIVE_DP = Decimal("0.00001")


def polymarket_taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Taker fee in USDC for one match.

    https://docs.polymarket.com/trading/fees.md (verified 2026-11-07):
    ``fee = C x feeRate x p x (1 - p)``, takers only, rounded to 5 decimal
    places; anything under 0.00001 rounds to zero. ``exponent`` is the
    market's "exponent applied to the price component of the fee curve"
    (https://docs.polymarket.com/market-data/market-details.md#trading-fees);
    it is 1 on every market checked, which reduces to the formula above.
    """
    if rate <= 0 or shares <= 0 or not 0 < price < 1:
        return 0.0
    if exponent == 1:
        component = Decimal(str(price)) * (Decimal(1) - Decimal(str(price)))
    else:
        component = Decimal(str((price * (1 - price)) ** exponent))
    raw = Decimal(str(shares)) * Decimal(str(rate)) * component
    return float(raw.quantize(_FIVE_DP, rounding=ROUND_HALF_UP))


# -- which rate applies ------------------------------------------------------------------

_ACTIVE: Any = None      # the FeesConfig in force; set once by open_app (configure)


def configure(fees_cfg: Any) -> None:
    global _ACTIVE
    _ACTIVE = fees_cfg


def table_rate(category: str | None, ts: int | None = None, fees_cfg: Any = None) -> float | None:
    """The category table's rate in force at ``ts`` (now if omitted), or None if no
    table is configured. A category the table does not name pays its ``other`` rate."""
    cfg = fees_cfg if fees_cfg is not None else _ACTIVE
    if cfg is None or not cfg.schedules:
        return None
    dated = sorted(((parse_as_of(str(e["from"])), e["rates"]) for e in cfg.schedules), key=lambda x: x[0])
    rates = dated[0][1]                       # a trade older than every table uses the oldest
    for start, table in dated:
        if ts is None or start <= ts:
            rates = table
    key = (category or "other").lower()
    return float(rates.get(key, rates.get("other", 0.0)))


def rate_for(market: MarketInfo, ts: int | None = None, fees_cfg: Any = None) -> tuple[float, str]:
    """(rate, where it came from) for a taker trade in ``market`` at ``ts``.

    The market's own ``feeSchedule.rate`` is what the venue charges, and it does not
    always equal the published category table: on 2026-11-12 open sports markets
    carried 0.03 (311 of them) as well as 0.05 (479), and 111 markets tagged
    geopolitics carried 0.04 where the table says 0. So the market's rate is the
    default; ``fees.rate_source`` can switch to the table or to the higher of the two."""
    cfg = fees_cfg if fees_cfg is not None else _ACTIVE
    own = market.fee_rate if market.fees_enabled and market.fee_rate > 0 else 0.0
    table = table_rate(market.category, ts, cfg)
    source = getattr(cfg, "rate_source", "market") if cfg is not None else "market"
    if table is None:
        return own, "market"
    if source == "table":
        return table, "table"
    if source == "higher":
        return (own, "market") if own >= table else (table, "table")
    if market.fees_enabled and market.fee_rate <= 0:
        return table, "table"                 # it charges a fee but publishes no rate: fall back
    return own, "market"


def taker_fee(fills: Sequence[Fill], rate: float, exponent: float = 1.0) -> float:
    """Each price level crossed is a separate match against a resting maker, so each
    is charged and rounded on its own."""
    if rate <= 0:
        return 0.0
    return round(sum(polymarket_taker_fee(shares, price, rate, exponent) for price, shares in fills), 5)


def polymarket_fee_model(fills: Sequence[Fill], market: MarketInfo, ts: int | None = None) -> float:
    """The fee for one order, at the rate ``rate_for`` picks for this market and time."""
    rate, _ = rate_for(market, ts)
    return taker_fee(fills, rate, market.fee_exponent or 1.0)


FEE_MODELS: dict[str, FeeModel] = {"polymarket": polymarket_fee_model}


def fee_model(venue: str) -> FeeModel:
    try:
        return FEE_MODELS[venue]
    except KeyError:
        raise ValueError(f"no fee model for venue {venue!r}") from None
