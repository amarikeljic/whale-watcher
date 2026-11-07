"""Accounting from activity rows, checked by hand."""

import pytest

from conftest import act
from whalewatcher.ledger import build_ledgers, entry_vwap


def test_buy_and_hold_to_a_win():
    led = build_ledgers([act(1, size=200, price=0.4)], as_of=10)["0xc1"]
    assert led.cost == pytest.approx(80)
    assert led.pnl([1.0, 0.0]) == pytest.approx(120)    # 200 * 1 - 80
    assert led.pnl([0.0, 1.0]) == pytest.approx(-80)
    assert led.pnl([0.5, 0.5]) == pytest.approx(20)     # a cancelled fight pays 0.50 a share


def test_partial_sell_then_resolution():
    rows = [act(1, size=200, price=0.4), act(2, side="SELL", size=50, price=0.6)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    # cash: -80 + 30 = -50; held 150 shares * 1.0
    assert led.pnl([1.0, 0.0]) == pytest.approx(100)


def test_redemption_before_as_of_gives_the_same_pnl_as_holding():
    held = build_ledgers([act(1, size=200, price=0.4)], as_of=10)["0xc1"]
    redeemed = build_ledgers([act(1, size=200, price=0.4),
                              act(5, "REDEEM", size=200, price=0, usdc=200)], as_of=10)["0xc1"]
    assert redeemed.pnl([1.0, 0.0]) == pytest.approx(held.pnl([1.0, 0.0]))


def test_rows_after_as_of_are_ignored():
    rows = [act(1, size=100, price=0.5), act(50, size=1000, price=0.9)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    assert led.cost == pytest.approx(50)


def test_split_and_merge():
    rows = [act(1, "SPLIT", size=100, price=0.5, usdc=100, index=None),
            act(2, side="SELL", size=100, price=0.7, index=1),     # sell the NO side
            act(3, "MERGE", size=0, usdc=0, index=None)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    assert led.two_sided and led.has_split
    # paid 100, got 70, holds 100 YES
    assert led.pnl([1.0, 0.0]) == pytest.approx(70)
    assert led.pnl([0.0, 1.0]) == pytest.approx(-30)


def test_buying_both_sides_is_two_sided():
    rows = [act(1, size=10, index=0), act(2, size=10, index=1)]
    assert build_ledgers(rows, as_of=10)["0xc1"].two_sided


def test_selling_shares_never_bought_marks_the_market_incomplete():
    rows = [act(5, side="SELL", size=100, price=0.9)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    assert led.incomplete and not led.reliable


def test_conversions_make_a_market_unreliable():
    rows = [act(1, size=10), act(2, "CONVERSION", size=20, usdc=160, index=None)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    assert led.has_conversion and not led.reliable


def test_same_second_buy_then_sell_is_not_negative():
    rows = [act(5, side="SELL", size=100, price=0.6), act(5, side="BUY", size=100, price=0.5)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    assert not led.incomplete


def test_entry_vwap_before_a_cutoff():
    rows = [act(1, size=100, price=0.4), act(2, size=100, price=0.6), act(9, size=100, price=0.9)]
    led = build_ledgers(rows, as_of=10)["0xc1"]
    vwap, shares, usd = entry_vwap(led.buys, before=5)
    assert (vwap, shares, usd) == (pytest.approx(0.5), 200, pytest.approx(100))
