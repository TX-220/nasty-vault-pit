#!/usr/bin/env python3
"""One-shot: X + Chat + Grok project files + GitHub."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script: str, extra: list[str] | None = None) -> None:
    cmd = [sys.executable, str(HERE / script), *(extra or [])]
    print(f"=== {script} ===", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(HERE)
    r = subprocess.run(cmd, cwd=str(HERE), env=env)
    if r.returncode != 0:
        print(f"FAILED {script} code={r.returncode}", flush=True)
        sys.exit(r.returncode)


def main() -> None:
    run("x_sync_bookmarks.py")
    run("chat_sync_sessions.py", ["--limit", "15"])
    run("grok_project_files_sync.py")
    run("github_sync.py")
    print("ALL AUTO SYNC DONE (X + Chat + Grok files + GitHub)", flush=True)
    print("SUMMARY kind=all added=0 skipped=0  # see per-kind SUMMARY lines above", flush=True)


if __name__ == "__main__":
    main()
