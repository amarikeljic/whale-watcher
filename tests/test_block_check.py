"""Part 4 #16: the one-off block-time check (scripts/check_block_times.py), with the RPC mocked."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_block_times.py"
spec = importlib.util.spec_from_file_location("check_block_times", SCRIPT)
cbt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbt)


class FakeRpc:
    url = "https://rpc.example"

    def __init__(self, chain=137, blocks=None, receipts=None):
        self.chain, self.blocks, self.receipts, self.calls = chain, blocks or {}, receipts or {}, []

    def call(self, method, params):
        self.calls.append(method)
        if method not in cbt.ALLOWED:
            raise ValueError(method)
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        return {"timestamp": hex(self.blocks[params[0]])}


def rows(*items):
    return [{"tx_hash": h, "ts": ts, "detected_at": seen, "source": "feed"} for h, ts, seen in items]


def test_the_api_time_is_compared_with_the_block_time_and_each_block_is_read_once():
    rpc = FakeRpc(blocks={"0x10": 1000, "0x11": 1002},
                  receipts={"0xa": {"blockNumber": "0x10"}, "0xb": {"blockNumber": "0x10"}, "0xc": {"blockNumber": "0x11"}})
    res = cbt.check(rpc, rows(("0xa", 1000, 1003.5), ("0xb", 1000, 1004.0), ("0xc", 1003, 1006.0)), log=lambda _m: None)
    assert res["api_minus_block"] == [0, 0, 1] and res["blocks"] == 2
    assert rpc.calls.count("eth_getBlockByNumber") == 2, "two fills in one block: one lookup"
    text = "\n".join(cbt.report(res))
    assert "equal on 2 of 3" in text and "1 fills differ from their block time" in text


def test_it_refuses_any_chain_but_polygon_and_any_method_it_does_not_need():
    with pytest.raises(SystemExit, match="not Polygon mainnet"):
        cbt.check(FakeRpc(chain=1), rows(("0xa", 1, 2)))
    with pytest.raises(ValueError, match="read-only methods"):
        cbt.Rpc("http://127.0.0.1:9", rate=100).call("eth_sendRawTransaction", ["0x00"])


def test_a_missing_receipt_is_counted_not_fatal():
    res = cbt.check(FakeRpc(receipts={}), rows(("0xa", 1, 2)), log=lambda _m: None)
    assert res["checked"] == 0 and res["missing"] == 1
    assert cbt.report(res) == ["no fill could be checked (1 lookups failed)"]


def test_the_database_is_opened_read_only(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fill_events (id INTEGER PRIMARY KEY, tx_hash TEXT, ts INTEGER, detected_at REAL, "
                 "source TEXT)")
    conn.execute("INSERT INTO fill_events (tx_hash, ts, detected_at, source) VALUES ('0xa', 1, 2.0, 'feed')")
    conn.commit()
    conn.close()
    before = path.stat().st_mtime_ns
    assert [r["tx_hash"] for r in cbt.fills(str(path), 10)] == ["0xa"]
    assert path.stat().st_mtime_ns == before
