"""Buyer-side visibility check: other accounts search for each LIVE listing.

After the owner scans finish, each checker profile (Pakistani, then USA) is
opened once. For every LIVE listing it searches Marketplace by title; when the
listing is not in the results it opens the listing link. Results are written
per owner row. The owner profile is never used to judge visibility.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

from core.config import load_config
from core.engine.exceptions import AutomationStopped
from core.automation import listing_visibility as lv
from core.automation.browser_manager import start_profile, stop_profile, resolve_profile_id
from core.automation.live_checker import LIVE_CHECKER_SHEET, capture_diagnostics
from core.services.sheets_service import update_column_values

SEARCH_SCROLLS = 2
MARKETPLACE_HOME = "https://www.facebook.com/marketplace/"

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


def buyer_options():
    return lv.BuyerOptions.from_settings(load_config())


def configured_checkers(options=None):
    """[(label, profile name or ID)] for the checkers filled in Settings."""
    return list((options or buyer_options()).checkers)


def validate_checker_roles(posts, options):
    """Reject self-checks/duplicate buyer accounts before any profile is launched."""
    resolved = {}
    for label, profile in options.checkers:
        canonical = resolve_profile_id(profile)
        if not canonical:
            return f"{label} checker cannot be resolved. Use its exact profile ID in Settings."
        if canonical in resolved:
            return "PK and US must be different Facebook profiles."
        resolved[canonical] = label
    if not resolved:
        return ""
    for post in posts:
        identifier = post.get("id") or post.get("ID") or post.get("profileName") or post.get("Profile Name") or post.get("profile name")
        if identifier and resolve_profile_id(str(identifier)) in resolved:
            return f"Row {post.get('rowNumber', '?')} is also a buyer checker. Keep checker accounts out of the seller queue, or turn buyer checks off."
    return ""


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
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        return False
    sleep(0.2)  # cooperative stop checkpoint, not a multi-second fixed delay
    return True


def _search(page, target, sleep, cache=None, need=1, exhaustive=False):
    """``"ID"`` / ``"TEXT"`` when the listing shows in search, ``""`` when it does not,
    ``None`` when the results page never loaded (never a confident miss)."""
    key = lv.search_url(target["title"])
    if cache is not None and key in cache:
        cached, complete = cache[key]
        kind = lv.match_in_search(cached, target, need)
        if kind:
            return kind
        if complete:
            return ""
    if not _goto(page, key, sleep):
        return None
    try:
        page.wait_for_function(r"""() =>
            document.querySelector('a[href*="/marketplace/item/"], input[name="email"]') ||
            /no results|no listings found|couldn't find/i.test(document.body.innerText)
        """, timeout=5000)
    except Exception:
        return None  # never turn an unloaded page into a confident search miss
    collected = []
    for scroll in range(SEARCH_SCROLLS + 1):
        try:
            results = page.evaluate(_SEARCH_RESULTS_JS) or []
        except Exception:
            return None
        collected.extend(results)
        kind = lv.match_in_search(collected, target, need)
        if kind and not exhaustive:
            if cache is not None:
                cache[key] = (collected, False)
            return kind
        if scroll < SEARCH_SCROLLS:
            try:
                page.mouse.wheel(0, random.randint(1400, 2200))
            except Exception:
                break
            sleep(random.uniform(0.6, 1.0))
    # Scrolled to the end (always, when several identical listings share this search):
    # the stored results are complete, so repeats are answered without another page load.
    if cache is not None:
        cache[key] = (collected, True)
    return lv.match_in_search(collected, target, need) or ""


def _open_link(page, target, sleep):
    if not _goto(page, lv.item_url(target["listingId"]), sleep):
        return lv.UNCLEAR
    try:
        page.wait_for_function(r"""(title) => {
            const text = document.body ? document.body.innerText.toLowerCase() : '';
            return text.includes(title.toLowerCase()) ||
                /listing isn't available|content isn't available|listing not found|log in|confirm your identity/.test(text);
        }""", arg=target["title"], timeout=5000)
        text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 20000) : ''") or ""
    except Exception:
        text = ""
    return lv.classify_item_page(page.url, text, target["title"])


def check_listings(debug_url, label, targets, log, sleep, on_result=None, on_detail=None):
    """One result per target, in order, from one checker browser."""
    results = []
    search_cache, result_cache, no_id_seen = {}, {}, {}
    repeats = {}
    for target in targets:
        if not target.get("listingId"):
            key = f"{target['title']}|{target.get('price', '')}"
            repeats[key] = repeats.get(key, 0) + 1
    if not any(target.get("listingId") or target.get("price") for target in targets):
        for _ in targets:
            if on_result:
                on_result()
        log(f"[{label} checker] No listing IDs or prices; this sample is unverified. No browser search performed.")
        return [lv.UNCLEAR] * len(targets)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            _goto(page, MARKETPLACE_HOME, sleep)
            blocked = _blocked_reason(page)
            if blocked:
                capture_diagnostics(page, f"checker_{label}", log)
                log(f"[{label} checker] {blocked}: cannot check listings with this account.")
                return [lv.ERROR] * len(targets)

            for number, target in enumerate(targets, 1):
                if on_detail:
                    on_detail(label, number, len(targets), target["title"], "Checking")
                try:
                    listing_id = target.get("listingId")
                    cache_key = listing_id or f"{target['title']}|{target.get('price', '')}"
                    if not listing_id and not target.get("price"):
                        state = lv.UNCLEAR
                        log(f"[{label} checker] No listing ID or price for '{target['title']}'; title-only matches are not verified.")
                    elif listing_id and listing_id in result_cache:
                        state = result_cache[listing_id]
                    else:
                        # Identical no-ID listings (same title and price) each need their own result card.
                        need = 1
                        if not listing_id:
                            need = no_id_seen[cache_key] = no_id_seen.get(cache_key, 0) + 1
                        # Search is the verdict: a listing counts as visible only if it shows in results.
                        exhaustive = not listing_id and repeats.get(cache_key, 0) > 1
                        kind = _search(page, target, sleep, search_cache, need=need, exhaustive=exhaustive)
                        if kind is None:
                            # A slow results page is not a miss: give it one more try before calling it unverified.
                            log(f"[{label} checker] Search page was slow for '{target['title']}'; trying once more...")
                            sleep(1.0)
                            kind = _search(page, target, sleep, search_cache, need=need, exhaustive=exhaustive)
                        if kind == "ID":
                            state = lv.SEARCH
                        elif kind == "TEXT":
                            state = lv.TEXT_MATCH
                        elif kind is None:
                            state = lv.UNCLEAR
                            log(f"[{label} checker] The search page did not finish loading; search visibility is unverified.")
                        elif listing_id:
                            state = _open_link(page, target, sleep)  # opens by link but not in search = LINK_ONLY
                        else:
                            state = lv.NOT_FOUND  # no ID, so there is no link to try
                            cards = search_cache.get(lv.search_url(target["title"]), ([], True))[0]
                            log(f"[{label} checker] '{target['title']}' {target.get('price', '')} not in search "
                                f"(saw: {lv.describe_results(cards)})")
                    if listing_id and state not in (lv.ERROR, lv.UNCLEAR):
                        result_cache[listing_id] = state
                    blocked = _blocked_reason(page)
                    if blocked:
                        sleep(3.0)  # a passing pop-up is not a block: look again before giving up
                        blocked = _blocked_reason(page)
                    if blocked:
                        capture_diagnostics(page, f"checker_{label}", log)
                        log(f"[{label} checker] account blocked mid-run ({blocked}); stopping this checker. "
                            "Open the profile by hand to see what Facebook is asking for.")
                        results.extend([lv.ERROR] * (len(targets) - len(results)))
                        break
                except AutomationStopped:
                    results.extend([lv.UNCLEAR] * (len(targets) - len(results)))
                    return results
                except Exception as exc:
                    log(f"[{label} checker] '{target['title']}': {exc}")
                    state = lv.ERROR
                results.append(state)
                log(f"[{label} checker] {number}/{len(targets)} {target['title']}: {state}")
                if on_result:
                    on_result()
                if on_detail:
                    on_detail(label, number, len(targets), target["title"], state)
                sleep(0.2)
        finally:
            try:
                page.close()
            except Exception:
                pass
    return results


def run_visibility_checks(posts, log, stop_event, master=None, progress=None, options=None, detail=None):
    """Check every LIVE listing of ``posts`` from each configured checker.

    Writes PK Check / US Check / Visibility / Visibility Details per row and
    returns ``{visibility code prefix: row count}`` for the end-of-run log.
    ``progress(done, total)`` is called after each listing check (any thread).
    """
    def sleep(seconds):
        if stop_event.wait(seconds):
            raise AutomationStopped("User manually stopped automation.")

    options = options or buyer_options()
    started_at = time.monotonic()
    checkers = configured_checkers(options)
    rows = []
    for post in posts:
        targets, skipped = lv.visibility_targets(post.get("_live_listings"), limit=options.limit)
        if targets and post.get("rowNumber"):
            rows.append((post, targets, skipped))
    if not checkers or not rows:
        return {}

    flat = [target for _, targets, _ in rows for target in targets]
    log(f"--- Visibility check: {len(flat)} LIVE listing(s) from {len(rows)} profile(s), "
        f"checkers: {', '.join(label for label, _ in checkers)} ---")

    total_checks = len(flat) * len(checkers)
    done = [0]
    lock = threading.Lock()

    def advance(count=1):
        with lock:
            done[0] += count
            current = min(done[0], total_checks)
        if progress:
            progress(current, total_checks)

    advance(0)
    per_label = {}
    counted = {}

    def run_one(label, profile):
        """One buyer account's pass. PK and US are different browsers, so they run side by side."""
        counted[label] = 0

        def on_result():
            counted[label] += 1
            advance()

        def count_remaining():
            remaining = len(flat) - counted[label]
            if remaining > 0:  # listings skipped by an early stop or failure
                counted[label] += remaining
                advance(remaining)

        if stop_event.is_set():
            return
        if not any(target.get("listingId") or target.get("price") for target in flat):
            log(f"[{label} checker] No listing IDs or prices in the sample; skipped browser launch.")
            per_label[label] = [lv.UNCLEAR] * len(flat)
            count_remaining()
            return
        log(f"[{label} checker] Starting profile '{profile}'...")
        from gui.ui_helpers import resolve_duplicates_ui
        started, data = start_profile(profile, duplicate_resolver=resolve_duplicates_ui, master=master, logger_callback=log)
        url = _debug_url(data) if started else ""
        if not url:
            log(f"[{label} checker] could not start: {data if not started else 'no CDP endpoint'}")
            per_label[label] = [lv.ERROR] * len(flat)
            count_remaining()
            if started:
                stop_profile(profile)
            return
        try:
            per_label[label] = check_listings(url, label, flat, log, sleep, on_result=on_result, on_detail=detail)
        except AutomationStopped:
            per_label[label] = [lv.UNCLEAR] * len(flat)
        except Exception as exc:
            log(f"[{label} checker] failed: {exc}")
            per_label[label] = [lv.ERROR] * len(flat)
        finally:
            count_remaining()
            if not stop_profile(profile):
                log(f"[{label} checker] Warning: the browser provider did not confirm the profile stopped.")
            stop_event.wait(0.2)

    def guarded(label, profile):
        try:
            run_one(label, profile)
        except Exception as exc:  # a launch failure on one buyer must not lose the other's results
            log(f"[{label} checker] failed: {exc}")
            per_label[label] = [lv.ERROR] * len(flat)

    with ThreadPoolExecutor(max_workers=max(1, len(checkers))) as pool:
        for future in [pool.submit(guarded, label, profile) for label, profile in checkers]:
            future.result()

    summary = {}
    offset = 0
    for post, targets, skipped in rows:
        own = {label: per_label.get(label, [lv.UNCLEAR] * len(flat))[offset:offset + len(targets)]
               for label, _ in checkers}
        offset += len(targets)
        # One entry per listing for the app's Buyer checks tab.
        post["_buyer_rows"] = [
            {"title": target["title"], "price": target.get("price", ""), "id": target.get("listingId", ""),
             **{label: own[label][index] if index < len(own[label]) else lv.UNCLEAR for label in own}}
            for index, target in enumerate(targets)
        ]
        values = lv.row_columns(targets, own, skipped, disabled={"PK", "US"} - set(own),
                                elapsed=time.monotonic() - started_at)
        if stop_event.is_set():
            values["Visibility"] = "STOPPED"
            values["Visibility Details"] = "Stopped by user. Partial/unverified results follow.\n" + values["Visibility Details"]
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
    log(f"Buyer sample finished in {int(time.monotonic() - started_at)}s; {len(flat)} target(s) per enabled buyer.")
    if stop_event.is_set():
        raise AutomationStopped("Buyer checks stopped; partial results saved.")
    return summary
