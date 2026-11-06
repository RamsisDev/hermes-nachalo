"""Nachalo for Hermes: pair agents end-to-end from six spoken words; the agent drafts, a person sends."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .nachalo_hermes import cli, commands, tools
from .nachalo_hermes.host import HermesHost, set_host
from .nachalo_hermes.listener import Listener

logger = logging.getLogger("nachalo")

_SKILLS = Path(__file__).parent / "skills"

# Relay topics and env-style key lines; every pattern starts with two literal characters.
REDACTION_PATTERNS = [
    r"nh[A-Za-z0-9_-]{32}",
    r"nr[A-Za-z0-9_-]{43}",
    r"n2[A-Za-z0-9_-]{43}",
    r"n3[A-Za-z0-9_-]{43}",
    r"NACHALO_STORE_KEY\s*=\s*\S+",
    r"NACHALO_RELAY_TOKEN\s*=\s*\S+",
]


def _gateway_process() -> bool:
    """The listener belongs to the long-lived gateway (or, with ``plugins.isolation: host``, to
    the plugin host it supervises), not to a one-shot ``hermes nachalo`` process."""
    if os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1":
        return True
    from gateway.status import get_running_pid

    return get_running_pid(cleanup_stale=False) == os.getpid()


def register(ctx) -> None:
    host = HermesHost(ctx)
    set_host(host)

    ctx.register_tool(
        name="nachalo_peers", toolset="nachalo", schema=tools.PEERS_SCHEMA, handler=tools.nachalo_peers, emoji="🤝"
    )
    ctx.register_tool(
        name="nachalo_inbox", toolset="nachalo", schema=tools.INBOX_SCHEMA, handler=tools.nachalo_inbox, emoji="📥"
    )
    ctx.register_tool(
        name="nachalo_draft", toolset="nachalo", schema=tools.DRAFT_SCHEMA, handler=tools.nachalo_draft, emoji="📝"
    )
    ctx.register_hook("pre_llm_call", tools.pre_llm_call)
    ctx.register_hook("pre_tool_call", tools.pre_tool_call)

    ctx.register_command(
        "nachalo",
        commands.make_handler(ctx.ask_choice),
        description="Nachalo peers: status, drafts, send a draft",
        args_hint="[send <draft_id>]",
    )
    ctx.register_cli_command(
        name="nachalo", help="Pair with another agent by six spoken words", setup_fn=cli.setup, handler_fn=cli.main
    )

    skill = _SKILLS / "nachalo" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("nachalo", skill)
    ctx.register_redaction_patterns(REDACTION_PATTERNS)

    listener = Listener(host, home=host.home(), should_run=_gateway_process)
    listener.start()
    ctx.on_unload(listener.close)
