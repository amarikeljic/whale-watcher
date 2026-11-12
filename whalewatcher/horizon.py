"""v2.1 Part 3: the time-to-close cutoff.

A paper dollar tied up in a market that settles in three months cannot be used
again for three months. With a small bankroll that matters more than the edge,
so a signal is skipped when its market is scheduled to end too far away.

The scheduled end is Gamma's ``endDate`` on the market
(https://docs.polymarket.com/api-reference/markets/list-markets.md, schema
``Market.endDate``, date-time, nullable; checked 2026-11-12). What it means differs
by market, measured on the 8,600 markets behind stored alerts:

  sports     it is the game's start time on 4 of 5 markets, so the bet settles a
             few hours AFTER it (median +3 h)
  crypto     the end of the window; settles within seconds
  "by date"  the deadline; these often settle long BEFORE it
  politics   the event date; 38% settled more than a day after it

On 1 sports market in 10 ``endDate`` trails the game by more than a day (and by more
than 14 days on 4 of 1,971), so for a market with a ``gameStartTime`` the schedule
is whichever of the two comes first.

So the cutoff is a rule about the schedule, not a promise about the holding time.
The real resolution time is recorded at settlement and reported beside it.
"""

from __future__ import annotations

from .config import FiltersConfig

DAY = 86400
TOO_LONG = "too_long"
NO_END_DATE = "no_end_date"
BUCKETS = ("under 1 day", "1 to 7 days", "7 to 14 days", "over 14 days")
UNKNOWN = "no end date"


def scheduled_end(end_date: int | None, game_start: int | None) -> int | None:
    """When the market is scheduled to be decided: its end date, or the game's start
    when that comes first (or is all there is)."""
    known = [t for t in (end_date, game_start) if t is not None]
    return min(known) if known else None


def days_to_close(end_date: int | None, now: float) -> float | None:
    """Days from ``now`` to the scheduled end. A market already past its scheduled
    end (a game in play, a deadline that slipped) is 0 days away, not negative."""
    if end_date is None:
        return None
    return max(0.0, (end_date - now) / DAY)


def close_skip(end_date: int | None, now: float, cfg: FiltersConfig) -> tuple[str, str] | None:
    """(code, reason) when the cutoff forbids entering now, else None. A market that
    ends exactly ``max_days_to_close`` away is allowed."""
    days = days_to_close(end_date, now)
    if days is None:
        if cfg.skip_if_no_end_date:
            return NO_END_DATE, f"{NO_END_DATE}: the market has no scheduled end date"
        return None
    if cfg.max_days_to_close is not None and days > cfg.max_days_to_close:
        return TOO_LONG, f"{TOO_LONG}: closes in {days:.1f} days (limit {cfg.max_days_to_close:g})"
    return None


def bucket(days: float | None) -> str:
    """The holding-time bands of the brief. 14.0 belongs with 7 to 14, as in the rule."""
    if days is None:
        return UNKNOWN
    if days < 1:
        return BUCKETS[0]
    if days < 7:
        return BUCKETS[1]
    if days <= 14:
        return BUCKETS[2]
    return BUCKETS[3]


def held_days(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start) / DAY)
