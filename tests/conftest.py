"""Shared fixtures. Nothing here touches the network: every API call goes to FakeHttp."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import pytest

from whalewatcher.app import open_app
from whalewatcher.config import Config
from whalewatcher.db import Database
from whalewatcher.polymarket import Activity, MarketInfo
from whalewatcher.timeutil import parse_time

NOW = parse_time("2026-11-07T12:00:00Z")
DAY = 86_400


class FakeHttp:
    """Routes ``get_json``/``send_json`` by URL path to handlers; records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def route(self, path: str, handler: Callable[[dict[str, Any]], Any] | Any, method: str = "GET") -> None:
        self.routes[(method, path)] = handler if callable(handler) else (lambda _p, value=handler: value)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def outage(self, host: str, **_: Any) -> None:
        return None

    async def get_json(self, url: str, params: dict[str, Any] | None = None, *, ttl: float = 0) -> Any:
        return self._dispatch("GET", url, dict(params or {}))

    async def send_json(self, method: str, url: str, body: dict[str, Any]) -> Any:
        return self._dispatch(method, url, body)

    def _dispatch(self, method: str, url: str, params: dict[str, Any]) -> Any:
        path = urlsplit(url).path
        self.calls.append((method, path, params))
        handler = self.routes.get((method, path))
        if handler is None:
            raise AssertionError(f"unexpected {method} {path} {params}")
        return handler(params)

    def count(self, path: str) -> int:
        return sum(1 for _, p, _ in self.calls if p == path)


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def cfg() -> Config:
    c = Config(database_path=":memory:")
    c.api.rate_limits = {}
    # Tests work out fees from each market's own rate; the shipped default ("higher")
    # and the table have their own tests in test_costs.py.
    c.fees.rate_source = "market"
    return c


@pytest.fixture(autouse=True)
def _fee_policy():
    """The fee policy is process-wide (set by open_app). Start every test from the same one."""
    from whalewatcher import fees
    from whalewatcher.config import FeesConfig
    fees.configure(FeesConfig(rate_source="market"))
    yield
    fees.configure(None)


@pytest.fixture
async def app(cfg, fake_http):
    db = Database(":memory:")
    async with open_app(cfg, http=fake_http, db=db, clock=lambda: NOW) as a:
        yield a
    db.close()


class Clock:
    """A settable clock for tests that span several polls."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
async def live_app(cfg, fake_http, clock):
    db = Database(":memory:")
    async with open_app(cfg, http=fake_http, db=db, clock=clock) as a:
        yield a
    db.close()


def act(ts: int, type_: str = "TRADE", *, cond: str = "0xc1", side: str = "BUY", size: float = 100.0,
        price: float = 0.5, usdc: float | None = None, index: int | None = 0, token: str | None = None,
        wallet: str = "0xw", tx: str | None = None) -> Activity:
    """One activity row as the API would serve it."""
    if token is None and type_ in ("TRADE", "REDEEM") and index is not None:
        token = f"{cond}-t{index}"
    return Activity(
        wallet=wallet, ts=ts, type=type_, condition_id=cond, token_id=token or "",
        outcome="Yes" if index == 0 else ("No" if index == 1 else ""), outcome_index=index,
        side=side if type_ == "TRADE" else "", size=size, price=price,
        usdc_size=usdc if usdc is not None else round(size * price, 6),
        tx_hash=tx or f"0xtx{ts}{cond}{side}{size}", fill_key=f"k{ts}{cond}{type_}{side}{size}{price}{index}")


def market(cond: str = "0xc1", *, resolved_at: int | None = None, payouts: list[float] | None = None,
           closed_time: int | None = None, game_start: int | None = None, category: str = "politics",
           fee_rate: float = 0.0) -> MarketInfo:
    return MarketInfo(
        condition_id=cond, question=f"Market {cond}?", slug=f"m-{cond}", event_slug=f"e-{cond}",
        category=category, outcomes=["Yes", "No"], token_ids=[f"{cond}-t0", f"{cond}-t1"],
        closed=resolved_at is not None, closed_time=closed_time if closed_time is not None else resolved_at,
        game_start_time=game_start, resolved=resolved_at is not None, resolved_at=resolved_at,
        payouts=payouts if payouts is not None else ([1.0, 0.0] if resolved_at is not None else None),
        fees_enabled=fee_rate > 0, fee_rate=fee_rate)
