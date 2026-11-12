"""v2 Phase 3b: the consensus pool, entity merging, the signal engine, and a backtest
that never uses data from after the day it is deciding about."""

from __future__ import annotations

import pytest

from conftest import DAY, NOW, act
from whalewatcher import consensus as cs
from whalewatcher.config import Config, ConfigError, ConsensusConfig, ConsensusPoolConfig, validate

RULES = ConsensusConfig()                 # 3 entities, $1,000 each, 48 h, no opposition
POOL = ConsensusPoolConfig()
T0 = NOW - 100 * DAY
YES, NO = "0xm-t0", "0xm-t1"


def buy(wallet, ts, *, usd=1500.0, price=0.5, cond="0xm", index=0, tx=None):
    return act(ts, wallet=wallet, cond=cond, index=index, size=usd / price, price=price, tx=tx)


def sell(wallet, ts, *, shares, price=0.5, cond="0xm", index=0):
    return act(ts, wallet=wallet, cond=cond, index=index, side="SELL", size=shares, price=price)


def everyone(w):                           # every wallet is in the pool, each its own entity
    return w


def feed(engine, fills, entity_of=everyone):
    return [s for f in fills if (s := engine.on_fill(f, entity_of)) is not None]


# -- a wallet's history, asked "as of day d" --------------------------------------------------


def test_profit_counts_only_markets_that_had_resolved_in_the_30_days_before_the_day():
    h = cs.WalletHistory.build("0xa", 0, [], [(T0 - 40 * DAY, 9000.0), (T0 - 10 * DAY, 500.0), (T0 + 5 * DAY, 70_000.0)])
    assert h.profit(T0) == 500.0, "the 40-day-old win is out of the window; the later one is not known yet"
    assert h.profit(T0 + 6 * DAY) == 70_500.0
    assert h.profit(T0 + 25 * DAY) == 70_000.0


def test_trades_a_day_counts_transactions_not_fills():
    rows = [buy("0xa", T0 - DAY + i, tx=f"0xt{i // 3}") for i in range(30)]       # 30 fills, 10 transactions
    h = cs.WalletHistory.build("0xa", 0, rows, [])
    assert h.trades_per_day(T0) == pytest.approx(10 / 30)
    assert h.trades_per_day(T0 - 2 * DAY) == 0


def test_two_sidedness_is_judged_on_what_had_been_bought_by_that_day():
    rows = [buy("0xa", T0 - 5 * DAY, cond="0xm"), buy("0xa", T0 - 5 * DAY, cond="0xn"),
            buy("0xa", T0 + 2 * DAY, cond="0xm", index=1)]                         # the other side, later
    h = cs.WalletHistory.build("0xa", 0, rows, [])
    assert h.two_sided_ratio(T0) == 0.0, "the second side had not been bought yet"
    assert h.two_sided_ratio(T0 + 3 * DAY) == 0.5
    assert h.two_sided_ratio(T0 - 20 * DAY) is None


# -- the pool ------------------------------------------------------------------------------------


def row(wallet, profit, tpd=10.0, two=0.1):
    return cs.PoolRow(wallet, profit, tpd, two, cs.activity_reasons(tpd, two, POOL))


def test_the_pool_drops_idle_wallets_machines_and_market_makers_then_applies_the_profit_bar():
    rows = [row("0xbig", 900_000), row("0xmid", 300_000), row("0xsmall", 120_000), row("0xidle", 800_000, tpd=1),
            row("0xbot", 800_000, tpd=900), row("0xmm", 800_000, two=0.8), row("0xloser", -5)]
    assert [r.wallet for r in cs.select(rows, 100_000, 0)] == ["0xbig", "0xmid", "0xsmall"]
    assert [r.wallet for r in cs.select(rows, 250_000, 0)] == ["0xbig", "0xmid"]
    assert [r.wallet for r in cs.select(rows, 0, 2)] == ["0xbig", "0xmid"], "top N by profit, after the rules"
    reasons = {r.wallet: r.reasons for r in rows if r.reasons}
    assert "trades a day < 5" in reasons["0xidle"][0] and "market maker or bot" in reasons["0xbot"][0]
    assert "buys both sides in 80%" in reasons["0xmm"][0]


# -- entities --------------------------------------------------------------------------------------


def test_wallets_holding_the_same_markets_or_buying_together_are_one_entity():
    markets = {"0xa": {f"m{i}" for i in range(10)}, "0xb": {f"m{i}" for i in range(2, 11)},      # 8 of 11 shared
               "0xc": {f"x{i}" for i in range(10)}, "0xd": {f"y{i}" for i in range(10)},
               "0xe": {f"m{i}" for i in range(3)}}                                          # too few to judge
    together = {(f"t{i}", 1000 + i) for i in range(12)}
    cotrades = {"0xc": together | {("z", 1)}, "0xd": together | {("q", 2), ("q", 3)}}
    got = cs.entities(sorted(markets), markets, cotrades, POOL)
    assert got["0xa"] == got["0xb"] == "0xa", "same markets"
    assert got["0xc"] == got["0xd"] == "0xc", "different markets, but 12 buys of the same outcome within 10 s"
    assert got["0xe"] == "0xe" and len(set(got.values())) == 3


# -- the signal ---------------------------------------------------------------------------------------


def test_the_third_agreeing_entity_fires_the_signal_with_who_agreed_and_when():
    engine = cs.Engine(RULES)
    fills = [buy("0xa", T0, price=0.40), buy("0xb", T0 + 3600, price=0.45), buy("0xc", T0 + 7200, price=0.50)]
    (s,) = feed(engine, fills)
    assert (s.token_id, s.ts, s.wallet, s.price) == (YES, T0 + 7200, "0xc", 0.50)
    assert [(e.entity, e.ts) for e in s.entries] == [("0xa", T0), ("0xb", T0 + 3600), ("0xc", T0 + 7200)]
    assert [e.price for e in s.entries] == pytest.approx([0.40, 0.45, 0.50])
    assert s.first.price == pytest.approx(0.40) and s.opposing == 0
    assert feed(engine, [buy("0xd", T0 + 9000)]) == [], "one signal per outcome"


def test_small_fills_add_up_to_a_position_and_too_small_a_position_does_not_count():
    engine = cs.Engine(RULES)
    assert feed(engine, [buy("0xa", T0), buy("0xb", T0 + 1), buy("0xc", T0 + 2, usd=600)]) == []
    (s,) = feed(engine, [buy("0xc", T0 + 3, usd=600)])
    assert s.entries[-1].usd == pytest.approx(1200) and s.entries[-1].ts == T0 + 3, "entered when it reached $1,000"


def test_twins_count_once():
    engine = cs.Engine(RULES)
    twin = {"0xa": "0xa", "0xa2": "0xa", "0xb": "0xb", "0xc": "0xc"}.get
    assert feed(engine, [buy("0xa", T0), buy("0xa2", T0 + 1), buy("0xb", T0 + 2)], twin) == [], "two entities"
    (s,) = feed(engine, [buy("0xc", T0 + 3)], twin)
    assert [e.entity for e in s.entries] == ["0xa", "0xb", "0xc"] and s.entries[0].usd == pytest.approx(3000)


def test_an_opposing_pool_entity_blocks_the_signal():
    engine = cs.Engine(RULES)
    fills = [buy("0xz", T0 - 99 * 3600, index=1), buy("0xa", T0), buy("0xb", T0 + 1), buy("0xc", T0 + 2)]
    assert feed(engine, fills) == [] and engine.blocked_by_opposition == 1
    engine.rules = ConsensusConfig(max_opposing_entities=1)
    (s,) = feed(engine, [buy("0xd", T0 + 3)])
    assert s.opposing == 1 and len(s.entries) == 4


def test_an_old_entry_is_not_fresh_agreement_and_a_sold_position_stops_agreeing():
    engine = cs.Engine(RULES)
    stale = [buy("0xa", T0 - 49 * 3600), buy("0xb", T0), buy("0xc", T0 + 1)]
    assert feed(engine, stale) == [], "0xa entered more than 48 h before the others"
    assert feed(engine, [sell("0xb", T0 + 2, shares=2800), buy("0xd", T0 + 3)]) == [], "0xb sold down to $100"
    (s,) = feed(engine, [buy("0xe", T0 + 4)])
    assert [e.entity for e in s.entries] == ["0xc", "0xd", "0xe"]


def test_only_pool_members_count_and_a_new_member_keeps_its_real_entry_time():
    engine = cs.Engine(RULES)
    pool = {"0xa", "0xb"}
    member = lambda w: w if w in pool else None                                       # noqa: E731
    assert feed(engine, [buy("0xa", T0), buy("0xb", T0 + 1), buy("0xout", T0 + 2)], member) == []
    pool.add("0xout")                                           # it joins the pool three days later...
    assert feed(engine, [buy("0xout", T0 + 3 * DAY, usd=50)], member) == [], \
        "...already holding: its entry is three days old, the others' too, so nothing is fresh"
    assert len(engine.new_entries) == 2, "only entries made while in the pool feed the single-wallet baseline"


# -- the replay: nothing from after the day being decided ---------------------------------------------


def history(wallet, discovered, *, wins_at, busy_from, cond_prefix="p"):
    """A wallet that trades 6 times a day from ``busy_from`` and banks $200k when each market in ``wins_at`` resolves."""
    rows = [buy(wallet, busy_from + i * 4 * 3600, usd=20, cond=f"{cond_prefix}{wallet}{i}", tx=f"{wallet}{i}")
            for i in range(6 * 80)]
    return cs.WalletHistory.build(wallet, discovered, rows, [(ts, 200_000.0) for ts in wins_at])


def test_the_replay_builds_each_days_pool_from_that_days_past_only():
    start, end = T0, T0 + 20 * DAY
    hs = {w: history(w, T0 - 60 * DAY, wins_at=[T0 - 5 * DAY], busy_from=T0 - 40 * DAY) for w in ("0xa", "0xb")}
    hs["0xlate"] = history("0xlate", T0 - 60 * DAY, wins_at=[T0 + 10 * DAY], busy_from=T0 - 40 * DAY)   # profit comes later
    hs["0xnew"] = history("0xnew", T0 + 12 * DAY, wins_at=[T0 - 5 * DAY], busy_from=T0 - 40 * DAY)      # found later
    for w, h in hs.items():
        h.trades += [buy(w, T0 + DAY + 60, cond="0xm")]             # day 1: all four buy YES
        h.trades.sort(key=lambda r: r.ts)
    res, entries = cs.replay(hs, start, end, RULES, POOL, 100_000, 0, "100k")
    assert res.pool_sizes[0] == 2 and res.pool_sizes[-1] == 4, "0xlate joins once its win resolves, 0xnew once found"
    assert res.signals == [], "on day 1 only two of the four were in the pool: no consensus, whatever happened later"
    assert {e.entity for e, _ in entries} == {"0xa", "0xb"}

    for w in ("0xa", "0xb", "0xlate", "0xnew"):                     # day 15: all four are in the pool by now
        hs[w].trades.append(buy(w, T0 + 15 * DAY + 60, cond="0xq"))
    res, _ = cs.replay(hs, start, end, RULES, POOL, 100_000, 0, "100k")
    (s,) = res.signals
    assert s.condition_id == "0xq" and s.ts == T0 + 15 * DAY + 60 and len(s.entries) >= 3


def test_the_random_baseline_and_report_never_show_a_profit_without_n_and_an_interval():
    g = cs.GroupStats("x", trades=40, filled=35, missed=5, settled=32, events=20, pnl=12.5, cost=320.0, roi=0.039,
                      mean_roi=0.04, ci=(-0.05, 0.13), win_rate=0.55, drawdown=30.0)
    line = cs._group_line(g)
    assert "32 settled in 20 events" in line and "95% CI [-5.0%, +13.0%]" in line and "ROI +3.9%" in line
    v = cs.VariantResult("100k", pool_sizes=[5, 6, 7], entity_counts=[5], consensus=g, single=g, random=g,
                         paired=(30, -0.01, -0.08, 0.06))
    rep = cs.ConsensusReport(T0, T0 + 180 * DAY, 700, 400, RULES, POOL, variants=[v])
    text = cs.describe(rep)
    assert "no difference shown" in text and "Verdict: FAILS" in text and "REALISED" in text
    v.consensus = cs.GroupStats("x", trades=3, filled=3, settled=3)
    assert "NOT ENOUGH SIGNALS" in cs.verdict_line(rep)


async def test_todays_pool_comes_from_the_leaderboard_and_is_stored_wallet_by_wallet(live_app, fake_http, clock):
    from world import World
    world = World(fake_http)
    names = {"0xgood": 900_000, "0xtwin": 600_000, "0xidle": 400_000, "0xsmall": 150_000, "0xloss": -10}
    world.leaderboard = [{"rank": i, "user_id": w, "pnl": p, "volume": 1, "user_name": w[2:]}
                         for i, (w, p) in enumerate(names.items(), start=1)]
    for w in ("0xgood", "0xtwin", "0xsmall"):                  # 8 transactions a day for 25 days, in the same markets
        for i in range(200):
            world.trade(w, NOW - 25 * DAY + i * 10_800 + (5 if w == "0xtwin" else 0), f"0xm{i % 12 if w != '0xsmall' else 50 + i % 12}",
                        size=10, price=0.5)
    world.trade("0xidle", NOW - DAY, "0xm1", size=10, price=0.5)
    pool = await cs.live_pool(live_app)
    assert pool.counts["100k"] == {"board": 4, "passed": 3, "entities": 2}, "0xidle fails; 0xgood and 0xtwin are one"
    assert pool.counts["500k"] == {"board": 2, "passed": 2, "entities": 1}
    assert pool.entity_of["0xtwin"] == pool.entity_of["0xgood"] == "0xgood"
    stored = {r["wallet"]: r for r in live_app.db.query("SELECT * FROM consensus_pool WHERE run_id = ?", (pool.run_id,))}
    assert set(stored) == {"0xgood", "0xtwin", "0xidle", "0xsmall"}, "wallets without a profit are not read"
    assert stored["0xidle"]["passed"] == 0 and "trades a day < 5" in stored["0xidle"]["reasons"]
    assert stored["0xtwin"]["entity"] == "0xgood" and stored["0xgood"]["rank"] == 1
    text = cs.describe_pool(pool, live_app.cfg.consensus_pool)
    assert "$100k+ profit" in text and "good (#1) = twin (#2)" in text and "Not checked: wallets funded" in text


def test_one_wallet_is_not_a_consensus():
    cfg = Config()
    cfg.consensus.min_agreeing_entities = 1
    with pytest.raises(ConfigError, match="at least 2"):
        validate(cfg)
