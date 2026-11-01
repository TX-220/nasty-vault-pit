#!/usr/bin/env python3
"""Pull Grok sessions into Second-Brain/Lilith/.

Highlights go to Projects/<project>/. A normal run also mirrors
~/.grok/sessions into Projects/<project>/<session-id>/, then pulls
Lilith's grok.com chats when Firefox is logged in.

Project folders are the hub genres in ~/.grok/vesper-hub.json.
Keyword names in Scripts/config/chat_projects.json are not folders.
Repeat runs skip unchanged highlight notes, unchanged mirrored files
(source size + mtime), and unchanged grok.com chats (list modifyTime).

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
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from classify_lib import classify_chat_project, safe_dir_name
from redact_lib import REDACTION_RULES_VERSION, is_text, redact_bytes
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
        + f"\n---\n全文は `Second-Brain/Lilith/Projects/{project}/{sid}/` を参照。\n"
    )
    sample = "\n".join(sample_bits[:3])
    return str(title), body, sample


def ensure_project_readme(project: str) -> None:
    d = VAULT / FOLDER / PROJECTS_ROOT / safe_dir_name(project)
    d.mkdir(parents=True, exist_ok=True)
    readme = d / "README.md"
    if not readme.is_file():
        readme.write_text(
            f"# {project}\n\nLilith project. Full sessions are in `<session-id>/`.\n"
            f"Project names come from `~/.grok/vesper-hub.json` genres.\n",
            encoding="utf-8",
        )


def reorganize_flat_notes() -> int:
    """Move Lilith/*.md (not in Projects/) into a real hub project, else Uncategorized.

    Keyword buckets from chat_projects.json are not folders. A note is filed by
    its session_id in the hub genre map, or by a frontmatter project only when
    that name is already a hub genre.
    """
    root = VAULT / FOLDER
    if not root.is_dir():
        return 0
    genres = load_genres()
    real = set(genres.values()) | {UNCATEGORIZED}
    moved = 0
    for p in list(root.glob("*.md")):
        if p.name in ("README.md",):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        sid = ""
        project = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                for line in text[3:end].splitlines():
                    if line.startswith("session_id:"):
                        sid = line.split(":", 1)[1].strip()
                    elif line.startswith("project:"):
                        project = line.split(":", 1)[1].strip()
        if sid in genres:
            project = project_for_session(sid, genres)
        elif safe_dir_name(project) not in real:
            project = UNCATEGORIZED
        else:
            project = safe_dir_name(project)
        ensure_project_readme(project)
        dest_dir = root / PROJECTS_ROOT / project
        dest = dest_dir / p.name
        if dest.exists():
            dest = dest_dir / f"{p.stem}-{moved}.md"
        shutil.move(str(p), str(dest))
        print(f"MOVE {p.name} → {PROJECTS_ROOT}/{project}/")
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
# Full mirror: every file under ~/.grok/sessions.
# Each session lands in Lilith/Projects/<real project>/<session-id>/.
# The project name is the hub genre (~/.grok/vesper-hub.json), the same list
# as Lilith's Projects sidebar. Files that are not inside a session stay in
# Lilith/Sessions/.
# ---------------------------------------------------------------------------
MIRROR_SUBDIR = "Sessions"
MIRROR_MANIFEST = ".session_mirror_manifest.json"
HUB_META = Path.home() / ".grok" / "vesper-hub.json"
UNCATEGORIZED = "Uncategorized"


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


def _iter_grok_files(grok_root: Path):
    """Every regular file under the sessions root. Symlinks are not followed."""
    for dirpath, dirnames, filenames in os.walk(grok_root, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink())
        for name in sorted(filenames):
            f = d / name
            if f.is_symlink() or not f.is_file() or not _within(f, grok_root):
                continue
            yield f


def _encoded_cwd_segment(name: str) -> bool:
    """Grok stores sessions under a percent-encoded cwd, e.g. %2Fhome%2Ftx220."""
    return name.startswith("%2F") or name.startswith("%2f")


def load_genres() -> dict[str, str]:
    """Session id -> Lilith project name, from the hub genre map."""
    try:
        data = json.loads(HUB_META.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("genres") or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for sid, name in raw.items():
        if not isinstance(sid, str) or not isinstance(name, str):
            continue
        label = name.strip()
        if not label or label == "Other":
            label = UNCATEGORIZED
        out[sid] = label
    return out


def project_for_session(sid: str, genres: dict[str, str]) -> str:
    return safe_dir_name(genres.get(sid) or UNCATEGORIZED)


def _session_index(grok_root: Path) -> dict[Path, str]:
    """Resolved session directory -> session id."""
    found: dict[Path, str] = {}
    for summary in grok_root.rglob("summary.json"):
        if summary.is_symlink() or not summary.is_file() or not _within(summary, grok_root):
            continue
        session_dir = summary.parent.resolve()
        sid = session_dir.name
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            info = data.get("info") or {}
            if isinstance(info, dict) and info.get("id"):
                sid = str(info["id"])
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        found[session_dir] = sid
    return found


def _session_of(src: Path, index: dict[Path, str]) -> tuple[str, Path] | None:
    resolved = src.resolve()
    for session_dir, sid in index.items():
        try:
            return sid, resolved.relative_to(session_dir)
        except ValueError:
            continue
    return None


def mirror_root() -> Path:
    """Lilith root. Sessions go under Projects/<real project>/, loose files under Sessions/."""
    return (VAULT / FOLDER).resolve(strict=False)


def warn_if_no_source() -> bool:
    """Print a warning when the sessions source is missing or empty. True if usable."""
    if not GROK.is_dir():
        print(f"WARN sessions source missing: {GROK} (nothing to mirror)")
        return False
    if not any(_iter_grok_files(GROK)):
        print(f"WARN sessions source empty (no files under {GROK}); nothing to mirror")
        return False
    return True


def _loose_rel(src: Path, grok_root: Path) -> Path:
    """Relative path for a file that is not inside a session directory."""
    rel = src.resolve().relative_to(grok_root.resolve())
    parts = rel.parts
    if len(parts) > 1 and _encoded_cwd_segment(parts[0]):
        return Path(*parts[1:])
    return rel


def _plan_mirror(grok_root: Path, genres: dict[str, str]) -> dict[str, Path]:
    """Path relative to Lilith/ -> source file."""
    index = _session_index(grok_root)
    planned: dict[str, Path] = {}
    for src in _iter_grok_files(grok_root):
        hit = _session_of(src, index)
        if hit:
            sid, inside = hit
            project = project_for_session(sid, genres)
            key = f"{PROJECTS_ROOT}/{project}/{sid}/{inside.as_posix()}"
        else:
            key = f"{MIRROR_SUBDIR}/{_loose_rel(src, grok_root).as_posix()}"
        if key in planned and planned[key].resolve() != src.resolve():
            key = f"{MIRROR_SUBDIR}/{src.resolve().relative_to(grok_root).as_posix()}"
        planned[key] = src
    return planned


def _remove_old_flat_sessions(lilith: Path, session_ids: set[str]) -> int:
    """Remove the previous Sessions/<session-id>/ layout now that files live under Projects/."""
    sessions_root = lilith / MIRROR_SUBDIR
    if not sessions_root.is_dir():
        return 0
    removed = 0
    for sid in sorted(session_ids):
        child = sessions_root / sid
        if child.is_dir() and not child.is_symlink():
            print(f"REMOVE flat mirror {MIRROR_SUBDIR}/{sid}/")
            shutil.rmtree(child)
            removed += 1
    old_manifest = sessions_root / ".mirror_manifest.json"
    if old_manifest.is_file() and not old_manifest.is_symlink():
        old_manifest.unlink()
    return removed


def _fp_record(mtime_ns: int, src_size: int, dst_size: int) -> dict[str, int]:
    return {"src_mtime_ns": int(mtime_ns), "src_size": int(src_size), "dst_size": int(dst_size)}


def _load_mirror_manifest(manifest: Path) -> tuple[set[str], object, dict]:
    previous: set[str] = set()
    prev_rules = None
    fingerprints: dict = {}
    if manifest.is_file() and not manifest.is_symlink():
        try:
            mdata = json.loads(manifest.read_text(encoding="utf-8"))
            previous = set(mdata.get("files") or [])
            prev_rules = mdata.get("redaction_rules")
            raw_fp = mdata.get("fingerprints") or {}
            if isinstance(raw_fp, dict):
                fingerprints = raw_fp
        except (json.JSONDecodeError, OSError, AttributeError):
            previous = set()
    return previous, prev_rules, fingerprints


def mirror_sessions(days: int | None, dry_run: bool, delete: bool, report: bool = False) -> None:
    """Copy every file under ~/.grok/sessions. `days` limits highlights only."""
    del days  # full mirror is always the whole tree
    dest_root = mirror_root()
    grok_root = GROK.resolve()
    genres = load_genres()
    tag = "DRY " if dry_run else ""
    copied = unchanged = deleted = binary = errors = redacted_files = 0
    redacted_totals: Counter = Counter()
    planned = _plan_mirror(grok_root, genres)
    sessions = sum(1 for key in planned if key.endswith("/summary.json"))
    wanted = set(planned)
    manifest = dest_root / MIRROR_MANIFEST
    previous, prev_rules, fingerprints = _load_mirror_manifest(manifest)
    rules_match = prev_rules == REDACTION_RULES_VERSION
    new_fps: dict[str, dict[str, int]] = {}
    for key in sorted(planned):
        src = planned[key]
        dst = (dest_root / key).resolve(strict=False)
        if not _within(dst, dest_root):
            print(f"SKIP unsafe target {key}")
            wanted.discard(key)
            continue
        st = src.stat()
        fp = fingerprints.get(key) if rules_match else None
        dst_ok = dst.is_file() and not dst.is_symlink()
        dst_size = dst.stat().st_size if dst_ok else -1
        if (
            isinstance(fp, dict)
            and int(fp.get("src_mtime_ns") or -1) == st.st_mtime_ns
            and int(fp.get("src_size") or -1) == st.st_size
            and dst_size == int(fp.get("dst_size") or -2)
        ):
            unchanged += 1
            new_fps[key] = _fp_record(st.st_mtime_ns, st.st_size, dst_size)
            continue
        raw = src.read_bytes()
        counts: Counter = Counter()
        if is_text(raw):
            try:
                out, counts = redact_bytes(raw)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN redaction failed, copied raw {key} ({type(exc).__name__})")
                errors += 1
                out = raw
        else:
            binary += 1
            out = raw
        redacted_totals.update(counts)
        if counts:
            redacted_files += 1
            if report:
                detail = ",".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"REDACT {key} {detail}")
        new_fps[key] = _fp_record(st.st_mtime_ns, st.st_size, len(out))
        if dst_ok and dst.read_bytes() == out:
            unchanged += 1
            continue
        print(f"{tag}COPY {key}")
        copied += 1
        if not dry_run:
            _write_redacted(dst, out, st.st_mtime)
    if delete:
        for stale in sorted(previous - wanted):
            victim = (dest_root / stale).resolve(strict=False)
            if not _within(victim, dest_root) or not victim.is_file() or victim.is_symlink():
                continue
            print(f"{tag}DELETE {stale}")
            deleted += 1
            if not dry_run:
                victim.unlink()
        keep = wanted
    else:
        keep = wanted | {p for p in previous if (dest_root / p).is_file()}
    legacy = 0
    if not dry_run:
        payload = {
            "source": str(grok_root),
            "redaction_rules": REDACTION_RULES_VERSION,
            "files": sorted(keep),
            "fingerprints": {k: new_fps[k] for k in sorted(new_fps)},
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        old_text = ""
        if manifest.is_file() and not manifest.is_symlink():
            try:
                old_text = manifest.read_text(encoding="utf-8")
            except OSError:
                old_text = ""
        if text != old_text:
            dest_root.mkdir(parents=True, exist_ok=True)
            manifest.write_text(text, encoding="utf-8")
        session_ids = {key.split("/")[2] for key in wanted if key.startswith(f"{PROJECTS_ROOT}/") and key.count("/") >= 2}
        legacy = _remove_old_flat_sessions(dest_root, session_ids)
    print(
        f"SUMMARY kind=chat_mirror added={copied} skipped={unchanged} "
        f"sessions={sessions} files={len(wanted)} deleted={deleted} legacy_buckets={legacy} "
        f"binary_raw={binary} redaction_errors_copied_raw={errors} dry_run={int(dry_run)}"
    )
    by_type = ",".join(f"{k}={v}" for k, v in sorted(redacted_totals.items())) or "none"
    print(f"REDACTION rules={REDACTION_RULES_VERSION} files_redacted={redacted_files} by_type={by_type}")
    print(f"MIRROR {'(dry-run) ' if dry_run else ''}dest={dest_root} source={GROK} "
          f"sessions={sessions} files={len(wanted)} copied={copied} unchanged={unchanged}")


def sync_lilith_web() -> None:
    """Pit Chat / All: pull grok.com project chats with the Firefox login."""
    script = Path(__file__).resolve().parent / "chat_sync_lilith_web.py"
    if not script.is_file():
        print("WARN lilith web sync missing", flush=True)
        return
    print("=== chat_sync_lilith_web.py ===", flush=True)
    result = subprocess.run([sys.executable, str(script)], cwd=str(script.parent))
    if result.returncode != 0:
        print(f"WARN lilith web sync failed code={result.returncode}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Default: highlight notes + a full copy of ~/.grok/sessions into Lilith/Projects/<project>/.")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reorganize", action="store_true", help="Only reorganize flat notes")
    ap.add_argument("--highlights-only", action="store_true",
                    help="Old behavior: highlight notes only, no mirror")
    ap.add_argument("--full-mirror", action="store_true",
                    help="Mirror only: copy every session into Lilith/Projects/<real project>/<session-id>/ (no highlights)")
    ap.add_argument("--dry-run", action="store_true", help="With --full-mirror: print actions, write nothing")
    ap.add_argument("--mirror-delete", action="store_true",
                    help="Remove mirrored files no longer in the source "
                         "(only files listed in the mirror's own manifest)")
    ap.add_argument("--redaction-report", action="store_true",
                    help="List per-file redaction counts (never values)")
    args = ap.parse_args()

    if args.highlights_only and args.full_mirror:
        ap.error("--highlights-only and --full-mirror are mutually exclusive")
    if args.dry_run and not args.full_mirror:
        ap.error("--dry-run requires --full-mirror")
    if (args.mirror_delete or args.redaction_report) and (args.highlights_only or args.reorganize):
        ap.error("--mirror-delete / --redaction-report need the mirror (not --highlights-only / --reorganize)")
    if args.full_mirror:
        if args.reorganize:
            ap.error("--full-mirror cannot be combined with --reorganize")
        if not warn_if_no_source() and not GROK.is_dir():
            sys.exit(f"No grok sessions dir: {GROK}")
        mirror_sessions(args.days, args.dry_run, args.mirror_delete, args.redaction_report)
        return
    if args.highlights_only or args.reorganize:
        run_highlights(args)
        return
    # Default: highlights, local session mirror, then Lilith's grok.com chats.
    # The Pit Chat button and All button both land here.
    has_source = warn_if_no_source()
    run_highlights(args)
    if has_source:
        mirror_sessions(args.days, False, args.mirror_delete, args.redaction_report)
    sync_lilith_web()


def run_highlights(args: argparse.Namespace) -> None:
    (VAULT / FOLDER / PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
    index = VAULT / FOLDER / "README.md"
    if not index.is_file() or "Projects/" not in index.read_text(encoding="utf-8", errors="ignore"):
        index.write_text(
            "# Lilith (Grok chat)\n\n"
            f"Path: `{FOLDER}/`\n\n"
            "Grok sessions, organized by hub project:\n\n"
            f"- `{PROJECTS_ROOT}/Infra & Environment/`\n"
            f"- `{PROJECTS_ROOT}/Security/`\n"
            f"- `{PROJECTS_ROOT}/Freelance/`\n"
            f"- `{PROJECTS_ROOT}/Trade Bot/`\n"
            f"- `{PROJECTS_ROOT}/Information Dominance/`\n"
            f"- `{PROJECTS_ROOT}/Game Dev/`\n"
            f"- `{PROJECTS_ROOT}/Uncategorized/`\n\n"
            "Names: `~/.grok/vesper-hub.json` genres.\n",
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
    genres = load_genres()
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

        project = project_for_session(sid, genres)
        score = 1 if sid in genres else 0
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
