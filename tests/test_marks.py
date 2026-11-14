"""Part 4 #8: open positions valued conservatively (what selling all of each would fetch,
down the recorded bids, after the fee), and Polymarket US positions marked from the US book."""

from __future__ import annotations

import json

import pytest

from conftest import NOW
from test_bankroll import sig, wallet_of, whale_buy
from test_polymarket_us import COND, US_SLUG, setup, signal
from whalewatcher import bankroll as br
from whalewatcher.bankroll import Marks, Params, run
from whalewatcher.stats import load_bot_trades

PLAIN = Params(amount=100.0, onramp_cost_pct=0.0)


def test_the_conservative_value_walks_the_bids_for_the_whole_position():
    # 16 shares bought at 0.50. Marked at a best bid of 0.48, but only 10 shares sit there,
    # 4 more at 0.40, and nothing below: 2 shares would not sell at all.
    depth = json.dumps([[0.48, 10], [0.40, 4]])
    marks = Marks([("tok1", 150, 0.48, depth)])
    res = run([sig(1, 100, 0.50)], PLAIN, marks=marks, as_of=200)
    assert res.equity == pytest.approx(92 + 16 * 0.48), "equity: every share at the best bid"
    assert res.conservative_equity == pytest.approx(92 + 10 * 0.48 + 4 * 0.40), "the bids, and 0 for the rest"
    assert res.summary()["conservative_equity"] == pytest.approx(98.4)


def test_the_sale_fee_comes_off_and_it_is_never_above_equity():
    depth = json.dumps([[0.48, 100]])
    fee_rate = 0.05
    res = run([sig(1, 100, 0.50, fee_rate=fee_rate)], PLAIN, marks=Marks([("tok1", 150, 0.48, depth)]), as_of=200)
    (e,) = res.entries
    fee = round(e.contracts * fee_rate * 0.48 * 0.52, 5)
    assert res.conservative_equity == pytest.approx(res.cash + e.contracts * 0.48 - fee)
    deep = run([sig(1, 100, 0.50)], PLAIN, marks=Marks([("tok1", 150, 0.48, json.dumps([[0.60, 100]]))]), as_of=200)
    assert deep.conservative_equity == pytest.approx(deep.equity), "a bid above the mark does not make it richer"


def test_a_mark_without_depth_is_the_best_bid_less_the_fee():
    res = run([sig(1, 100, 0.50, fee_rate=0.05)], PLAIN, marks=Marks([("tok1", 150, 0.48)]), as_of=200)
    (e,) = res.entries
    assert res.conservative_equity == pytest.approx(res.cash + e.contracts * 0.48 - round(e.contracts * 0.05 * 0.2496, 5))
    unmarked = run([sig(1, 100, 0.50)], PLAIN, as_of=200)
    assert unmarked.conservative_equity == pytest.approx(unmarked.equity), "no mark, no fee: at the price paid"


async def test_the_bot_keeps_the_bids_behind_each_mark(live_app, fake_http, clock):
    await whale_buy(live_app, fake_http, bids=((0.42, 5), (0.41, 500)))
    row = live_app.db.one("SELECT * FROM position_marks")
    assert row["best_bid"] == 0.42 and json.loads(row["bids_json"]) == [[0.42, 5.0], [0.41, 500.0]]
    res = wallet_of(live_app)
    (e,) = res.entries
    assert res.conservative_equity == pytest.approx(res.cash + 5 * 0.42 + (e.contracts - 5) * 0.41)
    assert res.conservative_equity < res.equity


async def test_us_positions_are_marked_from_the_us_book_not_the_international_one(live_app, fake_http, clock):
    world, venue, monitor, row_a = setup(live_app, fake_http, bids=((0.42, 500),), offers=((0.44, 500),))
    await signal(world, monitor, row_a)                       # long the Jaguars on the US book, at 0.44
    assert await row_a.mark_positions() == 1
    (mark,) = live_app.db.query("SELECT * FROM position_marks WHERE token_id LIKE 'us:%'")
    assert mark["token_id"] == f"us:{US_SLUG}:long" and mark["best_bid"] == 0.42
    venue.book(US_SLUG, ((0.30, 500),), ((0.44, 500),))       # the US bid falls; the international one does not
    clock.advance(301)
    await row_a.mark_positions()
    us_view = run(br.signals_by_source(load_bot_trades(live_app), live_app.cfg)["bot:us"], Params.of(live_app.cfg, 100.0),
                  marks=br.load_marks(live_app.db), as_of=clock.now + 1)
    (e,) = [x for x in us_view.entries if x.status == "TAKEN"]
    assert us_view.equity == pytest.approx(us_view.cash + e.contracts * 0.30), "the US position at the US bid"
    intl = live_app.db.one("SELECT best_bid FROM position_marks WHERE token_id = ?", (f"{COND}-t0",))
    assert intl is None or intl["best_bid"] != 0.30


async def test_a_us_short_is_worth_one_minus_the_offer(live_app, fake_http, clock):
    world, venue, monitor, row_a = setup(live_app, fake_http, bids=((0.44, 500),), offers=((0.47, 500),))
    await signal(world, monitor, row_a, index=1, price=0.55)  # the Titans: a short of the Jaguars instrument
    await row_a.mark_positions()
    (mark,) = live_app.db.query("SELECT * FROM position_marks WHERE token_id LIKE 'us:%'")
    assert mark["token_id"] == f"us:{US_SLUG}:short" and mark["best_bid"] == pytest.approx(0.53), "1 - 0.47"


def test_the_report_and_the_wallet_page_show_both(tmp_path):
    line = br.result_line(run([sig(1, 100, 0.50)], PLAIN, marks=Marks([("tok1", 150, 0.48, "[[0.48, 1]]")]),
                              as_of=200))
    assert "if every open position were sold into the bids" in line
    from dashfix import small
    from whalewatcher import dashboard as dash
    from whalewatcher.config import Config
    path = str(tmp_path / "ww.sqlite3")
    small(path, NOW).db.close()
    html = dash.render(Config(database_path=path), path, "/bankroll", {}, now=NOW)[1]
    assert "Conservative equity" in html and "every open position sold into the bids, after the fee" in html
