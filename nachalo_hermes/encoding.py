"""Encodings and public names shared by both sides: lv_cat, base64url, topics and peer ids."""

from __future__ import annotations

import base64
import hashlib

CONTEXT = b"hermes-nachalo/1"
"""Nachalo invitation context. Equal on both sides; never travels."""

LABEL_MSG2 = b"hermes-nachalo/1 msg2"
LABEL_MSG3 = b"hermes-nachalo/1 msg3"
LABEL_PEER_ID = b"hermes-nachalo/1 peer-id"

TOPIC_PREFIXES = ("nr", "n2", "n3", "nh")
"""Every relay topic the plugin uses starts with one of these (redaction patterns key on them)."""


def lv_cat(*parts: bytes) -> bytes:
    """Each part as its LEB128 length, then its bytes (Nachalo spec, CPace appendix A.1)."""
    out = bytearray()
    for part in parts:
        n = len(part)
        while True:
            byte = n & 0x7F
            n >>= 7
            if n:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                break
        out += part
    return bytes(out)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def rendezvous_topic(rendezvous: bytes) -> str:
    return "nr" + b64u(rendezvous)[:43]


def msg2_topic(msg1: bytes) -> str:
    return "n2" + b64u(hashlib.sha256(lv_cat(LABEL_MSG2, msg1)).digest())[:43]


def msg3_topic(msg2: bytes) -> str:
    return "n3" + b64u(hashlib.sha256(lv_cat(LABEL_MSG3, msg2)).digest())[:43]


def peer_id(transcript: bytes) -> str:
    """Public id of a pairing, equal on both sides: 16 base32 characters of the transcript hash."""
    digest = hashlib.sha256(lv_cat(LABEL_PEER_ID, transcript)).digest()
    return base64.b32encode(digest).decode("ascii")[:16]


def format_peer_id(pid: str) -> str:
    """``K7QX2M9P…`` as ``K7QX-2M9P-…`` for people to read."""
    return "-".join(pid[i : i + 4] for i in range(0, len(pid), 4))
