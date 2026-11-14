"""Part 4 #2: every variation listed with its role, and a forward test for anything that
looks good besides the headline."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import DAY, NOW
from dashfix import A, B, Fixture, small
from whalewatcher import dashboard as dash
from whalewatcher import experiments as ex
from whalewatcher.config import Config


def winners(fx, n: int, *, strategy: str, days_ago: float, wallet: str = A) -> None:
    for k in range(n):     # +50% each, bought at 0.50 against a closing price of 0.55: CLV +0.05
        fx.trade(wallet if k % 2 else B, days_ago - k * 0.01, strategy=strategy, pnl=5.0, closing=0.55)


async def test_every_variation_is_listed_with_its_role(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 5, pnl=2.0)
    fx.trade(A, 5, strategy="placebo_late", pnl=-1.0)
    fx.trade(A, 4, strategy="flat_mirror", pnl=1.0)
    roles = {e.key: e.role for e in ex.build(app)}
    assert roles["bot:single/flat_hold"] == ex.PRIMARY and roles["bot:single/placebo_late"] == ex.CONTROL
    assert roles["bot:single/flat_mirror"] == ex.EXPLORATORY
    assert roles["column:zero"] == roles["column:human"] == ex.COMPARISON
    text = "\n".join(ex.lines(app))
    assert "EXPERIMENTS: every variation tracked" in text and "about 0." in text and "by luck" in text


async def test_a_good_looking_variation_is_flagged_once_and_judged_only_on_later_signals(app, clock):
    fx = Fixture(app.db, NOW)
    winners(fx, 12, strategy="flat_mirror", days_ago=20)
    winners(fx, 12, strategy="flat_hold", days_ago=20)
    notices = ex.flag_new(app)
    assert len(notices) == 1 and "single / flat_mirror looks good" in notices[0], "the primary is never flagged"
    assert ex.flag_new(app) == [], "flagged once"
    (mirror,) = [e for e in ex.build(app) if e.key == "bot:single/flat_mirror"]
    assert mirror.flagged_at == NOW and mirror.after_n == 0
    assert "forward test: 0 of 100 settled events since then" in mirror.status, "what found it cannot also prove it"

    later = Fixture(app.db, NOW + 30 * DAY)                    # a month on: 100 more winners after the flag
    winners(later, 100, strategy="flat_mirror", days_ago=10)
    (mirror,) = [e for e in ex.build(app) if e.key == "bot:single/flat_mirror"]
    assert mirror.after_n == 100 and mirror.after_lo == pytest.approx(0.5) and mirror.after_events == 100
    assert mirror.after_clv == pytest.approx(0.05) and "meet the decision rule on their own: shown forward" in mirror.status


async def test_a_flag_is_never_moved_or_deleted(app):
    fx = Fixture(app.db, NOW)
    winners(fx, 12, strategy="flat_mirror", days_ago=20)
    ex.flag_new(app)
    with pytest.raises(sqlite3.IntegrityError, match="never moved"):
        app.db.execute("UPDATE experiment_flags SET flagged_at = 0")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        app.db.execute("DELETE FROM experiment_flags")


async def test_the_headline_meets_the_rule_only_with_enough_settled_trades(app):
    fx = Fixture(app.db, NOW)
    winners(fx, 12, strategy="flat_hold", days_ago=20)
    (head,) = [e for e in ex.build(app) if e.key == "bot:single/flat_hold"]
    assert head.looks_good and head.status == ("the headline, against the decision rule: not decided yet: "
                                                "12 of 100 settled events")


def test_the_edge_page_lists_the_variations(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    small(path, NOW).db.close()
    html = dash.render(Config(database_path=path), path, "/edge", {}, now=NOW)[1]
    assert "Every variation, and what counts as proof" in html and "primary" in html and "control" in html
