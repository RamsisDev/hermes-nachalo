"""ntfy as a dumb relay: publish with POST, read the cache with ``poll=1``, stream with NDJSON."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Iterator
from urllib.parse import urlparse

import httpx

from ..encoding import b64u, b64u_decode
from .base import Message, RelayError

MAX_TOPICS_PER_SUBSCRIPTION = 150
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_relay_url(url: str) -> str:
    """The relay URL without a trailing slash; ``https://`` is required except for localhost."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("relay_url must be an http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("relay_url must use https:// (http:// is only allowed for localhost)")
    return url.strip().rstrip("/")


class NtfyTransport:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 15.0):
        self.base_url = check_relay_url(base_url)
        headers = {"User-Agent": "hermes-nachalo/0.1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(headers=headers, timeout=timeout, follow_redirects=False)

    def publish(self, topic: str, data: bytes) -> str:
        try:
            resp = self._client.post(f"{self.base_url}/{topic}", content=b64u(data).encode("ascii"))
        except httpx.HTTPError as exc:
            raise RelayError(f"relay unreachable ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise RelayError(f"relay refused the publish (HTTP {resp.status_code})")
        try:
            return str(resp.json()["id"])
        except ValueError, KeyError:
            raise RelayError("relay answered without a message id") from None

    def poll(self, topic: str, since: str) -> list[Message]:
        try:
            resp = self._client.get(f"{self.base_url}/{topic}/json", params={"poll": "1", "since": since})
        except httpx.HTTPError as exc:
            raise RelayError(f"relay unreachable ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise RelayError(f"relay refused the read (HTTP {resp.status_code})")
        out = []
        for line in resp.text.splitlines():
            msg = _parse(line)
            if msg is not None:
                out.append(msg)
        return out

    def subscribe(self, topics: Iterable[str], since: str | None, stop: threading.Event) -> Iterator[Message | None]:
        topics = list(topics)
        if not topics:
            return
        if len(topics) > MAX_TOPICS_PER_SUBSCRIPTION:
            raise ValueError("too many topics for one subscription")
        params = {"since": since} if since else {}
        # ntfy sends a keepalive every 45 s by default; a longer read timeout notices a dead link.
        timeout = httpx.Timeout(10.0, read=70.0)
        try:
            with self._client.stream(
                "GET", f"{self.base_url}/{','.join(topics)}/json", params=params, timeout=timeout
            ) as resp:
                if resp.status_code != 200:
                    raise RelayError(f"relay refused the subscription (HTTP {resp.status_code})")
                for line in resp.iter_lines():
                    if stop.is_set():
                        return
                    msg = _parse(line)
                    yield msg
        except httpx.HTTPError as exc:
            raise RelayError(f"relay connection lost ({type(exc).__name__})") from None

    def close(self) -> None:
        self._client.close()


def _parse(line: str) -> Message | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("event") != "message" or "message" not in event:
        return None
    try:
        data = b64u_decode(event["message"])
    except ValueError, TypeError:
        return None
    return Message(
        id=str(event.get("id", "")), topic=str(event.get("topic", "")), data=data, time=int(event.get("time", 0))
    )
