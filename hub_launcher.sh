#!/usr/bin/env bash
# Terminal / zenity fallback. X + Chat only. Zero background cost.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
export PYTHONPATH="${HERE}${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"

pick="${1:-}"
if [[ -z "$pick" ]] && command -v zenity >/dev/null 2>&1; then
  pick=$(zenity --list --title="Nasty Ingest Pit" \
    --text="X / Chat / GitHub (no always-on watch)" \
    --column="ID" --column="Action" --hide-column=1 \
    all "All (X + Chat + GitHub)" \
    x "Full X bookmarks (X↔local)" \
    xunbook "X unbookmark suppressed only" \
    chat "Import Chat sessions" \
    gh "GitHub Repos + Stars" \
    ghrepos "GitHub Repos only" \
    ghstars "GitHub Stars only" \
    xdrop "X _drop" \
    chatdrop "Chat _drop" \
    2>/dev/null || true)
fi

case "${pick:-all}" in
  all)
    "$PY" hub_auto_sync.py
    ;;
  x|X)
    "$PY" x_sync_bookmarks.py
    ;;
  xunbook|unbookmark)
    "$PY" x_sync_bookmarks.py --unbookmark-only
    ;;
  chat|Chat)
    "$PY" chat_sync_sessions.py
    ;;
  gh|github)
    "$PY" github_sync.py
    ;;
  ghrepos)
    "$PY" github_sync.py --repos
    ;;
  ghstars)
    "$PY" github_sync.py --stars
    ;;
  xdrop)
    "$PY" x_bookmark.py --process-drop
    ;;
  chatdrop)
    "$PY" chat_save.py --process-drop
    ;;
  *)
    echo "Usage: $0 [all|x|xunbook|chat|gh|ghrepos|ghstars|xdrop|chatdrop]"
    exit 2
    ;;
esac
