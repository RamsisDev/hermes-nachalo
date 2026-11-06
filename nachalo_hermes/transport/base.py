"""The relay interface. A relay only carries bytes and takes no part in security: everything it
sees is either encrypted and authenticated or a public handshake value."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Message:
    id: str
    topic: str
    data: bytes
    time: int


class RelayError(Exception):
    """The relay did not answer or refused a request. Never carries topic names or payloads."""


class Transport(Protocol):
    def publish(self, topic: str, data: bytes) -> str:
        """Publish ``data`` to ``topic``; returns the relay's message id."""

    def poll(self, topic: str, since: str) -> list[Message]:
        """Cached messages of ``topic`` since ``since`` (a duration like ``10m``, a Unix time or
        a message id)."""

    def subscribe(self, topics: Iterable[str], since: str | None, stop: threading.Event) -> Iterator[Message | None]:
        """Stream messages of ``topics``. Yields ``None`` on keepalives so callers can check
        ``stop`` or change topics; returns when the connection ends or ``stop`` is set."""

    def close(self) -> None: ...


def pump(
    transport: Transport,
    topics: list[str],
    since: str | None,
    stop: threading.Event,
    out: queue.Queue[Message],
    retry_delay: float = 1.0,
) -> threading.Thread:
    """Subscribe in a daemon thread and put every message into ``out`` until ``stop`` is set,
    reconnecting after relay errors. Repeats after a reconnect are the consumer's to skip."""

    def run() -> None:
        while not stop.is_set():
            try:
                for msg in transport.subscribe(topics, since, stop):
                    if stop.is_set():
                        return
                    if msg is not None:
                        out.put(msg)
            except RelayError:
                stop.wait(retry_delay)

    thread = threading.Thread(target=run, name="nachalo-pump", daemon=True)
    thread.start()
    return thread
