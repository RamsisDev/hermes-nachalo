"""``hermes nachalo …``: the terminal surface. Words are shown and typed only here (I1).

Sensitive steps (pair, join, send, revoke, purge) need an interactive terminal the agent did not
launch (I9, I13).
"""

from __future__ import annotations

import argparse
import datetime
import sys
import threading
import time
from collections.abc import Callable

from . import channel as channel_mod
from .encoding import format_peer_id
from .host import Host, get_host
from .human import NotHuman, require_human_terminal
from .listener import Listener
from .pairing import InitiatorPairing, PairingError, ResponderPairing
from .runtime import own_payload, own_relay, transport_for
from .store import Store

INVITES_PER_HOUR = 6
SENSITIVE = {"pair", "join", "send", "revoke", "purge"}


def setup(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="nachalo_command")
    p = subs.add_parser("pair", help="Start a pairing (you get 6 words to read aloud)")
    p.add_argument("--name", help="Name shown to the other side, e.g. 'Ramsis · laptop'")
    p = subs.add_parser("join", help="Join a pairing with the 6 words you were given")
    p.add_argument("--name", help="Name shown to the other side")
    subs.add_parser("peers", help="List paired peers")
    p = subs.add_parser("inbox", help="Show held peer messages here, without the model")
    p.add_argument("--peer", help="Only this peer")
    subs.add_parser("drafts", help="List drafts the agent wrote")
    p = subs.add_parser("send", help="Send a draft, after showing its exact text")
    p.add_argument("draft_id")
    p.add_argument("--force", action="store_true", help="Send even if the leak filter objects")
    p = subs.add_parser("revoke", help="Revoke a peer and erase its channel")
    p.add_argument("peer")
    p = subs.add_parser("fingerprint", help="Show a peer's 4 safety words to re-verify by voice")
    p.add_argument("peer")
    subs.add_parser("purge", help="Erase all Nachalo state in this profile")
    subs.add_parser("relay-test", help="Check the relay with an ephemeral topic")
    subs.add_parser("listen", help="Run the listener in the foreground (when no gateway runs)")
    parser.set_defaults(func=main)


def main(args: argparse.Namespace, *, host: Host | None = None, out=None, ask: Callable[[str], str] = input) -> int:
    out = out or sys.stdout
    host = host or get_host()
    command = getattr(args, "nachalo_command", None)
    if not command:
        print(
            "Usage: hermes nachalo {pair,join,peers,inbox,drafts,send,revoke,fingerprint,purge,relay-test,listen}",
            file=out,
        )
        return 2
    if command in SENSITIVE:
        try:
            require_human_terminal(in_cron=host.in_cron())
        except NotHuman as exc:
            print(str(exc), file=out)
            return 3
    store = Store(host)
    try:
        return COMMANDS[command](args, host, store, out, ask)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was kept.", file=out)
        return 130
    finally:
        store.close()


def _say(out, text: str = "") -> None:
    print(text, file=out, flush=True)


def _pair(args, host: Host, store: Store, out, ask) -> int:
    recent = store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE event = 'invite' AND ts > ?", (int(time.time()) - 3600,)
    ).fetchone()[0]
    if recent >= INVITES_PER_HOUR:
        _say(out, f"At most {INVITES_PER_HOUR} invitations per hour. Try again later.")
        return 1
    store.audit("invite")
    session = InitiatorPairing(transport_for(host), own_payload(host, args.name))
    try:
        words = session.start()
    except PairingError as exc:
        _say(out, str(exc))
        return 1
    _say(out, "\nYour invitation words (one use, valid 10 minutes):\n")
    _say(out, "    " + "  ".join(w.upper() for w in words.split()))
    del words
    _say(out, "\nRead them aloud to the other person, on a call or in person.")
    _say(out, "Do not paste them into a chat, least of all one an agent reads.")
    _say(out, "They run: hermes nachalo join\n")
    _say(out, "Waiting for them… (Ctrl-C to cancel)")
    try:
        session.wait_for_peer()
    except PairingError as exc:
        _say(out, str(exc))
        return 1
    _say(out, "\nThey joined. Ask them to read you their 4 safety words, by voice or in person.")
    while True:
        typed = ask("Type the 4 safety words (or 'abort'): ").strip()
        if typed.lower() == "abort":
            session.mismatch()
            _say(out, "NOT paired. Someone may be in the middle; everything was discarded.")
            return 1
        try:
            if session.verify(typed):
                break
        except PairingError as exc:
            _say(out, str(exc))
            return 1
        _say(
            out,
            f"They don't match. Check what you typed and try again ({session.seconds_left()} s left), "
            "or type 'abort' if the words really differ.",
        )
    paired = session.finish(host, store, own_relay(host))
    _say(out, '\nMatched. Tell them: "they match".')
    _say(out, f"Paired with {paired.peer.name} ({paired.display_id}).")
    channel_mod.send_hello(host, store, transport_for(host), paired.peer)
    return 0


def _join(args, host: Host, store: Store, out, ask) -> int:
    words = ask("Type the 6 words you were given: ")
    try:
        session = ResponderPairing(transport_for(host), own_payload(host, args.name), words, answer_jitter=(0.0, 2.0))
    except PairingError as exc:
        _say(out, str(exc))
        return 1
    del words
    _say(out, "Looking for the other side… (Ctrl-C to cancel)")
    try:
        safety = session.wait_for_initiator()
    except PairingError as exc:
        _say(out, str(exc))
        return 1
    _say(out, "\nRead these 4 safety words aloud to the other person:\n")
    _say(out, "    " + "  ".join(w.upper() for w in safety.split()))
    _say(out, "\nThey type them. Only confirm when they say the words match.")
    answer = ask("Did they say the words match? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        session.mismatch()
        _say(out, "NOT paired. If the words differed, someone may be in the middle; everything was discarded.")
        return 1
    try:
        paired = session.confirm(host, store, own_relay(host))
    except PairingError as exc:
        _say(out, str(exc))
        return 1
    _say(out, f"Paired with {paired.peer.name} ({paired.display_id}).")
    return 0


def _peers(args, host, store: Store, out, ask) -> int:
    peers = store.peers(include_revoked=True)
    if not peers:
        _say(out, "No peers yet. Pair with: hermes nachalo pair")
        return 0
    for peer in peers:
        seen = datetime.datetime.fromtimestamp(peer.last_seen).isoformat(" ", "minutes") if peer.last_seen else "never"
        _say(out, f"{format_peer_id(peer.peer_id)}  {peer.name:<24} {peer.status:<16} last seen {seen}")
    return 0


def _inbox(args, host, store: Store, out, ask) -> int:
    peer_id = None
    if args.peer:
        peer = store.find_peer(args.peer)
        if peer is None:
            _say(out, "No peer by that name or id.")
            return 1
        peer_id = peer.peer_id
    rows = store.inbox(peer_id=peer_id, states=("held", "read"), limit=50)
    names = {p.peer_id: p.name for p in store.peers(include_revoked=True)}
    if not rows:
        _say(out, "Inbox empty.")
    for row in rows:
        when = datetime.datetime.fromtimestamp(row["received_at"]).isoformat(" ", "minutes")
        _say(out, f"--- {row['id']} · {names.get(row['peer_id'], '?')} · {row['type']} · {when} (untrusted)")
        _say(out, row["text"])
    return 0


def _drafts(args, host, store: Store, out, ask) -> int:
    names = {p.peer_id: p.name for p in store.peers(include_revoked=True)}
    rows = store.drafts()
    if not rows:
        _say(out, "No pending drafts.")
    for row in rows:
        _say(out, f"--- {row['id']} → {names.get(row['peer_id'], '?')} ({row['type']})")
        _say(out, row["text"])
    return 0


def _send(args, host: Host, store: Store, out, ask) -> int:
    draft, peer, problem = channel_mod.check_draft(store, host, args.draft_id, force=args.force)
    if problem:
        _say(out, f"Nothing sent. {problem}")
        return 1
    if args.force:
        _say(out, "WARNING: the leak filter was overridden for this message.")
    _say(out, f"To {peer.name} ({format_peer_id(peer.peer_id)}):\n----\n{draft['text']}\n----")
    if ask("Send exactly this? [y/N]: ").strip().lower() not in ("y", "yes"):
        _say(out, "Nothing sent.")
        return 1
    try:
        result = channel_mod.send_draft(host, store, transport_for(host), draft["id"], force=args.force)
    except channel_mod.SendError as exc:
        _say(out, f"Nothing sent. {exc}")
        return 1
    if result.published < result.frames:
        _say(out, "Sealed; the relay is not answering, the listener will retry.")
    else:
        _say(out, f"Sent to {peer.name}.")
    return 0


def _revoke(args, host: Host, store: Store, out, ask) -> int:
    peer = store.find_peer(args.peer)
    if peer is None:
        _say(out, "No peer by that name or id.")
        return 1
    if ask(f"Revoke {peer.name} ({format_peer_id(peer.peer_id)})? Its keys are erased. [y/N]: ").strip().lower() != "y":
        _say(out, "Nothing changed.")
        return 1
    delivered = channel_mod.revoke(host, store, transport_for(host), peer)
    _say(
        out,
        "Revoked." + ("" if delivered else " (The close notice did not reach the relay; the channel is dead anyway.)"),
    )
    return 0


def _fingerprint(args, host, store: Store, out, ask) -> int:
    peer = store.find_peer(args.peer)
    if peer is None:
        _say(out, "No peer by that name or id.")
        return 1
    _say(
        out, f"{peer.name} ({format_peer_id(peer.peer_id)}): " + "  ".join(w.upper() for w in peer.safety_words.split())
    )
    _say(out, "Read them to each other by voice: they must be the same on both sides.")
    return 0


def _purge(args, host, store: Store, out, ask) -> int:
    if ask("Erase ALL Nachalo peers, messages and drafts in this profile? Type 'purge': ").strip() != "purge":
        _say(out, "Nothing changed.")
        return 1
    if ask("Really? Peers will have to pair again. [y/N]: ").strip().lower() != "y":
        _say(out, "Nothing changed.")
        return 1
    store.purge()
    _say(out, "Purged. NACHALO_STORE_KEY stays in the profile's .env; remove it there if you want.")
    return 0


def _relay_test(args, host: Host, store: Store, out, ask) -> int:
    import secrets as _secrets

    from .encoding import b64u

    transport = transport_for(host)
    topic = "nt" + b64u(_secrets.token_bytes(24))
    stop = threading.Event()
    start = time.monotonic()
    try:
        transport.publish(topic, b"ping")
        found = transport.poll(topic, "1m")
    except Exception as exc:
        _say(out, f"Relay {own_relay(host)}: FAILED ({exc})")
        return 1
    finally:
        stop.set()
    ms = int((time.monotonic() - start) * 1000)
    ok = any(m.data == b"ping" for m in found)
    _say(out, f"Relay {own_relay(host)}: {'OK' if ok else 'published but not read back'} in {ms} ms")
    return 0 if ok else 1


def _listen(args, host: Host, store: Store, out, ask) -> int:
    listener = Listener(host)
    _say(out, "Listening for peer messages (Ctrl-C to stop)…")
    listener.start()
    try:
        while True:
            time.sleep(1)
    finally:
        listener.close()


COMMANDS = {
    "pair": _pair,
    "join": _join,
    "peers": _peers,
    "inbox": _inbox,
    "drafts": _drafts,
    "send": _send,
    "revoke": _revoke,
    "fingerprint": _fingerprint,
    "purge": _purge,
    "relay-test": _relay_test,
    "listen": _listen,
}
