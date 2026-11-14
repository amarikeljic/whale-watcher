#!/usr/bin/env python3
"""One-off check (v2.1 Part 4 #16): is the Data API's trade time the block's time?

The whale's fill time used everywhere is the Data API's trade ``timestamp``. This reads
the most recent fills the detector stored, looks up each one's transaction on Polygon,
and compares the API's time with the block's own timestamp, and with when our detector
saw the fill.

What it does and does not do:
* Reads the database READ-ONLY (SQLite ``mode=ro``). Nothing is written anywhere.
* Talks to one public Polygon RPC with three READ-ONLY JSON-RPC methods, and only these
  (ALLOWED below): ``eth_chainId``, ``eth_getTransactionReceipt``, ``eth_getBlockByNumber``.
  JSON-RPC is sent as an HTTP POST: that is how the protocol works. No key, no account,
  no wallet, nothing signed, no transaction sent. It refuses to run if the RPC is not
  Polygon mainnet (chain id 137).
* Python standard library only. Polite: at most ``--rate`` requests a second (default 2),
  each block fetched once, and a "too many requests" answer waited out (1, 2, 4, 8 s).

Sources, read 2026-11-14:
* https://ethereum.org/developers/docs/apis/json-rpc/ : eth_getTransactionReceipt returns
  ``blockNumber`` ("block number where this transaction was in"); eth_getBlockByNumber
  returns ``timestamp`` ("the unix timestamp for when the block was collated");
  quantities are hex strings with a 0x prefix.
* https://docs.polygon.technology/pos/reference/rpc-endpoints.md : mainnet chain id 137,
  RPC endpoint https://polygon.drpc.org, "Public RPCs are free to use but may have rate
  limits or traffic restrictions". Any other public RPC from that page works with --rpc.

Run on the server, from the project folder (the database is in ./data):

    python3 scripts/check_block_times.py --db data/whalewatcher.sqlite3 --limit 200
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_RPC = "https://polygon.drpc.org"
POLYGON_CHAIN_ID = 137
ALLOWED = ("eth_chainId", "eth_getTransactionReceipt", "eth_getBlockByNumber")


class Rpc:
    def __init__(self, url: str, rate: float, timeout: float = 20.0) -> None:
        self.url, self.gap, self.timeout, self.last, self.n = url, 1.0 / rate, timeout, 0.0, 0

    def call(self, method: str, params: list) -> object:
        if method not in ALLOWED:
            raise ValueError(f"{method} is not one of the read-only methods this script may use")
        wait = self.last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                    "User-Agent": "whale-watcher-block-check"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:     # a free RPC's "too many requests": wait and retry
                    raise
                time.sleep(2 ** attempt)
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out.get("result")


def fills(db_path: str, limit: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT tx_hash, ts, detected_at, source FROM fill_events WHERE tx_hash IS NOT NULL AND tx_hash != '' "
            "GROUP BY tx_hash ORDER BY MAX(id) DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()


def check(rpc: Rpc, rows: list[sqlite3.Row], *, log=print) -> dict:
    chain = rpc.call("eth_chainId", [])
    if int(str(chain), 16) != POLYGON_CHAIN_ID:
        raise SystemExit(f"{rpc.url} is chain {int(str(chain), 16)}, not Polygon mainnet ({POLYGON_CHAIN_ID}): stopping")
    blocks: dict[str, int] = {}
    api_minus_block, seen_minus_block, missing = [], [], 0
    for i, r in enumerate(rows, 1):
        try:
            receipt = rpc.call("eth_getTransactionReceipt", [r["tx_hash"]])
            if not receipt:
                missing += 1
                continue
            number = receipt["blockNumber"]
            if number not in blocks:
                block = rpc.call("eth_getBlockByNumber", [number, False])
                blocks[number] = int(block["timestamp"], 16)
        except Exception as exc:  # noqa: BLE001 - one failed lookup is a missing row, not the end
            log(f"  {r['tx_hash'][:12]}: {exc}")
            missing += 1
            continue
        api_minus_block.append(r["ts"] - blocks[number])
        seen_minus_block.append(r["detected_at"] - blocks[number])
        if i % 50 == 0:
            log(f"  {i} of {len(rows)} checked")
    return {"checked": len(api_minus_block), "missing": missing, "blocks": len(blocks),
            "api_minus_block": api_minus_block, "seen_minus_block": seen_minus_block}


def report(res: dict) -> list[str]:
    a, s = res["api_minus_block"], res["seen_minus_block"]
    if not a:
        return [f"no fill could be checked ({res['missing']} lookups failed)"]
    exact = sum(1 for x in a if x == 0)
    lines = [f"{res['checked']} fills checked in {res['blocks']} blocks ({res['missing']} could not be looked up)",
             f"API time minus block time: equal on {exact} of {len(a)}; min {min(a):+d}s, median "
             f"{statistics.median(a):+g}s, max {max(a):+d}s",
             f"our detector minus block time: median {statistics.median(s):.1f}s, max {max(s):.1f}s, min {min(s):.1f}s "
             "(fills the backstop found after a restart count here too)"]
    if exact == len(a):
        lines.append("-> the API's trade time IS the block time on every fill checked.")
    else:
        off = [x for x in a if x != 0]
        lines.append(f"-> {len(off)} fills differ from their block time; the delays and the CLV windows assume they "
                     "do not. Worth a look before trusting the timing figures.")
    if min(s) < 0:
        lines.append("-> some fills were 'seen' before their block: the detector's clock is ahead. Check the "
                     "server's clock (timedatectl).")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default="data/whalewatcher.sqlite3", help="the database, opened read-only")
    p.add_argument("--limit", type=int, default=200, help="how many recent fills to check (default 200)")
    p.add_argument("--rpc", default=DEFAULT_RPC, help=f"a public Polygon RPC (default {DEFAULT_RPC})")
    p.add_argument("--rate", type=float, default=2.0, help="requests a second at most (default 2)")
    args = p.parse_args(argv)
    rows = fills(args.db, args.limit)
    print(f"checking {len(rows)} fills against {args.rpc} (read-only, at most {args.rate:g} requests a second)")
    res = check(Rpc(args.rpc, args.rate), rows)
    print("\n".join(report(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
