"""`python -m whalewatcher v2check`: can this service trade and settle Polymarket's
new-format (Protocol V2) markets?

New markets move to Protocol V2 from 2026-11-23, most sports on 2026-12-01
(https://docs.polymarket.com/migrate/polymarket-v2/overview.md). A V2 market trades
under position ids instead of CLOB token ids. This command takes real V2 markets through
the same code, in the same order, as the live service:

    the global trade feed -> the market looked up on Gamma by the feed's condition id
    -> the trade's token matched to an outcome -> its order book -> its price history
    -> its resolution and payout -> the wallet's own activity (the backstop route)

It is read-only: public GET requests to the three hosts the service already uses, and a
scratch in-memory database, so nothing is written to the real one.

Exit code 0: every check passed on at least one V2 market. 1: a check failed (the
detail says which trades or markets). 2: no V2 market exists yet, nothing to check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .polymarket import Activity, MarketInfo, parse_resolution
from .protocol import decode_v2_position_id, is_v2_position_id
from .timeutil import iso

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
DAY = 86_400
UNKNOWN_SHARE_LIMIT = 0.02      # markets in the feed that Gamma does not know: above this is a failure
COMBO_MODULE = 3                # position ids of combos (several games joined into one bet) start with 3


def _is_combo(rows: list[Activity]) -> bool:
    """A combo: one bet on several games at once ("A AND B"). Seen live on 2026-11-15: its
    trades carry a 64-character condition id and module-3 position ids, with no slug and
    no outcome index, and Gamma's /markets does not list it. The service cannot price or
    settle one, so signals on combos are skipped ("market metadata unavailable")."""
    decoded = [decode_v2_position_id(r.token_id) for r in rows]
    return bool(decoded) and all(d is not None and d.module_id == COMBO_MODULE for d in decoded)


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    v2_markets: int = 0
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        return 0 if self.v2_markets else 2

    @property
    def verdict(self) -> str:
        if self.failed:
            return f"FAILED: {len(self.failed)} of {len(self.checks)} checks. Fix before 2026-12-01: " + \
                   "; ".join(c.name for c in self.failed)
        if not self.v2_markets:
            return "NOTHING TO CHECK YET: no new-format (V2) market was found. Run again after 2026-11-23."
        skipped = [c.name for c in self.checks if c.status == SKIP]
        return (f"ALL CHECKS PASSED on {self.v2_markets} new-format (V2) markets"
                + (f" ({len(skipped)} could not run yet: {'; '.join(skipped)})" if skipped else ""))

    def lines(self) -> list[str]:
        out = [f"{c.status:<4} {c.name}: {c.detail}" for c in self.checks]
        out += [f"note {n}" for n in self.notes]
        return out + [self.verdict]


def _day(ts: int) -> str:
    return iso(ts)[:10] + "T00:00:00Z"


def _mapping(m: MarketInfo, rows: list[Activity]) -> tuple[int, int, list[str]]:
    """(matched, matched through the other id list, problems) for trades on one market."""
    ok = alt = 0
    bad: list[str] = []
    for r in rows:
        index = m.outcome_index_of(r.token_id)
        if index is None:
            bad.append(f"{m.slug or m.condition_id[:14]}: token {r.token_id[:18]}… is not listed by its market")
        elif r.outcome_index is not None and index != r.outcome_index:
            bad.append(f"{m.slug or m.condition_id[:14]}: token {r.token_id[:18]}… maps to outcome {index}, "
                       f"the trade says {r.outcome_index}")
        else:
            ok += 1
            alt += r.token_id not in m.token_ids
    return ok, alt, bad


async def run(app: Any, *, pages: int = 3, sample: int = 8) -> Report:
    rep = Report()
    now = int(app.clock())

    # 1. The global trade feed, as the detector reads it.
    feed: list[Activity] = []
    cursor = None
    for _ in range(max(1, pages)):
        rows, cursor = await app.data.trade_feed(limit=1000, nonce=str(time.time_ns()), cursor=cursor)
        feed += rows
        if not cursor or not rows:
            break
    by_cond: dict[str, list[Activity]] = {}
    for r in feed:
        if r.condition_id:
            by_cond.setdefault(r.condition_id, []).append(r)

    # 2. Each feed market looked up on Gamma by the feed's condition id, as the runner does.
    found: dict[str, tuple[MarketInfo, dict]] = {}
    for raw in await app.gamma.markets_by_condition(sorted(by_cond)) if by_cond else []:
        m = MarketInfo.from_gamma(raw)
        if m.condition_id:
            found[m.condition_id] = (m, raw)
    combos = [c for c in by_cond if c not in found and _is_combo(by_cond[c])]
    singles = len(by_cond) - len(combos)
    unknown = [c for c in by_cond if c not in found and c not in combos]
    unknown_v2 = [c for c in unknown if any(is_v2_position_id(r.token_id) for r in by_cond[c])]
    share = len(unknown) / singles if singles else 0.0
    detail = (f"{len(found)} of {singles} markets in {len(feed)} feed trades were found on Gamma"
              + (f"; not found: {', '.join(c[:14] + '…' for c in unknown[:3])}" if unknown else ""))
    if combos:
        rep.notes.append(f"{len(combos)} combos (several games joined into one bet) in the feed, "
                         f"{sum(len(by_cond[c]) for c in combos)} trades: skipped by design, the service cannot "
                         "price or settle them")
    if not by_cond:
        rep.add("the trade feed answers", FAIL, "the global trade feed returned no trades")
    elif unknown_v2:
        # The feed's name for the market is not Gamma's. Ask Gamma by the trade's own
        # position id, to say what the fix has to map.
        tokens = {by_cond[c][0].token_id: c for c in unknown_v2}
        by_pos = {}
        for raw in await app.gamma.markets_by_position(list(tokens)[:20]):
            m = MarketInfo.from_gamma(raw)
            for t in m.token_ids + m.alt_token_ids:
                if t in tokens:
                    by_pos[tokens[t]] = m
        example = next(iter(by_pos.items()), None)
        rep.add("feed markets are found on Gamma", FAIL,
                detail + f". {len(unknown_v2)} of the missing ones trade under new-format ids, so the feed names V2 "
                         f"markets differently from Gamma and their signals would be skipped. Found by position id "
                         f"instead: {len(by_pos)} of {min(len(tokens), 20)}"
                + (f" (the feed says {example[0]}, Gamma says {example[1].condition_id}, version "
                   f"{example[1].version})" if example else ""))
    elif share > UNKNOWN_SHARE_LIMIT:
        rep.add("feed markets are found on Gamma", FAIL, detail + f" ({share:.1%} missing, limit "
                                                                    f"{UNKNOWN_SHARE_LIMIT:.0%})")
    else:
        rep.add("feed markets are found on Gamma", PASS, detail)

    # 3. V2 markets: those in the feed, plus the busiest open and just-closed ones Gamma lists
    #    (GET /markets ordered by volume inside an end-date window).
    v2: dict[str, tuple[MarketInfo, dict]] = {c: mr for c, mr in found.items() if mr[0].version == "v2"}
    listed = 0
    for closed, lo, hi in ((False, now, now + 3 * DAY), (True, now - 3 * DAY, now + DAY)):
        for raw in await app.gamma.top_markets(end_min=_day(lo), end_max=_day(hi + DAY), closed=closed):
            listed += 1
            m = MarketInfo.from_gamma(raw)
            if m.condition_id and m.version == "v2":
                v2.setdefault(m.condition_id, (m, raw))
    rep.v2_markets = len(v2)
    versions: dict[str, int] = {}
    for m, _ in found.values():
        versions[m.version] = versions.get(m.version, 0) + 1
    rep.notes.append(f"versions among feed markets: {versions or '-'}; Gamma listing rows read: {listed}")
    if not v2:
        return rep

    open_v2 = [mr for mr in v2.values() if not mr[0].closed][:sample]
    closed_v2 = [mr for mr in v2.values() if mr[0].closed][:sample]

    # 4. Trades on V2 markets: every token must be one its market lists, at the right outcome.
    #    From the feed, and asked per market (GET /v2/trades?condition=..), which also shows
    #    the Data API knows each market under Gamma's condition id.
    trades = {c: list(by_cond.get(c, [])) for c in v2}
    asked = answered = 0
    for m, _ in (open_v2 + closed_v2)[:sample]:
        asked += 1
        rows = await app.data.trades(conditions=[m.condition_id], taker_only=False, page_size=50)
        answered += bool(rows)
        have = {r.base_key for r in trades[m.condition_id]}
        trades[m.condition_id] += [r for r in rows if r.base_key not in have]
    ok = alt = 0
    bad: list[str] = []
    for c, rows in trades.items():
        a, b, problems = _mapping(v2[c][0], rows)
        ok, alt, bad = ok + a, alt + b, bad + problems
    if bad:
        rep.add("a V2 trade's token finds its outcome", FAIL,
                f"{len(bad)} of {ok + len(bad)} trades did not: " + " | ".join(bad[:3]))
    elif ok:
        rep.add("a V2 trade's token finds its outcome", PASS,
                f"{ok} trades on {sum(1 for r in trades.values() if r)} V2 markets, all matched"
                + (f" ({alt} through the other id list: the feed reports the older kind of id)" if alt else ""))
    else:
        rep.add("a V2 trade's token finds its outcome", SKIP, "no trade on a V2 market was returned yet")
    if asked:
        rep.add("trades can be asked for by Gamma's condition id", PASS if answered else FAIL,
                f"{answered} of {asked} V2 markets returned trades")

    # 5. Order books, by the id the market trades under.
    books = with_depth = 0
    for m, _ in open_v2:
        book = await app.clob.book(m.token_ids[0]) if m.token_ids else None
        books += book is not None
        with_depth += bool(book is not None and (book.asks or book.bids) and book.tick_size)
    if not open_v2:
        rep.add("order books for V2 ids", SKIP, "no open V2 market in the sample")
    else:
        rep.add("order books for V2 ids", PASS if with_depth else FAIL,
                f"{with_depth} of {len(open_v2)} open V2 markets returned a book with prices and a tick size "
                f"({books} returned a book at all)")

    # 6. Price history (closing prices and the prices 1, 5 and 30 minutes after a fill).
    priced = 0
    probe = (open_v2 + closed_v2)[:sample]
    for m, _ in probe:
        at = (m.closed_time or now) - 120
        point = await app.data.price_at(m.token_ids[0], at) if m.token_ids else None
        priced += point is not None and 0 <= point.price <= 1
    rep.add("price history for V2 ids", PASS if priced else FAIL, f"{priced} of {len(probe)} V2 markets returned a price")

    # 7. Resolution: the payout our own settlement code ends up with.
    said_resolved = [mr for mr in closed_v2 if str(mr[1].get("resolutionStatus") or "").lower() == "resolved"]
    if not said_resolved:
        rep.add("V2 markets settle", SKIP, f"no resolved V2 market yet ({len(closed_v2)} closed in the sample)")
    else:
        ids = [m.condition_id for m, _ in said_resolved]
        direct = {str(r.get("condition_id") or "").lower() for r in await app.data.resolutions(ids)
                  if parse_resolution(r, n_outcomes=2) is not None}
        stored = await app.markets.ensure(ids, refresh_after=0)
        settled, problems = 0, []
        for m, raw in said_resolved:
            got = stored.get(m.condition_id)
            pps = got.payout_for(got.outcome_index_of(m.token_ids[0])) if got is not None and m.token_ids else None
            if got is None or not got.resolved or pps is None:
                problems.append(m.slug or m.condition_id[:14])
            else:
                settled += 1
        via = f"{len(direct)} by /v2/resolutions, the rest by Gamma's resolutionStatus"
        rep.add("V2 markets settle", FAIL if problems else PASS,
                f"{settled} of {len(said_resolved)} resolved V2 markets got a payout ({via})"
                + (f"; without one: {', '.join(problems[:3])}" if problems else ""))

    # 8. The backstop route: the same fill in the wallet's own activity, under the same ids.
    sample_row = next((r for rows in trades.values() for r in rows if r.wallet and r.tx_hash), None)
    if sample_row is None:
        rep.add("wallet activity names a V2 trade the same way", SKIP, "no V2 trade with a wallet to look up yet")
    else:
        acts, _ = await app.data.activity(sample_row.wallet, start=sample_row.ts - 1, end=sample_row.ts + 1)
        same = [a for a in acts if a.tx_hash == sample_row.tx_hash and a.token_id == sample_row.token_id
                and a.condition_id == sample_row.condition_id]
        rep.add("wallet activity names a V2 trade the same way", PASS if same else FAIL,
                f"wallet {sample_row.wallet[:10]}…: " + ("found the same fill with the same market and token ids"
                                                          if same else f"{len(acts)} rows, none matched the feed's fill"))

    fees = sorted({(bool(raw.get("feesEnabled")), (raw.get("feeSchedule") or {}).get("rate")) for _, raw in v2.values()},
                  key=str)
    rep.notes.append(f"fee settings seen on V2 markets (enabled, rate): {fees}")
    return rep
