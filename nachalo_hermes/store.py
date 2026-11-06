"""``plugin_db("nachalo")``: peers, sealed channel state, inbox, drafts, outbox and audit.

Never stored: invitation words, pending handshake state or keys in the clear. The channel state
is a blob Rust sealed with the store key. There is no "approved" column anywhere: a draft only
gets ``sent_at``, written by the human-run handler after it publishes.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass

from .host import Host

SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen INTEGER,
    safety_words TEXT NOT NULL,
    relay_url TEXT NOT NULL,
    features TEXT NOT NULL,
    capabilities TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_state (
    peer_id TEXT PRIMARY KEY REFERENCES peers(peer_id) ON DELETE CASCADE,
    sealed_blob BLOB NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    type TEXT NOT NULL,
    reply_to TEXT,
    text TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (peer_id, id)
);
CREATE TABLE IF NOT EXISTS inbox_parts (
    peer_id TEXT NOT NULL,
    id TEXT NOT NULL,
    part INTEGER NOT NULL,
    total INTEGER NOT NULL,
    body TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, id, part)
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    peer_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    type TEXT NOT NULL,
    reply_to TEXT,
    text TEXT NOT NULL,
    sent_at INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT NOT NULL,
    part INTEGER NOT NULL,
    peer_id TEXT NOT NULL,
    counter INTEGER NOT NULL,
    tag TEXT NOT NULL,
    frame BLOB NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_attempt INTEGER,
    PRIMARY KEY (peer_id, id, part)
);
CREATE TABLE IF NOT EXISTS seen (
    peer_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, direction, message_id)
);
CREATE TABLE IF NOT EXISTS audit (
    ts INTEGER NOT NULL,
    event TEXT NOT NULL,
    peer_id TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_CAPABILITIES = {"receive_note": True, "receive_ask": True}
SEEN_TTL = 7 * 86400

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(now_ms: int | None = None) -> str:
    """A ULID: sortable, opaque, 26 characters."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


@dataclass
class Peer:
    peer_id: str
    name: str
    role: str
    status: str
    created_at: int
    last_seen: int | None
    safety_words: str
    relay_url: str
    features: list
    capabilities: dict

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Peer:
        return cls(
            peer_id=row["peer_id"],
            name=row["name"],
            role=row["role"],
            status=row["status"],
            created_at=row["created_at"],
            last_seen=row["last_seen"],
            safety_words=row["safety_words"],
            relay_url=row["relay_url"],
            features=json.loads(row["features"]),
            capabilities=json.loads(row["capabilities"]),
        )

    def public(self) -> dict:
        """What tools and the UI may see: no secrets, no topics."""
        return {
            "peer_id": self.peer_id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "capabilities": self.capabilities,
        }


class Store:
    def __init__(self, host: Host):
        self.host = host
        self.conn = host.db()
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # explicit transactions only
        self.conn.executescript(SCHEMA)
        _restrict_permissions(host)

    def close(self) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the database lock up front, so the listener and a
        human send in another process never interleave a read-modify-write of channel state."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # peers ---------------------------------------------------------------------------------

    def add_peer(self, peer: Peer, sealed_state: bytes) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO peers VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    peer.peer_id,
                    peer.name,
                    peer.role,
                    peer.status,
                    peer.created_at,
                    peer.last_seen,
                    peer.safety_words,
                    peer.relay_url,
                    json.dumps(peer.features),
                    json.dumps(peer.capabilities),
                ),
            )
            c.execute(
                "INSERT OR REPLACE INTO channel_state VALUES (?,?,?)", (peer.peer_id, sealed_state, int(time.time()))
            )
            self._audit(c, "paired", peer.peer_id, peer.role)

    def peers(self, include_revoked: bool = False) -> list[Peer]:
        sql = "SELECT * FROM peers" + ("" if include_revoked else " WHERE status = 'active'") + " ORDER BY created_at"
        return [Peer.from_row(r) for r in self.conn.execute(sql)]

    def find_peer(self, ref: str) -> Peer | None:
        """By peer id (with or without dashes), its prefix, or its exact name."""
        ref = ref.strip()
        bare = ref.replace("-", "").upper()
        rows = list(self.conn.execute("SELECT * FROM peers"))
        for row in rows:
            if row["peer_id"] == bare:
                return Peer.from_row(row)
        matches = [r for r in rows if len(bare) >= 4 and r["peer_id"].startswith(bare)]
        matches = matches or [r for r in rows if r["name"].casefold() == ref.casefold()]
        active = [r for r in matches if r["status"] == "active"] or matches
        return Peer.from_row(active[0]) if len(active) == 1 else None

    def touch_peer(self, c: sqlite3.Connection, peer_id: str) -> None:
        c.execute("UPDATE peers SET last_seen = ? WHERE peer_id = ?", (int(time.time()), peer_id))

    def set_peer_status(self, c: sqlite3.Connection, peer_id: str, status: str) -> None:
        c.execute("UPDATE peers SET status = ? WHERE peer_id = ?", (status, peer_id))

    def sealed_state(self, c: sqlite3.Connection, peer_id: str) -> bytes | None:
        row = c.execute("SELECT sealed_blob FROM channel_state WHERE peer_id = ?", (peer_id,)).fetchone()
        return bytes(row[0]) if row else None

    def save_state(self, c: sqlite3.Connection, peer_id: str, blob: bytes) -> None:
        c.execute(
            "UPDATE channel_state SET sealed_blob = ?, updated_at = ? WHERE peer_id = ?",
            (blob, int(time.time()), peer_id),
        )

    def forget_channel(self, c: sqlite3.Connection, peer_id: str) -> None:
        c.execute("DELETE FROM channel_state WHERE peer_id = ?", (peer_id,))
        c.execute("DELETE FROM outbox WHERE peer_id = ?", (peer_id,))
        c.execute("DELETE FROM seen WHERE peer_id = ?", (peer_id,))
        c.execute("DELETE FROM inbox_parts WHERE peer_id = ?", (peer_id,))

    # inbox ---------------------------------------------------------------------------------

    def inbox(self, peer_id: str | None = None, states=("held",), limit: int = 20) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in states)
        sql = f"SELECT * FROM inbox WHERE state IN ({marks})"
        args: list = list(states)
        if peer_id:
            sql += " AND peer_id = ?"
            args.append(peer_id)
        sql += " ORDER BY received_at, id LIMIT ?"
        args.append(int(limit))
        return list(self.conn.execute(sql, args))

    def inbox_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT peer_id, COUNT(*) AS n FROM inbox WHERE state = 'held' GROUP BY peer_id")
        return {r["peer_id"]: r["n"] for r in rows}

    def mark_read(self, ids: list[tuple[str, str]]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE inbox SET state = 'read' WHERE peer_id = ? AND id = ?", ids)

    def discard_inbox(self, peer_id: str, message_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE inbox SET state = 'discarded', text = '' WHERE peer_id = ? AND id = ?", (peer_id, message_id)
            )
            return cur.rowcount > 0

    # drafts --------------------------------------------------------------------------------

    def add_draft(self, peer_id: str, text: str, type_: str, reply_to: str | None) -> str:
        draft_id = new_id()
        with self.tx() as c:
            c.execute(
                "INSERT INTO drafts VALUES (?,?,?,?,?,?,NULL)",
                (draft_id, peer_id, int(time.time()), type_, reply_to, text),
            )
            self._audit(c, "draft", peer_id, draft_id)
        return draft_id

    def draft(self, draft_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id.strip().upper(),)).fetchone()

    def drafts(self, pending_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM drafts" + (" WHERE sent_at IS NULL" if pending_only else "") + " ORDER BY created_at"
        return list(self.conn.execute(sql))

    def discard_draft(self, draft_id: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM drafts WHERE id = ? AND sent_at IS NULL", (draft_id,)).rowcount > 0

    # rate limits ---------------------------------------------------------------------------

    def sent_last_hour(self, peer_id: str) -> int:
        cutoff = int(time.time()) - 3600
        return self.conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE peer_id = ? AND sent_at >= ?", (peer_id, cutoff)
        ).fetchone()[0]

    def received_last_hour(self, c: sqlite3.Connection, peer_id: str) -> int:
        cutoff = int(time.time()) - 3600
        return c.execute(
            "SELECT COUNT(*) FROM inbox WHERE peer_id = ? AND received_at >= ?", (peer_id, cutoff)
        ).fetchone()[0]

    # meta and audit ------------------------------------------------------------------------

    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, c: sqlite3.Connection, key: str, value: str) -> None:
        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def audit(self, event: str, peer_id: str | None = None, detail: str | None = None) -> None:
        with self.tx() as c:
            self._audit(c, event, peer_id, detail)

    @staticmethod
    def _audit(c: sqlite3.Connection, event: str, peer_id: str | None, detail: str | None) -> None:
        c.execute("INSERT INTO audit VALUES (?,?,?,?)", (int(time.time()), event, peer_id, detail))

    def prune(self, retention_days: int) -> None:
        now = int(time.time())
        with self.tx() as c:
            c.execute("DELETE FROM seen WHERE seen_at < ?", (now - SEEN_TTL,))
            c.execute("DELETE FROM inbox_parts WHERE received_at < ?", (now - SEEN_TTL,))
            c.execute("DELETE FROM inbox WHERE received_at < ?", (now - retention_days * 86400,))
            c.execute("DELETE FROM outbox WHERE state = 'acked' OR created_at < ?", (now - SEEN_TTL,))

    def purge(self) -> None:
        with self.tx() as c:
            for table in (
                "channel_state",
                "peers",
                "inbox",
                "inbox_parts",
                "drafts",
                "outbox",
                "seen",
                "audit",
                "meta",
            ):
                c.execute(f"DELETE FROM {table}")


def _restrict_permissions(host: Host) -> None:
    """0700 for the data directory and 0600 for its files."""
    try:
        directory = host.data_dir()
        os.chmod(directory, 0o700)
        for child in directory.iterdir():
            if child.is_file():
                os.chmod(child, 0o600)
    except OSError:
        pass
