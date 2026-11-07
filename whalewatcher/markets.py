"""Market metadata and resolutions, cached in SQLite.

A resolved market never changes, so it is fetched once. An unresolved market is
re-checked at most every ``refresh_after`` seconds, and only once it could have
resolved (closed, or past its end date or game start).

Resolution time and payout come from /v2/resolutions; Gamma's ``outcomePrices``
after UMA resolution is the fallback. Payouts are per outcome in dollars per
share, so a cancelled fight that resolves "50-50" is [0.5, 0.5], not a refund.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable

from .db import Database
from .polymarket import DataApi, Gamma, MarketInfo, parse_resolution
from .timeutil import now_ts

log = logging.getLogger(__name__)

_COLUMNS = (
    "condition_id", "question", "slug", "event_slug", "event_title", "category", "tags", "outcomes",
    "token_ids", "end_date", "closed", "closed_time", "game_start_time", "sports_market_type",
    "neg_risk", "accepting_orders", "fees_enabled", "fee_rate", "fee_exponent", "fee_taker_only",
    "description", "resolution_source", "resolved", "resolved_at", "payouts",
)


def _to_row(m: MarketInfo) -> dict:
    return {
        "condition_id": m.condition_id, "question": m.question, "slug": m.slug,
        "event_slug": m.event_slug, "event_title": m.event_title, "category": m.category,
        "tags": json.dumps(m.tags), "outcomes": json.dumps(m.outcomes), "token_ids": json.dumps(m.token_ids),
        "end_date": m.end_date, "closed": int(m.closed), "closed_time": m.closed_time,
        "game_start_time": m.game_start_time, "sports_market_type": m.sports_market_type,
        "neg_risk": int(m.neg_risk),
        "accepting_orders": None if m.accepting_orders is None else int(bool(m.accepting_orders)),
        "fees_enabled": int(m.fees_enabled), "fee_rate": m.fee_rate, "fee_exponent": m.fee_exponent,
        "fee_taker_only": int(m.fee_taker_only), "description": m.description,
        "resolution_source": m.resolution_source, "resolved": int(m.resolved),
        "resolved_at": m.resolved_at, "payouts": json.dumps(m.payouts) if m.payouts is not None else None,
    }


def _from_row(row) -> MarketInfo:
    return MarketInfo(
        condition_id=row["condition_id"], question=row["question"] or "", slug=row["slug"] or "",
        event_slug=row["event_slug"] or "", event_title=row["event_title"] or "",
        category=row["category"] or "other", tags=json.loads(row["tags"] or "[]"),
        outcomes=json.loads(row["outcomes"] or "[]"), token_ids=json.loads(row["token_ids"] or "[]"),
        end_date=row["end_date"], closed=bool(row["closed"]), closed_time=row["closed_time"],
        game_start_time=row["game_start_time"], sports_market_type=row["sports_market_type"],
        neg_risk=bool(row["neg_risk"]),
        accepting_orders=None if row["accepting_orders"] is None else bool(row["accepting_orders"]),
        fees_enabled=bool(row["fees_enabled"]), fee_rate=row["fee_rate"] or 0.0,
        fee_exponent=row["fee_exponent"] or 1.0, fee_taker_only=bool(row["fee_taker_only"]),
        description=row["description"] or "", resolution_source=row["resolution_source"] or "",
        resolved=bool(row["resolved"]), resolved_at=row["resolved_at"],
        payouts=json.loads(row["payouts"]) if row["payouts"] else None,
    )


class MarketStore:
    def __init__(self, db: Database, gamma: Gamma, data: DataApi, *,
                 clock: Callable[[], int] = now_ts) -> None:
        self.db = db
        self.gamma = gamma
        self.data = data
        self.clock = clock

    def get(self, condition_id: str) -> MarketInfo | None:
        row = self.db.one("SELECT * FROM poly_markets WHERE condition_id = ?", (condition_id.lower(),))
        return _from_row(row) if row is not None and row["meta_fetched_at"] is not None and row["question"] is not None else None

    def get_many(self, condition_ids: Iterable[str]) -> dict[str, MarketInfo]:
        ids = sorted({c.lower() for c in condition_ids if c})
        out: dict[str, MarketInfo] = {}
        for i in range(0, len(ids), 500):
            group = ids[i:i + 500]
            marks = ",".join("?" * len(group))
            for row in self.db.query(f"SELECT * FROM poly_markets WHERE condition_id IN ({marks})", group):
                if row["question"] is not None:
                    out[row["condition_id"]] = _from_row(row)
        return out

    def save(self, market: MarketInfo, *, meta_fetched_at: int | None = None,
             resolution_checked_at: int | None = None) -> None:
        self.save_many([market], meta_fetched_at=meta_fetched_at, resolution_checked_at=resolution_checked_at)

    def save_many(self, markets: Iterable[MarketInfo], *, meta_fetched_at: int | None = None,
                  resolution_checked_at: int | None = None) -> None:
        """Upsert in one transaction (one fsync, not one per market)."""
        rows = []
        for market in markets:
            row = _to_row(market)
            row["meta_fetched_at"] = meta_fetched_at
            row["resolution_checked_at"] = resolution_checked_at
            rows.append(row)
        if not rows:
            return
        cols = list(rows[0])
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, poly_markets.{c})" if c in ("meta_fetched_at", "resolution_checked_at")
            else f"{c} = excluded.{c}"
            for c in cols if c != "condition_id"
        )
        with self.db.tx() as conn:
            conn.executemany(
                f"INSERT INTO poly_markets ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                f"ON CONFLICT(condition_id) DO UPDATE SET {updates}",
                rows,
            )

    def _mark_missing(self, condition_ids: Iterable[str], now: int) -> None:
        with self.db.tx() as conn:
            conn.executemany(
                "INSERT INTO poly_markets (condition_id, meta_fetched_at) VALUES (?, ?) "
                "ON CONFLICT(condition_id) DO UPDATE SET meta_fetched_at = excluded.meta_fetched_at",
                [(cid, now) for cid in condition_ids],
            )

    async def ensure(self, condition_ids: Iterable[str], *, refresh_after: int = 6 * 3600) -> dict[str, MarketInfo]:
        """Metadata for every id (when Gamma knows it) with resolution state as fresh as allowed."""
        now = self.clock()
        ids = sorted({c.lower() for c in condition_ids if c})
        known = {r["condition_id"]: r for r in self._rows(ids)}

        need_meta = [c for c in ids if c not in known or known[c]["meta_fetched_at"] is None
                     or (not known[c]["resolved"] and (now - (known[c]["meta_fetched_at"] or 0)) > refresh_after)]
        if need_meta:
            raws = await self.gamma.markets_by_condition(need_meta)
            got = set()
            fresh: list[MarketInfo] = []
            for raw in raws:
                market = MarketInfo.from_gamma(raw)
                if not market.condition_id:
                    continue
                got.add(market.condition_id)
                existing = known.get(market.condition_id)
                if existing is not None and existing["resolved"]:
                    market.resolved = True
                    market.resolved_at = existing["resolved_at"]
                    market.payouts = json.loads(existing["payouts"]) if existing["payouts"] else None
                else:
                    fallback = market.gamma_final_payouts(raw)
                    if fallback is not None:
                        market.resolved, market.payouts = True, fallback
                        market.resolved_at = market.closed_time
                fresh.append(market)
            self.save_many(fresh, meta_fetched_at=now)
            missing = [c for c in need_meta if c not in got]
            if missing:
                log.debug("Gamma has no market for %d condition ids", len(missing))
                self._mark_missing(missing, now)

        markets = self.get_many(ids)
        check = []
        for m in markets.values():
            row = known.get(m.condition_id)
            if self._final(row) or not self._could_be_resolved(m, now):
                continue
            checked = row["resolution_checked_at"] if row is not None else None
            if checked is None or now - checked > refresh_after:
                check.append(m)
        if check:
            await self._refresh_resolutions(check, now)
            markets = self.get_many(ids)
        return markets

    @staticmethod
    def _final(row) -> bool:
        """Resolved by /v2/resolutions already: nothing left to learn."""
        return row is not None and bool(row["resolved"]) and row["resolution_checked_at"] is not None

    @staticmethod
    def _could_be_resolved(m: MarketInfo, now: int) -> bool:
        if m.closed or m.resolved:
            return True
        for ts in (m.end_date, m.game_start_time):
            if ts is not None and ts < now:
                return True
        return False

    async def _refresh_resolutions(self, markets: list[MarketInfo], now: int) -> None:
        by_id = {m.condition_id: m for m in markets}
        rows = await self.data.resolutions(list(by_id))
        best: dict[str, tuple[int, list[float]]] = {}
        for row in rows:
            cid = str(row.get("condition_id") or "").lower()
            market = by_id.get(cid)
            if market is None or cid in best:
                continue
            parsed = parse_resolution(row, n_outcomes=max(2, len(market.outcomes)))
            if parsed is not None:
                best[cid] = parsed
        for cid, market in by_id.items():
            if cid in best:
                market.resolved = True
                market.resolved_at, market.payouts = best[cid]
        self.save_many(by_id.values(), resolution_checked_at=now)

    def _rows(self, ids: list[str]):
        out = []
        for i in range(0, len(ids), 500):
            group = ids[i:i + 500]
            marks = ",".join("?" * len(group))
            out.extend(self.db.query(f"SELECT * FROM poly_markets WHERE condition_id IN ({marks})", group))
        return out
