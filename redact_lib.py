#!/usr/bin/env python3
"""Defense-in-depth secret redaction for --full-mirror (stdlib only).

Never logs matched values: callers only get counts per rule type.
Bump REDACTION_RULES_VERSION whenever a pattern changes.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

REDACTION_RULES_VERSION = "2026-10-31.2"

# Optional backslashes before quotes so JSON-escaped content inside jsonl strings matches too.
_Q = r'(?:\\*["\'])'

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("pem_private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")),
    ("url_credentials", re.compile(r"(?<=://)[^\s/@:\"'\\]+:[^\s/@\"'\\]+(?=@)")),
    ("bearer", re.compile(r"(?i)(?<=\bbearer\s)\s*[A-Za-z0-9._~+/=-]{8,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{10,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9_-]{16,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,40}\b")),
    ("discord_bot_token", re.compile(r"\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}\b")),
]

# Exact key names (optionally with a FOO_ / foo- prefix); word-bounded so
# `author`, `num_tokens`, `tokenizer`, `session_id` are NOT matched.
# Boundary also accepts a JSON escape (\\n, \\t, \\r) right before the key.
_KEYNAME = (r"(?:(?<![\w-])|(?<=\\[nrt]))(?:[A-Za-z0-9]+[_-])*"
            r"(?:password|passwd|secret|client_secret|api[_-]?key|apikey|access[_-]?key|secret[_-]?key"
            r"|private[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|credentials?|cookie)"
            r"(?![\w-])")
# "key": "value"  /  \"key\": \"value\"  /  key=value  /  key: value
_KV = re.compile(
    rf"(?i)(?P<k>{_Q}?{_KEYNAME}{_Q}?\s*[:=]\s*)"
    rf"(?:(?P<q>{_Q})(?P<qv>[^\"'\n\\]*)(?P=q)|(?P<v>[^\s\"',;&}}\]\\]+))"
)


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact_text(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    for kind, rx in _RULES:
        text, n = rx.subn(_marker(kind), text)
        if n:
            counts[kind] += n

    def kv(m: re.Match) -> str:
        val = m.group("qv") if m.group("q") else m.group("v")
        # Only secret-looking values: >= 8 chars, not already redacted, not plain numbers/booleans.
        if (not val or len(val) < 8 or val.startswith("[REDACTED:") or val.isdigit()
                or val.lower() in ("null", "none", "true", "false")):
            return m.group(0)
        counts["key_value"] += 1
        if m.group("q"):
            return f"{m.group('k')}{m.group('q')}{_marker('key_value')}{m.group('q')}"
        return f"{m.group('k')}{_marker('key_value')}"

    return _KV.sub(kv, text), counts


def is_text(data: bytes) -> bool:
    """UTF-8 text without NUL bytes. Anything else is copied byte-for-byte."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def redact_bytes(data: bytes) -> tuple[bytes, Counter]:
    out, counts = redact_text(data.decode("utf-8"))
    return out.encode("utf-8"), counts
