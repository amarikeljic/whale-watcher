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

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import followup
from .app import App
from .bot import Bot
from .detector import Detector
from .paper import PaperEngine
from .polymarket import Activity, Book, MarketInfo
from .timeutil import iso
from .watchlist import membership_at

log = logging.getLogger(__name__)

# An alert that was made. RECORDED is the only status written today. POSTED and UNSENT
# are history from when alerts were also sent to a chat webhook (removed 2026-11-13);
# those rows, and their posted_at and discord_message_id columns, are kept as they are.
RECORDED = "RECORDED"
ALERTED = (RECORDED, "POSTED", "UNSENT")

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
    exits_marked: int = 0
    bot: dict[str, int] = field(default_factory=dict)


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


def bot_line(app: App, alert_id: int, runner: str = "single") -> str | None:
    """What the paper bot did with this signal (the flat_hold account), for the alert text."""
    t = app.db.one("SELECT * FROM bot_trades WHERE runner = ? AND strategy = 'flat_hold' AND alert_id = ?",
                   (runner, alert_id))
    if t is None:
        return None
    delay = t["decided_at"] - t["signal_ts"]
    if t["status"] == "FILLED":
        return (f"Paper bot: bought ${t['stake_usd']:g} at {t['fill_price']:.3f}, {delay:.0f}s after the whale")
    return f"Paper bot: {t['status'].lower()} ({t['reason']})"


def format_alert(app: App, alert, market: MarketInfo | None, *, run_id: int | None, lag: int | None,
                 bot_line: str | None = None) -> str:
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
    if bot_line:
        lines.append(bot_line)
    if alert["probation"]:
        lines.append("Watchlist: on probation (recently added; counted separately in the stats)")
    if lag is not None and lag > 120:
        lines.append(f"⚠ Polymarket data is running ~{lag}s behind the chain")
    lines.append("Paper-trading research, not advice.")
    return "\n".join(lines)


class Monitor:
    """The ``single`` runner: turns published fills of watchlist wallets into alerts
    (one per buy group that crosses the threshold), posts them, and hands each one to
    the paper bot the moment it is detected.

    ``embedded`` runs a detector inside this process (tests, and a plain ``monitor``
    with no detector container). With ``embedded=False`` this class only reads what
    the separate detector process published. ``paper`` without ``bot`` is the pre-v3 behaviour
    (one human-delay paper trade per alert), kept for the tests of that rule."""

    RUNNER = "single"

    def __init__(self, app: App, *, paper: PaperEngine | None = None, bot: Bot | None = None,
                 embedded: bool = True, max_alert_age_seconds: int | None = None, us: Any = None) -> None:
        self.app = app
        self.us = us                    # polymarket_us.UsRowA, or None: the same signals priced on Polymarket US
        self.paper = paper
        self.bot = bot
        self.embedded = embedded
        self.detector = Detector(app) if embedded else None
        self.cfg = app.cfg.alerts
        self.max_alert_age = max_alert_age_seconds or self.cfg.max_alert_age_seconds
        self._lag: int | None = None
        self._lag_at = 0.0

    @property
    def run_id(self) -> int | None:
        return self.app.db.get_state("monitored_run_id")

    @property
    def cursor_key(self) -> str:
        return f"runner_cursor:{self.RUNNER}"

    def wallets(self):
        return self.app.db.query("SELECT * FROM monitored_wallets WHERE active = 1 ORDER BY rank")

    async def _data_lag(self) -> int | None:
        """serving.lag_seconds from /v2/status, asked at most twice a minute."""
        now = self.app.wall()
        if now - self._lag_at >= 30 or self._lag_at == 0.0:
            self._lag_at = now
            try:
                status = await self.app.data.status()
                self._lag = (status.get("serving") or {}).get("lag_seconds")
            except Exception as exc:
                log.debug("status check failed: %s", exc)
        return self._lag

    def _new_events(self) -> list:
        cursor = int(self.app.db.get_state(self.cursor_key, 0))
        return self.app.db.query("SELECT * FROM fill_events WHERE id > ? ORDER BY id LIMIT 2000", (cursor,))

    async def poll_once(self, *, backstop: bool | None = True) -> PollStats:
        """One runner step: (detect, when the detector is embedded,) then act on every
        newly published fill, then the things that fall due with time."""
        app = self.app
        counter = int(app.db.get_state("poll_counter", 0)) + 1
        app.db.set_state("poll_counter", counter)
        stats = PollStats(counter)
        if self.paper is not None:
            stats.paper_filled = await self.paper.process_due(counter)
        stats.data_lag = await self._data_lag()
        if self.embedded:
            found = await self.detector.poll_once(backstop=backstop)
            stats.errors += found.errors
        watched = {w["wallet"] for w in self.wallets()}
        stats.wallets = len(watched)
        events = self._new_events()
        mine = [e for e in events if e["wallet"] in watched]
        stats.new_fills = len(mine)
        self._prune_small_groups()
        for e in sorted(mine, key=lambda e: (e["ts"], e["id"])):
            if e["side"] == "SELL":
                if self.bot is not None:
                    stats.exits_marked += self.bot.on_whale_sell(e["wallet"], e["token_id"] or "", e["size"] or 0.0,
                                                                 e["ts"])
                continue
            if e["side"] != "BUY":
                continue
            outcome = await self._handle_buy(_as_activity(e), counter, stats.data_lag, detected_at=e["detected_at"])
            if outcome == "alert":
                stats.alerts += 1
            elif outcome == "skipped":
                stats.skipped += 1
            elif outcome == "edit":
                stats.edits += 1
        if events:
            app.db.set_state(self.cursor_key, events[-1]["id"])
        if self.bot is not None:
            stats.bot = await self.bot.tick()
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

    def _group_for(self, fill: Activity):
        window = self.cfg.dedupe_window_seconds
        return self.app.db.one(
            "SELECT * FROM alerts WHERE wallet = ? AND token_id = ? AND ABS(first_fill_ts - ?) <= ? "
            "AND status != 'BACKTEST' ORDER BY first_fill_ts DESC LIMIT 1",
            (fill.wallet, fill.token_id, fill.ts, window))

    async def _handle_buy(self, fill: Activity, counter: int, lag: int | None, *,
                          detected_at: float | None = None) -> str:
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
                return await self._trigger(alert, fill, counter, lag, detected_at=detected_at)
            except Exception as exc:
                # The fill will not come round again, so record the alert rather than lose it.
                log.warning("alert %s failed: %s", alert_id, exc)
                app.db.execute(
                    "UPDATE alerts SET status = 'FAILED', skip_reason = ?, detected_at = ?, trigger_fill_ts = ?, "
                    "updated_at = ? WHERE id = ?", (f"error: {exc}"[:300], now, fill.ts, now, alert_id))
                return "failed"
        if alert["status"] in ALERTED:
            # More fills joined an alert already made: its text is brought up to date.
            market = app.markets.get(alert["condition_id"])
            message = format_alert(app, alert, market, run_id=alert["snapshot_run_id"], lag=lag,
                                   bot_line=bot_line(app, alert_id, self.RUNNER) if self.bot else None)
            app.db.execute("UPDATE alerts SET message = ? WHERE id = ?", (message, alert_id))
            return "edit"
        return "grouped"

    async def _trigger(self, alert, fill: Activity, counter: int, lag: int | None, *,
                       detected_at: float | None = None) -> str:
        """The group just crossed the threshold: decide, paper trade, record.

        Order matters for latency. The bot acts first, on the order book read at this
        instant; the slower thing a bot would not wait for (the whale's position
        lookup) comes after. Nothing is sent anywhere: an alert is a row in the
        database and a line in the log, read on the dashboard."""
        app = self.app
        now = app.clock()
        detected_at = float(now) if detected_at is None else detected_at
        # The market's details and its order book, asked for together: every tenth of
        # a second here is price the whale's followers are taking.
        markets, book = await asyncio.gather(
            app.markets.ensure([alert["condition_id"]], refresh_after=300),
            app.clob.book(alert["token_id"]), return_exceptions=True)
        if isinstance(markets, BaseException):
            raise markets
        market = markets.get(alert["condition_id"])
        if isinstance(book, BaseException):
            log.warning("book fetch failed for %s: %s", alert["token_id"], book)
            book = None
        decided_at = app.wall()
        ask = book.best_ask if book else None
        bid = book.best_bid if book else None
        reason = self._skip_reason(alert, fill, market, book, now)
        run_id = self.run_id
        # Which stay on the watchlist this alert belongs to and whether the wallet was
        # still on probation, frozen now so later knowledge cannot leak into the judging.
        member = membership_at(app, alert["wallet"], now)
        app.db.execute(
            "UPDATE alerts SET detected_at = ?, trigger_fill_ts = ?, alert_best_ask = ?, alert_best_bid = ?, "
            "alert_book_ts = ?, snapshot_run_id = ?, membership_id = ?, probation = ?, detect_delay = ?, "
            "updated_at = ? WHERE id = ?",
            (int(detected_at), fill.ts, ask, bid, book.ts if book else None, run_id,
             member["id"] if member else None,
             (1 if now < member["probation_until"] else 0) if member else None,
             detected_at - fill.ts, now, alert["id"]))
        # What the whale bought of the other side in the day before (Part 4 #11). Read
        # before the bot only when a switch needs it: otherwise it never delays a decision.
        early = app.cfg.followup.skip_hedged_alerts
        if early:
            self._follow(alert)
        alert = app.db.one("SELECT * FROM alerts WHERE id = ?", (alert["id"],))
        tradable = not reason and market is not None
        if tradable and self.bot is not None:
            self.bot.enter(alert, market, book, detected_at=detected_at, decided_at=decided_at)
            if self.us is not None:
                # After the bot, and in the background: a comparison never delays a decision.
                self.us.submit(alert, market, detected_at=detected_at)
        if not early:
            self._follow(alert)

        kind, held = await self._kind(alert)
        app.db.execute("UPDATE alerts SET kind = ?, whale_position = ? WHERE id = ?", (kind, held, alert["id"]))
        if self.bot is not None:
            self.bot.set_whale_position(alert["id"], held)
        alert = app.db.one("SELECT * FROM alerts WHERE id = ?", (alert["id"],))
        message = format_alert(app, alert, market, run_id=run_id, lag=lag,
                               bot_line=bot_line(app, alert["id"], self.RUNNER) if self.bot else None)
        if reason:
            app.db.execute("UPDATE alerts SET status = 'SKIPPED', skip_reason = ?, message = ? WHERE id = ?",
                           (reason, message, alert["id"]))
            log.info("alert %s skipped: %s", alert["id"], reason)
            return "skipped"
        app.db.execute("UPDATE alerts SET status = ?, message = ? WHERE id = ?", (RECORDED, message, alert["id"]))
        log.info("alert %s:\n%s", alert["id"], message)
        # Before v3 every alert got one human-delay paper trade. With the bot running
        # that rule lives on as the bot's human_* comparison columns instead.
        if self.bot is None and self.paper is not None and market is not None:
            self.paper.open(alert["id"], poll_counter=counter, market=market)
        return "alert"

    def _follow(self, alert) -> None:
        try:
            followup.at_alert_time(self.app.db, alert)
        except Exception:  # noqa: BLE001 - a measurement must never stop an alert
            log.exception("could not read the whale's other-side buys for alert %s", alert["id"])

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

    async def _kind(self, alert) -> tuple[str, float | None]:
        """(NEW or ADD, shares the wallet holds in this outcome now). NEW unless it
        already held the outcome before the group started."""
        try:
            rows = await self.app.data.positions(alert["wallet"], conditions=[alert["condition_id"]])
        except Exception:
            return "NEW", None
        held = sum(float(r.get("current_size") or 0) for r in rows if str(r.get("token_id")) == alert["token_id"])
        before = held - alert["whale_shares"]
        return ("ADD" if before > max(1.0, 0.01 * alert["whale_shares"]) else "NEW"), held


def _as_activity(e) -> Activity:
    return Activity(wallet=e["wallet"], ts=e["ts"], type="TRADE", condition_id=e["condition_id"],
                    token_id=e["token_id"] or "", outcome=e["outcome"] or "", outcome_index=e["outcome_index"],
                    side=e["side"] or "", size=e["size"] or 0.0, usdc_size=e["usdc_size"] or 0.0,
                    price=e["price"] or 0.0, tx_hash=e["tx_hash"] or "", fill_key=e["fill_key"])
