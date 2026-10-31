#!/usr/bin/env python3
"""Pull Grok sessions into Second-Brain/Lilith/Projects/<project>/ (directory layout).

Projects are configurable in Scripts/config/chat_projects.json
(keywords + cwd). Adjust anytime without code changes.

Usage:
  python3 chat_sync_sessions.py
  python3 chat_sync_sessions.py --limit 20 --force
  python3 chat_sync_sessions.py --reorganize   # move flat notes into project dirs
  python3 chat_sync_sessions.py --full-mirror --dry-run   # preview raw session mirror
  python3 chat_sync_sessions.py --full-mirror [--mirror-delete]

A user timer (~/.config/systemd/user/grok-chat-sync.timer) runs this every
30 minutes. Sessions already imported are rewritten only when summary.json
is newer than the stored mtime, so a live session is not frozen at first sight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from classify_lib import classify_chat_project, safe_dir_name
from redact_lib import REDACTION_RULES_VERSION, exclusion_reason, redact_bytes
from vault_lib import LILITH_FOLDER, VAULT, sanitize_filename, stamp_full, write_note

GROK = Path.home() / ".grok" / "sessions"
FOLDER = LILITH_FOLDER
PROJECTS_ROOT = "Projects"  # Second-Brain/Lilith/Projects/<name>/
STATE = VAULT / FOLDER / ".sync_state.json"


@dataclass
class SyncState:
    ids: set[str] = field(default_factory=set)
    mtimes: dict[str, float] = field(default_factory=dict)
    last_sync: datetime | None = None


def load_state() -> SyncState:
    state = SyncState()
    if not STATE.is_file():
        return state
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return state
    state.ids = set(raw.get("session_ids") or [])
    for sid, mtime in (raw.get("session_mtimes") or {}).items():
        try:
            state.mtimes[str(sid)] = float(mtime)
        except (TypeError, ValueError):
            continue
    last = raw.get("last_sync")
    if isinstance(last, str):
        try:
            state.last_sync = datetime.strptime(last, "%Y-%m-%d %H:%M")
        except ValueError:
            state.last_sync = None
    return state


def save_state(state: SyncState) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_ids": sorted(state.ids),
        "session_mtimes": {sid: round(state.mtimes[sid], 3) for sid in sorted(state.mtimes)},
        "last_sync": stamp_full(),
    }
    STATE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def session_changed(summary_path: Path, state: SyncState, sid: str) -> bool:
    """True when an already-imported session has a newer summary.json."""
    mtime = summary_path.stat().st_mtime
    known = state.mtimes.get(sid)
    if known is not None:
        return mtime > known + 1.0
    if state.last_sync is None:
        return False
    file_minute = datetime.fromtimestamp(mtime).replace(second=0, microsecond=0)
    return file_minute > state.last_sync


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "\n".join(parts)
    return ""


def meaningful_user(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.startswith(("<command-name>", "<local-command", "<system-reminder>", "<user_info>", "<agent_")):
        return False
    if t.startswith("To:") and "From:" in t[:200]:
        return True
    return len(t) > 20


def session_mtime(summary_path: Path) -> datetime:
    return datetime.fromtimestamp(summary_path.stat().st_mtime, tz=timezone.utc)


def iter_sessions(days: int | None):
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for summary in sorted(GROK.rglob("summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if cutoff and session_mtime(summary) < cutoff:
            continue
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sid = data.get("info", {}).get("id") or summary.parent.name
        yield sid, summary, data


def build_highlight(
    sid: str, summary: dict, chat_path: Path, project: str
) -> tuple[str, str, str]:
    title = (
        summary.get("generated_title")
        or summary.get("session_summary")
        or summary.get("title")
        or summary.get("info", {}).get("title")
        or summary.get("preview")
        or sid[:8]
    )
    if isinstance(title, dict):
        title = title.get("text") or sid[:8]
    title = str(title)[:80]

    users: list[str] = []
    assistants: list[str] = []
    sample_bits: list[str] = []
    if chat_path.is_file():
        for raw in chat_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            text = extract_text(e.get("content"))
            if e.get("type") == "user" and meaningful_user(text):
                users.append(text.strip()[:1500])
                sample_bits.append(text[:800])
            elif e.get("type") == "assistant" and text.strip():
                assistants.append(text.strip()[:1500])

    parts = []
    if users:
        parts.append("## 依頼・指示（抜粋）\n")
        for u in users[:2]:
            parts.append(u + "\n")
        if len(users) > 2:
            parts.append(f"\n…他 {len(users) - 2} 件のユーザー発話\n")
            parts.append("\n### 最後のユーザー\n\n" + users[-1] + "\n")
    if assistants:
        parts.append("\n## 応答メモ（最後）\n\n" + assistants[-1][:2000] + "\n")

    body = (
        f"---\n"
        f"type: chat_highlight\n"
        f"source: grok_session\n"
        f"session_id: {sid}\n"
        f"project: {project}\n"
        f"who: Grok/Vesper\n"
        f"saved: {stamp_full()}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"**project:** {project}  \n"
        f"**session:** `{sid}`  \n"
        f"**when:** {stamp_full()}\n\n"
        + ("\n".join(parts) if parts else "_（本文抜粋なし — summary のみ）_\n")
        + f"\n---\n全文は `Second-Brain/Sessions/` または `grok-export-to-obsidian` を参照。\n"
    )
    sample = "\n".join(sample_bits[:3])
    return str(title), body, sample


def ensure_project_readme(project: str) -> None:
    d = VAULT / FOLDER / PROJECTS_ROOT / safe_dir_name(project)
    d.mkdir(parents=True, exist_ok=True)
    readme = d / "README.md"
    if not readme.is_file():
        readme.write_text(
            f"# {project}\n\nGrok プロジェクト相当の会話要点。\n"
            f"振り分けルール: `Scripts/config/chat_projects.json`\n",
            encoding="utf-8",
        )


def reorganize_flat_notes() -> int:
    """Move Lilith/*.md (not in Projects/) into project folders by frontmatter/title."""
    root = VAULT / FOLDER
    if not root.is_dir():
        return 0
    moved = 0
    for p in list(root.glob("*.md")):
        if p.name in ("README.md",):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        title = p.stem
        # try frontmatter project
        project = None
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                fm = text[3:end]
                for line in fm.splitlines():
                    if line.startswith("project:"):
                        project = line.split(":", 1)[1].strip()
        if not project:
            project, _ = classify_chat_project(title=title, summary=text[:2000], body_sample=text[:3000])
        ensure_project_readme(project)
        dest_dir = root / PROJECTS_ROOT / safe_dir_name(project)
        dest = dest_dir / p.name
        if dest.exists():
            dest = dest_dir / f"{p.stem}-{moved}.md"
        shutil.move(str(p), str(dest))
        print(f"MOVE {p.name} → {PROJECTS_ROOT}/{safe_dir_name(project)}/")
        moved += 1
    return moved


def classify_session(data: dict, chat: Path) -> tuple[str, int]:
    """Shared project classification (highlight flow and --full-mirror)."""
    cwd = (data.get("info") or {}).get("cwd") or data.get("git_root_dir") or ""
    title_hint = str(data.get("generated_title") or data.get("session_summary") or "")
    # pre-read sample for classification
    sample = ""
    if chat.is_file():
        sample = chat.read_text(encoding="utf-8", errors="ignore")[:4000]
    return classify_chat_project(
        title=title_hint,
        summary=str(data.get("session_summary") or ""),
        cwd=str(cwd),
        body_sample=sample,
    )


# ---------------------------------------------------------------------------
# --full-mirror: copy raw session dirs into Projects/<project>/Sessions/<rel>/
# ---------------------------------------------------------------------------
MIRROR_SUBDIR = "Sessions"
MIRROR_MANIFEST = ".mirror_manifest.json"


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _write_redacted(dst: Path, data: bytes, mtime: float) -> None:
    """Atomic write of already-redacted bytes, keeping the source mtime."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    tmp = dst.with_name(f".{dst.name}.mirror-tmp")
    try:
        tmp.write_bytes(data)
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _iter_session_files(session_dir: Path, grok_root: Path):
    """Regular files under session_dir; never follows symlinks or leaves grok_root."""
    for dirpath, dirnames, filenames in os.walk(session_dir, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink())
        for name in sorted(filenames):
            f = d / name
            if f.is_symlink() or not f.is_file() or not _within(f, grok_root):
                continue
            yield f


def mirror_sessions(days: int | None, dry_run: bool, delete: bool, report: bool = False) -> None:
    dest_root = (VAULT / FOLDER / PROJECTS_ROOT).resolve(strict=False)
    grok_root = GROK.resolve()
    tag = "DRY " if dry_run else ""
    copied = unchanged = deleted = sessions = excluded = errors = redacted_files = purged = 0
    redacted_totals: Counter = Counter()
    seen: set[Path] = set()
    for sid, summary_path, data in iter_sessions(days):
        session_dir = summary_path.parent
        if session_dir in seen or summary_path.is_symlink() or not _within(session_dir, grok_root):
            continue
        seen.add(session_dir)
        project, _ = classify_session(data, session_dir / "chat_history.jsonl")
        rel = session_dir.resolve().relative_to(grok_root)
        target = (dest_root / safe_dir_name(project) / MIRROR_SUBDIR / rel).resolve(strict=False)
        if rel == Path(".") or not _within(target, dest_root):
            print(f"SKIP unsafe target for session {sid}")
            continue
        sessions += 1
        wanted: set[str] = set()
        blocked: set[str] = set()
        for src in _iter_session_files(session_dir, grok_root):
            rp = src.relative_to(session_dir)
            dst = (target / rp).resolve(strict=False)
            if not _within(dst, target):
                continue
            rel_s = f"{rel.as_posix()}/{rp.as_posix()}"
            # Redaction is always on: raw bytes never reach the vault (fail closed).
            try:
                raw = src.read_bytes()
                reason = exclusion_reason(rp.as_posix(), raw)
                if reason:
                    print(f"EXCLUDE {rel_s} ({reason})")
                    excluded += 1
                    blocked.add(rp.as_posix())
                    continue
                out, counts = redact_bytes(raw)
                del raw
            except Exception as exc:  # noqa: BLE001 - never fall back to a raw copy
                print(f"ERROR redaction failed, skipped {rel_s} ({type(exc).__name__})")
                errors += 1
                blocked.add(rp.as_posix())
                continue
            wanted.add(rp.as_posix())
            redacted_totals.update(counts)
            if counts:
                redacted_files += 1
                detail = ",".join(f"{k}={v}" for k, v in sorted(counts.items()))
                if report:
                    print(f"REDACT {rel_s} {detail}")
            if dst.is_file() and not dst.is_symlink() and dst.read_bytes() == out:
                unchanged += 1
                continue
            print(f"{tag}COPY {rel_s} -> {safe_dir_name(project)}/{MIRROR_SUBDIR}/")
            copied += 1
            if not dry_run:
                _write_redacted(dst, out, src.stat().st_mtime)
        manifest = target / MIRROR_MANIFEST
        previous: set[str] = set()
        prev_rules = None
        if manifest.is_file():
            try:
                mdata = json.loads(manifest.read_text(encoding="utf-8"))
                previous = set(mdata.get("files") or [])
                prev_rules = mdata.get("redaction_rules")
            except (json.JSONDecodeError, OSError, AttributeError):
                previous = set()
        # Files this mirror wrote earlier that are now excluded/failed are purged
        # (independent of --mirror-delete) so no pre-redaction copy lingers.
        for bad in sorted(previous & blocked):
            victim = (target / bad).resolve(strict=False)
            if _within(victim, target) and victim.is_file():
                print(f"{tag}PURGE {safe_dir_name(project)}/{MIRROR_SUBDIR}/{rel.as_posix()}/{bad}")
                purged += 1
                if not dry_run:
                    victim.unlink()
        previous -= blocked
        if delete:
            # Only files this mirror previously wrote (listed in its manifest).
            for stale in sorted(previous - wanted):
                victim = (target / stale).resolve(strict=False)
                if not _within(victim, target) or not victim.is_file():
                    continue
                print(f"{tag}DELETE {safe_dir_name(project)}/{MIRROR_SUBDIR}/{rel.as_posix()}/{stale}")
                deleted += 1
                if not dry_run:
                    victim.unlink()
            keep = wanted
        else:
            keep = wanted | {p for p in previous if (target / p).is_file()}
        if not dry_run and (keep != previous or not manifest.is_file() or prev_rules != REDACTION_RULES_VERSION):
            target.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps({"session_id": sid, "source": rel.as_posix(),
                            "redaction_rules": REDACTION_RULES_VERSION, "files": sorted(keep)},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    print(
        f"SUMMARY kind=chat_mirror sessions={sessions} copied={copied} "
        f"unchanged={unchanged} deleted={deleted} excluded={excluded} errors={errors} purged={purged} dry_run={int(dry_run)}"
    )
    by_type = ",".join(f"{k}={v}" for k, v in sorted(redacted_totals.items())) or "none"
    print(f"REDACTION rules={REDACTION_RULES_VERSION} files_redacted={redacted_files} by_type={by_type}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reorganize", action="store_true", help="Only reorganize flat notes")
    ap.add_argument("--full-mirror", action="store_true",
                    help="Copy raw session dirs into Projects/<project>/Sessions/ (no highlights)")
    ap.add_argument("--dry-run", action="store_true", help="With --full-mirror: print actions, write nothing")
    ap.add_argument("--mirror-delete", action="store_true",
                    help="With --full-mirror: remove mirrored files no longer in the source "
                         "(only files listed in the mirror's own manifest)")
    ap.add_argument("--redaction-report", action="store_true",
                    help="With --full-mirror: list per-file redaction counts (never values)")
    args = ap.parse_args()

    if (args.dry_run or args.mirror_delete or args.redaction_report) and not args.full_mirror:
        ap.error("--dry-run / --mirror-delete / --redaction-report require --full-mirror")
    if args.full_mirror:
        if args.reorganize:
            ap.error("--full-mirror cannot be combined with --reorganize")
        if not GROK.is_dir():
            sys.exit(f"No grok sessions dir: {GROK}")
        mirror_sessions(args.days, args.dry_run, args.mirror_delete, args.redaction_report)
        return

    (VAULT / FOLDER / PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
    index = VAULT / FOLDER / "README.md"
    if not index.is_file() or "Projects/" not in index.read_text(encoding="utf-8", errors="ignore"):
        index.write_text(
            "# Lilith (Grok chat)\n\n"
            f"Path: `{FOLDER}/`\n\n"
            "Grok conversation notes, organized by project:\n\n"
            f"- `{PROJECTS_ROOT}/Web-Dev/`\n"
            f"- `{PROJECTS_ROOT}/Grok-Full-Fusion/`\n"
            f"- `{PROJECTS_ROOT}/X-Research-Chat/`\n"
            f"- `{PROJECTS_ROOT}/Extreme-Consciousness/`\n"
            f"- `{PROJECTS_ROOT}/TradeBot/`\n"
            f"- `{PROJECTS_ROOT}/Full-Remote-Mission/`\n"
            f"- `{PROJECTS_ROOT}/その他/`\n\n"
            "Routing: `Scripts/config/chat_projects.json`\n",
            encoding="utf-8",
        )

    if args.reorganize:
        n = reorganize_flat_notes()
        print(f"DONE kind=chat added=0 skipped=0 reorganized={n}")
        print(f"SUMMARY kind=chat added=0 skipped=0 reorganized={n}")
        return

    if not GROK.is_dir():
        sys.exit(f"No grok sessions dir: {GROK}")

    # always tidy flat notes a bit
    reorganized = reorganize_flat_notes()

    state = load_state()
    added = skipped = 0
    for sid, summary_path, data in iter_sessions(args.days):
        file_mtime = summary_path.stat().st_mtime
        refresh = session_changed(summary_path, state, sid)
        if sid in state.ids and not args.force and not refresh:
            state.mtimes.setdefault(sid, file_mtime)
            skipped += 1
            continue
        chat = summary_path.parent / "chat_history.jsonl"
        if data.get("num_messages", 1) == 0 and not chat.is_file():
            skipped += 1
            continue

        project, score = classify_session(data, chat)
        ensure_project_readme(project)

        title, body, _ = build_highlight(sid, data, chat, project)
        day = datetime.fromtimestamp(summary_path.stat().st_mtime).strftime("%Y-%m-%d")
        fname = f"{day}-Grok-{sanitize_filename(title, 50)}.md"
        rel = f"{FOLDER}/{PROJECTS_ROOT}/{safe_dir_name(project)}/{fname}"
        dest = VAULT / rel
        if dest.exists() and not args.force and not refresh:
            rel = (
                f"{FOLDER}/{PROJECTS_ROOT}/{safe_dir_name(project)}/"
                f"{day}-Grok-{sid[:8]}-{sanitize_filename(title, 40)}.md"
            )
        write_note(rel, body)
        state.ids.add(sid)
        state.mtimes[sid] = summary_path.stat().st_mtime
        added += 1
        print(f"OK [{project}|{score}] {rel}")
        if added >= args.limit:
            break

    save_state(state)
    vault_n = len(list((VAULT / FOLDER).rglob("*.md"))) if (VAULT / FOLDER).is_dir() else 0
    print(
        f"DONE kind=chat added={added} skipped={skipped} failed=0 "
        f"vault_notes={vault_n} reorganized={reorganized}"
    )
    print(
        f"SUMMARY kind=chat added={added} skipped={skipped} "
        f"vault_notes={vault_n} reorganized={reorganized}"
    )


if __name__ == "__main__":
    main()
