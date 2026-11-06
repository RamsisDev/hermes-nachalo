"""Live pairing over a relay (design §7). Surface-agnostic: the CLI and the Desktop backend
drive the same state machines.

Invariants held here:

- I1: the invitation words exist only in the caller's local UI; nothing here logs them.
- I4: a mismatch or abort drops every handshake object; nothing pending is persisted (§6.2).
- I5: payloads are public and padded to 256 bytes (:mod:`.payload`).
"""

from __future__ import annotations

import hashlib
import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import nachalo

from . import payload as payload_mod
from .clock import clock, monotonic_counting_sleep, now_unix
from .encoding import CONTEXT, format_peer_id, msg2_topic, msg3_topic, peer_id, rendezvous_topic
from .host import Host
from .store import DEFAULT_CAPABILITIES, Peer, Store
from .transport import Message, RelayError, Transport, pump

logger = logging.getLogger("nachalo")

MAX_ANSWERS = 64
TTL = 600


class PairingError(Exception):
    """Why pairing stopped. ``code``: expired, mismatch, aborted, relay, too_many, bad_words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Paired:
    peer: Peer
    safety_words: str

    @property
    def display_id(self) -> str:
        return format_peer_id(self.peer.peer_id)


def _config():
    return nachalo.Config(nachalo.Profile.INTERACTIVE).with_max_answers(MAX_ANSWERS)


def normalize_words(text: str) -> str:
    """Tolerate case, extra spaces, commas and dashes in typed words."""
    cleaned = text.replace(",", " ").replace("-", " ").replace("\n", " ").replace("\t", " ")
    return " ".join(w for w in cleaned.lower().split(" ") if w)


def complete(host: Host, store: Store, established, own_relay: str, safety_words: str) -> Paired:
    """Derive the channel inside Rust, seal its state and register the peer (both roles)."""
    pid = peer_id(established.transcript())
    channel = nachalo.Channel.from_established(established, pid.encode("ascii"))
    peer_payload = payload_mod.decode(established.peer_payload())
    store_key = host.store_key(create=True)
    blob = channel.seal_state(store_key)
    role = "initiator" if established.role() == nachalo.Role.INITIATOR else "responder"
    peer = Peer(
        peer_id=pid,
        name=peer_payload.name,
        role=role,
        status="active",
        created_at=now_unix(),
        last_seen=None,
        safety_words=safety_words,
        relay_url=peer_payload.relay or own_relay,
        features=list(peer_payload.features),
        capabilities=dict(DEFAULT_CAPABILITIES),
    )
    store.add_peer(peer, blob)
    host.broadcast("pairing.state", {"state": "paired", "peer_id": pid})
    return Paired(peer=peer, safety_words=safety_words)


class _Session:
    def __init__(self, transport: Transport, payload: bytes, on_state: Callable[[str], None] | None = None):
        self.transport = transport
        self.payload = payload
        self.on_state = on_state or (lambda state: None)
        self.stop = threading.Event()
        self.deadline = monotonic_counting_sleep() + TTL
        self._subs_stop = threading.Event()
        self._inbox: queue.Queue[Message] = queue.Queue()

    def abort(self) -> None:
        self.stop.set()
        self._subs_stop.set()

    def seconds_left(self) -> int:
        return max(0, self.deadline - monotonic_counting_sleep())

    def _state(self, state: str) -> None:
        try:
            self.on_state(state)
        except Exception:
            logger.debug("nachalo: pairing state callback failed", exc_info=True)

    def _listen(self, topics: list[str], since: str) -> None:
        pump(self.transport, topics, since, self._subs_stop, self._inbox)

    def _next(self) -> Message | None:
        """The next relayed message, or ``None`` after a short wait; raises once expired or
        aborted, after dropping every handshake object."""
        if self.stop.is_set():
            self._end()
            raise PairingError("aborted", "Pairing cancelled.")
        if self.seconds_left() <= 0:
            self._end()
            raise PairingError("expired", self._expired_message)
        try:
            return self._inbox.get(timeout=0.25)
        except queue.Empty:
            return None

    def _end(self) -> None:
        self._subs_stop.set()
        self._drop()

    def _drop(self) -> None:
        raise NotImplementedError


class InitiatorPairing(_Session):
    """Role A: draws the words, publishes msg1, takes msg2, sends msg3, verifies safety words."""

    _expired_message = "Nobody joined within 10 minutes. Start again for new words."

    def __init__(self, transport: Transport, payload: bytes, **kwargs):
        super().__init__(transport, payload, **kwargs)
        self._initiator = None
        self._unconfirmed = None
        self._established = None
        self._tried: set[bytes] = set()
        self.rejected = 0

    def start(self) -> str:
        """Publish msg1; return the 6 words for the local UI only (I1)."""
        words = nachalo.words(6)
        invitation = nachalo.Invitation.live(words, CONTEXT, _config())
        self._initiator = nachalo.Initiator.start(invitation, self.payload, clock())
        self.deadline = self._initiator.deadline()
        since = str(now_unix() - 2)
        self._listen([msg2_topic(self._initiator.msg1())], since)
        try:
            self.transport.publish(rendezvous_topic(invitation.rendezvous()), self._initiator.msg1())
        except RelayError as exc:
            self._end()
            raise PairingError("relay", str(exc)) from None
        self._state("waiting")
        return words

    def wait_for_peer(self) -> None:
        """Block until a msg2 completes the handshake and msg3 is published."""
        if self._initiator is None:
            raise PairingError("aborted", "pairing was not started")
        while True:
            msg = self._next()
            if msg is not None and self._try_msg2(msg.data):
                self._subs_stop.set()
                return

    def _try_msg2(self, msg2: bytes) -> bool:
        digest = hashlib.sha256(msg2).digest()
        if digest in self._tried:
            return False
        self._tried.add(digest)
        try:
            finished = self._initiator.finish(msg2, clock())
        except nachalo.NachaloError.Rejected:
            self.rejected += 1
            if self.rejected >= MAX_ANSWERS:
                self._end()
                raise PairingError("too_many", "Too many wrong answers: possible attack. Start again.") from None
            return False
        except nachalo.NachaloError.Expired:
            self._end()
            raise PairingError("expired", "The invitation expired.") from None
        try:
            self.transport.publish(msg3_topic(msg2), finished.msg3)
        except RelayError as exc:
            self._end()
            raise PairingError("relay", str(exc)) from None
        self._unconfirmed = finished.unconfirmed
        self._state("verifying")
        return True

    def verify(self, typed: str) -> bool:
        """A types the 4 words B reads aloud. ``False`` on a mismatch: retry until the deadline."""
        if self._unconfirmed is None:
            raise PairingError("aborted", "nothing to verify")
        if self.seconds_left() <= 0:
            self._end()
            raise PairingError("expired", "The pairing expired before the safety words matched.")
        try:
            self._established = self._unconfirmed.verify(normalize_words(typed), clock())
        except nachalo.NachaloError.Rejected:
            return False
        except nachalo.NachaloError.Expired:
            self._end()
            raise PairingError("expired", "The pairing expired before the safety words matched.") from None
        return True

    def finish(self, host: Host, store: Store, own_relay: str) -> Paired:
        if self._established is None:
            raise PairingError("aborted", "safety words not verified")
        try:
            return complete(host, store, self._established, own_relay, self._unconfirmed.safety_words())
        finally:
            self._end()

    def mismatch(self) -> None:
        """The person says the words really differ: discard everything (I4)."""
        self._end()
        self._state("mismatch")

    def _drop(self) -> None:
        self._initiator = self._unconfirmed = self._established = None


class ResponderPairing(_Session):
    """Role B: types the words, answers every msg1 at the rendezvous (up to 64), shows the safety
    words and confirms only when A says they match."""

    _expired_message = "Nobody was found with those words. Check them, or ask for new ones."

    def __init__(
        self,
        transport: Transport,
        payload: bytes,
        words: str,
        answer_jitter: tuple[float, float] = (0.0, 0.0),
        **kwargs,
    ):
        super().__init__(transport, payload, **kwargs)
        try:
            invitation = nachalo.Invitation.live(normalize_words(words), CONTEXT, _config())
        except nachalo.NachaloError.BadWords:
            raise PairingError("bad_words", "Those are not 6 words from the list. Check and retype them.") from None
        self._rendezvous = rendezvous_topic(invitation.rendezvous())
        self._listener = nachalo.Listener(invitation)
        self._jitter = answer_jitter
        self._pending: dict[str, object] = {}
        self._answered: set[bytes] = set()
        self._unconfirmed = None

    def wait_for_initiator(self) -> str:
        """Block until one answer completes; return the 4 safety words to show (I3)."""
        self._listen([self._rendezvous], "10m")
        self._state("searching")
        while True:
            msg = self._next()
            if msg is None:
                continue
            if msg.topic == self._rendezvous:
                self._answer(msg.data)
            elif msg.topic in self._pending:
                safety = self._try_msg3(msg.topic, msg.data)
                if safety:
                    self._subs_stop.set()
                    return safety

    def _answer(self, msg1: bytes) -> None:
        digest = hashlib.sha256(msg1).digest()
        if digest in self._answered:
            return
        self._answered.add(digest)
        low, high = self._jitter
        if high > 0:
            time.sleep(random.uniform(low, high))
        try:
            answer = self._listener.respond(msg1, self.payload, clock())
        except nachalo.NachaloError.Rejected:
            return
        except nachalo.NachaloError.Expired:
            self._end()
            raise PairingError(
                "too_many", "Too many attempts at this invitation: possible attack. Ask for new words."
            ) from None
        topic = msg3_topic(answer.msg2)
        self._pending[topic] = answer.responder
        self._listen([topic], str(now_unix() - 2))
        try:
            self.transport.publish(msg2_topic(msg1), answer.msg2)
        except RelayError as exc:
            self._end()
            raise PairingError("relay", str(exc)) from None

    def _try_msg3(self, topic: str, msg3: bytes) -> str | None:
        responder = self._pending.get(topic)
        try:
            unconfirmed = responder.finish(msg3, clock())
        except nachalo.NachaloError.Rejected, nachalo.NachaloError.Expired:
            return None
        self._unconfirmed = unconfirmed
        self._pending.clear()
        self._state("verifying")
        return unconfirmed.safety_words()

    def confirm(self, host: Host, store: Store, own_relay: str) -> Paired:
        """A said the words match (B's UI defaults to No)."""
        if self._unconfirmed is None:
            raise PairingError("aborted", "nothing to confirm")
        if self.seconds_left() <= 0:
            self._end()
            raise PairingError("expired", "The pairing expired before it was confirmed.")
        safety = self._unconfirmed.safety_words()
        try:
            established = self._unconfirmed.confirm(clock())
        except nachalo.NachaloError.Expired:
            self._end()
            raise PairingError("expired", "The pairing expired before it was confirmed.") from None
        try:
            return complete(host, store, established, own_relay, safety)
        finally:
            self._end()

    def mismatch(self) -> None:
        self._end()
        self._state("mismatch")

    def _drop(self) -> None:
        self._listener = None
        self._unconfirmed = None
        self._pending.clear()
