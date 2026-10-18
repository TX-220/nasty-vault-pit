#!/usr/bin/env python3
"""Fetch GitHub repos + stars via free `gh` CLI → Obsidian notes.

Usage:
  python3 github_sync.py              # both
  python3 github_sync.py --repos
  python3 github_sync.py --stars
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from vault_lib import VAULT, stamp_full, write_note

FOLDER = "GitHub"
JST = ZoneInfo("Asia/Tokyo")


def run_gh(args: list[str], timeout: int = 120) -> str:
    cmd = ["gh", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit("gh CLI not found. Install GitHub CLI (free).")
    except subprocess.TimeoutExpired:
        sys.exit(f"gh timed out: {' '.join(cmd)}")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        sys.exit(f"gh failed ({p.returncode}): {err[:500]}")
    return p.stdout


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    s = iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def esc_cell(text: str | None) -> str:
    if not text:
        return "—"
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "—"


def fetch_repos() -> list[dict]:
    # Own repos (public + private with repo scope). Limit high enough for personal use.
    raw = run_gh(
        [
            "repo",
            "list",
            "--limit",
            "500",
            "--json",
            "name,nameWithOwner,description,updatedAt,url,isPrivate,isFork,primaryLanguage,stargazerCount",
        ]
    )
    data = json.loads(raw or "[]")
    if not isinstance(data, list):
        sys.exit("Unexpected repo list JSON")
    return data


def fetch_stars() -> list[dict]:
    # Authenticated user stars (paginated). Free REST via gh.
    raw = run_gh(
        [
            "api",
            "--paginate",
            "user/starred",
            "--jq",
            (
                ".[] | {full_name, description, updated_at, html_url, "
                "language, stargazers_count, private, fork}"
            ),
        ],
        timeout=300,
    )
    items: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            # some gh versions dump a JSON array when not streaming
            try:
                blob = json.loads(raw)
                if isinstance(blob, list):
                    return blob
            except json.JSONDecodeError:
                pass
            continue
    if not items and raw.strip().startswith("["):
        blob = json.loads(raw)
        if isinstance(blob, list):
            return blob
    return items


def write_repos_note(repos: list[dict]) -> Path:
    # Sort by updated desc
    def key(r: dict) -> str:
        return r.get("updatedAt") or ""

    repos_sorted = sorted(repos, key=key, reverse=True)
    lines = [
        "---",
        "type: github_list",
        "kind: repositories",
        f"saved: {stamp_full()}",
        f"count: {len(repos_sorted)}",
        "source: gh repo list",
        "---",
        "",
        "# GitHub Repositories（自分のリポジトリ）",
        "",
        f"取得: {stamp_full()}  ·  件数: **{len(repos_sorted)}**",
        "",
        "| 名前 | 説明 | 更新 | Private |",
        "|------|------|------|---------|",
    ]
    for r in repos_sorted:
        name = r.get("nameWithOwner") or r.get("name") or "?"
        url = r.get("url") or ""
        link = f"[{esc_cell(name)}]({url})" if url else esc_cell(name)
        desc = esc_cell(r.get("description"))
        updated = fmt_date(r.get("updatedAt"))
        priv = "🔒" if r.get("isPrivate") else ""
        lines.append(f"| {link} | {desc} | {updated} | {priv} |")

    lines.extend(["", "## メモ", "", "- `gh` CLI（無料・ログイン済み）で取得", ""])
    body = "\n".join(lines)
    return write_note(f"{FOLDER}/Repositories.md", body)


def write_stars_note(stars: list[dict]) -> Path:
    def key(r: dict) -> str:
        return r.get("updated_at") or r.get("updatedAt") or ""

    stars_sorted = sorted(stars, key=key, reverse=True)
    lines = [
        "---",
        "type: github_list",
        "kind: stars",
        f"saved: {stamp_full()}",
        f"count: {len(stars_sorted)}",
        "source: gh api user/starred",
        "---",
        "",
        "# GitHub Stars（スターしたリポジトリ）",
        "",
        f"取得: {stamp_full()}  ·  件数: **{len(stars_sorted)}**",
        "",
        "| 名前 | 説明 | 更新 | 言語 |",
        "|------|------|------|------|",
    ]
    for r in stars_sorted:
        name = r.get("full_name") or r.get("nameWithOwner") or "?"
        url = r.get("html_url") or r.get("url") or ""
        link = f"[{esc_cell(name)}]({url})" if url else esc_cell(name)
        desc = esc_cell(r.get("description"))
        updated = fmt_date(r.get("updated_at") or r.get("updatedAt"))
        lang = esc_cell(r.get("language") or r.get("primaryLanguage"))
        if isinstance(r.get("primaryLanguage"), dict):
            lang = esc_cell(r["primaryLanguage"].get("name"))
        lines.append(f"| {link} | {desc} | {updated} | {lang} |")

    lines.extend(["", "## メモ", "", "- 詳細（Issue/中身）は取らない。一覧のみ。", ""])
    body = "\n".join(lines)
    return write_note(f"{FOLDER}/Stars.md", body)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync GitHub repos/stars into vault")
    ap.add_argument("--repos", action="store_true", help="Only own repositories")
    ap.add_argument("--stars", action="store_true", help="Only starred repositories")
    args = ap.parse_args()
    # no flags → both; only one flag → that one
    if args.repos and not args.stars:
        do_repos, do_stars = True, False
    elif args.stars and not args.repos:
        do_repos, do_stars = False, True
    else:
        do_repos = do_stars = True

    (VAULT / FOLDER).mkdir(parents=True, exist_ok=True)
    n_repos = n_stars = 0

    if do_repos:
        print("Fetching repositories…", flush=True)
        repos = fetch_repos()
        write_repos_note(repos)
        n_repos = len(repos)
        print(f"OK {FOLDER}/Repositories.md  ({n_repos})", flush=True)

    if do_stars:
        print("Fetching stars…", flush=True)
        stars = fetch_stars()
        write_stars_note(stars)
        n_stars = len(stars)
        print(f"OK {FOLDER}/Stars.md  ({n_stars})", flush=True)

    # README once
    readme = VAULT / FOLDER / "README.md"
    if not readme.is_file():
        readme.write_text(
            "# GitHub\n\n"
            "- `Repositories.md` … 自分のリポジトリ一覧\n"
            "- `Stars.md` … スターしたリポジトリ一覧\n\n"
            "ランチャーの GitHub ボタン、または `Scripts/github_sync.py` で更新。\n",
            encoding="utf-8",
        )

    print(
        f"DONE kind=github added={n_repos + n_stars} skipped=0 "
        f"failed=0 vault_notes={n_repos + n_stars}"
    )
    print(
        f"SUMMARY kind=github added={n_repos + n_stars} skipped=0 "
        f"repos={n_repos} stars={n_stars}"
    )


if __name__ == "__main__":
    main()
