"""Part 4 #7: paper fills in the amounts the exchanges accept (precision.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from whalewatcher.paper import rounded, walk_asks
from whalewatcher.polymarket import Book, Level
from whalewatcher.precision import (amount_decimals, ceil_cents, floor_cents, floor_step, round_amount,
                                    round_fills)


def book(*asks: tuple[float, float]) -> Book:
    return Book(token_id="t", ts=None, bids=[], asks=[Level(p, s) for p, s in asks])


def test_the_tick_sets_the_amount_decimals_as_the_docs_table_says():
    assert [amount_decimals(t) for t in (0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001)] == [3, 4, 5, 6, 5, 6]
    assert amount_decimals(None) == 4, "no tick recorded: the common 0.01 tick"


def test_shares_are_rounded_down_and_the_excess_comes_off_the_worst_level():
    fills, shares, amount = round_fills([(0.50, 10.0), (0.52, 15 / 0.52)])
    assert shares == 38.84 and fills == [(0.50, 10.0), (0.52, 28.84)]
    assert amount == pytest.approx(5 + 0.52 * 28.84) and amount <= 20.0, "never more than the stake"
    assert round_fills([(0.5, 0.004)]) is None, "less than one 0.01 step is no order"


def test_float_dust_does_not_cost_a_whole_step():
    assert floor_step(22.72, 0.01) == 22.72 and floor_step(0.1 + 0.2, 0.1) == pytest.approx(0.3)
    assert round_fills([(0.50, 10.0), (0.48, 22.72 - 10)])[1] == 22.72


def test_the_amount_rule_rounds_up_to_four_more_decimals_then_down():
    assert round_amount(Decimal("1.234567891"), 4) == Decimal("1.2345")
    assert round_amount(Decimal("1.23449999999"), 4) == Decimal("1.2345"), "up first, to 8 decimals: 1.23450000"
    assert round_amount(Decimal("1.2344999"), 4) == Decimal("1.2344")


def test_stakes_round_down_and_the_smallest_order_rounds_up_to_the_cent():
    assert floor_cents(7.8899) == 7.88 and floor_cents(8.51) == 8.51
    assert ceil_cents(2.181) == 2.19 and ceil_cents(2.5) == 2.5 and ceil_cents(2.5000000000000004) == 2.5
    for price in (0.437, 0.123, 0.5, 0.99, 0.011):
        assert floor_step(ceil_cents(5 * price) / price, 0.01) >= 5, f"5 shares at {price} must stay 5 shares"


def test_a_walked_buy_never_spends_more_than_the_stake():
    for stake in (1.0, 7.88, 10.0, 33.33, 99.99):
        for asks in (((0.437, 7.0), (0.441, 3.3), (0.6, 1000)), ((0.01, 10_000),), ((0.99, 5000),)):
            walk = rounded(walk_asks(book(*asks), stake), decimals=amount_decimals(0.001))
            assert walk is not None
            assert walk.vwap * walk.contracts <= stake + 1e-9
            assert round(walk.contracts, 2) == walk.contracts, "whole 0.01 steps"


def test_a_us_market_with_a_minimum_of_one_trades_whole_contracts():
    walk = rounded(walk_asks(book((0.44, 1000)), 10.0), step=1.0, decimals=None)
    assert walk.contracts == 22 and walk.vwap == pytest.approx(0.44), "22.7 contracts: 22 whole ones"
