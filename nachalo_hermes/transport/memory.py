"""An in-process relay with ntfy's semantics, for tests and the offline demo."""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator

from .base import Message, RelayError

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class MemoryRelay:
    """Shared message store. Several :class:`MemoryTransport` clients may attach to one relay.

    ``tamper`` lets a test act as a malicious relay: it sees every publish and returns the list
    of ``(topic, data)`` to store instead (drop, duplicate, rewrite or redirect).
    """

    def __init__(self, keepalive: float = 0.05):
        self._lock = threading.Condition()
        self._messages: list[Message] = []
        self._ids = itertools.count(1)
        self.keepalive = keepalive
        self.down = False
        self.published: list[Message] = []
        self.tamper: Callable[[str, bytes], list[tuple[str, bytes]]] | None = None

    def publish(self, topic: str, data: bytes) -> str:
        if self.down:
            raise RelayError("relay unreachable (down)")
        outputs = self.tamper(topic, data) if self.tamper else [(topic, data)]
        first = ""
        with self._lock:
            for out_topic, out_data in outputs:
                msg = Message(id=f"m{next(self._ids)}", topic=out_topic, data=out_data, time=int(time.time()))
                self._messages.append(msg)
                self.published.append(msg)
                first = first or msg.id
            self._lock.notify_all()
        return first or f"m{next(self._ids)}"

    def inject(self, topic: str, data: bytes) -> str:
        """Store a message without going through ``tamper`` (an attacker writing directly)."""
        with self._lock:
            msg = Message(id=f"m{next(self._ids)}", topic=topic, data=data, time=int(time.time()))
            self._messages.append(msg)
            self._lock.notify_all()
        return msg.id

    def _since_index(self, since: str | None) -> int:
        if since is None:
            return len(self._messages)
        if since == "all":
            return 0
        match = _DURATION.match(since)
        if match:
            cutoff = time.time() - int(match.group(1)) * _UNITS[match.group(2)]
            return next((i for i, m in enumerate(self._messages) if m.time >= cutoff), len(self._messages))
        if since.isdigit():
            cutoff = int(since)
            return next((i for i, m in enumerate(self._messages) if m.time >= cutoff), len(self._messages))
        for i, m in enumerate(self._messages):
            if m.id == since:
                return i + 1
        return 0

    def poll(self, topic: str, since: str) -> list[Message]:
        if self.down:
            raise RelayError("relay unreachable (down)")
        with self._lock:
            start = self._since_index(since)
            return [m for m in self._messages[start:] if m.topic == topic]

    def subscribe(self, topics: Iterable[str], since: str | None, stop: threading.Event) -> Iterator[Message | None]:
        if self.down:
            raise RelayError("relay unreachable (down)")
        wanted = set(topics)
        with self._lock:
            cursor = self._since_index(since)
        while not stop.is_set():
            if self.down:
                raise RelayError("relay connection lost")
            with self._lock:
                fresh = self._messages[cursor:]
                cursor = len(self._messages)
                if not fresh:
                    self._lock.wait(self.keepalive)
            matched = [m for m in fresh if m.topic in wanted]
            yield from matched
            if not matched:
                yield None


class MemoryTransport:
    def __init__(self, relay: MemoryRelay):
        self.relay = relay

    def publish(self, topic: str, data: bytes) -> str:
        return self.relay.publish(topic, data)

    def poll(self, topic: str, since: str) -> list[Message]:
        return self.relay.poll(topic, since)

    def subscribe(self, topics, since, stop):
        return self.relay.subscribe(topics, since, stop)

    def close(self) -> None:
        pass
