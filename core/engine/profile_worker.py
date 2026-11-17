import threading
import time
from core.engine.state_machine import AutomationState
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped, SheetsUnavailableError
from core.engine.retry_manager import RetryManager
from core.services.sheets_service import claim_row, update_row_status, update_column_value
from core.automation.browser_manager import start_profile, stop_profile, update_profile_proxy
from core.automation.profile_builder import get_val
from core.engine.automation_control import AutomationControl


def _clean_failure_message(message):
    text = str(message or "Unknown automation failure").strip()
    while text.lower().startswith("failed:"):
        text = text[len("failed:"):].strip()
    return text or "Unknown automation failure"


class ProfileWorker:
    def __init__(self, task_type, post_data, flow_function, logger_callback, completion_callback, master_ui_context=None):
        self.task_type = task_type
        self.post = post_data
        self.flow_function = flow_function
        self.logger_callback = logger_callback
        self.completion_callback = completion_callback
        self.master_ui_context = master_ui_context
        self.state = AutomationState.QUEUED
        self._stop_event = AutomationControl()
        self.retry_manager = RetryManager(max_attempts=3, backoff_seconds=5.0, logger_callback=self.log)
        
    def log(self, msg):
        self.logger_callback(f"[{get_val(self.post, 'profile name', 'name', 'profile') or 'Unknown'}] {msg}")

    def update_state(self, new_state, result_msg=""):
        self.state = new_state
        self.log(f"Status -> {new_state.value}")
        if result_msg:
            self.log(result_msg)

    def stop(self):
        self._stop_event.stop()

    def pause(self):
        return self._stop_event.pause()

    def resume(self):
        return self._stop_event.resume()

    def _log_failure(self, profile_id, row, exc_type, msg):
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"\n--- FAILURE DIAGNOSTICS ---\nTimestamp: {ts}\nProfile: {get_val(self.post, 'profile name', 'name', 'profile')}\nProfile ID: {profile_id}\nRow: {row}\nStage: {self.state.name}\nException type: {exc_type}\nError: {msg}\n---------------------------")


    def _check_stop(self):
        if self._stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")

    def run(self):
        profile_name_or_id = get_val(self.post, "profile name", "name", "profile")
        profile_id = self.post.get("id") if self.post.get("id") else profile_name_or_id
        row_number = self.post.get("rowNumber")

        # 1. Claim the row. Network failures are deferred, not misreported as
        # another process owning the row, and must never write a Failed status.
        self.log(f"Claiming row {row_number} in Google Sheets...")
        try:
            success, msg = claim_row(self.task_type, row_number)
        except SheetsUnavailableError as exc:
            self.update_state(
                AutomationState.FAILED_RETRYABLE,
                f"Row {row_number} deferred — {exc} The row remains pending.",
            )
            self.completion_callback(self.post, False)
            return

        if not success:
            if str(msg).lower().startswith("already claimed"):
                self.update_state(
                    AutomationState.SKIPPED,
                    f"Row {row_number} skipped — it is already being processed.",
                )
            else:
                self.update_state(AutomationState.FAILED_PERMANENT, f"Could not claim row {row_number}: {msg}")
            self.completion_callback(self.post, False)
            return
        self.update_state(AutomationState.CLAIMED)

        def _single_attempt():
            self._check_stop()

            # Proxy Update
            new_proxy_str = get_val(self.post, "proxy", "proxy string")
            if new_proxy_str and str(new_proxy_str).lower() != "n/a":
                self.log("Updating proxy before launch...")
                upd_ok, upd_msg = update_profile_proxy(profile_id, new_proxy_str)
                if upd_ok:
                    self.log("Proxy updated successfully.")
                else:
                    # Treat proxy failure as permanent to avoid wasting time on bad IPs
                    raise PermanentError(f"Failed to update proxy: {upd_msg}")

            # Start Profile
            self.update_state(AutomationState.PROFILE_STARTING)
            from gui.ui_helpers import resolve_duplicates_ui
            started, data = start_profile(profile_id, duplicate_resolver=resolve_duplicates_ui, master=self.master_ui_context, logger_callback=self.log)
            
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
                stop_profile(profile_id)
                raise RetryableError("Profile launched without a usable CDP endpoint.")

            # Run Flow
            self.update_state(AutomationState.CDP_CONNECTING)
            
            # The flow_wrapper needs to accept stop_event and state_callback if it wants to be smart
            # We will pass kwargs down
            status, result_msg = self.flow_function(
                debug_url=debug_url, 
                post=self.post, 
                logger=self.log,
                state_callback=self.update_state,
                stop_event=self._stop_event
            )
            
            if status == "FOUND" or status == True:
                return True, result_msg
            else:
                # Flow function itself might return False, result_msg
                # We can treat soft failures as PermanentError unless the flow raises RetryableError itself.
                raise PermanentError(_clean_failure_message(result_msg))

        # Execute with retries
        final_status = False
        try:
            success, result_msg = self.retry_manager.execute_with_retry(_single_attempt, stop_event=self._stop_event)
            self.update_state(AutomationState.SUCCESS, f"Link -> {result_msg}")

            if self.task_type in ["Auto Posting", "Auto Listing", "Auto Random Posting"] and str(result_msg).startswith("http"):
                update_column_value(self.task_type, row_number, "Post link", str(result_msg))
                update_row_status(self.task_type, row_number, "Posted")
            else:
                update_row_status(self.task_type, row_number, "Posted", f"Link: {result_msg}")
                
            final_status = True
        except AutomationStopped:
            self.update_state(AutomationState.STOPPED)
            update_row_status(self.task_type, row_number, "Failed (Stopped)")
        except PermanentError as e:
            failure_message = _clean_failure_message(e)
            self._log_failure(profile_id, row_number, "PermanentError", failure_message)
            self.update_state(AutomationState.FAILED_PERMANENT, failure_message)
            update_row_status(self.task_type, row_number, f"Failed: {failure_message}")
        except RetryableError as e:
            self._log_failure(profile_id, row_number, "RetryableError (Max Retries)", str(e))
            self.update_state(AutomationState.FAILED_RETRYABLE, f"Max retries reached: {str(e)}")
            update_row_status(self.task_type, row_number, f"Failed (Timeout/Retryable): {str(e)}")
        except Exception as e:
            failure_message = _clean_failure_message(e)
            self._log_failure(profile_id, row_number, type(e).__name__, failure_message)
            self.update_state(AutomationState.FAILED_PERMANENT, f"Unexpected Exception: {failure_message}")
            update_row_status(self.task_type, row_number, f"Failed: {failure_message}")
        finally:
            if self.post.get("_force_keep_open"):
                self.log("Keeping profile open because unread responses were found.")
            else:
                self.log("Stopping profile...")
                if not stop_profile(profile_id):
                    self.log("Warning: the browser provider did not confirm that the profile stopped.")
                self._stop_event.wait(3)
            self.completion_callback(self.post, final_status)


