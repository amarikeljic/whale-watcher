"""v2.1 Part 4 #15: outage windows the service records itself.

A window is a stretch of time in which the service could not act on a signal in time.
Stats and the dashboard leave the signals inside one out by default (stats.Scope). Each
window is a row of ``outage_windows`` with ``source = 'detected'`` and one of four kinds:

* ``runner``: the runner was not running. Recorded when it starts again, from its last
  heartbeat to the restart, if the gap was long enough to make a signal late.
* ``detector``: the detector was not running or was stuck. The runner opens a window when
  the detector's heartbeat goes stale and closes it when the heartbeat moves again. A
  detector starting after a gap closes the open window, or records the gap itself.
* ``data``: Polymarket's data API stopped answering (``HttpClient.outage``), from the first
  failed attempt until it answered again.

At most one window of a kind is open at a time, and opening one that is already open does
nothing, so a runner that restarts in the middle of an outage does not record it twice.
Windows of different kinds may overlap: a server reboot stops both processes.
Times are UTC epoch seconds, like everything else in the database.
"""

from __future__ import annotations

import logging
from typing import Any

from .timeutil import iso

log = logging.getLogger(__name__)

RUNNER, DETECTOR, DATA = "runner", "detector", "data"
KINDS = (RUNNER, DETECTOR, DATA)


def min_gap(cfg: Any) -> float:
    """A stop shorter than this cannot have made a signal late: not an outage."""
    return float(cfg.bot.max_signal_age_seconds)


def open_window(db: Any, kind: str, start: int, cause: str) -> int | None:
    """Open a window of this kind unless one is open already. Returns the new row's id."""
    assert kind in KINDS
    if db.one("SELECT 1 FROM outage_windows WHERE kind = ? AND end_ts IS NULL", (kind,)) is not None:
        return None
    cur = db.execute("INSERT INTO outage_windows (start_ts, end_ts, cause, source, kind) VALUES (?, NULL, ?, "
                     "'detected', ?)", (int(start), cause, kind))
    log.warning("outage window opened (%s) from %s: %s", kind, iso(start), cause)
    return int(cur.lastrowid)


def close_window(db: Any, kind: str, end: int) -> bool:
    """Close the open window of this kind, if there is one. An end before the start
    (clocks of two processes) becomes the start: a window never runs backwards."""
    row = db.one("SELECT id, start_ts FROM outage_windows WHERE kind = ? AND end_ts IS NULL", (kind,))
    if row is None:
        return False
    db.execute("UPDATE outage_windows SET end_ts = ? WHERE id = ?", (max(int(end), row["start_ts"]), row["id"]))
    log.warning("outage window closed (%s) at %s", kind, iso(end))
    return True


def record_gap(db: Any, kind: str, last_beat: int | None, now: int, gap: float, cause: str) -> int | None:
    """A process starting again: record [last heartbeat, now) if it was away longer than
    ``gap``. Closes a window of this kind that is still open (opened by the other process,
    or left open by a stop) instead of adding a second one."""
    if last_beat is None:            # first start ever: nothing was missed
        return None
    if close_window(db, kind, now):
        return None
    if now - last_beat <= gap:
        return None
    cur = db.execute("INSERT INTO outage_windows (start_ts, end_ts, cause, source, kind) VALUES (?, ?, ?, "
                     "'detected', ?)", (int(last_beat), int(now), cause, kind))
    log.warning("outage window recorded (%s): %s to %s, %s", kind, iso(last_beat), iso(now), cause)
    return int(cur.lastrowid)


def describe_gap(what: str, seconds: float) -> str:
    minutes = seconds / 60
    span = f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"
    return f"the {what} was not running for {span} (found when it started again)"
