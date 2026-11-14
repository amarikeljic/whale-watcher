"""v2 Phase 2: the persistent watchlist.

The monitored set used to be replaced every night by that night's top wallets.
Now it is a list that grows: a wallet joins when it meets the entry rules, is
re-scored every night, and leaves only after failing the (looser) exit rules
several nights in a row. Entry stricter than exit is the hysteresis that stops a
wallet near a line from flipping in and out.

    nightly():  discover -> review members -> add newcomers -> record everything

Nothing here is ever deleted. Every stay on the list is one row of
``watchlist_membership`` (a wallet that comes back gets a new row) and every
decision of every review is one row of ``watchlist_events``, so the list as it
stood on any past night can be rebuilt, and no alert is ever judged with
knowledge that came later: each alert stores the membership row and the score
snapshot that were current when it fired.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .app import App
from .config import Config, ScoringConfig, WatchlistExit
from .scoring import ScoreRun, WalletMetrics, candidate_pool, run_scoring
from .timeutil import iso, start_of_day

log = logging.getLogger(__name__)

DAY = 86_400
SEEDED_KEY = "watchlist_seeded"
CARRIED_OVER = "carried over from the v1 monitored set"
ADD, KEEP, FAIL, REMOVE, WAIT, REJECT, SKIP = "ADD", "KEEP", "FAIL", "REMOVE", "WAIT", "REJECT", "SKIP"


# -- rules ----------------------------------------------------------------------------


def entry_scoring(cfg: Config) -> ScoringConfig:
    """The scoring thresholds the nightly review runs with: ``scoring`` (how metrics
    are computed, ranking, the unreliable-market and too-active limits) with the
    watchlist's entry rules in place of its filter thresholds."""
    e = cfg.watchlist.entry
    return dataclasses.replace(
        cfg.scoring, min_roi=e.min_roi, min_settled_markets=e.min_settled_markets,
        min_profitable_months=e.min_profitable_months, max_single_market_share=e.max_single_market_share,
        max_two_sided_ratio=e.max_two_sided_ratio, max_days_since_last_trade=e.max_days_since_last_trade,
        top_k=cfg.watchlist.max_size)


def exit_failures(m: WalletMetrics, x: WatchlistExit, scoring: ScoringConfig) -> list[str]:
    """Exit rules the wallet fails on its score, empty if it passes them all."""
    if m.truncated:
        return [f"too active to score (>{scoring.max_activity_rows} rows: bot or market maker)"]
    out = []
    if m.roi is None or m.roi < x.min_roi:
        out.append(f"roi {_num(m.roi, '+.1%')} < {x.min_roi:+.1%}")
    if m.settled_markets < x.min_settled_markets:
        out.append(f"settled markets {m.settled_markets} < {x.min_settled_markets}")
    if m.max_single_market_share is None or m.max_single_market_share > x.max_single_market_share:
        out.append(f"largest market's share of profit {_num(m.max_single_market_share, '.2f')} "
                   f"> {x.max_single_market_share:g}")
    if m.days_since_last_trade is None or m.days_since_last_trade > x.max_days_since_last_trade:
        out.append(f"days since last trade {_num(m.days_since_last_trade, '.0f')} > {x.max_days_since_last_trade:g}")
    if m.two_sided_ratio is not None and m.two_sided_ratio > x.max_two_sided_ratio:
        out.append(f"two-sided ratio {m.two_sided_ratio:.2f} > {x.max_two_sided_ratio:g}")
    measured = m.settled_markets + m.unreliable_markets
    if measured and m.unreliable_markets / measured > scoring.max_unreliable_market_share:
        out.append(f"markets we cannot account for {m.unreliable_markets}/{measured}")
    return out


def _num(value: float | None, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


@dataclass(slots=True)
class Screen:
    """Lifetime figures from /v2/user-stats that mark a market maker or a bot."""
    rebate_share: float | None = None        # liquidity income / (liquidity income + position profit)
    fills_per_market: float | None = None


def screen_from_stats(stats: dict[str, Any] | None) -> Screen:
    """A wallet whose income is mostly maker rebates and liquidity rewards profits from
    providing liquidity, not from calling outcomes, and one with hundreds of fills per
    market is a machine working an order book. Neither can be copied by buying after it.
    (The rebate-share idea is from drkxbt/sharps, MIT; this is our own implementation,
    and it leaves taker rebates out because those are earned by taking, not making.)"""
    if not stats:
        return Screen()
    totals = stats.get("all_time_pnl") or {}
    liquidity = float(totals.get("maker_rebate") or 0) + float(totals.get("reward_income") or 0)
    profit = max(0.0, float(totals.get("position_pnl") or 0))
    share = liquidity / (liquidity + profit) if liquidity > 0 and liquidity + profit > 0 else 0.0
    markets = float(stats.get("trades") or 0)
    fills = float(totals.get("trade_count") or 0)
    return Screen(rebate_share=share, fills_per_market=fills / markets if markets > 0 else None)


def screen_failures(s: Screen, max_rebate_share: float, max_fills_per_market: float) -> list[str]:
    out = []
    if s.rebate_share is not None and s.rebate_share > max_rebate_share:
        out.append(f"likely market maker: {s.rebate_share:.0%} of lifetime income is rebates and rewards "
                   f"(limit {max_rebate_share:.0%})")
    if s.fills_per_market is not None and s.fills_per_market > max_fills_per_market:
        out.append(f"likely bot: {s.fills_per_market:.0f} fills per market (limit {max_fills_per_market:g})")
    return out


def live_record(app: App, wallet: str, as_of: int | None = None) -> tuple[int, float | None]:
    """(events, mean CLV) of our own settled live paper trades of this wallet's alerts.

    One figure per event (a wallet's fills in one game are one bet), over fills that
    have a closing price. In-play fills have none (stats.py explains why) and are
    left out. Simulated (backtest) trades never count.

    Two ledgers: the bot's headline strategy (``single / flat_hold``), where every
    copy has gone since the bot model started, and the old rule's paper trades from
    before it. Reading only the second froze this rule (found 2026-11-14).
    ``as_of``: only trades settled by then, so a review for a past date sees only
    what was known on that date."""
    until = app.clock() + 1 if as_of is None else as_of
    rows = app.db.query(
        "SELECT COALESCE(m.event_slug, p.condition_id) AS event, p.closing_price - p.fill_price AS clv "
        "FROM paper_trades p JOIN alerts a ON a.id = p.alert_id "
        "LEFT JOIN poly_markets m ON m.condition_id = p.condition_id "
        "WHERE a.wallet = ? AND p.status = 'FILLED' AND p.settled_at IS NOT NULL AND p.settled_at <= ? "
        "AND p.closing_price IS NOT NULL AND p.fill_price IS NOT NULL AND p.strategy NOT LIKE 'backtest:%' "
        "AND NOT (m.game_start_time IS NOT NULL AND p.fill_ts >= m.game_start_time) "
        "UNION ALL "
        "SELECT COALESCE(m.event_slug, b.condition_id) AS event, b.closing_price - b.fill_price AS clv "
        "FROM bot_trades b LEFT JOIN poly_markets m ON m.condition_id = b.condition_id "
        "WHERE b.wallet = ? AND b.runner = 'single' AND b.strategy = 'flat_hold' AND b.status = 'FILLED' "
        "AND b.closed_at IS NOT NULL AND b.closed_at <= ? "
        "AND b.closing_price IS NOT NULL AND b.fill_price IS NOT NULL "
        "AND NOT (m.game_start_time IS NOT NULL AND b.decided_at >= m.game_start_time)",
        (wallet, until, wallet, until))
    by_event: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r["clv"])
    if not by_event:
        return 0, None
    per_event = [sum(v) / len(v) for v in by_event.values()]
    return len(per_event), sum(per_event) / len(per_event)


def live_failures(app: App, wallet: str, x: WatchlistExit, as_of: int | None = None) -> list[str]:
    events, clv = live_record(app, wallet, as_of)
    if clv is not None and events >= x.live_min_events and clv < x.live_max_clv:
        return [f"live CLV {clv:+.3f} over {events} settled events < {x.live_max_clv:+.3f}"]
    return []


# -- membership -------------------------------------------------------------------------


def members(app: App) -> dict[str, Any]:
    """wallet -> its open membership row."""
    return {r["wallet"]: r for r in app.db.query(
        "SELECT * FROM watchlist_membership WHERE removed_at IS NULL ORDER BY added_at, id")}


def membership_at(app: App, wallet: str, ts: int | None = None) -> Any | None:
    """The membership row the wallet was on at ``ts`` (now if omitted), or None."""
    ts = app.clock() if ts is None else ts
    return app.db.one(
        "SELECT * FROM watchlist_membership WHERE wallet = ? AND added_at <= ? "
        "AND (removed_at IS NULL OR removed_at > ?) ORDER BY added_at DESC, id DESC LIMIT 1", (wallet, ts, ts))


def seed_from_monitored(app: App) -> int:
    """First run after the upgrade: the wallets v1 was monitoring become members, so
    nothing stops being tracked before the first review. That review holds them to
    the entry rules (see nightly); after it they are members like any other."""
    if app.db.get_state(SEEDED_KEY) is not None:
        return 0
    days = app.cfg.watchlist.probation_days
    current = members(app)
    n = 0
    with app.db.tx() as conn:
        for r in conn.execute("SELECT wallet, run_id, added_at FROM monitored_wallets WHERE active = 1").fetchall():
            if r["wallet"] in current:
                continue
            conn.execute(
                "INSERT INTO watchlist_membership (wallet, added_at, add_reason, added_run_id, probation_until) "
                "VALUES (?,?,?,?,?)",
                (r["wallet"], r["added_at"], CARRIED_OVER, r["run_id"],
                 r["added_at"] + days * DAY))
            n += 1
        conn.execute("INSERT INTO service_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (SEEDED_KEY, json.dumps(app.clock())))
    if n:
        log.info("watchlist: carried %d wallets over from the v1 monitored set", n)
    return n


def sync_monitored(app: App, run_id: int | None, ranks: dict[str, int | None] | None = None) -> None:
    """Make the poller's table (monitored_wallets) follow the watchlist. Its polling
    state (last_seen_ts) survives for wallets that stay."""
    now = app.clock()
    current = members(app)
    ranks = ranks or {}
    with app.db.tx() as conn:
        for r in conn.execute("SELECT wallet FROM monitored_wallets WHERE active = 1").fetchall():
            if r["wallet"] not in current:
                conn.execute("UPDATE monitored_wallets SET active = 0, removed_at = ? WHERE wallet = ?",
                             (now, r["wallet"]))
        for wallet, row in current.items():
            snapshot = run_id if run_id is not None else (row["last_review_run_id"] or row["added_run_id"] or 0)
            conn.execute(
                "INSERT INTO monitored_wallets (wallet, run_id, rank, active, added_at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(wallet) DO UPDATE SET run_id = excluded.run_id, rank = excluded.rank, active = 1, "
                "removed_at = NULL", (wallet, snapshot, ranks.get(wallet), row["added_at"]))
        if run_id is not None:
            conn.execute("INSERT INTO service_state (key, value) VALUES ('monitored_run_id', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(run_id),))


# -- the nightly review -------------------------------------------------------------------


@dataclass(slots=True)
class Decision:
    wallet: str
    action: str
    detail: str
    membership_id: int | None = None


@dataclass
class Review:
    run_id: int | None
    as_of: int
    before: int
    after: int
    max_size: int
    decisions: list[Decision] = field(default_factory=list)
    run: ScoreRun | None = None

    def of(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]


def _describe(m: WalletMetrics, rank_by: str) -> str:
    return (f"rank {m.rank} by {rank_by}, roi {_num(m.roi, '+.1%')}, {m.settled_markets} settled markets, "
            f"largest market {_num(m.max_single_market_share, '.2f')} of profit, "
            f"last trade {_num(m.days_since_last_trade, '.0f')}d ago")


async def _screen(app: App, wallet: str) -> Screen | None:
    """None when the stats could not be read (a failed call must not look like a pass)."""
    try:
        return screen_from_stats(await app.data.user_stats(wallet))
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown tonight", never "passed"
        log.warning("user-stats failed for %s: %s", wallet, exc)
        return None


async def nightly(app: App, *, as_of: int | None = None,
                  progress: Callable[[str], None] | None = None) -> Review:
    """Discover, review, add, remove. Returns what was decided and why."""
    cfg = app.cfg
    wl, x = cfg.watchlist, cfg.watchlist.exit
    seed_from_monitored(app)
    as_of = as_of if as_of is not None else start_of_day(app.clock())
    # A review for a past date may use only what was known on that date: the leak-free
    # pool instead of today's leaderboards, our own record up to that date, and no
    # market-maker check (its figures are lifetime totals as of today).
    past = as_of < start_of_day(app.clock())
    own_until = as_of if past else None
    before = members(app)

    # 1. Discover. Members are always in the pool, so each one is re-scored even on a
    #    night it appears on no leaderboard.
    pool, notes = await candidate_pool(app, as_of, historical=past)
    if past:
        notes.append("review for a past date: leak-free candidate pool, our own record up to that date, and no "
                     "market-maker check (it uses lifetime figures as of today)")
    for wallet in before:
        pool.setdefault(wallet, set()).add("watchlist")
    scoring = entry_scoring(cfg)
    run = await run_scoring(app, as_of, kind="nightly", pool=pool, extra_notes=notes, scoring_cfg=scoring,
                            progress=progress)
    by_wallet = {m.wallet: m for m in run.metrics}
    now = app.clock()
    review = Review(run.run_id, as_of, len(before), len(before), wl.max_size, run=run)
    updates: list[tuple[Any, ...]] = []       # (fail_streak, failed rules JSON, membership id)
    removals: list[tuple[str, int]] = []      # (reason, membership id)

    # 2. Review everyone already on the list against the exit rules.
    for wallet, row in before.items():
        m = by_wallet.get(wallet)
        if m is None or m.fetch_failed:
            review.decisions.append(Decision(wallet, SKIP, "could not be scored tonight (data unavailable); "
                                                           "not counted as a failed review", row["id"]))
            continue
        fails = exit_failures(m, x, scoring) + live_failures(app, wallet, x, own_until)
        screen = None if past else await _screen(app, wallet)
        if screen is not None:
            fails += screen_failures(screen, x.max_rebate_share, x.max_fills_per_market)
        # A wallet carried over from v1 never passed the entry rules: v1 picked a nightly
        # top 20 with the ROI floor switched off. Hysteresis protects wallets that
        # qualified, so at its first v2 review a carried-over wallet has to qualify too.
        if row["add_reason"] == CARRIED_OVER and row["last_review_run_id"] is None:
            entry_fails = [] if m.rank is not None else list(m.fail_reasons)
            if screen is not None:
                entry_fails += [f for f in screen_failures(screen, wl.entry.max_rebate_share,
                                                           wl.entry.max_fills_per_market) if f not in fails]
            if entry_fails and not fails:
                reason = "carried over from v1, but does not meet the v2 entry rules: " + "; ".join(entry_fails)
                updates.append((0, json.dumps(entry_fails), row["id"]))
                removals.append((reason, row["id"]))
                review.decisions.append(Decision(wallet, REMOVE, reason, row["id"]))
                continue
        if not fails:
            updates.append((0, "[]", row["id"]))
            review.decisions.append(Decision(wallet, KEEP, "passes every exit rule: " + _describe_kept(m), row["id"]))
            continue
        # A second review on the same data day (a manual run plus the scheduled one)
        # must not count the same failure twice.
        streak = row["fail_streak"] if _already_failed_for(app, row, as_of) else row["fail_streak"] + 1
        reasons = "; ".join(fails)
        updates.append((streak, json.dumps(fails), row["id"]))
        if streak >= x.fail_reviews_in_a_row:
            reason = f"{reasons} (failed {streak} reviews in a row)"
            removals.append((reason, row["id"]))
            review.decisions.append(Decision(wallet, REMOVE, reason, row["id"]))
        else:
            review.decisions.append(Decision(
                wallet, FAIL, f"failed review {streak} of {x.fail_reviews_in_a_row} before removal: {reasons}",
                row["id"]))

    # 3. Add newcomers that pass the entry rules, best ranked first, while there is room.
    #    A wallet leaves only by failing the exit rules, so nobody is pushed out to make room.
    size = len(before) - len(removals)
    additions: list[tuple[WalletMetrics, str]] = []
    newcomers = sorted((m for m in run.metrics if m.rank is not None and m.wallet not in before),
                       key=lambda m: m.rank)
    for m in newcomers:
        blocked = exit_failures(m, x, scoring) + live_failures(app, m.wallet, x, own_until)
        if blocked:
            review.decisions.append(Decision(m.wallet, REJECT, "passes the entry rules on its score, but: "
                                             + "; ".join(blocked)))
            continue
        if size >= wl.max_size:
            review.decisions.append(Decision(m.wallet, WAIT, f"qualifies, but the watchlist is full "
                                                             f"({wl.max_size}): {_describe(m, scoring.rank_by)}"))
            continue
        screen = None if past else await _screen(app, m.wallet)
        if screen is None and not past:
            review.decisions.append(Decision(m.wallet, WAIT, "qualifies, but the market-maker check could not "
                                                             "be read tonight; will retry at the next review"))
            continue
        flagged = [] if screen is None else screen_failures(screen, wl.entry.max_rebate_share,
                                                             wl.entry.max_fills_per_market)
        if flagged:
            review.decisions.append(Decision(m.wallet, REJECT, "; ".join(flagged)))
            continue
        additions.append((m, f"passed the entry rules: {_describe(m, scoring.rank_by)}"))
        size += 1

    # 4. Record everything in one transaction.
    probation_until = now + wl.probation_days * DAY
    with app.db.tx() as conn:
        for streak, failed, mid in updates:
            conn.execute("UPDATE watchlist_membership SET fail_streak = ?, last_review = ?, last_review_at = ?, "
                         "last_review_run_id = ? WHERE id = ?", (streak, failed, now, run.run_id, mid))
        for reason, mid in removals:
            conn.execute("UPDATE watchlist_membership SET removed_at = ?, remove_reason = ?, removed_run_id = ? "
                         "WHERE id = ?", (now, reason, run.run_id, mid))
        for m, reason in additions:
            cur = conn.execute(
                "INSERT INTO watchlist_membership (wallet, added_at, add_reason, added_run_id, probation_until, "
                "last_review_at, last_review_run_id, last_review) VALUES (?,?,?,?,?,?,?, '[]')",
                (m.wallet, now, reason, run.run_id, probation_until, now, run.run_id))
            review.decisions.append(Decision(
                m.wallet, ADD, f"{reason}; on probation until {iso(probation_until)[:10]}", int(cur.lastrowid)))
        conn.executemany(
            "INSERT INTO watchlist_events (at, run_id, wallet, membership_id, action, detail) VALUES (?,?,?,?,?,?)",
            [(now, run.run_id, d.wallet, d.membership_id, d.action, d.detail) for d in review.decisions])
        # In a nightly snapshot `selected` means: on the watchlist after this review.
        on_list = {r["wallet"] for r in conn.execute(
            "SELECT wallet FROM watchlist_membership WHERE removed_at IS NULL").fetchall()}
        conn.execute("UPDATE wallet_score_snapshots SET selected = 0 WHERE run_id = ?", (run.run_id,))
        conn.executemany("UPDATE wallet_score_snapshots SET selected = 1 WHERE run_id = ? AND wallet = ?",
                         [(run.run_id, w) for w in on_list])
    for m in run.metrics:
        m.selected = m.wallet in on_list
    review.after = len(on_list)
    sync_monitored(app, run.run_id, {m.wallet: m.rank for m in run.metrics})
    log.info("watchlist review run #%s: %d before, +%d added, %d kept, %d failing, -%d removed, %d now (max %d)",
             run.run_id, review.before, len(review.of(ADD)), len(review.of(KEEP)), len(review.of(FAIL)),
             len(review.of(REMOVE)), review.after, wl.max_size)
    return review


def _describe_kept(m: WalletMetrics) -> str:
    return (f"roi {_num(m.roi, '+.1%')}, {m.settled_markets} settled, largest market "
            f"{_num(m.max_single_market_share, '.2f')}, last trade {_num(m.days_since_last_trade, '.0f')}d ago")


def _already_failed_for(app: App, row: Any, as_of: int) -> bool:
    if not row["last_review_run_id"] or row["last_review"] in (None, "[]"):
        return False
    last = app.db.one("SELECT as_of FROM score_runs WHERE id = ?", (row["last_review_run_id"],))
    return last is not None and last["as_of"] == as_of


# -- display -----------------------------------------------------------------------------


def _short(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-3:]}" if len(wallet) > 12 else wallet


ORDER = (ADD, KEEP, FAIL, REMOVE, SKIP, WAIT, REJECT)
LABEL = {ADD: "ADDED", KEEP: "KEPT", FAIL: "FAILING (still on the list)", REMOVE: "REMOVED",
         SKIP: "NOT REVIEWED", WAIT: "QUALIFIED, NOT ADDED", REJECT: "REJECTED AT THE DOOR"}


def render_review(run_id: int | None, as_of: int, before: int, after: int, max_size: int,
                  decisions: list[Decision]) -> str:
    count = {a: sum(1 for d in decisions if d.action == a) for a in ORDER}
    lines = [f"Watchlist review, snapshot #{run_id}, using only data before {iso(as_of)}",
             f"  on the list before: {before}   added: {count[ADD]}   kept: {count[KEEP]}   "
             f"failing: {count[FAIL]}   removed: {count[REMOVE]}   on the list now: {after} of {max_size}"]
    for action in ORDER:
        rows = [d for d in decisions if d.action == action]
        if not rows:
            continue
        lines.append("")
        lines.append(f"{LABEL[action]} ({len(rows)})")
        lines += [f"  {_short(d.wallet):12s} {d.detail}" for d in rows]
    return "\n".join(lines)


def render_last_review(app: App) -> str:
    """The most recent review, rebuilt from watchlist_events."""
    last = app.db.one("SELECT run_id FROM watchlist_events WHERE run_id IS NOT NULL ORDER BY id DESC LIMIT 1")
    if last is None:
        return "No watchlist review has run yet."
    run = app.db.one("SELECT as_of FROM score_runs WHERE id = ?", (last["run_id"],))
    events = app.db.query("SELECT * FROM watchlist_events WHERE run_id = ? ORDER BY id", (last["run_id"],))
    decisions = [Decision(e["wallet"], e["action"], e["detail"] or "", e["membership_id"]) for e in events]
    at = events[0]["at"]
    after = app.db.one("SELECT COUNT(*) n FROM watchlist_membership WHERE added_at <= ? "
                       "AND (removed_at IS NULL OR removed_at > ?)", (at, at))["n"]
    before = after - sum(1 for d in decisions if d.action == ADD) + sum(1 for d in decisions if d.action == REMOVE)
    return render_review(last["run_id"], run["as_of"] if run else 0, before, after, app.cfg.watchlist.max_size,
                         decisions)


def current_rows(app: App) -> list[dict[str, Any]]:
    """Each member with the metrics of its latest review."""
    now = app.clock()
    out = []
    for wallet, row in members(app).items():
        run_id = row["last_review_run_id"] or row["added_run_id"]
        snap = app.db.one("SELECT * FROM wallet_score_snapshots WHERE run_id = ? AND wallet = ?", (run_id, wallet))
        events, clv = live_record(app, wallet)
        alerts = app.db.one("SELECT COUNT(*) n FROM alerts WHERE wallet = ? AND status IN ('RECORDED', 'POSTED', 'UNSENT')",
                            (wallet,))["n"]
        failing = json.loads(row["last_review"] or "[]")
        if row["fail_streak"]:
            status = f"failing {row['fail_streak']}/{app.cfg.watchlist.exit.fail_reviews_in_a_row}"
        elif now < row["probation_until"]:
            status = f"probation to {iso(row['probation_until'])[:10]}"
        else:
            status = "established"
        out.append({"wallet": wallet, "status": status, "days": (now - row["added_at"]) / DAY, "snap": snap,
                    "run_id": run_id, "live_events": events, "live_clv": clv, "alerts": alerts,
                    "failing": failing, "add_reason": row["add_reason"]})
    return out


def render_current(app: App) -> str:
    rows = current_rows(app)
    wl = app.cfg.watchlist
    if not rows:
        return "The watchlist is empty. It fills at the next nightly review (or run `watchlist --review`)."
    head = ("wallet", "status", "days", "roi", "settled", "max_share", "last_trade", "alerts", "live_ev",
            "live_clv", "snapshot")
    body = []
    for r in rows:
        s = r["snap"]
        body.append([
            _short(r["wallet"]), r["status"], f"{r['days']:.0f}",
            _num(s["roi"] if s else None, "+.1%"), str(s["settled_markets"]) if s and s["settled_markets"] is not None
            else "-", _num(s["max_single_market_share"] if s else None, ".2f"),
            (_num(s["days_since_last_trade"], ".0f") + "d") if s and s["days_since_last_trade"] is not None else "-",
            str(r["alerts"]), str(r["live_events"]), _num(r["live_clv"], "+.3f"), f"#{r['run_id']}"])
    widths = [max(len(h), *(len(b[i]) for b in body)) for i, h in enumerate(head)]
    def line(cells: list[str] | tuple[str, ...]) -> str:
        return "  ".join(c.ljust(w) if i < 2 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths, strict=True)))

    lines = [f"Watchlist: {len(rows)} of {wl.max_size} wallets. Metrics are from each wallet's latest review.",
             line(head), "  ".join("-" * w for w in widths)]
    lines += [line(b) for b in body]
    failing = [r for r in rows if r["failing"]]
    if failing:
        lines.append("")
        lines.append("Failing an exit rule at the last review:")
        lines += [f"  {_short(r['wallet']):12s} {'; '.join(r['failing'])}" for r in failing]
    lines.append("")
    lines.append(f"live_ev / live_clv: settled events and mean closing-line value of OUR paper trades on the "
                 f"wallet's alerts.\nA wallet with {wl.exit.live_min_events}+ events and live CLV below "
                 f"{wl.exit.live_max_clv:+.3f} fails its review. Fewer events than that is noise.")
    return "\n".join(lines)


def render_history(app: App) -> str:
    rows = app.db.query("SELECT * FROM watchlist_membership ORDER BY added_at, id")
    if not rows:
        return "No watchlist history yet."
    lines = ["Watchlist history (every stay on the list; never deleted):"]
    events: list[tuple[int, int, str]] = []
    for r in rows:
        added = (f"  {iso(r['added_at'])[:16]}  ADDED    {_short(r['wallet']):12s} "
                 f"(snapshot #{r['added_run_id']}) {r['add_reason']}")
        events.append((r["added_at"], r["id"], added))
        if r["removed_at"] is not None:
            days = (r["removed_at"] - r["added_at"]) / DAY
            removed = (f"  {iso(r['removed_at'])[:16]}  REMOVED  {_short(r['wallet']):12s} "
                       f"(snapshot #{r['removed_run_id']}) after {days:.0f} days: {r['remove_reason']}")
            events.append((r["removed_at"], r["id"], removed))
    lines += [text for _, _, text in sorted(events)]
    open_now = sum(1 for r in rows if r["removed_at"] is None)
    lines.append(f"\n{len(rows)} stays in total, {open_now} open now.")
    return "\n".join(lines)
