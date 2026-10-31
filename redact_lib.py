#!/usr/bin/env python3
"""Fail-closed secret redaction for --full-mirror (stdlib only).

Never logs matched values: callers only get counts per rule type.
Bump REDACTION_RULES_VERSION whenever a pattern changes.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

REDACTION_RULES_VERSION = "2026-10-31.1"

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
]

_KEYNAME = (r"[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
            r"|auth|credential|cookie|session[_-]?id)[\w.-]*")
# "key": "value"  /  \"key\": \"value\"  /  key=value  /  key: value
_KV = re.compile(
    rf"(?i)(?P<k>{_Q}?{_KEYNAME}{_Q}?\s*[:=]\s*)"
    rf"(?:(?P<q>{_Q})(?P<qv>.*?)(?P=q)|(?P<v>[^\s\"',;&}}\]\\]+))"
)

SENSITIVE_NAME = re.compile(
    r"(?i)^(?:\.env.*|.*\.pem|.*\.key|id_rsa.*|id_dsa.*|id_ecdsa.*|id_ed25519.*|credentials.*|.*\.p12|.*\.pfx)$"
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
        if not val or val.startswith("[REDACTED:") or val in ("null", "true", "false"):
            return m.group(0)
        counts["key_value"] += 1
        if m.group("q"):
            return f"{m.group('k')}{m.group('q')}{_marker('key_value')}{m.group('q')}"
        return f"{m.group('k')}{_marker('key_value')}"

    text = _KV.sub(kv, text)
    return text, counts


def exclusion_reason(rel: str, data: bytes) -> str | None:
    """Reason to never copy this file, or None."""
    if SENSITIVE_NAME.match(PurePosixPath(rel).name):
        return "sensitive_filename"
    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8"
    return None


def redact_bytes(data: bytes) -> tuple[bytes, Counter]:
    out, counts = redact_text(data.decode("utf-8"))
    return out.encode("utf-8"), counts
