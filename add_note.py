#!/usr/bin/env python3
"""Add a Markdown note to the Obsidian vault via Local REST API.

Free, local-only. No paid services.

Usage:
  python3 add_note.py --folder Second-Brain/Lilith --title "test" --body "hello"
  python3 add_note.py --folder 00-Inbox --title "clip" --stdin < notes.md
  echo "hi" | python3 add_note.py -f X_Bookmarks -t "bookmark"

Env (optional):
  OBSIDIAN_API_KEY   API key (else read from key file)
  OBSIDIAN_API_URL   default http://127.0.0.1:27123
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_URL = os.environ.get("OBSIDIAN_API_URL", "http://127.0.0.1:27123")
KEY_CANDIDATES = [
    Path(os.environ["OBSIDIAN_API_KEY_FILE"])
    if os.environ.get("OBSIDIAN_API_KEY_FILE")
    else None,
    Path.home() / ".config/viora/obsidian-local-rest-api.key",
    Path(__file__).resolve().parents[1] / ".secrets/local-rest-api.key",
]


def load_api_key() -> str:
    env = os.environ.get("OBSIDIAN_API_KEY", "").strip()
    if env:
        return env
    for p in KEY_CANDIDATES:
        if p and p.is_file():
            return p.read_text(encoding="utf-8").strip()
    sys.exit(
        "API key not found. Set OBSIDIAN_API_KEY or create "
        "~/.config/viora/obsidian-local-rest-api.key"
    )


def sanitize_filename(title: str) -> str:
    bad = '<>:"/\\|?*'
    name = "".join("_" if c in bad else c for c in title).strip() or "untitled"
    return name[:120]


def put_note(base_url: str, api_key: str, rel_path: str, body: str) -> None:
    # Local REST API: PUT /vault/{path}  body = file contents
    url = f"{base_url.rstrip('/')}/vault/{quote(rel_path, safe='/')}"
    data = body.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "text/markdown; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"OK {resp.status} -> {rel_path}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(
            f"Cannot reach Local REST API at {base_url}: {e.reason}\n"
            "Is Obsidian open? Did you reload after installing the plugin?"
        )


def get_status(base_url: str, api_key: str) -> str:
    url = f"{base_url.rstrip('/')}/"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(description="Add a note via Obsidian Local REST API")
    ap.add_argument("-f", "--folder", default="00-Inbox", help="Vault-relative folder")
    ap.add_argument("-t", "--title", default="", help="Note title (filename + H1)")
    ap.add_argument("-b", "--body", default="", help="Note body (Markdown)")
    ap.add_argument("--stdin", action="store_true", help="Read body from stdin")
    ap.add_argument(
        "--filename",
        default="",
        help="Exact filename (optional; default from title + date)",
    )
    ap.add_argument("--url", default=DEFAULT_URL, help="API base URL")
    ap.add_argument("--check", action="store_true", help="Only GET / and exit")
    args = ap.parse_args()

    api_key = load_api_key()

    if args.check:
        print(get_status(args.url, api_key)[:500])
        print("API reachable.")
        return

    body = args.body
    if args.stdin or (not body and not sys.stdin.isatty()):
        body = sys.stdin.read()
    if not body and not args.title:
        sys.exit("Provide --title/--body or --stdin")

    title = args.title or "untitled"
    stamp = datetime.now().strftime("%Y-%m-%d")
    if args.filename:
        fname = args.filename if args.filename.endswith(".md") else f"{args.filename}.md"
    else:
        fname = f"{stamp}-{sanitize_filename(title)}.md"

    folder = args.folder.strip("/").replace("\\", "/")
    rel = f"{folder}/{fname}" if folder else fname

    content = body if body.lstrip().startswith("#") else f"# {title}\n\n{body}".rstrip() + "\n"
    put_note(args.url, api_key, rel, content)


if __name__ == "__main__":
    main()
