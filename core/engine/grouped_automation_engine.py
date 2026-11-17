import threading
from concurrent.futures import ThreadPoolExecutor
import time
from core.engine.state_machine import AutomationState
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped, SheetsUnavailableError
from core.engine.retry_manager import RetryManager
from core.services.sheets_service import claim_row, update_row_status, update_column_value
from core.automation.browser_manager import start_profile, stop_profile, update_profile_proxy
from core.automation.profile_builder import get_val
from collections import defaultdict
from core.engine.automation_control import AutomationControl


def _clean_failure_message(message):
    text = str(message or "Unknown automation failure").strip()
    while text.lower().startswith("failed:"):
        text = text[len("failed:"):].strip()
    return text or "Unknown automation failure"


class GroupedProfileWorker:
    def __init__(self, task_type, profile_name_or_id, posts, flow_function, logger_callback, completion_callback, master_ui_context=None):
        self.task_type = task_type
        self.profile_id = profile_name_or_id
        self.posts = posts
        self.flow_function = flow_function
        self.logger_callback = logger_callback
        self.completion_callback = completion_callback
        self.master_ui_context = master_ui_context
        self._stop_event = AutomationControl()
        self.retry_manager = RetryManager(max_attempts=3, backoff_seconds=5.0, logger_callback=self.log)

    def log(self, msg):
        self.logger_callback(f"[{self.profile_id}] {msg}")

    def update_state(self, new_state, result_msg=""):
        self.log(f"Status -> {new_state.value}")
        if result_msg:
            self.log(result_msg)

    def stop(self):
        self._stop_event.stop()

    def pause(self):
        return self._stop_event.pause()

    def resume(self):
        return self._stop_event.resume()

    def _check_stop(self):
        if self._stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")

    def run(self):
        def _attempt_profile():
            self._check_stop()

            # Proxy Update (Use proxy from first post if available)
            first_post = self.posts[0]
            new_proxy_str = get_val(first_post, "proxy", "proxy string")
            if new_proxy_str and str(new_proxy_str).lower() != "n/a":
                self.log("Updating proxy before launch...")
                upd_ok, upd_msg = update_profile_proxy(self.profile_id, new_proxy_str)
                if not upd_ok:
                    raise PermanentError(f"Failed to update proxy: {upd_msg}")

            # Start Profile
            self.update_state(AutomationState.PROFILE_STARTING)
            from gui.ui_helpers import resolve_duplicates_ui
            started, data = start_profile(self.profile_id, duplicate_resolver=resolve_duplicates_ui, master=self.master_ui_context, logger_callback=self.log)
            if not started:
                raise RetryableError(f"Failed to start profile: {data}")

            self._check_stop()

            # Extract CDP
            debug_url = data.get("url") if isinstance(data, dict) else ""
            if not debug_url and "puppeteerUrl" in data: debug_url = data.get("puppeteerUrl")
            if not debug_url and "webSocketDebuggerUrl" in data: debug_url = data.get("webSocketDebuggerUrl")
            if not debug_url: debug_url = f"http://127.0.0.1:{data.get('port')}" if isinstance(data, dict) and data.get("port") else ""
            if debug_url and not debug_url.startswith("http") and not debug_url.startswith("ws"):
                debug_url = f"ws://{debug_url}"
            if not debug_url:
                stop_profile(self.profile_id)
                raise RetryableError("Profile launched without a usable CDP endpoint.")
                
            self.update_state(AutomationState.CDP_CONNECTING)
            return debug_url

        try:
            debug_url = self.retry_manager.execute_with_retry(_attempt_profile, stop_event=self._stop_event)
        except Exception as e:
            self.log(f"Failed to start profile for group: {e}")
            for p in self.posts:
                self.completion_callback(p, False)
            return

        # Profile is open, now process posts concurrently (up to 3 tabs at a time)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def process_post(post):
            row_number = post.get("rowNumber")
            try:
                self._check_stop()
                self.log(f"Claiming row {row_number} in Google Sheets...")
                try:
                    success, msg = claim_row(self.task_type, row_number)
                except SheetsUnavailableError as exc:
                    self.log(f"Row {row_number} deferred — {exc} The row remains pending.")
                    self.completion_callback(post, False)
                    return
                if not success:
                    if str(msg).lower().startswith("already claimed"):
                        self.log(f"Row {row_number} skipped — it is already being processed.")
                    else:
                        self.log(f"Could not claim row {row_number}: {msg}")
                    self.completion_callback(post, False)
                    return

                def _single_task():
                    self._check_stop()
                    status, result_msg = self.flow_function(
                        debug_url=debug_url, 
                        post=post, 
                        logger=self.log,
                        state_callback=self.update_state,
                        stop_event=self._stop_event
                    )
                    if status == "FOUND" or status == True:
                        return True, result_msg
                    else:
                        raise PermanentError(_clean_failure_message(result_msg))

                success, result_msg = self.retry_manager.execute_with_retry(_single_task, stop_event=self._stop_event)
                update_row_status(self.task_type, row_number, "Success", f"{result_msg}")
                self.completion_callback(post, True)

            except AutomationStopped:
                update_row_status(self.task_type, row_number, "Failed (Stopped)")
                self.completion_callback(post, False)
            except PermanentError as e:
                update_row_status(
                    self.task_type,
                    row_number,
                    f"Failed: {_clean_failure_message(e)}",
                )
                self.completion_callback(post, False)
            except Exception as e:
                update_row_status(
                    self.task_type,
                    row_number,
                    f"Failed: {_clean_failure_message(e)}",
                )
                self.completion_callback(post, False)

        self.log("Starting concurrent task execution (3 tabs)...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(process_post, p) for p in self.posts]
            for _ in as_completed(futures):
                pass

        if any(p.get("_force_keep_open") for p in self.posts):
            self.log("Finished all grouped tasks. Keeping profile OPEN as requested by Response Checker!")
        else:
            self.log("Finished all grouped tasks. Closing extra tabs and stopping profile...")
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b = pw.chromium.connect_over_cdp(debug_url, timeout=5000)
                    for ctx in b.contexts:
                        for idx, p in enumerate(ctx.pages):
                            if idx > 0:
                                try: p.close()
                                except: pass
                    b.close()
            except: pass
            try:
                if not stop_profile(self.profile_id):
                    self.log("Warning: the browser provider did not confirm that the profile stopped.")
            except Exception as exc:
                self.log(f"Warning: failed to stop profile cleanly: {exc}")
            self._stop_event.wait(3)

class GroupedAutomationEngine:
    def __init__(self, max_workers=3):
        self.max_workers = max_workers
        self.executor = None
        self.workers = []
        self.futures = []
        self._is_running = False
        self._is_paused = False
        self._lock = threading.Lock()

    @property
    def is_running(self):
        with self._lock:
            return self._is_running

    @property
    def is_paused(self):
        with self._lock:
            return self._is_paused

    def start(self, task_type, posts, flow_wrapper_function, logger_callback, completion_callback, master_ui_context=None, on_finished_callback=None):
        with self._lock:
            if self._is_running:
                logger_callback("Engine is already running.")
                return False
            self._is_running = True
            self._is_paused = False
            self.workers = []
            self.futures = []
            self.executor = ThreadPoolExecutor(max_workers=self.max_workers)

        logger_callback(f"--- Starting Grouped Automation Engine ({task_type}) ---")

        # Group posts by profile
        grouped_posts = defaultdict(list)
        for p in posts:
            p_name = get_val(p, "profile name", "name", "profile")
            p_id = p.get("id") if p.get("id") else p_name
            grouped_posts[p_id].append(p)

        def task_completion_hook(post, success):
            completion_callback(post, success)

        for p_id, p_list in grouped_posts.items():
            worker = GroupedProfileWorker(
                task_type=task_type,
                profile_name_or_id=p_id,
                posts=p_list,
                flow_function=flow_wrapper_function,
                logger_callback=logger_callback,
                completion_callback=task_completion_hook,
                master_ui_context=master_ui_context
            )
            self.workers.append(worker)
            future = self.executor.submit(worker.run)
            self.futures.append(future)

        def monitor_completion():
            if self.executor:
                self.executor.shutdown(wait=True)
            
            with self._lock:
                self._is_running = False
                self._is_paused = False
                self.workers = []
                self.futures = []
                self.executor = None
            
            logger_callback("--- Automation Engine Finished ---")
            if on_finished_callback:
                on_finished_callback()

        threading.Thread(target=monitor_completion, daemon=True).start()
        return True

    def stop(self, logger_callback=print):
        with self._lock:
            if not self._is_running:
                return
            logger_callback("Stopping Automation Engine... signaling workers.")
            for future in self.futures:
                future.cancel()
            for worker in self.workers:
                worker.stop()
            self._is_paused = False

    def pause(self, logger_callback=print):
        with self._lock:
            if not self._is_running or self._is_paused:
                return False
            self._is_paused = True
            workers = list(self.workers)

        for worker in workers:
            worker.pause()
        logger_callback("Automation paused. Click Continue to resume.")
        return True

    def resume(self, logger_callback=print):
        with self._lock:
            if not self._is_running or not self._is_paused:
                return False
            self._is_paused = False
            workers = list(self.workers)

        for worker in workers:
            worker.resume()
        logger_callback("Automation continued.")
        return True
