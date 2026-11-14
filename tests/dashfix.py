"""A small database with numbers that can be checked by hand, for the dashboard tests
(and, with more rows, for its screenshots)."""

from __future__ import annotations

from whalewatcher.db import Database

DAY = 86_400
A, B = "0xaaaa000000000000000000000000000000000001", "0xbbbb000000000000000000000000000000000002"


class Fixture:
    def __init__(self, db: Database, now: int) -> None:
        self.db, self.now, self.n = db, now, 0

    def market(self, cond: str, *, category: str = "sports", event: str | None = None, question: str | None = None,
               game_start: int | None = None, end_date: int | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO poly_markets (condition_id, question, slug, event_slug, category, game_start_time, "
            "end_date, description) VALUES (?,?,?,?,?,?,?,?)",
            (cond, question or f"Will {cond} happen?", f"m-{cond}", event or f"e-{cond}", category, game_start,
             end_date, f"Resolves Yes if {cond} happens. If the game is postponed beyond 7 days it resolves 50-50."))

    def trade(self, wallet: str, days_ago: float, *, strategy: str = "flat_hold", status: str = "FILLED",
              pnl: float | None = None, cost: float = 10.0, fill: float = 0.50, whale: float = 0.48,
              closing: float | None = None, markout: float | None = None, reason: str | None = None,
              detect: float | None = 3.0, cond: str | None = None, category: str = "sports", hour: int = 15,
              zero_pnl: float | None = None, human_pnl: float | None = None, probation: int | None = 0,
              hold_days: float = 0.5, event: str | None = None, end_days: float | None = None,
              skip_code: str | None = None) -> int:
        """One signal and what one strategy did with it. ``pnl`` set means it has closed.
        ``end_days``: the market's scheduled end, in days after our decision (None: no end date).
        ``hold_days``: when it really resolved, in days after the signal."""
        self.n += 1
        cond = cond or f"0xc{self.n}"
        ts = self.now - int(days_ago * DAY)
        ts = ts - ts % DAY + hour * 3600 + self.n % 50
        decided = ts + (detect or 3) + 1
        end = None if end_days is None else int(decided + end_days * DAY)
        self.market(cond, category=category, event=event, end_date=end)
        existing = self.db.one("SELECT id FROM alerts WHERE wallet = ? AND condition_id = ? AND first_fill_ts = ?",
                               (wallet, cond, ts))
        if existing:
            alert_id = existing["id"]
        else:
            alert_id = int(self.db.execute(
                "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
                "trigger_fill_ts, whale_shares, whale_usd, whale_price, fills, detected_at, status, detect_delay, "
                "probation, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wallet, cond, f"{cond}-t0", "Yes", "NEW", ts, ts, ts, 1000, 600, whale, 1, ts + 3, "POSTED", detect,
                 probation, ts)).lastrowid)
        filled = status == "FILLED"
        closed_at = ts + int(hold_days * DAY) if (pnl is not None or (status == "MISSED" and zero_pnl is not None)) else None
        self.db.execute(
            "INSERT INTO bot_trades (runner, strategy, alert_id, wallet, condition_id, token_id, event_key, status, "
            "reason, signal_ts, detected_at, decided_at, stake_usd, whale_price, best_ask, fill_price, contracts, fees, "
            "cost, zero_price, zero_pnl, human_done, human_pnl, closed_at, result, pnl, closing_price, markout_price, "
            "markout_done, updated_at, skip_code, scheduled_end, days_to_close, resolved_at, payout_per_share) "
            "VALUES ('single',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (strategy, alert_id, wallet, cond, f"{cond}-t0", event or f"e-{cond}", status, reason, ts, ts + (detect or 3),
             ts + (detect or 3) + 1, 10.0, whale, fill, fill if filled or status == "MISSED" else None,
             cost / fill if filled else None, 0.0 if filled else None, cost if filled else None, whale, zero_pnl,
             human_pnl, closed_at, ("WIN" if (pnl or 0) > 0 else "LOSS") if filled and pnl is not None else None,
             pnl if filled else (0.0 if closed_at else None), closing, markout, ts, skip_code, end, end_days,
             closed_at, (1.0 if pnl > 0 else 0.0) if filled and pnl is not None else None))
        return alert_id

    def us(self, alert_id: int, *, status: str = "FILLED", pnl: float | None = None, us_ask: float | None = 0.51,
           intl_ask: float = 0.50, reason: str | None = None, skip_code: str | None = None, fees: float = 0.24,
           slug: str | None = "aec-nfl-a-b-2026-11-01", side: str = "long", us_bid: float | None = None,
           rules: str | None = None) -> None:
        """Row A for an existing signal: the same alert priced on Polymarket US."""
        a = self.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        # As the real code does, the US row carries the baseline's schedule for the cutoff.
        base = self.db.one("SELECT scheduled_end, days_to_close FROM bot_trades WHERE runner = 'single' "
                           "AND strategy = 'flat_hold' AND alert_id = ?", (alert_id,))
        filled = status == "FILLED"
        # As the real code does: a price-ran miss keeps what the unlimited walk would have paid.
        walked = filled or (status == "MISSED" and (reason or "").startswith("price ran"))
        closed = a["first_fill_ts"] + DAY // 2 if pnl is not None else None
        self.db.execute(
            "INSERT INTO bot_trades (runner, strategy, alert_id, wallet, condition_id, token_id, event_key, status, "
            "reason, signal_ts, detected_at, decided_at, stake_usd, whale_price, best_ask, fill_price, contracts, fees, "
            "cost, human_done, closed_at, result, pnl, updated_at, skip_code, fee_rate, fee_source, us_slug, us_side, "
            "us_rule, intl_best_ask, price_gap, payout_per_share, us_best_bid, us_rules, us_end_date, scheduled_end, "
            "days_to_close) VALUES ('us','flat_hold',?,?,?,?,?,?,?,?,?,?,10,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?)",
            (alert_id, a["wallet"], a["condition_id"], a["token_id"], f"e-{a['condition_id']}", status, reason,
             a["first_fill_ts"], a["first_fill_ts"] + 3, a["first_fill_ts"] + 5, a["whale_price"], us_ask,
             us_ask if walked else None, 10 / us_ask if filled else None, fees if filled else None,
             10 + fees if filled else None, closed, ("WIN" if (pnl or 0) > 0 else "LOSS") if pnl is not None else None,
             pnl, a["first_fill_ts"], skip_code, 0.095 if filled else None, "us" if filled else None,
             slug if skip_code != "not_on_us" else None, side if slug and skip_code != "not_on_us" else None,
             "game_winner" if slug else "none", intl_ask,
             None if us_ask is None else round(us_ask - intl_ask, 6),
             (1.0 if pnl > 0 else 0.0) if pnl is not None else None, us_bid, rules,
             a["first_fill_ts"] + 14 * DAY if slug and skip_code != "not_on_us" else None,
             base["scheduled_end"] if base else None, base["days_to_close"] if base else None))

    def old_rule(self, wallet: str, days_ago: float, pnl: float) -> None:
        self.n += 1
        cond = f"0xo{self.n}"
        ts = self.now - int(days_ago * DAY)
        self.market(cond)
        alert_id = self.db.execute(
            "INSERT INTO alerts (wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, trigger_fill_ts, "
            "whale_shares, whale_usd, whale_price, fills, detected_at, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, cond, f"{cond}-t0", "NEW", ts, ts, ts, 1000, 600, 0.5, 1, ts + 35, "POSTED", ts)).lastrowid
        self.db.execute(
            "INSERT INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
            "alert_ts, fill_ts, fill_price, contracts, fees, cost, settled_at, pnl, updated_at) "
            "VALUES (?, 'polymarket', 'hold_to_resolution', 'FILLED', ?, ?, 10, 1, ?, ?, 0.5, 20, 0, 10, ?, ?, ?)",
            (alert_id, f"{cond}-t0", cond, ts + 35, ts + 95, ts + DAY // 2, pnl, ts))

    def member(self, wallet: str, added_days_ago: float, *, removed_days_ago: float | None = None,
               reason: str = "passed the entry rules: rank 1 by roi", why_removed: str | None = None,
               probation_days: int = 14) -> None:
        added = self.now - int(added_days_ago * DAY)
        self.db.execute(
            "INSERT INTO watchlist_membership (wallet, added_at, add_reason, added_run_id, probation_until, removed_at, "
            "remove_reason) VALUES (?,?,?,?,?,?,?)",
            (wallet, added, reason, 1, added + probation_days * DAY,
             None if removed_days_ago is None else self.now - int(removed_days_ago * DAY), why_removed))

    def snapshot(self, run_id: int, wallet: str, days_ago: float, roi: float, settled: int = 120) -> None:
        as_of = self.now - int(days_ago * DAY)
        self.db.execute("INSERT OR IGNORE INTO score_runs (id, as_of, kind, created_at, config_json, sources_json, "
                        "n_candidates, n_passed) VALUES (?, ?, 'nightly', ?, '{}', '{}', 400, 12)", (run_id, as_of, as_of + 1200))
        self.db.execute("INSERT INTO wallet_score_snapshots (run_id, wallet, as_of, settled_markets, roi, "
                        "max_single_market_share, passed) VALUES (?,?,?,?,?,?,1)", (run_id, wallet, as_of, settled, roi, 0.15))

    def outage(self, start_days_ago: float, hours: float, cause: str = "ISP outage") -> None:
        start = self.now - int(start_days_ago * DAY)
        self.db.execute("INSERT INTO outage_windows (start_ts, end_ts, cause, source) VALUES (?,?,?,'owner')",
                        (start, start + int(hours * 3600), cause))

    def heartbeat(self, runner_age: int = 4, detector_age: int = 3) -> None:
        self.db.set_state("heartbeat", self.now - runner_age)
        self.db.set_state("detector_heartbeat", self.now - detector_age)


def small(path: str, now: int) -> Fixture:
    """Five closed flat_hold trades: +12.50, -10, +6, +4, -10 on $10 each.
    Total +$2.50 on $50: ROI +5.0%, 3 wins of 5 (60%). One of them (-10) is inside the outage.

    Time to close: three were scheduled to end within a day (+12.50, -10, -10 = -$7.50) and two
    in 1 to 7 days (+6, +4 = +$10). The +4 was scheduled for 1 day and resolved after 3: two days
    late. The open trade is 30 days out (entered before the cutoff existed) and one signal was
    skipped as too long."""
    fx = Fixture(Database(path), now)
    one = fx.trade(A, 6, pnl=12.5, closing=0.55, markout=0.52, detect=3.0, zero_pnl=14.0, human_pnl=9.0,
                   category="sports", end_days=0.5)
    two = fx.trade(A, 5, pnl=-10.0, closing=0.47, markout=0.49, detect=4.0, zero_pnl=-10.0, human_pnl=-10.0,
                   category="sports", end_days=0.5)
    three = fx.trade(A, 4, pnl=6.0, markout=0.51, detect=5.0, zero_pnl=7.0, human_pnl=0.0, category="politics",
                     end_days=3, hold_days=3)
    # Row A, the same three signals on Polymarket US: the first filled a cent dearer and won
    # (+9.37 after a 24-cent fee), the second ran past the limit, the third is not listed there.
    fx.us(one, pnl=9.37, us_ask=0.51, intl_ask=0.50, us_bid=0.49,
          rules="If the game is postponed, this market will remain open until the game has been completed. "
                "If the game is canceled entirely, with no make-up game, this market will resolve 50-50.")
    fx.us(two, status="MISSED", us_ask=0.56, intl_ask=0.50, reason="price ran: filling $10 costs 0.560 a share, limit 0.510")
    fx.us(three, status="SKIPPED", us_ask=None, skip_code="not_on_us", slug=None,
          reason="not_on_us: no rule for politics markets")
    fx.trade(B, 4, pnl=4.0, detect=3.0, category="politics", end_days=1, hold_days=3)
    fx.trade(B, 3, pnl=-10.0, detect=4.0, hour=11, category="crypto", end_days=0.2)   # 11:00-12:00 UTC: in the outage
    fx.trade(A, 2, detect=3.0, end_days=30)                                      # open
    fx.trade(A, 2, status="MISSED", reason="price ran: filling $10 costs 0.560 a share, limit 0.510", fill=0.56,
             end_days=0.5)
    fx.trade(A, 1, status="SKIPPED", reason="event exposure: $50.00 open in this event, limit $50", end_days=0.5)
    fx.trade(A, 1, status="SKIPPED", reason="too_long: closes in 40.0 days (limit 14)", skip_code="too_long",
             end_days=40)
    fx.trade(A, 6, strategy="placebo_late", pnl=-3.0)
    fx.old_rule(A, 9, 2.0)
    fx.member(A, 40)
    fx.member(B, 30, removed_days_ago=2, why_removed="roi -3.0% < +0.0% (failed 3 reviews in a row)")
    fx.snapshot(1, A, 10, 0.10)
    fx.snapshot(2, A, 3, 0.14)
    start = now - 3 * DAY
    start = start - start % DAY + 10 * 3600 + 1800
    fx.db.execute("INSERT INTO outage_windows (start_ts, end_ts, cause, source) VALUES (?,?,?,'owner')",
                  (start, start + 2 * 3600, "ISP outage"))
    fx.heartbeat()
    return fx
