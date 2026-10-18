#!/usr/bin/env python3
"""Save an X (Twitter) post into X_Bookmarks/. Free, local.

Usage:
  python3 x_bookmark.py URL
  python3 x_bookmark.py URL --text "メモ" --author "@foo"
  python3 x_bookmark.py --stdin   # lines: URL | optional text
  python3 x_bookmark.py --from-firefox --days 7   # recent x.com/status from Firefox history
  python3 x_bookmark.py --process-drop            # X_Bookmarks/_drop/*.txt
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from vault_lib import (
    VAULT,
    existing_urls_in_folder,
    sanitize_filename,
    stamp,
    stamp_full,
    write_note,
)

FOLDER = "X_Bookmarks"
DROP = VAULT / FOLDER / "_drop"
STATUS_RE = re.compile(
    r"https?://(?:(?:mobile|www)\.)?(?:x\.com|twitter\.com)/([^/\s]+)/status/(\d+)",
    re.I,
)


def parse_status(url: str) -> tuple[str, str, str] | None:
    m = STATUS_RE.search(url.strip())
    if not m:
        return None
    user, sid = m.group(1), m.group(2)
    canonical = f"https://x.com/{user}/status/{sid}"
    return user, sid, canonical


def note_body(url: str, user: str, text: str, author: str, extra: str) -> str:
    who = author or f"@{user}"
    body = text.strip() or "(本文未取得 — URLのみ保存)"
    return (
        f"---\n"
        f"source: x\n"
        f"url: {url}\n"
        f"author: {who}\n"
        f"saved: {stamp_full()}\n"
        f"---\n\n"
        f"# {who}\n\n"
        f"{body}\n\n"
        f"- 元投稿: {url}\n"
        + (f"\n## メモ\n\n{extra.strip()}\n" if extra.strip() else "")
    )


def save_one(url: str, text: str = "", author: str = "", extra: str = "", known: set[str] | None = None) -> Path | None:
    parsed = parse_status(url)
    if not parsed:
        # non-status x link still save
        if "x.com" not in url and "twitter.com" not in url:
            print(f"SKIP not an X URL: {url}", file=sys.stderr)
            return None
        user = urlparse(url).path.strip("/").split("/")[0] or "x"
        sid = sanitize_filename(url[-40:])
        canonical = url.strip()
    else:
        user, sid, canonical = parsed

    if known is not None and canonical in known:
        print(f"SKIP dup: {canonical}")
        return None

    title_bit = sanitize_filename(text[:40] if text else sid)
    fname = f"{stamp()}-{user}-{sid}-{title_bit}.md".replace(" ", "-")
    # keep filename sane length
    if len(fname) > 140:
        fname = f"{stamp()}-{user}-{sid}.md"
    rel = f"{FOLDER}/{fname}"
    path = write_note(rel, note_body(canonical, user, text, author, extra))
    if known is not None:
        known.add(canonical)
    print(f"OK -> {rel}")
    return path


def process_stdin() -> int:
    known = existing_urls_in_folder(FOLDER)
    n = 0
    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            url, text = line.split("|", 1)
        else:
            url, text = line, ""
        if save_one(url.strip(), text=text.strip(), known=known):
            n += 1
    return n


def process_drop() -> int:
    DROP.mkdir(parents=True, exist_ok=True)
    known = existing_urls_in_folder(FOLDER)
    n = 0
    for p in sorted(DROP.glob("*.txt")):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                url, text = line.split("|", 1)
            else:
                url, text = line, ""
            if save_one(url.strip(), text=text.strip(), known=known):
                n += 1
        done = DROP / "done"
        done.mkdir(exist_ok=True)
        p.rename(done / p.name)
        print(f"processed drop {p.name}")
    return n


def firefox_places_candidates() -> list[Path]:
    roots = [
        Path.home() / "snap/firefox/common/.mozilla/firefox",
        Path.home() / ".mozilla/firefox",
        Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ]
    out: list[Path] = []
    for r in roots:
        if r.is_dir():
            out.extend(r.glob("*/places.sqlite"))
    return out


def import_firefox(days: int, limit: int) -> int:
    """Import recent *visited* status URLs from Firefox history (not X native bookmarks)."""
    known = existing_urls_in_folder(FOLDER)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for db in firefox_places_candidates():
        tmp = Path(tempfile.mkstemp(suffix=".sqlite")[1])
        try:
            shutil.copy2(db, tmp)
            wal, shm = Path(str(db) + "-wal"), Path(str(db) + "-shm")
            if wal.is_file():
                shutil.copy2(wal, Path(str(tmp) + "-wal"))
            if shm.is_file():
                shutil.copy2(shm, Path(str(tmp) + "-shm"))
            con = sqlite3.connect(str(tmp))
            cur = con.cursor()
            rows = cur.execute(
                """
                SELECT url, coalesce(title,''), last_visit_date
                FROM moz_places
                WHERE (url LIKE '%://x.com/%/status/%' OR url LIKE '%://twitter.com/%/status/%'
                       OR url LIKE '%://mobile.x.com/%/status/%')
                  AND last_visit_date IS NOT NULL
                ORDER BY last_visit_date DESC
                LIMIT ?
                """,
                (limit * 3,),
            ).fetchall()
            con.close()
        except Exception as e:
            print(f"WARN firefox {db}: {e}", file=sys.stderr)
            continue
        finally:
            tmp.unlink(missing_ok=True)
            Path(str(tmp) + "-wal").unlink(missing_ok=True)
            Path(str(tmp) + "-shm").unlink(missing_ok=True)

        for url, title, lvd in rows:
            if lvd:
                try:
                    visited = datetime.fromtimestamp(lvd / 1_000_000, tz=timezone.utc)
                    if visited < cutoff:
                        continue
                except (OverflowError, ValueError, OSError):
                    pass
            # strip "Xユーザーの...さん: 「...」 / X" style titles a bit
            text = title
            m = re.search(r"「(.+?)」", title)
            if m:
                text = m.group(1)
            if save_one(url, text=text, known=known):
                n += 1
                if n >= limit:
                    return n
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Save X posts into X_Bookmarks")
    ap.add_argument("url", nargs="?", help="Post URL")
    ap.add_argument("--text", default="", help="Post text / summary")
    ap.add_argument("--author", default="", help="Author label")
    ap.add_argument("--note", default="", help="Your memo")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--process-drop", action="store_true")
    ap.add_argument("--from-firefox", action="store_true", help="Import recent status from Firefox history")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    if args.process_drop:
        print(f"saved {process_drop()} notes")
        return
    if args.from_firefox:
        print(f"saved {import_firefox(args.days, args.limit)} notes from Firefox history")
        return
    if args.stdin:
        print(f"saved {process_stdin()} notes")
        return
    if not args.url:
        ap.print_help()
        sys.exit(2)
    known = existing_urls_in_folder(FOLDER)
    if not save_one(args.url, text=args.text, author=args.author, extra=args.note, known=known):
        sys.exit(1)


if __name__ == "__main__":
    main()
