"""Time helpers. Everything is stored and computed in UTC epoch seconds.

Polymarket writes times in at least five shapes, sometimes several in one
response: ``2026-11-08T04:59:59.999Z``, ``2026-09-29 23:00:00+00`` (gameStartTime),
``2026-11-06 03:30:03.883336+00`` (closedTime), ``...+00:00:00`` (umaEndDate),
epoch-second strings on some /v2/resolutions rows, and bare dates. One parser
handles all of them so nothing downstream has to care.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, timedelta

_OFFSET_TAIL = re.compile(r"([+-]\d\d)(?::?(\d\d))?(?::\d\d)?$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_ts() -> int:
    return int(utcnow().timestamp())


def to_ts(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def from_ts(ts: float | int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def iso(ts: float | int | None) -> str:
    """Display form, still UTC: ``2026-11-07 14:05:09Z``."""
    if ts is None:
        return "-"
    return from_ts(ts).strftime("%Y-%m-%d %H:%M:%SZ")


def parse_time(value: object) -> int | None:
    """Epoch seconds from any time format the APIs use, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Milliseconds (CLOB book timestamps) versus seconds.
        return int(value / 1000) if value > 1e11 else int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        return int(number / 1000) if number > 1e11 else int(number)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        d = date.fromisoformat(text)
        return to_ts(datetime(d.year, d.month, d.day, tzinfo=UTC))
    text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    else:
        match = _OFFSET_TAIL.search(text)
        if match and "T" in text and match.start() > text.index("T"):
            hours, minutes = match.group(1), match.group(2) or "00"
            text = text[: match.start()] + f"{hours}:{minutes}"
    # Python 3.11+ fromisoformat copes with any number of fractional digits up to 6.
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def parse_as_of(text: str) -> int:
    """CLI dates: ``2026-11-01`` means midnight UTC at the start of that day."""
    ts = parse_time(text)
    if ts is None:
        raise ValueError(f"not a date: {text!r} (use YYYY-MM-DD or an ISO timestamp)")
    return ts


def add_months(ts: int, months: int) -> int:
    """Calendar arithmetic, clamping the day: Mar 31 minus one month is Feb 28/29."""
    dt = from_ts(ts)
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return to_ts(dt.replace(year=year, month=month, day=day))


def month_key(ts: int) -> str:
    return from_ts(ts).strftime("%Y-%m")


def months_in_window(start_ts: int, end_ts: int) -> list[str]:
    """Calendar months that intersect [start, end). A window from the 1st to the
    1st covers exactly that many months; a mid-month window touches one more."""
    if end_ts <= start_ts:
        return []
    keys: list[str] = []
    cursor = from_ts(start_ts).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = from_ts(end_ts - 1)
    while (cursor.year, cursor.month) <= (last.year, last.month):
        keys.append(cursor.strftime("%Y-%m"))
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return keys


def start_of_day(ts: int) -> int:
    dt = from_ts(ts)
    return to_ts(dt.replace(hour=0, minute=0, second=0, microsecond=0))
