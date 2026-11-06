"""Wiring shared by every surface: which relay to use and this side's handshake payload."""

from __future__ import annotations

from collections.abc import Callable

from . import payload as payload_mod
from .host import RELAY_TOKEN_ENV, Host
from .transport import Transport
from .transport.ntfy import NtfyTransport, check_relay_url

_factory: Callable[[Host, str | None], Transport] | None = None


def set_transport_factory(factory: Callable[[Host, str | None], Transport] | None) -> None:
    """Tests and the offline demo swap ntfy for an in-memory relay."""
    global _factory
    _factory = factory


def own_relay(host: Host) -> str:
    return check_relay_url(str(host.settings().get("relay_url") or "https://ntfy.sh"))


def transport_for(host: Host, relay_url: str | None = None) -> Transport:
    if _factory is not None:
        return _factory(host, relay_url)
    url = relay_url or own_relay(host)
    token = host.get_secret(RELAY_TOKEN_ENV) if url == own_relay(host) else None
    return NtfyTransport(url, token=token or None)


def own_payload(host: Host, name: str | None = None) -> bytes:
    display = (name or str(host.settings().get("display_name") or "")).strip() or "Hermes"
    return payload_mod.encode(payload_mod.Payload(name=display, relay=own_relay(host)))
