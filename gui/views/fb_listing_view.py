import customtkinter as ctk
import threading
from core.engine.automation_engine import AutomationEngine
from core.automation.browser_manager import is_incogniton_running, launch_incogniton, wait_for_incogniton_ready, provider_label
from core.automation.fb_listing import run_fb_listing_flow
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


class FBListingView(AutomationDashboard):
    def __init__(self, master, **kwargs):
        super().__init__(
            master, title="FB Listings", sheet_name="FB Listings", fetch_first=True,
            description="Create Facebook Marketplace listings from the 'FB Listings' sheet. "
                        "Fetch pending rows, review the queue, then start posting.",
            start_text="▶  Start Posting", start_command=self.start_posting, **kwargs,
        )
        self.live_check_var = ctk.BooleanVar(value=False)
        self.live_check_checkbox = self.add_option("Check Live after posting", self.live_check_var)
        self.keep_open_var = ctk.BooleanVar(value=True)
        self.keep_open_checkbox = self.add_option("Keep open if response found", self.keep_open_var)

    def start_posting(self):
        if self.is_processing or self.is_fetching or not self.pending_posts:
            return

        concurrency, concurrency_warning = _parse_concurrency(self.concurrency_entry.get())
        concurrency = min(concurrency, len(self.pending_posts))
        self.concurrency_var.set(str(concurrency))
        keep_open_pref = bool(self.keep_open_var.get())
        live_check_pref = bool(self.live_check_var.get())
        if concurrency_warning:
            self.log(f"Warning: {concurrency_warning}")

        self._begin_run(self.pending_posts)
        live_note = "enabled" if live_check_pref else "disabled"
        self.log(f"\n--- Starting Facebook Marketplace Listings (live check {live_note}) ---")

        threading.Thread(
            target=self._posting_thread,
            args=(concurrency, keep_open_pref, live_check_pref),
            daemon=True,
        ).start()

    def _posting_thread(self, concurrency, keep_open_pref=False, live_check_pref=False):
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

        posts = [dict(pending_post) for pending_post in self.pending_posts]
        for p in posts:
            p["_keep_profile_open"] = keep_open_pref
            p["_run_live_checker"] = live_check_pref

        self.engine = AutomationEngine(max_workers=concurrency)

        def flow_wrapper(debug_url, post, logger, state_callback=None, stop_event=None):
            result = run_fb_listing_flow(
                debug_url=debug_url,
                post=post,
                logger_callback=logger,
                state_callback=state_callback,
                stop_event=stop_event,
            )
            return self.capture_result(post, result)

        def completion_callback(post, success):
            run_on_ui(self._on_post_done, post, success)

        def on_engine_finished():
            run_on_ui(self._finish_run)

        started = self.engine.start(
            task_type="FB Listings",
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
