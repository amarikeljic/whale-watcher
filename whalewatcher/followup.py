"""v2.1 Part 4 #10 and #11: what the whale did around each alert.

Worked out from the fills the detector stored for the wallet (``fill_events``), so it
costs no requests. Per alert:

* ``opposite_before_usd``: dollars of the market's other outcome the whale bought in the
  24 hours before the alert's first fill. Known when the bot acted.
* ``opposite_after_usd``: the same, in the hour after the alert's first fill. Known only
  later: it describes the alert, it could not have decided it.
* ``related_usd``: dollars bought in other markets of the same event, from 24 hours
  before to an hour after. A draw or a spread can offset a winner bet, or not.
* ``sold_back_share``: shares of the alert's outcome the whale sold within 30 minutes of
  the alert's first fill, as a share of the alert's shares (``whale_shares``).
* ``followed_at``: when the figures that need the hour after were filled in.

Classes, from all the other-side buying (before and after) against the alert's dollars:
``clean`` (none), ``partial`` (under ``hedge_ratio``), ``hedged`` (at least it: the whale
had little or no net bet). A fill the detector never saw (before the wallet was tracked)
is not counted, so a class can understate a hedge, never invent one.

Wallet flags, from its followed-up alerts only (all of them in the past):
* ``both sides``: at least ``min_alerts`` alerts, and on ``both_sides_share`` of them it
  bought the other side too (any amount).
* ``sells back``: at least ``min_alerts`` alerts, and on ``sold_back_alert_share`` of them
  it sold back ``sold_back_fraction`` of the position within 30 minutes.

The three switches that act on these (config ``followup``) are all off. Turning one on
changes which signals are copied: it is a new model version (version.py).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

HOUR, DAY = 3600, 86_400
BEFORE_SECONDS = DAY
AFTER_SECONDS = HOUR
SOLD_BACK_SECONDS = 30 * 60
CLEAN, PARTIAL, HEDGED = "clean", "partial", "hedged"
# Skip codes, recorded on bot_trades when a switch is on.
SKIP_HEDGED, SKIP_BOTH_SIDES, SKIP_SOLD_BACK = "hedged", "both_sides_wallet", "sold_back_wallet"
SKIP_CODES = (SKIP_HEDGED, SKIP_BOTH_SIDES, SKIP_SOLD_BACK)
ALERTED = ("RECORDED", "POSTED", "UNSENT", "SKIPPED")


def opposite_usd(db: Any, alert: Any, start: float, end: float) -> float:
    """Dollars of the same market's other outcome(s) this wallet bought in [start, end)."""
    row = db.one(
        "SELECT COALESCE(SUM(usdc_size), 0) AS usd FROM fill_events WHERE wallet = ? AND condition_id = ? "
        "AND token_id != ? AND side = 'BUY' AND ts >= ? AND ts < ?",
        (alert["wallet"], alert["condition_id"], alert["token_id"], int(start), int(end)))
    return float(row["usd"] or 0.0)


def related_usd(db: Any, alert: Any) -> float:
    """Dollars bought in other markets of the alert's event, 24 hours before to an hour after."""
    event = db.one("SELECT event_slug FROM poly_markets WHERE condition_id = ?", (alert["condition_id"],))
    if event is None or not event["event_slug"]:
        return 0.0
    start = alert["first_fill_ts"]
    row = db.one(
        "SELECT COALESCE(SUM(f.usdc_size), 0) AS usd FROM fill_events f "
        "JOIN poly_markets m ON m.condition_id = f.condition_id "
        "WHERE f.wallet = ? AND m.event_slug = ? AND f.condition_id != ? AND f.side = 'BUY' "
        "AND f.ts >= ? AND f.ts < ?",
        (alert["wallet"], event["event_slug"], alert["condition_id"], start - BEFORE_SECONDS, start + AFTER_SECONDS))
    return float(row["usd"] or 0.0)


def sold_back_share(db: Any, alert: Any) -> float:
    start = alert["first_fill_ts"]
    row = db.one(
        "SELECT COALESCE(SUM(size), 0) AS shares FROM fill_events WHERE wallet = ? AND token_id = ? "
        "AND side = 'SELL' AND ts >= ? AND ts <= ?",
        (alert["wallet"], alert["token_id"], start, start + SOLD_BACK_SECONDS))
    shares = alert["whale_shares"] or 0.0
    return float(row["shares"] or 0.0) / shares if shares > 0 else 0.0


def at_alert_time(db: Any, alert: Any) -> float:
    """``opposite_before_usd``, stored on the alert when it fires (what a live rule may use)."""
    value = opposite_usd(db, alert, alert["first_fill_ts"] - BEFORE_SECONDS, alert["first_fill_ts"])
    db.execute("UPDATE alerts SET opposite_before_usd = ? WHERE id = ?", (value, alert["id"]))
    return value


def update(app: Any, *, limit: int = 2000) -> int:
    """Fill in the follow-up of every alert whose hour has passed. Returns how many."""
    db, now = app.db, int(app.clock())
    rows = db.query(
        "SELECT * FROM alerts WHERE followed_at IS NULL AND status IN (?,?,?,?) AND first_fill_ts <= ? "
        "ORDER BY id LIMIT ?", (*ALERTED, now - AFTER_SECONDS - 60, limit))
    for a in rows:
        start = a["first_fill_ts"]
        before = a["opposite_before_usd"]
        if before is None:      # an alert from before this existed: same window, same data
            before = opposite_usd(db, a, start - BEFORE_SECONDS, start)
        db.execute(
            "UPDATE alerts SET opposite_before_usd = ?, opposite_after_usd = ?, related_usd = ?, sold_back_share = ?, "
            "followed_at = ? WHERE id = ?",
            (before, opposite_usd(db, a, start, start + AFTER_SECONDS), related_usd(db, a), sold_back_share(db, a),
             now, a["id"]))
    if rows:
        log.info("whale follow-up filled in for %d alerts", len(rows))
    return len(rows)


def hedge_class(row: Any, ratio: float) -> str | None:
    """clean | partial | hedged, or None until the follow-up is in."""
    if row.get("followed_at") is None or not row.get("whale_usd"):
        return None
    other = (row.get("opposite_before_usd") or 0.0) + (row.get("opposite_after_usd") or 0.0)
    if other <= 0:
        return CLEAN
    return HEDGED if other >= ratio * row["whale_usd"] else PARTIAL


@dataclass
class WalletFollowup:
    wallet: str
    alerts: int = 0                # followed up
    both_sides: int = 0            # bought the other side too, any amount
    hedged: int = 0                # ... at least hedge_ratio of the alert
    sold_back: int = 0             # sold back sold_back_fraction within 30 minutes

    def both_sides_flag(self, cfg: Any) -> bool:
        return self.alerts >= cfg.min_alerts and self.both_sides >= cfg.both_sides_share * self.alerts

    def sold_back_flag(self, cfg: Any) -> bool:
        return self.alerts >= cfg.min_alerts and self.sold_back >= cfg.sold_back_alert_share * self.alerts


def by_wallet(db: Any, cfg: Any, *, until: int | None = None, wallet: str | None = None) -> dict[str, WalletFollowup]:
    """Each wallet's followed-up alerts, followed up by ``until`` (default: all)."""
    sql = ("SELECT wallet, whale_usd, opposite_before_usd, opposite_after_usd, sold_back_share, followed_at "
           "FROM alerts WHERE followed_at IS NOT NULL")
    args: list[Any] = []
    if until is not None:
        sql += " AND followed_at <= ?"
        args.append(until)
    if wallet is not None:
        sql += " AND wallet = ?"
        args.append(wallet)
    out: dict[str, WalletFollowup] = defaultdict(lambda: WalletFollowup(""))
    for r in db.query(sql, args):
        w = out[r["wallet"]]
        w.wallet = r["wallet"]
        w.alerts += 1
        cls = hedge_class(dict(r), cfg.hedge_ratio)
        w.both_sides += cls in (PARTIAL, HEDGED)
        w.hedged += cls == HEDGED
        w.sold_back += (r["sold_back_share"] or 0.0) >= cfg.sold_back_fraction
    return dict(out)


def skip(app: Any, alert: Any) -> tuple[str, str] | None:
    """(skip code, reason) when a switch that is on says not to copy this alert. Uses
    only what was known when it fired: buys before it, and alerts followed up already."""
    cfg = app.cfg.followup
    if not (cfg.skip_hedged_alerts or cfg.skip_both_sides_wallets or cfg.skip_sold_back_wallets):
        return None
    db = app.db
    if cfg.skip_hedged_alerts:
        before = alert["opposite_before_usd"] if "opposite_before_usd" in alert.keys() else None
        if before is None:
            before = opposite_usd(db, alert, alert["first_fill_ts"] - BEFORE_SECONDS, alert["first_fill_ts"])
        if alert["whale_usd"] and before >= cfg.hedge_ratio * alert["whale_usd"]:
            return SKIP_HEDGED, (f"{SKIP_HEDGED}: the whale bought ${before:,.0f} of the other side in the 24 hours "
                                 f"before (at least {cfg.hedge_ratio:.0%} of this ${alert['whale_usd']:,.0f})")
    if cfg.skip_both_sides_wallets or cfg.skip_sold_back_wallets:
        w = by_wallet(db, cfg, until=int(app.clock()), wallet=alert["wallet"]).get(alert["wallet"])
        if w is not None and cfg.skip_both_sides_wallets and w.both_sides_flag(cfg):
            return SKIP_BOTH_SIDES, (f"{SKIP_BOTH_SIDES}: bought both sides on {w.both_sides} of its "
                                     f"{w.alerts} followed-up alerts")
        if w is not None and cfg.skip_sold_back_wallets and w.sold_back_flag(cfg):
            return SKIP_SOLD_BACK, (f"{SKIP_SOLD_BACK}: sold back within 30 minutes on {w.sold_back} of its "
                                    f"{w.alerts} followed-up alerts")
    return None
