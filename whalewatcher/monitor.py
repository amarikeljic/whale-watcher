"""Phase 2: poll monitored wallets, group their buys, alert, and hand alerts to paper trading.

Grouping without adding latency: the spec groups a wallet's fills on one market
within ``dedupe_window_seconds`` into one alert. Waiting for the window to end
before posting would add up to five minutes to every alert, which is the very
delay Phase 3 measures. So the alert goes out the moment a group's total
crosses ``min_whale_trade_usd``, and later fills in the same window edit that
message instead of posting a new one. One group, one message, no duplicates.

Groups are per (wallet, outcome token): buying YES and NO on one market inside
five minutes is two different bets, and merging them would hide exactly the
two-sided behaviour the scoring screens out.

Only BUY fills open groups. SELL fills are stored for mirror exits (Phase 3b).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .app import App
from .discord import NullSink, Sink
from .paper import PaperEngine
from .polymarket import Activity, Book, MarketInfo, assign_fill_keys
from .timeutil import iso
from .watchlist import membership_at

log = logging.getLogger(__name__)

DATA_HOST = "data-api.polymarket.com"


@dataclass
class PollStats:
    counter: int
    wallets: int = 0
    new_fills: int = 0
    alerts: int = 0
    skipped: int = 0
    edits: int = 0
    errors: int = 0
    data_lag: int | None = None
    paper_filled: list[int] = field(default_factory=list)


def short(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-3:]}"


def outcome_label(outcome: str) -> str:
    return outcome.upper() if outcome.lower() in ("yes", "no") else outcome


def score_line(app: App, wallet: str, run_id: int | None) -> str:
    if run_id is None:
        return "Wallet score: n/a"
    r = app.db.one("SELECT * FROM wallet_score_snapshots WHERE run_id = ? AND wallet = ?", (run_id, wallet))
    if r is None:
        return "Wallet score: n/a"
    if not r["settled_markets"]:
        reasons = json.loads(r["fail_reasons"] or "[]")
        why = "too active to score" if any("too_active" in x for x in reasons) else "no settled markets"
        return f"Wallet score: not scored ({why})  (snapshot #{run_id})"
    clv = "n/a" if r["clv"] is None else f"{r['clv']:+.2f}"
    roi = "n/a" if r["roi"] is None else f"{r['roi']:.0%}"
    return (f"Wallet score: CLV {clv} | ROI {roi} | {r['settled_markets']} settled | "
            f"{r['profitable_months']}/{r['months_in_window']} profitable months  (snapshot #{run_id})")


def format_alert(app: App, alert, market: MarketInfo | None, *, run_id: int | None, lag: int | None) -> str:
    kind = "opened a position" if alert["kind"] == "NEW" else "added to a position"
    question = market.question if market else alert["condition_id"]
    ask = "n/a" if alert["alert_best_ask"] is None else f"{alert['alert_best_ask']:.3f}"
    fills = f" ({alert['fills']} fills)" if alert["fills"] > 1 else ""
    lines = [
        f"🐋 WALLET {short(alert['wallet'])} bought {outcome_label(alert['outcome'] or '?')} ({kind})",
        f"Market: {question}",
        f"Whale price: {alert['whale_price']:.3f}   Current best ask: {ask}",
        f"Whale size: ${alert['whale_usd']:,.0f}{fills}",
        score_line(app, alert["wallet"], run_id),
        f"Whale trade: {iso(alert['first_fill_ts'])}   detected: {iso(alert['detected_at'])}",
        f"Polymarket: {market.url if market else 'https://polymarket.com'}",
    ]
    if alert["probation"]:
        lines.append("Watchlist: on probation (recently added; counted separately in the stats)")
    if lag is not None and lag > 120:
        lines.append(f"⚠ Polymarket data is running ~{lag}s behind the chain")
    lines.append("Paper-trading research, not advice.")
    return "\n".join(lines)


class Monitor:
    def __init__(self, app: App, sink: Sink, *, paper: PaperEngine | None = None,
                 max_alert_age_seconds: int | None = None) -> None:
        self.app = app
        self.sink = sink
        self.paper = paper
        self.cfg = app.cfg.alerts
        self.max_alert_age = max_alert_age_seconds or self.cfg.max_alert_age_seconds

    @property
    def run_id(self) -> int | None:
        return self.app.db.get_state("monitored_run_id")

    def wallets(self):
        return self.app.db.query("SELECT * FROM monitored_wallets WHERE active = 1 ORDER BY rank")

    async def poll_once(self) -> PollStats:
        app = self.app
        counter = int(app.db.get_state("poll_counter", 0)) + 1
        app.db.set_state("poll_counter", counter)
        stats = PollStats(counter)
        if self.paper is not None:
            stats.paper_filled = await self.paper.process_due(counter)
        try:
            status = await app.data.status()
            stats.data_lag = (status.get("serving") or {}).get("lag_seconds")
        except Exception as exc:
            log.debug("status check failed: %s", exc)

        buys: list[Activity] = []
        wallets = self.wallets()
        stats.wallets = len(wallets)
        for w in wallets:
            try:
                new = await self._poll_wallet(w)
            except Exception as exc:  # degrade: skip this wallet this cycle
                stats.errors += 1
                log.warning("poll failed for %s: %s", w["wallet"], exc)
                continue
            stats.new_fills += len(new)
            buys.extend(r for r in new if r.side == "BUY")
        self._prune_small_groups()
        for fill in sorted(buys, key=lambda r: r.ts):
            outcome = await self._handle_buy(fill, counter, stats.data_lag)
            if outcome == "alert":
                stats.alerts += 1
            elif outcome == "skipped":
                stats.skipped += 1
            elif outcome == "edit":
                stats.edits += 1
        return stats

    def _prune_small_groups(self) -> None:
        """Drop groups that never reached the threshold once their window has closed.

        They are not alerts (the spec ignores trades below min_whale_trade_usd),
        and an active wallet leaves hundreds of them an hour. Their fills stay in
        wallet_trades.
        """
        cutoff = self.app.clock() - 2 * self.cfg.dedupe_window_seconds
        with self.app.db.tx() as conn:
            conn.execute("DELETE FROM alert_fills WHERE alert_id IN "
                         "(SELECT id FROM alerts WHERE status = 'BELOW_THRESHOLD' AND first_fill_ts < ?)", (cutoff,))
            conn.execute("DELETE FROM alerts WHERE status = 'BELOW_THRESHOLD' AND first_fill_ts < ?", (cutoff,))

    async def _poll_wallet(self, w) -> list[Activity]:
        app = self.app
        now = app.clock()
        overlap = max(self.cfg.dedupe_window_seconds, 3 * self.cfg.poll_interval_seconds)
        start = (w["last_seen_ts"] or now - 2 * self.cfg.poll_interval_seconds) - overlap
        rows, _ = await app.data.activity(w["wallet"], types=("TRADE",), start=start, end=now + 60,
                                          max_rows=3000)
        assign_fill_keys(rows)
        new: list[Activity] = []
        for r in rows:
            cur = app.db.execute(
                "INSERT OR IGNORE INTO wallet_trades (wallet, ts, type, condition_id, token_id, outcome, "
                "outcome_index, side, size, usdc_size, price, tx_hash, fill_key, seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.wallet, r.ts, r.type, r.condition_id, r.token_id, r.outcome, r.outcome_index, r.side,
                 r.size, r.usdc_size, r.price, r.tx_hash, r.fill_key, now))
            if cur.rowcount == 1:
                new.append(r)
        newest = max([r.ts for r in rows], default=None)
        app.db.execute(
            "UPDATE monitored_wallets SET last_polled_at = ?, last_seen_ts = MAX(COALESCE(last_seen_ts, 0), ?) "
            "WHERE wallet = ?", (now, newest or (w["last_seen_ts"] or now - 2 * self.cfg.poll_interval_seconds),
                                 w["wallet"]))
        return new

    def _group_for(self, fill: Activity):
        window = self.cfg.dedupe_window_seconds
        return self.app.db.one(
            "SELECT * FROM alerts WHERE wallet = ? AND token_id = ? AND ABS(first_fill_ts - ?) <= ? "
            "AND status != 'BACKTEST' ORDER BY first_fill_ts DESC LIMIT 1",
            (fill.wallet, fill.token_id, fill.ts, window))

    async def _handle_buy(self, fill: Activity, counter: int, lag: int | None) -> str:
        app = self.app
        now = app.clock()
        group = self._group_for(fill)
        if group is None:
            cur = app.db.execute(
                "INSERT INTO alerts (wallet, condition_id, token_id, outcome, kind, first_fill_ts, last_fill_ts, "
                "whale_shares, whale_usd, whale_price, fills, detected_at, status, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill.wallet, fill.condition_id, fill.token_id, fill.outcome, "NEW", fill.ts, fill.ts, fill.size,
                 fill.usdc_size, fill.price, 1, now, "BELOW_THRESHOLD", now))
            alert_id = int(cur.lastrowid)
            app.db.execute("INSERT INTO alert_fills (alert_id, fill_key) VALUES (?, ?)", (alert_id, fill.fill_key))
        else:
            alert_id = group["id"]
            exists = app.db.one("SELECT 1 FROM alert_fills WHERE alert_id = ? AND fill_key = ?",
                                (alert_id, fill.fill_key))
            if exists:
                return "dup"
            shares = group["whale_shares"] + fill.size
            usd = group["whale_usd"] + fill.usdc_size
            # The whale's price is the VWAP of the fill prices. usdc_size includes the taker
            # fee on taker fills (checked 2026-11-10 on 263k fills in fee markets), so
            # usd / shares would put the whale's fee into its price, and the zero-latency
            # shadow would then pay a fee twice.
            price = (group["whale_price"] * group["whale_shares"] + fill.price * fill.size) / shares if shares \
                else fill.price
            app.db.execute(
                "UPDATE alerts SET whale_shares = ?, whale_usd = ?, whale_price = ?, fills = fills + 1, "
                "first_fill_ts = MIN(first_fill_ts, ?), last_fill_ts = MAX(last_fill_ts, ?), updated_at = ? "
                "WHERE id = ?", (shares, usd, price, fill.ts, fill.ts, now, alert_id))
            app.db.execute("INSERT INTO alert_fills (alert_id, fill_key) VALUES (?, ?)", (alert_id, fill.fill_key))

        alert = app.db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if alert["status"] == "BELOW_THRESHOLD":
            if alert["whale_usd"] < self.cfg.min_whale_trade_usd:
                return "below"
            try:
                return await self._trigger(alert, fill, counter, lag)
            except Exception as exc:
                # The fill will not come round again, so record the alert rather than lose it.
                log.warning("alert %s failed: %s", alert_id, exc)
                app.db.execute(
                    "UPDATE alerts SET status = 'FAILED', skip_reason = ?, detected_at = ?, trigger_fill_ts = ?, "
                    "updated_at = ? WHERE id = ?", (f"error: {exc}"[:300], now, fill.ts, now, alert_id))
                return "failed"
        if alert["status"] == "POSTED" and alert["discord_message_id"]:
            market = app.markets.get(alert["condition_id"])
            message = format_alert(app, alert, market, run_id=alert["snapshot_run_id"], lag=lag)
            if await self.sink.edit(alert["discord_message_id"], message):
                app.db.execute("UPDATE alerts SET message = ? WHERE id = ?", (message, alert_id))
            return "edit"
        return "grouped"

    async def _trigger(self, alert, fill: Activity, counter: int, lag: int | None) -> str:
        """The group just crossed the threshold: decide, record, post, paper trade."""
        app = self.app
        now = app.clock()
        markets = await app.markets.ensure([alert["condition_id"]], refresh_after=300)
        market = markets.get(alert["condition_id"])
        book: Book | None = None
        try:
            book = await app.clob.book(alert["token_id"])
        except Exception as exc:
            log.warning("book fetch failed for %s: %s", alert["token_id"], exc)
        kind = await self._kind(alert)
        ask = book.best_ask if book else None
        bid = book.best_bid if book else None
        reason = self._skip_reason(alert, fill, market, book, now)
        run_id = self.run_id
        # Which stay on the watchlist this alert belongs to and whether the wallet was
        # still on probation, frozen now so later knowledge cannot leak into the judging.
        member = membership_at(app, alert["wallet"], now)
        app.db.execute(
            "UPDATE alerts SET kind = ?, detected_at = ?, trigger_fill_ts = ?, alert_best_ask = ?, alert_best_bid = ?, "
            "alert_book_ts = ?, snapshot_run_id = ?, membership_id = ?, probation = ?, updated_at = ? WHERE id = ?",
            (kind, now, fill.ts, ask, bid, book.ts if book else None, run_id,
             member["id"] if member else None,
             (1 if now < member["probation_until"] else 0) if member else None, now, alert["id"]))
        alert = app.db.one("SELECT * FROM alerts WHERE id = ?", (alert["id"],))
        message = format_alert(app, alert, market, run_id=run_id, lag=lag)
        if reason:
            app.db.execute("UPDATE alerts SET status = 'SKIPPED', skip_reason = ?, message = ? WHERE id = ?",
                           (reason, message, alert["id"]))
            log.info("alert %s skipped: %s", alert["id"], reason)
            return "skipped"
        message_id = await self.sink.post(message)
        posted_at = app.clock()
        if message_id:
            status = "POSTED"
        elif isinstance(self.sink, NullSink):
            status, posted_at = "UNSENT", None
        else:
            status, posted_at = "FAILED", None
        app.db.execute("UPDATE alerts SET status = ?, posted_at = ?, discord_message_id = ?, message = ? WHERE id = ?",
                       (status, posted_at, message_id, message, alert["id"]))
        log.info("alert %s %s:\n%s", alert["id"], status, message)
        if status in ("POSTED", "UNSENT") and self.paper is not None and market is not None:
            self.paper.open(alert["id"], poll_counter=counter, market=market)
        return "alert" if status != "FAILED" else "failed"

    def _skip_reason(self, alert, fill: Activity, market: MarketInfo | None, book: Book | None, now: int) -> str | None:
        cfg = self.cfg
        if now - fill.ts > self.max_alert_age:
            return f"stale: the fill that crossed the threshold is {now - fill.ts}s old"
        if market is None:
            return "market metadata unavailable"
        if market.closed or market.resolved or market.accepting_orders is False:
            return "market closed"
        if book is None:
            return "no order book"
        price = book.best_ask if book.best_ask is not None else alert["whale_price"]
        if book.best_ask is None:
            return "no asks on the book"
        if not cfg.min_price <= price <= cfg.max_price:
            return f"price {price:.3f} outside {cfg.min_price:g}-{cfg.max_price:g}"
        return None

    async def _kind(self, alert) -> str:
        """NEW unless the wallet already held this outcome before the group started."""
        try:
            rows = await self.app.data.positions(alert["wallet"], conditions=[alert["condition_id"]])
        except Exception:
            rows = []
        held = sum(float(r.get("current_size") or 0) for r in rows if str(r.get("token_id")) == alert["token_id"])
        before = held - alert["whale_shares"]
        return "ADD" if before > max(1.0, 0.01 * alert["whale_shares"]) else "NEW"
