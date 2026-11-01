#!/usr/bin/env python3
"""Bidirectional X bookmarks sync (bird / birdclaw.sh) ↔ X_Bookmarks/.

Usage:
  python3 x_sync_bookmarks.py              # full: pull X→local + push local deletes→X
  python3 x_sync_bookmarks.py --limit 30   # sample pull
  python3 x_sync_bookmarks.py --from-json path.json
  python3 x_sync_bookmarks.py --dry-run
  python3 x_sync_bookmarks.py --unbookmark-only          # push suppressed → X unbookmark
  python3 x_sync_bookmarks.py --no-push-deletes          # pull only (old behavior)

Directions:
  X → local: fetch bookmarks, write new notes (skip local + suppressed)
  local → X: notes removed locally land in suppressed_ids, then bird unbookmark
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from classify_lib import classify_x_category, safe_dir_name
from vault_lib import VAULT, existing_urls_in_folder, sanitize_filename, stamp_full, write_note

HERE = Path(__file__).resolve().parent
FOLDER = "X_Bookmarks"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "viora" / "x-bookmarks-latest.json"
STATE = VAULT / FOLDER / ".sync_state.json"
ENEMY_BLOCKLIST = HERE / "config" / "x_enemy_blocklist.json"

# Frontmatter id: 123… or filename snowflake (15–20 digits)
_ID_FM_RE = re.compile(r"(?m)^id:\s*[\"']?(\d+)[\"']?\s*$")
_ID_FN_RE = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")

_enemy_cache: dict | None = None


def load_enemy_blocklist() -> dict:
    """既得権サイド / 親分反対派 blocklist (import skip + unbookmark)."""
    global _enemy_cache
    if _enemy_cache is not None:
        return _enemy_cache
    if ENEMY_BLOCKLIST.is_file():
        try:
            _enemy_cache = json.loads(ENEMY_BLOCKLIST.read_text(encoding="utf-8"))
            return _enemy_cache
        except json.JSONDecodeError:
            pass
    _enemy_cache = {}
    return _enemy_cache


def is_enemy_tweet(t: dict) -> str | None:
    """Return reason string if tweet is enemy (incumbent / anti-boss), else None."""
    bl = load_enemy_blocklist()
    if not bl:
        return None
    author = ((t.get("author") or {}).get("username") or "").lstrip("@")
    al = author.lower()
    exact = {x.lower() for x in (bl.get("author_exact") or [])}
    if al in exact:
        return f"enemy_author:@{author}"
    for s in bl.get("author_substr") or []:
        s = s.lower()
        if s and s in al:
            if s == "meta" and not (al.startswith("meta") or al in ("meta", "zuck")):
                continue
            return f"enemy_author_frag:@{author}"
    text = (t.get("text") or t.get("full_text") or "")
    blob = f"@{author}\n{text}"
    brand_pat = bl.get("brand_regex") or ""
    url_pat = bl.get("url_regex") or ""
    if brand_pat and re.search(brand_pat, blob):
        return "enemy_brand"
    if url_pat and re.search(url_pat, blob):
        return "enemy_url"
    return None


def load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"ids": [], "suppressed_ids": [], "unbookmarked_ids": []}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def unbookmark_on_x(
    ids: list[str],
    *,
    dry_run: bool = False,
    batch_size: int = 15,
    delay_sec: float = 1.2,
) -> tuple[int, int, list[str]]:
    """Remove bookmarks on X via bird unbookmark.

    Returns (ok_count, fail_count, successfully_unbookmarked_ids).
    """
    ids = [str(i).strip() for i in ids if str(i).strip()]
    if not ids:
        print("unbookmark: nothing to do", file=sys.stderr)
        return 0, 0, []

    print(f"unbookmark: {len(ids)} id(s) → X (batch={batch_size})", file=sys.stderr)
    if dry_run:
        for i, tid in enumerate(ids[:20], 1):
            print(f"DRY UNBOOKMARK {i}/{len(ids)} {tid}", file=sys.stderr)
        if len(ids) > 20:
            print(f"DRY UNBOOKMARK … +{len(ids) - 20} more", file=sys.stderr)
        print(f"DRY-RUN: would unbookmark {len(ids)} on X (no API calls)", file=sys.stderr)
        return len(ids), 0, []

    from bird_cookies import bird_env

    env = bird_env()
    ok = 0
    fail = 0
    done: list[str] = []
    # bird sometimes says "favorites" when the tweet is simply not bookmarked anymore
    already_gone_re = re.compile(
        r"(not found in actor|not found|was not found|_Missing)",
        re.I,
    )
    removed_re = re.compile(r"Removed bookmark for (\d+)")

    def _classify_output(text: str, tid: str) -> str:
        """Return 'ok' | 'gone' | 'fail' for a single-id result text."""
        if "Removed bookmark" in text or re.search(rf"Removed bookmark for {tid}", text):
            return "ok"
        if already_gone_re.search(text):
            return "gone"
        return "fail"

    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        cmd = ["bird", "unbookmark", *batch]
        print(
            f"unbookmark batch {start // batch_size + 1}: {len(batch)} ids …",
            file=sys.stderr,
        )
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"FAIL timeout batch starting {batch[0]}", file=sys.stderr)
            fail += len(batch)
            time.sleep(delay_sec * 2)
            continue

        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        removed_in_batch = set(removed_re.findall(out))

        if proc.returncode == 0:
            # All requested treated as done (API accepted batch)
            for tid in batch:
                ok += 1
                done.append(tid)
            if out:
                print(out[:500], file=sys.stderr)
            print(f"OK unbookmarked {len(batch)} (total_ok={ok})", file=sys.stderr)
        else:
            # Partial success: keep ids bird already reported as removed
            pending_retry = []
            for tid in batch:
                if tid in removed_in_batch:
                    ok += 1
                    done.append(tid)
                else:
                    pending_retry.append(tid)
            print(
                f"batch rc={proc.returncode}: pre-ok={len(removed_in_batch)} "
                f"retry={len(pending_retry)}",
                file=sys.stderr,
            )
            for tid in pending_retry:
                try:
                    p2 = subprocess.run(
                        ["bird", "unbookmark", tid],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                except subprocess.TimeoutExpired:
                    fail += 1
                    print(f"FAIL timeout {tid}", file=sys.stderr)
                    continue
                text2 = ((p2.stdout or "") + "\n" + (p2.stderr or "")).strip()
                kind = _classify_output(text2, tid)
                if kind in ("ok", "gone"):
                    ok += 1
                    done.append(tid)
                    label = "OK" if kind == "ok" else "GONE"
                    print(f"{label} {tid}", file=sys.stderr)
                else:
                    fail += 1
                    print(f"FAIL {tid}: {text2[:200]}", file=sys.stderr)
                time.sleep(0.35)
        time.sleep(delay_sec)

    print(f"unbookmark done: ok={ok} fail={fail}", file=sys.stderr)
    return ok, fail, done


def push_local_deletes_to_x(*, dry_run: bool = False) -> tuple[int, int]:
    """Unbookmark on X every suppressed id not yet marked unbookmarked.

    Keeps suppressed_ids (blocks re-import if re-bookmarked on X later).
    Adds successful ids to unbookmarked_ids.
    """
    state = load_state()
    # Fresh reconcile so disk deletes are in suppressed before push
    _local, suppressed, pruned = reconcile_state_with_local(state)
    already = {str(x) for x in (state.get("unbookmarked_ids") or [])}
    pending = sorted(set(suppressed) - already)
    print(
        f"push deletes: suppressed={len(suppressed)} already_unbookmarked={len(already)} "
        f"pending={len(pending)} newly_pruned={pruned}",
        file=sys.stderr,
    )
    if not pending:
        if not dry_run:
            state["suppressed_ids"] = sorted(suppressed)
            save_state(state)
        return 0, 0

    ok, fail, done = unbookmark_on_x(pending, dry_run=dry_run)
    if not dry_run:
        already |= set(done)
        state["suppressed_ids"] = sorted(suppressed)
        state["unbookmarked_ids"] = sorted(already)
        state["last_unbookmark"] = stamp_full()
        state["last_unbookmark_ok"] = ok
        state["last_unbookmark_fail"] = fail
        save_state(state)
    return ok, fail


def collect_local_bookmark_ids() -> set[str]:
    """Scan X_Bookmarks/ for tweet ids present as notes (frontmatter id: or filename).

    Excludes README.md and anything under _drop/. Notes without a parseable id are ignored.
    """
    root = VAULT / FOLDER
    found: set[str] = set()
    if not root.is_dir():
        return found
    for p in root.rglob("*.md"):
        if p.name == "README.md":
            continue
        if "_drop" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _ID_FM_RE.search(text)
        if m:
            found.add(m.group(1))
            continue
        m2 = _ID_FN_RE.search(p.name)
        if m2:
            found.add(m2.group(1))
    return found


def reconcile_state_with_local(state: dict) -> tuple[set[str], set[str], int]:
    """Align state with on-disk notes; treat missing notes as intentional deletes.

    Returns:
      local_ids: ids that currently have notes
      suppressed: ids that must not be re-imported (deleted locally, still bookmarked on X)
      pruned_count: how many ids dropped from state['ids'] because the note is gone
    """
    local_ids = collect_local_bookmark_ids()
    prev_ids = {str(x) for x in (state.get("ids") or [])}
    suppressed = {str(x) for x in (state.get("suppressed_ids") or [])}

    newly_gone = prev_ids - local_ids
    # Dropped from local → suppress forever (until a note reappears for that id)
    suppressed = (suppressed | newly_gone) - local_ids
    pruned_count = len(newly_gone)

    # ids tracks only notes that exist locally (before this run's new imports)
    state["ids"] = sorted(local_ids)
    state["suppressed_ids"] = sorted(suppressed)
    return local_ids, suppressed, pruned_count


def fetch_json(limit: int | None, max_pages: int | None) -> Path:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Prefer pure-python fetch (more reliable than nested bash eval)
    from bird_cookies import bird_env

    cmd = ["bird", "bookmarks", "--json"]
    if limit:
        cmd += ["-n", str(limit)]
    else:
        cmd.append("--all")
    if max_pages:
        cmd += ["--max-pages", str(max_pages)]

    print("Running:", " ".join(cmd), file=sys.stderr)
    env2 = bird_env()
    proc = subprocess.run(cmd, env=env2, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        # fallback birdclaw.sh
        print("bird failed, trying birdclaw.sh…", proc.stderr[:300], file=sys.stderr)
        sh = HERE / "birdclaw.sh"
        args = [str(sh)]
        if limit:
            args += ["--limit", str(limit)]
        if max_pages:
            args += ["--max-pages", str(max_pages)]
        proc2 = subprocess.run(args, env=env, capture_output=True, text=True, timeout=900, cwd=str(HERE))
        if proc2.returncode != 0:
            sys.exit(f"Fetch failed:\n{proc.stderr}\n{proc2.stderr}")
        out = (proc2.stdout or "").strip().splitlines()[-1]
        return Path(out)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(proc.stdout, encoding="utf-8")
    return CACHE


def tweet_to_note(t: dict) -> tuple[str, str, str]:
    """Return (tweet_id, url, markdown body)."""
    tid = str(t.get("id") or t.get("tweet_id") or "")
    author = t.get("author") or {}
    user = author.get("username") or author.get("screen_name") or "unknown"
    name = author.get("name") or user
    text = (t.get("text") or t.get("full_text") or "").strip()
    created = t.get("createdAt") or t.get("created_at") or ""
    url = f"https://x.com/{user}/status/{tid}" if tid else ""

    quoted = t.get("quotedTweet") or t.get("quoted_tweet")
    qblock = ""
    if isinstance(quoted, dict):
        qa = (quoted.get("author") or {}).get("username") or "?"
        qblock = f"\n> QT @{qa}: { (quoted.get('text') or '')[:500] }\n"

    media = t.get("media") or []
    mlines = ""
    if media:
        mlines = "\n## Media\n\n"
        for m in media:
            if isinstance(m, dict) and m.get("url"):
                mlines += f"- {m.get('type', 'media')}: {m['url']}\n"

    body = (
        f"---\n"
        f"source: x\n"
        f"id: {tid}\n"
        f"url: {url}\n"
        f"author: @{user}\n"
        f"author_name: {name}\n"
        f"created: {created}\n"
        f"saved: {stamp_full()}\n"
        f"via: birdclaw\n"
        f"category: {{category}}\n"
        f"---\n\n"
        f"# @{user} — {name}\n\n"
        f"{text}\n"
        f"{qblock}"
        f"{mlines}\n"
        f"- 元投稿: {url}\n"
    )
    return tid, url, body, text


def ensure_category_readme(cat: str) -> None:
    d = VAULT / FOLDER / safe_dir_name(cat)
    d.mkdir(parents=True, exist_ok=True)
    readme = d / "README.md"
    if not readme.is_file():
        readme.write_text(
            f"# {cat}\n\n内容キーワードで自動分類。\n"
            f"調整: `Scripts/config/x_categories.json`\n",
            encoding="utf-8",
        )


def reclassify_existing(dry_run: bool = False) -> int:
    """Move flat or wrongly-filed bookmark notes into category folders by content."""
    root = VAULT / FOLDER
    if not root.is_dir():
        return 0
    moved = 0
    # all md except README under root and category folders — re-score content
    for p in list(root.rglob("*.md")):
        if p.name == "README.md":
            continue
        if "_drop" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        cat, score = classify_x_category(text=text, title=p.stem)
        # inject/update category in frontmatter if present
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                fm = text[3:end]
                body_rest = text[end + 4 :]
                if "category:" in fm:
                    lines = []
                    for line in fm.splitlines():
                        if line.startswith("category:"):
                            lines.append(f"category: {cat}")
                        else:
                            lines.append(line)
                    fm = "\n".join(lines)
                else:
                    fm = fm.rstrip() + f"\ncategory: {cat}"
                text = f"---\n{fm}\n---{body_rest}"
        ensure_category_readme(cat)
        dest_dir = root / safe_dir_name(cat)
        dest = dest_dir / p.name
        if p.resolve().parent == dest_dir.resolve():
            # already in right folder; maybe update body
            if not dry_run and p.read_text(encoding="utf-8", errors="ignore") != text:
                p.write_text(text, encoding="utf-8")
            continue
        if dest.exists():
            dest = dest_dir / f"{p.stem}-moved.md"
        if dry_run:
            print(f"DRY MOVE [{cat}|{score}] {p.relative_to(root)} → {safe_dir_name(cat)}/")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            if p.read_text(encoding="utf-8", errors="ignore") != text:
                p.write_text(text, encoding="utf-8")
            shutil.move(str(p), str(dest))
            print(f"MOVE [{cat}|{score}] → {safe_dir_name(cat)}/{dest.name}")
        moved += 1
    return moved


def import_tweets(tweets: list, dry_run: bool) -> tuple[int, int, int, int]:
    """Import bookmarks. Returns (added, skipped, failed, state_pruned)."""
    state = load_state()
    local_ids, suppressed, pruned = reconcile_state_with_local(state)
    # Local notes + intentional deletes must never be re-created this run (or later)
    skip_ids = set(local_ids) | set(suppressed)
    seen_ids = set(local_ids)  # grows with newly written notes
    known_urls = existing_urls_in_folder(FOLDER)
    added = skipped = failed = 0

    print(
        f"state reconcile: local_ids={len(local_ids)} "
        f"pruned_from_state={pruned} suppressed_ids={len(suppressed)}",
        file=sys.stderr,
    )
    if dry_run and pruned:
        print(
            f"DRY-RUN: would drop {pruned} id(s) from state (notes missing locally; intentional delete)",
            file=sys.stderr,
        )

    enemy_blocked = 0
    for t in tweets:
        if not isinstance(t, dict):
            failed += 1
            continue
        tid, url, body_tmpl, text = tweet_to_note(t)
        if not tid:
            failed += 1
            continue
        # 既得権 / 親分反対派: never import; suppress + later unbookmark
        enemy_why = is_enemy_tweet(t)
        if enemy_why:
            enemy_blocked += 1
            skipped += 1
            suppressed.add(tid)
            skip_ids.add(tid)
            if dry_run:
                print(f"DRY ENEMY-SKIP {tid} ({enemy_why})", file=sys.stderr)
            continue
        if tid in skip_ids or (url and url in known_urls):
            skipped += 1
            continue
        cat, score = classify_x_category(text=text, title="")
        body = body_tmpl.replace("{category}", cat)
        user = ((t.get("author") or {}).get("username")) or "x"
        snippet = sanitize_filename((t.get("text") or "")[:40], 40)
        fname = f"{datetime.now().strftime('%Y-%m-%d')}-{user}-{tid}-{snippet}.md".replace(" ", "-")
        if len(fname) > 150:
            fname = f"{datetime.now().strftime('%Y-%m-%d')}-{user}-{tid}.md"
        ensure_category_readme(cat)
        rel = f"{FOLDER}/{safe_dir_name(cat)}/{fname}"
        if dry_run:
            print(f"DRY [{cat}|{score}] {rel}")
            added += 1
            continue
        write_note(rel, body)
        seen_ids.add(tid)
        skip_ids.add(tid)
        if url:
            known_urls.add(url)
        added += 1
        print(f"OK [{cat}|{score}] {rel}")

    if enemy_blocked:
        print(
            f"enemy blocklist: blocked {enemy_blocked} (incumbent/anti-boss; will unbookmark if push-deletes on)",
            file=sys.stderr,
        )

    if not dry_run:
        state["ids"] = sorted(seen_ids)
        state["suppressed_ids"] = sorted(suppressed)
        state["last_sync"] = stamp_full()
        state["last_count"] = len(tweets)
        state["last_enemy_blocked"] = enemy_blocked
        save_state(state)
    else:
        print(
            f"DRY-RUN summary: state_pruned={pruned} add_candidates={added} "
            f"skipped={skipped} enemy_blocked={enemy_blocked} failed={failed} (no writes)",
            file=sys.stderr,
        )
    return added, skipped, failed, pruned


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bidirectional X bookmarks sync (X↔Obsidian)"
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--from-json", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--reclassify-only",
        action="store_true",
        help="Only re-file existing notes by content (no fetch)",
    )
    ap.add_argument(
        "--no-reclassify",
        action="store_true",
        help="Skip reclassifying existing notes after sync",
    )
    ap.add_argument(
        "--unbookmark-only",
        action="store_true",
        help="Only push local deletes to X (bird unbookmark suppressed_ids)",
    )
    ap.add_argument(
        "--no-push-deletes",
        action="store_true",
        help="Pull only: do not unbookmark on X (old one-way mode)",
    )
    args = ap.parse_args()

    # root README — rewrite only when the text itself changed
    readme = VAULT / FOLDER / "README.md"
    readme_body = (
        "# X_Bookmarks\n\n"
        "内容キーワードで自動分類（作者名では分けない）:\n\n"
        "- `Tech-Tools/`\n"
        "- `親分関連/`\n"
        "- `Obsidian-Info/`\n"
        "- `Trading/`\n"
        "- `Consciousness-Spirit/`\n"
        "- `その他/`\n\n"
        "ルール編集: `Scripts/config/x_categories.json`\n"
        "再分類のみ: `python3 Scripts/x_sync_bookmarks.py --reclassify-only`\n"
        "Xから削除のみ: `python3 Scripts/x_sync_bookmarks.py --unbookmark-only`\n"
        "双方向: Full Sync は pull + ローカル削除分を X unbookmark\n"
    )
    old_readme = ""
    if readme.is_file():
        try:
            old_readme = readme.read_text(encoding="utf-8")
        except OSError:
            old_readme = ""
    if old_readme != readme_body:
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text(readme_body, encoding="utf-8")

    if args.reclassify_only:
        n = reclassify_existing(dry_run=args.dry_run)
        print(f"DONE kind=x added=0 skipped=0 failed=0 reclassified={n}")
        print(f"SUMMARY kind=x added=0 skipped=0 reclassified={n}")
        return

    if args.unbookmark_only:
        u_ok, u_fail = push_local_deletes_to_x(dry_run=args.dry_run)
        print(
            f"DONE kind=x added=0 skipped=0 failed={u_fail} "
            f"unbookmarked={u_ok} unbookmark_fail={u_fail}"
        )
        print(
            f"SUMMARY kind=x added=0 skipped=0 failed={u_fail} "
            f"unbookmarked={u_ok} unbookmark_fail={u_fail}"
        )
        return

    if args.from_json:
        path = Path(args.from_json)
    else:
        path = fetch_json(args.limit, args.max_pages)

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        tweets = data.get("bookmarks") or data.get("data") or data.get("tweets") or []
    else:
        tweets = data
    if not isinstance(tweets, list):
        sys.exit(f"Unexpected JSON shape in {path}")

    print(f"Loaded {len(tweets)} bookmarks from {path}")
    a, s, f, pruned = import_tweets(tweets, dry_run=args.dry_run)

    # local → X: push intentional deletes (default ON for true two-way sync)
    u_ok = u_fail = 0
    if not args.no_push_deletes:
        u_ok, u_fail = push_local_deletes_to_x(dry_run=args.dry_run)

    moved = 0
    if not args.no_reclassify and not args.dry_run:
        moved = reclassify_existing(dry_run=False)
    vault_n = (
        len(
            [
                p
                for p in (VAULT / FOLDER).rglob("*.md")
                if p.name != "README.md" and "_drop" not in p.parts and not p.name.startswith("_")
            ]
        )
        if (VAULT / FOLDER).is_dir()
        else 0
    )
    print(
        f"DONE kind=x added={a} skipped={s} failed={f} "
        f"state_pruned={pruned} unbookmarked={u_ok} unbookmark_fail={u_fail} "
        f"vault_notes={vault_n} reclassified={moved}"
    )
    print(
        f"SUMMARY kind=x added={a} skipped={s} failed={f} "
        f"state_pruned={pruned} unbookmarked={u_ok} unbookmark_fail={u_fail} "
        f"vault_notes={vault_n} fetched={len(tweets)} reclassified={moved}"
    )


if __name__ == "__main__":
    main()
