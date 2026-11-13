"""v2.1 Part 1: the fee formula against the published examples, where the rate comes
from, and a P&L split into gross, fees, slippage and net without touching the originals."""

from __future__ import annotations

import pytest

from conftest import DAY, NOW, market
from whalewatcher import costs, fees
from whalewatcher.bot import Bot
from whalewatcher.config import Config, ConfigError, FeesConfig, validate
from whalewatcher.monitor import Monitor
from whalewatcher.paper import PaperEngine
from whalewatcher.stats import render
from world import FakeMarket, World

WHALE, COND, YES = "0xwhale", "0xm", "0xm-t0"

# https://docs.polymarket.com/trading/fees.md, "Fee Tables (100 Shares)", read 2026-11-10.
DOCS = [("crypto", 0.07, 0.50, 1.75), ("crypto", 0.07, 0.30, 1.47), ("crypto", 0.07, 0.10, 0.63),
        ("crypto", 0.07, 0.95, 0.33), ("crypto", 0.07, 0.01, 0.07), ("sports", 0.05, 0.50, 1.25),
        ("politics", 0.04, 0.50, 1.00), ("politics", 0.04, 0.25, 0.75), ("culture", 0.05, 0.80, 0.80)]


@pytest.mark.parametrize("category,rate,price,dollars", DOCS)
def test_the_fee_matches_the_published_tables(category, rate, price, dollars):
    assert fees.polymarket_taker_fee(100, price, rate) == pytest.approx(dollars, abs=0.005)
    assert fees.table_rate(category, NOW, FeesConfig()) == rate


def test_one_hundred_sports_shares_at_fifty_cents_cost_a_dollar_twenty_five():
    assert fees.polymarket_taker_fee(100, 0.50, 0.05) == 1.25
    assert fees.polymarket_taker_fee(100, 0.30, 0.05) == fees.polymarket_taker_fee(100, 0.70, 0.05), "symmetric"
    assert fees.taker_fee([(0.50, 60), (0.52, 40)], 0.05) == pytest.approx(0.75 + 0.4992)      # charged per level


def test_geopolitics_is_free_in_the_table_and_an_unknown_category_pays_the_other_rate():
    cfg = FeesConfig()
    assert fees.table_rate("geopolitics", NOW, cfg) == 0.0
    assert fees.table_rate("something-new", NOW, cfg) == 0.05 and fees.table_rate(None, NOW, cfg) == 0.05


def test_the_rate_can_come_from_the_market_the_table_or_the_higher_of_the_two():
    ufc = market(category="sports", fee_rate=0.03)              # a sports market that charges 0.03, as some do
    free = market(category="geopolitics", fee_rate=0.0)
    own = FeesConfig(rate_source="market")
    assert FeesConfig().rate_source == "higher", "the shipped default is the pessimistic one"
    assert fees.rate_for(ufc, NOW, own) == (0.03, "market"), "what the venue charges"
    assert fees.rate_for(ufc, NOW, FeesConfig(rate_source="table")) == (0.05, "table")
    assert fees.rate_for(ufc, NOW, FeesConfig()) == (0.05, "table"), "higher: the table's 0.05 beats the market's 0.03"
    dear = market(category="geopolitics", fee_rate=0.04)        # the table says geopolitics is free; this market is not
    assert fees.rate_for(dear, NOW, FeesConfig()) == (0.04, "market"), "higher: the market's 0.04 beats the table's 0"
    assert fees.rate_for(free, NOW, own) == (0.0, "market") and fees.rate_for(free, NOW, FeesConfig()) == (0.0, "market")
    waived = market(category="sports", fee_rate=0.0)             # the venue marks it fee-free
    assert fees.rate_for(waived, NOW, own) == (0.0, "market")
    assert fees.rate_for(waived, NOW, FeesConfig()) == (0.05, "table"), "higher still charges the category rate"
    silent = market(category="crypto", fee_rate=0.0)
    silent.fees_enabled = True                                   # charges a fee, publishes no rate
    assert fees.rate_for(silent, NOW, own) == (0.07, "table"), "the table is the fallback"


def test_a_fee_change_applies_from_its_date_and_not_before():
    cfg = FeesConfig(schedules=[{"from": "2026-03-31", "rates": {"sports": 0.05, "other": 0.05}},
                                {"from": "2026-12-01", "rates": {"sports": 0.08, "other": 0.05}}])
    validate(Config(fees=cfg))
    from whalewatcher.timeutil import parse_as_of
    assert fees.table_rate("sports", parse_as_of("2026-11-30"), cfg) == 0.05
    assert fees.table_rate("sports", parse_as_of("2026-12-01"), cfg) == 0.08
    assert fees.table_rate("sports", parse_as_of("2026-01-01"), cfg) == 0.05, "older than every table: the oldest"


def test_bad_fee_settings_are_refused():
    with pytest.raises(ConfigError, match="rate_source"):
        validate(Config(fees=FeesConfig(rate_source="cheapest")))
    with pytest.raises(ConfigError, match="between 0 and 0.5"):
        validate(Config(fees=FeesConfig(schedules=[{"from": "2026-01-01", "rates": {"sports": 5}}])))


# -- on real paper trades ------------------------------------------------------------------------


def setup(app, fake_http, *, asks, bids=((0.48, 5000),), fee_rate=0.05):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 30 * DAY, fee_rate=fee_rate, tags=["sports"]))
    world.set_book(YES, list(asks), list(bids))
    app.db.execute("INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at, last_seen_ts) "
                   "VALUES (?, 1, 1, 1, ?, ?)", (WHALE, NOW - 3600, NOW - 120))
    bot = Bot(app)
    return world, Monitor(app, paper=PaperEngine(app), bot=bot), bot


def one(app, strategy="flat_hold"):
    (t,) = app.db.query("SELECT * FROM bot_trades WHERE strategy = ?", (strategy,))
    return t


async def test_a_fill_records_its_rate_its_source_and_what_it_paid_beyond_the_best_ask(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 10), (0.52, 1000)])
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    t = one(live_app)
    second = 5 / 0.52                                           # $5 at 0.50, then $5 at 0.52
    assert (t["fee_rate"], t["fee_source"]) == (0.05, "market")
    assert t["fees"] == pytest.approx(10 * 0.05 * 0.25 + round(second * 0.05 * 0.52 * 0.48, 5))
    assert t["slippage_cost"] == pytest.approx((t["fill_price"] - 0.50) * t["contracts"]), "paid beyond the best ask"
    assert t["slippage_cost"] == pytest.approx(second * 0.02)


async def test_a_closed_trade_splits_into_gross_fees_slippage_and_net(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 10), (0.52, 1000)])
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    before = dict(one(live_app))
    assert costs.recalculate(live_app)["bot_trades"] >= 1

    t = one(live_app)
    assert (t["fees"], t["cost"], t["pnl"]) == (before["fees"], before["cost"], before["pnl"]), "originals untouched"
    shares = 10 + 5 / 0.52
    assert t["pnl"] == pytest.approx(shares - 10 - t["fees"]), "net: payout minus stake minus fee"
    assert t["gross_pnl"] == pytest.approx(shares - shares * 0.50), "gross: the same shares bought at the best ask, no fee"
    assert t["gross_pnl"] == pytest.approx(t["pnl"] + t["fees"] + t["slippage_cost"])
    assert t["fees_recalc"] == pytest.approx(t["fees"]), "same rate: the recalculation is the fee charged, exactly"
    assert t["net_pnl_recalc"] == pytest.approx(t["pnl"])

    c = costs.breakdown(live_app.db.query("SELECT * FROM bot_trades WHERE strategy = 'flat_hold'"))
    assert c.n == 1 and c.net == pytest.approx(c.gross - c.fees - c.slippage)
    text = render(live_app, with_breakdowns=False)
    # 19.615 shares: gross 19.615 x (1 - 0.50) = 9.81; fees 0.245; slippage 9.615 x 0.02 = 0.19; net 9.37
    assert "costs (n=1 closed): gross +$9.81 | fees -$0.24 | slippage -$0.19 | net +$9.37" in text
    assert "are all net" in text
    assert costs.recalculate(live_app) == {"bot_trades": 0, "paper_trades": 0}, "nothing left to do the second time"


async def test_a_mirror_exit_pays_the_fee_on_the_sell_as_well(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 5000)], bids=[(0.60, 5), (0.58, 5000)])
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    clock.advance(600)
    world.trade(WHALE, clock.now - 2, COND, side="SELL", size=1300, price=0.60, feed=True)
    await monitor.poll_once()
    t = one(live_app, "flat_mirror")
    assert t["result"] == "EXIT" and t["exit_best_bid"] == 0.60
    buy_fee = 20 * 0.05 * 0.25
    sell_fee = round(5 * 0.05 * 0.6 * 0.4, 5) + round(15 * 0.05 * 0.58 * 0.42, 5)
    assert t["fees"] == pytest.approx(buy_fee) and t["exit_fees"] == pytest.approx(sell_fee)
    assert t["slippage_cost"] == pytest.approx(15 * 0.02), "15 of the 20 shares sold two cents under the best bid"
    assert t["pnl"] == pytest.approx(5 * 0.60 + 15 * 0.58 - sell_fee - 10 - buy_fee)
    gross, paid, slip, net = costs.trade_costs(t)
    assert paid == pytest.approx(buy_fee + sell_fee) and gross == pytest.approx(net + paid + slip)
    assert gross == pytest.approx(20 * 0.60 - 10), "gross: bought at the best ask, sold at the best bid"


async def test_old_rule_trades_are_recalculated_into_new_columns_only(live_app, fake_http, clock):
    world = World(fake_http)
    world.add_market(FakeMarket(COND, end_date=NOW + 30 * DAY, fee_rate=0.05, tags=["sports"]))
    await live_app.markets.ensure([COND])
    db = live_app.db
    alert = db.execute(
        "INSERT INTO alerts (wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, whale_shares, whale_usd, "
        "whale_price, fills, detected_at, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (WHALE, COND, YES, "NEW", NOW, NOW, 100, 600, 0.5, 1, NOW, "POSTED", NOW)).lastrowid
    db.execute(
        "INSERT INTO paper_trades (id, alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
        "alert_ts, fill_ts, fill_price, contracts, fees, cost, settled_at, pnl, updated_at) VALUES "
        "(7, ?, 'polymarket', 'hold_to_resolution', 'FILLED', ?, ?, 10, 1, ?, ?, 0.5, 20, 0.25, 10.25, ?, 9.75, ?)",
        (alert, YES, COND, NOW, NOW + 60, NOW + 600, NOW))
    assert costs.recalculate(live_app)["paper_trades"] == 1
    t = db.one("SELECT * FROM paper_trades WHERE id = 7")
    assert (t["fees"], t["cost"], t["pnl"]) == (0.25, 10.25, 9.75), "the original values are never rewritten"
    assert (t["fee_rate"], t["fee_source"], t["fees_recalc"]) == (0.05, "market", 0.25)
    assert t["gross_pnl"] == pytest.approx(10.0) and t["net_pnl_recalc"] == pytest.approx(9.75)
    assert "costs (n=1 closed): gross +$10.00 | fees -$0.25 | slippage -$0.00 | net +$9.75" in render(live_app, with_breakdowns=False)

    rep = costs.audit_rows(live_app)
    text = costs.render_audit(rep)
    assert rep["kind"] == "old rule" and "by hand: 20.00 x 0.05 x 0.500 x 0.500 = 0.2500" in text
    assert "gross +10.00 | fees -0.25 | slippage -0.00 | net +9.75" in text
    assert "differs from the formula by more than a cent on 0 of 1 trades" in text


async def test_switching_to_the_pessimistic_rate_recosts_old_trades_beside_the_originals(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 5000)], fee_rate=0.03)      # the market charges 0.03
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    costs.recalculate(live_app)
    t = one(live_app)
    assert t["fees"] == pytest.approx(0.15) and t["fees_recalc"] == pytest.approx(0.15) and t["pnl"] == pytest.approx(9.85)

    live_app.cfg.fees.rate_source = "higher"                    # the table's 0.05 for sports now applies
    fees.configure(live_app.cfg.fees)
    assert costs.recalculate(live_app)["bot_trades"] >= 1, "a policy change re-costs every trade"
    t = one(live_app)
    assert (t["fees"], t["pnl"], t["fee_rate"]) == (pytest.approx(0.15), pytest.approx(9.85), 0.03), "what it was charged stays"
    assert t["fees_recalc"] == pytest.approx(0.25) and t["net_pnl_recalc"] == pytest.approx(9.75)
    from whalewatcher.stats import load_bot_trades
    (shown,) = [x for x in load_bot_trades(live_app) if x["strategy"] == "flat_hold"]
    assert shown["pnl"] == pytest.approx(9.75) and shown["cost"] == pytest.approx(10.25), "reports use the re-costed trade"
    assert shown["pnl_charged"] == pytest.approx(9.85)
    assert bot.account("flat_hold").realized == pytest.approx(9.75)
    assert "net +$9.75" in render(live_app, with_breakdowns=False)
    assert "Recalculated under today's policy (fees.rate_source = higher): fees -0.25 | net +9.75" in \
        costs.render_audit(costs.audit_rows(live_app))


async def test_the_audit_flags_a_stored_fee_that_does_not_match_and_a_rate_off_the_table(live_app, fake_http, clock):
    world, monitor, bot = setup(live_app, fake_http, asks=[(0.50, 5000)], fee_rate=0.03)      # table says sports = 0.05
    world.trade(WHALE, NOW - 3, COND, size=1300, price=0.50, usdc=650, feed=True)
    await monitor.poll_once()
    world.markets[COND].resolved_at, world.markets[COND].payouts = clock.now + 60, [1.0, 0.0]
    clock.advance(700)
    await bot.settle()
    costs.recalculate(live_app)
    live_app.db.execute("UPDATE bot_trades SET fees = 0.90 WHERE strategy = 'flat_hold'")       # a wrong stored fee
    text = costs.render_audit(costs.audit_rows(live_app))
    assert "CHARGED 0.9000 BUT THE FORMULA GIVES 0.1500" in text
    assert "table says 0.05, market charges 0.03" in text
    assert "more than a cent on 1 of 1 trades" in text and "differs from the config table on 1 of 1 trades" in text


def test_superseded_migration_backups_are_pruned_but_the_first_and_any_hand_made_one_stay(tmp_path):
    from whalewatcher.db import Database
    db = Database(str(tmp_path / "ww.sqlite3"))
    folder = tmp_path / "backups"
    folder.mkdir()
    names = [f"ww.sqlite3.2026111{i}T000000Z.pre-v{i + 2}" for i in range(5)]
    for name in names + ["ww.sqlite3.20261110T170049Z", "notes.txt"]:
        (folder / name).write_text("x")
    removed = db.prune_migration_backups()
    assert removed == names[1:3], "the middle ones go"
    left = sorted(p.name for p in folder.iterdir())
    assert left == sorted([names[0], names[3], names[4], "ww.sqlite3.20261110T170049Z", "notes.txt"])
    assert db.prune_migration_backups() == []
    db.close()
