"""Kalshi: the fee, ticker and name parsing, game matching (with the traps found on real data),
quotes, the copy rule, and the cross-venue backtest end to end. Every call goes to FakeHttp."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from conftest import DAY
from whalewatcher.backtest import AlertEvent, run_backtest, simulate_kalshi
from whalewatcher.kalshi import (
    KalshiIndex,
    KalshiMarket,
    Quote,
    event_date,
    match,
    name_similarity,
    parse_candles,
    poly_game,
    quote_at,
    taker_fee,
    windows,
)
from whalewatcher.optimize import Fold, FoldWallet
from whalewatcher.polymarket import MarketInfo
from whalewatcher.scoring import WalletMetrics
from whalewatcher.stats import render
from whalewatcher.timeutil import iso
from world import FakeMarket, World

T0 = 1_791_133_200          # 2026-10-04 17:00 UTC, a whole minute


def km(ticker: str, name: str, *, series: str, result: str = "", value: float | None = None, open_time: int = 0,
       close_time: int | None = None, expiry: int | None = None) -> KalshiMarket:
    event = ticker.rsplit("-", 1)[0]
    return KalshiMarket(ticker=ticker, event_ticker=event, series=series, code=ticker.rsplit("-", 1)[-1], name=name,
                        event_date=event_date(event, series), status="finalized" if result else "active",
                        result=result, settlement_value=value, open_time=open_time, close_time=close_time,
                        expected_expiration=expiry, settled_at=close_time, archived=True)


def pm(slug: str, outcomes: tuple[str, str], *, question: str = "", event_title: str = "",
       start: int | None = None, cond: str = "0xg") -> MarketInfo:
    return MarketInfo(condition_id=cond, question=question or event_title, slug=slug, event_slug=slug,
                      event_title=event_title, category="sports", outcomes=list(outcomes),
                      token_ids=[f"{cond}-t0", f"{cond}-t1"], game_start_time=start, sports_market_type="moneyline")


def test_the_taker_fee_follows_the_schedule_and_rounds_up_to_the_cent():
    assert taker_fee(100, 0.5) == 1.75, "0.07 x 100 x 0.5 x 0.5"
    assert taker_fee(10, 0.5) == 0.18, "0.175 rounds up"
    assert taker_fee(10, 0.5, multiplier=0.5) == 0.09, "KXMLBGAME's 0.5 multiplier"
    assert taker_fee(0, 0.5) == 0.0 and taker_fee(10, 1.0) == 0.0


def test_event_tickers_carry_the_game_date():
    assert event_date("KXNFLGAME-26OCT04DALHOU", "KXNFLGAME") == date(2026, 10, 4)
    assert event_date("KXMLBGAME-26SEP251305CHCBOSG1", "KXMLBGAME") == date(2026, 9, 25), "doubleheader game 1"
    assert event_date("KXNFLGAME-NOTADATE", "KXNFLGAME") is None


@pytest.mark.parametrize(("a", "b", "score"), [
    ("Liverpool FC", "Liverpool", 2),
    ("FC Internazionale Milano", "Inter Milan", 2),
    ("Los Angeles Lakers", "Los Angeles L", 2),
    ("Los Angeles Clippers", "Los Angeles L", 0),
    ("West Virginia", "Virginia", 1),              # contained, not equal: must lose to an exact pair
    ("Shuai Zhang", "Zhang Shuai", 2),
    ("Jose Henrique", "Jose Delano", 0),           # a shared first name is not a match
    ("Florida State", "Florida St.", 2),
])
def test_names_pair_exactly_before_partially(a, b, score):
    assert name_similarity(a, b) == score


def nfl_index() -> KalshiIndex:
    return KalshiIndex([
        km("KXNFLGAME-26OCT04DALHOU-DAL", "Dallas", series="KXNFLGAME", expiry=T0 + 3 * 3600),
        km("KXNFLGAME-26OCT04DALHOU-HOU", "Houston", series="KXNFLGAME", expiry=T0 + 3 * 3600),
        km("KXNFLGAME-26OCT04ARINYG-ARI", "Arizona", series="KXNFLGAME", expiry=T0 + 6 * 3600),
        km("KXNFLGAME-26OCT04ARINYG-NYG", "New York G", series="KXNFLGAME", expiry=T0 + 6 * 3600),
    ])


def test_two_way_games_match_by_code_and_each_outcome_buys_its_own_team():
    legs = match(pm("nfl-dal-hou-2026-10-04", ("Cowboys", "Texans"), start=T0), nfl_index())
    assert {i: (leg.ticker, leg.side) for i, leg in legs.items()} == {
        0: ("KXNFLGAME-26OCT04DALHOU-DAL", "yes"), 1: ("KXNFLGAME-26OCT04DALHOU-HOU", "yes")}
    assert legs[0].method == "code+code"
    spread = dataclasses.replace(pm("nfl-dal-hou-2026-10-04", ("Cowboys", "Texans"), start=T0),
                                 sports_market_type="spreads")
    assert match(spread, nfl_index()) is None, "only game-winner markets are matched"


def test_soccer_markets_buy_yes_or_no_on_the_team_or_the_tie():
    index = KalshiIndex([km(f"KXEPLGAME-26MAY09LFCCFC-{c}", n, series="KXEPLGAME")
                         for c, n in (("LFC", "Liverpool"), ("TIE", "Tie"), ("CFC", "Chelsea"))])
    title = "Liverpool FC vs. Chelsea FC"
    win = match(pm("epl-liv-che-2026-05-09-liv", ("Yes", "No"), question="Will Liverpool FC win on 2026-05-09?",
                   event_title=title), index)
    assert {i: (leg.ticker, leg.side) for i, leg in win.items()} == {
        0: ("KXEPLGAME-26MAY09LFCCFC-LFC", "yes"), 1: ("KXEPLGAME-26MAY09LFCCFC-LFC", "no")}
    assert win[0].method == "name+name", "LFC/CFC are not Polymarket's liv/che: the names carry it"
    draw = match(pm("epl-liv-che-2026-05-09-draw", ("Yes", "No"), question="Will Liverpool FC vs. Chelsea FC end "
                    "in a draw?", event_title=title), index)
    assert (draw[0].ticker, draw[0].side, draw[1].side) == ("KXEPLGAME-26MAY09LFCCFC-TIE", "yes", "no")


def test_a_team_named_inside_another_does_not_take_its_market():
    index = KalshiIndex([km("KXNCAAFGAME-26SEP19WVUUVA-WVU", "West Virginia", series="KXNCAAFGAME"),
                         km("KXNCAAFGAME-26SEP19WVUUVA-UVA", "Virginia", series="KXNCAAFGAME")])
    legs = match(pm("cfb-wvir-vir-2026-09-19", ("West Virginia", "Virginia")), index)
    assert (legs[0].ticker, legs[1].ticker) == ("KXNCAAFGAME-26SEP19WVUUVA-WVU", "KXNCAAFGAME-26SEP19WVUUVA-UVA")


def test_a_day_apart_is_allowed_except_in_baseball():
    football = KalshiIndex([km("KXNCAAFGAME-26OCT30WISIOWA-WIS", "Wisconsin", series="KXNCAAFGAME"),
                            km("KXNCAAFGAME-26OCT30WISIOWA-IOWA", "Iowa", series="KXNCAAFGAME")])
    legs = match(pm("cfb-wisc-iowa-2026-10-31", ("Wisconsin", "Iowa")), football)
    assert legs[0].ticker.endswith("-WIS") and legs[0].method.endswith(", 1d")
    baseball = KalshiIndex([km("KXMLBGAME-26SEP261915CHCBOS-CHC", "Chicago C", series="KXMLBGAME"),
                            km("KXMLBGAME-26SEP261915CHCBOS-BOS", "Boston", series="KXMLBGAME")])
    assert match(pm("mlb-chc-bos-2026-09-27", ("Chicago Cubs", "Boston Red Sox")), baseball) is None, \
        "the previous day's game in the same series is a different game"


def test_poly_game_reads_the_slug_and_title():
    g = poly_game(pm("ucl-psg-ars-2026-05-30-ars", ("Yes", "No"), question="Will Arsenal FC win on 2026-05-30?",
                     event_title="Paris Saint-Germain FC vs. Arsenal FC"))
    assert (g.league, g.date, g.codes, g.names, g.subject) == (
        "ucl", date(2026, 5, 30), ("psg", "ars"), ("Paris Saint-Germain FC", "Arsenal FC"), 1)
    assert poly_game(pm("m-0xpol", ("Yes", "No"))) is None


def test_quotes_parse_both_candle_shapes_and_the_no_side_pays_one_minus_the_bid():
    archived = {"candlesticks": [{"end_period_ts": 120, "yes_bid": {"close": "0.4900"},
                                  "yes_ask": {"close": "0.5000"}}]}
    live = {"candlesticks": [{"end_period_ts": 180, "yes_bid": {"close_dollars": "0.6000"},
                              "yes_ask": {"close_dollars": "0.6200"}}]}
    qs = sorted(parse_candles(archived) + parse_candles(live), key=lambda q: q.ts)
    assert [(q.ts, q.yes_bid, q.yes_ask) for q in qs] == [(120, 0.49, 0.50), (180, 0.60, 0.62)]
    assert quote_at(qs, 61).ts == 120 and quote_at(qs, 121).ts == 180 and quote_at(qs, 5000) is None
    assert qs[0].ask("yes") == 0.50 and qs[0].ask("no") == pytest.approx(0.51)
    assert Quote(0, 0.0, 0.01).ask("no") is None, "no bids on YES: nobody sells NO"
    assert Quote(0, 0.99, 1.0).ask("yes") is None, "no offers"
    assert windows([1000, 1033, 1090]) == [(880, 2110)] and len(windows([0, 10**6])) == 2


def event(trigger: int = T0) -> AlertEvent:
    return AlertEvent(wallet="0xw", condition_id="0xnfl", token_id="0xnfl-t0", outcome="Cowboys", outcome_index=0,
                      first_fill_ts=trigger - 40, trigger_ts=trigger, last_fill_ts=trigger, whale_price=0.40,
                      whale_usd=600, whale_shares=1500, fills=2)


def quotes(fill_ask: float, fill_bid: float = 0.30) -> list[Quote]:
    # trigger T0 -> candle ending T0; detection T0+33 -> ending T0+60; fill T0+90 -> ending T0+120
    return [Quote(T0, 0.39, 0.40), Quote(T0 + 60, 0.40, 0.41), Quote(T0 + 120, fill_bid, fill_ask)]


COPY = dict(strategy="backtest:test", stake=10.0, delay=90, max_slippage=0.03, fee_multiplier=1.0)


def test_the_kalshi_copy_fills_at_kalshis_ask_pays_kalshis_fee_and_settles_on_kalshi():
    won = km("KXNFLGAME-26OCT04DALHOU-DAL", "Dallas", series="KXNFLGAME", result="yes", value=1.0,
             close_time=T0 + 4 * 3600)
    t = simulate_kalshi(event(), won, "yes", quotes(0.43), **COPY)
    contracts = 10 / 0.43
    fee = taker_fee(contracts, 0.43)
    assert t.venue == "kalshi" and t.contract == "KXNFLGAME-26OCT04DALHOU-DAL:yes"
    assert t.status == "FILLED" and t.fill_price == 0.43 and t.fill_ts == T0 + 120
    assert t.fees == fee > 0 and t.cost == pytest.approx(10 + fee)
    assert t.settled["result"] == "WIN" and t.settled["pnl"] == pytest.approx(contracts - 10 - fee)
    assert t.zero[0] == 0.40 and t.atask[0] == 0.41, "shadows at Kalshi's own trigger-time and alert-time asks"

    assert simulate_kalshi(event(), won, "yes", quotes(0.45), **COPY).miss_reason.startswith("slippage"), \
        "0.04 above the 0.41 alert-time ask"
    no = simulate_kalshi(event(), won, "no", quotes(0.43, fill_bid=0.57), **COPY)
    assert no.fill_price == pytest.approx(0.43) and no.settled["payout_per_share"] == 0.0
    assert no.settled["pnl"] == pytest.approx(-no.cost), "NO loses when the market settles YES"
    early = km("KXNFLGAME-26OCT04DALHOU-DAL", "Dallas", series="KXNFLGAME", close_time=T0 + 60)
    assert simulate_kalshi(event(), early, "yes", quotes(0.41), **COPY).miss_reason == "market closed before the fill"


# -- end to end -------------------------------------------------------------------------------------

W = "0xwhale"
KAPI = "/trade-api/v2"


def kalshi_row(ticker: str, name: str, result: str) -> dict:
    t = lambda ts: iso(ts).replace(" ", "T")  # noqa: E731
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_sub_title": name, "status": "finalized",
            "result": result, "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
            "open_time": t(T0 - 5 * DAY), "close_time": t(T0 + 4 * 3600), "expected_expiration_time": t(T0 + 3 * 3600),
            "settlement_ts": t(T0 + 4 * 3600 + 300)}


def candles(start: int, end: int) -> dict:
    out = []
    for e in range(start - start % 60 + 60, end + 1, 60):
        ask = 0.40 if e <= T0 else (0.41 if e <= T0 + 60 else 0.42)
        out.append({"end_period_ts": e, "yes_bid": {"close": f"{ask - 0.01:.4f}"}, "yes_ask": {"close": f"{ask:.4f}"}})
    return {"ticker": "x", "candlesticks": out}


async def test_the_backtest_copies_matched_games_on_kalshi_beside_polymarket(app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xnfl", resolved_at=T0 + 4 * 3600, payouts=[1.0, 0.0], game_start=T0 - 3600,
                                slug="nfl-dal-hou-2026-10-04", outcomes=("Cowboys", "Texans"),
                                sports_market_type="moneyline", event="nfl-dal-hou-2026-10-04",
                                event_title="Cowboys vs. Texans", tags=["sports"]))
    world.add_market(FakeMarket("0xpol", end_date=T0 + 30 * DAY))
    world.trade(W, T0, "0xnfl", size=1500, price=0.40)                  # $600 on the Cowboys, in-play
    world.trade(W, T0 + 600, "0xpol", size=1500, price=0.40)            # politics: no Kalshi game
    world.clob_history = lambda token, s, e: [(s + 30, 0.41)]
    rows = [kalshi_row("KXNFLGAME-26OCT04DALHOU-DAL", "Dallas", "yes"),
            kalshi_row("KXNFLGAME-26OCT04DALHOU-HOU", "Houston", "no")]
    fake_http.route(f"{KAPI}/historical/markets",
                    lambda p: {"markets": rows if p.get("series_ticker") == "KXNFLGAME" else [], "cursor": ""})
    fake_http.route(f"{KAPI}/markets", {"markets": [], "cursor": ""})
    fake_http.route(f"{KAPI}/series/KXNFLGAME", {"series": {"ticker": "KXNFLGAME", "fee_type": "quadratic",
                                                            "fee_multiplier": 1}})
    fake_http.route(f"{KAPI}/historical/markets/KXNFLGAME-26OCT04DALHOU-DAL/candlesticks",
                    lambda p: candles(int(p["start_ts"]), int(p["end_ts"])))
    m = WalletMetrics(W, T0, settled_markets=80, total_pnl=900, total_cost=6000, roi=0.15, profitable_months=5,
                      months_in_window=6, max_single_market_share=0.1, avg_stake_usd=300, days_since_last_trade=1,
                      two_sided_ratio=0.1, win_rate=0.6, clv=0.02, clv_n=40)
    fold = Fold(T0 - 2 * DAY, T0 + 10 * DAY, 1, 0, [FoldWallet(W, m, [])])

    report = await run_backtest(app, [fold], kalshi=True)
    poly = [t for t in report.trades if t.venue == "polymarket"]
    (k,) = [t for t in report.trades if t.venue == "kalshi"]
    assert len(poly) == 2 and k.event.condition_id == "0xnfl"
    assert report.skipped[("backtest:spec on kalshi", "not a game-winner market")] == 1
    assert k.status == "FILLED" and k.fill_price == pytest.approx(0.42) and k.settled["result"] == "WIN"
    assert any("Kalshi" in line and "n=1" in line for line in report.comparison)
    stored = app.db.one("SELECT venue, token_id FROM paper_trades WHERE venue = 'kalshi'")
    assert stored["token_id"] == "kalshi:KXNFLGAME-26OCT04DALHOU-DAL:yes"
    assert "kalshi / backtest:spec (SIMULATED)" in render(app)

    calls = len(fake_http.calls)
    await run_backtest(app, [fold], kalshi=True)
    kalshi_calls = [c for c in fake_http.calls[calls:] if c[1].startswith(KAPI)]
    assert kalshi_calls == [], "market lists and quotes come from the cache the second time"
    assert app.db.one("SELECT COUNT(*) AS n FROM paper_trades WHERE venue = 'kalshi'")["n"] == 1
