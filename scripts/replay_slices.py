#!/usr/bin/env python3
"""Where does a copier keep the whale's edge? The six-month replay, cut by things known at
the moment of the signal.

Read-only. Standard library plus this repository's own bootstrap:

    python3 scripts/replay_slices.py --db data/whalewatcher.sqlite3

It reads the simulated replays (`backtest` writes them: wallets picked with earlier data
only, every alert-worthy buy group copied) and takes each distinct signal once. For every
cut it prints, with a 95% interval that resamples wallets and events together:

* at the whale's price: what the whale itself earned on the signal, fee included;
* copied 90 s later: the replay's own fill (the minute price plus a cent, plus the fee),
  for the copies that filled. "missed" is the share whose price ran 3 cents or more first.

These are cuts made after seeing the data: 22 here, about 45 looked at in all, so one
or two intervals clear zero by luck alone. Read them as where to test forward, not as
results. README, "Where could a
copier's edge come from?" holds the run of 2026-11-15.
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whalewatcher.horizon import bucket, days_to_close, scheduled_end
from whalewatcher.stats_util import two_way_bootstrap_mean_ci

QUERY = """
SELECT p.strategy, p.status, p.alert_id, p.cost, p.pnl, p.zero_cost, p.zero_pnl, p.zero_fees, p.payout_per_share,
       a.wallet, a.condition_id, a.token_id, a.whale_usd, a.whale_price, a.first_fill_ts, a.trigger_fill_ts,
       m.category, m.game_start_time, m.end_date, m.event_slug
FROM paper_trades p JOIN alerts a ON a.id = p.alert_id LEFT JOIN poly_markets m ON m.condition_id = p.condition_id
WHERE p.venue = 'polymarket' AND p.strategy IN ('backtest:spec', 'backtest:walk-forward') AND p.settled_at IS NOT NULL
"""
IN_PLAY, NOT_IN_PLAY = "in-play (game started)", "game not started, or not sports"


def load(conn: sqlite3.Connection) -> list[dict]:
    """One row per distinct signal (a wallet's buy group), whichever replay it came from."""
    rows = [dict(r) for r in conn.execute(QUERY)]
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["strategy"] != "backtest:walk-forward", r["alert_id"])):
        key = (r["wallet"], r["token_id"], r["first_fill_ts"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def hedged(conn: sqlite3.Connection, r: dict) -> bool:
    """Did the wallet buy this market's other outcome in the 24 hours before? Known at the signal."""
    row = conn.execute(
        "SELECT COALESCE(SUM(usdc_size), 0) FROM wallet_trades WHERE wallet = ? AND ts >= ? AND ts < ? "
        "AND condition_id = ? AND token_id != ? AND side = 'BUY' AND type = 'TRADE'",
        (r["wallet"], r["first_fill_ts"] - 86_400, r["first_fill_ts"], r["condition_id"], r["token_id"])).fetchone()
    return (row[0] or 0.0) > 0


def describe(conn: sqlite3.Connection, r: dict) -> None:
    at = r["trigger_fill_ts"] or r["first_fill_ts"]
    start = r["game_start_time"]
    r["phase"] = IN_PLAY if start and at >= start else NOT_IN_PLAY
    end = scheduled_end(r["end_date"], start)
    r["closes"] = bucket(days_to_close(end, at)) if end is not None else "no end date"
    p = r["whale_price"]
    r["price"] = ("under 0.20" if p < 0.2 else "0.20-0.40" if p < 0.4 else "0.40-0.60" if p < 0.6
                  else "0.60-0.80" if p < 0.8 else "0.80 and up")
    usd = r["whale_usd"]
    r["size"] = "under $1k" if usd < 1000 else "$1k-5k" if usd < 5000 else "$5k-20k" if usd < 20000 else "$20k and up"
    r["hedge"] = "bought the other side in the 24 h before" if hedged(conn, r) else "no other-side buying before"
    ran = "the price ran 3c+ within 90 s (missed)" if r["status"] == "MISSED" else "the price sat still (filled)"
    r["ran"] = f"{'in-play' if r['phase'] == IN_PLAY else 'not in-play'}: {ran}"
    r["zero_roi"] = r["zero_pnl"] / r["zero_cost"] if r["zero_cost"] else None
    r["copy_roi"] = r["pnl"] / r["cost"] if r["status"] == "FILLED" and r["cost"] else None
    r["win"] = (r["payout_per_share"] or 0) > 0.5


def interval(pairs: list[tuple[float, dict]], samples: int) -> str:
    if not pairs:
        return "-"
    m, lo, hi = two_way_bootstrap_mean_ci([x for x, _ in pairs], [r["wallet"] for _, r in pairs],
                                          [r["event_slug"] or r["condition_id"] for _, r in pairs], samples=samples)
    return f"{m:+6.1%}" if lo is None else f"{m:+6.1%} [{lo:+.0%}, {hi:+.0%}]"


def table(signals: list[dict], title: str, key: str, samples: int) -> list[str]:
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in signals:
        groups[r[key]].append(r)
    out = ["", title, f"  {'cut':<52}{'n':>5} {'wins':>5} {'missed':>6}  {'at the whale price':<24} copied 90 s later"]
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        zero = [(r["zero_roi"], r) for r in g if r["zero_roi"] is not None]
        copy = [(r["copy_roi"], r) for r in g if r["copy_roi"] is not None]
        out.append(f"  {name:<52}{len(g):>5} {sum(r['win'] for r in g) / len(g):>5.0%} "
                   f"{sum(r['status'] == 'MISSED' for r in g) / len(g):>6.0%}  {interval(zero, samples):<24} "
                   f"{interval(copy, samples)}")
    return out


def report(conn: sqlite3.Connection, samples: int = 1000) -> list[str]:
    signals = load(conn)
    if not signals:
        return ["no settled replay trades in this database: run `python -m whalewatcher backtest` first"]
    for r in signals:
        describe(conn, r)
    fees = [r["zero_fees"] / r["zero_cost"] for r in signals if r["zero_cost"] and r["zero_fees"] is not None]
    out = [f"{len(signals)} distinct replay signals, {len({r['wallet'] for r in signals})} wallets, "
           f"{len({r['event_slug'] or r['condition_id'] for r in signals})} events. The buyer's fee was "
           f"{sum(fees) / len(fees):.1%} of the stake on average." if fees else f"{len(signals)} distinct replay signals"]
    for title, key in (("IN-PLAY OR NOT", "phase"), ("DID THE PRICE RUN?", "ran"),
                       ("SCHEDULED TIME TO CLOSE", "closes"), ("PRICE THE WHALE PAID", "price"),
                       ("SIZE OF THE WHALE'S BUY", "size"), ("HEDGED?", "hedge")):
        out += table(signals, title, key, samples)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="data/whalewatcher.sqlite3")
    ap.add_argument("--samples", type=int, default=1000, help="bootstrap draws per interval (default 1000)")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print("\n".join(report(conn, args.samples)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
