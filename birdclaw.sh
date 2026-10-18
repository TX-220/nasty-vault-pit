#!/usr/bin/env bash
# birdclaw.sh — fetch X bookmarks via bird + Firefox cookies (free/local).
# Named for the birdclaw workflow; uses `bird bookmarks` under the hood.
#
# Usage:
#   birdclaw.sh                 # all bookmarks → cache JSON
#   birdclaw.sh --limit 20
#   birdclaw.sh --max-pages 5
#   birdclaw.sh --out /path.json
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/viora"
mkdir -p "$CACHE_DIR"
OUT="${BIRDCLAW_OUT:-$CACHE_DIR/x-bookmarks-latest.json}"
LIMIT=""
MAX_PAGES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --max-pages) MAX_PAGES="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown: $1" >&2; exit 2 ;;
  esac
done

if ! command -v bird >/dev/null 2>&1; then
  echo "ERROR: bird CLI not in PATH" >&2
  exit 1
fi

# Fetch only → JSON cache (import separately via x_sync_bookmarks.py)
python3 - <<PY
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, "$HERE")
from bird_cookies import bird_env

out = Path("$OUT")
cmd = ["bird", "bookmarks", "--json"]
limit = "$LIMIT"
max_pages = "$MAX_PAGES"
if limit:
    cmd += ["-n", limit]
else:
    cmd.append("--all")
if max_pages:
    cmd += ["--max-pages", max_pages]
print("Fetching:", " ".join(cmd), file=sys.stderr)
env = bird_env()
p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
if p.returncode != 0:
    print(p.stderr or p.stdout, file=sys.stderr)
    sys.exit(p.returncode)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(p.stdout, encoding="utf-8")
data = json.loads(p.stdout)
n = len(data) if isinstance(data, list) else "?"
print(f"OK: {n} bookmarks → {out}", file=sys.stderr)
print(out)
PY
