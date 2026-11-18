import customtkinter as ctk
import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.services.sheets_service import fetch_pending_rows
from core.automation.browser_manager import is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, provider_label
from core.automation.response_checker import run_response_checker_flow
from gui.automation_dashboard import AutomationDashboard
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


class ResponseCheckerView(AutomationDashboard):
    def __init__(self, master, **kwargs):
        super().__init__(
            master, title="Response Checker", sheet_name="Response Checker", concurrency=3,
            description="Check pending profiles for Marketplace responses. Results are saved to the "
                        "'Response Checker' sheet; profiles with responses can stay open for follow-up.",
            start_text="▶  Start Response Check", start_command=self.start_automation, **kwargs,
        )
        self.keep_open_var = ctk.BooleanVar(value=True)
        self.keep_open_checkbox = self.add_option("Keep profile open if response found", self.keep_open_var)

    def update_start_button(self):
        run_on_ui(self._sync_run_controls)

    def start_automation(self):
        if self.is_processing or (self.engine and self.engine.is_running):
            return

        self.clear_log()

        keep_open = self.keep_open_var.get()
        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_var.get())
        self.concurrency_var.set(str(concurrency))
        master_ui_context = self.winfo_toplevel()

        self._begin_run([])
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
            self.append_log(msg)

        try:
            success, result = fetch_pending_rows("Response Checker")
        except Exception as exc:
            logger_proxy(f"ERROR: Failed to fetch pending tasks: {exc}")
            run_on_ui(self._preparation_failed, "Fetch failed – see Log")
            return

        if not success:
            logger_proxy(f"ERROR: {result}")
            run_on_ui(self._preparation_failed, "Fetch failed – see Log")
            return

        posts = list(result or [])
        if not posts:
            logger_proxy("No pending tasks found.")
            run_on_ui(self._no_pending_rows)
            return

        concurrency = min(concurrency, len(posts))

        # Inject keep_open preference into posts
        for post in posts:
            post["_keep_profile_open"] = keep_open

        logger_proxy(f"Found {len(posts)} profiles to check. Using concurrency: {concurrency}")

        run_on_ui(self._show_posts, posts)
        self.engine = GroupedAutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            result = run_response_checker_flow(
                debug_url=debug_url,
                post=post,
                logger=logger,
                state_callback=state_callback,
                stop_event=stop_event
            )
            return self.capture_result(post, result)

        def on_completion(post, success):
            run_on_ui(self._on_post_done, post, success)

        def on_finished():
            run_on_ui(self._finish_run)
            self.append_log("--- All Response Checks Completed ---")

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
                    run_on_ui(self._preparation_failed, "Launch failed")
                    return

            logger_proxy(f"Verifying that the {label} API is ready...")
            ready, ready_msg = wait_for_incogniton_ready(timeout=60, logger_callback=logger_proxy)
            if not ready:
                logger_proxy(f"{label} did not become ready: {ready_msg}")
                run_on_ui(self._preparation_failed, "Launch timeout")
                return
        except Exception as exc:
            logger_proxy(f"Failed while preparing {label}: {exc}")
            run_on_ui(self._preparation_failed, "Browser error")
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
            run_on_ui(self._sync_run_controls)
        else:
            run_on_ui(self._preparation_failed, "Could not start – see Log")
