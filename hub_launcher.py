#!/usr/bin/env python3
"""Nasty Ingest Pit — one-click vault sync launcher (no daemons, no background cost).

Layout (always visible at bottom):
  - vault counts + last-run summary
  - log
  - footer buttons
Buttons sit above in a scrollable strip so they never hide the log.

Visual tone: cosmic / lewd night ops (aligned with Nasty Command Pit / Session Pit).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from vault_lib import LILITH_FOLDER, VAULT as VL_VAULT  # noqa: E402

VAULT = Path(VL_VAULT)
PYTHON = sys.executable
APP_NAME = "Nasty Ingest Pit"
ICON_PATH = HERE / "icons" / "nasty-ingest-pit.png"

# Cosmic / lewd night palette (Nasty Command Pit aligned)
C = {
    "bg": "#0d0514",
    "bg_deep": "#08030e",
    "bg_panel": "#12081c",
    "bg_hover": "#1f0f2e",
    "bg_active": "#2a143c",
    "border": "#3a1848",
    "text": "#f5e8f2",
    "text_muted": "#a88aaa",
    "accent": "#ff2d95",
    "accent_hot": "#e91e8c",
    "sub": "#c4a0ff",
    "success": "#5dffb0",
    "warn": "#ffb3d9",
    "scroll_thumb": "#ff2d95",
    "scroll_thumb_hot": "#ff8ac4",
    "scroll_track": "#3a1848",
}


def style_vertical_scrollbar(sb: tk.Scrollbar) -> None:
    """Hot-pink thumb on a visible track. Hover only brightens it."""
    sb.configure(
        bg=C["scroll_thumb"],
        activebackground=C["scroll_thumb_hot"],
        troughcolor=C["scroll_track"],
        highlightthickness=0,
        borderwidth=0,
        width=16,
        relief="flat",
    )


def render_title_glow(text: str) -> tk.PhotoImage:
    """Bloom the title: purple haze, magenta mid-glow, hot-pink core."""
    import base64
    import io

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    size = 30
    font = None
    for path in (
        "/usr/share/fonts/truetype/lato/Lato-Black.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(path).is_file():
            font = ImageFont.truetype(path, size)
            break
    if font is None:
        font = ImageFont.load_default()

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 42
    width, height = tw + pad * 2, th + pad * 2
    origin = (pad - bbox[0], pad - bbox[1])

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text(origin, text, font=font, fill=255)

    def glow(radius: float, color: tuple[int, int, int], scale: float) -> Image.Image:
        alpha = mask.filter(ImageFilter.GaussianBlur(radius))
        alpha = alpha.point(lambda p: min(255, int(p * scale)))
        layer = Image.new("RGBA", (width, height), (*color, 0))
        layer.putalpha(alpha)
        return layer

    bg = tuple(int(C["bg"][i : i + 2], 16) for i in (1, 3, 5))
    canvas = Image.new("RGBA", (width, height), (*bg, 255))
    for radius, color, scale in (
        (26, (120, 40, 210), 0.70),
        (16, (196, 40, 180), 0.75),
        (8, (255, 45, 149), 0.90),
        (3, (255, 60, 130), 0.85),
    ):
        canvas = Image.alpha_composite(canvas, glow(radius, color, scale))

    letters = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(letters).text(origin, text, font=font, fill=(255, 45, 112, 255))
    canvas = Image.alpha_composite(canvas, letters)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))


ACTIONS = [
    # Full X = pull X→local + push local deletes→X unbookmark (bidirectional)
    ("★ Full X Bookmarks Sync (X↔local)", ["x_sync_bookmarks.py"], "#e91e8c"),
    ("X: Unbookmark suppressed only (local→X)", ["x_sync_bookmarks.py", "--unbookmark-only"], "#9b1b5a"),
    ("★ Chat: Lilith grok.com + local sessions", ["chat_sync_sessions.py"], "#7b4dff"),
    ("★ Grok Project Files (attachments)", ["grok_project_files_sync.py"], "#a78bfa"),
    ("★ GitHub: Repos + Stars", ["github_sync.py"], "#5c3d7a"),
    ("GitHub: Repos only", ["github_sync.py", "--repos"], "#4a3060"),
    ("GitHub: Stars only", ["github_sync.py", "--stars"], "#6a4588"),
    ("All (X + Chat + Files + GitHub)", ["hub_auto_sync.py"], "#ff2d95"),
    ("X _drop only", ["x_bookmark.py", "--process-drop"], "#6b3fa0"),
    ("Chat _drop only", ["chat_save.py", "--process-drop"], "#8b5cf6"),
    ("Both _drop (X + Chat)", ["hub_process_all.py"], "#c42d7a"),
    # Simple overview Canvas (no AI) — Lilith/Projects folder map
    ("Canvas: All Lilith Sessions Overview", ["export_all_sessions_canvas.py", "--lilith"], "#9d4edd"),
]

SUMMARY_RE = re.compile(
    r"SUMMARY kind=(?P<kind>\w+)\s+added=(?P<added>\d+)\s+skipped=(?P<skipped>\d+)"
    r"(?:\s+failed=(?P<failed>\d+))?"
    r"(?:\s+vault_notes=(?P<vault>\d+))?"
    r"(?:\s+candidates=(?P<candidates>\d+))?"
    r"(?:\s+fetched=(?P<fetched>\d+))?"
    r"(?:\s+repos=(?P<repos>\d+))?"
    r"(?:\s+stars=(?P<stars>\d+))?",
)
DONE_RE = re.compile(
    r"DONE(?: kind=(?P<kind>\w+))? added=(?P<added>\d+) skipped(?:_seen)?=(?P<skipped>\d+)"
    r"(?:\s+failed=(?P<failed>\d+))?"
)


def vault_counts() -> dict[str, int]:
    def md_count(folder: str) -> int:
        p = VAULT / folder
        if not p.is_dir():
            return 0
        return sum(1 for f in p.rglob("*.md") if f.name != "README.md" and "_drop" not in f.parts)

    def github_lists() -> int:
        p = VAULT / "GitHub"
        if not p.is_dir():
            return 0
        return sum(1 for name in ("Repositories.md", "Stars.md") if (p / name).is_file())

    return {
        "x": md_count("X_Bookmarks"),
        "chat": md_count(LILITH_FOLDER),
        "github": github_lists(),
    }


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("640x720")
        self.minsize(560, 640)
        self.configure(bg=C["bg"])
        self._busy = False
        self._icon_img = None
        self._set_icon()

        # ============================================================
        # Pack BOTTOM first so log + counts never disappear under buttons
        # ============================================================

        # --- footer (bottom-most) ---
        foot = tk.Frame(self, bg=C["bg"])
        foot.pack(side="bottom", fill="x", padx=12, pady=(4, 10))
        for text, cmd, side, padx in (
            ("Refresh counts", self.refresh_counts, "left", 0),
            ("Open folder", self.open_vault, "left", 6),
            ("Clear log", self.clear_log, "left", 0),
            ("Close", self.destroy, "right", 0),
        ):
            tk.Button(
                foot,
                text=text,
                command=cmd,
                bg=C["bg_active"],
                fg=C["text"],
                activebackground=C["accent_hot"],
                activeforeground=C["text"],
                relief="flat",
                padx=8,
                pady=4,
                cursor="hand2",
            ).pack(side=side, padx=padx)

        # --- counts bar (always above footer) ---
        self.counts_var = tk.StringVar()
        self.last_var = tk.StringVar(value="Last run: none yet")
        counts_frame = tk.Frame(self, bg=C["bg_panel"], padx=10, pady=8)
        counts_frame.pack(side="bottom", fill="x", padx=12, pady=(0, 4))
        tk.Label(
            counts_frame,
            text="Counts",
            font=("Sans", 8),
            fg=C["text_muted"],
            bg=C["bg_panel"],
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            counts_frame,
            textvariable=self.counts_var,
            font=("Sans", 11, "bold"),
            fg=C["text"],
            bg=C["bg_panel"],
            anchor="w",
            justify="left",
        ).pack(fill="x")
        tk.Label(
            counts_frame,
            textvariable=self.last_var,
            font=("Monospace", 9),
            fg=C["success"],
            bg=C["bg_panel"],
            anchor="w",
            justify="left",
            wraplength=600,
        ).pack(fill="x", pady=(4, 0))
        self.refresh_counts()

        # --- log (fills remaining space above counts) ---
        log_wrap = tk.Frame(self, bg=C["bg"])
        log_wrap.pack(side="bottom", fill="both", expand=True, padx=12, pady=(4, 6))
        tk.Label(
            log_wrap,
            text="Log",
            font=("Sans", 9),
            fg=C["text_muted"],
            bg=C["bg"],
            anchor="w",
        ).pack(fill="x")
        self.log = scrolledtext.ScrolledText(
            log_wrap,
            height=12,
            font=("Monospace", 9),
            bg=C["bg_deep"],
            fg=C["text"],
            insertbackground=C["accent"],
            selectbackground=C["bg_active"],
            selectforeground=C["text"],
            relief="flat",
        )
        self.log.pack(fill="both", expand=True, pady=(2, 0))
        style_vertical_scrollbar(self.log.vbar)
        self._append(
            f"{APP_NAME}\nVault: {VAULT}\nScripts: {HERE}\n"
            "Ready. The Counts bar and this log stay visible.\n"
        )

        # ============================================================
        # Top: header + scrollable buttons
        # ============================================================
        top = tk.Frame(self, bg=C["bg"])
        top.pack(side="top", fill="x")

        try:
            self._title_img = render_title_glow(APP_NAME)
            tk.Label(
                top,
                image=self._title_img,
                bg=C["bg"],
                borderwidth=0,
                highlightthickness=0,
            ).pack(pady=(8, 0))
        except Exception:
            tk.Label(
                top,
                text=APP_NAME,
                font=("Sans", 15, "bold"),
                fg=C["accent"],
                bg=C["bg"],
            ).pack(pady=(12, 2))
        tk.Label(
            top,
            text="X / Chat / GitHub  ·  cosmic vault pit  ·  ★ = auto-fetch  ·  no always-on watch",
            font=("Sans", 9),
            fg=C["sub"],
            bg=C["bg"],
        ).pack(pady=(0, 8))

        # Scrollable button strip (fixed height so log stays visible)
        btn_h = min(320, 36 * len(ACTIONS) + 20)
        btn_canvas = tk.Canvas(
            top,
            height=btn_h,
            bg=C["bg"],
            highlightthickness=0,
            borderwidth=0,
        )
        btn_scroll = tk.Scrollbar(
            top,
            orient="vertical",
            command=btn_canvas.yview,
        )
        style_vertical_scrollbar(btn_scroll)
        btn_inner = tk.Frame(btn_canvas, bg=C["bg"])
        btn_inner.bind(
            "<Configure>",
            lambda e: btn_canvas.configure(scrollregion=btn_canvas.bbox("all")),
        )
        btn_win = btn_canvas.create_window((0, 0), window=btn_inner, anchor="nw")
        btn_canvas.configure(yscrollcommand=btn_scroll.set)

        def _on_canvas_configure(event: tk.Event) -> None:
            btn_canvas.itemconfigure(btn_win, width=event.width)

        btn_canvas.bind("<Configure>", _on_canvas_configure)

        btn_scroll.pack(side="right", fill="y", padx=(0, 12))
        btn_canvas.pack(side="left", fill="x", expand=True, padx=(12, 0))

        for label, args, color in ACTIONS:
            b = tk.Button(
                btn_inner,
                text=label,
                font=("Sans", 10),
                fg=C["text"],
                bg=color,
                activebackground=C["accent"],
                activeforeground=C["text"],
                relief="flat",
                padx=6,
                pady=5,
                cursor="hand2",
                command=lambda a=args, l=label: self.run_action(l, a),
            )
            b.pack(fill="x", pady=2, padx=4)

        def _wheel(event: tk.Event) -> None:
            # Linux: Button-4/5; Windows/mac: delta
            if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
                btn_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
                btn_canvas.yview_scroll(1, "units")

        btn_canvas.bind_all("<MouseWheel>", _wheel)
        btn_canvas.bind_all("<Button-4>", _wheel)
        btn_canvas.bind_all("<Button-5>", _wheel)

    def _set_icon(self) -> None:
        """Window icon (PNG). Fail soft if missing or unsupported."""
        if not ICON_PATH.is_file():
            return
        try:
            # PhotoImage supports PNG on modern Tk
            self._icon_img = tk.PhotoImage(file=str(ICON_PATH))
            self.iconphoto(True, self._icon_img)
        except tk.TclError:
            try:
                from PIL import Image, ImageTk  # type: ignore

                pil = Image.open(ICON_PATH).convert("RGBA").resize((64, 64))
                self._icon_img = ImageTk.PhotoImage(pil)
                self.iconphoto(True, self._icon_img)
            except Exception:
                pass

    def refresh_counts(self) -> None:
        c = vault_counts()
        self.counts_var.set(
            f"In vault  X: {c['x']}   Chat: {c['chat']}   "
            f"GitHub lists: {c['github']}/2"
        )

    def clear_log(self) -> None:
        self.log.delete("1.0", "end")
        self._append("Log cleared.\n")

    def _append(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def open_vault(self) -> None:
        subprocess.Popen(["xdg-open", str(VAULT)], start_new_session=True)

    def run_action(self, label: str, args: list[str]) -> None:
        if self._busy:
            messagebox.showinfo("Busy", "Another job is still running. Wait until it finishes.")
            return
        script = HERE / args[0]
        if not script.is_file():
            messagebox.showerror("Error", f"Not found:\n{script}")
            return
        self._busy = True
        self.last_var.set(f"Running: {label} …")
        self._append(f"\n--- {label} ---\n$ {' '.join([PYTHON, str(script), *args[1:]])}\n")
        threading.Thread(target=self._worker, args=(label, args), daemon=True).start()

    def _worker(self, label: str, args: list[str]) -> None:
        cmd = [PYTHON, str(HERE / args[0]), *args[1:]]
        timeout = 900 if any(
            x in args[0] for x in ("x_sync", "hub_auto", "github_sync", "grok_project", "chat_sync")
        ) else 300
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(HERE),
                capture_output=True,
                text=True,
                env={**os.environ, "OBSIDIAN_VAULT": str(VAULT), "PYTHONPATH": str(HERE)},
                timeout=timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if not out.strip():
                out = f"(exit {proc.returncode}, no output)\n"
            self.after(0, lambda: self._done(label, proc.returncode, out))
        except subprocess.TimeoutExpired:
            self.after(0, lambda: self._done(label, -1, f"TIMEOUT ({timeout}s)\n"))
        except Exception as e:
            self.after(0, lambda: self._done(label, -1, f"{type(e).__name__}: {e}\n"))

    def _parse_summaries(self, out: str) -> list[str]:
        lines: list[str] = []
        for m in SUMMARY_RE.finditer(out):
            d = m.groupdict()
            kind = d.get("kind") or "?"
            parts = [f"{kind}: +{d['added']} (skip {d['skipped']})"]
            if d.get("failed") and d["failed"] != "0":
                parts.append(f"fail {d['failed']}")
            if d.get("fetched"):
                parts.append(f"fetched {d['fetched']}")
            if d.get("candidates"):
                parts.append(f"candidates {d['candidates']}")
            if d.get("repos"):
                parts.append(f"repos {d['repos']}")
            if d.get("stars"):
                parts.append(f"stars {d['stars']}")
            if d.get("vault"):
                parts.append(f"in vault {d['vault']}")
            lines.append(" · ".join(parts))
        if not lines:
            for m in DONE_RE.finditer(out):
                d = m.groupdict()
                kind = d.get("kind") or "run"
                lines.append(f"{kind}: +{d['added']} (skip {d['skipped']})")
        return lines

    def _done(self, label: str, code: int, out: str) -> None:
        self._busy = False
        lines = out.splitlines()
        if len(lines) > 80:
            head = "\n".join(lines[:15])
            tail = "\n".join(lines[-40:])
            self._append(f"{head}\n… ({len(lines) - 55} lines omitted) …\n{tail}\n")
        else:
            self._append(out if out.endswith("\n") else out + "\n")

        summaries = self._parse_summaries(out)
        self.refresh_counts()
        status = "OK" if code == 0 else f"failed (code {code})"
        if summaries:
            summary_text = "  |  ".join(summaries)
            self.last_var.set(f"Last [{status}] {label}\n{summary_text}")
            self._append(f"→ counts: {summary_text}\n")
        else:
            self.last_var.set(f"Last [{status}] {label}")
        self._append(f"→ {label}: {status}\n")
        self._append(f"→ Vault total: {self.counts_var.get()}\n")
        if code != 0:
            messagebox.showwarning(
                "Finished (with errors)",
                f"{label}\nExit code: {code}\nCheck the Counts bar and log below.",
            )


def main() -> None:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("No display — use scripts from terminal.", file=sys.stderr)
        sys.exit(2)
    app = Launcher()
    app.mainloop()


if __name__ == "__main__":
    main()
