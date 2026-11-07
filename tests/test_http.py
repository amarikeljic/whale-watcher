"""The HTTP client: retries, backoff, outage detection, query building, ETags."""

from __future__ import annotations

import time

import pytest

from whalewatcher.http import OUTAGE_ATTEMPTS, HostHealth, HttpClient, HttpError, build_url


class FakeResponse:
    def __init__(self, status, text="{}", headers=None):
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        pass


async def no_sleep(_seconds):
    pass


def client(responses, **kw):
    session = FakeSession(responses)
    return HttpClient(session_factory=lambda: session, sleep=no_sleep, **kw), session


def test_lists_repeat_the_key_for_gamma():
    url = build_url("https://g/markets", {"condition_ids": ["0xa", "0xb"], "closed": "any", "x": None,
                                          "include_tag": True})
    assert url == "https://g/markets?closed=any&condition_ids=0xa&condition_ids=0xb&include_tag=true"


async def test_retries_server_errors_then_succeeds():
    c, session = client([FakeResponse(503), FakeResponse(502), FakeResponse(200, '{"ok": 1}')])
    assert await c.get_json("https://h/x") == {"ok": 1}
    assert len(session.requests) == 3


async def test_honours_retry_after_on_429():
    waits = []

    async def record(seconds):
        waits.append(seconds)

    session = FakeSession([FakeResponse(429, "", {"Retry-After": "7"}), FakeResponse(200, "[]")])
    c = HttpClient(session_factory=lambda: session, sleep=record)
    assert await c.get_json("https://h/x") == []
    assert 7 <= waits[0] < 7.5


async def test_a_404_is_an_answer_not_an_outage():
    c, _ = client([FakeResponse(404, "not found")])
    with pytest.raises(HttpError) as err:
        await c.get_json("https://h/x")
    assert err.value.status == 404 and err.value.answered
    assert c.health["h"].failures == 0


async def test_gives_up_after_the_attempts():
    c, _ = client([OSError("boom")] * 4, attempts=4)
    with pytest.raises(HttpError):
        await c.get_json("https://h/x")
    assert c.health["h"].failures == 4


def test_outage_needs_both_time_and_attempts():
    c = HttpClient()
    h = c.health.setdefault("h", HostHealth())
    for _ in range(OUTAGE_ATTEMPTS - 1):
        h.failed("x")
    h.failing_since = time.monotonic() - 3600
    assert c.outage("h") is None, "too few attempts"
    h.failed("x")
    assert c.outage("h") is not None
    h.failing_since = time.monotonic() - 30
    assert c.outage("h") is None, "too short"
    h.ok()
    assert c.outage("h") is None


async def test_etag_is_sent_back_and_a_304_serves_the_stored_body():
    c, session = client([FakeResponse(200, '{"v": 1}', {"ETag": '"abc"'}), FakeResponse(304, "")])
    assert await c.get_json("https://h/x") == {"v": 1}
    assert await c.get_json("https://h/x") == {"v": 1}
    assert session.requests[1][2]["headers"]["If-None-Match"] == '"abc"'


async def test_ttl_cache_avoids_a_second_request():
    c, session = client([FakeResponse(200, '{"v": 1}')])
    await c.get_json("https://h/x", ttl=60)
    await c.get_json("https://h/x", ttl=60)
    assert len(session.requests) == 1
