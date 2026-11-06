"""The handshake payload: public, disposable data padded to exactly 256 bytes.

The responder's payload can become readable to an active quantum attacker, and whoever knows
the words sees both payloads before the safety words are compared, so nothing secret goes here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PAYLOAD_LEN = 256
APP = "hermes-nachalo"
FEATURES = ("note", "ask")
MAX_NAME = 64


@dataclass(frozen=True)
class Payload:
    name: str
    relay: str
    features: tuple = field(default=FEATURES)


def encode(payload: Payload) -> bytes:
    name = payload.name.strip()[:MAX_NAME] or "Hermes"
    relay = payload.relay
    body = {"v": 1, "app": APP, "name": name, "features": list(payload.features), "relay": relay, "pad": ""}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    while len(raw) > PAYLOAD_LEN and len(name) > 1:
        name = name[:-1]
        body["name"] = name
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > PAYLOAD_LEN:
        raise ValueError("relay URL too long for the handshake payload")
    body["pad"] = "." * (PAYLOAD_LEN - len(raw))
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) == PAYLOAD_LEN
    return raw


def decode(raw: bytes) -> Payload:
    """Parse the peer's payload; anything unexpected yields safe defaults, never an exception."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return Payload(name="Unknown peer", relay="")
    if not isinstance(body, dict) or body.get("app") != APP:
        return Payload(name="Unknown peer", relay="")
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    relay = body.get("relay") if isinstance(body.get("relay"), str) else ""
    features = body.get("features") if isinstance(body.get("features"), list) else []
    clean_name = "".join(c for c in name if c.isprintable())[:MAX_NAME] or "Unnamed peer"
    return Payload(
        name=clean_name,
        relay=relay[:200],
        features=tuple(f for f in features if f in FEATURES),
    )
