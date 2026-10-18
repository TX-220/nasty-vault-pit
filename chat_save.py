#!/usr/bin/env python3
"""Save conversation highlights into Second-Brain/Lilith/. Free, local.

Usage:
  python3 chat_save.py -w Lilith -t "Vault Git" -b "決定: REST API で書く"
  python3 chat_save.py -w Vesper --stdin < notes.md
  python3 chat_save.py --process-drop
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vault_lib import LILITH_FOLDER, VAULT, sanitize_filename, stamp, stamp_full, write_note

FOLDER = LILITH_FOLDER
DROP = VAULT / FOLDER / "_drop"


def save(who: str, title: str, body: str, tags: str = "") -> Path:
    who_s = sanitize_filename(who or "unknown")
    title_s = sanitize_filename(title or "memo")
    fname = f"{stamp()}-{who_s}-{title_s}.md"
    tag_line = f"tags: [{tags}]\n" if tags else ""
    content = (
        f"---\n"
        f"type: chat_highlight\n"
        f"who: {who}\n"
        f"saved: {stamp_full()}\n"
        f"{tag_line}"
        f"---\n\n"
        f"# {title}\n\n"
        f"**with:** {who}  \n"
        f"**when:** {stamp_full()}\n\n"
        f"{body.strip()}\n"
    )
    rel = f"{FOLDER}/{fname}"
    path = write_note(rel, content)
    print(f"OK -> {rel}")
    return path


def process_drop() -> int:
    """Drop files named WHO__title.txt or plain .txt (who=unknown)."""
    DROP.mkdir(parents=True, exist_ok=True)
    n = 0
    done = DROP / "done"
    done.mkdir(exist_ok=True)
    for p in sorted(DROP.glob("*.txt")):
        raw = p.read_text(encoding="utf-8", errors="ignore")
        stem = p.stem
        if "__" in stem:
            who, title = stem.split("__", 1)
        else:
            who, title = "unknown", stem
        save(who.replace("-", " "), title.replace("-", " "), raw)
        p.rename(done / p.name)
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Save chat highlights to Second-Brain/Lilith")
    ap.add_argument("-w", "--who", default="Lilith", help="Speaker / partner name")
    ap.add_argument("-t", "--title", default="memo", help="Short title")
    ap.add_argument("-b", "--body", default="", help="Highlight body")
    ap.add_argument("--tags", default="", help="comma tags")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--process-drop", action="store_true")
    args = ap.parse_args()

    if args.process_drop:
        print(f"saved {process_drop()} notes")
        return

    body = args.body
    if args.stdin or (not body and not sys.stdin.isatty()):
        body = sys.stdin.read()
    if not body.strip():
        sys.exit("Need --body or --stdin content")
    save(args.who, args.title, body, tags=args.tags)


if __name__ == "__main__":
    main()
