"""A small fake Polymarket: activity, markets, resolutions, prices and books behind FakeHttp.

Routes mimic the real v2 contracts closely enough to exercise the clients:
``{data, pagination: {next_cursor}}`` envelopes, inclusive start/end bounds,
DESC by default, filters re-sent on every page.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from whalewatcher.timeutil import iso


@dataclass
class FakeMarket:
    cond: str
    resolved_at: int | None = None
    payouts: list[float] | None = None
    closed_time: int | None = None
    game_start: int | None = None
    end_date: int | None = None
    tags: list[str] = field(default_factory=lambda: ["politics"])
    fee_rate: float = 0.0
    accepting_orders: bool = True
    question: str = ""

    event: str | None = None
    slug: str | None = None
    outcomes: tuple[str, str] = ("Yes", "No")
    sports_market_type: str | None = None
    event_title: str | None = None

    def gamma(self) -> dict[str, Any]:
        closed = self.resolved_at is not None or self.closed_time is not None
        event = self.event or f"e-{self.cond}"
        return {
            "conditionId": self.cond, "question": self.question or f"Will {self.cond} happen?",
            "slug": self.slug or f"m-{self.cond}",
            "events": [{"slug": event, "title": self.event_title or f"Event {event}"}],
            "tags": [{"slug": t, "label": t.title()} for t in self.tags],
            "sportsMarketType": self.sports_market_type,
            "outcomes": json.dumps(list(self.outcomes)),
            "clobTokenIds": json.dumps([f"{self.cond}-t0", f"{self.cond}-t1"]),
            "endDate": iso(self.end_date).replace(" ", "T") if self.end_date else None,
            "closed": closed,
            "closedTime": iso(self.closed_time or self.resolved_at).replace("Z", "+00") if closed else None,
            "gameStartTime": iso(self.game_start).replace("Z", "+00") if self.game_start else None,
            "feesEnabled": self.fee_rate > 0,
            "feeSchedule": {"rate": self.fee_rate, "exponent": 1, "takerOnly": True, "rebateRate": 0.25},
            "description": f"Resolves Yes if {self.cond} happens.", "acceptingOrders": self.accepting_orders,
            "umaResolutionStatus": "resolved" if self.resolved_at else None,
            "outcomePrices": json.dumps([str(p) for p in (self.payouts or [0.5, 0.5])]),
        }


class World:
    def __init__(self, http) -> None:
        self.http = http
        self.activity: list[dict[str, Any]] = []
        self.markets: dict[str, FakeMarket] = {}
        self.price_fn: Callable[[str, int], float | None] = lambda token, ts: 0.5
        self.books: dict[str, dict[str, Any]] = {}
        self.leaderboard: list[dict[str, Any]] = []
        self.global_trades: list[dict[str, Any]] = []
        # CLOB minute history: (token, startTs, endTs) -> [(t, p), ...]
        self.clob_history: Callable[[str, int, int], list[tuple[int, float]]] = lambda token, s, e: [(s + 30, 0.5)]
        http.route("/v2/activity", self._activity)
        http.route("/markets", self._gamma_markets)
        http.route("/v2/resolutions", self._resolutions)
        http.route("/v2/prices-history", self._prices)
        http.route("/v2/trades", self._trades)
        http.route("/v2/leaderboard", self._leaderboard)
        http.route("/book", self._book)
        http.route("/v2/status", {"data": {"age_seconds": 3, "serving": {"lag_seconds": 2}}})
        http.route("/v2/positions", self._positions)
        http.route("/prices-history", self._clob_prices)
        # /v2/user-stats: wallet -> the `data` object; an unknown wallet answers data: null.
        self.user_stats: dict[str, dict[str, Any]] = {}
        http.route("/v2/user-stats", lambda params: {"data": self.user_stats.get(params["user"])})

    def _clob_prices(self, params: dict[str, Any]) -> dict[str, Any]:
        token, start, end = params["market"], int(params["startTs"]), int(params["endTs"])
        return {"history": [{"t": t, "p": p} for t, p in self.clob_history(token, start, end)]}

    def _positions(self, params: dict[str, Any]) -> dict[str, Any]:
        user = params["user"]
        conds = set(str(params["condition"]).split(",")) if params.get("condition") else None
        held: dict[str, float] = {}
        for r in self.activity:
            if r["proxy_wallet"] != user or r["type"] != "TRADE" or (conds and r["condition_id"] not in conds):
                continue
            sign = 1 if r["side"] == "BUY" else -1
            held[r["token_id"]] = held.get(r["token_id"], 0.0) + sign * r["size"]
        rows = [{"token_id": t, "current_size": s} for t, s in held.items() if s > 0]
        return {"data": rows, "pagination": {"next_cursor": None}}

    # -- building --------------------------------------------------------------

    def add_market(self, m: FakeMarket) -> FakeMarket:
        self.markets[m.cond] = m
        return m

    def trade(self, wallet: str, ts: int, cond: str, *, side: str = "BUY", index: int = 0, size: float = 100,
              price: float = 0.5, usdc: float | None = None, type_: str = "TRADE", tx: str | None = None) -> None:
        self.activity.append({
            "proxy_wallet": wallet, "timestamp": ts, "condition_id": cond, "type": type_, "size": size,
            "usdc_size": usdc if usdc is not None else round(size * price, 6),
            "transaction_hash": tx or f"0x{ts:x}{len(self.activity)}", "price": price,
            "token_id": f"{cond}-t{index}" if type_ in ("TRADE", "REDEEM") else "",
            "side": side if type_ == "TRADE" else "", "outcome_index": index if type_ in ("TRADE", "REDEEM") else 999,
            "outcome": ["Yes", "No"][index] if type_ in ("TRADE", "REDEEM") else "",
            "title": f"Will {cond} happen?", "slug": f"m-{cond}", "event_slug": f"e-{cond}",
        })

    # -- routes ------------------------------------------------------------------

    @staticmethod
    def _page(rows: list[dict], params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 100)
        offset = int(params.get("cursor") or 0)
        page = rows[offset:offset + limit]
        nxt = str(offset + limit) if offset + limit < len(rows) else None
        return {"data": page, "pagination": {"limit": limit, "offset": offset, "has_more": nxt is not None,
                                             "next_cursor": nxt}}

    def _activity(self, params: dict[str, Any]) -> dict[str, Any]:
        user = params["user"]
        types = set(str(params.get("type") or "TRADE").split(","))
        start, end = int(params.get("start") or 0), int(params.get("end") or 10**12)
        conds = set(str(params["condition"]).split(",")) if params.get("condition") else None
        side = params.get("side")
        rows = [r for r in self.activity if r["proxy_wallet"] == user and r["type"] in types
                and start <= r["timestamp"] <= end and (conds is None or r["condition_id"] in conds)
                and (side is None or r["side"] == side)]
        rows.sort(key=lambda r: r["timestamp"], reverse=params.get("sort_direction", "DESC") == "DESC")
        return self._page(rows, params)

    def _gamma_markets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        ids = params.get("condition_ids")
        if ids is not None:
            ids = ids if isinstance(ids, list) else [ids]
            return [self.markets[c].gamma() for c in ids if c in self.markets]
        offset, limit = int(params.get("offset") or 0), min(100, int(params.get("limit") or 100))
        return [m.gamma() for m in self.markets.values()][offset:offset + limit]

    def _resolutions(self, params: dict[str, Any]) -> dict[str, Any]:
        out = []
        for c in str(params["condition"]).split(","):
            m = self.markets.get(c)
            if m is None:
                continue
            if m.resolved_at is None:
                out.append({"condition_id": c, "status": "proposed", "last_update_timestamp": "0"})
            else:
                out.append({"condition_id": c, "status": "resolved",
                            "resolved_at": iso(m.resolved_at).replace(" ", "T"),
                            "payouts": [int(p * 1_000_000) for p in (m.payouts or [1.0, 0.0])],
                            "last_update_timestamp": iso(m.resolved_at).replace(" ", "T")})
        return {"data": out}

    def _prices(self, params: dict[str, Any]) -> dict[str, Any]:
        token, at = params["token_id"], int(params["as_of"])
        price = self.price_fn(token, at)
        return {"data": [] if price is None else [{"timestamp": at - 30, "price": price, "resolution_seconds": 60}],
                "pagination": {"next_cursor": None}}

    def _trades(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("condition"):
            conds = set(str(params["condition"]).split(","))
            rows = [r for r in self.activity if r["type"] == "TRADE" and r["condition_id"] in conds
                    and r["usdc_size"] >= float(params.get("filter_amount") or 0)]
        else:
            rows = list(self.global_trades)
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return self._page(rows, params)

    def _leaderboard(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._page(self.leaderboard, params)

    def _book(self, params: dict[str, Any]) -> dict[str, Any]:
        token = params["token_id"]
        if token not in self.books:
            from whalewatcher.http import HttpError
            raise HttpError("404", status=404, answered=True)
        return self.books[token]

    def set_book(self, token: str, asks: list[tuple[float, float]], bids: list[tuple[float, float]] = ()) -> None:
        # Real books arrive best-LAST on both sides.
        self.books[token] = {
            "asks": [{"price": str(p), "size": str(s)} for p, s in sorted(asks, key=lambda x: -x[0])],
            "bids": [{"price": str(p), "size": str(s)} for p, s in sorted(bids, key=lambda x: x[0])],
            "timestamp": "1794000000000", "tick_size": "0.01", "min_order_size": "5",
        }
