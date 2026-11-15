"""scripts/replay_slices.py: the replay cut by things known at the signal (read-only)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from conftest import DAY, NOW
from dashfix import A, B, Fixture

from whalewatcher.db import Database

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_slices.py"
spec = importlib.util.spec_from_file_location("replay_slices", SCRIPT)
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)


def read_only(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_an_empty_database_says_what_to_run(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    Database(path).close()
    assert rs.report(read_only(path))[0].startswith("no settled replay trades")


def test_each_signal_counts_once_and_is_cut_by_what_was_known_then(tmp_path):
    path = str(tmp_path / "ww.sqlite3")
    db = Database(path)
    fx = Fixture(db, NOW)
    fx.replay(A, 5, pnl=10.0)                                       # filled, won
    fx.replay(B, 6, pnl=-10.0, strategy="backtest:walk-forward")    # filled, lost
    fx.replay(B, 7, pnl=0.0)
    # The same buy group seen by both replays is one signal; give the pair their numbers.
    first = db.one("SELECT a.wallet, a.token_id, a.first_fill_ts, a.condition_id FROM alerts a ORDER BY a.id LIMIT 1")
    db.execute("INSERT INTO alerts (wallet, condition_id, token_id, kind, first_fill_ts, last_fill_ts, trigger_fill_ts, "
               "whale_shares, whale_usd, whale_price, fills, detected_at, status, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (first["wallet"], first["condition_id"], first["token_id"], "NEW", first["first_fill_ts"],
                first["first_fill_ts"], first["first_fill_ts"], 1000, 600, 0.5, 1, NOW, "BACKTEST", NOW))
    dup = db.one("SELECT MAX(id) id FROM alerts")["id"]
    db.execute("INSERT INTO paper_trades (alert_id, venue, strategy, status, token_id, condition_id, stake_usd, due_poll, "
               "alert_ts, fill_ts, fill_price, contracts, fees, cost, settled_at, pnl, updated_at) "
               "VALUES (?, 'polymarket', 'backtest:walk-forward', 'FILLED', ?, ?, 10, 1, ?, ?, 0.5, 20, 0, 10, ?, 10, ?)",
               (dup, first["token_id"], first["condition_id"], NOW - 5 * DAY, NOW - 5 * DAY + 90, NOW, NOW))
    db.execute("UPDATE paper_trades SET zero_cost = 10, zero_fees = 0.1, zero_pnl = pnl, "
               "payout_per_share = CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END")
    db.execute("UPDATE paper_trades SET status = 'MISSED', cost = NULL WHERE pnl = 0")
    db.close()
    conn = read_only(path)
    assert len(rs.load(conn)) == 3, "four replay rows, three distinct signals"
    text = "\n".join(rs.report(conn, samples=50))
    assert "3 distinct replay signals, 2 wallets" in text and "1.0% of the stake" in text
    for title in ("IN-PLAY OR NOT", "DID THE PRICE RUN?", "SCHEDULED TIME TO CLOSE", "PRICE THE WHALE PAID",
                  "SIZE OF THE WHALE'S BUY", "HEDGED?"):
        assert title in text
    assert "the price ran 3c+ within 90 s (missed)" in text and "no other-side buying before" in text


def test_it_opens_the_database_read_only(tmp_path, capsys):
    path = str(tmp_path / "ww.sqlite3")
    Database(path).close()
    before = Path(path).stat().st_mtime_ns
    assert rs.main(["--db", path]) == 0 and "no settled replay trades" in capsys.readouterr().out
    assert Path(path).stat().st_mtime_ns == before
