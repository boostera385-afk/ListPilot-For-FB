import customtkinter as ctk
import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.services.sheets_service import fetch_pending_rows, reset_rows_for_recheck
from core.automation.browser_manager import is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, provider_label
from core.automation.live_checker import run_live_checker_flow, LIVE_CHECKER_SHEET
from core.automation.listing_status import format_counts
from core.utils.app_logger import log_event
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


class LiveCheckerView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(self, text="Live Checker", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.grid(row=0, column=0, padx=20, pady=(20, 5), sticky="w")

        self.desc_label = ctk.CTkLabel(
            self,
            justify="left",
            text=(
                "Opens each profile's Marketplace listings, reads every tab and saves LIVE / PENDING / REJECTED counts,\n"
                f"per-listing details and a Check Result to the '{LIVE_CHECKER_SHEET}' sheet (re-deploy the Apps Script once for the new columns)."
            ),
        )
        self.desc_label.grid(row=1, column=0, padx=20, pady=(0, 10), sticky="w")

        controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        controls_frame.grid(row=2, column=0, padx=20, pady=10, sticky="ew")

        self.start_btn = ctk.CTkButton(controls_frame, text="Start Live Check", command=self.start_automation,
                                       width=160, height=35, font=ctk.CTkFont(weight="bold"))
        self.start_btn.grid(row=0, column=0, padx=(0, 10))

        self.stop_btn = ctk.CTkButton(controls_frame, text="Stop Automation", command=self.stop_automation,
                                      fg_color="#D9534F", hover_color="#C9302C", width=130, height=35,
                                      state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(0, 10))

        self.continue_btn = ctk.CTkButton(controls_frame, text="Continue", command=self.continue_automation,
                                          width=90, height=35, state="disabled")
        self.continue_btn.grid(row=0, column=2, padx=(0, 20))

        ctk.CTkLabel(controls_frame, text="Profiles at a time:").grid(row=0, column=3, padx=(0, 5))
        self.concurrency_var = ctk.StringVar(value="3")
        self.concurrency_entry = ctk.CTkEntry(controls_frame, textvariable=self.concurrency_var, width=50)
        self.concurrency_entry.grid(row=0, column=4)

        ctk.CTkLabel(controls_frame, text="Check:").grid(row=0, column=5, padx=(20, 5))
        self.mode_var = ctk.StringVar(value=MODE_LABELS["new"])
        self.mode_menu = ctk.CTkOptionMenu(
            controls_frame, variable=self.mode_var, values=list(MODE_LABELS.values()), width=250
        )
        self.mode_menu.grid(row=0, column=6)

        self.log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=3, column=0, padx=20, pady=(0, 20), sticky="nsew")
        self.log_box.configure(state="disabled")

        self.engine = None
        self.is_processing = False
        self.total_count = 0
        self.success_count = 0

    def append_log(self, text):
        log_event(text)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.update_idletasks()

    def update_start_button(self):
        def _update():
            engine_running = bool(self.engine and self.engine.is_running)
            paused = bool(engine_running and self.engine.is_paused)
            if self.is_processing or engine_running:
                label = "Paused" if paused else "Running"
                self.start_btn.configure(text=f"{label} ({self.success_count}/{self.total_count})", state="disabled")
                self.stop_btn.configure(state="disabled" if paused or not engine_running else "normal")
                self.continue_btn.configure(state="normal" if paused else "disabled")
                self.concurrency_entry.configure(state="disabled")
                self.mode_menu.configure(state="disabled")
            else:
                self.mode_menu.configure(state="normal")
                self.start_btn.configure(text="Start Live Check", state="normal")
                self.stop_btn.configure(state="disabled")
                self.continue_btn.configure(state="disabled")
                self.concurrency_entry.configure(state="normal")

        run_on_ui(_update)

    def _finish_idle(self):
        self.is_processing = False
        run_on_ui(self.update_start_button)

    def start_automation(self):
        if self.is_processing or (self.engine and self.engine.is_running):
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_var.get())
        master_ui_context = self.winfo_toplevel()
        mode = next((key for key, label in MODE_LABELS.items() if label == self.mode_var.get()), "new")

        self.is_processing = True
        self.start_btn.configure(text="Preparing...", state="disabled")
        self.stop_btn.configure(state="disabled")
        self.continue_btn.configure(state="disabled")
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

        if mode != "new":
            try:
                ok, result = reset_rows_for_recheck(LIVE_CHECKER_SHEET, mode)
            except Exception as exc:
                ok, result = False, str(exc)
            if not ok:
                logger_proxy(f"ERROR: could not queue rows for '{MODE_LABELS[mode]}': {result}")
                self._finish_idle()
                return
            logger_proxy(f"Queued {result} profile(s) for '{MODE_LABELS[mode]}'.")

        try:
            success, result = fetch_pending_rows(LIVE_CHECKER_SHEET)
        except Exception as exc:
            logger_proxy(f"ERROR: Failed to fetch pending tasks: {exc}")
            self._finish_idle()
            return

        if not success:
            logger_proxy(f"ERROR: {result}")
            logger_proxy(f"Please ensure your Google Sheet has a tab named '{LIVE_CHECKER_SHEET}' with an ID, Profile Name and Status column "
                         "(re-deploy the latest Apps Script from Script Builder and run setupSheets to create it).")
            self._finish_idle()
            return

        posts = list(result or [])
        if not posts:
            logger_proxy("No pending tasks found.")
            self._finish_idle()
            return

        concurrency = min(concurrency, len(posts))
        logger_proxy(f"Found {len(posts)} profiles to check. Using concurrency: {concurrency}")

        self.success_count = 0
        self.total_count = len(posts)
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
            run_on_ui(self.update_start_button)

        def on_finished():
            self.is_processing = False
            run_on_ui(self.update_start_button)
            run_on_ui(self.append_log, "--- All Live Checks Completed ---")
            with totals_lock:
                run_on_ui(self.append_log, f"Listings found: {format_counts(totals)}")
                if issues:
                    run_on_ui(self.append_log, "Profiles with issues: " + ", ".join(f"{n} {c}" for c, n in sorted(issues.items())))

        self._run_engine_thread(posts, flow_wrapper, logger_proxy, on_completion, on_finished, master_ui_context)

    def _run_engine_thread(self, posts, flow_wrapper, logger_proxy, on_completion, on_finished, master_ui_context):
        label = provider_label()

        try:
            if not is_incogniton_running():
                logger_proxy(f"{label} is not running. Attempting to launch...")
                launched, launch_msg = launch_incogniton()
                if not launched:
                    logger_proxy(f"Failed to launch {label}: {launch_msg}")
                    self._finish_idle()
                    return

            logger_proxy(f"Verifying that the {label} API is ready...")
            ready, ready_msg = wait_for_incogniton_ready(timeout=60, logger_callback=logger_proxy)
            if not ready:
                logger_proxy(f"{label} did not become ready: {ready_msg}")
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
        if self.engine and self.engine.pause(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()

    def continue_automation(self):
        if self.engine and self.engine.resume(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()
