"""Validation: selection sees only pre-cutoff data; the holdout counts only positions opened after it."""

from __future__ import annotations

import pytest

from conftest import DAY
from whalewatcher.stats_util import bootstrap_mean_ci, max_drawdown, spearman
from whalewatcher.timeutil import add_months, parse_time
from whalewatcher.validate import run_validation
from world import FakeMarket, World

AS_OF = parse_time("2026-11-01T00:00:00Z")
CUTOFF = add_months(AS_OF, -2)


def loosen(cfg):
    s = cfg.scoring
    s.min_settled_markets, s.min_profitable_months, s.max_single_market_share = 6, 1, 0.9
    s.min_avg_stake_usd, s.min_clv_positions, s.top_k, s.rank_by = 10, 1, 1, "roi"
    cfg.candidates.historical_markets = 50
    cfg.candidates.historical_min_trade_usd = 50


def populate(world: World) -> None:
    """Two wallets. 'skilled' wins before the cutoff and keeps winning after it;
    'lucky' wins before the cutoff only. Both trade every market."""
    n = 0
    for phase, start in (("sel", CUTOFF - 60 * DAY), ("hold", CUTOFF + 5 * DAY)):
        for k in range(8):
            cond = f"0x{phase}{k}"
            opened = start + k * 5 * DAY
            resolved = opened + 2 * DAY
            world.add_market(FakeMarket(cond, resolved_at=resolved, payouts=[1.0, 0.0]))
            world.trade("0xskilled", opened, cond, index=0, size=250, price=0.4, usdc=100)
            lucky_side = 0 if phase == "sel" else 1
            world.trade("0xlucky", opened, cond, index=lucky_side, size=200, price=0.5 if phase == "sel" else 0.6,
                        usdc=100 if phase == "sel" else 120)
            n += 1
    # A position opened BEFORE the cutoff that resolves inside the holdout must not count there.
    world.add_market(FakeMarket("0xstraddle", resolved_at=CUTOFF + 10 * DAY, payouts=[1.0, 0.0]))
    world.trade("0xlucky", CUTOFF - 3 * DAY, "0xstraddle", index=0, size=10_000, price=0.01, usdc=100)


async def test_selection_and_holdout(app, fake_http):
    loosen(app.cfg)
    world = World(fake_http)
    populate(world)
    loads = []
    original_ensure = app.markets.ensure

    async def counting_ensure(ids, **kw):
        loads.append(len(set(ids)))
        return await original_ensure(ids, **kw)

    app.markets.ensure = counting_ensure
    rep = await run_validation(app, AS_OF, holdout_months=2, min_holdout_positions=3)
    assert len(loads) == 2, "one market load for the selection, one for the whole holdout"
    stages = [st.name for st in rep.watch.stages]
    assert "holdout: markets" in stages and "selection: wallet activity" in stages
    assert "total" in rep.watch.table()
    by_wallet = {r.wallet: r for r in rep.rows}
    skilled, lucky = by_wallet["0xskilled"], by_wallet["0xlucky"]
    # Selection, at the cutoff: both made money; skilled has the higher ROI (1.5 vs 1.0).
    assert skilled.selection.roi == pytest.approx(1.5) and lucky.selection.roi == pytest.approx(1.0)
    assert skilled.group == "selected" and lucky.group == "passed_rest"
    # Holdout: only the 8 positions opened after the cutoff; the straddling winner is excluded.
    assert skilled.n == 8 and skilled.roi == pytest.approx(1.5)
    assert lucky.n == 8 and lucky.roi == pytest.approx(-1.0)
    sel = next(g for g in rep.groups if g.name == "selected")
    assert sel.positions == 8 and sel.holdout_roi == pytest.approx(1.5)
    assert "Not enough data" in rep.verdict, "8 positions is not a conclusion"
    # The selection run is stored with the cutoff as its as_of.
    row = app.db.one("SELECT as_of, kind FROM score_runs WHERE id = ?", (rep.selection_run_id,))
    assert (row["as_of"], row["kind"]) == (CUTOFF, "validate-selection")
    # Selection data never reached past the cutoff: every activity request for the
    # selection ended at or before it.
    sel_calls = [c for c in fake_http.calls if c[1] == "/v2/activity"]
    assert any(int(c[2]["end"]) == CUTOFF for c in sel_calls)


def test_bootstrap_ci_brackets_the_mean():
    m, lo, hi = bootstrap_mean_ci([0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.05], samples=2000)
    assert lo < m < hi
    assert bootstrap_mean_ci([]) == (None, None, None)


def test_spearman_and_drawdown():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert max_drawdown([5, -3, -4, 2, 10, -1]) == pytest.approx(7)


def test_cluster_bootstrap_is_wider_than_pretending_positions_are_independent():
    from whalewatcher.stats_util import cluster_bootstrap_mean_ci
    # One wallet with 200 small wins, three wallets with a few big losses.
    groups = [[0.05] * 200, [-0.5] * 3, [-0.4] * 2, [-0.6] * 3]
    m, lo, hi = cluster_bootstrap_mean_ci(groups, samples=3000)
    flat = bootstrap_mean_ci([x for g in groups for x in g], samples=3000)
    assert m == pytest.approx(flat[0])
    assert (hi - lo) > 3 * (flat[2] - flat[1])
