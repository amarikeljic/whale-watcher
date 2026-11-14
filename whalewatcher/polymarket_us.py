"""v2.1 Part 3b, row A: the same whale signals, priced on Polymarket US.

Polymarket US is a separate, CFTC-regulated exchange with its own order books,
fees and markets. Nobody's trades are visible there (docs/polymarket-us.md), so
there is nothing to copy ON it. What can be measured is what it would have cost
to act there on a signal seen on the international site. That is row A.

For every signal the ``single`` runner makes, this module:

  1. finds the same market on Polymarket US by a deterministic rule on its slug,
     and refuses anything it cannot line up exactly (``not_on_us``);
  2. reads that market's order book from the PUBLIC gateway (no key, no account);
  3. fills the same fixed stake, fill-or-kill, under the same limit as the bot
     (the whale's price + max_slippage), at US fees;
  4. records both venues' best asks and the gap between them.

The result is one row per signal in ``bot_trades`` under the runner ``us``, so
every report, the dashboard and the small wallet treat it like any other runner
and never mix it with the international one.

READ ONLY. Every call here is a GET to https://gateway.polymarket.us, the public
market-data host. The trading host (api.polymarket.us) is never contacted: it
needs an account and a key that can place orders, and this project holds neither.

Two things about the venue that are easy to get wrong:

  * One instrument per market. There are no separate Yes and No shares: the other
    side is a SHORT of the same instrument, filled against its bids, and costs
    ``1 - bid`` a contract. (https://docs.polymarket.us/learn/trading/basics/buying-yes-vs-selling-no.md)
  * Fees are one coefficient for every market, 0.095 for an account under $10,000
    of monthly volume: ``fee = 0.095 x contracts x p x (1 - p)``, rounded to the
    cent, half to even. (https://docs.polymarket.us/fees.md, read 2026-11-12.)
    That is two to three times the international fee.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .http import HttpClient, HttpError
from .horizon import NO_END_DATE, TOO_LONG, resolution_type
from .polymarket import Book, Level, MarketInfo
from .timeutil import parse_time

log = logging.getLogger(__name__)

RUNNER = "us"
STRATEGY = "flat_hold"
NOT_ON_US, NOT_SAME_BET, US_EXCLUDED = "not_on_us", "not_same_bet", "us_excluded"
GAME_TIME_TOLERANCE = 6 * 3600      # two venues' kickoff times for one game never differ by this much
OPEN = "MARKET_STATUS_OPEN"


# -- what the gateway returns -----------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class UsSide:
    description: str
    long: bool
    names: tuple[str, ...]      # every name the side goes by: description, team name, alias, abbreviation


@dataclass(slots=True)
class UsMarket:
    slug: str
    question: str
    category: str
    market_type: str
    status: str
    closed: bool
    game_start: int | None
    end_date: int | None
    tick: float | None
    min_qty: float | None
    fee_coefficient: float | None
    outcomes: list[str]
    sides: list[UsSide]
    description: str = ""          # the market's resolution rules, as the venue words them

    @property
    def open(self) -> bool:
        return self.status == OPEN and not self.closed

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> UsMarket:
        sides = []
        for s in raw.get("marketSides") or []:
            team = s.get("team") or {}
            names = [s.get("description"), team.get("name"), team.get("alias"), team.get("safeName"),
                     team.get("abbreviation"), team.get("displayAbbreviation")]
            sides.append(UsSide(str(s.get("description") or ""), bool(s.get("long")),
                                tuple(str(n) for n in names if n)))
        try:
            outcomes = [str(o) for o in json.loads(raw.get("outcomes") or "[]")]
        except (TypeError, ValueError):
            outcomes = []
        return cls(slug=str(raw.get("slug") or ""), question=str(raw.get("question") or ""),
                   category=str(raw.get("category") or ""), market_type=str(raw.get("marketType") or ""),
                   status=str(raw.get("status") or ""), closed=bool(raw.get("closed")),
                   game_start=parse_time(raw.get("gameStartTime")), end_date=parse_time(raw.get("endDate")),
                   tick=_num(raw.get("orderPriceMinTickSize")), min_qty=_num(raw.get("minimumTradeQty")),
                   fee_coefficient=_num(raw.get("feeCoefficient")), outcomes=outcomes, sides=sides,
                   description=str(raw.get("description") or ""))


@dataclass(slots=True)
class UsBook:
    slug: str
    bids: list[tuple[float, float]]       # (price, contracts), best (highest) first
    offers: list[tuple[float, float]]     # best (lowest) first
    state: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any], slug: str) -> UsBook:
        def levels(key: str) -> list[tuple[float, float]]:
            out = []
            for lv in raw.get(key) or []:
                px, qty = _num(lv.get("px")), _num(lv.get("qty"))
                if px is not None and qty is not None and 0 < px < 1 and qty > 0:
                    out.append((px, qty))
            return out

        return cls(slug, sorted(levels("bids"), key=lambda x: -x[0]), sorted(levels("offers"), key=lambda x: x[0]),
                   str(raw.get("state") or ""))

    def best_bid_for(self, long: bool) -> float | None:
        """What the side bought could be sold for now: the long side's best bid, or for
        the short side 1 - the best offer (buying the instrument back closes the short)."""
        if long:
            return self.bids[0][0] if self.bids else None
        return round(1 - self.offers[0][0], 6) if self.offers else None

    def asks_for(self, long: bool) -> list[tuple[float, float]]:
        """What a buyer of this side pays, cheapest first. The long side lifts the offers.
        The other side is a short of the same instrument: it hits the bids, and a
        contract shorted at a bid of b costs 1 - b."""
        if long:
            return list(self.offers)
        return [(round(1 - price, 6), qty) for price, qty in self.bids]


class UsGateway:
    """The public Polymarket US market-data API. GET only, no credentials.
    https://docs.polymarket.us/api-reference/introduction.md (the "Public API")."""

    def __init__(self, http: HttpClient, base: str = "https://gateway.polymarket.us") -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def markets(self, slugs: list[str]) -> dict[str, UsMarket]:
        # https://docs.polymarket.us/api-reference/markets/get-markets.md  GET /v1/markets?slug=a&slug=b
        # Returns resolved markets too. A slug that does not exist is simply absent.
        if not slugs:
            return {}
        payload = await self.http.get_json(f"{self.base}/v1/markets", {"slug": sorted(set(slugs)), "limit": 100})
        found = [UsMarket.from_api(m) for m in (payload or {}).get("markets") or []]
        return {m.slug: m for m in found if m.slug}

    async def book(self, slug: str, *, nonce: str | None = None) -> UsBook | None:
        # https://docs.polymarket.us/api-reference/markets/get-market-book.md  GET /v1/markets/{slug}/book
        # Served with `cache-control: public, max-age=30`: a plain request was 29 s old
        # when checked (2026-11-12). A unique query parameter gets a fresh book; the
        # owner approved that, there being no other way without a trading key.
        try:
            payload = await self.http.get_json(f"{self.base}/v1/markets/{slug}/book", {"_t": nonce} if nonce else None)
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        data = (payload or {}).get("marketData")
        return UsBook.from_api(data, slug) if data else None

    async def settlement(self, slug: str) -> float | None:
        # https://docs.polymarket.us/api-reference/markets/get-market-settlement.md
        # GET /v1/markets/{slug}/settlement -> {"slug", "settlement": 1} once settled, 404 before.
        # The value is what the LONG side pays per contract.
        try:
            payload = await self.http.get_json(f"{self.base}/v1/markets/{slug}/settlement")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return _num((payload or {}).get("settlement"))


# -- fees ---------------------------------------------------------------------------------------------


def us_taker_fee(fills: list[tuple[float, float]], theta: float) -> float:
    """``theta x contracts x p x (1 - p)`` over the fills, to the cent, half to even.

    https://docs.polymarket.us/fees.md: each fill is charged its rounded fee, adjusted
    so the order's total never exceeds the rounding of the exact total. That cap is
    what is charged here: the most the order can cost."""
    exact = sum(Decimal(str(theta)) * Decimal(str(c)) * Decimal(str(p)) * (Decimal(1) - Decimal(str(p)))
                for p, c in fills if 0 < p < 1 and c > 0)
    return float(Decimal(exact).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)) if theta > 0 else 0.0


# -- matching an international market to its US market ----------------------------------------------

_UPDOWN = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m|1h|4h|1d)-(\d{10})$")
_UPDOWN_PREFIX = {"btc": "cpc-btc", "eth": "cpceth-eth", "sol": "cpcsol-sol", "xrp": "cpcxrp-xrp"}
_GAME = re.compile(r"^(.+)-(\d{4}-\d{2}-\d{2})$")


_TENNIS = ("atp", "wta")


def _name_codes(name: str) -> list[str]:
    """The ways a player's name can become a six-letter code: first name + last name,
    and, for three names or more, first name + second name."""
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in name.replace("-", " ").split()]
    parts = [w for w in parts if w]
    if len(parts) < 2:
        return []
    codes = [parts[0][:3] + parts[-1][:3]]
    if len(parts) > 2:
        codes.append(parts[0][:3] + parts[1][:3])
    return codes


def _players(market: MarketInfo) -> tuple[list[str], list[str]] | None:
    title = (market.event_title or market.question or "").split(":")[-1]
    names = re.split(r"\s+vs\.?\s+", title.strip(), flags=re.IGNORECASE)
    if len(names) != 2:
        return None
    one, two = _name_codes(names[0]), _name_codes(names[1])
    return (one, two) if one and two else None


@dataclass(slots=True)
class Candidates:
    rule: str                       # game_winner | tennis_winner | three_way | map_winner | crypto_updown | none
    slugs: list[str] = field(default_factory=list)
    same_bet: bool = True
    why_not: str = ""
    date: str = ""                  # the international date, when the rule also tries the days either side
    exact: set[str] = field(default_factory=set)    # the slugs on that date itself

    def __post_init__(self) -> None:
        if not self.exact and self.date:
            self.exact = {s for s in self.slugs if f"-{self.date}" in s}


def candidates(market: MarketInfo) -> Candidates:
    """US market slugs this international market could be, by rule. No search, no
    guessing by title: a rule either applies to the slug or the market is not matched."""
    slug = market.slug or ""
    m = _UPDOWN.match(slug)
    if m:
        asset, horizon, start = m.group(1), m.group(2), int(m.group(3))
        at = datetime.fromtimestamp(start, UTC)
        # Same window, but the US market settles on the CME CF index (BRTI and its
        # siblings) and the international one on a Chainlink stream: on a close
        # window they can settle differently. Not the same bet.
        return Candidates("crypto_updown", [f"{_UPDOWN_PREFIX[asset]}-updown-{horizon}-{at:%Y-%m-%d-%H%M}z"],
                          same_bet=False)
    if market.sports_market_type == "moneyline" and market.event_slug and slug == market.event_slug:
        g = _GAME.match(slug)
        if g and slug.split("-", 1)[0] in _TENNIS:
            # Tennis: the US slug is built from the players' names, three letters of the
            # first name and three of the last ("Sebastian Baez" is sebbae), where the
            # international one truncates surnames. A player with several names can be
            # cut two ways, so both are tried; the sides are checked by name afterwards.
            league, day = slug.split("-", 1)[0], date.fromisoformat(g.group(2))
            players = _players(market)
            if players is None:
                return Candidates("none", why_not="a tennis match whose players could not be read from its title")
            slugs = [f"aec-{league}-{x}-{y}-{d}" for d in (day, day - timedelta(days=1), day + timedelta(days=1))
                     for one, two in (players, players[::-1]) for x in one for y in two]
            return Candidates("tennis_winner", slugs, date=str(day))
        if g:
            # The international slug carries the UTC date of the game, the US slug the
            # US Eastern date: an evening game is one day earlier there. Kickoff times
            # are compared afterwards, so trying a neighbouring day cannot match another game.
            day = date.fromisoformat(g.group(2))
            return Candidates("game_winner", [f"aec-{g.group(1)}-{d}" for d in
                                              (day, day - timedelta(days=1), day + timedelta(days=1))],
                              date=str(day))
        return Candidates("none", why_not="a game winner whose slug carries no date")
    child = re.fullmatch(re.escape(market.event_slug or "-") + r"-game(\d{1,2})", slug)
    if market.sports_market_type == "child_moneyline" and child and _GAME.match(market.event_slug):
        # Esports maps: "Counter-Strike: Vitality vs MOUZ - Map 2 Winner". The US venue
        # lists one Yes/No instrument, "Will Vitality win Map 2 vs MOUZ?", under the
        # match's own slug. The side is read from that question (map_side).
        g = _GAME.match(market.event_slug)
        day, number = date.fromisoformat(g.group(2)), child.group(1)
        return Candidates("map_winner", [f"astatc-{g.group(1)}-{d}-{word}{number}" for d in
                                         (day, day - timedelta(days=1), day + timedelta(days=1))
                                         for word in ("map", "game")], date=str(day))
    if market.sports_market_type == "moneyline" and market.event_slug and slug.startswith(market.event_slug + "-"):
        # Football with a draw: three markets ("Will A win?", "Will B win?", "Draw?"), each
        # its own Yes/No. The US venue lists the same three as one instrument each.
        g, suffix = _GAME.match(market.event_slug), slug[len(market.event_slug) + 1:]
        if g and re.fullmatch(r"[a-z0-9]{2,8}", suffix):
            day = date.fromisoformat(g.group(2))
            return Candidates("three_way", [f"atc-{g.group(1)}-{d}-{suffix}" for d in
                                            (day, day - timedelta(days=1), day + timedelta(days=1))],
                              date=str(day))
    if market.sports_market_type:
        return Candidates("none", why_not=f"no rule yet for sports market type {market.sports_market_type}")
    return Candidates("none", why_not=f"no rule for {market.category or 'this kind of'} markets")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_MAP_QUESTION = re.compile(r"^Will (.+?) win (?:Map|Game) (\d{1,2}) (?:vs\.?|against) (.+?)\??$", re.IGNORECASE)


def _same_name(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or a in b.split() or b in a.split() or a.replace(" ", "") == b.replace(" ", ""))


def map_side(us: UsMarket, wanted: str, number: str | None = None) -> bool | None:
    """The side of "Will <team> win Map N vs <other>?" that ``wanted`` (a team name) is:
    True (long: <team> wins it), False (short: <other> wins it), None if it is neither,
    both, or the map number differs."""
    q = _MAP_QUESTION.match(us.question.strip())
    if q is None or (number is not None and q.group(2) != number):
        return None
    team, other = _same_name(wanted, q.group(1)), _same_name(wanted, q.group(3))
    return True if team and not other else False if other and not team else None


def side_for(us: UsMarket, market: MarketInfo, outcome_index: int | None, rule: str) -> bool | None:
    """True for the long side, False for the short side, None when the outcome cannot
    be lined up with exactly one side. Never a guess."""
    if outcome_index is None or outcome_index >= len(market.outcomes):
        return None
    wanted = _norm(market.outcomes[outcome_index])
    if rule == "crypto_updown":
        return {"up": True, "down": False}.get(wanted)
    if rule == "three_way":
        return {"yes": True, "no": False}.get(wanted)     # one instrument per outcome: No is a short of it
    if rule == "map_winner":
        number = re.search(r"-game(\d{1,2})$", market.slug or "")
        return map_side(us, market.outcomes[outcome_index], number.group(1) if number else None)
    hits = []
    for side in us.sides:
        names = {_norm(n) for n in side.names}
        if wanted in names or any(wanted and (wanted in n.split() or n in wanted.split()) for n in names if n):
            hits.append(side.long)
    return hits[0] if len(hits) == 1 else None


@dataclass(slots=True)
class Match:
    market: UsMarket | None
    long: bool | None
    rule: str
    same_bet: bool = True
    why_not: str = ""

    @property
    def found(self) -> bool:
        return self.market is not None and self.long is not None


def pick(cands: Candidates, found: dict[str, UsMarket], market: MarketInfo, outcome_index: int | None) -> Match:
    if cands.rule == "none":
        return Match(None, None, cands.rule, why_not=cands.why_not)
    for slug in cands.slugs:
        us = found.get(slug)
        if us is None:
            continue
        if cands.rule in ("game_winner", "tennis_winner", "three_way", "map_winner"):
            wanted_type = {"three_way": "drawable_outcome", "map_winner": "props"}.get(cands.rule, "moneyline")
            if us.market_type != wanted_type:
                continue
            timed = bool(market.game_start_time and us.game_start)
            if timed and abs(us.game_start - market.game_start_time) > GAME_TIME_TOLERANCE:
                continue                # same teams, another day
            if not timed and cands.exact and slug not in cands.exact:
                continue                # no kickoff to compare: a neighbouring date could be another game
        long = side_for(us, market, outcome_index, cands.rule)
        if long is None:
            return Match(None, None, cands.rule, cands.same_bet,
                         f"{us.slug} exists but its sides do not line up with {market.outcomes}")
        return Match(us, long, cands.rule, cands.same_bet)
    return Match(None, None, cands.rule, cands.same_bet, "no market with that slug on Polymarket US")


# -- row A ----------------------------------------------------------------------------------------------


class UsRowA:
    """Prices every signal of the ``single`` runner on Polymarket US and keeps the
    ledger rows of the ``us`` runner. Never in the bot's way: ``submit`` returns at
    once and the work happens in the background."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.cfg = app.cfg.polymarket_us
        self.gateway: UsGateway = app.us
        self._tasks: set[asyncio.Task] = set()
        self._markets: dict[str, tuple[float, UsMarket | None]] = {}     # slug -> (looked up at, market)
        self._calls = 0

    # -- entering ---------------------------------------------------------------------------------

    def submit(self, alert: Any, market: MarketInfo | None, *, detected_at: float) -> None:
        task = asyncio.create_task(self._guarded(dict(alert), market, detected_at))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _guarded(self, alert: dict, market: MarketInfo | None, detected_at: float) -> None:
        try:
            await self.enter(alert, market, detected_at=detected_at)
        except Exception as exc:  # noqa: BLE001 - a comparison row is never worth the runner's life
            log.warning("Polymarket US pricing of alert %s failed: %s", alert.get("id"), exc)
            self._insert(alert, "MISSED", f"error: {type(exc).__name__}: {exc}"[:200], detected_at=detected_at)

    def _fee_seen(self, m: UsMarket) -> None:
        """Part 4 #14: a Polymarket US market's published coefficient, and its history."""
        db, now = self.app.db, int(self.app.clock())
        try:
            old = db.one("SELECT fee_coefficient FROM us_fee_seen WHERE slug = ?", (m.slug,))
            if old is not None and old["fee_coefficient"] is not None and m.fee_coefficient is not None \
                    and abs(old["fee_coefficient"] - m.fee_coefficient) > 1e-12:
                db.execute("INSERT INTO fee_changes (seen_at, venue, market, field, old_value, new_value) "
                           "VALUES (?, 'polymarket_us', ?, 'fee_coefficient', ?, ?)",
                           (now, m.slug, old["fee_coefficient"], m.fee_coefficient))
                log.warning("fee change: Polymarket US %s coefficient %s -> %s", m.slug, old["fee_coefficient"],
                            m.fee_coefficient)
            db.execute("INSERT INTO us_fee_seen (slug, fee_coefficient, seen_at) VALUES (?, ?, ?) "
                       "ON CONFLICT(slug) DO UPDATE SET fee_coefficient = excluded.fee_coefficient, "
                       "seen_at = excluded.seen_at", (m.slug, m.fee_coefficient, now))
        except Exception:  # noqa: BLE001 - a log of fees is never worth a missed price
            log.exception("could not record the fee of Polymarket US market %s", m.slug)

    async def _lookup(self, slugs: list[str]) -> dict[str, UsMarket]:
        now = self.app.wall()
        fresh = {s: m for s, (at, m) in self._markets.items() if now - at < self.cfg.market_cache_seconds}
        missing = [s for s in slugs if s not in fresh]
        if missing:
            got = await self.gateway.markets(missing)
            for s in missing:
                self._markets[s] = (now, got.get(s))
                fresh[s] = got.get(s)
                if got.get(s) is not None:
                    self._fee_seen(got[s])
        return {s: fresh[s] for s in slugs if fresh.get(s) is not None}

    async def enter(self, alert: dict, market: MarketInfo | None, *, detected_at: float) -> str:
        """One ``us`` row for this signal. Returns its status."""
        from .paper import rounded, walk_asks   # here, not at the top: paper imports app, which imports this module
        from .precision import SHARE_STEP
        app, bot = self.app, self.app.cfg.bot
        base = app.db.one("SELECT * FROM bot_trades WHERE runner = 'single' AND strategy = ? AND alert_id = ?",
                          (STRATEGY, alert["id"]))
        intl_ask = base["best_ask"] if base else alert.get("alert_best_ask")
        horizon = (base["scheduled_end"], base["days_to_close"]) if base else (None, None)
        common = dict(detected_at=detected_at, intl_ask=intl_ask, horizon=horizon)
        # The gates that have nothing to do with the venue are the baseline's own.
        from .followup import SKIP_CODES
        if base is not None and base["skip_code"] in (TOO_LONG, NO_END_DATE, *SKIP_CODES):
            return self._insert(alert, "SKIPPED", base["reason"], skip_code=base["skip_code"], **common)
        if base is not None and base["status"] == "MISSED" and (base["reason"] or "").startswith("too late"):
            return self._insert(alert, "MISSED", base["reason"], **common)
        if market is None:
            return self._insert(alert, "MISSED", "market metadata unavailable", **common)

        cands = candidates(market)
        found = await self._lookup(cands.slugs) if cands.slugs else {}
        match = pick(cands, found, market, market.outcome_index_of(alert["token_id"]))
        if not match.found:
            return self._insert(alert, "SKIPPED", f"{NOT_ON_US}: {match.why_not}", skip_code=NOT_ON_US,
                                rule=match.rule, **common)
        us, long = match.market, bool(match.long)
        common.update(us=us, long=long, rule=match.rule)
        if not match.same_bet and not self.cfg.include_different_index:
            return self._insert(alert, "SKIPPED", f"{NOT_SAME_BET}: {us.slug} covers the same window but settles on "
                                "a different price index", skip_code=NOT_SAME_BET, **common)
        excluded = {c.lower() for c in self.cfg.exclude_categories}
        if (market.category or "").lower() in excluded or us.category.lower() in excluded:
            return self._insert(alert, "SKIPPED", f"{US_EXCLUDED}: {market.category or us.category} is excluded on "
                                "Polymarket US (polymarket_us.exclude_categories)", skip_code=US_EXCLUDED, **common)
        if not us.open:
            return self._insert(alert, "MISSED", f"the Polymarket US market is not open ({us.status or 'closed'})",
                                **common)

        self._calls += 1
        nonce = f"{int(app.wall() * 1000)}{self._calls}" if self.cfg.bypass_cache else None
        book = await self.gateway.book(us.slug, nonce=nonce)
        decided_at = app.wall()
        asks = book.asks_for(long) if book else []
        common.update(decided_at=decided_at, asks=asks, us_bid=book.best_bid_for(long) if book else None)
        if not asks:
            return self._insert(alert, "MISSED", "nothing offered on the Polymarket US book", **common)
        stake = min(bot.flat_hold.stake_usd, bot.flat_hold.max_stake_usd)
        limit = alert["whale_price"] + bot.max_slippage
        side_book = Book(token_id=us.slug, ts=None, bids=[], asks=[Level(p, q) for p, q in asks])
        walk = walk_asks(side_book, stake, limit_price=limit)
        if walk is None:
            free = walk_asks(side_book, stake)
            if free is None:
                return self._insert(alert, "MISSED", f"book too thin for ${stake:g}", **common)
            return self._insert(alert, "MISSED", f"price ran: filling ${stake:g} costs {free.vwap:.3f} a share, "
                                f"limit {limit:.3f}", would_pay=free.vwap, **common)
        # Contracts in steps of the market's minimum (precision.py); no amount rounding is documented.
        walk = rounded(walk, step=us.min_qty or SHARE_STEP, decimals=None)
        if walk is None:
            return self._insert(alert, "MISSED", f"below the minimum order size ({us.min_qty or SHARE_STEP:g} "
                                "contracts)", **common)
        if us.min_qty and walk.contracts < us.min_qty - 1e-9:
            return self._insert(alert, "MISSED", f"below the minimum order size ({walk.contracts:.2f} of "
                                f"{us.min_qty:g} contracts)", **common)
        theta = us.fee_coefficient if us.fee_coefficient is not None else self.cfg.fallback_fee_coefficient
        return self._insert(alert, "FILLED", None, walk=walk, fees=us_taker_fee(walk.fills, theta), theta=theta,
                            **common)

    def _insert(self, alert: dict, status: str, reason: str | None, *, detected_at: float,
                decided_at: float | None = None, skip_code: str | None = None, us: UsMarket | None = None,
                long: bool | None = None, rule: str | None = None, intl_ask: float | None = None,
                asks: list[tuple[float, float]] | None = None, walk: Any = None, fees: float | None = None,
                theta: float | None = None, would_pay: float | None = None, us_bid: float | None = None,
                horizon: tuple[Any, Any] = (None, None)) -> str:
        app, bot = self.app, self.app.cfg.bot
        decided_at = app.wall() if decided_at is None else decided_at
        best = asks[0][0] if asks else None
        signal_ts = alert.get("trigger_fill_ts") or alert.get("last_fill_ts")
        whale = alert.get("whale_price")
        slip = max(0.0, (walk.vwap - best) * walk.contracts) if walk is not None and best is not None else None
        app.db.execute(
            "INSERT OR IGNORE INTO bot_trades (runner, strategy, alert_id, wallet, condition_id, token_id, event_key, "
            "status, reason, signal_ts, detected_at, decided_at, stake_usd, whale_price, best_ask, fill_price, "
            "contracts, fees, cost, levels, human_done, whale_shares, updated_at, fee_rate, fee_source, "
            "slippage_cost, skip_code, scheduled_end, days_to_close, asks_json, limit_price, min_order_size, "
            "us_slug, us_side, us_rule, intl_best_ask, price_gap, us_rules, us_end_date, us_best_bid, tick_size) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (RUNNER, STRATEGY, alert["id"], alert["wallet"], alert["condition_id"], alert["token_id"],
             self._event_key(alert), status, reason, signal_ts, detected_at, decided_at,
             min(bot.flat_hold.stake_usd, bot.flat_hold.max_stake_usd), whale, best,
             walk.vwap if walk is not None else would_pay, walk.contracts if walk is not None else None, fees,
             (walk.vwap * walk.contracts + fees) if walk is not None else None,
             walk.levels if walk is not None else None, alert.get("whale_shares"), app.clock(),
             theta if walk is not None else None, "us" if walk is not None else None, slip, skip_code,
             horizon[0], horizon[1], json.dumps([[p, q] for p, q in asks[:25]]) if asks else None,
             whale + bot.max_slippage if whale is not None else None, us.min_qty if us else None,
             us.slug if us else None, None if long is None else ("long" if long else "short"), rule, intl_ask,
             best - intl_ask if best is not None and intl_ask is not None else None,
             us.description or None if us else None, us.end_date if us else None, us_bid, us.tick if us else None))
        return status

    def _event_key(self, alert: dict) -> str:
        row = self.app.db.one("SELECT event_slug FROM poly_markets WHERE condition_id = ?", (alert["condition_id"],))
        return (row["event_slug"] if row else None) or alert["condition_id"]

    # -- settling ---------------------------------------------------------------------------------------

    async def settle(self) -> int:
        """Close every ``us`` position whose US market has settled, at what the US
        venue itself paid: the long side gets ``settlement``, the short side the rest."""
        from .paper import result_label
        app = self.app
        rows = app.db.query("SELECT * FROM bot_trades WHERE runner = ? AND status = 'FILLED' AND closed_at IS NULL "
                            "AND us_slug IS NOT NULL", (RUNNER,))
        results: dict[str, float | None] = {}
        for slug in {r["us_slug"] for r in rows}:
            try:
                results[slug] = await self.gateway.settlement(slug)
            except Exception as exc:  # noqa: BLE001 - try again next time
                log.warning("could not read the Polymarket US settlement of %s: %s", slug, exc)
                results[slug] = None
        done = 0
        for t in rows:
            value = results.get(t["us_slug"])
            if value is None:
                continue
            pps = value if t["us_side"] == "long" else 1 - value
            # Part 4 #9, #13. The US venue publishes no dispute history: disputed is never set here.
            kind = resolution_type(payouts=[value, 1 - value], scheduled_end=t["scheduled_end"],
                                   resolved_at=app.clock())
            app.db.execute(
                "UPDATE bot_trades SET closed_at = ?, result = ?, payout_per_share = ?, pnl = ?, resolved_at = ?, "
                "updated_at = ?, paid_at = ?, resolution_type = ? WHERE id = ? AND closed_at IS NULL",
                (app.clock(), result_label(pps), pps, t["contracts"] * pps - t["cost"], app.clock(), app.clock(),
                 app.clock(), kind, t["id"]))
            done += 1
        return done


# -- reports --------------------------------------------------------------------------------------------


@dataclass
class Coverage:
    signals: int = 0
    on_us: int = 0                 # a US market was found and the sides lined up
    filled: int = 0
    missed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    gaps: list[float] = field(default_factory=list)


def coverage(rows: list[dict]) -> Coverage:
    """How much of the signal stream exists on Polymarket US, from the ``us`` rows."""
    c = Coverage()
    for t in rows:
        c.signals += 1
        code = t.get("skip_code")
        if t.get("us_slug"):
            c.on_us += 1
        if t["status"] == "FILLED":
            c.filled += 1
        elif t["status"] == "MISSED":
            c.missed += 1
        if code:
            why = code if code != NOT_ON_US else (t.get("reason") or code).split(":", 1)[-1].strip()
            why = re.sub(r"aec-[a-z0-9-]+ exists but its sides do not line up.*", "the sides do not line up", why)
            c.reasons[f"{code}: {why}" if code == NOT_ON_US else code] = c.reasons.get(
                f"{code}: {why}" if code == NOT_ON_US else code, 0) + 1
        if t.get("price_gap") is not None:
            c.gaps.append(t["price_gap"])
    return c


async def match_report(app: Any, days: int = 14) -> list[str]:
    """The match rate on the alerts of the last ``days``: for the acceptance check and
    for a database with no ``us`` rows yet. Reads the gateway; writes nothing."""
    since = app.clock() - days * 86_400
    alerts = app.db.query("SELECT condition_id, token_id, COUNT(*) n FROM alerts WHERE status IN "
                          "('RECORDED', 'POSTED', 'UNSENT') AND detected_at >= ? GROUP BY condition_id, token_id",
                          (since,))
    markets = app.markets.get_many({a["condition_id"] for a in alerts})
    wanted: dict[str, None] = {}
    plans = []
    for a in alerts:
        m = markets.get(a["condition_id"])
        c = candidates(m) if m is not None else Candidates("none", why_not="market metadata unavailable")
        plans.append((a, m, c))
        wanted.update(dict.fromkeys(c.slugs))
    found: dict[str, UsMarket] = {}
    slugs = list(wanted)
    for i in range(0, len(slugs), 20):
        found.update(await app.us.markets(slugs[i:i + 20]))
    total = sum(a["n"] for a in alerts)
    by: dict[tuple[str, str], list[int]] = {}
    for a, m, c in plans:
        match = pick(c, found, m, m.outcome_index_of(a["token_id"])) if m is not None else Match(None, None, "none")
        kind = (m.category if m else None) or "unknown"
        outcome = ("same bet" if match.same_bet else "same window, different index") if match.found else \
            f"not on US: {re.sub(r'aec-[a-z0-9-]+ exists but its sides.*', 'the sides do not line up', match.why_not or c.why_not)}"
        row = by.setdefault((kind, outcome), [0, 0])
        row[0] += 1
        row[1] += a["n"]
    same = sum(v[1] for (_, o), v in by.items() if o == "same bet")
    window = sum(v[1] for (_, o), v in by.items() if o.startswith("same window"))
    lines = [f"Polymarket US match rate, alerts of the last {days} days: {total} alerts in {len(alerts)} outcomes",
             f"  the same bet exists on Polymarket US: {same} alerts ({same / total:.1%})" if total else "  no alerts",
             f"  same window but a different price index (crypto up/down): {window} alerts "
             f"({window / total:.1%})" if total else "",
             f"  not on Polymarket US: {total - same - window} alerts ({(total - same - window) / total:.1%})"
             if total else "", "", f"  {'category':<14}{'result':<64}{'outcomes':>9}{'alerts':>8}"]
    for (kind, outcome), (n_out, n_alerts) in sorted(by.items(), key=lambda kv: -kv[1][1]):
        lines.append(f"  {kind:<14}{outcome[:62]:<64}{n_out:>9}{n_alerts:>8}")
    return [line for line in lines if line is not None]
