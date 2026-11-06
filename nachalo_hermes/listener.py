"""The background listener: one relay subscription per relay, frames into the inbox, nothing else.

It never invokes the agent, never opens a turn and never posts to a chat (I7). It notifies
through the ``pre_llm_call`` hook (counts only) and Desktop events (state only).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import nachalo

from . import channel as channel_mod
from .clock import now_unix
from .host import Host
from .runtime import transport_for
from .store import Store
from .transport import Message, pump

logger = logging.getLogger("nachalo")

LEASE_KEY = "listener_lease"
LEASE_TTL = 90
RECHECK = 30
ACK_EVERY = 30


class Listener:
    def __init__(
        self,
        host: Host,
        home: Path | None = None,
        recheck: float = RECHECK,
        ack_every: float = ACK_EVERY,
        should_run: Callable[[], bool] = lambda: True,
    ):
        self.host = host
        self.home = home
        self.should_run = should_run
        self.stop = threading.Event()
        self.recheck = recheck
        self.ack_every = ack_every
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{id(self)}"
        self._thread: threading.Thread | None = None
        self.processed = 0

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="nachalo-listener", daemon=True)
        self._thread.start()
        return self._thread

    def close(self, timeout: float = 5.0) -> None:
        self.stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        if self.home is not None:
            with self.host.profile_scope(self.home):
                self._run()
        else:
            self._run()

    def _run(self) -> None:
        store = Store(self.host)
        try:
            while not self.stop.is_set():
                if not self._ready() or not self._lease(store):
                    self.stop.wait(self.recheck)
                    continue
                try:
                    self._serve(store)
                except Exception:
                    logger.warning("nachalo: listener error; restarting in 5 s", exc_info=True)
                    self.stop.wait(5)
        finally:
            self._release(store)
            store.close()

    def _ready(self) -> bool:
        try:
            return bool(self.should_run())
        except Exception:
            return False

    # one owner per profile ------------------------------------------------------------------

    def _lease(self, store: Store) -> bool:
        now = now_unix()
        with store.tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (LEASE_KEY,)).fetchone()
            if row:
                lease = json.loads(row[0])
                if lease.get("owner") != self.owner and now - int(lease.get("ts", 0)) < LEASE_TTL:
                    return False
            store.set_meta(c, LEASE_KEY, json.dumps({"owner": self.owner, "ts": now}))
        return True

    def _release(self, store: Store) -> None:
        try:
            with store.tx() as c:
                row = c.execute("SELECT value FROM meta WHERE key = ?", (LEASE_KEY,)).fetchone()
                if row and json.loads(row[0]).get("owner") == self.owner:
                    c.execute("DELETE FROM meta WHERE key = ?", (LEASE_KEY,))
        except Exception:
            pass

    # serving ---------------------------------------------------------------------------------

    def _plan(self, store: Store):
        """Topics to read per relay, the peer behind each topic, and a fingerprint of the plan."""
        peers = store.peers()
        by_relay: dict[str, list[str]] = {}
        owner: dict[str, object] = {}
        since: dict[str, int] = {}
        for peer in peers:
            for topic in channel_mod.recv_topics(self.host, store, peer):
                by_relay.setdefault(peer.relay_url, []).append(topic)
                owner[topic] = peer
            last = int(store.meta(f"since:{peer.relay_url}", "0") or 0)
            start = max(last, peer.created_at) - 60
            since[peer.relay_url] = min(since.get(peer.relay_url, start), start)
        fingerprint = (tuple(sorted(by_relay.items())), nachalo.channel_epoch(now_unix()))
        return by_relay, owner, since, fingerprint

    def _serve(self, store: Store) -> None:
        by_relay, owner, since, fingerprint = self._plan(store)
        if not by_relay:
            self.stop.wait(self.recheck)
            return
        inbox: queue.Queue[Message] = queue.Queue()
        subs_stop = threading.Event()
        transports = {relay: transport_for(self.host, relay) for relay in by_relay}
        for relay, topics in by_relay.items():
            for i in range(0, len(topics), 150):
                pump(transports[relay], topics[i : i + 150], str(since[relay]), subs_stop, inbox)
        topic_relay = {t: r for r, ts in by_relay.items() for t in ts}
        last_check = last_ack = time.monotonic()
        try:
            while not self.stop.is_set():
                try:
                    msg = inbox.get(timeout=0.5)
                except queue.Empty:
                    msg = None
                if msg is not None and msg.topic in owner:
                    self._handle(store, owner[msg.topic], msg, topic_relay[msg.topic])
                now = time.monotonic()
                if now - last_ack >= self.ack_every:
                    last_ack = now
                    self._housekeeping(store)
                if now - last_check >= self.recheck:
                    last_check = now
                    if not self._ready() or not self._lease(store):
                        return
                    if self._plan(store)[3] != fingerprint:
                        return
        finally:
            subs_stop.set()
            for t in transports.values():
                try:
                    t.close()
                except Exception:
                    pass

    def _handle(self, store: Store, peer, msg: Message, relay: str) -> None:
        current = store.find_peer(peer.peer_id)
        if current is None or current.status != "active":
            return
        try:
            received = channel_mod.process_frame(self.host, store, current, msg.data)
        except Exception:
            logger.warning("nachalo: could not process a frame", exc_info=True)
            return
        with store.tx() as c:
            previous = int(store.meta(f"since:{relay}", "0") or 0)
            store.set_meta(c, f"since:{relay}", str(max(previous, msg.time)))
        self.processed += 1
        if received is not None and received.stored:
            self.host.broadcast("inbox.count", {"held": sum(store.inbox_counts().values())})
        if received is not None and received.kind == "close":
            self.host.broadcast("peers.changed", {"count": len(store.peers())})

    def _housekeeping(self, store: Store) -> None:
        for peer in store.peers():
            try:
                channel_mod.send_acks(self.host, store, transport_for(self.host), peer)
            except Exception:
                logger.debug("nachalo: ack failed", exc_info=True)
        try:
            channel_mod.retry_outbox(self.host, store, transport_for)
            store.prune(int(self.host.settings().get("inbox_retention_days", 30)))
        except Exception:
            logger.debug("nachalo: housekeeping failed", exc_info=True)
