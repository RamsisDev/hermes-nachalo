"""The only module that touches Hermes. Every import here is on the public plugin API list
(https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api).

Tests and the offline demo install a :class:`LocalHost` instead, so the core never needs Hermes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PLUGIN_ID = "nachalo"
STORE_KEY_ENV = "NACHALO_STORE_KEY"
RELAY_TOKEN_ENV = "NACHALO_RELAY_TOKEN"

logger = logging.getLogger("nachalo")

DEFAULT_SETTINGS: dict[str, Any] = {
    "relay_url": "https://ntfy.sh",
    "display_name": "",
    "max_message_chars": 4000,
    "inbox_retention_days": 30,
    "notify_in_chat": True,
    "rate_limit_per_hour": 30,
}


class Host:
    """What the core needs from its environment."""

    def home(self) -> Path:
        raise NotImplementedError

    def db(self) -> sqlite3.Connection:
        raise NotImplementedError

    def data_dir(self) -> Path:
        raise NotImplementedError

    def get_secret(self, name: str) -> str | None:
        raise NotImplementedError

    def save_secret(self, name: str, value: str) -> None:
        raise NotImplementedError

    def settings(self) -> dict[str, Any]:
        raise NotImplementedError

    def broadcast(self, event: str, payload: dict) -> None:
        """Push a state-only event to Desktop. Never secrets: it reaches every window."""

    def in_cron(self) -> bool:
        return False

    @contextlib.contextmanager
    def profile_scope(self, home: Path) -> Iterator[None]:
        yield

    def store_key(self, create: bool = False) -> bytes | None:
        raw = self.get_secret(STORE_KEY_ENV)
        if raw:
            try:
                key = bytes.fromhex(raw.strip())
            except ValueError:
                key = b""
            if len(key) == 32:
                return key
            raise RuntimeError(f"{STORE_KEY_ENV} is not 64 hex characters; fix or remove it")
        if not create:
            return None
        key = secrets.token_bytes(32)
        self.save_secret(STORE_KEY_ENV, key.hex())
        return key


class HermesHost(Host):
    """Hermes's public surfaces. Each call resolves the active profile, so it follows
    ``HERMES_HOME`` and any context-local override the listener installs."""

    def __init__(self, ctx: Any = None):
        self._ctx = ctx

    def home(self) -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home()

    def db(self) -> sqlite3.Connection:
        from plugins.plugin_storage import plugin_db

        conn = plugin_db(PLUGIN_ID)
        conn.row_factory = sqlite3.Row
        return conn

    def data_dir(self) -> Path:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID)

    def get_secret(self, name: str) -> str | None:
        from agent.secret_scope import build_profile_secret_scope, get_secret

        value = get_secret(name)
        if value or name not in (STORE_KEY_ENV, RELAY_TOKEN_ENV):
            return value
        # The process env and an installed scope are snapshots: a key that another process
        # saved later (a CLI pairing while the gateway runs) only shows up in the profile itself.
        return build_profile_secret_scope(self.home()).get(name)

    def save_secret(self, name: str, value: str) -> None:
        from hermes_cli.config import save_env_value

        save_env_value(name, value)

    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        if self._ctx is not None:
            for key in DEFAULT_SETTINGS:
                value = self._ctx.get_config(key, default=None)
                if value is not None:
                    out[key] = value
            return out
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        for key, value in (entry.get("settings") or {}).items():
            if key in DEFAULT_SETTINGS and value is not None:
                out[key] = value
        return out

    def broadcast(self, event: str, payload: dict) -> None:
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event

            broadcast_plugin_event(PLUGIN_ID, event, payload)
        except Exception:  # fire-and-forget by contract; a missing Desktop is not an error
            logger.debug("nachalo: broadcast of %s skipped", event)

    def in_cron(self) -> bool:
        if self._ctx is None:
            return False
        try:
            return self._ctx.current_cron_execution() is not None
        except Exception:
            return False

    @contextlib.contextmanager
    def profile_scope(self, home: Path) -> Iterator[None]:
        """Pin ``home`` and its secret scope for the current thread or task. Under the
        multiplexing gateway ``get_secret`` raises without a scope, and ``plugin_db`` follows the
        context's home, so the listener must run inside this."""
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_token = set_hermes_home_override(home)
        secret_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            yield
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)


class LocalHost(Host):
    """A self-contained host rooted at a directory: tests, the offline demo and CI."""

    def __init__(self, root: Path, settings: dict | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secrets: dict[str, str] = {}
        self._settings = dict(DEFAULT_SETTINGS, **(settings or {}))
        self.events: list[tuple[str, dict]] = []
        self.cron = False

    def home(self) -> Path:
        return self.root

    def data_dir(self) -> Path:
        path = self.root / "plugin-data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.data_dir() / "data.db", check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name) or os.environ.get(f"NACHALO_LOCALHOST_{name}")

    def save_secret(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def broadcast(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload)))

    def in_cron(self) -> bool:
        return self.cron


_current: Host | None = None


def set_host(host: Host | None) -> None:
    global _current
    _current = host


def get_host() -> Host:
    if _current is None:
        raise RuntimeError("nachalo: no host installed (register() not called)")
    return _current
