import customtkinter as ctk
import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.services.sheets_service import fetch_pending_rows, reset_rows_for_recheck
from core.automation.browser_manager import is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, provider_label
from core.automation.live_checker import run_live_checker_flow, LIVE_CHECKER_SHEET
from core.automation.listing_status import format_counts
from core.automation.visibility_checker import configured_checkers, run_visibility_checks
from core.automation import listing_visibility as lv
from core.engine.automation_control import AutomationControl
from core.engine.exceptions import AutomationStopped
from core.utils.app_logger import log_event
from gui import theme
from gui import animations as anim
from gui.widgets import Card, Chip, StatCard, StepProgress, NumberStepper, ColoredLog, ResultsTable, Toast
from gui.ui_helpers import run_on_ui


MAX_CONCURRENCY = 10

# What to queue. "new" uses rows whose Status is empty/Pending; the others first
# clear Status on finished rows so the usual pending fetch picks them up again.
MODE_LABELS = {
    "new": "New rows (empty Status)",
    "pending": "Re-check profiles with pending listings",
    "failed": "Re-check failed profiles",
    "all": "Re-check every profile",
}
MODE_HELP = {
    "new": "Checks rows that have not been processed yet.",
    "pending": "Re-opens profiles whose listings were still in review last time.",
    "failed": "Retries profiles that failed or were only partly read.",
    "all": "Clears every finished row and checks all profiles again.",
}

RESULT_COLUMNS = ("Profile", "Result", "Live", "Pending", "Rejected", "Total", "PK", "US", "Visibility")
RESULT_WEIGHTS = (4, 3, 2, 2, 2, 2, 3, 3, 3)


def _parse_concurrency(value, default=3):
    """Return a safe worker count plus an optional user-facing warning."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default, f"Invalid concurrency value. Defaulting to {default}."

    clamped = max(1, min(MAX_CONCURRENCY, parsed))
    if clamped != parsed:
        return clamped, f"Concurrency must be between 1 and {MAX_CONCURRENCY}. Using {clamped}."
    return clamped, None


def _profile_name(post):
    return str(post.get("profile name") or post.get("Profile Name") or post.get("profileName") or post.get("id") or "?")


class LiveCheckerView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.grid_rowconfigure(4, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.engine = None
        self.visibility_control = None  # set while the PK/US checker pass runs
        self.is_processing = False
        self.total_count = 0
        self.success_count = 0
        self.done_count = 0
        self.totals = {}
        self.visibility_issues = 0

        self._build_header()
        self._build_controls()
        self._build_stats()
        self._build_progress()
        self._build_tabs()

        self.status_pulse = anim.Pulse(self.status_chip, "text_color", theme.ACCENT_SOFT, theme.GREEN)
        self._refresh_checkers()
        self._set_buttons(True, False, False)

    # --- Layout ------------------------------------------------------------------
    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=theme.PAD, pady=(12, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Live Checker", font=theme.font(26, "bold"), text_color=theme.TEXT,
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, anchor="w", justify="left", font=theme.font(13), text_color=theme.TEXT_MUTED,
            text=f"Step 1 reads every profile's listings. Step 2 lets the PK and US checker accounts search "
                 f"for each LIVE listing as buyers. Results go to the '{LIVE_CHECKER_SHEET}' sheet.",
            wraplength=900,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        chips = ctk.CTkFrame(header, fg_color="transparent")
        chips.grid(row=0, column=1, sticky="e")
        self.status_chip = Chip(chips, "Idle", theme.GRAY)
        self.status_chip.grid(row=0, column=0, padx=(0, 8))
        self.pk_chip = Chip(chips, "PK checker")
        self.pk_chip.grid(row=0, column=1, padx=(0, 8))
        self.us_chip = Chip(chips, "US checker")
        self.us_chip.grid(row=0, column=2)

    def _build_controls(self):
        card = Card(self)
        card.grid(row=1, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        card.grid_columnconfigure(4, weight=1)

        self.start_btn = ctk.CTkButton(card, text="▶  Start Live Check", command=self.start_automation,
                                       width=190, height=40, corner_radius=theme.RADIUS_SM,
                                       fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                                       font=theme.font(14, "bold"))
        self.start_btn.grid(row=0, column=0, rowspan=2, padx=(16, 8), pady=16)

        self.stop_btn = ctk.CTkButton(card, text="■  Stop", command=self.stop_automation, width=96, height=40,
                                      corner_radius=theme.RADIUS_SM, fg_color=theme.DANGER,
                                      hover_color=theme.DANGER_HOVER, font=theme.font(13, "bold"), state="disabled")
        self.stop_btn.grid(row=0, column=1, rowspan=2, padx=(0, 8))

        self.continue_btn = ctk.CTkButton(card, text="⏵  Continue", command=self.continue_automation, width=110,
                                          height=40, corner_radius=theme.RADIUS_SM, fg_color=theme.NEUTRAL_BTN,
                                          hover_color=theme.NEUTRAL_BTN_HOVER, text_color=theme.TEXT,
                                          font=theme.font(13, "bold"), state="disabled")
        self.continue_btn.grid(row=0, column=2, rowspan=2, padx=(0, 20))

        ctk.CTkLabel(card, text="Profiles at a time", font=theme.font(12), text_color=theme.TEXT_MUTED).grid(
            row=0, column=3, sticky="sw", pady=(12, 2))
        self.concurrency = NumberStepper(card, value=3, minimum=1, maximum=MAX_CONCURRENCY)
        self.concurrency.grid(row=1, column=3, sticky="nw", pady=(0, 14))
        self.concurrency_var = self.concurrency.var  # kept for older callers

        mode_box = ctk.CTkFrame(card, fg_color="transparent")
        mode_box.grid(row=0, column=5, rowspan=2, sticky="e", padx=16)
        ctk.CTkLabel(mode_box, text="What to check", font=theme.font(12), text_color=theme.TEXT_MUTED,
                     anchor="w").grid(row=0, column=0, sticky="w")
        self.mode_var = ctk.StringVar(value=MODE_LABELS["new"])
        self.mode_menu = ctk.CTkOptionMenu(
            mode_box, variable=self.mode_var, values=list(MODE_LABELS.values()), width=290, height=32,
            fg_color=theme.SURFACE_ALT, button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER,
            text_color=theme.TEXT, font=theme.font(12), command=self._on_mode_change,
        )
        self.mode_menu.grid(row=1, column=0, sticky="w", pady=(2, 2))
        self.mode_help = ctk.CTkLabel(mode_box, text=MODE_HELP["new"], font=theme.font(11),
                                      text_color=theme.TEXT_MUTED, anchor="w")
        self.mode_help.grid(row=2, column=0, sticky="w")

    def _build_stats(self):
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        specs = (
            ("profiles", "Profiles checked", theme.ACCENT),
            ("total", "Total listings", theme.BLUE),
            ("LIVE", "Live", theme.GREEN),
            ("PENDING", "Pending", theme.AMBER),
            ("REJECTED", "Rejected", theme.RED),
            ("hidden", "Visibility issues", theme.PURPLE),
        )
        self.stats = {}
        for index, (key, title, color) in enumerate(specs):
            row.grid_columnconfigure(index, weight=1, uniform="stats")
            card = StatCard(row, title, color)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0))
            self.stats[key] = card

    def _build_progress(self):
        card = Card(self)
        card.grid(row=3, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        card.grid_columnconfigure((0, 1), weight=1, uniform="steps")
        self.step1 = StepProgress(card, 1, "Owner profile scan", theme.ACCENT)
        self.step1.grid(row=0, column=0, sticky="ew", padx=(16, 12), pady=14)
        self.step2 = StepProgress(card, 2, "Buyer visibility (PK + US)", theme.PURPLE)
        self.step2.grid(row=0, column=1, sticky="ew", padx=(12, 16), pady=14)

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, fg_color=theme.SURFACE, corner_radius=theme.RADIUS,
                                   border_width=1, border_color=theme.BORDER,
                                   segmented_button_selected_color=theme.ACCENT,
                                   segmented_button_selected_hover_color=theme.ACCENT_HOVER)
        self.tabs.grid(row=4, column=0, sticky="nsew", padx=theme.PAD, pady=(0, theme.PAD))
        for name in ("Results", "Log"):
            tab = self.tabs.add(name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        self.table = ResultsTable(self.tabs.tab("Results"), RESULT_COLUMNS, RESULT_WEIGHTS)
        self.table.grid(row=0, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.log = ColoredLog(self.tabs.tab("Log"))
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.tabs.set("Log")

    # --- Small UI helpers ------------------------------------------------------------
    def _on_mode_change(self, label):
        key = next((k for k, v in MODE_LABELS.items() if v == label), "new")
        self.mode_help.configure(text=MODE_HELP[key])

    def _refresh_checkers(self):
        configured = dict(configured_checkers())
        for label, chip in (("PK", self.pk_chip), ("US", self.us_chip)):
            if label in configured:
                chip.set(f"{label}: {configured[label]}"[:28], theme.GREEN)
            else:
                chip.set(f"{label}: not set", theme.AMBER)
        self.after(3000, self._refresh_checkers)

    def refresh_theme(self):
        self.log.refresh_theme()

    def _set_status(self, text, color, pulse):
        self.status_chip.set(text, color)
        if pulse:
            self.status_pulse.start()
        else:
            self.status_pulse.stop(color)

    def _reset_dashboard(self):
        self.log.clear()
        self.table.clear()
        self.totals, self.done_count, self.visibility_issues = {}, 0, 0
        for card in self.stats.values():
            card.set(0)
        self.step1.reset()
        self.step2.reset("Waiting" if configured_checkers() else "Skipped – set checkers in Settings")

    def append_log(self, text):
        log_event(text)
        self.log.append(text)

    def update_start_button(self):
        def _update():
            engine_running = bool(self.engine and self.engine.is_running)
            paused = bool(engine_running and self.engine.is_paused)
            if self.visibility_control:
                self.start_btn.configure(text="Checking visibility...")
                self._set_buttons(False, True, False)
                self.concurrency.set_enabled(False)
                self.mode_menu.configure(state="disabled")
                self._set_status("Checking visibility", theme.PURPLE, True)
            elif self.is_processing or engine_running:
                label = "Paused" if paused else "Running"
                self.start_btn.configure(text=f"{label}  {self.success_count}/{self.total_count}")
                self._set_buttons(False, engine_running and not paused, paused)
                self.concurrency.set_enabled(False)
                self.mode_menu.configure(state="disabled")
                if paused:
                    self._set_status("Paused", theme.AMBER, False)
                else:
                    self._set_status("Running", theme.GREEN, True)
            else:
                self.mode_menu.configure(state="normal")
                self.start_btn.configure(text="▶  Start Live Check")
                self._set_buttons(True, False, False)
                self.concurrency.set_enabled(True)
                self._set_status("Idle", theme.GRAY, False)

        run_on_ui(_update)

    @staticmethod
    def _enable(button, enabled, color, hover, text_color="#FFFFFF"):
        """Disabled buttons turn neutral so only usable actions look clickable."""
        if enabled:
            button.configure(state="normal", fg_color=color, hover_color=hover, text_color=text_color)
        else:
            button.configure(state="disabled", fg_color=theme.NEUTRAL_BTN)

    def _set_buttons(self, start, stop, cont):
        self._enable(self.start_btn, start, theme.ACCENT, theme.ACCENT_HOVER)
        self._enable(self.stop_btn, stop, theme.DANGER, theme.DANGER_HOVER)
        self._enable(self.continue_btn, cont, theme.GREEN, theme.GREEN)

    def _finish_idle(self):
        self.is_processing = False
        run_on_ui(self.update_start_button)

    # --- Live dashboard updates (UI thread) --------------------------------------------
    def _on_profile_done(self, post, success):
        self.done_count += 1
        counts = post.get("_live_counts") or {}
        for state, count in counts.items():
            self.totals[state] = self.totals.get(state, 0) + count
        code = post.get("_live_check") or ("OK" if success else "FAILED")
        self.table.upsert(post.get("rowNumber") or _profile_name(post), {
            "Profile": _profile_name(post),
            "Result": code.split(":")[0],
            "Live": counts.get("LIVE", 0),
            "Pending": counts.get("PENDING", 0),
            "Rejected": counts.get("REJECTED", 0),
            "Total": sum(counts.values()),
            "PK": "…" if counts.get("LIVE") and configured_checkers() else "—",
            "US": "…" if counts.get("LIVE") and configured_checkers() else "—",
            "Visibility": "—",
        }, colors={"Result": theme.status_color(code), "Live": theme.GREEN,
                   "Pending": theme.AMBER, "Rejected": theme.RED})
        self.stats["profiles"].set(self.done_count)
        self.stats["total"].set(sum(self.totals.values()))
        for state in ("LIVE", "PENDING", "REJECTED"):
            self.stats[state].set(self.totals.get(state, 0))
        self.step1.update_progress(self.done_count, self.total_count, " profiles")
        if self.done_count >= self.total_count:
            self.step1.finish(f"{self.done_count} / {self.total_count} profiles")

    def _on_visibility_done(self, posts):
        for post in posts:
            values = post.get("_visibility_cols")
            if not values:
                continue
            code = values.get("Visibility", "")
            if code != "OK":
                self.visibility_issues += 1
            pk = values.get(lv.checker_column("PK"), "—")
            us = values.get(lv.checker_column("US"), "—")
            self.table.upsert(post.get("rowNumber") or _profile_name(post), {
                "PK": pk.split(",")[0], "US": us.split(",")[0], "Visibility": code.split(":")[0],
            }, colors={"Visibility": theme.status_color(code)})
        self.stats["hidden"].set(self.visibility_issues)

    # --- Run ------------------------------------------------------------------------
    def start_automation(self):
        if self.is_processing or (self.engine and self.engine.is_running):
            return

        self._reset_dashboard()

        concurrency, concurrency_warning = _parse_concurrency(self.concurrency.get())
        master_ui_context = self.winfo_toplevel()
        mode = next((key for key, label in MODE_LABELS.items() if label == self.mode_var.get()), "new")

        self.is_processing = True
        self.start_btn.configure(text="Preparing...")
        self._set_buttons(False, False, False)
        self._set_status("Preparing", theme.ACCENT, True)
        self.append_log(f"Fetching pending rows from '{LIVE_CHECKER_SHEET}' sheet...")
        if concurrency_warning:
            self.append_log(f"Warning: {concurrency_warning}")

        threading.Thread(
            target=self._prepare_and_run,
            args=(concurrency, master_ui_context, mode),
            daemon=True,
        ).start()

    def _prepare_and_run(self, concurrency, master_ui_context, mode="new"):
        def logger_proxy(msg):
            run_on_ui(self.append_log, msg)

        def toast(message, color):
            run_on_ui(Toast.show, self, message, color)

        if mode != "new":
            try:
                ok, result = reset_rows_for_recheck(LIVE_CHECKER_SHEET, mode)
            except Exception as exc:
                ok, result = False, str(exc)
            if not ok:
                logger_proxy(f"ERROR: could not queue rows for '{MODE_LABELS[mode]}': {result}")
                toast("Could not queue rows – see the log.", theme.RED)
                self._finish_idle()
                return
            logger_proxy(f"Queued {result} profile(s) for '{MODE_LABELS[mode]}'.")

        try:
            success, result = fetch_pending_rows(LIVE_CHECKER_SHEET)
        except Exception as exc:
            logger_proxy(f"ERROR: Failed to fetch pending tasks: {exc}")
            toast("Could not reach Google Sheets.", theme.RED)
            self._finish_idle()
            return

        if not success:
            logger_proxy(f"ERROR: {result}")
            logger_proxy(f"Please ensure your Google Sheet has a tab named '{LIVE_CHECKER_SHEET}' with an ID, Profile Name and Status column "
                         "(re-deploy the latest Apps Script from Script Builder and run setupSheets to create it).")
            toast(f"'{LIVE_CHECKER_SHEET}' sheet not ready – see the log.", theme.RED)
            self._finish_idle()
            return

        posts = list(result or [])
        if not posts:
            logger_proxy("No pending tasks found.")
            toast("Nothing to check – no pending rows.", theme.AMBER)
            self._finish_idle()
            return

        concurrency = min(concurrency, len(posts))
        logger_proxy(f"Found {len(posts)} profiles to check. Using concurrency: {concurrency}")

        self.success_count = 0
        self.total_count = len(posts)
        run_on_ui(self.step1.update_progress, 0, len(posts), " profiles")
        run_on_ui(self.tabs.set, "Results")
        self.engine = GroupedAutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            return run_live_checker_flow(
                debug_url=debug_url,
                post=post,
                logger=logger,
                state_callback=state_callback,
                stop_event=stop_event,
            )

        totals = {}
        issues = {}
        totals_lock = threading.Lock()

        def on_completion(post, success):
            with totals_lock:
                if success:
                    self.success_count += 1
                for state, count in (post.get("_live_counts") or {}).items():
                    totals[state] = totals.get(state, 0) + count
                code = post.get("_live_check")
                if code and code != "OK":
                    issues[code] = issues.get(code, 0) + 1
            run_on_ui(self._on_profile_done, post, success)
            run_on_ui(self.update_start_button)

        def on_finished():
            run_on_ui(self.append_log, "--- All Live Checks Completed ---")
            with totals_lock:
                run_on_ui(self.append_log, f"Listings found: {format_counts(totals)}")
                if issues:
                    run_on_ui(self.append_log, "Profiles with issues: " + ", ".join(f"{n} {c}" for c, n in sorted(issues.items())))
            run_on_ui(self.step1.finish, f"{self.success_count} / {self.total_count} succeeded")
            self._run_visibility_pass(posts, logger_proxy, master_ui_context)
            self.is_processing = False
            run_on_ui(self.update_start_button)
            toast(f"Live check finished: {format_counts(totals)}", theme.GREEN if not issues else theme.AMBER)

        self._run_engine_thread(posts, flow_wrapper, logger_proxy, on_completion, on_finished, master_ui_context)

    def _run_visibility_pass(self, posts, logger_proxy, master_ui_context):
        """Second phase: the PK and US checker accounts look for each LIVE listing."""
        if not configured_checkers():
            logger_proxy("Visibility check skipped: set the PK / US checker profiles in Settings to enable it.")
            return
        self.visibility_control = AutomationControl()
        run_on_ui(self.update_start_button)

        def progress(done, total):
            run_on_ui(self.step2.update_progress, done, total, " checks")

        try:
            summary = run_visibility_checks(posts, logger_proxy, self.visibility_control, master_ui_context,
                                            progress=progress)
            if summary:
                logger_proxy("Visibility: " + ", ".join(f"{n} {c}" for c, n in sorted(summary.items())))
                run_on_ui(self.step2.finish, ", ".join(f"{n} {c}" for c, n in sorted(summary.items())))
            else:
                logger_proxy("Visibility check: no LIVE listings to verify.")
                run_on_ui(self.step2.finish, "No LIVE listings")
        except AutomationStopped:
            logger_proxy("Visibility check stopped by user.")
            run_on_ui(lambda: self.step2.counter.configure(text="Stopped"))
        except Exception as exc:
            logger_proxy(f"Visibility check failed: {exc}")
        finally:
            self.visibility_control = None
            run_on_ui(self._on_visibility_done, posts)
            run_on_ui(self.update_start_button)

    def _run_engine_thread(self, posts, flow_wrapper, logger_proxy, on_completion, on_finished, master_ui_context):
        label = provider_label()

        try:
            if not is_incogniton_running():
                logger_proxy(f"{label} is not running. Attempting to launch...")
                launched, launch_msg = launch_incogniton()
                if not launched:
                    logger_proxy(f"Failed to launch {label}: {launch_msg}")
                    run_on_ui(Toast.show, self, f"Could not launch {label}.", theme.RED)
                    self._finish_idle()
                    return

            logger_proxy(f"Verifying that the {label} API is ready...")
            ready, ready_msg = wait_for_incogniton_ready(timeout=60, logger_callback=logger_proxy)
            if not ready:
                logger_proxy(f"{label} did not become ready: {ready_msg}")
                run_on_ui(Toast.show, self, f"{label} is not ready.", theme.RED)
                self._finish_idle()
                return
        except Exception as exc:
            logger_proxy(f"Failed while preparing {label}: {exc}")
            self._finish_idle()
            return

        started = self.engine.start(
            task_type=LIVE_CHECKER_SHEET,
            posts=posts,
            flow_wrapper_function=flow_wrapper,
            logger_callback=logger_proxy,
            completion_callback=on_completion,
            master_ui_context=master_ui_context,
            on_finished_callback=on_finished,
        )
        if started:
            run_on_ui(self.update_start_button)

    def stop_automation(self):
        if self.visibility_control:
            self.visibility_control.stop()
            self.append_log("Stopping the visibility check...")
            return
        if self.engine and self.engine.pause(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()

    def continue_automation(self):
        if self.engine and self.engine.resume(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()
