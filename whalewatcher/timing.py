"""Where a long run spends its time: per-stage wall time and request counts, plus progress with an ETA."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Stage:
    name: str
    seconds: float
    requests: int


class Stopwatch:
    def __init__(self, http: object) -> None:
        self.http = http
        self.stages: list[Stage] = []

    def _requests(self) -> int:
        return int(getattr(self.http, "requests_made", 0) or 0)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started, before = time.monotonic(), self._requests()
        try:
            yield
        finally:
            self.stages.append(Stage(name, time.monotonic() - started, self._requests() - before))

    def table(self) -> str:
        total_s = sum(s.seconds for s in self.stages)
        total_r = sum(s.requests for s in self.stages)
        width = max([len(s.name) for s in self.stages] + [5])
        lines = [f"  {'stage':{width}s}  {'time':>8s}  {'requests':>8s}"]
        for s in self.stages:
            lines.append(f"  {s.name:{width}s}  {_dur(s.seconds):>8s}  {s.requests:8d}")
        lines.append(f"  {'total':{width}s}  {_dur(total_s):>8s}  {total_r:8d}")
        return "\n".join(lines)


def _dur(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Progress:
    """Reports ``label: done/total, elapsed, ~ETA`` at most every ``every`` seconds."""

    def __init__(self, say: Callable[[str], None], label: str, total: int, *, every: float = 15.0) -> None:
        self.say, self.label, self.total, self.every = say, label, total, every
        self.done = 0
        self.started = self.last = time.monotonic()

    def tick(self, n: int = 1) -> None:
        self.done += n
        now = time.monotonic()
        if self.done < self.total and now - self.last < self.every:
            return
        self.last = now
        elapsed = now - self.started
        eta = elapsed / self.done * (self.total - self.done) if self.done else 0
        tail = "" if self.done >= self.total else f", ~{_dur(eta)} left"
        self.say(f"{self.label}: {self.done}/{self.total} ({_dur(elapsed)}{tail})")
