"""One shared aiohttp client: polite rate limits, retries, a small cache, outage detection.

Patterns carried over from the UFC bot (ufcbot/sources/http.py):

* ``HttpError.answered`` separates "the host replied with an error" from "the
  host is unreachable". Only the second counts toward an outage.
* An outage needs both time and attempts: one bad minute is not an outage.
* Retries back off exponentially with jitter, and honour ``Retry-After``.
* Identical in-flight GETs share one round trip.
* ETag / If-None-Match conditional requests. None of the APIs this service
  reads sends an ETag today (checked 2026-11-07: Gamma sends
  ``Cache-Control: max-age=300``, the Data API and CLOB send neither), so this
  path is dormant until one does.

``trust_env=True`` so an HTTPS_PROXY in the environment is honoured; on the
home server there is none and it changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp

log = logging.getLogger(__name__)

RETRYABLE = (429, 500, 502, 503, 504)

# An outage is declared only after this long with nothing but failures AND at
# least this many failed attempts. A 60 s poller makes ~10 attempts in 10 min.
OUTAGE_AFTER_SECONDS = 600
OUTAGE_ATTEMPTS = 8


class HttpError(Exception):
    def __init__(self, message: str, *, status: int | None = None, answered: bool = False,
                 body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.answered = answered
        self.body = body


@dataclass(slots=True)
class HostHealth:
    failures: int = 0
    failing_since: float | None = None   # monotonic
    last_error: str = ""
    last_ok: float | None = None

    def ok(self) -> None:
        self.failures = 0
        self.failing_since = None
        self.last_error = ""
        self.last_ok = time.monotonic()

    def failed(self, error: str) -> None:
        self.failures += 1
        self.last_error = error
        if self.failing_since is None:
            self.failing_since = time.monotonic()


class RateLimiter:
    """Token bucket per host: ``rate`` requests per second, bursts up to ``burst``."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def build_url(url: str, params: dict[str, Any] | None) -> str:
    """Query string with lists repeated (``a=1&a=2``), Nones dropped, bools lower-cased.

    Gamma needs repeated keys for array filters: ``condition_ids=a,b`` silently
    matches nothing (verified 2026-11-07).
    """
    if not params:
        return url
    clean: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, bool):
                item = "true" if item else "false"
            clean.append((key, str(item)))
    if not clean:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(clean)}"


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str = "whale-watcher/0.1",
        timeout: float = 20.0,
        max_concurrency: int = 8,
        rate_limits: dict[str, float] | None = None,
        attempts: int = 4,
        max_cache_entries: int = 4000,
        session_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        # Keys are a host ("data-api.polymarket.com") or a host plus path prefix
        # ("data-api.polymarket.com/v2/activity"). A request waits on its host's
        # limiter and on the most specific prefix that matches its path, so a slow
        # endpoint's cap does not throttle the host's other endpoints.
        self.limiters: dict[str, RateLimiter] = {}
        self._prefix_limiters: list[tuple[str, str, RateLimiter]] = []
        for key, rate in (rate_limits or {}).items():
            limiter = RateLimiter(rate)
            host, slash, path = key.partition("/")
            if slash:
                self._prefix_limiters.append((host, "/" + path, limiter))
            else:
                self.limiters[host] = limiter
        self._prefix_limiters.sort(key=lambda item: -len(item[1]))
        self.attempts = attempts
        self.health: dict[str, HostHealth] = {}
        self._cache: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        # Bounded: the detector asks a URL it has never asked before every second, and
        # an unbounded map of URL -> payload would grow by a page of trades each time.
        self._etags: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        self._max_etags = 256
        self._max_cache = max_cache_entries
        self._inflight: dict[str, asyncio.Future] = {}
        self._session = None
        self._session_factory = session_factory
        self._sleep = sleep
        self.requests_made = 0

    async def __aenter__(self) -> HttpClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is not None:
            return
        if self._session_factory is not None:
            self._session = self._session_factory()
        else:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                trust_env=True,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )

    async def close(self) -> None:
        if self._session is not None and hasattr(self._session, "close"):
            result = self._session.close()
            if asyncio.iscoroutine(result):
                await result
        self._session = None

    # -- health ----------------------------------------------------------------

    def _note(self, url: str, *, ok: bool, error: str = "") -> None:
        health = self.health.setdefault(urlsplit(url).netloc, HostHealth())
        if ok:
            health.ok()
        else:
            health.failed(error)

    def outage(self, host: str, *, after: float = OUTAGE_AFTER_SECONDS,
               attempts: int = OUTAGE_ATTEMPTS) -> str | None:
        """A description of the outage if ``host`` has stopped answering, else None."""
        h = self.health.get(host)
        if h is None or h.failing_since is None or h.failures < attempts:
            return None
        idle = time.monotonic() - h.failing_since
        if idle < after:
            return None
        return f"{host}: {h.failures} failed attempts over {idle / 60:.0f} min; last error: {h.last_error}"

    # -- requests ----------------------------------------------------------------

    async def get_json(self, url: str, params: dict[str, Any] | None = None, *,
                       ttl: float = 0) -> Any:
        full = build_url(url, params)
        if ttl > 0:
            hit = self._cache.get(full)
            if hit is not None and time.monotonic() - hit[0] < hit[1]:
                self._cache.move_to_end(full)
                return hit[2]
        pending = self._inflight.get(full)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[full] = future
        try:
            payload = await self._request("GET", full)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if ttl > 0:
                self._cache[full] = (time.monotonic(), int(ttl), payload)
                while len(self._cache) > self._max_cache:
                    self._cache.popitem(last=False)
            future.set_result(payload)
            return payload
        finally:
            self._inflight.pop(full, None)

    async def _request(self, method: str, url: str) -> Any:
        """GET only. This client has no way to send a body: the service reads public
        data and sends nothing to anyone."""
        if method != "GET":
            raise ValueError("this service only reads: GET is the only request it makes")
        if self._session is None:
            await self.start()
        parts = urlsplit(url)
        host = parts.netloc
        limiters = [lim for lim in (self._prefix_limiter(host, parts.path), self.limiters.get(host)) if lim]
        last_error = ""
        for attempt in range(self.attempts):
            for limiter in limiters:
                await limiter.acquire()
            headers: dict[str, str] = {}
            etag = self._etags.get(url) if method == "GET" else None
            if etag:
                headers["If-None-Match"] = etag[0]
            try:
                async with self.semaphore:
                    self.requests_made += 1
                    async with self._session.request("GET", url, headers=headers) as resp:
                        status = resp.status
                        text = await resp.text()
                        resp_headers = resp.headers
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                last_error = repr(exc)
                self._note(url, ok=False, error=last_error)
                await self._sleep(self._backoff(attempt, None))
                continue

            if status == 304 and etag:
                self._note(url, ok=True)
                return etag[1]
            if status in RETRYABLE:
                last_error = f"HTTP {status}"
                retry_after = _retry_after(resp_headers, text)
                if status >= 500:
                    self._note(url, ok=False, error=last_error)
                else:
                    self._note(url, ok=True)  # 429: the host is fine, we are too fast
                log.debug("retryable %s from %s (attempt %d)", status, url, attempt + 1)
                await self._sleep(self._backoff(attempt, retry_after))
                continue
            self._note(url, ok=True)
            if status >= 400:
                raise HttpError(f"HTTP {status} from {url}: {text[:300]}", status=status,
                                answered=True, body=text)
            if status == 204 or not text:
                return None
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise HttpError(f"invalid JSON from {url}", status=status, answered=True,
                                body=text[:300]) from exc
            new_etag = resp_headers.get("ETag") if method == "GET" else None
            if new_etag:
                self._etags[url] = (new_etag, payload)
                self._etags.move_to_end(url)
                while len(self._etags) > self._max_etags:
                    self._etags.popitem(last=False)
            return payload
        raise HttpError(f"failed after {self.attempts} attempts: {url}: {last_error}")

    def _prefix_limiter(self, host: str, path: str) -> RateLimiter | None:
        for h, prefix, limiter in self._prefix_limiters:
            if h == host and path.startswith(prefix):
                return limiter
        return None

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(60.0, retry_after) + random.uniform(0, 0.25)
        return min(30.0, 0.5 * 2 ** attempt) + random.uniform(0, 0.3)


def _retry_after(headers: Any, text: str) -> float | None:
    value = None
    try:
        value = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        value = None
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    # Some APIs put it in the JSON body as seconds.
    if text and "retry_after" in text:
        try:
            return float(json.loads(text).get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            return None
    return None
