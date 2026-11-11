"""Wires the clients, the database and the caches together."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .config import Config
from .db import Database
from .http import HttpClient
from .markets import MarketStore
from .polymarket import Clob, DataApi, Gamma, PricePoint
from .timeutil import now_ts

log = logging.getLogger(__name__)


class PriceLookup:
    """Point-in-time prices from /v2/prices-history?as_of=, cached in SQLite.

    Only answers about instants at least an hour old are cached: a read about
    the recent past can still change as late fills are indexed.
    """

    SOURCE = "prices-history-as-of"

    def __init__(self, db: Database, data: DataApi, clock: Callable[[], int] = now_ts) -> None:
        self.db = db
        self.data = data
        self.clock = clock

    async def at(self, token_id: str, ts: int) -> PricePoint | None:
        row = self.db.one(
            "SELECT price, observed_ts, resolution_seconds FROM price_snapshots "
            "WHERE token_id = ? AND at_ts = ? AND source = ?", (token_id, int(ts), self.SOURCE))
        if row is not None:
            if row["price"] is None:
                return None
            return PricePoint(row["price"], row["observed_ts"], row["resolution_seconds"])
        point = await self.data.price_at(token_id, ts)
        if ts < self.clock() - 3600:
            self.db.execute(
                "INSERT OR REPLACE INTO price_snapshots (token_id, at_ts, price, observed_ts, "
                "resolution_seconds, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, int(ts), None if point is None else point.price,
                 None if point is None else point.ts, None if point is None else point.resolution_seconds,
                 self.SOURCE, self.clock()))
        return point


@dataclass
class App:
    cfg: Config
    db: Database
    http: HttpClient
    data: DataApi
    gamma: Gamma
    clob: Clob
    markets: MarketStore
    prices: PriceLookup
    clock: Callable[[], int]
    # Fractional wall clock, for latency. Whole seconds cannot tell a 0.4 s
    # detection from a 1.4 s one. Under a test clock it is that clock.
    wall: Callable[[], float] = time.time


@asynccontextmanager
async def open_app(cfg: Config, *, http: HttpClient | None = None, db: Database | None = None,
                   clock: Callable[[], int] = now_ts) -> AsyncIterator[App]:
    own_http = http is None
    if http is None:
        http = HttpClient(user_agent=cfg.api.user_agent, timeout=cfg.api.timeout_seconds,
                          max_concurrency=cfg.api.max_concurrency, rate_limits=cfg.api.rate_limits)
    own_db = db is None
    db = db or Database(cfg.database_path)
    await http.start()
    data = DataApi(http, cfg.api.data_api)
    gamma = Gamma(http, cfg.api.gamma_api)
    clob = Clob(http, cfg.api.clob_api)
    try:
        yield App(cfg=cfg, db=db, http=http, data=data, gamma=gamma, clob=clob,
                  markets=MarketStore(db, gamma, data, clock=clock),
                  prices=PriceLookup(db, data, clock), clock=clock,
                  wall=time.time if clock is now_ts else (lambda: float(clock())))
    finally:
        if own_http:
            await http.close()
        if own_db:
            db.close()
