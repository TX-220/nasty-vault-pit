#!/usr/bin/env python3
"""Extract X (Twitter) auth cookies from local Firefox (stdlib only)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def firefox_profile_dirs() -> list[Path]:
    roots = [
        Path.home() / "snap/firefox/common/.mozilla/firefox",
        Path.home() / ".mozilla/firefox",
        Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ]
    out: list[Path] = []
    for r in roots:
        if not r.is_dir():
            continue
        ini = r / "profiles.ini"
        if ini.is_file():
            # prefer Default=1 path
            default_path = None
            cur_path = None
            for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("Path="):
                    cur_path = line.split("=", 1)[1].strip()
                if line.strip() == "Default=1" and cur_path:
                    default_path = cur_path
            if default_path:
                p = r / default_path if not Path(default_path).is_absolute() else Path(default_path)
                if (p / "cookies.sqlite").is_file():
                    out.append(p)
        for p in sorted(r.glob("*")):
            if p.is_dir() and (p / "cookies.sqlite").is_file() and p not in out:
                out.append(p)
    return out


def _copy_db(src: Path) -> Path:
    fd, name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    dst = Path(name)
    shutil.copy2(src, dst)
    for suf in ("-wal", "-shm"):
        side = Path(str(src) + suf)
        if side.is_file():
            shutil.copy2(side, Path(str(dst) + suf))
    return dst


def extract_x_cookies() -> tuple[str, str]:
    """Return (auth_token, ct0). Raises RuntimeError if missing."""
    last_err = "no Firefox profile with cookies.sqlite"
    for prof in firefox_profile_dirs():
        src = prof / "cookies.sqlite"
        tmp = _copy_db(src)
        try:
            con = sqlite3.connect(str(tmp))
            cur = con.cursor()
            auth = cur.execute(
                """
                SELECT value FROM moz_cookies
                WHERE name='auth_token' AND (host LIKE '%x.com%' OR host LIKE '%twitter.com%')
                ORDER BY lastAccessed DESC LIMIT 1
                """
            ).fetchone()
            ct0 = cur.execute(
                """
                SELECT value FROM moz_cookies
                WHERE name='ct0' AND (host LIKE '%x.com%' OR host LIKE '%twitter.com%')
                ORDER BY lastAccessed DESC LIMIT 1
                """
            ).fetchone()
            con.close()
            if auth and ct0 and auth[0] and ct0[0]:
                return auth[0], ct0[0]
            last_err = f"no auth_token/ct0 in {prof}"
        except Exception as e:
            last_err = f"{prof}: {e}"
        finally:
            tmp.unlink(missing_ok=True)
            Path(str(tmp) + "-wal").unlink(missing_ok=True)
            Path(str(tmp) + "-shm").unlink(missing_ok=True)
    raise RuntimeError(
        f"X cookies not found ({last_err}). "
        "Log into x.com in Firefox, then retry."
    )


def bird_env() -> dict[str, str]:
    auth, ct0 = extract_x_cookies()
    env = os.environ.copy()
    env["AUTH_TOKEN"] = auth
    env["CT0"] = ct0
    return env
