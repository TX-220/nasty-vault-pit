#!/usr/bin/env python3
"""Pull Lilith's grok.com project chats into Second-Brain/Lilith/Projects/.

Uses whichever Firefox profile is already logged into grok.com.
Does not read ~/.grok/sessions and does not store cookies.

A conversation is downloaded only when its list modifyTime changed,
or the note is missing. Unchanged chats are skipped after the list call.
No Firefox login is a clean skip (exit 0), not a failed sync.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from redact_lib import redact_bytes
from vault_lib import LILITH_FOLDER, VAULT

PROJECTS = VAULT / LILITH_FOLDER / "Projects"
INDEX = VAULT / LILITH_FOLDER / ".lilith_web_index.json"
UNCATEGORIZED = "Uncategorized"
PAGE = 100
_FIREFOX_ROOTS = (
    Path.home() / "snap/firefox/common/.mozilla/firefox",
    Path.home() / ".mozilla/firefox",
    Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
)


class LoginExpired(Exception):
    """Firefox has grok.com cookies, but grok.com rejected them."""


def _safe(name: str, limit: int = 80) -> str:
    bad = '<>:"/\\|?*'
    text = "".join("_" if ch in bad else ch for ch in name).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit] or "chat"


def _cookie_dbs() -> list[Path]:
    found: list[Path] = []
    for root in _FIREFOX_ROOTS:
        if root.is_dir():
            found.extend(sorted(root.glob("*/cookies.sqlite")))
    return found


def _read_cookie_header(db: Path) -> str:
    """Return a Cookie header, or "" when this profile has no grok.com login."""
    tmp = Path("/tmp") / f"lilith-web-cookies-{db.parent.name}.sqlite"
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE '%grok.com%' OR host LIKE '%x.ai%'"
            ).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return ""
    finally:
        tmp.unlink(missing_ok=True)
        Path(str(tmp) + "-wal").unlink(missing_ok=True)
        Path(str(tmp) + "-shm").unlink(missing_ok=True)
    if not rows:
        return ""
    return "; ".join(f"{name}={value}" for name, value in rows)


def _cookie_header() -> str:
    """First Firefox profile that is logged into grok.com. Never prints values."""
    for db in _cookie_dbs():
        header = _read_cookie_header(db)
        if header:
            return header
    return ""


def _skip(reason: str) -> None:
    print(f"SKIP lilith web: {reason}")
    print("SUMMARY kind=lilith_web added=0 skipped=0 empty=0 downloaded=0")


class GrokWeb:
    def __init__(self, cookie: str) -> None:
        self.cookie = cookie

    def call(self, url: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode()
        method = "GET" if payload is None else "POST"
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
                "Cookie": self.cookie,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise LoginExpired() from exc
            if exc.code == 429:
                time.sleep(5)
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            raise


def list_conversations(api: GrokWeb) -> list[dict]:
    out: list[dict] = []
    token = ""
    seen: set[str] = set()
    while True:
        url = f"https://grok.com/rest/app-chat/conversations?pageSize={PAGE}"
        if token:
            url += "&pageToken=" + urllib.request.quote(token)
        data = api.call(url)
        for row in data.get("conversations") or []:
            cid = str(row.get("conversationId") or "")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(row)
        token = str(data.get("nextPageToken") or "")
        if not token:
            return out


def workspace_name(api: GrokWeb, cache: dict[str, str], workspace_id: str) -> str:
    if not workspace_id:
        return UNCATEGORIZED
    if workspace_id in cache:
        return cache[workspace_id]
    aliases = {"Game-Dev": "Game Dev"}
    try:
        data = api.call(f"https://grok.com/rest/workspaces/{workspace_id}")
        raw = str(data.get("name") or UNCATEGORIZED)
        name = _safe(aliases.get(raw, raw), 80)
    except urllib.error.HTTPError:
        name = UNCATEGORIZED
    cache[workspace_id] = name or UNCATEGORIZED
    return cache[workspace_id]


def load_messages(api: GrokWeb, conversation_id: str) -> list[tuple[str, str]]:
    nodes = api.call(
        f"https://grok.com/rest/app-chat/conversations/{conversation_id}/response-node"
    )
    ids = [
        str(node.get("responseId"))
        for node in nodes.get("responseNodes") or []
        if node.get("responseId")
    ]
    if not ids:
        return []
    by_id: dict[str, dict] = {}
    for start in range(0, len(ids), 40):
        chunk = ids[start : start + 40]
        payload = api.call(
            f"https://grok.com/rest/app-chat/conversations/{conversation_id}/load-responses",
            {"responseIds": chunk},
        )
        for rec in payload.get("responses") or []:
            rid = str(rec.get("responseId") or "")
            if rid:
                by_id[rid] = rec
    messages: list[tuple[str, str]] = []
    for rid in ids:
        rec = by_id.get(rid)
        if not rec or rec.get("partial") is True:
            continue
        text = str(rec.get("message") or "").strip()
        if not text:
            continue
        sender = str(rec.get("sender") or "").lower()
        if sender in ("human", "user"):
            role = "User"
        elif sender in ("assistant", "bot", "grok"):
            role = "Lilith"
        else:
            role = sender or "Note"
        redacted, _counts = redact_bytes(text.encode("utf-8"))
        messages.append((role, redacted.decode("utf-8")))
    return messages


def _day(value: str) -> str:
    if not value:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d")


def _meta(raw: object) -> dict:
    if isinstance(raw, str):
        return {"path": raw, "modifyTime": "", "empty": False}
    if isinstance(raw, dict):
        return {
            "path": str(raw.get("path") or ""),
            "modifyTime": str(raw.get("modifyTime") or ""),
            "empty": bool(raw.get("empty")),
        }
    return {"path": "", "modifyTime": "", "empty": False}


def _saved_covers(path: Path, server_mtime: str) -> bool:
    """True when the note was written after the server's last modifyTime.

    `saved:` is minute precision. A server change inside that same minute
    is not proven, so only an earlier modifyTime counts as already saved.
    """
    try:
        server = datetime.fromisoformat(server_mtime.replace("Z", "+00:00"))
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:800]
    except OSError:
        return False
    match = re.search(r"(?m)^saved:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$", head)
    if not match:
        return False
    try:
        saved = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return False
    return server < saved


def write_chat(project: str, row: dict, messages: list[tuple[str, str]], index: dict) -> tuple[Path, bool]:
    cid = str(row.get("conversationId"))
    title = _safe(str(row.get("title") or "Untitled"), 70)
    day = _day(str(row.get("createTime") or ""))
    wid = str(row.get("workspaceId") or "")
    url = f"https://grok.com/project/{wid}?chat={cid}" if wid else f"https://grok.com/c/{cid}"
    dest_dir = PROJECTS / project
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta = _meta(index.get(cid))
    previous = meta["path"]
    if previous:
        dest = VAULT / previous
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = dest_dir / f"{day}-Lilith-{title}.md"
        if dest.exists():
            dest = dest_dir / f"{day}-Lilith-{title}-{cid[:8]}.md"
    body = [
        "---",
        "type: lilith_chat",
        "source: grok.com",
        f"conversation_id: {cid}",
        f"project: {project}",
        f"url: {url}",
        f"saved: {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    for role, text in messages:
        body.append(f"## {role}")
        body.append("")
        body.append(text)
        body.append("")
    text = "\n".join(body)
    changed = True
    if dest.is_file():
        old = dest.read_text(encoding="utf-8")
        old_stable = re.sub(r"^saved:.*$", "saved: stable", old, count=1, flags=re.M)
        new_stable = re.sub(r"^saved:.*$", "saved: stable", text, count=1, flags=re.M)
        changed = old_stable != new_stable
    if changed:
        dest.write_text(text, encoding="utf-8")
    index[cid] = {
        "path": str(dest.relative_to(VAULT)),
        "modifyTime": str(row.get("modifyTime") or ""),
        "empty": False,
    }
    return dest, changed


def _load_index() -> dict:
    if not INDEX.is_file():
        return {}
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(index: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    if not INDEX.is_file() or INDEX.read_text(encoding="utf-8") != payload:
        INDEX.write_text(payload, encoding="utf-8")


def main() -> None:
    cookie = _cookie_header()
    if not cookie:
        _skip("no Firefox grok.com login. Open grok.com in Firefox, then sync again.")
        return
    api = GrokWeb(cookie)
    try:
        rows = list_conversations(api)
    except LoginExpired:
        _skip("Firefox login was refused. Open grok.com in Firefox, then sync again.")
        return
    index = _load_index()
    names: dict[str, str] = {}
    written = 0
    unchanged = 0
    empty = 0
    downloaded = 0
    messages = 0
    by_project: dict[str, int] = {}
    for row in rows:
        cid = str(row.get("conversationId") or "")
        if not cid:
            continue
        server_mt = str(row.get("modifyTime") or "")
        meta = _meta(index.get(cid))
        dest = (VAULT / meta["path"]) if meta["path"] else None
        note_exists = bool(dest and dest.is_file())
        if server_mt and meta["modifyTime"] == server_mt and meta["empty"]:
            empty += 1
            index[cid] = meta
            continue
        if server_mt and meta["modifyTime"] == server_mt and note_exists:
            unchanged += 1
            index[cid] = meta
            continue
        if (
            server_mt
            and not meta["modifyTime"]
            and note_exists
            and dest is not None
            and _saved_covers(dest, server_mt)
        ):
            index[cid] = {"path": meta["path"], "modifyTime": server_mt, "empty": False}
            unchanged += 1
            continue
        project = workspace_name(api, names, str(row.get("workspaceId") or ""))
        try:
            thread = load_messages(api, cid)
        except LoginExpired:
            _save_index(index)
            _skip("Firefox login was refused. Open grok.com in Firefox, then sync again.")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"WARN {cid[:8]} {type(exc).__name__}")
            continue
        downloaded += 1
        if not thread:
            empty += 1
            index[cid] = {"path": meta["path"], "modifyTime": server_mt, "empty": True}
            time.sleep(0.15)
            continue
        _dest, changed = write_chat(project, row, thread, index)
        if changed:
            written += 1
        else:
            unchanged += 1
        messages += len(thread)
        by_project[project] = by_project.get(project, 0) + 1
        if downloaded % 10 == 0:
            print(f"PROGRESS downloaded {downloaded}")
        time.sleep(0.15)
    _save_index(index)
    summary = ", ".join(f"{name}={count}" for name, count in sorted(by_project.items()))
    print(
        f"lilith web: downloaded {downloaded}, skipped {unchanged} unchanged, "
        f"wrote {written}"
    )
    print(
        f"SUMMARY kind=lilith_web added={written} skipped={unchanged} "
        f"empty={empty} downloaded={downloaded} messages={messages} projects={summary}"
    )


if __name__ == "__main__":
    main()
