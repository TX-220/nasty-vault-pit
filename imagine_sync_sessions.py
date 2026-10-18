#!/usr/bin/env python3
"""Import Imagine-generated images + prompts into Imagine/ (not chat attachments).

Sources (local, free):
  1) Grok chat_history tool calls: image_gen / image_edit / image_to_video / reference_to_video
     → pair prompt args with result file paths
  2) Workspace folders: <cwd>/images/N.jpg|png|webp  (Imagine default output)
     → only numeric filenames (chat UUID images are ignored)

Does NOT scan session `images/image-UUID.png` user-attachment dumps.

Usage:
  python3 imagine_sync_sessions.py
  python3 imagine_sync_sessions.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from vault_lib import VAULT, sanitize_filename, stamp_full, write_note

GROK = Path.home() / ".grok" / "sessions"
FOLDER = "Imagine"
STATE = VAULT / FOLDER / ".sync_state.json"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGINE_TOOLS = {
    "image_gen",
    "image_edit",
    "image_to_video",
    "reference_to_video",
}
NUM_NAME = re.compile(r"^\d+\.(png|jpe?g|webp|gif)$", re.I)
PATH_RE = re.compile(
    r"(?:file://)?(/[^\s\"'<>]+\.(?:png|jpe?g|webp|gif))"
    r"|(?:^|[\s\"'])((?:[\w./-]+/)?images/\d+\.(?:png|jpe?g|webp|gif))",
    re.I,
)


def load_state() -> set[str]:
    if STATE.is_file():
        try:
            return set(json.loads(STATE.read_text(encoding="utf-8")).get("image_keys") or [])
        except json.JSONDecodeError:
            pass
    return set()


def save_state(keys: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "image_keys": sorted(keys),
                "last_sync": stamp_full(),
                "source": "imagine_tools_and_workspace_images",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_args_blob(args):
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return {"prompt": args}
    return args if isinstance(args, dict) else {}


def extract_paths_from_result(content, session_cwd: Path | None) -> list[Path]:
    """Resolve absolute/relative image paths from a tool_result payload."""
    found: list[Path] = []
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.append(json.dumps(content, ensure_ascii=False))
    elif isinstance(content, dict):
        texts.append(json.dumps(content, ensure_ascii=False))
    else:
        texts.append(str(content or ""))

    for text in texts:
        for m in PATH_RE.finditer(text):
            raw = m.group(1) or m.group(2)
            if not raw:
                continue
            raw = raw.strip().rstrip(").,;")
            if raw.startswith("file://"):
                raw = urlparse(raw).path
            raw = unquote(raw)
            p = Path(raw)
            if not p.is_absolute() and session_cwd:
                p = (session_cwd / raw).resolve()
            if p.is_file() and p.suffix.lower() in IMG_EXT:
                found.append(p)
    # also short form "images/1.jpg" mentioned as session-relative
    for m in re.finditer(r"\bimages/(\d+\.(?:png|jpe?g|webp|gif))\b", "\n".join(texts), re.I):
        if session_cwd:
            p = session_cwd / "images" / m.group(1)
            if p.is_file():
                found.append(p)
    # dedupe
    out, seen = [], set()
    for p in found:
        rp = str(p.resolve())
        if rp not in seen:
            seen.add(rp)
            out.append(p.resolve())
    return out


def session_cwd(summary_path: Path) -> Path | None:
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cwd = data.get("info", {}).get("cwd") or data.get("git_root_dir")
    if cwd:
        p = Path(cwd)
        if p.is_dir():
            return p
    return None


def collect_from_tool_calls() -> list[dict]:
    """Return list of {path, prompt, tool, session, mtime}."""
    items: list[dict] = []
    for chat in GROK.rglob("chat_history.jsonl"):
        summary = chat.parent / "summary.json"
        cwd = session_cwd(summary) if summary.is_file() else None
        lines: list[dict] = []
        for raw in chat.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

        pending: dict[str, dict] = {}  # tool_call_id -> {prompt, tool}
        for e in lines:
            if e.get("type") == "assistant":
                for call in e.get("tool_calls") or []:
                    name = (call.get("name") or "").strip()
                    if name not in IMAGINE_TOOLS:
                        continue
                    args = parse_args_blob(call.get("arguments"))
                    prompt = ""
                    if isinstance(args, dict):
                        prompt = str(
                            args.get("prompt")
                            or args.get("description")
                            or args.get("text")
                            or ""
                        )[:4000]
                    cid = call.get("id") or ""
                    pending[cid] = {"prompt": prompt, "tool": name, "session": chat.parent.name}
            elif e.get("type") == "tool_result":
                cid = e.get("tool_call_id") or ""
                meta = pending.pop(cid, None)
                if not meta:
                    # Only pair with known Imagine tool_calls — never scrape random shell output
                    continue
                for p in extract_paths_from_result(e.get("content"), cwd):
                    # ignore session chat-attachment UUID dumps
                    if p.parent.name == "images" and p.name.startswith("image-") and len(p.stem) > 20:
                        continue
                    if any(bad in str(p) for bad in ("node_modules", ".cache", "ComfyUI", "site-packages")):
                        continue
                    items.append(
                        {
                            "path": p,
                            "prompt": meta.get("prompt") or "",
                            "tool": meta.get("tool") or "image_gen",
                            "session": meta.get("session") or chat.parent.name,
                            "mtime": p.stat().st_mtime,
                        }
                    )
    return items


def collect_workspace_numeric_images() -> list[dict]:
    """Scan known session cwds for images/N.ext (Imagine default layout)."""
    cwds: set[Path] = set()
    for summary in GROK.rglob("summary.json"):
        c = session_cwd(summary)
        if c:
            cwds.add(c)
    # also common project roots
    for extra in (Path.home() / "projects", Path.home() / "dev", Path.home()):
        if extra.is_dir():
            for imgdir in extra.glob("**/images"):
                if not imgdir.is_dir():
                    continue
                if any(x in str(imgdir) for x in ("node_modules", ".cache", "ComfyUI", ".git")):
                    continue
                cwds.add(imgdir.parent)

    items: list[dict] = []
    seen: set[str] = set()
    for cwd in cwds:
        if any(bad in str(cwd) for bad in ("node_modules", ".cache", "ComfyUI", "site-packages")):
            continue
        imgdir = cwd / "images"
        if not imgdir.is_dir():
            continue
        for p in imgdir.iterdir():
            if not p.is_file() or p.suffix.lower() not in IMG_EXT:
                continue
            if not NUM_NAME.match(p.name):
                continue  # skip UUID chat attachments
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            items.append(
                {
                    "path": p.resolve(),
                    "prompt": "",
                    "tool": "workspace_images",
                    "session": str(cwd),
                    "mtime": p.stat().st_mtime,
                }
            )
    return items


def save_item(item: dict, seen: set[str], force: bool) -> bool:
    p: Path = item["path"]
    key = str(p.resolve())
    if key in seen and not force:
        return False
    day = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
    stem = sanitize_filename(p.stem[:40], 40)
    tool = sanitize_filename(item.get("tool") or "imagine", 30)
    dname = f"{day}-{tool}-{stem}"
    base = VAULT / FOLDER / dname
    n = 1
    while base.exists() and not force:
        dname = f"{day}-{tool}-{stem}-{n}"
        base = VAULT / FOLDER / dname
        n += 1
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"image{p.suffix.lower()}"
    shutil.copy2(p, dest)
    prompt = (item.get("prompt") or "").strip() or (
        "(prompt not found — image only; re-sync after generation with tool logs)"
    )
    md = (
        f"---\n"
        f"type: imagine\n"
        f"source: imagine_tool\n"
        f"tool: {item.get('tool')}\n"
        f"session: {item.get('session')}\n"
        f"orig: {p}\n"
        f"saved: {stamp_full()}\n"
        f"---\n\n"
        f"# {dname}\n\n"
        f"## Prompt\n\n"
        f"```\n{prompt}\n```\n\n"
        f"![[{FOLDER}/{dname}/{dest.name}]]\n"
    )
    write_note(f"{FOLDER}/{dname}/prompt.md", md)
    seen.add(key)
    print(f"OK {FOLDER}/{dname}/  tool={item.get('tool')}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    seen = load_state()
    # drop old chat-attachment keys style still ok in state
    items = collect_from_tool_calls() + collect_workspace_numeric_images()
    # dedupe by path, prefer one with prompt
    by_path: dict[str, dict] = {}
    for it in items:
        k = str(it["path"].resolve())
        if k not in by_path or (it.get("prompt") and not by_path[k].get("prompt")):
            by_path[k] = it
    ordered = sorted(by_path.values(), key=lambda x: x["mtime"], reverse=True)

    added = skipped = 0
    candidates = len(ordered)
    for it in ordered:
        key = str(it["path"].resolve())
        if key in seen and not args.force:
            skipped += 1
            continue
        if save_item(it, seen, args.force):
            added += 1
        if added >= args.limit:
            break

    save_state(seen)
    print(
        f"DONE kind=imagine added={added} skipped={skipped} "
        f"candidates={candidates} failed=0"
    )
    print(
        f"SUMMARY kind=imagine added={added} skipped={skipped} candidates={candidates}"
    )
    if candidates == 0:
        print(
            "NOTE: No Imagine outputs found. "
            "Chat attachment images (session images/image-UUID.png) are intentionally ignored. "
            "Generate with image_gen → files under <project>/images/N.jpg are picked up."
        )


if __name__ == "__main__":
    main()
