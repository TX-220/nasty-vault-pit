# Nasty Vault Pit

Formerly the vault hub. Same one-click sync — **cosmic purple / hot magenta** night-ops skin.

On-demand launcher for a local Obsidian vault: X bookmarks, Grok session notes, and GitHub lists. Press a button, it runs, it stops. No always-on watcher.


## Screenshots

![Nasty Vault Pit](screenshots/nasty-vault-pit-001.png)

---

## Features

Buttons in the pit:

- **Full X Bookmarks Sync (X↔local)** — pull bookmarks into the vault, and push local deletes back to X
- **Unbookmark suppressed only (local→X)** — remove on X what you already dropped locally
- **Chat: Import Grok Sessions** — session notes into the vault
- **Grok Project Files (attachments)** — local copies of project files into project folders
- **GitHub: Repos + Stars** — both lists
- **GitHub: Repos only** / **GitHub: Stars only**
- **All (X + Chat + Files + GitHub)** — one pass of the four
- **X _drop** / **Chat _drop** / **Both _drop** — ingest files you dropped in by hand
- **Canvas: All Lilith Sessions Overview** — a folder map, no model call

The log and the vault counts stay on screen. The title glows. The scrollbars stay hot pink without a hover.

---

## Requirements

| Item | Notes |
|------|--------|
| OS | Linux |
| Python | 3 |
| X sync | Firefox logged into x.com, and `bird` on `PATH` (cookies are read at run time, not stored here) |
| GitHub sync | `gh` authenticated (`gh auth status`) |
| Optional | Obsidian Local REST API. If it is down, notes are written to the filesystem |
| Optional | Pillow, for the glowing title. Without it the title is a plain label |

---

## Quick start

```bash
git clone git@github.com:TX-220/nasty-vault-pit.git
cd nasty-vault-pit

export OBSIDIAN_VAULT=/path/to/your/vault
python3 hub_launcher.py
```

If this folder lives at `<vault>/Scripts`, `OBSIDIAN_VAULT` can be omitted. The default vault is the parent directory.

A terminal menu is also there: `./hub_launcher.sh`.

---

## Local config

These stay on your machine. They are not in git.

- `config/grok_project_files.json` — copy from `config/grok_project_files.example.json` and set your own paths.
- `hub_launcher.desktop.example` — desktop entry. Replace `/PATH/TO/nasty-vault-pit` before installing it.

---

## Chat: full session mirror (opt-in)

Default `chat_sync_sessions.py` still writes highlight notes only. To also keep the raw sessions:

```bash
python3 chat_sync_sessions.py --full-mirror --dry-run      # preview, writes nothing
python3 chat_sync_sessions.py --full-mirror                # copy
python3 chat_sync_sessions.py --full-mirror --mirror-delete  # also prune removed files
```

Each session directory under `~/.grok/sessions` (`chat_history.jsonl`, `summary.json`, and anything else in it) is copied
to `Lilith/Projects/<project>/Sessions/<original relative path>/`, using the same project classification as the highlights.
Unchanged files are skipped, symlinks are not followed, and nothing is written outside `Lilith/Projects/`.
`--mirror-delete` removes only files the mirror itself copied earlier (tracked in `.mirror_manifest.json`); without it, nothing is deleted.

**Secret redaction is always on in mirror mode** (rules version `2026-10-31.1`, see `redact_lib.py`). Every file is redacted in memory before it is written
(atomic write, mtime kept): AWS / GitHub / OpenAI / Anthropic / xAI / Slack / Google / Stripe keys, JWTs, PEM private keys, `Bearer` tokens,
URL credentials, and `password|secret|token|api_key|auth|cookie|session_id…` key/value pairs become `[REDACTED:<type>]`.
Binary or non-UTF-8 files and sensitive names (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `*.p12`, `*.pfx`) are never copied.
If redaction fails for a file, it is skipped (fail closed). Output shows only paths and counts per type; add `--redaction-report` for per-file counts.
Regex redaction is best effort: unknown secret formats can still slip through.

---

## License

MIT — see [LICENSE](./LICENSE).

Copyright (c) 2026 TX-220 with assistance from [Grok (xAI)](https://x.ai).
