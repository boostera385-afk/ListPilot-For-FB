import os

import customtkinter as ctk

from core.config import get_setting, set_setting
from gui import theme
from gui.guide_content import SECTIONS, auto_done, progress
from gui.widgets import Card, Chip

GUIDE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "docs", "TESTING_GUIDE.md")


class GuideView(ctk.CTkFrame):
    """Step-by-step setup and first-test guide for new users, with tick boxes."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.vars = {}
        self.chips = {}

        ctk.CTkLabel(self, text="Getting Started", font=theme.font(24, "bold"), text_color=theme.TEXT,
                     anchor="w").grid(row=0, column=0, padx=theme.PAD, pady=(theme.PAD, 4), sticky="w")

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, padx=theme.PAD, pady=(0, 8), sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self.summary = ctk.CTkLabel(top, text="", font=theme.font(13), text_color=theme.TEXT_MUTED)
        self.summary.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.bar = ctk.CTkProgressBar(top, height=8, corner_radius=4, progress_color=theme.GREEN,
                                      fg_color=theme.SURFACE_ALT)
        self.bar.grid(row=0, column=1, sticky="ew")
        self.bar.set(0)
        ctk.CTkButton(top, text="Open full guide", width=130, height=30, fg_color=theme.NEUTRAL_BTN,
                      hover_color=theme.NEUTRAL_BTN_HOVER, text_color=theme.TEXT,
                      command=self.open_full_guide).grid(row=0, column=2, padx=(12, 0))

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=2, column=0, padx=theme.PAD - 6, pady=(0, theme.PAD), sticky="nsew")
        self.scroll.grid_columnconfigure(0, weight=1)

        saved = set(get_setting("guide_done", []) or [])
        row = 0
        for section, steps in SECTIONS:
            ctk.CTkLabel(self.scroll, text=section, font=theme.font(15, "bold"), text_color=theme.ACCENT,
                         anchor="w").grid(row=row, column=0, sticky="w", padx=6, pady=(14, 6))
            row += 1
            for step in steps:
                self._build_step(row, step, step["id"] in saved)
                row += 1
        self.refresh()
        self._poll()

    def _build_step(self, row, step, checked):
        card = Card(self.scroll)
        card.grid(row=row, column=0, sticky="ew", padx=6, pady=5)
        card.grid_columnconfigure(1, weight=1)

        var = ctk.BooleanVar(value=checked)
        self.vars[step["id"]] = var
        ctk.CTkCheckBox(card, text="", variable=var, width=24, command=self._save).grid(
            row=0, column=0, rowspan=2, padx=(14, 6), pady=14, sticky="n")
        ctk.CTkLabel(card, text=step["title"], font=theme.font(14, "bold"), text_color=theme.TEXT,
                     anchor="w").grid(row=0, column=1, sticky="w", padx=4, pady=(12, 2))
        ctk.CTkLabel(card, text=step["body"], font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w",
                     justify="left", wraplength=620).grid(row=1, column=1, sticky="w", padx=4, pady=(0, 12))

        side = ctk.CTkFrame(card, fg_color="transparent")
        side.grid(row=0, column=2, rowspan=2, padx=14, pady=10, sticky="ne")
        if step.get("auto"):
            chip = Chip(side, "Not set", theme.GRAY)
            chip.grid(row=0, column=0, pady=(0, 6))
            self.chips[step["id"]] = (chip, step["auto"])
        if step.get("action"):
            label, key = step["action"]
            ctk.CTkButton(side, text=label, width=150, height=30, fg_color=theme.ACCENT,
                          hover_color=theme.ACCENT_HOVER, command=lambda k=key: self.go(k)).grid(row=1, column=0)

    def go(self, key):
        app = self.winfo_toplevel()
        if hasattr(app, "show"):
            app.show(key)

    def _save(self):
        set_setting("guide_done", [step_id for step_id, var in self.vars.items() if var.get()])
        self.refresh()

    def refresh(self):
        """Update the progress bar and the 'detected' chips from the current settings."""
        for chip, auto_key in self.chips.values():
            if auto_done(auto_key, get_setting):
                chip.set("Detected", theme.GREEN)
            else:
                chip.set("Not set", theme.GRAY)
        done_ids = {step_id for step_id, var in self.vars.items() if var.get()}
        finished, total = progress(done_ids, get_setting)
        self.summary.configure(text=f"{finished} of {total} steps done")
        self.bar.set(finished / total if total else 0)

    def _poll(self):
        """Settings are changed on another page, so re-read them every few seconds."""
        try:
            self.refresh()
            self.after(3000, self._poll)
        except Exception:
            pass

    def refresh_theme(self):
        self.refresh()

    def open_full_guide(self):
        try:
            os.startfile(GUIDE_FILE)  # Windows: opens in the default editor
        except Exception:
            pass
