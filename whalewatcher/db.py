"""SQLite storage. All times are UTC epoch seconds (INTEGER); convert only for display.

Plain ``sqlite3`` rather than an async driver: the service writes a few rows a
minute, every statement takes well under a millisecond, and a synchronous
connection keeps the code and the tests simple.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 1: the original build. 2: v2 Phase 2, the persistent watchlist (watchlist_membership,
# watchlist_events, alerts.membership_id, alerts.probation). 3: v2 Phase 3, the bot
# model (fill_events, bot_trades, alerts.detect_delay, alerts.whale_position). 4: v2
# Phase 3b, consensus (consensus_signals, consensus_pool_runs, consensus_pool).
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address      TEXT PRIMARY KEY,
    name         TEXT,
    sources      TEXT,             -- JSON list: where the wallet entered the candidate pool
    first_seen   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- Activity rows from /v2/activity: TRADE, SPLIT, MERGE, REDEEM, CONVERSION.
-- Market text (title, slug) lives in poly_markets: repeating it on every fill
-- made this table ~400 bytes a row heavier.
CREATE TABLE IF NOT EXISTS wallet_trades (
    id            INTEGER PRIMARY KEY,
    wallet        TEXT NOT NULL,
    ts            INTEGER NOT NULL,   -- block timestamp of the fill
    type          TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    token_id      TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    side          TEXT,
    size          REAL,
    usdc_size     REAL,
    price         REAL,
    tx_hash       TEXT,
    fill_key      TEXT NOT NULL UNIQUE,   -- sha1 of the fill's fields (see polymarket.assign_fill_keys)
    seen_at       INTEGER NOT NULL    -- when this service first saw the row
);
CREATE INDEX IF NOT EXISTS wallet_trades_wallet_ts ON wallet_trades (wallet, ts);
CREATE INDEX IF NOT EXISTS wallet_trades_wallet_cond ON wallet_trades (wallet, condition_id);

-- Which time ranges of a wallet's activity are already stored, so a rescore
-- only fetches what is new.
CREATE TABLE IF NOT EXISTS wallet_fetches (
    wallet      TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    rows        INTEGER NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (wallet, start_ts, end_ts)
);

CREATE TABLE IF NOT EXISTS poly_markets (
    condition_id      TEXT PRIMARY KEY,
    question          TEXT,
    slug              TEXT,
    event_slug        TEXT,
    event_title       TEXT,
    category          TEXT,
    tags              TEXT,            -- JSON list of tag slugs
    outcomes          TEXT,            -- JSON list
    token_ids         TEXT,            -- JSON list, same order as outcomes
    end_date          INTEGER,
    closed            INTEGER,
    closed_time       INTEGER,
    game_start_time   INTEGER,
    sports_market_type TEXT,
    neg_risk          INTEGER,
    accepting_orders  INTEGER,
    fees_enabled      INTEGER,
    fee_rate          REAL,
    fee_exponent      REAL,
    fee_taker_only    INTEGER,
    description       TEXT,            -- resolution rules
    resolution_source TEXT,
    resolved          INTEGER NOT NULL DEFAULT 0,
    resolved_at       INTEGER,
    payouts           TEXT,            -- JSON list, per outcome index, dollars per share
    meta_fetched_at   INTEGER,
    resolution_checked_at INTEGER
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    token_id          TEXT NOT NULL,
    at_ts             INTEGER NOT NULL,  -- the instant asked about
    price             REAL,              -- NULL: asked, and there was no observation
    observed_ts       INTEGER,
    resolution_seconds INTEGER,
    source            TEXT NOT NULL,     -- 'prices-history-as-of', 'book-ask', ...
    fetched_at        INTEGER NOT NULL,
    PRIMARY KEY (token_id, at_ts, source)
);

CREATE TABLE IF NOT EXISTS score_runs (
    id             INTEGER PRIMARY KEY,
    as_of          INTEGER NOT NULL,
    kind           TEXT NOT NULL,        -- score | nightly | validate-selection
    created_at     INTEGER NOT NULL,
    config_json    TEXT NOT NULL,
    sources_json   TEXT NOT NULL,
    n_candidates   INTEGER NOT NULL,
    n_passed       INTEGER NOT NULL,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS wallet_score_snapshots (
    run_id                  INTEGER NOT NULL REFERENCES score_runs(id),
    wallet                  TEXT NOT NULL,
    as_of                   INTEGER NOT NULL,
    settled_markets         INTEGER,
    total_pnl               REAL,
    total_cost              REAL,
    roi                     REAL,
    profitable_months       INTEGER,
    months_in_window        INTEGER,
    max_single_market_share REAL,
    avg_stake_usd           REAL,
    days_since_last_trade   REAL,
    two_sided_ratio         REAL,
    clv                     REAL,
    clv_n                   INTEGER,
    clv_markout             REAL,
    clv_markout_n           INTEGER,
    win_rate                REAL,
    unreliable_markets      INTEGER,
    activity_rows           INTEGER,
    passed                  INTEGER NOT NULL,
    fail_reasons            TEXT,         -- JSON list
    rank                    INTEGER,
    selected                INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, wallet)
);

CREATE TABLE IF NOT EXISTS monitored_wallets (
    wallet        TEXT PRIMARY KEY,
    run_id        INTEGER NOT NULL,      -- snapshot that put it here
    rank          INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    added_at      INTEGER NOT NULL,
    removed_at    INTEGER,
    last_polled_at INTEGER,
    last_seen_ts  INTEGER                -- newest fill timestamp seen
);

-- One row per stay on the watchlist. A wallet that is removed and later qualifies
-- again gets a NEW row. Rows are never deleted (the triggers below refuse it), so
-- what the list looked like on any past night can always be rebuilt.
CREATE TABLE IF NOT EXISTS watchlist_membership (
    id                 INTEGER PRIMARY KEY,
    wallet             TEXT NOT NULL,
    added_at           INTEGER NOT NULL,
    add_reason         TEXT NOT NULL,
    added_run_id       INTEGER,           -- score snapshot in force at the add
    probation_until    INTEGER NOT NULL,
    removed_at         INTEGER,           -- NULL while on the list
    remove_reason      TEXT,
    removed_run_id     INTEGER,           -- score snapshot in force at the removal
    fail_streak        INTEGER NOT NULL DEFAULT 0,  -- failed nightly reviews in a row
    last_review_at     INTEGER,
    last_review_run_id INTEGER,
    last_review        TEXT               -- JSON list: exit rules failed at the last review ([] = passed)
);
CREATE UNIQUE INDEX IF NOT EXISTS watchlist_one_open ON watchlist_membership (wallet) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS watchlist_wallet ON watchlist_membership (wallet, added_at);

-- What each nightly review decided about each wallet, and why.
CREATE TABLE IF NOT EXISTS watchlist_events (
    id            INTEGER PRIMARY KEY,
    at            INTEGER NOT NULL,
    run_id        INTEGER,
    wallet        TEXT NOT NULL,
    membership_id INTEGER,
    action        TEXT NOT NULL,          -- ADD | KEEP | FAIL | REMOVE | WAIT | SKIP
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS watchlist_events_run ON watchlist_events (run_id);

CREATE TRIGGER IF NOT EXISTS watchlist_membership_keep BEFORE DELETE ON watchlist_membership
BEGIN SELECT RAISE(ABORT, 'watchlist history is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS watchlist_events_keep BEFORE DELETE ON watchlist_events
BEGIN SELECT RAISE(ABORT, 'watchlist history is never deleted'); END;

-- A group of one wallet's buys of one outcome token inside the dedupe window.
CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY,
    wallet           TEXT NOT NULL,
    condition_id     TEXT NOT NULL,
    token_id         TEXT NOT NULL,
    outcome          TEXT,
    kind             TEXT NOT NULL,       -- NEW | ADD
    first_fill_ts    INTEGER NOT NULL,    -- whale trade time (first fill of the group)
    last_fill_ts     INTEGER NOT NULL,
    trigger_fill_ts  INTEGER,             -- the fill that took the group over the threshold
    whale_shares     REAL NOT NULL,
    whale_usd        REAL NOT NULL,
    whale_price      REAL NOT NULL,       -- VWAP of the group's fills
    fills            INTEGER NOT NULL,
    detected_at      INTEGER NOT NULL,    -- when this service saw the fill that crossed the threshold
    posted_at        INTEGER,             -- when the webhook accepted it (NULL if skipped / not posted)
    status           TEXT NOT NULL,       -- POSTED | SKIPPED | UNSENT (no webhook) | FAILED
    skip_reason      TEXT,
    alert_best_ask   REAL,
    alert_best_bid   REAL,
    alert_book_ts    INTEGER,
    snapshot_run_id  INTEGER,             -- score run current when the alert fired
    discord_message_id TEXT,
    message          TEXT,
    updated_at       INTEGER NOT NULL,
    membership_id    INTEGER,             -- watchlist_membership row the wallet was on when the alert fired
    probation        INTEGER              -- 1 if that membership was still in probation at the time
);
CREATE INDEX IF NOT EXISTS alerts_group ON alerts (wallet, token_id, first_fill_ts);

CREATE TABLE IF NOT EXISTS alert_fills (
    alert_id   INTEGER NOT NULL REFERENCES alerts(id),
    fill_key   TEXT NOT NULL,
    PRIMARY KEY (alert_id, fill_key)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id               INTEGER PRIMARY KEY,
    alert_id         INTEGER NOT NULL REFERENCES alerts(id),
    venue            TEXT NOT NULL,       -- polymarket
    strategy         TEXT NOT NULL,       -- hold_to_resolution
    status           TEXT NOT NULL,       -- PENDING | FILLED | MISSED | SETTLED
    miss_reason      TEXT,
    token_id         TEXT NOT NULL,
    condition_id     TEXT NOT NULL,
    stake_usd        REAL NOT NULL,
    due_poll         INTEGER NOT NULL,    -- poll counter value at which to fill
    alert_ts         INTEGER NOT NULL,
    alert_ask        REAL,
    fill_ts          INTEGER,
    fill_price       REAL,                -- VWAP of the walk, before fees
    contracts        REAL,
    fees             REAL,
    cost             REAL,                -- fill_price * contracts + fees
    levels_walked    INTEGER,
    -- shadow fills, hypothetical, never part of the headline numbers
    zero_price       REAL, zero_contracts REAL, zero_fees REAL, zero_cost REAL,
    atask_price      REAL, atask_contracts REAL, atask_fees REAL, atask_cost REAL,
    -- settlement
    settled_at       INTEGER,
    settle_result    TEXT,                -- WIN | LOSS | SPLIT | REFUND
    payout_per_share REAL,
    payout           REAL,
    pnl              REAL,
    zero_pnl         REAL,
    atask_pnl        REAL,
    closing_price    REAL,                -- for CLV of the paper fill
    updated_at       INTEGER NOT NULL,
    UNIQUE (alert_id, venue, strategy)
);

-- What the detector publishes: every fill of a tracked wallet, once, with the moment
-- it was first seen. Runners read this table in id order and keep their own cursor,
-- so they all see the same fills with the same timing and no API is asked twice.
CREATE TABLE IF NOT EXISTS fill_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_key      TEXT NOT NULL UNIQUE,   -- same key as wallet_trades.fill_key
    wallet        TEXT NOT NULL,
    ts            INTEGER NOT NULL,       -- block timestamp of the fill
    condition_id  TEXT NOT NULL,
    token_id      TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    side          TEXT,
    size          REAL,
    usdc_size     REAL,
    price         REAL,
    tx_hash       TEXT,
    detected_at   REAL NOT NULL,          -- wall clock when the detector first saw it (fractional seconds)
    source        TEXT NOT NULL           -- feed (global trade feed) | backstop (per-wallet activity)
);
CREATE INDEX IF NOT EXISTS fill_events_wallet ON fill_events (wallet, token_id, ts);

-- The bot model's ledger: one row per signal per strategy, whatever happened to it.
-- Each (runner, strategy) is its own paper account: bankroll, exposure and P&L are
-- all computed from its rows alone.
CREATE TABLE IF NOT EXISTS bot_trades (
    id               INTEGER PRIMARY KEY,
    runner           TEXT NOT NULL,       -- single | consensus
    strategy         TEXT NOT NULL,       -- flat_hold | flat_mirror | scaled_hold | placebo_late
    alert_id         INTEGER REFERENCES alerts(id),
    wallet           TEXT,
    condition_id     TEXT NOT NULL,
    token_id         TEXT NOT NULL,
    event_key        TEXT NOT NULL,       -- event slug: several markets of one game are one risk
    status           TEXT NOT NULL,       -- PENDING (placebo, waiting) | FILLED | MISSED | SKIPPED
    reason           TEXT,                -- why MISSED (the book) or SKIPPED (a risk limit)
    signal_ts        INTEGER NOT NULL,    -- block time of the whale fill that made the signal
    detected_at      REAL NOT NULL,       -- when the detector saw that fill
    decided_at       REAL NOT NULL,       -- when the order book was read and the decision made
    due_at           REAL,                -- placebo only: when it enters
    stake_usd        REAL NOT NULL,
    whale_price      REAL,
    best_ask         REAL,
    fill_price       REAL,                -- VWAP of the walk, before fees
    contracts        REAL,
    fees             REAL,
    cost             REAL,                -- fill_price * contracts + fees
    levels           INTEGER,
    -- comparison columns, same stake, never part of the headline
    zero_price       REAL, zero_contracts REAL, zero_cost REAL, zero_pnl REAL,      -- at the whale's own price
    human_due        REAL, human_done INTEGER NOT NULL DEFAULT 0,                   -- the old rule: a book walk
    human_price      REAL, human_contracts REAL, human_cost REAL, human_pnl REAL,  -- human_delay_seconds later
    -- mirror exits
    whale_shares     REAL,                -- what the whale held in this outcome when we entered
    whale_sold       REAL NOT NULL DEFAULT 0,   -- what it has sold since
    exit_due         INTEGER NOT NULL DEFAULT 0,
    exit_tried_at    REAL,
    exit_at          REAL,
    exit_price       REAL,
    exit_fees        REAL,
    proceeds         REAL,
    -- the end of the trade: an exit or the market's resolution
    closed_at        INTEGER,
    result           TEXT,                -- WIN | LOSS | SPLIT | EXIT
    payout_per_share REAL,
    pnl              REAL,
    closing_price    REAL,                -- closing line (pre-game fills only)
    markout_price    REAL,                -- price markout_seconds after our fill (every fill)
    markout_done     INTEGER NOT NULL DEFAULT 0,
    updated_at       INTEGER NOT NULL,
    UNIQUE (runner, strategy, alert_id)
);
CREATE INDEX IF NOT EXISTS bot_trades_open ON bot_trades (runner, strategy, closed_at);

-- Consensus (v2 Phase 3b). One row per signal: who agreed, when, at what price, and
-- how far the price had moved since the first of them entered.
CREATE TABLE IF NOT EXISTS consensus_signals (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,      -- backtest:<pool> | live
    condition_id      TEXT NOT NULL,
    token_id          TEXT NOT NULL,
    outcome           TEXT,
    signal_ts         INTEGER NOT NULL,   -- block time of the fill that completed the consensus
    trigger_wallet    TEXT,
    trigger_price     REAL,               -- the market price when the signal fired
    entities          INTEGER NOT NULL,
    opposing          INTEGER NOT NULL DEFAULT 0,
    first_entry_ts    INTEGER,
    first_entry_price REAL,
    runup             REAL,               -- trigger_price - first_entry_price
    details           TEXT,               -- JSON: [{entity, entry_ts, entry_price, usd}]
    status            TEXT,               -- FILLED | MISSED | UNPRICED
    miss_reason       TEXT,
    fill_ts           INTEGER,
    fill_price        REAL,
    cost              REAL,
    pnl               REAL,
    payout_per_share  REAL,
    settled_at        INTEGER,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS consensus_signals_source ON consensus_signals (source, signal_ts);

-- The consensus pool as it stood at each refresh, wallet by wallet, with why.
CREATE TABLE IF NOT EXISTS consensus_pool_runs (
    id          INTEGER PRIMARY KEY,
    as_of       INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    rules_json  TEXT NOT NULL,
    counts_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consensus_pool (
    run_id          INTEGER NOT NULL REFERENCES consensus_pool_runs(id),
    wallet          TEXT NOT NULL,
    name            TEXT,
    entity          TEXT,                 -- wallets sharing this are counted as one
    rank            INTEGER,              -- on the 30-day profit leaderboard
    profit_30d      REAL,
    trades_per_day  REAL,
    two_sided_ratio REAL,
    passed          INTEGER NOT NULL,     -- the activity rules (the profit bar is applied on top)
    reasons         TEXT,                 -- JSON list
    PRIMARY KEY (run_id, wallet)
);

CREATE TABLE IF NOT EXISTS service_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Kalshi public market data, cached for the backtest's cross-venue copies.
CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker               TEXT PRIMARY KEY,
    event_ticker         TEXT NOT NULL,
    series_ticker        TEXT NOT NULL,
    name                 TEXT,             -- yes_sub_title
    status               TEXT,
    result               TEXT,
    settlement_value     REAL,             -- what one YES contract paid
    open_time            INTEGER,
    close_time           INTEGER,
    expected_expiration  INTEGER,
    settled_at           INTEGER,
    archived             INTEGER NOT NULL, -- settled before the historical cutoff
    fetched_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS kalshi_markets_series ON kalshi_markets (series_ticker);

CREATE TABLE IF NOT EXISTS kalshi_series (
    ticker          TEXT PRIMARY KEY,
    fee_type        TEXT,
    fee_multiplier  REAL,
    markets_fetched_at INTEGER,            -- when the series' market list was last read
    fetched_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_quotes (
    ticker   TEXT NOT NULL,
    ts       INTEGER NOT NULL,             -- end of the one-minute candle
    yes_bid  REAL,
    yes_ask  REAL,
    PRIMARY KEY (ticker, ts)
);

-- The fresh-wallet test (fresh.py): large fills per market and each wallet's first trade.
CREATE TABLE IF NOT EXISTS market_fills (
    condition_id TEXT NOT NULL,
    wallet       TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    side         TEXT NOT NULL,
    usdc         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS market_fills_cond ON market_fills (condition_id);

CREATE TABLE IF NOT EXISTS market_fill_fetches (
    condition_id TEXT PRIMARY KEY,
    min_usd      REAL NOT NULL,
    rows         INTEGER NOT NULL,
    truncated    INTEGER NOT NULL,
    fetched_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_first_trades (
    wallet      TEXT PRIMARY KEY,
    first_ts    INTEGER,               -- NULL: no trade found when checked
    checked_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_quote_windows (
    ticker   TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    PRIMARY KEY (ticker, start_ts, end_ts)
);
"""


# Columns added to existing tables after version 1. CREATE TABLE IF NOT EXISTS leaves
# an existing table as it is, so these are added one by one; existing rows keep
# every value they had and get NULL in the new column.
ADDED_COLUMNS = (
    (2, "alerts", "membership_id", "INTEGER"),
    (2, "alerts", "probation", "INTEGER"),
    (3, "alerts", "detect_delay", "REAL"),      # detector saw the triggering fill this long after its block
    (3, "alerts", "whale_position", "REAL"),    # shares the whale held in the outcome when the alert fired
)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.backup_path: str | None = None     # set when opening took a pre-migration backup
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA busy_timeout = 5000")
        # Several processes (detector, runners, a CLI command) can start at once. A file
        # lock makes exactly one of them back up and migrate; the others wait and then
        # find the database already current.
        with self._migration_lock():
            version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            has_data = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone() is not None
            if has_data and version < SCHEMA_VERSION and path != ":memory:":
                # Ground rule: back the database up before any migration. A database that
                # cannot be backed up is not migrated.
                self.backup_path = self.backup(f"pre-v{SCHEMA_VERSION}")
                log.warning("database is at schema v%d; backed up to %s before migrating to v%d",
                            version, self.backup_path, SCHEMA_VERSION)
            if version < SCHEMA_VERSION:
                self.conn.executescript(SCHEMA)
                self._add_columns()
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        if self.path == ":memory:":
            yield
            return
        import fcntl
        with open(f"{self.path}.migrate.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _add_columns(self) -> None:
        for _version, table, column, decl in ADDED_COLUMNS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def backup(self, label: str) -> str:
        """Copy the whole database to ``<db dir>/backups/<name>.<UTC time>.<label>`` with
        SQLite's online backup API (https://www.sqlite.org/backup.html), which is safe
        while the file is in use and includes what is still in the write-ahead log; a
        plain ``cp`` of a WAL database can miss recent writes."""
        src = Path(self.path)
        folder = src.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        need = src.stat().st_size
        free = shutil.disk_usage(folder).free
        if free < need * 1.05:
            raise RuntimeError(
                f"not enough disk space to back up {src} before migrating: need {need / 1e9:.2f} GB, "
                f"{free / 1e9:.2f} GB free in {folder}. Free some space and start again; nothing was changed.")
        dest = folder / f"{src.name}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.{label}"
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return str(dest)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # -- service state ------------------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM service_state WHERE key = ?", (key,))
        return default if row is None else json.loads(row["value"])

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO service_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
