import customtkinter as ctk
import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.services.sheets_service import fetch_pending_rows, update_row_status
from core.automation.browser_manager import start_profile, stop_profile, is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, update_profile_proxy, provider_label
from core.automation.profile_builder import get_val
from core.automation.dominancy_checker import run_dominancy_checker_flow
from core.utils.app_logger import log_event
from gui.ui_helpers import run_on_ui


MAX_CONCURRENCY = 10


def _parse_concurrency(value, default=1):
    """Return a safe worker count plus an optional user-facing warning."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default, f"Invalid concurrency value. Defaulting to {default}."

    clamped = max(1, min(MAX_CONCURRENCY, parsed))
    if clamped != parsed:
        return clamped, f"Concurrency must be between 1 and {MAX_CONCURRENCY}. Using {clamped}."
    return clamped, None


class DominancyCheckerView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.pending_posts = []
        self.is_processing = False
        self.engine = None
        self.log_lock = threading.Lock()

        # Grid layout
        self.grid_rowconfigure(4, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        self.header_label = ctk.CTkLabel(self, text="Dominancy Checker", font=ctk.CTkFont(size=24, weight="bold"))
        self.header_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")

        self.desc_label = ctk.CTkLabel(self, text="Checks Marketplace dominancy using search queries.\nSheet must be named 'Dominancy Checker'.")
        self.desc_label.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="w")

        # Controls
        self.controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.controls_frame.grid(row=2, column=0, padx=20, pady=0, sticky="ew")

        self.fetch_btn = ctk.CTkButton(self.controls_frame, text="Fetch Pending Listings", command=self.fetch_posts)
        self.fetch_btn.pack(side="left", padx=(0, 10))

        self.concurrency_label = ctk.CTkLabel(self.controls_frame, text="Concurrent Profiles:")
        self.concurrency_label.pack(side="left", padx=(10, 5))

        self.concurrency_entry = ctk.CTkEntry(self.controls_frame, width=40)
        self.concurrency_entry.pack(side="left", padx=(0, 10))
        self.concurrency_entry.insert(0, "1")

        self.start_btn = ctk.CTkButton(self.controls_frame, text="Start Automation", fg_color="green", hover_color="darkgreen", command=self.start_posting, state="disabled")
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ctk.CTkButton(
            self.controls_frame,
            text="Stop Automation",
            fg_color="#D9534F",
            hover_color="#C9302C",
            command=self.stop_automation,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(0, 10))

        self.continue_btn = ctk.CTkButton(
            self.controls_frame,
            text="Continue",
            command=self.continue_automation,
            state="disabled",
        )
        self.continue_btn.pack(side="left")

        self.status_label = ctk.CTkLabel(self.controls_frame, text="Ready", text_color="gray")
        self.status_label.pack(side="right", padx=10)

        # Data View
        self.data_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12))
        self.data_box.grid(row=4, column=0, padx=20, pady=10, sticky="nsew")
        self.data_box.insert("1.0", "Click 'Fetch Pending Listings' to load data from the 'Dominancy Checker' sheet.")
        self.data_box.configure(state="disabled")

    def _set_run_controls(self, mode):
        if mode == "running":
            self.fetch_btn.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.continue_btn.configure(state="disabled")
            self.concurrency_entry.configure(state="disabled")
        elif mode == "paused":
            self.fetch_btn.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.continue_btn.configure(state="normal")
            self.concurrency_entry.configure(state="disabled")
        elif mode == "preparing":
            self.fetch_btn.configure(state="disabled")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.continue_btn.configure(state="disabled")
            self.concurrency_entry.configure(state="disabled")
        else:
            self.fetch_btn.configure(state="normal")
            self.start_btn.configure(state="normal" if self.pending_posts else "disabled")
            self.stop_btn.configure(state="disabled")
            self.continue_btn.configure(state="disabled")
            self.concurrency_entry.configure(state="normal")

    def log(self, message):
        log_event(message)
        def _append():
            with self.log_lock:
                self.data_box.configure(state="normal")
                self.data_box.insert("end", f"\n{message}")
                self.data_box.see("end")
                self.data_box.configure(state="disabled")
        run_on_ui(_append)

    def clear_log(self):
        def _clear():
            self.data_box.configure(state="normal")
            self.data_box.delete("1.0", "end")
            self.data_box.configure(state="disabled")
        run_on_ui(_clear)

    def fetch_posts(self):
        self.clear_log()
        run_on_ui(lambda: self.status_label.configure(text="Fetching...", text_color="orange"))
        self.log("Connecting to Google Sheets...")

        threading.Thread(target=self._fetch_thread, daemon=True).start()

    def _fetch_thread(self):
        success, result = fetch_pending_rows("Dominancy Checker")

        if success:
            self.pending_posts = result
            count = len(self.pending_posts)

            if count > 0:
                self.log(f"Found {count} pending listings.")
                for p in self.pending_posts:
                    lower_p = {str(k).lower().strip(): v for k, v in p.items()}
                    proxy_val = lower_p.get('proxy', lower_p.get('proxy string', ''))
                    proxy_display = " | Proxy: configured" if proxy_val else ""
                    self.log(f"- Row {p['rowNumber']}: {get_val(p, 'profile name', 'name') or 'Unknown'}{proxy_display}")

                run_on_ui(lambda: self.status_label.configure(text=f"Loaded {count} listings", text_color="green"))
                run_on_ui(self._set_run_controls, "idle")
            else:
                self.log("No pending listings found. (All rows have a Status)")
                run_on_ui(lambda: self.status_label.configure(text="No listings found", text_color="gray"))
                run_on_ui(self._set_run_controls, "idle")
        else:
            self.log(f"ERROR: {result}")
            self.log("Please ensure your Google Sheet has a tab named 'Dominancy Checker' with an ID, Profile Name and Status column.")
            run_on_ui(lambda: self.status_label.configure(text="Fetch Failed", text_color="red"))
            run_on_ui(self._set_run_controls, "idle")

    def start_posting(self):
        if self.is_processing or not self.pending_posts:
            return

        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_entry.get())
        concurrency = min(concurrency, len(self.pending_posts))
        if concurrency_warning:
            self.log(f"Warning: {concurrency_warning}")

        self.is_processing = True
        run_on_ui(self._set_run_controls, "preparing")
        run_on_ui(lambda: self.status_label.configure(text="Running automation...", text_color="orange"))
        self.log("\n--- Starting Facebook Marketplace Listings ---")

        threading.Thread(target=self._posting_thread, args=(concurrency,), daemon=True).start()

    def stop_automation(self):
        if self.engine and self.engine.pause(self.log):
            self.status_label.configure(text="Stopped temporarily — click Continue", text_color="orange")
            self._set_run_controls("paused")

    def continue_automation(self):
        if self.engine and self.engine.resume(self.log):
            self.status_label.configure(text="Running automation...", text_color="orange")
            self._set_run_controls("running")

    def _posting_thread(self, concurrency):
        label = provider_label()
        try:
            if not is_incogniton_running():
                self.log(f"{label} is not running. Launching it automatically...")
                success, msg = launch_incogniton()
                if not success:
                    self.log(f"ERROR: {msg}")
                    run_on_ui(lambda: self.status_label.configure(text="Launch Failed", text_color="red"))
                    self.is_processing = False
                    run_on_ui(self._set_run_controls, "idle")
                    return

            self.log(f"Verifying that the {label} API is ready...")
            ready, rmsg = wait_for_incogniton_ready(timeout=60, logger_callback=self.log)
            if not ready:
                self.log(f"ERROR: {rmsg}")
                run_on_ui(lambda: self.status_label.configure(text="Launch Timeout", text_color="red"))
                self.is_processing = False
                run_on_ui(self._set_run_controls, "idle")
                return
        except Exception as exc:
            self.log(f"ERROR: Failed while preparing {label}: {exc}")
            run_on_ui(lambda: self.status_label.configure(text="Browser Error", text_color="red"))
            self.is_processing = False
            run_on_ui(self._set_run_controls, "idle")
            return

        posts = self.pending_posts

        self.success_count = 0
        self.total_count = len(posts)

        self.engine = GroupedAutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            return run_dominancy_checker_flow(
                debug_url=debug_url,
                post=post,
                logger=logger,
                state_callback=state_callback,
                stop_event=stop_event,
            )

        def completion_callback(post, success):
            if success:
                self.success_count += 1

        def on_engine_finished():
            self.is_processing = False
            self.pending_posts = []
            run_on_ui(lambda: self.status_label.configure(text=f"Completed: {self.success_count}/{self.total_count} successful", text_color="green"))
            self.log("\nFinished processing automatically.")
            run_on_ui(self._set_run_controls, "idle")

        started = self.engine.start(
            task_type="Dominancy Checker",
            posts=posts,
            flow_wrapper_function=flow_wrapper,
            logger_callback=self.log,
            completion_callback=completion_callback,
            master_ui_context=self,
            on_finished_callback=on_engine_finished
        )
        if started:
            run_on_ui(self._set_run_controls, "running")
