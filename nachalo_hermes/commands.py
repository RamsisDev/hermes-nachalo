"""``/nachalo`` inside a session. The reply goes straight to the chat, never into a model turn.

``/nachalo send <id>`` is the chat's human send: it shows the peer and the exact text, asks with
``ctx.ask_choice`` and sends in the same handler. ``ask_choice`` returns ``None`` when nobody can
answer (cron, hooks), so nothing is sent then. Pairing is refused here on purpose: a command
reply on Telegram or Discord crosses that platform's servers (I1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import channel as channel_mod
from .encoding import format_peer_id
from .host import get_host
from .runtime import transport_for
from .store import Store

PAIR_HINT = (
    "To pair, run `hermes nachalo pair` (or `join`) in your own terminal, or use the "
    "Pair button in Hermes Desktop. Words never go through a chat."
)
LOCAL_PLATFORMS = {"local", "cli", "tui", "desktop", ""}
USAGE = "Usage: /nachalo [status | drafts | send <draft_id> | discard <draft_id> | pair]"


def make_handler(ask_choice: Callable[[str, list], str | None]):
    def handle(raw_args: str = "", *, platform: str = "", source: Any = None, **kwargs) -> str:
        words = (raw_args or "").strip().split()
        sub = words[0].lower() if words else "status"
        host = get_host()
        store = Store(host)
        try:
            if sub in ("status", ""):
                return _status(store)
            if sub in ("pair", "join"):
                return PAIR_HINT
            if sub == "drafts":
                return _drafts(store)
            if sub == "discard" and len(words) == 2:
                return "Draft discarded." if store.discard_draft(words[1].upper()) else "No pending draft with that id."
            if sub == "send" and len(words) == 2:
                return _send(host, store, words[1], platform, source, ask_choice)
            return USAGE
        finally:
            store.close()

    return handle


def _status(store: Store) -> str:
    peers = store.peers()
    held = sum(store.inbox_counts().values())
    drafts = len(store.drafts())
    return (
        f"Nachalo: {len(peers)} paired peer(s), {held} held message(s), {drafts} pending draft(s). "
        f"/nachalo drafts lists the drafts."
    )


def _drafts(store: Store) -> str:
    rows = store.drafts()
    if not rows:
        return "No pending drafts."
    names = {p.peer_id: p.name for p in store.peers(include_revoked=True)}
    lines = []
    for row in rows:
        preview = row["text"].replace("\n", " ")
        preview = preview[:80] + ("…" if len(preview) > 80 else "")
        lines.append(f"{row['id']} → {names.get(row['peer_id'], '?')}: {preview}")
    return "\n".join(lines) + "\nSend one with /nachalo send <id>."


def _send(host, store: Store, draft_id: str, platform: str, source: Any, ask_choice) -> str:
    if host.in_cron():
        return "Nothing sent: cron runs cannot send peer messages."
    if source is None and (platform or "").lower() not in LOCAL_PLATFORMS:
        return "Nothing sent: this chat could not be identified."
    draft, peer, problem = channel_mod.check_draft(store, host, draft_id.upper())
    if problem:
        return f"Nothing sent. {problem}"
    question = f"Send this to {peer.name} ({format_peer_id(peer.peer_id)})?\n----\n{draft['text']}\n----"
    choice = ask_choice(
        question,
        [
            {"value": "send", "label": "Send", "description": "encrypt and send exactly this text"},
            {"value": "cancel", "label": "Cancel"},
        ],
    )
    if choice != "send":
        return "Nothing sent."
    try:
        result = channel_mod.send_draft(host, store, transport_for(host), draft["id"])
    except channel_mod.SendError as exc:
        return f"Nothing sent. {exc}"
    if result.published < result.frames:
        return f"Sealed for {peer.name}; the relay is not answering, it will be retried in the background."
    return f"Sent to {peer.name}."
