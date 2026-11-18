import threading
from core.engine.grouped_automation_engine import GroupedAutomationEngine
from core.automation.browser_manager import is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, provider_label
from core.automation.dominancy_checker import run_dominancy_checker_flow
from gui.automation_dashboard import AutomationDashboard
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


class DominancyCheckerView(AutomationDashboard):
    def __init__(self, master, **kwargs):
        super().__init__(
            master, title="Dominancy Checker", sheet_name="Dominancy Checker", fetch_first=True,
            description="Check Marketplace search placement using the 'Dominancy Checker' sheet. "
                        "Fetch pending rows, review the queue, then start checking.",
            start_text="▶  Start Dominancy Check", start_command=self.start_posting, **kwargs,
        )

    def start_posting(self):
        if self.is_processing or self.is_fetching or not self.pending_posts:
            return

        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_entry.get())
        concurrency = min(concurrency, len(self.pending_posts))
        self.concurrency_var.set(str(concurrency))
        if concurrency_warning:
            self.log(f"Warning: {concurrency_warning}")

        self._begin_run(self.pending_posts)
        self.log("--- Starting Dominancy Checker ---")

        threading.Thread(target=self._posting_thread, args=(concurrency,), daemon=True).start()

    def _posting_thread(self, concurrency):
        label = provider_label()
        try:
            if not is_incogniton_running():
                self.log(f"{label} is not running. Launching it automatically...")
                success, msg = launch_incogniton()
                if not success:
                    self.log(f"ERROR: {msg}")
                    run_on_ui(self._preparation_failed, "Launch failed")
                    return

            self.log(f"Verifying that the {label} API is ready...")
            ready, rmsg = wait_for_incogniton_ready(timeout=60, logger_callback=self.log)
            if not ready:
                self.log(f"ERROR: {rmsg}")
                run_on_ui(self._preparation_failed, "Launch timeout")
                return
        except Exception as exc:
            self.log(f"ERROR: Failed while preparing {label}: {exc}")
            run_on_ui(self._preparation_failed, "Browser error")
            return

        posts = self.pending_posts

        self.engine = GroupedAutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            result = run_dominancy_checker_flow(
                debug_url=debug_url,
                post=post,
                logger=logger,
                state_callback=state_callback,
                stop_event=stop_event,
            )
            return self.capture_result(post, result)

        def completion_callback(post, success):
            run_on_ui(self._on_post_done, post, success)

        def on_engine_finished():
            run_on_ui(self._finish_run)

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
            run_on_ui(self._sync_run_controls)
        else:
            run_on_ui(self._preparation_failed, "Could not start – see Log")
