#!/usr/bin/env python3
"""Editable classification for Chat projects and X bookmark categories."""

from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    return text.casefold()


def score_keywords(text: str, keywords: list[str]) -> int:
    t = _norm(text)
    score = 0
    for kw in keywords:
        k = _norm(kw)
        if not k:
            continue
        if k in t:
            # longer keywords weigh a bit more
            score += 1 + min(3, len(k) // 8)
    return score


def classify_chat_project(
    *,
    title: str = "",
    summary: str = "",
    cwd: str = "",
    body_sample: str = "",
) -> tuple[str, int]:
    """Return (project_name, score)."""
    cfg = _load("chat_projects.json")
    default = cfg.get("default_project") or "その他"
    projects = cfg.get("projects") or []
    blob = "\n".join([title, summary, body_sample])
    cwd_l = _norm(cwd or "")

    best_name, best_score = default, 0
    for i, proj in enumerate(projects):
        name = proj.get("name") or default
        kw = score_keywords(blob, proj.get("keywords") or [])
        cwd_hit = 0
        for frag in proj.get("cwd_contains") or []:
            if _norm(frag) in cwd_l:
                cwd_hit += 1
        # Keywords dominate. cwd only boosts when keywords already match,
        # so a coding cwd (vesper-hub) won't swallow TradeBot/意識 etc.
        sc = kw * 10
        if kw > 0:
            sc += cwd_hit * 3
        elif cwd_hit and name in ("Web-Dev",):
            sc += cwd_hit  # weak fallback only for Web-Dev
        sc += max(0, 5 - i)  # tie-break: list order
        if sc > best_score:
            best_score, best_name = sc, name

    if best_score <= 5:  # only order bias, no real hit
        return default, 0
    return best_name, best_score


def classify_x_category(*, text: str = "", title: str = "") -> tuple[str, int]:
    """Content-only classification (ignore author). Return (category, score)."""
    cfg = _load("x_categories.json")
    default = cfg.get("default_category") or "その他"
    cats = cfg.get("categories") or []
    blob = f"{title}\n{text}"

    best_name, best_score = default, 0
    for i, cat in enumerate(cats):
        name = cat.get("name") or default
        sc = score_keywords(blob, cat.get("keywords") or [])
        sc = sc * 10 + max(0, 5 - i)
        if sc > best_score:
            best_score, best_name = sc, name

    if best_score <= 5:
        return default, 0
    return best_name, best_score


def safe_dir_name(name: str) -> str:
    """Filesystem-safe single path segment."""
    bad = '<>:"/\\|?*'
    s = "".join("_" if c in bad else c for c in name).strip() or "その他"
    s = re.sub(r"\s+", " ", s)
    return s[:80]
