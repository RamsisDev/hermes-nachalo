"""Is a person present on a surface the agent cannot drive? Fail closed (I9, I13)."""

from __future__ import annotations

import os
import sys

# The agent's terminal and execute_code children carry these (tools/environments/local.py
# bridges the gateway session context into every child env).
AGENT_SESSION_VARS = (
    "HERMES_SESSION_KEY",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_PROFILE",
    "HERMES_SESSION_USER_ID",
)


class NotHuman(Exception):
    pass


AGENT_REFUSAL = "A person has to do this: use your own terminal or Hermes Desktop, not the agent."


def launched_by_agent(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return any(env.get(name) for name in AGENT_SESSION_VARS)


def require_human_terminal(in_cron: bool = False) -> None:
    """Sensitive CLI steps (pair, join, send, revoke, purge) need an interactive terminal that the
    agent did not launch. A TTY alone proves nothing (the agent can drive a PTY), which is why the
    session variables are checked too."""
    if in_cron:
        raise NotHuman("Refusing inside a cron run: nobody is there to confirm.")
    if launched_by_agent():
        raise NotHuman(AGENT_REFUSAL)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise NotHuman("This needs an interactive terminal; nothing was done.")
