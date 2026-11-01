#!/usr/bin/env python3
"""Pull each grok.com project's file library into that same project folder.

Destination: Second-Brain/Lilith/Projects/<project>/Files/<library path>
The project name is the grok.com workspace name (Game-Dev is stored as Game Dev).
Keyword buckets are not used.

A file is downloaded only when its size or modified time changed, or the
local copy is missing. No Firefox login is a clean skip (exit 0).
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from chat_sync_lilith_web import GrokWeb, LoginExpired, _cookie_header
from classify_lib import safe_dir_name
from redact_lib import is_text, redact_bytes
from vault_lib import LILITH_FOLDER, VAULT

PROJECTS = VAULT / LILITH_FOLDER / "Projects"
INDEX = VAULT / LILITH_FOLDER / ".lilith_cloud_files.json"
ALIASES = {"Game-Dev": "Game Dev"}


def _skip(reason: str) -> None:
    print(f"SKIP project files: {reason}")
    print("SUMMARY kind=project_files added=0 skipped=0")


def _rel_parts(path: str) -> list[str] | None:
    parts: list[str] = []
    for part in str(path or "").strip("/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." or "/" in part or "\\" in part:
            return None
        parts.append(part)
    return parts or None


def _token(entry: dict) -> str:
    return f"{entry.get('size') or ''}|{entry.get('modifiedAt') or ''}"


def _list_dir(api: GrokWeb, workspace_id: str, path: str) -> list[dict]:
    url = f"https://grok.com/rest/workspaces/{workspace_id}/files"
    if path:
        url += "?path=" + urllib.request.quote(path)
    data = api.call(url)
    files = data.get("files") or []
    return files if isinstance(files, list) else []


def _walk(api: GrokWeb, workspace_id: str, path: str = "", seen: set[str] | None = None, depth: int = 0):
    if seen is None:
        seen = set()
    if path in seen or depth > 12:
        return
    seen.add(path)
    try:
        entries = _list_dir(api, workspace_id, path)
    except urllib.error.HTTPError:
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        child = str(entry.get("path") or "")
        if entry.get("isDirectory"):
            yield from _walk(api, workspace_id, child, seen, depth + 1)
        else:
            yield entry


def _download(api: GrokWeb, workspace_id: str, path: str, dest: Path) -> None:
    meta = api.call(
        f"https://grok.com/rest/workspaces/{workspace_id}/files/content?path="
        + urllib.request.quote(path)
    )
    url = str(meta.get("signedUrl") or "")
    if not url:
        raise RuntimeError("no download url")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 256)
        data = tmp.read_bytes()
        if is_text(data):
            try:
                data, _counts = redact_bytes(data)
            except Exception:
                pass
        tmp.write_bytes(data)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _load_index() -> dict:
    if not INDEX.is_file():
        return {}
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def _save_index(files: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n"
    if not INDEX.is_file() or INDEX.read_text(encoding="utf-8") != payload:
        INDEX.write_text(payload, encoding="utf-8")


def _ensure_readme(project_dir: Path, project: str) -> None:
    readme = project_dir / "README.md"
    if readme.is_file():
        return
    project_dir.mkdir(parents=True, exist_ok=True)
    readme.write_text(
        f"# {project} files\n\n"
        "File library from this grok.com project.\n"
        "Synced by `chat_sync_lilith_files.py`.\n",
        encoding="utf-8",
    )


def main() -> None:
    cookie = _cookie_header()
    if not cookie:
        _skip("no Firefox grok.com login. Open grok.com in Firefox, then sync again.")
        return
    api = GrokWeb(cookie)
    try:
        spaces = api.call("https://grok.com/rest/workspaces?pageSize=50").get("workspaces") or []
    except LoginExpired:
        _skip("Firefox login was refused. Open grok.com in Firefox, then sync again.")
        return
    if not isinstance(spaces, list):
        _skip("workspace list was empty")
        return
    index = _load_index()
    added = skipped = failed = 0
    by_project: dict[str, int] = {}
    for space in spaces:
        if not isinstance(space, dict):
            continue
        wid = str(space.get("workspaceId") or "")
        raw_name = str(space.get("name") or "Uncategorized")
        project = safe_dir_name(ALIASES.get(raw_name, raw_name))
        if not wid:
            continue
        print(f"PROJECT {project}", flush=True)
        project_dir = PROJECTS / project / "Files"
        try:
            entries = list(_walk(api, wid))
        except LoginExpired:
            _save_index(index)
            _skip("Firefox login was refused. Open grok.com in Firefox, then sync again.")
            return
        except urllib.error.HTTPError:
            print(f"WARN {project} list failed")
            failed += 1
            continue
        wrote_here = False
        for entry in entries:
            path = str(entry.get("path") or "")
            parts = _rel_parts(path)
            if not parts:
                failed += 1
                continue
            dest = project_dir.joinpath(*parts)
            token = _token(entry)
            key = f"{wid}:{path}"
            prev = index.get(key) if isinstance(index.get(key), dict) else {}
            if prev.get("token") == token and dest.is_file():
                skipped += 1
                by_project[project] = by_project.get(project, 0) + 1
                continue
            try:
                _download(api, wid, path, dest)
            except LoginExpired:
                _save_index(index)
                _skip("Firefox login was refused. Open grok.com in Firefox, then sync again.")
                return
            except Exception as exc:
                print(f"WARN {project} {parts[-1]} {type(exc).__name__}")
                failed += 1
                continue
            index[key] = {
                "token": token,
                "dest": str(dest.relative_to(VAULT)),
                "project": project,
            }
            added += 1
            wrote_here = True
            by_project[project] = by_project.get(project, 0) + 1
            if added % 25 == 0:
                _save_index(index)
                print(f"PROGRESS added={added} skipped={skipped}", flush=True)
        if wrote_here or project_dir.is_dir():
            _ensure_readme(project_dir, project)
    _save_index(index)
    summary = ", ".join(f"{name}={count}" for name, count in sorted(by_project.items()))
    print(f"project files: added={added} skipped={skipped} failed={failed} {summary}")
    print(
        f"SUMMARY kind=project_files added={added} skipped={skipped} "
        f"failed={failed}"
    )


if __name__ == "__main__":
    main()
