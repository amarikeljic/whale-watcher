"""v2 Phase 5: the daily summary shows the last 24 hours beside all-time, each with its n
and interval, the decision rule, and what the latest nightly review changed."""

from __future__ import annotations

from conftest import DAY, NOW
from dashfix import A, B, Fixture
from whalewatcher.stats import daily_summary


def table(text: str) -> dict[str, tuple[str, str]]:
    """The two columns of the side-by-side block, by row label."""
    out = {}
    for line in text.splitlines():
        if line.startswith("  ") and len(line) > 62 and not line.startswith("   "):
            label, a, b = line[2:38].strip(), line[38:64].strip(), line[64:].strip()
            if label:
                out[label] = (a, b)
    return out


async def test_the_last_24_hours_sit_beside_all_time_each_with_n_and_interval(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 1, pnl=5.0, hold_days=0.5)            # signal yesterday afternoon, closed in the night: last 24 h
    fx.trade(B, 5, pnl=-10.0, hold_days=0.5)          # a week's history
    fx.trade(A, 4, pnl=4.0, hold_days=0.5)
    fx.trade(A, 1, status="MISSED", reason="price ran")
    text = daily_summary(app)
    rows = table(text)
    assert rows["signals: filled / missed / skipped"] == ("1 / 1 / 0", "3 / 1 / 0")
    assert rows["closed trades (n)"] == ("1", "3")
    assert rows["net P&L"] == ("+$5.00", "-$1.00")
    day_ci, all_ci = rows["mean ROI per trade, 95% CI"]
    assert day_ci.startswith("+50.0%") and "[" in all_ci, "an interval in both columns: no profit figure without one"
    assert "last 24 hours" in text and "all-time" in text and "today" not in text.lower()
    assert "decision rule: 3 of 100 settled events" in text


async def test_a_quiet_day_says_so_and_still_shows_all_time(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 6, pnl=5.0, hold_days=0.5)
    rows = table(daily_summary(app))
    assert rows["closed trades (n)"] == ("0", "1") and rows["net P&L"] == ("-", "+$5.00")


async def test_the_nightly_reviews_changes_are_listed(app):
    fx = Fixture(app.db, NOW)
    fx.trade(A, 2, pnl=1.0)
    fx.member(A, 10)
    fx.member(B, 30, removed_days_ago=0.5, why_removed="roi -3.0% < +0.0% (failed 3 reviews in a row)")
    at = NOW - DAY // 2
    for wallet, action, detail in ((A, "KEEP", "rank 2 by roi"), ("0x" + "c" * 40, "ADD", "rank 1 by roi, roi +12.3%"),
                                   (B, "REMOVE", "roi -3.0% < +0.0% (failed 3 reviews in a row)"),
                                   ("0x" + "d" * 40, "REJECT", "a market maker")):
        app.db.execute("INSERT INTO watchlist_events (at, run_id, wallet, action, detail) VALUES (?, 7, ?, ?, ?)",
                       (at, wallet, action, detail))
    text = daily_summary(app)
    assert "watchlist, nightly review of" in text and "added 1, removed 1, failed a review 0, turned away 1" in text
    assert "  + 0xcccc…cccc: rank 1 by roi, roi +12.3%" in text
    assert "  - 0xbbbb…0002: roi -3.0% < +0.0% (failed 3 reviews in a row)" in text


async def test_before_any_review_the_summary_says_so(app):
    Fixture(app.db, NOW).trade(A, 2, pnl=1.0)
    assert "watchlist: no nightly review has run yet" in daily_summary(app)
