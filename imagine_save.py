#!/usr/bin/env python3
"""Save Imagine prompt (+ optional image) under Imagine/<date-slug>/.

Usage:
  python3 imagine_save.py -t "legion portrait" -p "anime girl, ..."
  python3 imagine_save.py -t "icon" -p "..." --image /path/to.png
  python3 imagine_save.py --process-drop
      # drop: Imagine/_drop/<name>.txt (prompt) and optional <name>.png/webp
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from vault_lib import VAULT, sanitize_filename, stamp, stamp_full, write_bytes, write_note

FOLDER = "Imagine"
DROP = VAULT / FOLDER / "_drop"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def slug_dir(title: str) -> str:
    return f"{stamp()}-{sanitize_filename(title, 60)}".replace(" ", "-")


def save(title: str, prompt: str, image: Path | None, extra: str = "") -> Path:
    dname = slug_dir(title)
    base = f"{FOLDER}/{dname}"
    img_link = ""
    if image and image.is_file():
        ext = image.suffix.lower() or ".png"
        img_name = f"image{ext}"
        data = image.read_bytes()
        ctype = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext, "application/octet-stream")
        write_bytes(f"{base}/{img_name}", data, content_type=ctype)
        img_link = f"\n![[{base}/{img_name}]]\n"
        # also copy via FS to be sure Obsidian sees binary
        dest = VAULT / base / img_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.stat().st_size != len(data):
            dest.write_bytes(data)

    md = (
        f"---\n"
        f"type: imagine\n"
        f"title: {title}\n"
        f"saved: {stamp_full()}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"## Prompt\n\n"
        f"```\n{prompt.strip()}\n```\n"
        f"{img_link}"
        + (f"\n## Notes\n\n{extra.strip()}\n" if extra.strip() else "")
    )
    path = write_note(f"{base}/prompt.md", md)
    print(f"OK -> {base}/")
    return path


def process_drop() -> int:
    DROP.mkdir(parents=True, exist_ok=True)
    done = DROP / "done"
    done.mkdir(exist_ok=True)
    n = 0
    # pair .txt prompts with same-stem images
    for p in sorted(DROP.glob("*.txt")):
        prompt = p.read_text(encoding="utf-8", errors="ignore")
        title = p.stem.replace("-", " ").replace("_", " ")
        image = None
        for ext in IMG_EXT:
            cand = DROP / f"{p.stem}{ext}"
            if cand.is_file():
                image = cand
                break
        save(title, prompt, image)
        p.rename(done / p.name)
        if image:
            image.rename(done / image.name)
        n += 1
    # orphan images without txt
    for img in sorted(DROP.iterdir()):
        if img.suffix.lower() in IMG_EXT and img.is_file():
            save(img.stem, f"(prompt missing — image only)\nsource: {img.name}", img)
            img.rename(done / img.name)
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Save Imagine prompt + image")
    ap.add_argument("-t", "--title", default="imagine", help="Short name")
    ap.add_argument("-p", "--prompt", default="", help="Prompt text")
    ap.add_argument("--image", default="", help="Path to image file")
    ap.add_argument("--note", default="", help="Extra notes")
    ap.add_argument("--stdin", action="store_true", help="Read prompt from stdin")
    ap.add_argument("--process-drop", action="store_true")
    args = ap.parse_args()

    if args.process_drop:
        print(f"saved {process_drop()} packages")
        return

    prompt = args.prompt
    if args.stdin or (not prompt and not sys.stdin.isatty()):
        prompt = sys.stdin.read()
    if not prompt.strip():
        sys.exit("Need --prompt or --stdin")
    img = Path(args.image) if args.image else None
    if args.image and not img.is_file():
        sys.exit(f"Image not found: {args.image}")
    save(args.title, prompt, img, extra=args.note)


if __name__ == "__main__":
    main()
