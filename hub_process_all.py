#!/usr/bin/env python3
"""Process X + Chat _drop inboxes (Imagine deferred)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script: str) -> None:
    print(f"=== {script} ===")
    r = subprocess.run([sys.executable, str(HERE / script), "--process-drop"])
    if r.returncode != 0:
        sys.exit(r.returncode)


def main() -> None:
    run("x_bookmark.py")
    run("chat_save.py")
    print("ALL DROP PROCESSING DONE (X + Chat)")


if __name__ == "__main__":
    main()
