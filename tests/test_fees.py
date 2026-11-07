"""Fee model checked against the published table (https://docs.polymarket.com/trading/fees.md)."""

import pytest

from conftest import market
from whalewatcher.fees import fee_model, polymarket_fee_model, polymarket_taker_fee


@pytest.mark.parametrize("price, rate, expected", [
    # 100 shares, from the docs' fee tables
    (0.50, 0.07, 1.75), (0.30, 0.07, 1.47), (0.05, 0.07, 0.33), (0.99, 0.07, 0.07),
    (0.50, 0.05, 1.25), (0.20, 0.05, 0.80), (0.50, 0.04, 1.00), (0.10, 0.04, 0.36),
])
def test_matches_the_published_tables(price, rate, expected):
    assert polymarket_taker_fee(100, price, rate) == pytest.approx(expected, abs=0.006)


def test_rounds_to_five_decimals_and_tiny_fees_vanish():
    assert polymarket_taker_fee(1, 0.5, 0.04) == 0.01
    assert polymarket_taker_fee(0.0001, 0.01, 0.04) == 0.0


def test_fee_free_market_and_per_level_charging():
    free = market(fee_rate=0.0)
    assert polymarket_fee_model([(0.5, 100)], free) == 0.0
    paid = market(fee_rate=0.05)
    two_levels = polymarket_fee_model([(0.40, 10), (0.41, 10)], paid)
    assert two_levels == pytest.approx(polymarket_taker_fee(10, 0.40, 0.05) + polymarket_taker_fee(10, 0.41, 0.05))


def test_registry():
    assert fee_model("polymarket") is polymarket_fee_model
    with pytest.raises(ValueError):
        fee_model("nowhere")
