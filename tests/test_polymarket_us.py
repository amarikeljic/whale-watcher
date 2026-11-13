"""v2.1 Part 3b, row A: the same signals priced on Polymarket US. Matching, the
one-instrument book, US fees, the fill, settlement, and that it only ever reads."""

from __future__ import annotations

import json

import pytest

from conftest import DAY, NOW
from whalewatcher import polymarket_us as pus
from whalewatcher.bot import Bot
from whalewatcher.http import HttpError
from whalewatcher.monitor import Monitor
from whalewatcher.paper import PaperEngine
from whalewatcher.polymarket import MarketInfo
from whalewatcher.polymarket_us import UsBook, UsMarket, UsRowA, candidates, pick, side_for, us_taker_fee
from world import FakeMarket, World

WHALE = "0xwhale"
COND = "0xgame"
SLUG = "nfl-jax-ten-2026-11-15"
US_SLUG = "aec-nfl-jax-ten-2026-11-15"
KICKOFF = NOW + 3 * 3600


def intl(slug=SLUG, *, event=None, outcomes=("Jaguars", "Titans"), kind="moneyline", category="sports",
         game_start=KICKOFF, title="") -> MarketInfo:
    return MarketInfo(condition_id=COND, slug=slug, event_slug=event or slug, category=category,
                      outcomes=list(outcomes), token_ids=[f"{COND}-t0", f"{COND}-t1"], sports_market_type=kind,
                      game_start_time=game_start, event_title=title)


def us_raw(slug=US_SLUG, *, sides=(("Jaguars", True), ("Titans", False)), kind="moneyline", start=KICKOFF,
           status="MARKET_STATUS_OPEN", category="sports", fee=0.095, min_qty=0.01) -> dict:
    return {"slug": slug, "question": "Who will win?", "category": category, "marketType": kind, "status": status,
            "closed": status != "MARKET_STATUS_OPEN", "gameStartTime": __import__("whalewatcher.timeutil", fromlist=["iso"]).iso(start),
            "orderPriceMinTickSize": 0.01, "minimumTradeQty": min_qty, "feeCoefficient": fee,
            "outcomes": json.dumps([s[0] for s in sides]),
            "marketSides": [{"description": name, "long": long, "team": {"name": f"Team {name}", "alias": name}}
                            for name, long in sides]}


# -- matching -----------------------------------------------------------------------------------------


def test_a_game_winner_is_looked_for_on_its_own_day_and_the_days_either_side():
    c = candidates(intl("nfl-jax-bal-2026-11-06"))
    assert c.rule == "game_winner" and c.same_bet
    assert c.slugs == ["aec-nfl-jax-bal-2026-11-06", "aec-nfl-jax-bal-2026-11-05", "aec-nfl-jax-bal-2026-11-07"], \
        "an evening game is dated a day earlier on the US venue (Eastern date, not UTC)"


def test_tennis_slugs_are_built_from_the_players_names():
    m = intl("atp-kecmano-rune-2026-11-11", outcomes=("Miomir Kecmanovic", "Holger Rune"),
             title="BNP Paribas Nordic Open: Miomir Kecmanovic vs Holger Rune")
    c = candidates(m)
    assert c.rule == "tennis_winner" and "aec-atp-miokec-holrun-2026-11-11" in c.slugs
    assert "aec-atp-holrun-miokec-2026-11-10" in c.slugs, "either order, either neighbouring day"
    three = candidates(intl("wta-carle-ribera-2026-11-11", title="Sopo: Maria Lourdes Carle vs Marina Bassols Ribera"))
    assert "aec-wta-marcar-marrib-2026-11-11" in three.slugs and "aec-wta-marlou-marbas-2026-11-11" in three.slugs, \
        "a player with three names can be cut two ways: both are tried"
    assert candidates(intl("atp-x-y-2026-11-11", title="Final")).rule == "none"


def test_a_three_way_football_market_maps_to_one_instrument_per_outcome():
    m = intl("uwcl-juv-oly-2026-11-11-draw", event="uwcl-juv-oly-2026-11-11", outcomes=("Yes", "No"))
    c = candidates(m)
    assert c.rule == "three_way" and c.slugs[0] == "atc-uwcl-juv-oly-2026-11-11-draw"
    us = UsMarket.from_api(us_raw("atc-uwcl-juv-oly-2026-11-11-draw", sides=(("Yes", True), ("No", False)),
                                  kind="drawable_outcome"))
    assert side_for(us, m, 0, "three_way") is True and side_for(us, m, 1, "three_way") is False, "No is a short"


def test_crypto_up_or_down_is_found_but_is_not_the_same_bet():
    c = candidates(intl("btc-updown-5m-1793796600", kind=None, category="crypto", outcomes=("Up", "Down")))
    assert c.rule == "crypto_updown" and c.slugs == ["cpc-btc-updown-5m-2026-11-04-1250z"]
    assert c.same_bet is False, "the US market settles on a different price index"
    assert candidates(intl("eth-updown-1h-1793796600", kind=None, category="crypto")).slugs == [
        "cpceth-eth-updown-1h-2026-11-04-1250z"]


def test_what_has_no_rule_is_not_matched_and_says_why():
    assert candidates(intl("nfl-ne-mia-2026-11-01-spread-away-8pt5", event="nfl-ne-mia-2026-11-01",
                           kind="spreads")).why_not == "no rule yet for sports market type spreads"
    politics = candidates(intl("will-the-republicans-win-the-montana-senate-race-in-2026", kind=None,
                               category="politics", game_start=None))
    assert politics.rule == "none" and politics.why_not == "no rule for politics markets"


def test_sides_line_up_by_name_or_not_at_all():
    us = UsMarket.from_api(us_raw(sides=(("Hawks", True), ("Bulls", False))))
    m = intl(outcomes=("Saint Joseph's Hawks", "South Florida Bulls"))
    assert side_for(us, m, 0, "game_winner") is True and side_for(us, m, 1, "game_winner") is False
    twins = UsMarket.from_api(us_raw(sides=(("Hawks", True), ("Hawks", False))))
    assert side_for(twins, m, 0, "game_winner") is None, "two sides answer to the name: no guess"
    assert side_for(us, intl(outcomes=("Eagles", "Giants")), 0, "game_winner") is None
    assert side_for(us, m, None, "game_winner") is None


def test_the_same_teams_on_another_day_or_another_kind_of_market_are_refused():
    m, c = intl(), candidates(intl())
    ok = pick(c, {US_SLUG: UsMarket.from_api(us_raw())}, m, 1)
    assert ok.found and ok.market.slug == US_SLUG and ok.long is False
    late = pick(c, {US_SLUG: UsMarket.from_api(us_raw(start=KICKOFF + 2 * DAY))}, m, 0)
    assert not late.found and late.why_not == "no market with that slug on Polymarket US"
    wrong = pick(c, {US_SLUG: UsMarket.from_api(us_raw(kind="spreads"))}, m, 0)
    assert not wrong.found
    odd = pick(c, {US_SLUG: UsMarket.from_api(us_raw(sides=(("Lions", True), ("Bears", False))))}, m, 0)
    assert not odd.found and "its sides do not line up" in odd.why_not


# -- the book and the fee ---------------------------------------------------------------------------------


def test_the_other_side_is_a_short_of_the_same_instrument():
    book = UsBook.from_api({"bids": [{"px": {"value": "0.44"}, "qty": "300"}, {"px": {"value": "0.45"}, "qty": "100"}],
                            "offers": [{"px": {"value": "0.48"}, "qty": "50"}, {"px": {"value": "0.47"}, "qty": "200"}]},
                           US_SLUG)
    assert book.asks_for(True) == [(0.47, 200.0), (0.48, 50.0)], "the long side lifts the offers, cheapest first"
    assert book.asks_for(False) == [(0.55, 100.0), (0.56, 300.0)], "a short at a bid of 0.45 costs 0.55 a contract"


def test_the_fee_matches_the_examples_in_the_official_schedule():
    """https://docs.polymarket.us/fees.md, standard fee examples at the 0.0695 tier."""
    assert us_taker_fee([(0.10, 100)], 0.0695) == 0.63
    assert us_taker_fee([(0.65, 100)], 0.0695) == 1.58
    assert us_taker_fee([(0.30, 100)], 0.0695) == 1.46
    assert us_taker_fee([(0.90, 100)], 0.0695) == 0.63
    assert us_taker_fee([(0.50, 100)], 0.0695) == 1.74
    assert us_taker_fee([(0.50, 100)], 0.095) == 2.38, "the tier an account under $10,000 a month pays: 2.375, to even"
    assert us_taker_fee([(0.50, 2)], 0.05) == 0.02 and us_taker_fee([(0.50, 2)], 0.07) == 0.04, \
        "half a cent goes to the even cent: 0.025 -> 0.02, 0.035 -> 0.04"
    assert us_taker_fee([(0.50, 10), (0.52, 10)], 0.095) == 0.47, "one rounding for the whole order: 0.2375 + 0.23712"
    assert us_taker_fee([(0.50, 100)], 0) == 0.0


# -- the fill, end to end -------------------------------------------------------------------------------------


class UsVenue:
    """A Polymarket US gateway for the fake HTTP client."""

    def __init__(self, fake_http) -> None:
        self.http, self.markets, self.settled = fake_http, {}, {}
        fake_http.route("/v1/markets", lambda p: {"markets": [self.markets[s] for s in _as_list(p.get("slug"))
                                                              if s in self.markets]})

    def add(self, raw: dict, *, bids=((0.42, 500),), offers=((0.47, 500),)) -> None:
        slug = raw["slug"]
        self.markets[slug] = raw
        self.book(slug, bids, offers)
        self.http.route(f"/v1/markets/{slug}/settlement", lambda _p, s=slug: self._settlement(s))

    def book(self, slug, bids, offers) -> None:
        body = {"marketData": {"marketSlug": slug, "state": "MARKET_STATE_OPEN",
                               "bids": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in bids],
                               "offers": [{"px": {"value": str(p), "currency": "USD"}, "qty": str(q)} for p, q in offers]}}
        self.http.route(f"/v1/markets/{slug}/book", body)

    def _settlement(self, slug):
        if slug not in self.settled:
            raise HttpError("not found", status=404, answered=True)
        return {"slug": slug, "settlement": self.settled[slug]}


def _as_list(value):
    return value if isinstance(value, (list, tuple)) else ([] if value is None else [value])


def setup(app, fake_http, *, market: FakeMarket | None = None, us: dict | None = None, **book):
    app.cfg.polymarket_us.enabled = True
    world = World(fake_http)
    world.add_market(market or FakeMarket(COND, slug=SLUG, event=SLUG, outcomes=("Jaguars", "Titans"),
                                          sports_market_type="moneyline", game_start=KICKOFF, end_date=KICKOFF,
                                          tags=["sports"], fee_rate=0.05))
    world.set_book(f"{COND}-t0", [(0.44, 1000), (0.45, 1000)], [(0.42, 500)])
    world.set_book(f"{COND}-t1", [(0.57, 1000)], [(0.55, 500)])
    venue = UsVenue(fake_http)
    if us is not False:
        venue.add(us or us_raw(), **book)
    app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                   "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    row_a = UsRowA(app)
    monitor = Monitor(app, paper=PaperEngine(app), bot=Bot(app), us=row_a)
    return world, venue, monitor, row_a


async def signal(world, monitor, row_a, *, index=0, price=0.42):
    world.trade(WHALE, NOW - 3, COND, size=1500, price=price, usdc=1500 * price, index=index, feed=True)
    stats = await monitor.poll_once()
    await row_a.drain()
    return stats


def us_row(app):
    return app.db.one("SELECT * FROM bot_trades WHERE runner = 'us'")


async def test_a_signal_is_filled_on_the_us_book_at_us_fees_beside_the_international_fill(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http, offers=((0.44, 10), (0.45, 500)))
    assert (await signal(world, monitor, row_a)).alerts == 1
    t = us_row(live_app)
    assert (t["status"], t["strategy"], t["us_slug"], t["us_side"], t["us_rule"]) == (
        "FILLED", "flat_hold", US_SLUG, "long", "game_winner")
    shares = 10 + (10 - 4.4) / 0.45                                  # $4.40 at 0.44, the other $5.60 at 0.45
    assert t["contracts"] == pytest.approx(shares) and t["fill_price"] == pytest.approx(10 / shares)
    fee = round(0.095 * (10 * 0.44 * 0.56 + (5.6 / 0.45) * 0.45 * 0.55), 2)      # 0.095 x (2.464 + 3.08)
    assert t["fees"] == fee == 0.53 and t["cost"] == pytest.approx(10 + fee)
    assert (t["fee_rate"], t["fee_source"]) == (0.095, "us")
    assert (t["best_ask"], t["intl_best_ask"]) == (0.44, 0.44) and t["price_gap"] == pytest.approx(0.0)
    assert t["limit_price"] == pytest.approx(0.45) and t["min_order_size"] == 0.01
    assert json.loads(t["asks_json"]) == [[0.44, 10.0], [0.45, 500.0]]
    base = live_app.db.one("SELECT * FROM bot_trades WHERE runner = 'single' AND strategy = 'flat_hold'")
    assert base["status"] == "FILLED" and base["fill_price"] == 0.44 and base["fees"] == pytest.approx(
        0.05 * (10 / 0.44) * 0.44 * 0.56), "the international fill is untouched, and costs a third of the US fee"
    assert live_app.db.one("SELECT COUNT(*) n FROM bot_trades WHERE runner = 'us'")["n"] == 1, "one row a signal"


async def test_the_other_team_is_bought_as_a_short_against_the_bids(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http, bids=((0.44, 500),), offers=((0.47, 500),))
    await signal(world, monitor, row_a, index=1, price=0.55)
    t = us_row(live_app)
    assert (t["status"], t["us_side"]) == ("FILLED", "short")
    assert t["fill_price"] == pytest.approx(0.56), "a bid of 0.44 on the Jaguars is 0.56 for the Titans"
    assert t["contracts"] == pytest.approx(10 / 0.56) and t["fees"] == round(0.095 * (10 / 0.56) * 0.56 * 0.44, 2)


async def test_what_is_not_on_the_us_venue_is_skipped_and_counted(live_app, fake_http, clock):
    politics = FakeMarket(COND, slug="will-x-win-the-senate-race", event="senate", end_date=NOW + 2 * DAY)
    world, _, monitor, row_a = setup(live_app, fake_http, market=politics, us=False)
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert (t["status"], t["skip_code"], t["reason"]) == ("SKIPPED", "not_on_us", "not_on_us: no rule for politics markets")
    assert t["us_slug"] is None and t["cost"] is None
    assert fake_http.count("/v1/markets") == 0, "no rule, so nothing was even asked"


async def test_a_game_the_us_venue_does_not_list_is_not_on_us(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http, us=False)
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert t["skip_code"] == "not_on_us" and t["reason"] == "not_on_us: no market with that slug on Polymarket US"
    assert t["us_rule"] == "game_winner"


async def test_crypto_up_or_down_is_not_the_same_bet_unless_the_owner_says_to_include_it(live_app, fake_http, clock):
    start = NOW + 600
    slug = f"btc-updown-5m-{start}"
    at = __import__("datetime").datetime.fromtimestamp(start, __import__("datetime").UTC)
    us_slug = f"cpc-btc-updown-5m-{at:%Y-%m-%d-%H%M}z"
    crypto = FakeMarket(COND, slug=slug, event=slug, outcomes=("Up", "Down"), end_date=start + 300, tags=["crypto"])
    us = us_raw(us_slug, sides=(("Yes", True), ("No", False)), kind="", category="crypto", start=start)
    world, _, monitor, row_a = setup(live_app, fake_http, market=crypto, us=us, offers=((0.45, 500),))
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert (t["status"], t["skip_code"], t["us_slug"]) == ("SKIPPED", "not_same_bet", us_slug)
    assert "settles on a different price index" in t["reason"]


async def test_an_excluded_category_is_left_out_on_the_us_venue_only(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http)
    live_app.cfg.polymarket_us.exclude_categories = ["Sports"]
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert (t["status"], t["skip_code"]) == ("SKIPPED", "us_excluded") and t["us_slug"] == US_SLUG
    assert live_app.db.one("SELECT status FROM bot_trades WHERE runner = 'single' AND strategy = 'flat_hold'")[
        "status"] == "FILLED", "the international runner still trades it"


async def test_a_us_price_past_the_limit_or_a_closed_us_market_is_a_miss(live_app, fake_http, clock):
    world, venue, monitor, row_a = setup(live_app, fake_http, offers=((0.50, 500),))
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert t["status"] == "MISSED" and t["reason"] == "price ran: filling $10 costs 0.500 a share, limit 0.450"
    assert t["best_ask"] == 0.50 and t["price_gap"] == pytest.approx(0.06) and t["fill_price"] == 0.50, \
        "the gap is recorded even when the order would not have filled"


async def test_a_closed_us_market_and_an_empty_book_are_misses(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http, us=us_raw(status="MARKET_STATUS_SUSPENDED"))
    await signal(world, monitor, row_a)
    assert us_row(live_app)["reason"] == "the Polymarket US market is not open (MARKET_STATUS_SUSPENDED)"


async def test_the_time_to_close_cutoff_applies_to_the_us_row_as_it_does_to_the_baseline(live_app, fake_http, clock):
    far = FakeMarket(COND, slug=SLUG, event=SLUG, outcomes=("Jaguars", "Titans"), sports_market_type="moneyline",
                     end_date=NOW + 40 * DAY, tags=["sports"])
    world, _, monitor, row_a = setup(live_app, fake_http, market=far)
    live_app.cfg.filters.max_days_to_close = 14.0
    await signal(world, monitor, row_a)
    t = us_row(live_app)
    assert (t["status"], t["skip_code"]) == ("SKIPPED", "too_long") and fake_http.count("/v1/markets") == 0


async def test_a_gateway_failure_costs_a_row_not_the_runner(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http)

    def down(_params):
        raise HttpError("503 from the gateway", status=503, answered=True)

    fake_http.route("/v1/markets", down)
    stats = await signal(world, monitor, row_a)
    assert stats.alerts == 1 and stats.errors == 0
    t = us_row(live_app)
    assert t["status"] == "MISSED" and t["reason"].startswith("error: HttpError")
    assert live_app.db.one("SELECT status FROM bot_trades WHERE runner = 'single' AND strategy = 'flat_hold'")[
        "status"] == "FILLED"


async def test_the_us_row_settles_at_what_the_us_venue_paid(live_app, fake_http, clock):
    world, venue, monitor, row_a = setup(live_app, fake_http, bids=((0.44, 500),), offers=((0.45, 500),))
    await signal(world, monitor, row_a, index=1, price=0.55)             # the Titans: the short side
    assert await row_a.settle() == 0 and us_row(live_app)["closed_at"] is None, "not settled there yet"
    venue.settled[US_SLUG] = 0                                           # the Jaguars lost: the long side pays 0
    clock.advance(4 * 3600)
    assert await row_a.settle() == 1
    t = us_row(live_app)
    assert (t["result"], t["payout_per_share"]) == ("WIN", 1.0)
    assert t["pnl"] == pytest.approx(t["contracts"] - t["cost"]) and t["closed_at"] == clock.now
    assert await row_a.settle() == 0


async def test_us_fees_are_never_recosted_under_the_international_fee_policy(live_app, fake_http, clock):
    from whalewatcher.costs import recalculate
    world, venue, monitor, row_a = setup(live_app, fake_http, offers=((0.45, 500),))
    await signal(world, monitor, row_a)
    venue.settled[US_SLUG] = 1
    await row_a.settle()
    live_app.cfg.fees.rate_source = "higher"
    recalculate(live_app)
    t = us_row(live_app)
    assert t["fees_recalc"] == pytest.approx(t["fees"]) and t["net_pnl_recalc"] == pytest.approx(t["pnl"])


async def test_row_a_only_ever_reads_the_public_gateway(live_app, fake_http, clock):
    world, venue, monitor, row_a = setup(live_app, fake_http, offers=((0.45, 500),))
    await signal(world, monitor, row_a)
    venue.settled[US_SLUG] = 1
    await row_a.settle()
    us_calls = [(method, path) for method, path, _ in fake_http.calls if path.startswith("/v1/")]
    assert {m for m, _ in us_calls} == {"GET"}
    assert {p for _, p in us_calls} == {"/v1/markets", f"/v1/markets/{US_SLUG}/book", f"/v1/markets/{US_SLUG}/settlement"}
    book_calls = [params for _, path, params in fake_http.calls if path.endswith("/book") and path.startswith("/v1/")]
    assert book_calls and all(p.get("_t") for p in book_calls), "a fresh book each time, not the 30-second copy"
    assert live_app.us.base == "https://gateway.polymarket.us"
    source = open(pus.__file__).read()
    assert "api.polymarket.us/" not in source.replace("(api.polymarket.us)", ""), "the trading host is never called"
    assert not any(word in source for word in ("X-PM-Access-Key", "secret_key", "/v1/orders", "ORDER_INTENT"))


async def test_the_match_rate_report_counts_alerts_not_just_markets(live_app, fake_http, clock):
    world, _, monitor, row_a = setup(live_app, fake_http)
    await signal(world, monitor, row_a)
    text = "\n".join(await pus.match_report(live_app, days=14))
    assert "alerts of the last 14 days: 1 alerts in 1 outcomes" in text
    assert "the same bet exists on Polymarket US: 1 alerts (100.0%)" in text and "same bet" in text


# -- esports maps -----------------------------------------------------------------------------------------


def map_market(question: str, slug: str = "astatc-cs2-vit-mouz-2026-11-13-map2", kind: str = "props") -> UsMarket:
    return UsMarket.from_api({"slug": slug, "marketType": kind, "status": "MARKET_STATUS_OPEN", "question": question,
                              "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}]})


def map_signal(slug="cs2-vit-mouz-2026-11-13-game2", outcomes=("Vitality", "MOUZ")) -> MarketInfo:
    return intl(slug, event=slug.rsplit("-", 1)[0], outcomes=outcomes, kind="child_moneyline")


def test_an_esports_map_is_one_yes_no_instrument_named_after_the_first_team():
    m = map_signal()
    c = candidates(m)
    assert c.rule == "map_winner" and c.slugs[:2] == ["astatc-cs2-vit-mouz-2026-11-13-map2",
                                                     "astatc-cs2-vit-mouz-2026-11-13-game2"], \
        "CS2 calls them maps, LoL games: both are tried"
    us = map_market("Will Vitality win Map 2 vs MOUZ?")
    assert side_for(us, m, 0, "map_winner") is True, "Vitality winning map 2 is the instrument itself"
    assert side_for(us, m, 1, "map_winner") is False, "MOUZ winning it is the short side"
    found = pick(c, {us.slug: us}, m, 1)
    assert found.found and found.long is False and found.market.slug == us.slug


def test_the_map_side_is_read_from_the_question_by_name_and_number_or_not_at_all():
    lol = map_signal("lol-blg-t1-2026-11-08-game2", ("Bilibili Gaming", "T1"))
    us = map_market("Will BILIBILI GAMING win Game 2 vs T1?", "astatc-lol-blg-t1-2026-11-08-game2")
    assert side_for(us, lol, 0, "map_winner") is True and side_for(us, lol, 1, "map_winner") is False
    other_map = map_market("Will Vitality win Map 3 vs MOUZ?")
    assert side_for(other_map, map_signal(), 0, "map_winner") is None, "map 3 is not map 2"
    stranger = map_signal(outcomes=("Natus Vincere", "FaZe"))
    assert side_for(map_market("Will Vitality win Map 2 vs MOUZ?"), stranger, 0, "map_winner") is None
    odd = map_market("Vitality to win map 2")
    assert side_for(odd, map_signal(), 0, "map_winner") is None, "a question in another form is not guessed at"
    wrong_kind = map_market("Will Vitality win Map 2 vs MOUZ?", kind="moneyline")
    assert not pick(candidates(map_signal()), {wrong_kind.slug: wrong_kind}, map_signal(), 0).found


def test_a_child_market_that_is_not_a_numbered_game_has_no_rule():
    first_half = intl("nfl-kc-den-2026-11-01-1h-moneyline", event="nfl-kc-den-2026-11-01", kind="first_half_moneyline")
    assert candidates(first_half).rule == "none"
    odd = intl("cs2-vit-mouz-2026-11-13-overtime", event="cs2-vit-mouz-2026-11-13", kind="child_moneyline")
    assert candidates(odd).rule == "none"


# -- the small wallet on the US venue ---------------------------------------------------------------------


def test_the_wallet_treats_a_bet_that_is_not_on_the_us_venue_as_a_skip_and_charges_us_fees(cfg):
    from whalewatcher import bankroll as br
    base = {"runner": "us", "strategy": "flat_hold", "id": 1, "token_id": "t", "condition_id": "c", "event_key": "e",
            "decided_at": NOW, "alert_ts": NOW, "stake_usd": 10.0, "whale_price": 0.42}
    absent = br.signal_of({**base, "status": "SKIPPED", "skip_code": "not_on_us",
                           "reason": "not_on_us: no rule for politics markets"}, cfg)
    assert absent.venue_skip == "not_on_us"
    (e,) = br.run([absent], br.Params(amount=100.0, onramp_cost_pct=0.0)).entries
    assert (e.status, e.code, e.reason) == ("SKIPPED", "not_on_us", "not_on_us: no rule for politics markets"), \
        "a bet the venue does not list was never there to miss"
    ran = br.signal_of({**base, "id": 2, "status": "MISSED", "reason": "price ran: filling $10 costs 0.500 a share",
                        "best_ask": 0.50, "asks_json": "[[0.43, 100]]", "limit_price": 0.45, "min_order_size": 0.01},
                       cfg)
    assert (ran.fee_rate, ran.fee_exponent, ran.venue_skip) == (0.095, 1.0, None), \
        "a row that did not fill stored no rate: the US coefficient applies, never the international policy"
    taken = br.run([ran], br.Params(amount=100.0, onramp_cost_pct=0.0)).entries[0]
    assert taken.status == "TAKEN" and taken.fee == pytest.approx(0.095 * taken.contracts * 0.43 * 0.57, abs=1e-4), \
        "re-walked at the wallet's own $8 stake on the stored US book, at the US fee"


async def test_the_runner_keeps_a_live_wallet_run_for_the_us_venue_too(live_app, fake_http, clock):
    from whalewatcher import bankroll as br
    from whalewatcher.service import Service
    live_app.cfg.polymarket_us.enabled = True
    svc = Service(live_app)
    assert svc.us is not None
    svc._sync_wallet()
    assert br.open_run(live_app.db, "single") is not None and br.open_run(live_app.db, "us") is not None
    live_app.cfg.polymarket_us.enabled = False
    assert Service(live_app).us is None, "switched off: no row A, and not a request to the US venue"



def test_without_kickoff_times_only_the_same_date_is_accepted():
    m = intl("nba-lal-bos-2026-11-06", game_start=None)
    c = candidates(m)
    assert c.exact == {"aec-nba-lal-bos-2026-11-06"}
    other_day = UsMarket.from_api(us_raw("aec-nba-lal-bos-2026-11-05", sides=(("Lakers", True), ("Celtics", False))))
    other_day.game_start = None
    assert not pick(c, {other_day.slug: other_day}, intl("nba-lal-bos-2026-11-06", game_start=None,
                                                         outcomes=("Lakers", "Celtics")), 0).found, \
        "the evening before could be the other game of a back-to-back"
    same_day = UsMarket.from_api(us_raw("aec-nba-lal-bos-2026-11-06", sides=(("Lakers", True), ("Celtics", False))))
    same_day.game_start = None
    assert pick(c, {same_day.slug: same_day}, intl("nba-lal-bos-2026-11-06", game_start=None,
                                                   outcomes=("Lakers", "Celtics")), 0).found
    timed = UsMarket.from_api(us_raw("aec-nba-lal-bos-2026-11-05", sides=(("Lakers", True), ("Celtics", False))))
    assert pick(candidates(intl("nba-lal-bos-2026-11-06")), {timed.slug: timed},
                intl("nba-lal-bos-2026-11-06", outcomes=("Lakers", "Celtics")), 0).found, \
        "with both kickoff times the neighbouring date is fine: the times prove it is the same game"
