"""Fee models, one pluggable function per venue.

A fee model takes the fills of one order (price, shares per level crossed) and
the market, and returns the fee in USD. Kalshi will plug in here when Kalshi
paper trading comes back; see docs/API_NOTES.md for its verified fee fields.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal

from .polymarket import MarketInfo

Fill = tuple[float, float]  # (price, shares)
FeeModel = Callable[[Sequence[Fill], MarketInfo], float]

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


def polymarket_fee_model(fills: Sequence[Fill], market: MarketInfo) -> float:
    """Each price level crossed is a separate match against a resting maker, so
    each is charged and rounded on its own. The rate is the market's own
    ``feeSchedule.rate``, not the category table: live markets differ from the
    table (a UFC market at 0.03 where the table says sports pay 0.05), and the
    2026-03-31 changelog says to calculate from ``feeSchedule``."""
    if not market.fees_enabled or market.fee_rate <= 0:
        return 0.0
    return round(sum(polymarket_taker_fee(shares, price, market.fee_rate, market.fee_exponent)
                     for price, shares in fills), 5)


FEE_MODELS: dict[str, FeeModel] = {"polymarket": polymarket_fee_model}


def fee_model(venue: str) -> FeeModel:
    try:
        return FEE_MODELS[venue]
    except KeyError:
        raise ValueError(f"no fee model for venue {venue!r}") from None
