# Nasty Vault Pit

Private. This repository is not public.

On-demand launcher for a local Obsidian vault: X bookmarks, Grok session notes, and GitHub repo lists. Buttons run when you press them. There is no always-on watcher in this app.

## Run

```bash
export OBSIDIAN_VAULT=/path/to/your/vault
python3 hub_launcher.py
```

If this folder lives at `<vault>/Scripts`, `OBSIDIAN_VAULT` can be omitted. The default vault is the parent directory.

X sync needs Firefox logged into x.com. GitHub sync needs `gh auth status`. Obsidian Local REST is optional; notes fall back to the filesystem.

## Local config (not in git)

- `config/grok_project_files.json` — copy from `config/grok_project_files.example.json` and set your own paths.
- `hub_launcher.desktop.example` — desktop entry. Replace `/PATH/TO/nasty-vault-pit` before installing it.
