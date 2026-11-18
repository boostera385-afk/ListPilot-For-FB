"""Shared Live Checker-style dashboard for the sheet-driven automation views."""
import threading

import customtkinter as ctk

from core.automation.profile_builder import get_val
from core.services.sheets_service import fetch_pending_rows
from core.utils.app_logger import log_event
from gui import theme
from gui.ui_helpers import run_on_ui
from gui.widgets import Card, Chip, ColoredLog, NumberStepper, ResultsTable, StatCard, StepProgress


class AutomationDashboard(ctk.CTkFrame):
    def __init__(self, master, *, title, description, start_text, start_command,
                 sheet_name, concurrency=1, fetch_first=False, **kwargs):
        super().__init__(master, **kwargs)
        self.sheet_name = sheet_name
        self.start_text = start_text
        self.fetch_first = fetch_first
        self.pending_posts = []
        self.engine = None
        self.is_processing = False
        self.is_fetching = False
        self.total_count = self.done_count = self.success_count = 0
        self._completed = set()
        self._option_widgets = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.PAD, pady=(12, 10))
        header.grid_columnconfigure(0, weight=1)
        self.header_label = ctk.CTkLabel(header, text=title, font=theme.font(26, "bold"),
                                         text_color=theme.TEXT, anchor="w")
        self.header_label.grid(row=0, column=0, sticky="w")
        self.status_chip = Chip(header, "Idle", theme.GRAY)
        self.status_chip.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.desc_label = ctk.CTkLabel(header, text=description, font=theme.font(13),
                                       text_color=theme.TEXT_MUTED, anchor="w", justify="left",
                                       wraplength=700)
        self.desc_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        header.bind("<Configure>", lambda event: self.desc_label.configure(
            wraplength=max(240, self._reverse_widget_scaling(event.width) - 8)))

        self.controls_frame = Card(self)
        self.controls_frame.grid(row=1, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        self.controls_frame.grid_columnconfigure(0, weight=1)
        actions = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        actions.grid(row=0, column=0, sticky="w", padx=16, pady=14)
        style = dict(height=40, corner_radius=theme.RADIUS_SM, font=theme.font(13, "bold"))
        self.start_btn = ctk.CTkButton(actions, text=start_text, command=start_command,
                                       width=180, fg_color=theme.ACCENT,
                                       hover_color=theme.ACCENT_HOVER, **style)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))
        self.stop_btn = ctk.CTkButton(actions, text="■  Stop", command=self.stop_automation,
                                      width=90, **style)
        self.stop_btn.grid(row=0, column=1, padx=(0, 8))
        self.continue_btn = ctk.CTkButton(actions, text="⏵  Continue", command=self.continue_automation,
                                          width=108, **style)
        self.continue_btn.grid(row=0, column=2, padx=(0, 16))
        workers = ctk.CTkFrame(actions, fg_color="transparent")
        workers.grid(row=0, column=3, sticky="w")
        ctk.CTkLabel(workers, text="Profiles at a time", font=theme.font(12),
                     text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="w")
        self.concurrency = NumberStepper(workers, value=concurrency, minimum=1, maximum=10)
        self.concurrency.grid(row=1, column=0, sticky="w")
        self.concurrency_var = self.concurrency.var
        self.concurrency_entry = self.concurrency.entry
        self.options_frame = ctk.CTkFrame(self.controls_frame, fg_color="transparent")
        if fetch_first:
            self.options_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))
            self.fetch_btn = ctk.CTkButton(
                self.options_frame, text="Fetch Pending", command=self.fetch_posts, width=120, **style)
            self.fetch_btn.grid(row=0, column=0, padx=(0, 24))

        stats_row = ctk.CTkFrame(self, fg_color="transparent")
        stats_row.grid(row=2, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        self.stats = {}
        for index, (key, label, color) in enumerate((
            ("queued", "Queued rows", theme.ACCENT), ("completed", "Completed", theme.BLUE),
            ("success", "Successful", theme.GREEN), ("failed", "Failed", theme.RED),
        )):
            stats_row.grid_columnconfigure(index, weight=1, uniform="stats")
            card = StatCard(stats_row, label, color)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0))
            self.stats[key] = card

        progress_card = Card(self)
        progress_card.grid(row=3, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        progress_card.grid_columnconfigure(0, weight=1)
        self.progress = StepProgress(progress_card, 1, "Row progress")
        self.progress.grid(row=0, column=0, sticky="ew", padx=16, pady=14)
        self.tabs = ctk.CTkTabview(
            self, fg_color=theme.SURFACE, corner_radius=theme.RADIUS, border_width=1,
            border_color=theme.BORDER, segmented_button_selected_color=theme.ACCENT,
            segmented_button_selected_hover_color=theme.ACCENT_HOVER,
        )
        self.tabs.grid(row=4, column=0, sticky="nsew", padx=theme.PAD, pady=(0, theme.PAD))
        for name in ("Results", "Log"):
            tab = self.tabs.add(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        self.table = ResultsTable(self.tabs.tab("Results"), ("Row", "Profile", "Result", "Details"),
                                  (1, 3, 2, 5), entity_name="row",
                                  empty_text="Pending rows and results appear here.",
                                  wraplengths=(60, 140, 80, 240))
        self.table.grid(row=0, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.log_view = ColoredLog(self.tabs.tab("Log"))
        self.log_view.grid(row=0, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.tabs.set("Log")
        self._set_run_controls("idle")

    def add_option(self, text, variable):
        self.options_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))
        option = ctk.CTkCheckBox(
            self.options_frame, text=text, variable=variable, font=theme.font(12),
            text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            border_color=theme.TEXT_MUTED,
        )
        option.grid(row=0, column=len(self._option_widgets) + int(self.fetch_first), sticky="w", padx=(0, 24))
        self._option_widgets.append(option)
        return option

    @staticmethod
    def _enable(button, enabled, color, hover, text_color="#FFFFFF"):
        button.configure(state="normal" if enabled else "disabled",
                         fg_color=color if enabled else theme.NEUTRAL_BTN,
                         hover_color=hover, text_color=text_color)

    def set_status(self, text, color=theme.GRAY):
        self.status_chip.set(text, color)

    def _set_run_controls(self, mode):
        idle = mode == "idle"
        self._enable(self.start_btn, idle and (not self.fetch_first or bool(self.pending_posts)),
                     theme.ACCENT, theme.ACCENT_HOVER)
        self._enable(self.stop_btn, mode == "running", theme.DANGER, theme.DANGER_HOVER)
        self._enable(self.continue_btn, mode == "paused", theme.GREEN, theme.GREEN)
        if self.fetch_first:
            self._enable(self.fetch_btn, idle, theme.NEUTRAL_BTN, theme.NEUTRAL_BTN_HOVER, theme.TEXT)
        self.concurrency.set_enabled(idle)
        for option in self._option_widgets:
            option.configure(state="normal" if idle else "disabled")
        labels = {"preparing": "Preparing...", "fetching": "Fetching...",
                  "running": f"Running  {self.done_count}/{self.total_count}",
                  "paused": f"Paused  {self.done_count}/{self.total_count}"}
        self.start_btn.configure(text=labels.get(mode, self.start_text))
        if mode != "idle":
            self.set_status({"preparing": "Preparing", "fetching": "Fetching",
                             "running": "Running", "paused": "Paused"}[mode],
                            theme.GREEN if mode == "running" else theme.AMBER)

    def _sync_run_controls(self):
        if not self.is_processing:
            self._set_run_controls("idle")
        elif self.engine and self.engine.is_running:
            self._set_run_controls("paused" if self.engine.is_paused else "running")
        else:
            self._set_run_controls("preparing")

    def log(self, message):
        log_event(message)
        run_on_ui(self.log_view.append, message)

    def append_log(self, message):
        self.log(message)

    def clear_log(self):
        run_on_ui(self.log_view.clear)

    def refresh_theme(self):
        self.log_view.refresh_theme()

    def fetch_posts(self):
        if self.is_processing or self.is_fetching:
            return
        self.is_fetching = True
        self._set_run_controls("fetching")
        self.clear_log()
        self.log(f"Fetching pending rows from '{self.sheet_name}' sheet...")
        threading.Thread(target=self._fetch_thread, daemon=True).start()

    def _fetch_thread(self):
        try:
            success, result = fetch_pending_rows(self.sheet_name)
        except Exception as exc:
            success, result = False, str(exc)
        run_on_ui(self._finish_fetch, success, result)

    def _finish_fetch(self, success, result):
        self.is_fetching = False
        self.pending_posts = list(result or []) if success else []
        self._show_posts(self.pending_posts)
        self._set_run_controls("idle")
        if success:
            self.log(f"Found {self.total_count} pending rows.")
            self.set_status(f"Loaded {self.total_count} rows" if result else "No pending rows",
                            theme.GREEN if result else theme.GRAY)
        else:
            self.log(f"ERROR: {result}")
            self.log(f"Check the '{self.sheet_name}' tab and Google Sheets URL in Settings.")
            self.set_status("Fetch failed – see Log", theme.RED)
            self.tabs.set("Log")

    @staticmethod
    def _row_key(post):
        return post.get("rowNumber") or id(post)

    def _show_posts(self, posts):
        self.total_count = len(posts)
        self.done_count = self.success_count = 0
        self._completed.clear()
        self.table.clear()
        self.progress.reset("Ready" if posts else "Waiting")
        self.stats["queued"].set(self.total_count)
        for key in ("completed", "success", "failed"):
            self.stats[key].set(0)
        for post in posts:
            self.table.upsert(self._row_key(post), {
                "Row": post.get("rowNumber", "—"),
                "Profile": get_val(post, "profile name", "name", "profile") or post.get("id") or "Unknown",
                "Result": "Queued", "Details": "Waiting to run",
            }, colors={"Result": theme.TEXT_MUTED})
        if posts:
            self.tabs.set("Results")

    def _begin_run(self, posts):
        for post in posts:
            post.pop("_dashboard_detail", None)
        self._show_posts(posts)
        self.is_processing = True
        self._set_run_controls("preparing")

    def _no_pending_rows(self):
        self.is_processing = False
        self._set_run_controls("idle")
        self.set_status("No pending rows", theme.GRAY)

    @staticmethod
    def capture_result(post, result):
        post["_dashboard_detail"] = str(result[1])
        return result

    def _on_post_done(self, post, success):
        key = self._row_key(post)
        if key in self._completed:
            return
        self._completed.add(key)
        self.done_count += 1
        self.success_count += int(bool(success))
        self.table.upsert(key, {
            "Result": "Success" if success else "Failed",
            "Details": post.get("_dashboard_detail") or ("Finished" if success else "See Log for details"),
        }, colors={"Result": theme.GREEN if success else theme.RED})
        self.stats["completed"].set(self.done_count)
        self.stats["success"].set(self.success_count)
        self.stats["failed"].set(self.done_count - self.success_count)
        self.progress.update_progress(self.done_count, self.total_count, " rows")
        self._sync_run_controls()

    def _finish_run(self):
        self.is_processing = False
        self.pending_posts = []
        self._set_run_controls("idle")
        self.progress.finish(f"{self.done_count} / {self.total_count} rows")
        self.set_status("Completed" if self.done_count == self.success_count else "Completed with errors",
                        theme.GREEN if self.done_count == self.success_count else theme.AMBER)
        self.log(f"Completed: {self.success_count}/{self.total_count} successful.")

    def _preparation_failed(self, message):
        self.is_processing = False
        self._set_run_controls("idle")
        self.set_status(message, theme.RED)
        self.tabs.set("Log")

    def stop_automation(self):
        if self.engine and self.engine.pause(self.log):
            self._sync_run_controls()

    def continue_automation(self):
        if self.engine and self.engine.resume(self.log):
            self._sync_run_controls()
