"""v2 Phase 5: the decision rule, "Copying has an edge if mean CLV stays above 0 and the
95% CI for ROI excludes 0 after 100 settled events", and progress toward it."""

from __future__ import annotations

from conftest import NOW
from dashfix import A, B, Fixture, small
from whalewatcher import dashboard as dash
from whalewatcher.config import Config
from whalewatcher.db import Database
from whalewatcher.stats import DecisionRule, render
from whalewatcher.version import MODEL_VERSION


def rule(events=100, clv=0.02, lo=0.01, hi=0.09):
    return DecisionRule(events=events, need=100, clv=clv, clv_n=40, mean=(lo + hi) / 2 if lo is not None else None,
                        lo=lo, hi=hi)


def test_each_verdict():
    assert rule(events=37).verdict == "not decided yet: 37 of 100 settled events"
    assert rule().met and rule().verdict == "copying has an edge: both tests pass after 100 settled events"
    assert rule(lo=-0.09, hi=-0.01).verdict == "copying loses money: the whole interval is below 0 after 100 settled events"
    assert rule(clv=-0.01, lo=-0.02).verdict == ("no edge shown after 100 settled events: mean CLV is not above 0 and "
                                                "the interval includes 0")
    assert rule(clv=None).verdict.endswith("mean CLV is not above 0"), "no pre-game fill: CLV cannot pass"
    assert "after 100 settled events" in rule().text


async def test_stats_shows_the_rule_under_the_headline(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 3, pnl=5.0, closing=0.55)
    fx.trade(B, 2, pnl=-10.0, closing=0.45)
    text = render(app)
    assert "decision rule: 2 of 100 settled events | mean CLV +0.000 (n=2 pre-game fills), above 0: no" in text
    assert "-> not decided yet: 2 of 100 settled events" in text


async def test_with_no_closed_trade_the_progress_is_still_shown(app):
    Fixture(app.db, NOW).trade(A, 1)                       # open
    assert "decision rule: 0 of 100 settled events" in render(app)


def test_the_overview_shows_the_rule_its_three_tests_and_the_progress(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    small(path, NOW).db.close()
    html = dash.render(Config(database_path=path), path, "/", {"outages": "include"}, now=NOW)[1]
    assert "The decision rule" in html and "after 100 settled events." in html
    assert "5 of 100" in html and 'aria-label="5 of 100 settled events"' in html and 'width="5.0"' in html
    assert "+0.010 (n=2 pre-game fills)" in html, "CLV of the two pre-game fills: (+0.05 - 0.03) / 2"
    assert "Not decided yet: 5 of 100 settled events." in html
    assert "counting model 1 (current)" in html


def test_a_met_rule_says_so(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    db.stamp_new_rows(commit="abc1234", model_version=MODEL_VERSION, settings="0123456789ab")
    fx = Fixture(db, NOW)
    for k in range(100):
        fx.trade(A if k % 2 else B, 30 - k * 0.2, pnl=5.0, closing=0.55)     # 100 events, +50% each, CLV +0.05
    db.close()
    html = dash.render(Config(database_path=path), path, "/", {}, now=NOW)[1]
    assert "Copying has an edge: both tests pass after 100 settled events." in html
    assert 'class="banner good"' in html
