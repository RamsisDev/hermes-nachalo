"""Thin layer over ``nachalo.Channel`` (Rust): sending a draft, opening frames, acks, revocation.

Every state change runs as open → operate → seal inside one ``BEGIN IMMEDIATE`` transaction, so
two processes never advance the same ratchet from the same snapshot. A frame is sealed and the
new state committed *before* it is published: publishing first and crashing would reuse a
message key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

import nachalo

from . import inbox as inbox_mod
from . import policy
from .clock import now_unix
from .encoding import lv_cat
from .host import Host
from .store import Peer, Store, new_id
from .transport import RelayError, Transport

logger = logging.getLogger("nachalo")

MAX_PARTS = 8
ACK_RETRY_AFTER = 60
MAX_ATTEMPTS = 3
RECEIVE_PER_HOUR = 120
ALLOWED_TYPES = {"note", "ask", "answer"}


class SendError(Exception):
    pass


@dataclass
class SendResult:
    draft_id: str
    peer: Peer
    frames: int
    published: int


def _require_key(host: Host) -> bytes:
    key = host.store_key(create=False)
    if key is None:
        raise SendError("No store key: pair with someone first.")
    return key


def _open(store: Store, c, peer_id: str, key: bytes):
    blob = store.sealed_state(c, peer_id)
    if blob is None:
        raise SendError("No channel for that peer (revoked or purged).")
    return nachalo.Channel.open_state(blob, key, peer_id.encode("ascii"))


def _outbox_tag(key: bytes, peer_id: str, message_id: str, part: int, counter: int, frame: bytes) -> str:
    mac_key = hmac.new(key, b"hermes-nachalo/1 outbox", hashlib.sha256).digest()
    data = lv_cat(peer_id.encode(), message_id.encode(), str(part).encode(), str(counter).encode(), frame)
    return hmac.new(mac_key, data, hashlib.sha256).hexdigest()


def split_text(message_id: str, type_: str, reply_to: str | None, text: str) -> list[bytes]:
    """JSON bodies of at most ``channel_max_body()`` bytes each, in order."""
    limit = nachalo.channel_max_body()

    def body(chunk: str, part: int, total: int) -> bytes:
        doc = {
            "id": message_id,
            "type": type_,
            "reply_to": reply_to,
            "ts": now_unix(),
            "part": [part, total],
            "text": chunk,
        }
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()

    chunks: list[str] = []
    rest = text
    while rest:
        size = min(len(rest), limit)
        while len(body(rest[:size], MAX_PARTS, MAX_PARTS)) > limit:
            size = int(size * 0.9)
        chunks.append(rest[:size])
        rest = rest[size:]
    chunks = chunks or [""]
    if len(chunks) > MAX_PARTS:
        raise SendError(f"Message too long: more than {MAX_PARTS} frames.")
    return [body(chunk, i + 1, len(chunks)) for i, chunk in enumerate(chunks)]


def check_draft(store: Store, host: Host, draft_id: str, *, force: bool = False):
    """What a human handler shows before confirming: the peer, the exact text, and any refusal.
    Returns ``(draft_row, peer, problem)``; ``problem`` is ``None`` when it may be sent."""
    draft = store.draft(draft_id)
    if draft is None:
        return None, None, "No draft with that id."
    peer = store.find_peer(draft["peer_id"])
    if draft["sent_at"] is not None:
        return draft, peer, "That draft was already sent."
    if peer is None or peer.status != "active":
        return draft, peer, "That peer is no longer active."
    findings = policy.scan(draft["text"])
    if findings and not force:
        return draft, peer, policy.describe(findings)
    limit = int(host.settings().get("rate_limit_per_hour", 30))
    if store.sent_last_hour(peer.peer_id) >= limit:
        return draft, peer, f"Rate limit: at most {limit} messages per hour to one peer."
    return draft, peer, None


def send_draft(host: Host, store: Store, transport: Transport, draft_id: str, *, force: bool = False) -> SendResult:
    """Seal and publish a draft. Call **only** from a handler a person triggered, after showing
    them the exact text: this is the one path by which content leaves (I8)."""
    draft, peer, problem = check_draft(store, host, draft_id, force=force)
    if problem:
        raise SendError(problem)
    key = _require_key(host)
    bodies = split_text(draft["id"], draft["type"], draft["reply_to"], draft["text"])
    now = now_unix()
    frames = []
    with store.tx() as c:
        if c.execute("SELECT sent_at FROM drafts WHERE id = ?", (draft["id"],)).fetchone()[0] is not None:
            raise SendError("That draft was already sent.")
        ch = _open(store, c, peer.peer_id, key)
        topic = ch.send_topic(nachalo.channel_epoch(now))
        for part, body in enumerate(bodies, start=1):
            counter = ch.sent()
            frame = ch.seal(nachalo.FrameKind.DATA, body, now)
            frames.append((part, frame))
            c.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    draft["id"],
                    part,
                    peer.peer_id,
                    counter,
                    _outbox_tag(key, peer.peer_id, draft["id"], part, counter, frame),
                    frame,
                    "data",
                    "queued",
                    0,
                    now,
                ),
            )
        store.save_state(c, peer.peer_id, ch.seal_state(key))
        c.execute("UPDATE drafts SET sent_at = ? WHERE id = ?", (now, draft["id"]))
        store._audit(c, "sent", peer.peer_id, f"{draft['id']} frames={len(frames)}")
    published = _publish_frames(store, transport, peer.peer_id, draft["id"], topic, frames)
    host.broadcast("drafts.changed", {"pending": len(store.drafts())})
    return SendResult(draft_id=draft["id"], peer=peer, frames=len(frames), published=published)


def _publish_frames(store: Store, transport: Transport, peer_id: str, message_id: str, topic: str, frames: list) -> int:
    published = 0
    for part, frame in frames:
        try:
            transport.publish(topic, frame)
        except RelayError:
            with store.tx() as c:
                c.execute(
                    "UPDATE outbox SET attempts = attempts + 1, last_attempt = ? "
                    "WHERE peer_id = ? AND id = ? AND part = ?",
                    (now_unix(), peer_id, message_id, part),
                )
            continue
        published += 1
        with store.tx() as c:
            c.execute(
                "UPDATE outbox SET state = 'published', attempts = attempts + 1, last_attempt = ? "
                "WHERE peer_id = ? AND id = ? AND part = ?",
                (now_unix(), peer_id, message_id, part),
            )
    return published


def retry_outbox(host: Host, store: Store, transport_for) -> int:
    """Republish frames a person already sent that the relay refused or the peer did not ack.

    Rows are not trusted: anything written into ``plugin_db`` by someone without the store key
    fails its tag and is dropped, and the topic is recomputed from the channel, never read from
    the row. So writing to the database cannot make this publish new content or to a new place.
    """
    key = host.store_key(create=False)
    if key is None:
        return 0
    now = now_unix()
    rows = list(
        store.conn.execute(
            "SELECT * FROM outbox WHERE state IN ('queued','published') AND attempts < ? "
            "AND (state = 'queued' OR last_attempt < ?)",
            (MAX_ATTEMPTS, now - ACK_RETRY_AFTER),
        )
    )
    done = 0
    for row in rows:
        frame = bytes(row["frame"])
        expected = _outbox_tag(key, row["peer_id"], row["id"], row["part"], row["counter"], frame)
        if not hmac.compare_digest(expected, row["tag"]):
            with store.tx() as c:
                c.execute(
                    "DELETE FROM outbox WHERE peer_id = ? AND id = ? AND part = ?",
                    (row["peer_id"], row["id"], row["part"]),
                )
                store._audit(c, "outbox_forged", row["peer_id"], None)
            continue
        peer = store.find_peer(row["peer_id"])
        if peer is None or peer.status != "active":
            continue
        with store.tx() as c:
            topic = _open(store, c, peer.peer_id, key).send_topic(nachalo.channel_epoch(now))
        done += _publish_frames(store, transport_for(host), peer.peer_id, row["id"], topic, [(row["part"], frame)])
    return done


# --- receiving -----------------------------------------------------------------------------


@dataclass
class Received:
    peer_id: str
    kind: str
    message_id: str | None = None
    stored: bool = False


def process_frame(host: Host, store: Store, peer: Peer, frame: bytes) -> Received | None:
    """Open one frame from ``peer`` and file it. Nothing here calls the agent or a tool (I7)."""
    key = host.store_key(create=False)
    if key is None:
        return None
    settings = host.settings()
    now = now_unix()
    with store.tx() as c:
        blob = store.sealed_state(c, peer.peer_id)
        if blob is None:
            return None
        ch = nachalo.Channel.open_state(blob, key, peer.peer_id.encode("ascii"))
        try:
            opened = ch.open(frame, now)
        except nachalo.ChannelError.Rejected:
            store._audit(c, "bad_frame", peer.peer_id, None)
            return None
        except nachalo.ChannelError.Replay, nachalo.ChannelError.TooFarAhead:
            return None
        store.save_state(c, peer.peer_id, ch.seal_state(key))
        store.touch_peer(c, peer.peer_id)

        if opened.kind == nachalo.FrameKind.CLOSE:
            store.forget_channel(c, peer.peer_id)
            store.set_peer_status(c, peer.peer_id, "revoked_by_peer")
            store._audit(c, "revoked_by_peer", peer.peer_id, None)
            return Received(peer.peer_id, "close")
        body = _parse_body(opened.body)
        if body is None:
            store._audit(c, "bad_body", peer.peer_id, None)
            return None
        if opened.kind == nachalo.FrameKind.ACK:
            ids = [i for i in body.get("ids", []) if isinstance(i, str)][:256]
            c.executemany(
                "UPDATE outbox SET state = 'acked' WHERE peer_id = ? AND id = ?", [(peer.peer_id, i) for i in ids]
            )
            return Received(peer.peer_id, "ack")
        if opened.kind != nachalo.FrameKind.DATA:
            return None
        return _file_data(store, c, peer, body, settings, now)


def _parse_body(raw: bytes) -> dict | None:
    try:
        body = json.loads(bytes(raw).decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _file_data(store: Store, c, peer: Peer, body: dict, settings: dict, now: int) -> Received | None:
    message_id = body.get("id")
    type_ = body.get("type")
    text = body.get("text")
    part = body.get("part") or [1, 1]
    if not (isinstance(message_id, str) and 0 < len(message_id) <= 64 and isinstance(text, str)):
        store._audit(c, "bad_body", peer.peer_id, None)
        return None
    if type_ == "hello":
        store._audit(c, "channel_ok", peer.peer_id, None)
        return Received(peer.peer_id, "hello", message_id)
    if type_ not in ALLOWED_TYPES:
        store._audit(c, "type_refused", peer.peer_id, str(type_)[:16])
        return None
    capability = "receive_ask" if type_ == "ask" else "receive_note"
    if not peer.capabilities.get(capability, False):
        store._audit(c, "capability_refused", peer.peer_id, type_)
        return None
    if c.execute(
        "SELECT 1 FROM seen WHERE peer_id = ? AND direction = 'in' AND message_id = ?", (peer.peer_id, message_id)
    ).fetchone():
        return Received(peer.peer_id, "duplicate", message_id)
    try:
        index, total = int(part[0]), int(part[1])
    except TypeError, ValueError, IndexError:
        return None
    if not (1 <= index <= total <= MAX_PARTS):
        return None
    reply_to = body.get("reply_to") if isinstance(body.get("reply_to"), str) else None

    if total > 1:
        c.execute(
            "INSERT OR IGNORE INTO inbox_parts VALUES (?,?,?,?,?,?)",
            (peer.peer_id, message_id, index, total, text, now),
        )
        parts = list(
            c.execute(
                "SELECT part, body FROM inbox_parts WHERE peer_id = ? AND id = ? ORDER BY part",
                (peer.peer_id, message_id),
            )
        )
        if len(parts) < total:
            return Received(peer.peer_id, "part", message_id)
        text = "".join(p["body"] for p in parts)
        c.execute("DELETE FROM inbox_parts WHERE peer_id = ? AND id = ?", (peer.peer_id, message_id))

    held = c.execute("SELECT COUNT(*) FROM inbox WHERE state = 'held'").fetchone()[0]
    if held >= policy.MAX_INBOX_HELD or store.received_last_hour(c, peer.peer_id) >= RECEIVE_PER_HOUR:
        store._audit(c, "inbox_limit", peer.peer_id, message_id)
        return None
    clean = inbox_mod.sanitize(text, int(settings.get("max_message_chars", 4000)))
    c.execute(
        "INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,'held')",
        (message_id, peer.peer_id, now, type_, reply_to, clean),
    )
    c.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)", (peer.peer_id, "in", message_id, now))
    pending = json.loads(store.meta(f"ack:{peer.peer_id}", "[]") or "[]")
    store.set_meta(c, f"ack:{peer.peer_id}", json.dumps((pending + [message_id])[-256:]))
    return Received(peer.peer_id, "data", message_id, stored=True)


def send_acks(host: Host, store: Store, transport: Transport, peer: Peer) -> bool:
    """One batched ack frame for the message ids received from ``peer`` since the last one."""
    key = host.store_key(create=False)
    pending = json.loads(store.meta(f"ack:{peer.peer_id}", "[]") or "[]")
    if key is None or not pending:
        return False
    now = now_unix()
    with store.tx() as c:
        ch = _open(store, c, peer.peer_id, key)
        frame = ch.seal(nachalo.FrameKind.ACK, json.dumps({"type": "ack", "ids": pending}).encode(), now)
        topic = ch.send_topic(nachalo.channel_epoch(now))
        store.save_state(c, peer.peer_id, ch.seal_state(key))
        store.set_meta(c, f"ack:{peer.peer_id}", "[]")
    try:
        transport.publish(topic, frame)
    except RelayError:
        return False
    return True


def send_hello(host: Host, store: Store, transport: Transport, peer: Peer) -> bool:
    """The fixed first frame after pairing, part of the pairing a person just did."""
    key = _require_key(host)
    now = now_unix()
    body = json.dumps(
        {"id": new_id(), "type": "hello", "reply_to": None, "ts": now, "part": [1, 1], "text": ""}
    ).encode()
    with store.tx() as c:
        ch = _open(store, c, peer.peer_id, key)
        frame = ch.seal(nachalo.FrameKind.DATA, body, now)
        topic = ch.send_topic(nachalo.channel_epoch(now))
        store.save_state(c, peer.peer_id, ch.seal_state(key))
    try:
        transport.publish(topic, frame)
    except RelayError:
        return False
    return True


def revoke(host: Host, store: Store, transport: Transport, peer: Peer) -> bool:
    """Send ``close``, erase the channel state and mark the peer revoked. Without the keys the
    two sides stop understanding each other even if ``close`` never arrives."""
    key = host.store_key(create=False)
    frame = topic = None
    now = now_unix()
    with store.tx() as c:
        if key is not None and store.sealed_state(c, peer.peer_id) is not None:
            ch = _open(store, c, peer.peer_id, key)
            frame = ch.seal(nachalo.FrameKind.CLOSE, b"{}", now)
            topic = ch.send_topic(nachalo.channel_epoch(now))
        store.forget_channel(c, peer.peer_id)
        store.set_peer_status(c, peer.peer_id, "revoked")
        store._audit(c, "revoked", peer.peer_id, None)
    delivered = False
    if frame is not None:
        try:
            transport.publish(topic, frame)
            delivered = True
        except RelayError:
            pass
    host.broadcast("peers.changed", {"count": len(store.peers())})
    return delivered


def recv_topics(host: Host, store: Store, peer: Peer, now: int | None = None) -> list[str]:
    """Topics to read for ``peer``: epochs e-1, e and e+1."""
    key = host.store_key(create=False)
    if key is None:
        return []
    e = nachalo.channel_epoch(now_unix() if now is None else now)
    blob = store.sealed_state(store.conn, peer.peer_id)
    if blob is None:
        return []
    ch = nachalo.Channel.open_state(blob, key, peer.peer_id.encode("ascii"))
    return [ch.recv_topic(x) for x in (e - 1, e, e + 1)]
