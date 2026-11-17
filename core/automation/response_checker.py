import time
import random
from playwright.sync_api import sync_playwright
from core.engine.state_machine import AutomationState
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped
from core.automation.profile_builder import get_val
from core.automation.fb_listing import read_messenger_unread_count

FB_HOME = "https://www.facebook.com/"

def run_response_checker_flow(debug_url, post, logger=print, state_callback=None, stop_event=None):
    def log(msg):
        logger(msg)

    def set_state(state, msg=""):
        if state_callback:
            state_callback(state, msg)

    def check_stop():
        if stop_event and stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")

    def interruptible_sleep(seconds):
        check_stop()
        if stop_event:
            if stop_event.wait(seconds):
                raise AutomationStopped("User manually stopped automation.")
        else:
            time.sleep(seconds)

    def check_for_checkpoint(p):
        try:
            texts_to_check = [
                "Confirm your identity",
                "Confirm you're a real person",
                "Start video selfie",
                "Account restricted",
                "We suspended your account",
                "Your account has been locked",
                "We need more info to make sure you're human"
            ]
            for t in texts_to_check:
                loc = p.get_by_text(t, exact=False).first
                if loc.is_visible():
                    return True
        except Exception:
            pass
        return False

    def is_logged_out(page):
        try:
            login_selectors = ['input[name="email"]', '#email', 'input[name="pass"]']
            for sel in login_selectors:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    return True
            return False
        except Exception:
            return False

    def is_logged_in(page):
        try:
            indicators = ['div[aria-label="Create"]', 'div[aria-label="Messenger"]', 'div[aria-label="Account"]', 'div[role="navigation"]']
            for sel in indicators:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    return True
            return False
        except Exception:
            return False

    with sync_playwright() as p:
        check_stop()
        log("Connecting to browser...")
        try:
            browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
        except Exception as e:
            raise RetryableError(f"CDP Connection failed: {e}")

        context = browser.contexts[0]
        page = context.new_page()
        page.bring_to_front()

        try:
            log("Navigating to Facebook homepage...")
            try:
                page.goto(FB_HOME, timeout=60000)
            except Exception:
                pass
            interruptible_sleep(2)

            check_stop()
            set_state(AutomationState.LOGIN_CHECK if hasattr(AutomationState, "LOGIN_CHECK") else AutomationState.PAGE_LOADING)
            
            if is_logged_out(page) and not is_logged_in(page):
                raise PermanentError("Facebook profile is not logged in. Please log in to this profile first.")
            
            if check_for_checkpoint(page):
                raise PermanentError("Confirm your identity")
                
            log("Facebook session is logged in.")
            set_state(AutomationState.READY)

            # Scroll the feed slowly (human-like) for 10s
            check_stop()
            log("Scrolling the Facebook feed for 10 seconds...")
            scroll_start = time.time()
            while time.time() - scroll_start < 10:
                check_stop()
                try:
                    scroll_amt = random.randint(150, 400)
                    page.evaluate(f"window.scrollBy({{top: {scroll_amt}, left: 0, behavior: 'smooth'}})")
                except Exception:
                    pass
                interruptible_sleep(random.uniform(0.5, 1.2))

            log("Checking for new responses (Messenger badge)...")
            unread_count = read_messenger_unread_count(page, interruptible_sleep)
            if unread_count is None:
                log("Could not find the Messenger icon on the page.")
            elif post.get("rowNumber"):
                try:
                    from core.services.sheets_service import update_column_value
                    saved, message = update_column_value(
                        post.get("_target_sheet", "Response Checker"), post.get("rowNumber"), "Responses", str(unread_count)
                    )
                    if not saved:
                        log(f"Note: could not save the 'Responses' column ({message}).")
                except Exception as exc:
                    log(f"Note: could not save the 'Responses' column: {exc}")
            if unread_count is None:
                unread_count = 0

            if unread_count > 0:
                log(f"Found {unread_count} new response(s)!")
                # Check if keep_open is enabled
                keep_open = post.get("_keep_profile_open", False)
                if keep_open:
                    post["_force_keep_open"] = True
                    log("Profile will be kept open so you can read the messages.")
                return True, f"Found {unread_count} responses"
            else:
                log("No new responses found.")
                return True, "No responses"

        finally:
            if post.get("_force_keep_open", False):
                log("Leaving the response tab open for review.")
            else:
                try:
                    page.close()
                except Exception:
                    pass
