#!/usr/bin/env python3
"""Shared helpers: Local REST API + filesystem fallback (stdlib only)."""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

VAULT = Path(os.environ.get("OBSIDIAN_VAULT", Path(__file__).resolve().parents[1]))
DEFAULT_URL = os.environ.get("OBSIDIAN_API_URL", "http://127.0.0.1:27123")

# Lilith (Grok chat) — single tree under Second-Brain (never vault-root).
# Override: OBSIDIAN_LILITH_ROOT=Second-Brain/Lilith
LILITH_FOLDER = os.environ.get("OBSIDIAN_LILITH_ROOT", "Second-Brain/Lilith").strip().strip("/") or "Second-Brain/Lilith"
# Legacy paths (pre-2026-08) — skip-scan / migration only; do not write here
LILITH_LEGACY_FOLDERS = (
    "Chat_History",
    "Lilith",  # old vault-root folder or symlink
    "Second-Brain/Sessions/Lilith",  # old canvas-only folder
)
KEY_CANDIDATES = [
    Path(os.environ["OBSIDIAN_API_KEY_FILE"])
    if os.environ.get("OBSIDIAN_API_KEY_FILE")
    else None,
    Path.home() / ".config/viora/obsidian-local-rest-api.key",
    VAULT / ".secrets/local-rest-api.key",
]


def load_api_key() -> str | None:
    env = os.environ.get("OBSIDIAN_API_KEY", "").strip()
    if env:
        return env
    for p in KEY_CANDIDATES:
        if p and p.is_file():
            return p.read_text(encoding="utf-8").strip()
    return None


def sanitize_filename(title: str, max_len: int = 80) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    name = "".join("_" if c in bad else c for c in title).strip()
    name = re.sub(r"\s+", " ", name).strip(" ._") or "untitled"
    return name[:max_len]


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def stamp_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def api_put(rel_path: str, body: str | bytes, content_type: str = "text/markdown; charset=utf-8") -> bool:
    """PUT via Local REST API. Returns True on success."""
    key = load_api_key()
    if not key:
        return False
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    url = f"{DEFAULT_URL.rstrip('/')}/vault/{quote(rel_path, safe='/')}"
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300 or resp.status == 204
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def write_note(rel_path: str, body: str, prefer_api: bool = True) -> Path:
    """Write markdown into vault. Prefer API so Obsidian indexes immediately; else FS."""
    rel_path = rel_path.lstrip("/").replace("\\", "/")
    if prefer_api and api_put(rel_path, body):
        path = VAULT / rel_path
        # API may have written; ensure local visibility
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return path
    path = VAULT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_bytes(rel_path: str, data: bytes, content_type: str = "application/octet-stream") -> Path:
    rel_path = rel_path.lstrip("/").replace("\\", "/")
    if api_put(rel_path, data, content_type=content_type):
        path = VAULT / rel_path
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return path
    path = VAULT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def existing_urls_in_folder(folder: str) -> set[str]:
    """Collect http(s) URLs already mentioned under folder (for dedupe)."""
    root = VAULT / folder
    found: set[str] = set()
    if not root.is_dir():
        return found
    url_re = re.compile(r"https?://[^\s\)\]\>\"']+")
    for p in root.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in url_re.findall(text):
            found.add(m.rstrip(".,;"))
    return found
