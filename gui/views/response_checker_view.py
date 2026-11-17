import customtkinter as ctk
import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.services.sheets_service import fetch_pending_rows, update_row_status
from core.automation.browser_manager import start_profile, stop_profile, is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, update_profile_proxy, provider_label
from core.automation.profile_builder import get_val
from core.automation.response_checker import run_response_checker_flow
from core.utils.app_logger import log_event
from gui.ui_helpers import run_on_ui


MAX_CONCURRENCY = 10


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


class ResponseCheckerView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")

        self.title_label = ctk.CTkLabel(header_frame, text="Response Checker", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(side="left")

        controls_frame = ctk.CTkFrame(self, fg_color="transparent")
        controls_frame.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        controls_frame.grid_columnconfigure(5, weight=1)

        self.start_btn = ctk.CTkButton(controls_frame, text="Start Response Check", command=self.start_automation, 
                                       width=180, height=35, font=ctk.CTkFont(weight="bold"))
        self.start_btn.grid(row=0, column=0, padx=(0, 10))

        self.stop_btn = ctk.CTkButton(controls_frame, text="Stop Automation", command=self.stop_automation,
                                      fg_color="#D9534F", hover_color="#C9302C", width=130, height=35,
                                      state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(0, 10))

        self.continue_btn = ctk.CTkButton(controls_frame, text="Continue", command=self.continue_automation,
                                          width=90, height=35, state="disabled")
        self.continue_btn.grid(row=0, column=2, padx=(0, 20))

        # Concurrency Spinbox
        ctk.CTkLabel(controls_frame, text="Profiles at a time:").grid(row=0, column=3, padx=(0, 5))
        self.concurrency_var = ctk.StringVar(value="3")
        self.concurrency_entry = ctk.CTkEntry(controls_frame, textvariable=self.concurrency_var, width=50)
        self.concurrency_entry.grid(row=0, column=4, padx=(0, 20))

        # Keep open checkbox
        self.keep_open_var = ctk.BooleanVar(value=True)
        self.keep_open_checkbox = ctk.CTkCheckBox(controls_frame, text="Keep profile open if response found", variable=self.keep_open_var)
        self.keep_open_checkbox.grid(row=0, column=5, sticky="w")

        self.log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="nsew")
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
                self.keep_open_checkbox.configure(state="disabled")
            else:
                self.start_btn.configure(text="Start Response Check", state="normal")
                self.stop_btn.configure(state="disabled")
                self.continue_btn.configure(state="disabled")
                self.concurrency_entry.configure(state="normal")
                self.keep_open_checkbox.configure(state="normal")

        run_on_ui(_update)

    def start_automation(self):
        if self.is_processing or (self.engine and self.engine._is_running):
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        keep_open = self.keep_open_var.get()
        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_var.get())
        master_ui_context = self.winfo_toplevel()

        self.is_processing = True
        self.start_btn.configure(text="Preparing...", state="disabled")
        self.stop_btn.configure(state="disabled")
        self.continue_btn.configure(state="disabled")
        self.append_log("Fetching pending rows from 'Response Checker' sheet...")
        if concurrency_warning:
            self.append_log(f"Warning: {concurrency_warning}")

        threading.Thread(
            target=self._prepare_and_run,
            args=(keep_open, concurrency, master_ui_context),
            daemon=True,
        ).start()

    def _prepare_and_run(self, keep_open, concurrency, master_ui_context):
        def logger_proxy(msg):
            run_on_ui(self.append_log, msg)

        try:
            success, result = fetch_pending_rows("Response Checker")
        except Exception as exc:
            logger_proxy(f"ERROR: Failed to fetch pending tasks: {exc}")
            self.is_processing = False
            run_on_ui(self.update_start_button)
            return

        if not success:
            logger_proxy(f"ERROR: {result}")
            self.is_processing = False
            run_on_ui(self.update_start_button)
            return

        posts = list(result or [])
        if not posts:
            logger_proxy("No pending tasks found.")
            self.is_processing = False
            run_on_ui(self.update_start_button)
            return

        concurrency = min(concurrency, len(posts))

        # Inject keep_open preference into posts
        for post in posts:
            post["_keep_profile_open"] = keep_open

        logger_proxy(f"Found {len(posts)} profiles to check. Using concurrency: {concurrency}")

        self.success_count = 0
        self.total_count = len(posts)
        self.engine = GroupedAutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            return run_response_checker_flow(
                debug_url=debug_url,
                post=post,
                logger=logger,
                state_callback=state_callback,
                stop_event=stop_event
            )

        def on_completion(post, success):
            if success:
                self.success_count += 1
            run_on_ui(self.update_start_button)

        def on_finished():
            self.is_processing = False
            run_on_ui(self.update_start_button)
            run_on_ui(self.append_log, "--- All Response Checks Completed ---")

        self._run_engine_thread(
            posts,
            flow_wrapper,
            logger_proxy,
            on_completion,
            on_finished,
            master_ui_context,
        )

    def _run_engine_thread(self, posts, flow_wrapper, logger_proxy, on_completion, on_finished, master_ui_context):
        label = provider_label()

        try:
            if not is_incogniton_running():
                logger_proxy(f"{label} is not running. Attempting to launch...")
                launched, launch_msg = launch_incogniton()
                if not launched:
                    logger_proxy(f"Failed to launch {label}: {launch_msg}")
                    self.is_processing = False
                    run_on_ui(self.update_start_button)
                    return

            logger_proxy(f"Verifying that the {label} API is ready...")
            ready, ready_msg = wait_for_incogniton_ready(timeout=60, logger_callback=logger_proxy)
            if not ready:
                logger_proxy(f"{label} did not become ready: {ready_msg}")
                self.is_processing = False
                run_on_ui(self.update_start_button)
                return
        except Exception as exc:
            logger_proxy(f"Failed while preparing {label}: {exc}")
            self.is_processing = False
            run_on_ui(self.update_start_button)
            return

        started = self.engine.start(
            task_type="Response Checker",
            posts=posts,
            flow_wrapper_function=flow_wrapper,
            logger_callback=logger_proxy,
            completion_callback=on_completion,
            master_ui_context=master_ui_context,
            on_finished_callback=on_finished
        )
        if started:
            run_on_ui(self.update_start_button)

    def stop_automation(self):
        if self.engine and self.engine.pause(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()

    def continue_automation(self):
        if self.engine and self.engine.resume(logger_callback=lambda msg: run_on_ui(self.append_log, msg)):
            self.update_start_button()
