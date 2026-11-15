"""`python -m whalewatcher v2check`: real new-format (Protocol V2) markets taken through
the service's own code. Here the network is the fake Polymarket."""

from __future__ import annotations

from conftest import DAY, NOW
from world import FakeMarket, World

from whalewatcher import v2check
from whalewatcher.cli import build_parser
from whalewatcher.v2check import FAIL, PASS, SKIP

WHALE = "0xwhale"
# Real ids read from Polymarket on 2026-11-14 and 15 (see test_protocol.py).
LIVE_IDS = ("666889148854578891742619935283574117032411625920935004001936672821144977408",
            "666889148854578891742619935283574117032411625920935004001936672821144977409")
FEED_NAME = "0x0179722553dc59c1676071459c0b009e450000000000000000000000000000"     # the key inside LIVE_IDS
COMBO_COND = "0x03e684ae8f1e7eb7588ff5d7f6f1e31d2a0000000000000000000000000000"
COMBO_ID = "1764229107269526617413719898682628492622161691141937249771935148617448292352"


def status(report, name: str) -> str:
    (check,) = [c for c in report.checks if c.name == name]
    return check.status


def v2_world(fake_http) -> World:
    world = World(fake_http)
    live = world.add_market(FakeMarket("0xopen", end_date=NOW + DAY, version="v2"))
    world.add_market(FakeMarket("0xdone", end_date=NOW - DAY, version="v2", resolved_at=NOW - 3600,
                                payouts=[1.0, 0.0]))
    world.add_market(FakeMarket("0xold", end_date=NOW + DAY))                 # a market in the old format
    world.set_book(live.token(0), [(0.44, 1000)], [(0.42, 500)])
    world.trade(WHALE, NOW - 5, "0xopen", size=100, price=0.43, feed=True)
    world.trade(WHALE, NOW - 9000, "0xdone", index=1, size=50, price=0.2, feed=True)
    world.trade("0xother", NOW - 7, "0xold", size=10, price=0.5, feed=True)
    return world


async def test_a_consistent_v2_world_passes_every_check(live_app, fake_http):
    v2_world(fake_http)
    report = await v2check.run(live_app)
    assert [c.status for c in report.checks] == [PASS] * 7, report.lines()
    assert report.exit_code == 0 and report.v2_markets == 2
    assert report.verdict == "ALL CHECKS PASSED on 2 new-format (V2) markets"
    text = "\n".join(report.lines())
    assert "2 trades on 2 V2 markets, all matched" in text
    assert "1 of 1 resolved V2 markets got a payout" in text
    assert "found the same fill with the same market and token ids" in text
    assert live_app.db.one("SELECT COUNT(*) n FROM alerts")["n"] == 0, "it reads; no signal is made"


async def test_before_any_v2_market_exists_it_says_so(live_app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xold", end_date=NOW + DAY))
    world.trade(WHALE, NOW - 5, "0xold", feed=True)
    report = await v2check.run(live_app)
    assert report.exit_code == 2 and report.verdict.startswith("NOTHING TO CHECK YET")
    assert status(report, "feed markets are found on Gamma") == PASS


async def test_a_token_its_market_does_not_list_fails(live_app, fake_http):
    world = v2_world(fake_http)
    world.global_trades.append({"proxy_wallet": WHALE, "timestamp": NOW - 3, "condition_id": "0xopen", "type": "TRADE",
                                "size": 10, "usdc_size": 5, "price": 0.5, "transaction_hash": "0xabc",
                                "token_id": "424242", "side": "BUY", "outcome_index": 0, "outcome": "Yes"})
    report = await v2check.run(live_app)
    assert status(report, "a V2 trade's token finds its outcome") == FAIL and report.exit_code == 1
    assert "token 424242… is not listed by its market" in "\n".join(report.lines())
    assert "Fix before 2026-12-01" in report.verdict


async def test_a_feed_that_names_v2_markets_differently_fails_and_says_what_gamma_calls_it(live_app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xgamma-name", end_date=NOW + DAY, version="v2", position_ids=LIVE_IDS))
    world.global_trades.append({"proxy_wallet": WHALE, "timestamp": NOW - 3, "condition_id": FEED_NAME, "type": "TRADE",
                                "size": 10, "usdc_size": 5, "price": 0.5, "transaction_hash": "0xabc",
                                "token_id": LIVE_IDS[0], "side": "BUY", "outcome_index": 0, "outcome": "Yes"})
    report = await v2check.run(live_app)
    check = report.checks[0]
    assert (check.name, check.status) == ("feed markets are found on Gamma", FAIL)
    assert "the feed names V2 markets differently from Gamma" in check.detail
    assert f"the feed says {FEED_NAME}, Gamma says 0xgamma-name, version v2" in check.detail
    assert report.exit_code == 1


async def test_combos_are_noted_and_do_not_fail_the_check(live_app, fake_http):
    world = World(fake_http)
    world.add_market(FakeMarket("0xold", end_date=NOW + DAY))
    world.trade(WHALE, NOW - 5, "0xold", feed=True)
    world.global_trades.append({"proxy_wallet": WHALE, "timestamp": NOW - 3, "condition_id": COMBO_COND, "type": "TRADE",
                                "size": 23.9, "usdc_size": None, "price": 0.41, "transaction_hash": "0xdef",
                                "token_id": COMBO_ID, "side": "BUY", "outcome_index": 999, "outcome": "",
                                "title": "Bulls vs. Timberwolves AND New Zealand leading at halftime?"})
    report = await v2check.run(live_app)
    assert status(report, "feed markets are found on Gamma") == PASS
    assert "1 of 1 markets" in report.checks[0].detail, "the combo is not counted as a market that should be found"
    assert any("1 combos" in n and "skipped by design" in n for n in report.notes)
    assert report.exit_code == 2, "a combo is not a V2 market the service could trade"


async def test_checks_that_cannot_run_yet_are_skipped_not_failed(live_app, fake_http):
    world = World(fake_http)
    live = world.add_market(FakeMarket("0xopen", end_date=NOW + DAY, version="v2"))
    world.set_book(live.token(0), [(0.44, 1000)], [(0.42, 500)])
    world.trade(WHALE, NOW - 5, "0xopen", feed=True)
    report = await v2check.run(live_app)
    assert status(report, "V2 markets settle") == SKIP and report.exit_code == 0
    assert "1 could not run yet: V2 markets settle" in report.verdict


def test_the_command_is_documented_in_the_parser():
    args = build_parser().parse_args(["v2check", "--pages", "1", "--sample", "3"])
    assert (args.command, args.pages, args.sample) == ("v2check", 1, 3)
