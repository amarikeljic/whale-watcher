"""Order precision on both venues (v2.1 Part 4 #7): paper fills in the amounts the
exchanges would accept, not in floating-point dust.

Polymarket, https://docs.polymarket.com/trading/place-orders.md ("Calculate the Order
Amounts", read 2026-11-14):

* the share quantity is rounded DOWN to the size decimals, which are 2 at every tick
  size (so shares come in steps of 0.01);
* the USD amount (price x size), when it has more decimals than the tick's amount
  decimals, is rounded UP to amount decimals + 4 and then DOWN to the amount decimals;
* "the SDK rounds BUY shares down to supported precision without increasing the
  signed spend", so a buy never spends more than asked.

  | tick   | price decimals | size decimals | amount decimals |
  | 0.1    | 1              | 2             | 3               |
  | 0.01   | 2              | 2             | 4               |
  | 0.005  | 3              | 2             | 5               |
  | 0.0025 | 4              | 2             | 6               |
  | 0.001  | 3              | 2             | 5               |
  | 0.0001 | 4              | 2             | 6               |

Polymarket US, https://docs.polymarket.us/api-reference/orders/create-order.md (read
2026-11-14): quantity is in contracts and "supports decimal quantities on markets whose
minimumTradeQty is less than 1"; minimumTradeQty is the minimum order (0.01 or 1 on every
open market checked). The docs give no finer step, so the step taken is the market's
minimum: 0.01 contracts, or whole contracts where the minimum is 1. The US venue rounds
its fee to the cent (polymarket_us.us_taker_fee); no rounding of the amount is documented.

Shares rounded off a walk come off the worst level first: the order is the same order,
slightly smaller. A sell that cannot sell a fraction below the step leaves it unsold;
it is worth under a cent and is dropped.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SHARE_STEP = 0.01
AMOUNT_DECIMALS = {0.1: 3, 0.01: 4, 0.005: 5, 0.0025: 6, 0.001: 5, 0.0001: 6}
DEFAULT_AMOUNT_DECIMALS = 4          # the 0.01 tick, the most common one


def amount_decimals(tick: float | None) -> int:
    if not tick:
        return DEFAULT_AMOUNT_DECIMALS
    for t, d in AMOUNT_DECIMALS.items():
        if abs(t - tick) < 1e-12:
            return d
    return max(AMOUNT_DECIMALS.values())


# A walk's arithmetic in floats leaves dust: 22.72 - 10 is 12.719999999999999. Rounding
# that down would drop a whole step, so a quantity this close below a step counts as it.
_DUST = Decimal("1e-9")


def _d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def _steps(x: Decimal, step: Decimal) -> Decimal:
    return (x / step + _DUST).to_integral_value(ROUND_FLOOR)


def floor_step(x: float, step: float) -> float:
    """``x`` rounded down to a whole number of ``step``."""
    s = _d(step)
    return float(_steps(_d(x), s) * s)


def floor_cents(x: float) -> float:
    return floor_step(x, 0.01)


def ceil_cents(x: float) -> float:
    """``x`` rounded up to the cent (a hair of float dust above a cent does not count)."""
    c = Decimal("0.01")
    return float((_d(x) / c - _DUST).to_integral_value(ROUND_CEILING) * c)


def exact_ratio(amount: float, shares: float) -> float:
    """A price from a rounded amount and share count, divided in decimal: $9.9968 for
    22.72 shares is 0.44, not 0.44000000000000006."""
    return float(_d(amount) / _d(shares))


def round_amount(amount: Decimal, decimals: int) -> Decimal:
    """Up to ``decimals + 4``, then down to ``decimals``: Polymarket's rule."""
    up = amount.quantize(Decimal(1).scaleb(-(decimals + 4)), ROUND_CEILING)
    return up.quantize(Decimal(1).scaleb(-decimals), ROUND_FLOOR)


def round_fills(fills: list[tuple[float, float]], *, step: float = SHARE_STEP,
                decimals: int | None = DEFAULT_AMOUNT_DECIMALS) -> tuple[list[tuple[float, float]], float, float] | None:
    """(fills, shares, amount) for a walk's ``(price, shares)`` fills once the total is a
    whole number of ``step`` (the excess taken off the worst level) and the amount is at
    ``decimals`` (None: left exact). None when less than one step is left."""
    total = sum((_d(s) for _, s in fills), Decimal(0))
    s = _d(step)
    kept = _steps(total, s) * s
    if kept <= 0:
        return None
    excess = total - kept
    out = [(p, _d(q)) for p, q in fills]
    while excess > 0 and out:
        p, q = out[-1]
        if q <= excess:
            out.pop()
            excess -= q
        else:
            out[-1] = (p, q - excess)
            excess = Decimal(0)
    amount = sum((_d(p) * q for p, q in out), Decimal(0))
    if decimals is not None:
        amount = round_amount(amount, decimals)
    return [(p, float(q)) for p, q in out], float(kept), float(amount)
