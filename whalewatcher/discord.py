"""Discord webhook sink: post an alert, then edit it as more fills join the group.

Discord's webhook API (discord.com/developers/docs/resources/webhook; that host
is not reachable from the build sandbox, so these come from the published API
rather than a fresh read):

* Execute Webhook: ``POST {webhook_url}?wait=true`` returns the created message,
  whose ``id`` is needed to edit it later.
* Edit Webhook Message: ``PATCH {webhook_url}/messages/{message_id}``.
* 429 responses carry ``retry_after`` (seconds) in the JSON body; the HTTP
  client honours it.
* ``allowed_mentions: {parse: []}`` so an alert can never ping anyone.

The webhook URL contains a secret token. It lives only in .env and is never
logged: errors report the webhook id at most.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

MAX_CONTENT = 2000


class Sink(Protocol):
    async def post(self, content: str) -> str | None: ...
    async def edit(self, message_id: str, content: str) -> bool: ...


def _redact(url: str) -> str:
    return re.sub(r"(/webhooks/\d+/)[^/?]+", r"\1<token>", url)


class DiscordWebhook:
    def __init__(self, http: HttpClient, url: str) -> None:
        self.http = http
        self.url = url.rstrip("/")

    def _body(self, content: str) -> dict[str, Any]:
        return {"content": content[:MAX_CONTENT], "allowed_mentions": {"parse": []}}

    async def post(self, content: str) -> str | None:
        """Message id once Discord has accepted it, or None if it would not."""
        try:
            message = await self.http.send_json("POST", f"{self.url}?wait=true", self._body(content))
        except HttpError as exc:
            log.warning("Discord post failed (%s): %s", _redact(self.url), exc.status)
            return None
        return str((message or {}).get("id") or "") or None

    async def edit(self, message_id: str, content: str) -> bool:
        try:
            await self.http.send_json("PATCH", f"{self.url}/messages/{message_id}", self._body(content))
        except HttpError as exc:
            log.warning("Discord edit failed (%s): %s", _redact(self.url), exc.status)
            return False
        return True


class NullSink:
    """No webhook configured: nothing is sent, alerts are stored as UNSENT."""

    async def post(self, content: str) -> str | None:
        return None

    async def edit(self, message_id: str, content: str) -> bool:
        return False


class MemorySink:
    """Captures messages; used by tests and by ``monitor --no-post`` dry runs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str]] = []

    async def post(self, content: str) -> str | None:
        message_id = str(len(self.posts) + 1)
        self.posts.append((message_id, content))
        return message_id

    async def edit(self, message_id: str, content: str) -> bool:
        self.edits.append((message_id, content))
        return True
