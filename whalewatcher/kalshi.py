"""Kalshi: public market data, game matching against Polymarket, and the taker fee.

Read-only. Only public market-data GET endpoints are used, which need no API key
(https://docs.kalshi.com/llms.txt, OpenAPI https://docs.kalshi.com/openapi.yaml,
Trade API v3.36.0, verified 2026-11-07). No order, portfolio or account endpoint
is ever called.

Matching covers game-winner markets (moneyline), where both venues list one
contract per side: on Polymarket either two outcomes named after the teams, or,
for soccer, one Yes/No market per team plus one for the draw; on Kalshi one
market per team (plus ``TIE`` in soccer) under an event such as
``KXEPLGAME-26MAY09LFCCFC``. A game matches when the league maps to the Kalshi
series, the dates are at most a day apart, and both teams pair with distinct
Kalshi markets by code or by name.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Any

from .polymarket import MarketInfo
from .timeutil import parse_time

if TYPE_CHECKING:
    from .app import App

# Polymarket event-slug prefix -> Kalshi game-winner series. Checked by hand
# against both venues' listings on 2026-11-07: the prefixes do not always mean
# what they look like ("col" is the UEFA Conference League, "lec" the Leagues Cup).
LEAGUE_SERIES: dict[str, tuple[str, ...]] = {
    "nfl": ("KXNFLGAME",), "nba": ("KXNBAGAME",), "cfb": ("KXNCAAFGAME",), "mlb": ("KXMLBGAME",),
    "nhl": ("KXNHLGAME",), "wnba": ("KXWNBAGAME",), "bkcba": ("KXCBAGAME",),
    "atp": ("KXATPMATCH", "KXATPCHALLENGERMATCH"), "wta": ("KXWTAMATCH", "KXWTACHALLENGERMATCH"),
    "ufc": ("KXUFCFIGHT",),
    "lol": ("KXLOLGAME",), "cs2": ("KXCS2GAME",), "dota2": ("KXDOTA2GAME",), "val": ("KXVALORANTGAME",),
    "epl": ("KXEPLGAME",), "lal": ("KXLALIGAGAME",), "bun": ("KXBUNDESLIGAGAME",), "sea": ("KXSERIEAGAME",),
    "fl1": ("KXLIGUE1GAME",), "ucl": ("KXUCLGAME",), "uel": ("KXUELGAME",), "col": ("KXUECLGAME",),
    "unl": ("KXUEFANLGAME",), "fifwc": ("KXWCGAME",), "fif": ("KXINTLFRIENDLYGAME", "KXFIFAGAME"),
    "mls": ("KXMLSGAME",), "es2": ("KXLALIGA2GAME",), "bl2": ("KXBUNDESLIGA2GAME",), "jap": ("KXJLEAGUEGAME",),
    "ja2": ("KXJ2LEAGUEGAME",), "tur": ("KXSUPERLIGGAME",), "tur2": ("KXTFF1LIGGAME",), "dfb": ("KXDFBPOKALGAME",),
    "arg": ("KXARGPREMDIVGAME",), "lec": ("KXLEAGUESCUPGAME",), "itsb": ("KXSERIEBGAME",),
    "swe": ("KXALLSVENSKANGAME",), "acle": ("KXAFCCLGAME",), "bra": ("KXBRASILEIROGAME",),
    "elc": ("KXEFLCHAMPIONSHIPGAME",), "efl": ("KXEFLCUPGAME",), "uwcl": ("KXUCLWGAME",), "mex": ("KXLIGAMXGAME",),
    "por": ("KXLIGAPORTUGALGAME",), "nor": ("KXELITESERIENGAME",), "pol": ("KXEKSTRAKLASAGAME",),
    "gre1": ("KXSLGREECEGAME",), "rou1": ("KXROML1GAME",), "chi": ("KXCHNSLGAME",), "chi1": ("KXCHLLDPGAME",),
    "sud": ("KXCONMEBOLSUDGAME",), "lib": ("KXCONMEBOLLIBGAME",), "ere": ("KXEREDIVISIEGAME",),
    "ecu1": ("KXECULPGAME",), "spl": ("KXSAUDIPLGAME",),
}
MONEYLINE_TYPES = ("moneyline",)
SAME_OPPONENT_NEXT_DAY = {"mlb"}   # baseball series: one game a day against the same team
TIE = "TIE"

# Taker fee per contract: fee_multiplier x 0.07 x P x (1 - P). The 0.07 is the
# general trading fee in https://kalshi.com/docs/kalshi-fee-schedule.pdf, which
# sits behind a bot check here. The same coefficient is in every secondary source
# checked (2026-11-07), and it agrees with the API's description of maker fees as
# a 0.25 (or 0.5) multiple. The series' own fee_multiplier (0.5 on KXMLBGAME)
# comes from GET /series/{ticker}. Rounded up to the cent, the most a member pays
# (https://docs.kalshi.com/getting_started/fee_rounding.md).
TAKER_FEE_COEFFICIENT = 0.07
_CENT = Decimal("0.01")


def taker_fee(contracts: float, price: float, multiplier: float = 1.0) -> float:
    if contracts <= 0 or not 0 < price < 1 or multiplier <= 0:
        return 0.0
    raw = Decimal(str(multiplier)) * Decimal(str(TAKER_FEE_COEFFICIENT)) * Decimal(str(contracts)) \
        * Decimal(str(price)) * (Decimal(1) - Decimal(str(price)))
    return float(raw.quantize(_CENT, rounding=ROUND_CEILING))


# -- Kalshi markets ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"), start=1)}
_EVENT_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")


def event_date(event_ticker: str, series: str) -> date | None:
    """``KXNFLGAME-26OCT04DALHOU`` -> 2026-10-04 (the game's local date, as Kalshi names it)."""
    rest = event_ticker[len(series) + 1:] if event_ticker.startswith(series + "-") else event_ticker.split("-", 1)[-1]
    m = _EVENT_DATE.match(rest)
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def _dollars(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> int | None:
    if not value:
        return None
    try:
        return parse_time(str(value))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    series: str
    code: str                     # the ticker's last part: a team code, or TIE
    name: str                     # yes_sub_title: "Liverpool", "New York G", "Tie"
    event_date: date | None
    status: str
    result: str                   # yes | no | "" (unsettled) | scalar
    settlement_value: float | None  # what one YES contract paid
    open_time: int | None
    close_time: int | None
    expected_expiration: int | None   # scheduled when listed: about when the game should end
    settled_at: int | None
    archived: bool                # settled before the historical cutoff: candles via /historical

    @classmethod
    def from_api(cls, raw: dict[str, Any], series: str, *, archived: bool) -> KalshiMarket:
        ticker = str(raw["ticker"])
        event = str(raw.get("event_ticker") or ticker.rsplit("-", 1)[0])
        return cls(
            ticker=ticker, event_ticker=event, series=series, code=ticker.rsplit("-", 1)[-1].upper(),
            name=str(raw.get("yes_sub_title") or raw.get("title") or ""), event_date=event_date(event, series),
            status=str(raw.get("status") or ""), result=str(raw.get("result") or ""),
            settlement_value=_dollars(raw.get("settlement_value_dollars")),
            open_time=_ts(raw.get("open_time")), close_time=_ts(raw.get("close_time")),
            expected_expiration=_ts(raw.get("expected_expiration_time")), settled_at=_ts(raw.get("settlement_ts")),
            archived=archived)

    @property
    def settled(self) -> bool:
        return self.result in ("yes", "no", "scalar") and self.settlement_value is not None

    def open_at(self, ts: int) -> bool:
        """Whether the market was trading at ``ts`` (opened, not yet closed)."""
        return (self.open_time is None or self.open_time <= ts) and (self.close_time is None or ts < self.close_time)

    def payout(self, side: str) -> float | None:
        """What one contract of ``side`` paid at settlement."""
        if not self.settled:
            return None
        return self.settlement_value if side == "yes" else 1.0 - self.settlement_value


# -- names ------------------------------------------------------------------------------------

_FILLER = {
    "fc", "cf", "sc", "afc", "ac", "as", "us", "ssc", "cd", "ca", "sd", "rc", "fk", "sk", "bv", "vfb", "vfl", "tsg",
    "sv", "ud", "cs", "club", "calcio", "futbol", "football", "de", "del", "the", "esports", "team", "gaming",
    "and", "of",
}
_ABBREV = {"st": ("state", "saint"), "utd": ("united",), "intl": ("international",), "mt": ("mount",)}


def tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t not in _FILLER and not t.isdigit()]


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) == 1:
        return long_.startswith(short)          # "Los Angeles L" -> Lakers, "New York G" -> Giants
    if long_ in _ABBREV.get(short, ()):
        return True
    return len(short) >= 3 and long_.startswith(short)   # "Inter" ~ "Internazionale", "Milan" ~ "Milano"


def name_similarity(a: str, b: str) -> int:
    """2: every token of each name pairs with one of the other's ("Inter Milan" ~ "FC Internazionale
    Milano"). 1: the shorter name is contained in the longer ("Virginia" in "West Virginia"). 0: neither."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) == 1 and len(short[0]) == 1:
        return 0                                # a bare initial says nothing
    if not all(any(_token_match(s, t) for t in long_) for s in short):
        return 0
    return 2 if all(any(_token_match(t, s) for s in short) for t in long_) else 1


def names_match(a: str, b: str) -> bool:
    return name_similarity(a, b) > 0


# -- Polymarket games ---------------------------------------------------------------------------

_SLUG = re.compile(r"^(?P<league>[a-z0-9]+)-(?P<body>.+?)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<suffix>[a-z0-9-]+))?$")


@dataclass(frozen=True, slots=True)
class PolyGame:
    league: str
    date: date
    codes: tuple[str, str]
    names: tuple[str, str]
    subject: int | str | None     # soccer: 0 or 1 (the team the market is about) or TIE; None: two-way
    game_start: int | None


def _title_teams(title: str) -> tuple[str, str] | None:
    t = title.split(": ", 1)[-1]
    t = re.split(r" \(| - ", t, maxsplit=1)[0]
    parts = re.split(r" vs\.? ", t)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def poly_game(m: MarketInfo) -> PolyGame | None:
    """What a Polymarket game-winner market is about, or None if it is not one we can match."""
    if m.sports_market_type not in MONEYLINE_TYPES or len(m.outcomes) != 2:
        return None
    s = _SLUG.match(m.slug or "")
    if not s or s.group("league") not in LEAGUE_SERIES:
        return None
    codes = tuple(s.group("body").split("-"))
    if len(codes) != 2:
        return None
    try:
        d = date.fromisoformat(s.group("date"))
    except ValueError:
        return None
    yes_no = [o.strip().lower() for o in m.outcomes] == ["yes", "no"]
    if not yes_no:
        return PolyGame(s.group("league"), d, codes, (m.outcomes[0], m.outcomes[1]), None, m.game_start_time)
    teams = _title_teams(m.event_title or "") or _title_teams(m.question or "")
    if teams is None:
        return None
    suffix = s.group("suffix") or ""
    if suffix == "draw" or "draw" in (m.question or "").lower():
        subject: int | str | None = TIE
    elif suffix == codes[0]:
        subject = 0
    elif suffix == codes[1]:
        subject = 1
    else:
        q = re.match(r"Will (.+?) win", m.question or "")
        hits = [i for i in (0, 1) if q and names_match(q.group(1), teams[i])]
        if len(hits) != 1:
            return None
        subject = hits[0]
    return PolyGame(s.group("league"), d, codes, teams, subject, m.game_start_time)


# -- matching -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Leg:
    """Buy ``side`` of Kalshi ``ticker`` when copying a Polymarket outcome."""

    ticker: str
    side: str          # yes | no
    method: str        # how the game was matched, for auditing


class KalshiIndex:
    """Kalshi game markets grouped by series and event date."""

    def __init__(self, markets: Iterable[KalshiMarket]) -> None:
        self.by_ticker: dict[str, KalshiMarket] = {}
        events: dict[str, list[KalshiMarket]] = defaultdict(list)
        for m in markets:
            self.by_ticker[m.ticker] = m
            events[m.event_ticker].append(m)
        self.by_day: dict[tuple[str, date], list[list[KalshiMarket]]] = defaultdict(list)
        for ms in events.values():
            first = ms[0]
            if first.event_date is not None:
                self.by_day[(first.series, first.event_date)].append(sorted(ms, key=lambda x: x.ticker))

    def candidates(self, league: str, day: date) -> list[tuple[int, list[KalshiMarket]]]:
        out = []
        for series in LEAGUE_SERIES.get(league, ()):
            # Venues can date a game a day apart (time zones, rescheduling), except where
            # the same two teams also play the next day.
            for delta in (0,) if league in SAME_OPPONENT_NEXT_DAY else (0, -1, 1):
                for ev in self.by_day.get((series, day + timedelta(days=delta)), []):
                    out.append((abs(delta), ev))
        return out


# Matching uses only what is known before the game: league, date, teams, and the
# market's scheduled expiry as a tie-break between games of the same two teams (an
# MLB series plays on consecutive days). Whether a copy can trade is a separate,
# per-alert question a live copier can answer too: was the market open then
# (``open_at``)? The actual close time is never used to pick a match.
CODE, FULL_NAME, PARTIAL_NAME = 6, 5, 4


def _team_score(code: str, name: str, km: KalshiMarket) -> int:
    if km.code == TIE:
        return 0
    if code and code.upper() == km.code:
        return CODE
    return {2: FULL_NAME, 1: PARTIAL_NAME}.get(name_similarity(name, km.name), 0)


def match(m: MarketInfo, index: KalshiIndex) -> dict[int, Leg] | None:
    """Polymarket outcome index -> the Kalshi contract that pays on the same result."""
    g = poly_game(m)
    if g is None:
        return None
    best: tuple | None = None
    for day_gap, event in index.candidates(g.league, g.date):
        teams = [km for km in event if km.code != TIE]
        if len(teams) < 2:
            continue
        for a, ka in enumerate(teams):
            sa = _team_score(g.codes[0], g.names[0], ka)
            if not sa:
                continue
            for b, kb in enumerate(teams):
                if b == a:
                    continue
                sb = _team_score(g.codes[1], g.names[1], kb)
                if not sb:
                    continue
                expiry = ka.expected_expiration or 0
                timing = abs(expiry - g.game_start) if g.game_start and expiry else 10**9
                key = (-(sa + sb), day_gap, timing)
                if best is None or key < best[0]:
                    best = (key, event, ka, kb, sa, sb)
    if best is None:
        return None
    key, event, ka, kb, sa, sb = best
    label = {CODE: "code", FULL_NAME: "name", PARTIAL_NAME: "part"}
    method = f"{label[sa]}+{label[sb]}" + (f", {key[1]}d" if key[1] else "")
    if g.subject is None:
        return {0: Leg(ka.ticker, "yes", method), 1: Leg(kb.ticker, "yes", method)}
    if g.subject == TIE:
        tie = next((km for km in event if km.code == TIE), None)
        if tie is None:
            return None
        target = tie
    else:
        target = ka if g.subject == 0 else kb
    return {0: Leg(target.ticker, "yes", method), 1: Leg(target.ticker, "no", method)}


# -- quotes -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quote:
    ts: int               # the end of the minute the quote is from
    yes_bid: float | None
    yes_ask: float | None

    def ask(self, side: str) -> float | None:
        """The price to buy one contract of ``side``; None when that side has no offers."""
        if side == "yes":
            p = self.yes_ask
        else:
            p = None if self.yes_bid is None else 1.0 - self.yes_bid
        return p if p is not None and 0.005 < p < 0.995 else None


def parse_candles(payload: dict[str, Any]) -> list[Quote]:
    """Both candlestick shapes: archived ({"close": ...}) and live ({"close_dollars": ...})."""
    out = []
    def close(candle: dict[str, Any], side: str) -> float | None:
        d = candle.get(side) or {}
        return _dollars(d.get("close", d.get("close_dollars")))

    for c in (payload or {}).get("candlesticks") or []:
        if "end_period_ts" in c:
            out.append(Quote(int(c["end_period_ts"]), close(c, "yes_bid"), close(c, "yes_ask")))
    out.sort(key=lambda q: q.ts)
    return out


def quote_at(quotes: list[Quote], t: int, *, horizon: int = 900) -> Quote | None:
    """The quote at the end of the minute that contains ``t`` (or the next one within ``horizon``)."""
    lo, hi = 0, len(quotes)
    while lo < hi:
        mid = (lo + hi) // 2
        if quotes[mid].ts < t:
            lo = mid + 1
        else:
            hi = mid
    return quotes[lo] if lo < len(quotes) and quotes[lo].ts - t <= horizon else None


# -- the client ---------------------------------------------------------------------------------


class KalshiApi:
    """Public Kalshi market data. GET only; no key; no trading endpoint exists here."""

    def __init__(self, http, base: str) -> None:
        self.http = http
        self.base = base.rstrip("/")

    async def _pages(self, path: str, params: dict[str, Any], key: str) -> list[dict[str, Any]]:
        # Cursor pagination: https://docs.kalshi.com/getting_started/pagination.md
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            p = dict(params, limit=1000)
            if cursor:
                p["cursor"] = cursor
            page = await self.http.get_json(f"{self.base}{path}", p)
            out += (page or {}).get(key) or []
            cursor = (page or {}).get("cursor")
            if not cursor:
                return out

    async def cutoff(self) -> dict[str, Any]:
        # GET /historical/cutoff: markets settled before market_settled_ts only exist under /historical.
        return await self.http.get_json(f"{self.base}/historical/cutoff")

    async def series(self, ticker: str) -> dict[str, Any]:
        # GET /series/{series_ticker}: fee_type, fee_multiplier.
        return ((await self.http.get_json(f"{self.base}/series/{ticker}")) or {}).get("series") or {}

    async def markets(self, series: str, *, min_close_ts: int) -> list[KalshiMarket]:
        """Every market of a series: the archive (GET /historical/markets, series_ticker is
        exclusive of other filters there) and the live set closing after ``min_close_ts``."""
        archived = await self._pages("/historical/markets", {"series_ticker": series}, "markets")
        live = await self._pages("/markets", {"series_ticker": series, "min_close_ts": min_close_ts}, "markets")
        out = {r["ticker"]: KalshiMarket.from_api(r, series, archived=True) for r in archived}
        out.update({r["ticker"]: KalshiMarket.from_api(r, series, archived=False) for r in live})
        return list(out.values())

    async def candles(self, market: KalshiMarket, start: int, end: int) -> list[Quote]:
        """One-minute quotes. Archived markets: GET /historical/markets/{ticker}/candlesticks;
        live ones: GET /series/{series}/markets/{ticker}/candlesticks."""
        params = {"start_ts": int(start), "end_ts": int(end), "period_interval": 1}
        if market.archived:
            url = f"{self.base}/historical/markets/{market.ticker}/candlesticks"
        else:
            url = f"{self.base}/series/{market.series}/markets/{market.ticker}/candlesticks"
        return parse_candles(await self.http.get_json(url, params))


def windows(times: Iterable[int], *, before: int = 120, after: int = 1020, max_span: int = 6 * 3600) -> list[tuple[int, int]]:
    """Merge the quote windows needed around each copy into as few requests as possible."""
    out: list[list[int]] = []
    for t in sorted(set(times)):
        s, e = t - before, t + after
        if out and s <= out[-1][1] and e - out[-1][0] <= max_span:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def ceil_minute(ts: int) -> int:
    return int(math.ceil(ts / 60) * 60)


# -- the cache ------------------------------------------------------------------------------------


_MARKET_COLS = ("ticker", "event_ticker", "series_ticker", "name", "status", "result", "settlement_value", "open_time",
                "close_time", "expected_expiration", "settled_at", "archived", "fetched_at")


class KalshiStore:
    """Kalshi market lists, fee multipliers and minute quotes, cached in SQLite."""

    def __init__(self, app: App, *, max_age: int = 6 * 3600) -> None:
        self.app = app
        self.api = KalshiApi(app.http, app.cfg.api.kalshi_api)
        self.max_age = max_age

    async def series_markets(self, series: str, *, min_close_ts: int) -> list[KalshiMarket]:
        db, now = self.app.db, self.app.clock()
        row = db.one("SELECT markets_fetched_at FROM kalshi_series WHERE ticker = ?", (series,))
        if row is None or row["markets_fetched_at"] is None or now - row["markets_fetched_at"] > self.max_age:
            info = await self.api.series(series)
            fresh = [m for m in await self.api.markets(series, min_close_ts=min_close_ts)
                     if m.close_time is None or m.close_time >= min_close_ts]
            with db.tx() as conn:
                conn.executemany(
                    f"INSERT OR REPLACE INTO kalshi_markets ({', '.join(_MARKET_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_MARKET_COLS))})",
                    [(m.ticker, m.event_ticker, m.series, m.name, m.status, m.result, m.settlement_value, m.open_time,
                      m.close_time, m.expected_expiration, m.settled_at, int(m.archived), now) for m in fresh])
                conn.execute(
                    "INSERT OR REPLACE INTO kalshi_series (ticker, fee_type, fee_multiplier, markets_fetched_at, "
                    "fetched_at) VALUES (?,?,?,?,?)",
                    (series, info.get("fee_type"), _dollars(info.get("fee_multiplier")), now, now))
        out = []
        for r in db.query("SELECT * FROM kalshi_markets WHERE series_ticker = ? AND "
                          "(close_time IS NULL OR close_time >= ?)", (series, min_close_ts)):
            out.append(KalshiMarket(
                ticker=r["ticker"], event_ticker=r["event_ticker"], series=series,
                code=r["ticker"].rsplit("-", 1)[-1].upper(), name=r["name"] or "",
                event_date=event_date(r["event_ticker"], series), status=r["status"] or "", result=r["result"] or "",
                settlement_value=r["settlement_value"], open_time=r["open_time"], close_time=r["close_time"],
                expected_expiration=r["expected_expiration"], settled_at=r["settled_at"],
                archived=bool(r["archived"])))
        return out

    async def index(self, leagues: Iterable[str], *, min_close_ts: int) -> KalshiIndex:
        markets: list[KalshiMarket] = []
        for series in sorted({s for lg in leagues for s in LEAGUE_SERIES.get(lg, ())}):
            markets += await self.series_markets(series, min_close_ts=min_close_ts)
        return KalshiIndex(markets)

    def fee_multiplier(self, series: str) -> float:
        row = self.app.db.one("SELECT fee_multiplier FROM kalshi_series WHERE ticker = ?", (series,))
        return 1.0 if row is None or row["fee_multiplier"] is None else float(row["fee_multiplier"])

    async def quotes(self, market: KalshiMarket, times: Iterable[int]) -> list[Quote]:
        """Minute quotes covering every time in ``times`` (and 17 minutes after each)."""
        db = self.app.db
        wins = windows(times)
        have = [(r["start_ts"], r["end_ts"]) for r in
                db.query("SELECT start_ts, end_ts FROM kalshi_quote_windows WHERE ticker = ?", (market.ticker,))]
        for s, e in wins:
            if any(hs <= s and e <= he for hs, he in have):
                continue
            quotes = await self.api.candles(market, s, e)
            with db.tx() as conn:
                conn.executemany("INSERT OR REPLACE INTO kalshi_quotes (ticker, ts, yes_bid, yes_ask) VALUES (?,?,?,?)",
                                 [(market.ticker, q.ts, q.yes_bid, q.yes_ask) for q in quotes])
                conn.execute("INSERT OR REPLACE INTO kalshi_quote_windows (ticker, start_ts, end_ts) VALUES (?,?,?)",
                             (market.ticker, s, e))
        if not wins:
            return []
        lo, hi = min(s for s, _ in wins), max(e for _, e in wins)
        return [Quote(r["ts"], r["yes_bid"], r["yes_ask"]) for r in
                db.query("SELECT ts, yes_bid, yes_ask FROM kalshi_quotes WHERE ticker = ? AND ts BETWEEN ? AND ? "
                         "ORDER BY ts", (market.ticker, lo, hi))]
