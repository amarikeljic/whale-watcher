"""Protocol V2 (Dec 1 for most sports): a market's outcome ids are chosen by its
`version`, and a V2 market says it has resolved in `resolutionStatus`.
https://docs.polymarket.com/migrate/polymarket-v2/api-integrations.md"""

from __future__ import annotations

import json

from whalewatcher.polymarket import MarketInfo, outcome_ids

CLOB = ["111", "222"]
POS = ["666889148854578891742619935283574117032411625920935004001936672821144977408",
       "666889148854578891742619935283574117032411625920935004001936672821144977409"]


def gamma(version: str | None, **extra) -> dict:
    raw = {"conditionId": "0xC1", "question": "Will it?", "outcomes": json.dumps(["Yes", "No"]),
           "clobTokenIds": json.dumps(CLOB), "positionIds": POS, "closed": True,
           "outcomePrices": json.dumps(["1", "0"])}
    if version is not None:
        raw["version"] = version
    raw.update(extra)
    return raw


def test_a_v1_market_trades_under_its_clob_ids_even_when_position_ids_are_listed():
    """Live 2026-11-14: 37 of 100 open v1 markets already list positionIds."""
    for version in ("v1", None):
        m = MarketInfo.from_gamma(gamma(version))
        assert m.version == "v1" and m.token_ids == CLOB and m.alt_token_ids == POS


def test_a_v2_market_trades_under_its_position_ids():
    m = MarketInfo.from_gamma(gamma("v2"))
    assert m.version == "v2" and m.token_ids == POS and m.alt_token_ids == CLOB
    assert outcome_ids({"version": "V2", "positionIds": POS}) == ("v2", POS, [])


def test_a_trade_finds_its_outcome_with_either_kind_of_id_in_any_spelling():
    m = MarketInfo.from_gamma(gamma("v2"))
    assert m.outcome_index_of(POS[1]) == 1 and m.outcome_index_of("222") == 1
    assert m.outcome_index_of(hex(int(POS[0]))) == 0, "the same number in hex"
    assert m.outcome_index_of("999") is None and m.outcome_index_of("") is None
    assert m.outcome_index_of("0xnot-hex") is None


def test_the_fallback_payout_reads_the_status_field_of_the_market_version():
    v2 = MarketInfo.from_gamma(gamma("v2"))
    assert v2.gamma_final_payouts(gamma("v2", resolutionStatus="resolved")) == [1.0, 0.0]
    assert v2.gamma_final_payouts(gamma("v2", resolutionStatus="active")) is None
    assert v2.gamma_final_payouts(gamma("v2", umaResolutionStatus="resolved")) is None, "v2 has its own field"
    v1 = MarketInfo.from_gamma(gamma("v1"))
    assert v1.gamma_final_payouts(gamma("v1", umaResolutionStatus="resolved")) == [1.0, 0.0]
    assert v1.gamma_final_payouts(gamma("v1", resolutionStatus="resolved")) is None


# -- end to end: a new-format market, and what the Health page says --------------------------


async def test_a_signal_on_a_v2_market_fills_and_settles(live_app, fake_http, clock):
    from conftest import DAY, NOW
    from test_bot import WHALE, buy, one
    from world import FakeMarket, World

    from whalewatcher.bot import Bot
    from whalewatcher.monitor import Monitor
    from whalewatcher.paper import PaperEngine
    from whalewatcher.stats import settle_check

    world = World(fake_http)
    m = world.add_market(FakeMarket("0xv2", end_date=NOW + 30 * DAY, question="Will A beat B?", version="v2"))
    world.set_book(m.token(0), [(0.44, 1000)], [(0.42, 500)])
    live_app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                        "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    bot = Bot(live_app)
    await buy(world, Monitor(live_app, paper=PaperEngine(live_app), bot=bot), clock, "0xv2")
    t = one(live_app, "flat_hold")
    assert (t["status"], t["token_id"]) == ("FILLED", "0xv2-p0")

    m.resolved_at, m.payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    assert await bot.settle() >= 1
    t = one(live_app, "flat_hold")
    assert t["closed_at"] is not None and t["payout_per_share"] == 1.0, "a position id finds its outcome"
    chk = settle_check(live_app)
    assert (chk.v2_markets, chk.v2_signals, chk.v2_filled, chk.stuck) == (1, 1, 1, [])


def test_a_fill_that_cannot_settle_is_shown_not_left_silent(tmp_path):
    from conftest import NOW
    from dashfix import A, Fixture

    from whalewatcher import dashboard as dash
    from whalewatcher.config import Config
    from whalewatcher.db import Database
    from whalewatcher.stats import settle_check, settle_check_lines

    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    fx = Fixture(db, NOW)
    fx.trade(A, 2)                                                      # open
    fx.trade(A, 3, status="MISSED", reason="market metadata unavailable")
    (t,) = db.query("SELECT condition_id FROM bot_trades WHERE status = 'FILLED'")
    db.execute("UPDATE poly_markets SET resolved = 1, payouts = '[1.0, 0.0]', token_ids = '[\"someone-else\"]', "
               "version = 'v2' WHERE condition_id = ?", (t["condition_id"],))
    app = type("A", (), {"db": db, "clock": staticmethod(lambda: NOW), "cfg": Config(database_path=path)})()
    chk = settle_check(app)
    assert len(chk.stuck) == 1 and (chk.lost, chk.signals) == (1, 2) and not chk.ok
    text = "\n".join(settle_check_lines(app, brief=True))
    assert "open fills that cannot settle: 1" in text and "1 open fills cannot settle" in text
    db.close()
    html = dash.render(Config(database_path=path), path, "/health", {}, now=NOW)[1]
    assert "Can every trade settle?" in html and "1 open fills cannot settle" in html
    assert "1 signals in the last 7 days were missed because Gamma had no data" in html


def test_a_quiet_check_stays_out_of_the_daily_summary(tmp_path):
    from conftest import NOW
    from dashfix import A, Fixture

    from whalewatcher.config import Config
    from whalewatcher.db import Database
    from whalewatcher.stats import settle_check_lines

    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    Fixture(db, NOW).trade(A, 2, pnl=1.0)
    app = type("A", (), {"db": db, "clock": staticmethod(lambda: NOW), "cfg": Config(database_path=path)})()
    assert settle_check_lines(app, brief=True) == []
    assert settle_check_lines(app)[0].startswith("CAN EVERY TRADE SETTLE?")
    db.close()
