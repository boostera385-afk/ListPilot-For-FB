"""Reusable ListPilot widgets: cards, chips, progress steps, colored log, table, toasts."""
import re

import customtkinter as ctk

from gui import theme
from gui import animations as anim


# --- Log line classification (pure, unit tested) --------------------------------

LOG_ERROR, LOG_WARN, LOG_OK, LOG_VIS, LOG_STEP, LOG_INFO = "error", "warn", "ok", "vis", "step", "info"

_LOG_RULES = (
    (LOG_ERROR, r"\berror\b|\bfailed\b|failure|exception|traceback|logged_out|checkpoint|could not start"),
    (LOG_WARN, r"\bwarning\b|\bnote:|partial|skipped|deferred|not_in_search|hidden"),
    (LOG_VIS, r"checker\]|visibility"),
    (LOG_OK, r"\bsuccess\b|completed|finished|\bok\b|saved|full scan|started"),
    (LOG_STEP, r"^\s*---"),
)


def classify_log_line(text):
    lowered = str(text or "").lower()
    for kind, pattern in _LOG_RULES:
        if re.search(pattern, lowered):
            return kind
    return LOG_INFO


_LOG_COLORS = {
    LOG_ERROR: theme.RED,
    LOG_WARN: theme.AMBER,
    LOG_OK: theme.GREEN,
    LOG_VIS: theme.PURPLE,
    LOG_STEP: theme.ACCENT,
    LOG_INFO: theme.TEXT,
}


# --- Building blocks -------------------------------------------------------------

class Card(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        kwargs.setdefault("fg_color", theme.SURFACE)
        kwargs.setdefault("corner_radius", theme.RADIUS)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", theme.BORDER)
        super().__init__(master, **kwargs)


class Chip(ctk.CTkLabel):
    """Rounded status pill, e.g. 'PK: ready'."""

    def __init__(self, master, text="", color=theme.GRAY, **kwargs):
        super().__init__(
            master, text=f"   ●  {text}   ", corner_radius=13, height=26,
            fg_color=theme.SURFACE_ALT, text_color=color, font=theme.font(12, "bold"), **kwargs
        )

    def set(self, text, color=theme.GRAY):
        self.configure(text=f"   ●  {text}   ", text_color=color)


class StatCard(Card):
    """Big animated number with a colored accent bar."""

    def __init__(self, master, title, color=theme.ACCENT, compact=False, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(self, width=4, height=10, corner_radius=2, fg_color=color).grid(
            row=0, column=0, rowspan=2, sticky="ns", padx=(12, 10), pady=14
        )
        self.value = ctk.CTkLabel(self, text="0", font=theme.font(22 if compact else 26, "bold"), text_color=theme.TEXT, anchor="w")
        self.value.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(8 if compact else 12, 0))
        ctk.CTkLabel(self, text=title, font=theme.font(11 if compact else 12), text_color=theme.TEXT_MUTED, anchor="w").grid(
            row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 8 if compact else 12)
        )

    def set(self, value):
        anim.count_up(self.value, value)


class StepProgress(ctk.CTkFrame):
    """'1  Owner scan    12 / 40' with an animated bar underneath."""

    def __init__(self, master, number, title, color=theme.ACCENT, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(1, weight=1)
        self.badge = ctk.CTkLabel(self, text=str(number), width=26, height=26, corner_radius=13,
                                  fg_color=theme.SURFACE_ALT, text_color=theme.TEXT_MUTED, font=theme.font(12, "bold"))
        self.badge.grid(row=0, column=0, padx=(0, 10))
        ctk.CTkLabel(self, text=title, font=theme.font(13, "bold"), text_color=theme.TEXT, anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        self.counter = ctk.CTkLabel(self, text="Waiting", font=theme.font(12), text_color=theme.TEXT_MUTED)
        self.counter.grid(row=0, column=2, sticky="e")
        self.bar = ctk.CTkProgressBar(self, height=8, corner_radius=4, progress_color=color, fg_color=theme.SURFACE_ALT)
        self.bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.bar.set(0)
        self._color = color
        self._number = str(number)

    def reset(self, text="Waiting"):
        self.bar.set(0)
        self.counter.configure(text=text)
        self.badge.configure(fg_color=theme.SURFACE_ALT, text_color=theme.TEXT_MUTED, text=self._number)
        self.bar.configure(progress_color=self._color)

    def update_progress(self, done, total, suffix=""):
        fraction = (done / total) if total else 0
        anim.progress_to(self.bar, fraction)
        self.counter.configure(text=f"{done} / {total}{suffix}")
        self.badge.configure(fg_color=self._color, text_color="#FFFFFF")

    def finish(self, text="Done", color=None):
        anim.progress_to(self.bar, 1.0)
        self.counter.configure(text=text)
        self.badge.configure(fg_color=color or theme.GREEN, text_color="#FFFFFF",
                             text="!" if color in (theme.AMBER, theme.RED) else "✓")
        self.bar.configure(progress_color=color or theme.GREEN)


class NumberStepper(ctk.CTkFrame):
    """[-] 3 [+] with clamping; ``get()`` returns the text like a StringVar."""

    def __init__(self, master, value=3, minimum=1, maximum=10, **kwargs):
        super().__init__(master, fg_color=theme.SURFACE_ALT, corner_radius=theme.RADIUS_SM, **kwargs)
        self.minimum, self.maximum = minimum, maximum
        self.var = ctk.StringVar(value=str(value))
        style = dict(width=30, height=30, corner_radius=theme.RADIUS_SM, fg_color="transparent",
                     hover_color=theme.NEUTRAL_BTN_HOVER, text_color=theme.TEXT, font=theme.font(15, "bold"))
        self.minus = ctk.CTkButton(self, text="−", command=lambda: self._step(-1), **style)
        self.minus.grid(row=0, column=0, padx=2, pady=2)
        self.entry = ctk.CTkEntry(self, textvariable=self.var, width=42, height=30, justify="center",
                                  border_width=0, fg_color="transparent", font=theme.font(13, "bold"))
        self.entry.grid(row=0, column=1)
        self.plus = ctk.CTkButton(self, text="+", command=lambda: self._step(1), **style)
        self.plus.grid(row=0, column=2, padx=2, pady=2)

    def _step(self, delta):
        try:
            value = int(self.var.get())
        except ValueError:
            value = self.minimum
        self.var.set(str(max(self.minimum, min(self.maximum, value + delta))))

    def get(self):
        return self.var.get()

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for widget in (self.minus, self.entry, self.plus):
            widget.configure(state=state)


class ColoredLog(ctk.CTkFrame):
    """Log box with colored lines, a filter (All / Errors / Visibility) and Copy."""

    MAX_LINES = 5000
    FILTERS = ("All", "Errors", "Visibility")

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.lines = []

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bar.grid_columnconfigure(1, weight=1)
        self.filter = ctk.CTkSegmentedButton(bar, values=list(self.FILTERS), command=lambda _: self._render(),
                                             selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
                                             font=theme.font(12))
        self.filter.set("All")
        self.filter.grid(row=0, column=0, sticky="w")
        self.copy_btn = ctk.CTkButton(bar, text="Copy log", width=90, height=28, command=self.copy,
                                      fg_color=theme.NEUTRAL_BTN, hover_color=theme.NEUTRAL_BTN_HOVER,
                                      text_color=theme.TEXT, font=theme.font(12))
        self.copy_btn.grid(row=0, column=2, sticky="e")

        self.box = ctk.CTkTextbox(self, font=theme.mono(12), fg_color=theme.SURFACE_ALT,
                                  corner_radius=theme.RADIUS_SM, wrap="word")
        self.box.grid(row=1, column=0, sticky="nsew")
        self._apply_tag_colors()
        self.box.configure(state="disabled")

    def _apply_tag_colors(self):
        for kind, color in _LOG_COLORS.items():
            self.box.tag_config(kind, foreground=theme.pick(color))

    def _visible(self, kind):
        mode = self.filter.get()
        if mode == "Errors":
            return kind in (LOG_ERROR, LOG_WARN)
        if mode == "Visibility":
            return kind == LOG_VIS
        return True

    def append(self, text):
        kind = classify_log_line(text)
        self.lines.append((text, kind))
        if len(self.lines) > self.MAX_LINES:
            self.lines = self.lines[-self.MAX_LINES:]
        if self._visible(kind):
            self.box.configure(state="normal")
            self.box.insert("end", text + "\n", kind)
            self.box.see("end")
            self.box.configure(state="disabled")

    def clear(self):
        self.lines = []
        self._render()

    def refresh_theme(self):
        self._apply_tag_colors()

    def _render(self):
        self._apply_tag_colors()
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        for text, kind in self.lines:
            if self._visible(kind):
                self.box.insert("end", text + "\n", kind)
        self.box.see("end")
        self.box.configure(state="disabled")

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(text for text, _ in self.lines))
        Toast.show(self, "Log copied to clipboard", theme.ACCENT)


class ResultsTable(ctk.CTkFrame):
    """One row per profile, updated in place, with a search filter."""

    def __init__(self, master, columns, weights=None, *, entity_name="profile", empty_text=None,
                 wraplengths=None, min_width=None, display_formatters=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.columns = columns
        self.weights = weights or [1] * len(columns)
        self.entity_name = entity_name
        self.wraplengths = wraplengths
        self.display_formatters = display_formatters or {}
        self.rows = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1 if min_width else 2, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.grid_columnconfigure(0, weight=1)
        # No textvariable: CustomTkinter hides the placeholder when one is set.
        self.search = ctk.CTkEntry(top, placeholder_text="Search profile or result...",
                                   height=32, fg_color=theme.SURFACE_ALT, border_width=0, font=theme.font(12))
        self.search.grid(row=0, column=0, sticky="ew")
        self.search.bind("<KeyRelease>", lambda _e: self._apply_filter())
        self.count_label = ctk.CTkLabel(top, text=f"0 {entity_name}s", text_color=theme.TEXT_MUTED, font=theme.font(12))
        self.count_label.grid(row=0, column=1, padx=(12, 0))

        table_host, header_row, body_row = self, 1, 2
        if min_width:
            self.horizontal = ctk.CTkScrollableFrame(self, orientation="horizontal", fg_color="transparent",
                                                    corner_radius=0, height=100)
            self.horizontal.grid(row=1, column=0, sticky="nsew")
            self.horizontal.grid_columnconfigure(0, weight=1)
            self.horizontal.grid_rowconfigure(0, weight=1)
            table_host = ctk.CTkFrame(self.horizontal, width=min_width, height=120, fg_color="transparent")
            table_host.grid(row=0, column=0, sticky="nsew")
            table_host.grid_propagate(False)
            table_host.grid_columnconfigure(0, weight=1)
            table_host.grid_rowconfigure(1, weight=1)
            self.bind("<Configure>", lambda event: table_host.configure(
                width=max(min_width, self._reverse_widget_scaling(event.width) - 16)))
            header_row, body_row = 0, 1
        header = ctk.CTkFrame(table_host, fg_color=theme.SURFACE_ALT, corner_radius=theme.RADIUS_SM)
        header.grid(row=header_row, column=0, sticky="ew", padx=(0, 14) if min_width else 0)
        self._configure_columns(header)
        for index, name in enumerate(columns):
            ctk.CTkLabel(header, text=name.upper(), font=theme.font(11, "bold"), text_color=theme.TEXT_MUTED,
                         anchor="w").grid(row=0, column=index, sticky="w", padx=6 if wraplengths else 10, pady=8)

        self.body = ctk.CTkScrollableFrame(table_host, fg_color="transparent")
        self.body.grid(row=body_row, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)

        self.empty = ctk.CTkLabel(self.body, text=empty_text or "Results appear here while the check runs.",
                                  text_color=theme.TEXT_MUTED, font=theme.font(13))
        self.empty.grid(row=0, column=0, pady=40)

    def _configure_columns(self, frame):
        for index, weight in enumerate(self.weights):
            frame.grid_columnconfigure(index, weight=weight, uniform="results")

    def clear(self):
        for row in self.rows.values():
            row["frame"].destroy()
        self.rows = {}
        self.empty.grid(row=0, column=0, pady=40)
        self.count_label.configure(text=f"0 {self.entity_name}s")

    def upsert(self, key, values, colors=None):
        """Add or update a row. ``colors`` maps column name -> text color."""
        colors = colors or {}
        self.empty.grid_remove()
        row = self.rows.get(key)
        if row is None:
            frame = ctk.CTkFrame(self.body, fg_color=theme.SURFACE, corner_radius=0)
            frame.grid(row=len(self.rows) + 1, column=0, sticky="ew", pady=1)
            self._configure_columns(frame)
            cells = []
            for index, _ in enumerate(self.columns):
                cell = ctk.CTkLabel(frame, text="", anchor="w", font=theme.font(12), text_color=theme.TEXT)
                if self.wraplengths:
                    cell.configure(wraplength=self.wraplengths[index], justify="left")
                cell.grid(row=0, column=index, sticky="w", padx=6 if self.wraplengths else 10, pady=6)
                cells.append(cell)
            frame.bind("<Enter>", lambda _e, f=frame: f.configure(fg_color=theme.SURFACE_ALT))
            frame.bind("<Leave>", lambda _e, f=frame: f.configure(fg_color=theme.SURFACE))
            row = {"frame": frame, "cells": cells, "values": {}}
            self.rows[key] = row
            self.count_label.configure(text=f"{len(self.rows)} {self.entity_name}" + ("" if len(self.rows) == 1 else "s"))
        row["values"].update(values)
        for index, name in enumerate(self.columns):
            if name in values:
                text = str(self.display_formatters.get(name, str)(values[name]))
                row["cells"][index].configure(
                    text=text if len(text) <= 60 else text[:57] + "...",
                    text_color=colors.get(name, theme.TEXT),
                )
        # Flash the updated row so the eye catches it.
        anim.fade_color(row["frame"], "fg_color", theme.ACCENT_SOFT, theme.SURFACE, duration_ms=900)
        self._apply_filter()

    def _apply_filter(self):
        needle = self.search.get().strip().lower()
        for row in self.rows.values():
            haystack = " ".join(str(v) for v in row["values"].values()).lower()
            if not needle or needle in haystack:
                row["frame"].grid()
            else:
                row["frame"].grid_remove()


class Toast:
    """Slide-up notification in the window's bottom-right corner."""

    _active = []

    @classmethod
    def show(cls, widget, message, color=theme.ACCENT, duration_ms=3200):
        root = widget.winfo_toplevel()
        toast = ctk.CTkFrame(root, fg_color=theme.SURFACE, corner_radius=theme.RADIUS,
                             border_width=1, border_color=theme.BORDER)
        ctk.CTkFrame(toast, width=4, height=10, corner_radius=2, fg_color=color).grid(row=0, column=0, sticky="ns",
                                                                           padx=(10, 8), pady=10)
        ctk.CTkLabel(toast, text=message, font=theme.font(13), text_color=theme.TEXT,
                     wraplength=320, justify="left").grid(row=0, column=1, padx=(0, 16), pady=12)
        offset = 24 + sum(t.winfo_reqheight() + 10 for t in cls._active if t.winfo_exists())
        cls._active.append(toast)

        toast.place(relx=1.0, rely=1.0, x=-24, y=-offset + 40, anchor="se")
        toast.lift()
        anim.animate(toast, "toast_in", 260, lambda t: toast.place_configure(y=-offset + 40 * (1 - t)))

        def close():
            if toast in cls._active:
                cls._active.remove(toast)
            try:
                toast.destroy()
            except Exception:
                pass

        toast.after(duration_ms, close)
        return toast
