"""Polymarket read-only clients: Data API v2, Gamma, CLOB.

Every endpoint, parameter and field below was checked against the official
docs on 2026-11-07. The doc URL is next to each method. Things the docs and
the live API disagreed on, or that are easy to get wrong, are called out.

* Data API v1 was retired on 2026-11-02 (``/trades`` now answers 410).
  v2 wraps payloads in ``{data, pagination}``, pages with an opaque cursor,
  and uses snake_case. https://docs.polymarket.com/migrate/data-api-v1-to-v2.md
* Data API cursors do NOT carry filters: page 2 of ``/v2/activity?type=TRADE``
  fetched with only the cursor returns every activity type. Every page here
  re-sends the full parameter set.
* Gamma array filters must repeat the key (``condition_ids=a&condition_ids=b``);
  a comma-joined value silently matches nothing.
* CLOB ``/book`` returns bids ascending and asks descending (best price LAST),
  although the OpenAPI schema text says the opposite. Books are re-sorted here.
* Cloudflare answers 403 to Python urllib's default User-Agent; the HTTP
  client always sends its own.

Nothing in this module can place an order: it has no credentials and calls no
authenticated endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .http import HttpClient, HttpError
from .timeutil import parse_time

log = logging.getLogger(__name__)

# Tag slugs mapped to a top-level category, most specific first. Gamma's own
# ``category`` field is empty on current markets (checked 2026-11-07), so the
# category comes from the market's tags (``include_tag=true``).
CATEGORY_TAGS = [
    ("sports", "sports"), ("esports", "sports"), ("crypto", "crypto"),
    ("geopolitics", "geopolitics"), ("politics", "politics"), ("elections", "politics"),
    ("economy", "economics"), ("economics", "economics"), ("fed", "economics"),
    ("finance", "finance"), ("business", "finance"), ("stocks", "finance"),
    ("tech", "tech"), ("ai", "tech"), ("science", "tech"), ("weather", "weather"),
    ("climate", "weather"), ("mentions", "mentions"), ("culture", "culture"),
    ("pop-culture", "culture"), ("movies", "culture"), ("music", "culture"),
    ("world", "geopolitics"),
]


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def category_from_tags(tags: Iterable[str]) -> str:
    slugs = {t.lower() for t in tags}
    for tag, category in CATEGORY_TAGS:
        if tag in slugs:
            return category
    return "other"


# -- models -------------------------------------------------------------------


@dataclass(slots=True)
class Activity:
    """One row of /v2/activity (or /v2/trades, which has the same trade fields)."""

    wallet: str
    ts: int
    type: str
    condition_id: str
    token_id: str
    outcome: str
    outcome_index: int | None
    side: str
    size: float
    usdc_size: float
    price: float
    tx_hash: str
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    fill_key: str = ""

    @property
    def base_key(self) -> str:
        return "|".join(
            [self.tx_hash, self.type, self.token_id or self.condition_id, self.side,
             f"{self.size:.6f}", f"{self.price:.6f}", str(self.ts)]
        )

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, wallet: str | None = None) -> Activity:
        index = raw.get("outcome_index")
        if not isinstance(index, int) or index == 999 or index < 0:
            index = None
        size = _float(raw.get("size"))
        price = _float(raw.get("price"))
        usdc = raw.get("usdc_size")
        return cls(
            wallet=(wallet or raw.get("proxy_wallet") or "").lower(),
            ts=int(raw.get("timestamp") or 0),
            type=str(raw.get("type") or "TRADE").upper(),
            condition_id=str(raw.get("condition_id") or "").lower(),
            token_id=str(raw.get("token_id") or ""),
            outcome=str(raw.get("outcome") or ""),
            outcome_index=index,
            side=str(raw.get("side") or "").upper(),
            size=size,
            usdc_size=_float(usdc) if usdc is not None else size * price,
            price=price,
            tx_hash=str(raw.get("transaction_hash") or ""),
            title=str(raw.get("title") or ""),
            slug=str(raw.get("slug") or ""),
            event_slug=str(raw.get("event_slug") or ""),
        )


def assign_fill_keys(rows: list[Activity]) -> list[Activity]:
    """Give each row a stable unique key.

    The API exposes no per-fill id. Two genuinely separate fills can share every
    field (same tx, size and price), so identical rows are numbered in the order
    they arrive. All rows of one transaction share a block timestamp, and every
    fetch here starts at a timestamp boundary, so the numbering is the same in
    every overlapping fetch.
    """
    seen: Counter[str] = Counter()
    for row in rows:
        base = row.base_key
        row.fill_key = hashlib.sha1(f"{base}#{seen[base]}".encode()).hexdigest()
        seen[base] += 1
    return rows


@dataclass(slots=True)
class MarketInfo:
    condition_id: str
    question: str = ""
    slug: str = ""
    event_slug: str = ""
    event_title: str = ""
    category: str = "other"
    tags: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)
    end_date: int | None = None
    closed: bool = False
    closed_time: int | None = None
    game_start_time: int | None = None
    sports_market_type: str | None = None
    neg_risk: bool = False
    accepting_orders: bool | None = None
    fees_enabled: bool = False
    fee_rate: float = 0.0
    fee_exponent: float = 1.0
    fee_taker_only: bool = True
    description: str = ""
    resolution_source: str = ""
    resolved: bool = False
    resolved_at: int | None = None
    payouts: list[float] | None = None

    @property
    def is_sports(self) -> bool:
        return self.game_start_time is not None or bool(self.sports_market_type) or self.category == "sports"

    def outcome_index_of(self, token_id: str) -> int | None:
        try:
            return self.token_ids.index(token_id)
        except ValueError:
            return None

    def close_reference_ts(self) -> int | None:
        """The 'closing' instant for CLV as the spec defines it.

        Sports: the event start (``gameStartTime``), so the closing price is the
        last pre-game price. Everything else: when the market closed for trading.
        """
        if self.game_start_time is not None:
            return self.game_start_time
        return self.closed_time or self.resolved_at

    def resolved_by(self, as_of: int) -> bool:
        """Resolved at or before ``as_of``. Anything later is the future."""
        return self.resolved and self.resolved_at is not None and self.resolved_at <= as_of

    def payout_for(self, index: int | None) -> float | None:
        if self.payouts is None or index is None or index >= len(self.payouts):
            return None
        return self.payouts[index]

    @property
    def url(self) -> str:
        if self.event_slug and self.slug and self.event_slug != self.slug:
            return f"https://polymarket.com/event/{self.event_slug}/{self.slug}"
        if self.event_slug or self.slug:
            return f"https://polymarket.com/event/{self.event_slug or self.slug}"
        return "https://polymarket.com"

    @classmethod
    def from_gamma(cls, raw: dict[str, Any]) -> MarketInfo:
        events = raw.get("events") or []
        event = events[0] if events and isinstance(events[0], dict) else {}
        tags = [t.get("slug") or t.get("label") or "" for t in (raw.get("tags") or []) if isinstance(t, dict)]
        tags = [t for t in tags if t]
        fee = raw.get("feeSchedule") or {}
        fees_enabled = bool(raw.get("feesEnabled"))
        return cls(
            condition_id=str(raw.get("conditionId") or "").lower(),
            question=str(raw.get("question") or ""),
            slug=str(raw.get("slug") or ""),
            event_slug=str(event.get("slug") or ""),
            event_title=str(event.get("title") or ""),
            category=category_from_tags(tags),
            tags=tags,
            outcomes=[str(o) for o in _json_list(raw.get("outcomes"))],
            token_ids=[str(t) for t in _json_list(raw.get("clobTokenIds"))],
            end_date=parse_time(raw.get("endDate")),
            closed=bool(raw.get("closed")),
            closed_time=parse_time(raw.get("closedTime")),
            game_start_time=parse_time(raw.get("gameStartTime")),
            sports_market_type=raw.get("sportsMarketType") or None,
            neg_risk=bool(raw.get("negRisk")),
            accepting_orders=raw.get("acceptingOrders"),
            fees_enabled=fees_enabled,
            fee_rate=_float(fee.get("rate")) if fees_enabled else 0.0,
            fee_exponent=_float(fee.get("exponent"), 1.0) or 1.0,
            fee_taker_only=fee.get("takerOnly") is not False,
            description=str(raw.get("description") or ""),
            resolution_source=str(raw.get("resolutionSource") or ""),
        )

    def gamma_final_payouts(self, raw: dict[str, Any]) -> list[float] | None:
        """Gamma's ``outcomePrices`` once UMA says resolved: the fallback payout."""
        if str(raw.get("umaResolutionStatus") or "").lower() != "resolved" or not raw.get("closed"):
            return None
        prices = [_float(p, -1) for p in _json_list(raw.get("outcomePrices"))]
        if not prices or any(p < 0 or p > 1 for p in prices):
            return None
        if abs(sum(prices) - 1) > 0.02:
            return None
        return prices


@dataclass(slots=True)
class Level:
    price: float
    size: float


@dataclass(slots=True)
class Book:
    token_id: str
    ts: int | None
    bids: list[Level]   # best (highest) first
    asks: list[Level]   # best (lowest) first
    tick_size: float | None = None
    min_order_size: float | None = None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @classmethod
    def from_api(cls, raw: dict[str, Any], token_id: str) -> Book:
        def levels(key: str) -> list[Level]:
            out = []
            for item in raw.get(key) or []:
                price, size = _float(item.get("price"), -1), _float(item.get("size"))
                if 0 < price < 1 and size > 0:
                    out.append(Level(price, size))
            return out

        return cls(
            token_id=token_id,
            ts=parse_time(raw.get("timestamp")),
            bids=sorted(levels("bids"), key=lambda lv: -lv.price),
            asks=sorted(levels("asks"), key=lambda lv: lv.price),
            tick_size=_float(raw.get("tick_size")) or None,
            min_order_size=_float(raw.get("min_order_size")) or None,
        )


@dataclass(slots=True)
class PricePoint:
    price: float
    ts: int
    resolution_seconds: int


@dataclass(slots=True)
class LeaderboardEntry:
    wallet: str
    rank: int
    pnl: float
    volume: float
    name: str


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# -- Data API v2 ----------------------------------------------------------------


class DataApi:
    """https://data-api.polymarket.com/v2/openapi.json (Polymarket Data API v2 0.1.0)."""

    def __init__(self, http: HttpClient, base: str = "https://data-api.polymarket.com") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def _pages(self, path: str, params: dict[str, Any], *, max_rows: int | None = None,
                     max_pages: int | None = None,
                     stop_after_first: Callable[[list[dict]], bool] | None = None) -> tuple[list[dict], bool]:
        """Follow ``pagination.next_cursor``; returns (rows, truncated).

        ``stop_after_first`` sees the first page when more pages exist; True
        stops there (truncated), so a caller can decide from one page whether
        the rest is worth fetching without fetching that page twice.
        """
        rows: list[dict] = []
        cursor: str | None = None
        pages = 0
        while True:
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            payload = await self.http.get_json(f"{self.base}{path}", query)
            data = (payload or {}).get("data") or []
            rows.extend(data)
            pages += 1
            cursor = ((payload or {}).get("pagination") or {}).get("next_cursor")
            if not cursor:
                return rows, False
            if pages == 1 and stop_after_first is not None and stop_after_first(data):
                return rows, True
            if (max_rows is not None and len(rows) >= max_rows) or (max_pages is not None and pages >= max_pages):
                return rows, True

    # https://docs.polymarket.com/api-reference (Data API) GET /v2/leaderboard
    # time_period: day|week|month|all; category: overall or a Gamma category;
    # sort_by: PNL|VOLUME. Finite windows are marked-to-market equity change;
    # `all` is realized-only. These boards describe NOW, which matters for any
    # past as_of: see scoring.candidate_pool.
    async def leaderboard(self, time_period: str, *, category: str = "overall",
                          sort_by: str = "PNL", limit: int = 100) -> list[LeaderboardEntry]:
        rows, _ = await self._pages(
            "/v2/leaderboard",
            {"time_period": time_period, "category": category, "sort_by": sort_by, "limit": min(limit, 1000)},
            max_rows=limit,
        )
        out = []
        for r in rows[:limit]:
            wallet = str(r.get("user_id") or "").lower()
            if wallet:
                out.append(LeaderboardEntry(wallet, int(r.get("rank") or 0), _float(r.get("pnl")),
                                            _float(r.get("volume")), str(r.get("user_name") or "")))
        return out

    # GET /v2/activity: user required; type is a comma list (TRADE, SPLIT, MERGE,
    # REDEEM, CONVERSION, ...); start/end are inclusive epoch seconds on
    # block_timestamp (omitted start floors to three years back); limit <= 1000;
    # sort_direction ASC|DESC (DESC default).
    async def activity(self, user: str, *, types: Iterable[str] = ("TRADE",), start: int | None = None,
                       end: int | None = None, side: str | None = None,
                       conditions: list[str] | None = None, direction: str = "DESC",
                       max_rows: int | None = None, page_size: int = 1000,
                       stop_after_first: Callable[[list[dict]], bool] | None = None,
                       ) -> tuple[list[Activity], bool]:
        params: dict[str, Any] = {
            "user": user, "type": ",".join(types), "limit": page_size,
            "sort_by": "TIMESTAMP", "sort_direction": direction,
        }
        if start is not None:
            params["start"] = max(1, int(start))
        if end is not None:
            params["end"] = int(end)
        if side:
            params["side"] = side
        if conditions:
            params["condition"] = ",".join(conditions)
        raw, truncated = await self._pages("/v2/activity", params, max_rows=max_rows,
                                           stop_after_first=stop_after_first)
        rows = [Activity.from_api(r, wallet=user) for r in raw]
        return rows, truncated

    # GET /v2/trades: with no user/condition it is the global feed covering the
    # current and previous month (start/end ignored). filter_type CASH +
    # filter_amount keeps only fills at least that many USDC. taker_only
    # defaults to true (each fill once, on its taker side).
    async def trades(self, *, user: str | None = None, conditions: list[str] | None = None,
                     min_usd: float | None = None, taker_only: bool = True, side: str | None = None,
                     max_pages: int = 1, page_size: int = 1000) -> list[Activity]:
        params: dict[str, Any] = {"limit": page_size, "taker_only": taker_only}
        if user:
            params["user"] = user
        if conditions:
            params["condition"] = ",".join(conditions)
        if min_usd:
            params["filter_type"] = "CASH"
            params["filter_amount"] = min_usd
        if side:
            params["side"] = side
        raw, _ = await self._pages("/v2/trades", params, max_pages=max_pages)
        return [Activity.from_api({**r, "type": "TRADE"}) for r in raw]

    # GET /v2/resolutions: condition accepts at most 20 ids. Condition-keyed rows
    # carry resolved_at (RFC3339) and payouts (micro-USDC per share per outcome);
    # UMA rows carry status, last_update_timestamp (epoch string) and price
    # (1e18 = first outcome wins, "69" = unset).
    async def resolutions(self, condition_ids: list[str]) -> list[dict]:
        async def one(group: list[str]) -> list[dict]:
            payload = await self.http.get_json(f"{self.base}/v2/resolutions", {"condition": ",".join(group)})
            return (payload or {}).get("data") or []

        # Concurrent; the HTTP client's semaphore and per-host rate limit keep it polite.
        results = await asyncio.gather(*(one(g) for g in chunks(condition_ids, 20)))
        return [row for rows in results for row in rows]

    # GET /v2/prices-history?token_id=..&as_of=..: point-in-time read, the latest
    # observation at or before as_of (inclusive). Fine grains expire (1-minute
    # data kept at least 7 days, 5-minute at least 60, 30-minute at least 90);
    # 3h and 12h series are permanent, so old reads come back coarser, and each
    # point says how coarse via resolution_seconds.
    async def price_at(self, token_id: str, ts: int) -> PricePoint | None:
        payload = await self.http.get_json(f"{self.base}/v2/prices-history",
                                           {"token_id": token_id, "as_of": int(ts)})
        data = (payload or {}).get("data") or []
        if not data:
            return None
        point = data[-1]
        return PricePoint(_float(point.get("price")), int(point.get("timestamp") or 0),
                          int(point.get("resolution_seconds") or 0))

    # GET /v2/status: data freshness. serving.lag_seconds is the worst projection
    # lag; a climbing age_seconds means the status snapshot itself is stale.
    async def status(self) -> dict:
        payload = await self.http.get_json(f"{self.base}/v2/status")
        return (payload or {}).get("data") or {}

    # GET /v2/positions: user and/or condition; status OPEN (superset, includes
    # REDEEMABLE) | REDEEMABLE | CLOSED | ... current_size is shares held now.
    async def positions(self, user: str, *, conditions: list[str] | None = None,
                        status: str = "OPEN") -> list[dict]:
        params: dict[str, Any] = {"user": user, "status": status, "limit": 500}
        if conditions:
            params["condition"] = ",".join(conditions)
        rows, _ = await self._pages("/v2/positions", params, max_pages=5)
        return rows


# -- Gamma -------------------------------------------------------------------------


class Gamma:
    """https://docs.polymarket.com/api-spec/gamma-openapi.yaml"""

    def __init__(self, http: HttpClient, base: str = "https://gamma-api.polymarket.com") -> None:
        self.http = http
        self.base = base.rstrip("/")

    # GET /markets: condition_ids repeated (max 100 per filter); closed=any to
    # include closed markets in an identifier lookup; include_tag=true for tags.
    async def markets_by_condition(self, condition_ids: list[str]) -> list[dict]:
        async def one(group: list[str]) -> list[dict]:
            payload = await self.http.get_json(
                f"{self.base}/markets",
                {"condition_ids": group, "closed": "any", "include_tag": True, "limit": len(group)},
            )
            return payload or []

        results = await asyncio.gather(*(one(g) for g in chunks(sorted(set(condition_ids)), 40)))
        return [row for rows in results for row in rows]

    # GET /markets ordered by volume within an end-date window (closed markets).
    async def top_markets(self, *, end_min: str, end_max: str, limit: int, closed: bool = True) -> list[dict]:
        payload = await self.http.get_json(
            f"{self.base}/markets",
            {"closed": closed, "end_date_min": end_min, "end_date_max": end_max,
             "order": "volumeNum", "ascending": False, "limit": limit, "include_tag": True},
        )
        return payload or []


# -- CLOB ----------------------------------------------------------------------------


class Clob:
    """https://docs.polymarket.com/api-spec/clob-openapi.yaml (public market data only)."""

    def __init__(self, http: HttpClient, base: str = "https://clob.polymarket.com") -> None:
        self.http = http
        self.base = base.rstrip("/")

    # GET /book?token_id=..: 404 means no order book (closed/resolved market).
    async def book(self, token_id: str) -> Book | None:
        try:
            payload = await self.http.get_json(f"{self.base}/book", {"token_id": token_id})
        except HttpError as exc:
            if exc.status in (400, 404):
                return None
            raise
        return Book.from_api(payload or {}, token_id)


# -- resolution parsing ----------------------------------------------------------------


UMA_UNSET = "69"


def parse_resolution(row: dict[str, Any], n_outcomes: int = 2) -> tuple[int, list[float]] | None:
    """(resolved_at, payouts) from one /v2/resolutions row, or None if unresolved."""
    if str(row.get("status") or "").lower() != "resolved":
        return None
    at = parse_time(row.get("resolved_at")) or parse_time(row.get("last_update_timestamp"))
    if at is None:
        return None
    payouts = row.get("payouts")
    if isinstance(payouts, list) and payouts:
        values = [_float(p) / 1_000_000 for p in payouts]
        if abs(sum(values) - 1) <= 0.02:
            return at, values
    price = row.get("price")
    if price is not None and str(price) != UMA_UNSET and n_outcomes == 2:
        try:
            first = int(str(price)) / 1e18
        except ValueError:
            return None
        if 0 <= first <= 1:
            return at, [first, 1 - first]
    return None
