"""Policies: the anti-leak filter (DLP), the agent-terminal block and per-peer limits."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

# Each pattern names one kind of credential. Tests build matching strings at run time so no
# real-looking key is ever written in this repository (the catalog scanner flags them).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OpenAI/Anthropic-style API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    (".env assignment", re.compile(r"\b[A-Z][A-Z0-9_]{2,}(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD)\s*=\s*\S{6,}")),
    ("Nachalo internal value", re.compile(r"NACHALO_STORE_KEY|NACHALO_RELAY_TOKEN|\bn[hr23][A-Za-z0-9_-]{30,43}\b")),
]

_TOKENISH = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


@dataclass(frozen=True)
class Finding:
    kind: str
    excerpt: str


def scan(text: str) -> list[Finding]:
    """What in ``text`` looks like a secret. Excerpts are masked: they never repeat the value."""
    findings: list[Finding] = []
    for kind, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(kind, _mask(match.group(0))))
    for match in _TOKENISH.finditer(text):
        token = match.group(0)
        if _entropy(token) >= 4.2 and not any(f.excerpt == _mask(token) for f in findings):
            findings.append(Finding("high-entropy string", _mask(token)))
    return findings


def _mask(value: str) -> str:
    return value[:4] + "…" + f"({len(value)} chars)"


def describe(findings: list[Finding]) -> str:
    kinds = sorted({f.kind for f in findings})
    return "This text looks like it contains a secret (" + ", ".join(kinds) + "). It was not sent."


# --- the agent's terminal is not a human surface (I13) ------------------------------------

_TERMINAL_TOOLS = {"terminal", "execute_code", "process", "shell", "bash"}
_FORBIDDEN = re.compile(
    r"\bhermes\b[^\n;|&]*?\bnachalo\b|nachalo_hermes|plugin-data[/\\]+nachalo|NACHALO_STORE_KEY"
    r"|NACHALO_RELAY_TOKEN|hermes_plugins\.nachalo|/api/plugins/nachalo",
    re.IGNORECASE,
)
BLOCK_MESSAGE = "Nachalo: a person has to do this, not the agent."


def agent_call_is_forbidden(tool_name: str, args: Any) -> bool:
    """True when an agent tool call tries to drive the nachalo CLI, its data or its routes.

    A text match, so an obfuscated command can evade it: this is a second layer. What protects
    sending is that no stored state can trigger a send (I8).
    """
    if tool_name not in _TERMINAL_TOOLS:
        return False
    try:
        text = json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
    except TypeError, ValueError:
        text = str(args)
    return bool(_FORBIDDEN.search(text))


# --- limits --------------------------------------------------------------------------------

MAX_INBOX_HELD = 500
MAX_DRAFT_CHARS = 15_000
