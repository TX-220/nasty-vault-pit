#!/usr/bin/env python3
"""Export simple all-sessions Obsidian Canvas (no AI, no deep analysis).

Modes:
  --lilith   Second-Brain/Lilith/Projects → Second-Brain/Lilith/All-Lilith-Sessions.canvas
  --vesper   Prefer VesperForge API if running; else message to use Forge UI

Independent of logical-session linking. Folder structure is trusted as-is.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from vault_lib import LILITH_FOLDER, VAULT as VL_VAULT  # noqa: E402

VAULT = Path(VL_VAULT)

CHAT_PROJECTS = VAULT / LILITH_FOLDER / "Projects"
# Lilith overview lives with Lilith notes (not under Sessions/)
OUT_LILITH = VAULT / LILITH_FOLDER / "All-Lilith-Sessions.canvas"
OUT_VESPER = VAULT / "Second-Brain" / "Sessions" / "Vesper" / "All-Vesper-Sessions.canvas"

FORGE_URL = "http://127.0.0.1:7420"

# Layout (match VesperForge lib/canvas_export.py overview)
GROUP_W, GROUP_H = 280, 88
LEAF_W, LEAF_H = 300, 72
GROUP_H_GAP = 80
GROUP_V_GAP = 48
LEAF_V_GAP = 16
_PROJECT_COLORS = ("4", "5", "3", "2", "1", "6")


def _node(nid: str, text: str, *, x: int, y: int, width: int, height: int, color: str) -> dict:
    return {
        "id": nid,
        "type": "text",
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "color": color,
    }


def _edge(eid: str, frm: str, to: str, *, color: str) -> dict:
    return {
        "id": eid,
        "fromNode": frm,
        "fromSide": "right",
        "toNode": to,
        "toSide": "left",
        "label": "",
        "color": color,
    }


def _date_prefix(stem: str) -> str:
    if len(stem) >= 10 and stem[4] == "-" and stem[7] == "-":
        maybe = stem[:10]
        if maybe[0:4].isdigit() and maybe[5:7].isdigit() and maybe[8:10].isdigit():
            return maybe
    return ""


def discover_lilith() -> list[dict]:
    if not CHAT_PROJECTS.is_dir():
        return []
    skip_dirs = {"_uploads", "_drop", ".git"}
    by_proj: dict[str, list[dict]] = defaultdict(list)
    for project_dir in sorted(CHAT_PROJECTS.iterdir(), key=lambda p: p.name.lower()):
        if not project_dir.is_dir() or project_dir.name in skip_dirs or project_dir.name.startswith("."):
            continue
        for path in project_dir.glob("*.md"):
            if path.name.lower() == "readme.md" or path.name.startswith("."):
                continue
            stem = path.stem
            date = _date_prefix(stem)
            title = stem
            if date and stem.startswith(date):
                title = stem[len(date) :].lstrip("-_ ") or stem
            by_proj[project_dir.name].append({"title": title, "date": date})
    rows: list[dict] = []
    for proj in sorted(by_proj.keys(), key=str.lower):
        items = by_proj[proj]
        items.sort(key=lambda r: r.get("date") or "", reverse=True)
        for it in items:
            rows.append({"project": proj, **it})
    return rows


def build_grouped_canvas(
    *,
    root_title: str,
    root_subtitle: str,
    groups: list[dict],
    root_color: str = "5",
) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    active = [g for g in groups if g.get("items")]
    leaf_total = sum(len(g["items"]) for g in active)
    root_id = "root"
    nodes.append(
        _node(
            root_id,
            f"# {root_title}\n{root_subtitle}\n\n{leaf_total} sessions · {len(active)} groups",
            x=-GROUP_W // 2,
            y=-GROUP_H - 40,
            width=max(GROUP_W, 360),
            height=GROUP_H + 20,
            color=root_color,
        )
    )
    if not active:
        nodes.append(
            _node(
                "empty",
                "### 空\nセッションが見つかりませんでした",
                x=GROUP_W + GROUP_H_GAP,
                y=0,
                width=LEAF_W,
                height=LEAF_H,
                color="6",
            )
        )
        return {"nodes": nodes, "edges": edges}

    y_cursor = 0
    for gi, group in enumerate(active):
        gname = group["name"]
        gcolor = group.get("color") or "6"
        items = group["items"]
        gid = f"group-{gi}"
        nodes.append(
            _node(
                gid,
                f"### {gname}\n{len(items)} sessions",
                x=0,
                y=y_cursor,
                width=GROUP_W,
                height=GROUP_H,
                color=gcolor,
            )
        )
        edges.append(
            {
                "id": f"e-root-{gi}",
                "fromNode": root_id,
                "fromSide": "bottom",
                "toNode": gid,
                "toSide": "top",
                "label": "",
                "color": gcolor,
            }
        )
        for ii, item in enumerate(items):
            title = item.get("title") or "untitled"
            date = item.get("date") or ""
            body = f"**{title}**"
            if date and date not in title:
                body += f"\n{date}"
            nid = f"leaf-{gi}-{ii}"
            nodes.append(
                _node(
                    nid,
                    body,
                    x=GROUP_W + GROUP_H_GAP,
                    y=y_cursor + ii * (LEAF_H + LEAF_V_GAP),
                    width=LEAF_W,
                    height=LEAF_H,
                    color=gcolor,
                )
            )
            edges.append(_edge(f"e-{gi}-{ii}", gid, nid, color=gcolor))
        block_h = max(GROUP_H, len(items) * LEAF_H + max(0, len(items) - 1) * LEAF_V_GAP)
        y_cursor += block_h + GROUP_V_GAP
    return {"nodes": nodes, "edges": edges}


def export_lilith() -> Path:
    rows = discover_lilith()
    project_order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        proj = r["project"]
        if proj not in buckets:
            buckets[proj] = []
            project_order.append(proj)
        buckets[proj].append({"title": r["title"], "date": r.get("date") or ""})
    groups = [
        {
            "name": name,
            "color": _PROJECT_COLORS[i % len(_PROJECT_COLORS)],
            "items": buckets[name],
        }
        for i, name in enumerate(project_order)
    ]
    canvas = build_grouped_canvas(
        root_title="All Lilith Sessions",
        root_subtitle="Grok/Chat · Second-Brain/Lilith/Projects folder map",
        groups=groups,
        root_color="5",
    )
    OUT_LILITH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(canvas, ensure_ascii=False, indent=2) + "\n"
    if OUT_LILITH.is_file():
        try:
            same = OUT_LILITH.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        if same:
            print(f"OK unchanged path={OUT_LILITH}")
            print(f"SUMMARY kind=lilith_canvas added=0 skipped=1 vault_notes={len(rows)}")
            print(f"sessions={len(rows)} groups={len(groups)} nodes={len(canvas['nodes'])}")
            return OUT_LILITH
    OUT_LILITH.write_text(text, encoding="utf-8")
    print(f"OK path={OUT_LILITH}")
    print(f"SUMMARY kind=lilith_canvas added=1 skipped=0 vault_notes={len(rows)}")
    print(f"sessions={len(rows)} groups={len(groups)} nodes={len(canvas['nodes'])}")
    return OUT_LILITH


def export_vesper_via_forge() -> bool:
    """Call running VesperForge if available."""
    url = f"{FORGE_URL.rstrip('/')}/api/canvas/all-vesper"
    data = json.dumps({"save": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"VesperForge unavailable ({exc}). Start with: vesper-forge", file=sys.stderr)
        print("Or open VesperForge UI → All Vesper Canvas", file=sys.stderr)
        return False
    path = body.get("path") or str(OUT_VESPER)
    n = body.get("session_count", 0)
    print(f"OK path={path}")
    print(f"SUMMARY kind=vesper_canvas added=1 skipped=0 vault_notes={n}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Export all-sessions Obsidian Canvas")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--lilith", action="store_true", help="Second-Brain/Lilith/Projects overview")
    g.add_argument("--vesper", action="store_true", help="All Vesper via VesperForge API")
    g.add_argument("--all", action="store_true", help="Both (lilith local + vesper via forge)")
    args = ap.parse_args()

    ok = True
    if args.lilith or args.all:
        export_lilith()
    if args.vesper or args.all:
        if not export_vesper_via_forge():
            ok = False if args.vesper else ok  # --all: lilith still ok
            if args.vesper:
                return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
