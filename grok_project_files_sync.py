#!/usr/bin/env python3
"""Sync grok.com project file libraries into Projects/<project>/Files/.

The cloud library is fetched per workspace (the project it belongs to).
Outside-vault local paths in config are still copied. Paths already inside
the vault are not copied again, and keyword names are not project folders.

Config: Scripts/config/grok_project_files.json (extend anytime).

Usage:
  python3 grok_project_files_sync.py
  python3 grok_project_files_sync.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from classify_lib import classify_chat_project, safe_dir_name
from vault_lib import LILITH_FOLDER, LILITH_LEGACY_FOLDERS, VAULT, sanitize_filename, stamp_full, write_note

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config" / "grok_project_files.json"
FOLDER = LILITH_FOLDER
PROJECTS_ROOT = "Projects"
STATE = VAULT / FOLDER / ".uploads_sync_state.json"
# Keyword buckets are not Lilith projects. TradeBot / Other alias onto the
# real hub folders. The rest are reference piles under Lilith/Materials/.
_PROJECT_ALIAS = {
    "TradeBot": "Trade Bot",
    "Other": "Uncategorized",
    "その他": "Uncategorized",
}
_NOT_A_PROJECT = {
    "Grok-Full-Fusion",
    "Web-Dev",
    "Full-Remote-Mission",
    "Extreme-Consciousness",
    "X-Research-Chat",
}


def load_config() -> dict:
    if not CONFIG.is_file():
        sys.exit(f"Missing config: {CONFIG}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"files": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(path.resolve()).encode())
    h.update(str(st.st_size).encode())
    h.update(str(int(st.st_mtime)).encode())
    # content sample for rename detection
    try:
        data = path.read_bytes()[:8192]
        h.update(data)
    except OSError:
        pass
    return h.hexdigest()[:24]


def is_text_candidate(path: Path, exts: set[str], max_bytes: int) -> bool:
    if path.suffix.lower() not in exts:
        return False
    try:
        if path.stat().st_size > max_bytes:
            return False
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    return True


def should_skip(path: Path, skip_frags: list[str]) -> bool:
    s = str(path)
    parts = set(path.parts)
    if ".obsidian" in parts or ".git" in parts or "node_modules" in parts:
        return True
    if path.suffix.lower() == ".json" and path.name in {
        "app.json",
        "appearance.json",
        "core-plugins.json",
        "community-plugins.json",
        "graph.json",
        "workspace.json",
        "workspace-mobile.json",
    }:
        return True
    for frag in skip_frags:
        if frag in s:
            return True
    # Skip already-in-Lilith trees (current + legacy) to avoid re-import loops
    for folder in (LILITH_FOLDER, *LILITH_LEGACY_FOLDERS):
        frag = f"/{folder.strip('/')}/"
        if frag in s or s.endswith(f"/{folder.strip('/')}"):
            return True
    return False


def resolve_project(path: Path, cfg: dict, force_project: str | None) -> str:
    if force_project:
        return force_project
    name = path.name
    lower = name.casefold()
    for rule in cfg.get("name_rules") or []:
        for frag in rule.get("name_contains") or []:
            if frag.casefold() in lower or frag in name:
                return rule.get("project") or "その他"
    # content-based fallback (first 3k)
    try:
        sample = path.read_text(encoding="utf-8", errors="ignore")[:3000]
    except OSError:
        sample = ""
    project, score = classify_chat_project(
        title=name, summary=sample, body_sample=sample, cwd=""
    )
    return project if score > 0 else (cfg.get("default_project") or "その他")


def read_as_markdown(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # strip BOM
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        body = raw
        # avoid nested frontmatter collision: if starts with ---, keep as body section
        if body.lstrip().startswith("---"):
            body = "## Original document\n\n" + body
        return body
    if ext == ".json":
        try:
            obj = json.loads(raw)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
            return f"```json\n{pretty}\n```\n"
        except json.JSONDecodeError:
            return f"```\n{raw}\n```\n"
    # plain text-ish
    return f"```\n{raw}\n```\n"


def dest_name(path: Path) -> str:
    stem = sanitize_filename(path.stem, 80)
    # avoid clobbering our folder README with source README.md
    if stem.casefold() == "readme":
        parent = sanitize_filename(path.parent.name, 40)
        stem = f"README-from-{parent}"
    return f"{stem}.md"


def _inside_vault(path: Path) -> bool:
    try:
        path.resolve().relative_to(VAULT.resolve())
        return True
    except (OSError, ValueError):
        return False


def upload_rel(project: str, path: Path, cfg: dict) -> str:
    """Changed attachments never create a fake project folder."""
    name = dest_name(path)
    if project in _NOT_A_PROJECT:
        return f"{FOLDER}/Materials/{safe_dir_name(project)}/{name}"
    folder = _PROJECT_ALIAS.get(project, project)
    sub = cfg.get("upload_subdir") or "_uploads"
    return f"{FOLDER}/{PROJECTS_ROOT}/{safe_dir_name(folder)}/{sub}/{name}"


def collect_candidates(cfg: dict) -> tuple[list[tuple[Path, str]], int, int]:
    """Return (files, missing_paths, in_vault_sources).

    A source that already lives in the vault is not copied again.
    Missing paths are counted, not printed (they are local config).
    """
    exts = {e.lower() for e in (cfg.get("text_extensions") or [".md", ".txt"])}
    max_bytes = int(cfg.get("max_file_bytes") or 2_000_000)
    skip = list(cfg.get("skip_name_contains") or [])
    found: dict[str, tuple[Path, str]] = {}
    missing = 0
    in_vault = 0

    # explicit first (highest priority)
    for item in cfg.get("explicit_files") or []:
        p = Path(item["path"]).expanduser()
        if not p.is_file():
            missing += 1
            continue
        if _inside_vault(p):
            in_vault += 1
            continue
        if not is_text_candidate(p, exts, max_bytes):
            missing += 1
            continue
        proj = item.get("project") or resolve_project(p, cfg, None)
        found[str(p.resolve())] = (p, proj)

    for src in cfg.get("sources") or []:
        root = Path(src["path"]).expanduser()
        if not root.exists():
            missing += 1
            continue
        if _inside_vault(root):
            in_vault += 1
            continue
        force = src.get("force_project")
        recursive = bool(src.get("recursive"))
        paths = root.rglob("*") if recursive and root.is_dir() else (
            root.iterdir() if root.is_dir() else [root]
        )
        for p in paths:
            if not p.is_file():
                continue
            if should_skip(p, skip):
                continue
            if not is_text_candidate(p, exts, max_bytes):
                continue
            # for vault sources with force_project, still apply name rules if stronger?
            proj = resolve_project(p, cfg, force)
            # only keep if name_rules or force or content classifies (avoid dumping entire 30-Projects)
            if not force:
                # require name rule hit or content classification with score
                name_hit = False
                lower = p.name.casefold()
                for rule in cfg.get("name_rules") or []:
                    for frag in rule.get("name_contains") or []:
                        if frag.casefold() in lower or frag in p.name:
                            name_hit = True
                if not name_hit:
                    try:
                        sample = p.read_text(encoding="utf-8", errors="ignore")[:2000]
                    except OSError:
                        sample = ""
                    _, sc = classify_chat_project(title=p.name, summary=sample, body_sample=sample)
                    if sc < 20:
                        continue
            key = str(p.resolve())
            if key not in found:
                found[key] = (p, proj)

    return list(found.values()), missing, in_vault


def sync_one(path: Path, project: str, cfg: dict, state: dict, dry_run: bool) -> str:
    """Returns 'added'|'updated'|'skipped'."""
    fp = file_fingerprint(path)
    key = str(path.resolve())
    prev = (state.get("files") or {}).get(key)
    if prev and prev.get("fp") == fp:
        # Unchanged source. A missing note was removed on purpose.
        return "skipped"

    rel = upload_rel(project, path, cfg)
    mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    body_core = read_as_markdown(path)
    note = (
        f"---\n"
        f"type: grok_project_file\n"
        f"project: {project}\n"
        f"source_path: {path}\n"
        f"original_name: {path.name}\n"
        f"source_mtime: {mtime}\n"
        f"synced: {stamp_full()}\n"
        f"---\n\n"
        f"# {path.name}\n\n"
        f"> **Grokプロジェクト資料**  \n"
        f"> プロジェクト: `{project}`  \n"
        f"> 元ファイル: `{path}`  \n"
        f"> 元更新: {mtime}\n\n"
        f"---\n\n"
        f"{body_core.rstrip()}\n"
    )
    if dry_run:
        print(f"DRY [{project}] {path.name} → {rel}")
        return "updated" if prev else "added"
    write_note(rel, note)
    readme = (VAULT / rel).parent / "README.md"
    if not readme.is_file():
        readme.write_text(
            f"# {project} — reference files\n\n"
            "Local text copied by `grok_project_files_sync.py`.\n",
            encoding="utf-8",
        )
    state.setdefault("files", {})[key] = {
        "fp": fp,
        "dest": rel,
        "project": project,
        "name": path.name,
        "synced": stamp_full(),
    }
    print(f"OK [{project}] {rel}")
    return "updated" if prev else "added"


def _stable_stamp(text: str) -> str:
    text = re.sub(r"(?m)^saved:.*$", "saved:", text)
    text = re.sub(r"(?m)^同期:.*$", "同期:", text)
    return text


def write_index(by_project: dict[str, list[str]]) -> None:
    lines = [
        "---",
        "type: grok_project_files_index",
        f"saved: {stamp_full()}",
        "---",
        "",
        "# Grok プロジェクト資料インデックス",
        "",
        f"同期: {stamp_full()}",
        "",
        "各プロジェクトの `_uploads/` に本文を Markdown 化して保存。",
        "",
    ]
    for proj in sorted(by_project.keys()):
        rels = [rel for rel in by_project[proj] if (VAULT / rel).is_file()]
        if not rels:
            continue
        lines.append(f"## {proj}")
        lines.append("")
        for rel in sorted(rels):
            name = Path(rel).name
            lines.append(f"- [[{rel}|{name}]]")
        lines.append("")
    body = "\n".join(lines) + "\n"
    rel = f"{FOLDER}/{PROJECTS_ROOT}/Grok-Materials-Index.md"
    path = VAULT / rel
    if path.is_file():
        try:
            old = path.read_text(encoding="utf-8")
        except OSError:
            old = ""
        if _stable_stamp(old) == _stable_stamp(body):
            print("INDEX unchanged", flush=True)
            return
    write_note(rel, body)


def sync_cloud_libraries() -> None:
    """Pit Files / All: grok.com file library, one folder per project."""
    script = Path(__file__).resolve().parent / "chat_sync_lilith_files.py"
    if not script.is_file():
        print("WARN cloud project files missing", flush=True)
        return
    print("=== chat_sync_lilith_files.py ===", flush=True)
    result = subprocess.run([sys.executable, str(script)], cwd=str(script.parent))
    if result.returncode != 0:
        print(f"WARN cloud project files failed code={result.returncode}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Grok project files into Second-Brain/Lilith Projects")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        sync_cloud_libraries()

    cfg = load_config()
    state = load_state()
    candidates, missing, in_vault = collect_candidates(cfg)
    print(f"Candidates: {len(candidates)}", flush=True)
    if missing:
        print(
            f"SKIP missing paths: {missing} (config/grok_project_files.json)",
            file=sys.stderr,
        )
    if in_vault:
        print(
            f"SKIP in-vault sources: {in_vault} (already in the vault, not copied again)",
            file=sys.stderr,
        )

    added = updated = skipped = 0
    by_project: dict[str, list[str]] = {}

    for path, project in sorted(candidates, key=lambda x: (x[1], x[0].name)):
        status = sync_one(path, project, cfg, state, args.dry_run)
        if status == "added":
            added += 1
        elif status == "updated":
            updated += 1
        else:
            skipped += 1
        dest = (state.get("files") or {}).get(str(path.resolve()), {}).get("dest")
        if dest and (VAULT / dest).is_file():
            by_project.setdefault(project, []).append(dest)

    for meta in (state.get("files") or {}).values():
        dest = meta.get("dest")
        proj = meta.get("project") or "Uncategorized"
        if dest and (VAULT / dest).is_file():
            bucket = by_project.setdefault(proj, [])
            if dest not in bucket:
                bucket.append(dest)

    if not args.dry_run:
        save_state(state)
        write_index(by_project)

    total = added + updated
    print(
        f"DONE kind=grok_files added={total} skipped={skipped} "
        f"failed=0 updated={updated} candidates={len(candidates)}"
    )
    print(
        f"SUMMARY kind=grok_files added={total} skipped={skipped} "
        f"candidates={len(candidates)} updated={updated}"
    )


if __name__ == "__main__":
    main()
