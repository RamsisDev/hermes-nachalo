"""REST routes for the Desktop half, mounted by Hermes under ``/api/plugins/nachalo/``.

Runs inside the gateway; Hermes authenticates every route with the UI session token and binds
the request's profile. The renderer never receives keys or topics (I6): it gets the 6 words once,
in the response to the window that asked, the 4 safety words, and state. Events broadcast to every
window carry state only.
"""

from __future__ import annotations

import contextvars
import importlib.util
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parent.parent


def _load_core():
    """Import this plugin's ``nachalo_hermes`` package by path (this file is loaded on its own)."""
    name = "hermes_nachalo_dashboard_core"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "nachalo_hermes" / "__init__.py", submodule_search_locations=[str(_ROOT / "nachalo_hermes")]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load_core()
from hermes_nachalo_dashboard_core import channel as channel_mod  # noqa: E402
from hermes_nachalo_dashboard_core import policy  # noqa: E402
from hermes_nachalo_dashboard_core.encoding import format_peer_id  # noqa: E402
from hermes_nachalo_dashboard_core.host import HermesHost, get_host, set_host  # noqa: E402
from hermes_nachalo_dashboard_core.pairing import InitiatorPairing, PairingError, ResponderPairing  # noqa: E402
from hermes_nachalo_dashboard_core.runtime import own_payload, own_relay, transport_for  # noqa: E402
from hermes_nachalo_dashboard_core.store import Store  # noqa: E402

set_host(HermesHost())
router = APIRouter()

SESSION_TTL = 900
_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _store() -> Store:
    return Store(get_host())


def _event(state: str, session: str) -> None:
    get_host().broadcast("pairing.state", {"state": state, "session": session})


def _gc() -> None:
    now = time.monotonic()
    with _lock:
        for key in [k for k, v in _sessions.items() if now - v["created"] > SESSION_TTL]:
            _sessions[key]["obj"].abort()
            del _sessions[key]


def _session(session: str) -> dict[str, Any]:
    _gc()
    with _lock:
        entry = _sessions.get(session)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such pairing session.")
    return entry


def _background(entry: dict[str, Any], fn) -> None:
    """Run a blocking pairing step off the request, in the request's profile context."""
    ctx = contextvars.copy_context()

    def run() -> None:
        try:
            result = fn()
            if result is not None:
                entry["safety_words"] = result
        except PairingError as exc:
            entry.update(state="failed", error=str(exc), code=exc.code)
            _event("failed", entry["id"])

    threading.Thread(target=lambda: ctx.run(run), name="nachalo-pairing", daemon=True).start()


def _new_entry(role: str, obj) -> dict[str, Any]:
    session = secrets.token_urlsafe(12)
    entry = {
        "id": session,
        "role": role,
        "obj": obj,
        "state": "starting",
        "created": time.monotonic(),
        "safety_words": None,
        "error": None,
        "code": None,
        "peer": None,
    }

    def on_state(state: str) -> None:
        entry["state"] = state
        _event(state, session)

    obj.on_state = on_state
    with _lock:
        _sessions[session] = entry
    return entry


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    out = {
        "session": entry["id"],
        "role": entry["role"],
        "state": entry["state"],
        "seconds_left": entry["obj"].seconds_left(),
        "error": entry["error"],
        "code": entry["code"],
        "peer": entry["peer"],
    }
    if entry["role"] == "responder" and entry["state"] == "verifying":
        out["safety_words"] = entry["safety_words"]
    return out


class PairStart(BaseModel):
    name: str | None = None


class JoinStart(BaseModel):
    words: str
    name: str | None = None


class Verify(BaseModel):
    words: str


class Confirm(BaseModel):
    match: bool


@router.get("/status")
def status() -> dict:
    store = _store()
    try:
        return {"peers": len(store.peers()), "held": sum(store.inbox_counts().values()), "drafts": len(store.drafts())}
    finally:
        store.close()


@router.post("/pair/start")
def pair_start(body: PairStart) -> dict:
    host = get_host()
    obj = InitiatorPairing(transport_for(host), own_payload(host, body.name))
    entry = _new_entry("initiator", obj)
    try:
        words = obj.start()
    except PairingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    _background(entry, obj.wait_for_peer)
    # The words go to this window only, once; they are kept nowhere on the server side.
    return {**_public(entry), "words": words.split()}


@router.post("/join/start")
def join_start(body: JoinStart) -> dict:
    host = get_host()
    try:
        obj = ResponderPairing(transport_for(host), own_payload(host, body.name), body.words, answer_jitter=(0.0, 2.0))
    except PairingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    entry = _new_entry("responder", obj)
    _background(entry, obj.wait_for_initiator)
    return _public(entry)


@router.get("/pair/{session}")
def pair_state(session: str) -> dict:
    return _public(_session(session))


@router.post("/pair/{session}/verify")
def pair_verify(session: str, body: Verify) -> dict:
    entry = _session(session)
    if entry["role"] != "initiator" or entry["state"] != "verifying":
        raise HTTPException(status_code=409, detail="Nothing to verify.")
    obj = entry["obj"]
    try:
        if not obj.verify(body.words):
            return {**_public(entry), "match": False}
        store = _store()
        try:
            paired = obj.finish(get_host(), store, own_relay(get_host()))
            channel_mod.send_hello(get_host(), store, transport_for(get_host()), paired.peer)
        finally:
            store.close()
    except PairingError as exc:
        entry.update(state="failed", error=str(exc), code=exc.code)
        return {**_public(entry), "match": False}
    entry.update(state="paired", peer={"name": paired.peer.name, "peer_id": paired.display_id})
    return {**_public(entry), "match": True}


@router.post("/pair/{session}/confirm")
def pair_confirm(session: str, body: Confirm) -> dict:
    entry = _session(session)
    if entry["role"] != "responder" or entry["state"] != "verifying":
        raise HTTPException(status_code=409, detail="Nothing to confirm.")
    obj = entry["obj"]
    if not body.match:
        obj.mismatch()
        entry.update(state="mismatch")
        return _public(entry)
    store = _store()
    try:
        paired = obj.confirm(get_host(), store, own_relay(get_host()))
    except PairingError as exc:
        entry.update(state="failed", error=str(exc), code=exc.code)
        return _public(entry)
    finally:
        store.close()
    entry.update(state="paired", peer={"name": paired.peer.name, "peer_id": paired.display_id})
    return _public(entry)


@router.post("/pair/{session}/abort")
def pair_abort(session: str) -> dict:
    entry = _session(session)
    entry["obj"].mismatch()
    entry.update(state="mismatch")
    with _lock:
        _sessions.pop(session, None)
    return {"session": session, "state": "mismatch"}


@router.get("/peers")
def peers() -> dict:
    store = _store()
    try:
        counts = store.inbox_counts()
        items = []
        for peer in store.peers(include_revoked=True):
            item = peer.public()
            item["display_id"] = format_peer_id(peer.peer_id)
            item["held"] = counts.get(peer.peer_id, 0)
            items.append(item)
        return {"peers": items}
    finally:
        store.close()


@router.get("/peers/{peer_id}/fingerprint")
def fingerprint(peer_id: str) -> dict:
    store = _store()
    try:
        peer = store.find_peer(peer_id)
        if peer is None:
            raise HTTPException(status_code=404, detail="No such peer.")
        return {"peer_id": format_peer_id(peer.peer_id), "safety_words": peer.safety_words.split()}
    finally:
        store.close()


@router.post("/peers/{peer_id}/revoke")
def revoke(peer_id: str) -> dict:
    store = _store()
    try:
        peer = store.find_peer(peer_id)
        if peer is None:
            raise HTTPException(status_code=404, detail="No such peer.")
        delivered = channel_mod.revoke(get_host(), store, transport_for(get_host()), peer)
        return {"revoked": True, "close_delivered": delivered}
    finally:
        store.close()


@router.get("/inbox")
def inbox() -> dict:
    store = _store()
    try:
        names = {p.peer_id: p.name for p in store.peers(include_revoked=True)}
        rows = store.inbox(states=("held", "read"), limit=100)
        return {
            "messages": [
                {
                    "id": r["id"],
                    "peer_id": r["peer_id"],
                    "peer": names.get(r["peer_id"], "?"),
                    "type": r["type"],
                    "reply_to": r["reply_to"],
                    "received_at": r["received_at"],
                    "state": r["state"],
                    "text": r["text"],
                }
                for r in rows
            ]
        }
    finally:
        store.close()


@router.post("/inbox/{peer_id}/{message_id}/discard")
def inbox_discard(peer_id: str, message_id: str) -> dict:
    store = _store()
    try:
        return {"discarded": store.discard_inbox(peer_id, message_id)}
    finally:
        store.close()


@router.get("/drafts")
def drafts() -> dict:
    store = _store()
    try:
        names = {p.peer_id: p.name for p in store.peers(include_revoked=True)}
        out = []
        for row in store.drafts():
            findings = policy.scan(row["text"])
            out.append(
                {
                    "id": row["id"],
                    "peer_id": row["peer_id"],
                    "peer": names.get(row["peer_id"], "?"),
                    "type": row["type"],
                    "created_at": row["created_at"],
                    "text": row["text"],
                    "leak_warning": policy.describe(findings) if findings else None,
                }
            )
        return {"drafts": out}
    finally:
        store.close()


@router.post("/drafts/{draft_id}/send")
def draft_send(draft_id: str) -> dict:
    """The Desktop Send button: the person sees the exact text in the Drafts pane and clicks."""
    store = _store()
    try:
        result = channel_mod.send_draft(get_host(), store, transport_for(get_host()), draft_id)
    except channel_mod.SendError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    finally:
        store.close()
    return {"sent": True, "peer": result.peer.name, "frames": result.frames, "published": result.published}


@router.post("/drafts/{draft_id}/discard")
def draft_discard(draft_id: str) -> dict:
    store = _store()
    try:
        return {"discarded": store.discard_draft(draft_id)}
    finally:
        store.close()
