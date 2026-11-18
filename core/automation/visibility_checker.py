"""Buyer-side visibility check: other accounts search for each LIVE listing.

After the owner scans finish, each checker profile (Pakistani, then USA) is
opened once. For every LIVE listing it searches Marketplace by title; when the
listing is not in the results it opens the listing link. Results are written
per owner row. The owner profile is never used to judge visibility.
"""
import random
import time

from playwright.sync_api import sync_playwright

from core.config import get_setting
from core.engine.exceptions import AutomationStopped
from core.automation import listing_visibility as lv
from core.automation.browser_manager import start_profile, stop_profile
from core.automation.live_checker import LIVE_CHECKER_SHEET, capture_diagnostics
from core.services.sheets_service import update_column_values

SEARCH_SCROLLS = 3

_SEARCH_RESULTS_JS = r"""
() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).map(a => ({
  href: a.getAttribute('href') || '',
  text: (a.innerText || a.getAttribute('aria-label') || '').slice(0, 300),
}))
"""

_LOGGED_OUT_SELECTORS = ('input[name="email"]', "#email", 'input[name="pass"]')
_CHECKPOINT_TEXTS = (
    "Confirm your identity",
    "Confirm you're a real person",
    "Account restricted",
    "We suspended your account",
    "Your account has been locked",
)


def configured_checkers():
    """[(label, profile name or ID)] for the checkers filled in Settings."""
    found = []
    for label, key in lv.CHECKERS:
        profile = str(get_setting(key, "") or "").strip()
        if profile:
            found.append((label, profile))
    return found


def _debug_url(data):
    if not isinstance(data, dict):
        return ""
    url = data.get("url") or data.get("puppeteerUrl") or data.get("webSocketDebuggerUrl") or ""
    if not url and data.get("port"):
        url = f"http://127.0.0.1:{data['port']}"
    if url and not url.startswith(("http", "ws")):
        url = f"ws://{url}"
    return url


def _blocked_reason(page):
    try:
        for sel in _LOGGED_OUT_SELECTORS:
            if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                return "LOGGED_OUT"
    except Exception:
        pass
    for text in _CHECKPOINT_TEXTS:
        try:
            if page.get_by_text(text, exact=False).first.is_visible():
                return "CHECKPOINT"
        except Exception:
            pass
    if "/checkpoint" in page.url:
        return "CHECKPOINT"
    if "/login" in page.url:
        return "LOGGED_OUT"
    return ""


def _goto(page, url, sleep):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    sleep(random.uniform(2.5, 4.5))


def _search(page, target, sleep):
    _goto(page, lv.search_url(target["title"]), sleep)
    for scroll in range(SEARCH_SCROLLS + 1):
        try:
            results = page.evaluate(_SEARCH_RESULTS_JS) or []
        except Exception:
            results = []
        if lv.found_in_search(results, target):
            return True
        if scroll < SEARCH_SCROLLS:
            try:
                page.mouse.wheel(0, random.randint(1400, 2200))
            except Exception:
                break
            sleep(random.uniform(1.5, 2.5))
    return False


def _open_link(page, target, sleep):
    _goto(page, lv.item_url(target["listingId"]), sleep)
    try:
        text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 20000) : ''") or ""
    except Exception:
        text = ""
    return lv.classify_item_page(page.url, text, target["title"])


def check_listings(debug_url, label, targets, log, sleep, on_result=None):
    """One result per target, in order, from one checker browser."""
    results = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            _goto(page, "https://www.facebook.com/marketplace/", sleep)
            blocked = _blocked_reason(page)
            if blocked:
                capture_diagnostics(page, f"checker_{label}", log)
                log(f"[{label} checker] {blocked}: cannot check listings with this account.")
                return [lv.ERROR] * len(targets)

            for number, target in enumerate(targets, 1):
                try:
                    if _search(page, target, sleep):
                        state = lv.SEARCH
                    elif target["listingId"]:
                        state = _open_link(page, target, sleep)
                    else:
                        state = lv.UNCLEAR  # older layout: no ID, so no link to open
                    if _blocked_reason(page):
                        log(f"[{label} checker] account blocked mid-run; stopping this checker.")
                        results.extend([lv.ERROR] * (len(targets) - len(results)))
                        break
                except AutomationStopped:
                    raise
                except Exception as exc:
                    log(f"[{label} checker] '{target['title']}': {exc}")
                    state = lv.ERROR
                results.append(state)
                log(f"[{label} checker] {number}/{len(targets)} {target['title']}: {state}")
                if on_result:
                    on_result()
                sleep(random.uniform(2.0, 4.0))
        finally:
            try:
                page.close()
            except Exception:
                pass
    return results


def run_visibility_checks(posts, log, stop_event, master=None, progress=None):
    """Check every LIVE listing of ``posts`` from each configured checker.

    Writes PK Check / US Check / Visibility / Visibility Details per row and
    returns ``{visibility code prefix: row count}`` for the end-of-run log.
    ``progress(done, total)`` is called after each listing check (any thread).
    """
    def sleep(seconds):
        if stop_event.wait(seconds):
            raise AutomationStopped("User manually stopped automation.")

    checkers = configured_checkers()
    rows = []
    for post in posts:
        targets, skipped = lv.visibility_targets(post.get("_live_listings"))
        if targets and post.get("rowNumber"):
            rows.append((post, targets, skipped))
    if not checkers or not rows:
        return {}

    flat = [target for _, targets, _ in rows for target in targets]
    log(f"--- Visibility check: {len(flat)} LIVE listing(s) from {len(rows)} profile(s), "
        f"checkers: {', '.join(label for label, _ in checkers)} ---")

    total_checks = len(flat) * len(checkers)
    done = [0]

    def advance(count=1):
        done[0] += count
        if progress:
            progress(min(done[0], total_checks), total_checks)

    advance(0)
    per_label = {}
    for label, profile in checkers:
        label_start = done[0]
        if stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")
        log(f"[{label} checker] Starting profile '{profile}'...")
        from gui.ui_helpers import resolve_duplicates_ui
        started, data = start_profile(profile, duplicate_resolver=resolve_duplicates_ui, master=master, logger_callback=log)
        url = _debug_url(data) if started else ""
        if not url:
            log(f"[{label} checker] could not start: {data if not started else 'no CDP endpoint'}")
            per_label[label] = [lv.ERROR] * len(flat)
            advance(len(flat))
            if started:
                stop_profile(profile)
            continue
        try:
            per_label[label] = check_listings(url, label, flat, log, sleep, on_result=advance)
        except AutomationStopped:
            raise
        except Exception as exc:
            log(f"[{label} checker] failed: {exc}")
            per_label[label] = [lv.ERROR] * len(flat)
        finally:
            advance(label_start + len(flat) - done[0])  # count listings skipped by an early stop
            if not stop_profile(profile):
                log(f"[{label} checker] Warning: the browser provider did not confirm the profile stopped.")
            stop_event.wait(3)

    summary = {}
    offset = 0
    for post, targets, skipped in rows:
        own = {label: results[offset:offset + len(targets)] for label, results in per_label.items()}
        offset += len(targets)
        values = lv.row_columns(targets, own, skipped)
        post["_visibility"] = values["Visibility"]
        post["_visibility_cols"] = values
        code = values["Visibility"].split(":")[0]
        summary[code] = summary.get(code, 0) + 1
        try:
            saved, message = update_column_values(LIVE_CHECKER_SHEET, post["rowNumber"], values)
            if not saved:
                log(f"Note: row {post['rowNumber']}: could not save visibility columns ({message}).")
        except Exception as exc:
            log(f"Note: row {post['rowNumber']}: could not save visibility columns: {exc}")
    return summary
