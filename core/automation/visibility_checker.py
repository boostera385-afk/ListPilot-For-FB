"""Buyer-side visibility check: other accounts search for each LIVE listing.

After owner scans finish (or using explicit targets when owner scans are off),
each checker profile opens once. For every target it searches Marketplace by title; when the
listing is not in the results it opens the listing link. Results are written
per seller row. Only an exact ID in search verifies search visibility; the
owner profile is never used to judge buyer visibility.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import sync_playwright

from core.config import load_config
from core.engine.exceptions import AutomationStopped
from core.automation import listing_visibility as lv
from core.automation.facebook_page import page_issue
from core.automation.browser_manager import start_profile, stop_profile, resolve_profile_id
from core.automation.live_checker import LIVE_CHECKER_SHEET, capture_diagnostics
from core.services.sheets_service import update_column_values

SEARCH_SCROLLS = 2
MARKETPLACE_HOME = "https://www.facebook.com/marketplace/"

_SEARCH_RESULTS_JS = r"""
() => {
  const main = document.querySelector('main,[role="main"]') || document.body;
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const cards = [];
  let recommendations = false;
  for (const el of main.querySelectorAll('h1,h2,h3,[role="heading"],a[href*="/marketplace/item/"]')) {
    if (!visible(el) || el.closest('[role="dialog"],nav,[role="complementary"]')) continue;
    if (!el.matches('a')) {
      if (/^(suggested|recommended|related|you (?:may|might) also like|more (?:items|listings|for you)|similar items|today.s picks|just for you|results outside)/i.test(el.innerText.trim()))
        recommendations = true;
      continue;
    }
    if (!recommendations) cards.push({
      href: el.getAttribute('href') || '',
      text: (el.innerText || el.getAttribute('aria-label') || '').slice(0, 300),
    });
  }
  return cards;
}
"""

_ITEM_HEADINGS_JS = r"""
() => {
  const main = document.querySelector('main,[role="main"]') || document.body;
  return Array.from(main.querySelectorAll('h1,[role="heading"][aria-level="1"]'))
    .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' &&
      !el.closest('nav,[role="dialog"],[role="complementary"]'))
    .map(el => el.innerText).slice(0, 1);
}
"""
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
        canonical = resolve_profile_id(str(identifier)) if identifier else None
        if not canonical and not options.owner_enabled:
            return f"Row {post.get('rowNumber', '?')}: enter the seller's exact Profile ID before running buyers only."
        if canonical in resolved:
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
    return page_issue(page)


def _goto(page, url, sleep, attempts=2):
    for attempt in range(attempts):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=12000)
            if response is not None and response.status >= 400:
                return False
            return True
        except AutomationStopped:
            raise
        except Exception:
            if attempt + 1 < attempts:
                sleep(2.0)  # bounded recovery for a Wi-Fi/IP change, never a visibility miss
    return False


def _search(page, target, sleep, cache=None, need=1, exhaustive=False, query=None):
    """``"ID"`` / ``"TEXT"`` when the listing shows in search, ``""`` when it does not,
    ``None`` when the results page never loaded (never a confident miss)."""
    key = lv.search_url(target["title"] if query is None else query)
    if cache is not None and key in cache:
        cached, sampled = cache[key]
        kind = lv.match_in_search(cached, target, need)
        if kind:
            return kind
        if sampled:
            return ""
    # The caller owns the one search retry; don't nest navigation retries.
    if not _goto(page, key, sleep, attempts=1):
        return None
    if not lv.matches_search_page(key, page.url) or _blocked_reason(page):
        return None  # login-wall/background cards are not usable search results
    try:
        page.wait_for_function(r"""() =>
            document.querySelector('a[href*="/marketplace/item/"], input[name="email"]') ||
            /\bno results\b|\bno listings found\b/i.test(document.body.innerText)
        """, timeout=5000)
    except Exception:
        return None  # never turn an unloaded page into a confident search miss
    collected = []
    empty = False
    for scroll in range(SEARCH_SCROLLS + 1):
        try:
            results = page.evaluate(_SEARCH_RESULTS_JS) or []
        except Exception:
            return None
        collected.extend(results)
        if not lv.matches_search_page(key, page.url) or _blocked_reason(page):
            return None
        kind = lv.match_in_search(collected, target, need)
        if kind and not exhaustive:
            if cache is not None:
                cache[key] = (collected, False)
            return kind
        if scroll < SEARCH_SCROLLS:
            try:
                page.mouse.wheel(0, random.randint(1400, 2200))
            except Exception:
                return None
            sleep(random.uniform(0.6, 1.0))
    if not collected:
        try:
            empty = page.evaluate(r"""() => /\bno results\b|\bno listings found\b/i.test(
                (document.querySelector('main,[role="main"]') || document.body).innerText)""") is True
        except Exception:
            return None
        if not empty:
            return None
    # Cache the bounded sample, not an assertion that all results were read.
    if cache is not None:
        cache[key] = (collected, True)
    return lv.match_in_search(collected, target, need) or ""


def _search_with_fallback(page, target, sleep, log, cache, need=1, exhaustive=False):
    for index, query in enumerate(lv.search_queries(target)):
        if index:
            log(f"Title search did not find the listing; trying keywords: '{query}'. "
                "The buyer's saved Marketplace location still applies.")
            kind = _search(page, target, sleep, cache, need=need, exhaustive=exhaustive, query=query)
        else:
            kind = _search(page, target, sleep, cache, need=need, exhaustive=exhaustive)
        if kind is None or kind:
            return kind
    return ""


def _open_link(page, target, sleep):
    requested = lv.item_url(target["listingId"])
    if not _goto(page, requested, sleep):
        return lv.UNCLEAR
    try:
        page.wait_for_function(
            f"() => ({_ITEM_HEADINGS_JS.strip()})().some(text => text.trim())", timeout=5000)
        headings = page.evaluate(_ITEM_HEADINGS_JS) or []
    except Exception:
        return lv.UNCLEAR
    return lv.classify_item_page(page.url, headings, target["title"], requested)


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
        if not browser.contexts:
            raise RuntimeError("Profile has no persistent browser context; refusing an unauthenticated buyer check.")
        context = browser.contexts[0]
        page = context.new_page()
        try:
            _goto(page, MARKETPLACE_HOME, sleep)
            blocked = _blocked_reason(page)
            if blocked:
                capture_diagnostics(page, f"checker_{label}", log)
                log(f"[{label} checker] {blocked}: cannot check listings with this account.")
                return [_checker_failure_state(blocked)] * len(targets)

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
                        kind = _search_with_fallback(page, target, sleep, log, search_cache, need=need, exhaustive=exhaustive)
                        if kind is None:
                            # Don't re-navigate an account checkpoint or sign-in wall.
                            if not _blocked_reason(page):
                                log(f"[{label} checker] Search for '{target['title']}' could not be read; trying once more...")
                                kind = _search_with_fallback(page, target, sleep, log, search_cache, need=need, exhaustive=exhaustive)
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
                            log(f"[{label} checker] '{target['title']}' {target.get('price', '')} not seen in sampled search "
                                f"(saw: {lv.describe_results(cards)})")
                    blocked = _blocked_reason(page)
                    if blocked:
                        sleep(3.0)  # a passing pop-up is not a block: look again before giving up
                        if blocked == lv.NETWORK_ERROR:
                            log(f"[{label} checker] Connection interrupted; retrying this page once...")
                            search_cache.pop(lv.search_url(target["title"]), None)
                            _goto(page, page.url, sleep)
                            state = lv.UNCLEAR  # pre-interruption cards cannot establish a verdict
                        blocked = _blocked_reason(page)
                    if blocked:
                        capture_diagnostics(page, f"checker_{label}", log)
                        hint = ("Restore the internet/proxy connection and re-run the check."
                                if blocked == lv.NETWORK_ERROR else
                                "Open this checker profile and sign in again." if blocked == "LOGGED_OUT" else
                                "Open this checker profile to complete Facebook's account check.")
                        log(f"[{label} checker] Check interrupted ({blocked}); stopping this checker. {hint} "
                            "This is not evidence that the seller listings are hidden or the account is banned.")
                        results.extend([_checker_failure_state(blocked)] * (len(targets) - len(results)))
                        break
                    if listing_id and state not in lv.UNVERIFIED_STATES:
                        result_cache[listing_id] = state
                except AutomationStopped:
                    results.extend([lv.UNCLEAR] * (len(targets) - len(results)))
                    return results
                except Exception as exc:
                    log(f"[{label} checker] '{target['title']}': {exc}")
                    state = lv.ERROR
                results.append(state)
                log(f"[{label} checker] {number}/{len(targets)} {target['title']}: {state}")
                if state == lv.SEARCH:
                    log(f"[{label} checker] Exact listing ID {target['listingId']} verified in Marketplace search.")
                if on_result:
                    on_result()
                if on_detail:
                    on_detail(label, number, len(targets), target["title"], state)
                if number < len(targets):
                    try:
                        sleep(0)
                    except AutomationStopped:
                        results.extend([lv.UNCLEAR] * (len(targets) - len(results)))
                        return results
        finally:
            try:
                page.close()
            except Exception:
                pass
    return results


def _checker_failure_state(reason):
    return {"LOGGED_OUT": lv.LOGIN_REQUIRED, "CHECKPOINT": lv.CHECKPOINT,
            lv.NETWORK_ERROR: lv.NETWORK_ERROR}.get(reason, lv.ERROR)


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
        if "_buyer_targets" in post:
            supplied = post["_buyer_targets"]
            targets, skipped = supplied[:options.limit], max(0, len(supplied) - options.limit)
        else:
            targets, skipped = lv.visibility_targets(post.get("_live_listings"), limit=options.limit)
        if targets and post.get("rowNumber"):
            rows.append((post, targets, skipped))
    if not checkers or not rows:
        return {}

    flat = [target for _, targets, _ in rows for target in targets]
    log(f"--- Visibility check: {len(flat)} target(s) from {len(rows)} profile(s), "
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
        started, data = start_profile(profile, duplicate_resolver=resolve_duplicates_ui, master=master,
                                     logger_callback=log, stop_event=stop_event)
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
            try:
                if not stop_profile(profile):
                    log(f"[{label} checker] Warning: the browser provider did not confirm the profile stopped.")
            except Exception as exc:
                log(f"[{label} checker] Warning: profile cleanup failed: {exc}")

    def guarded(label, profile):
        try:
            run_one(label, profile)
        except AutomationStopped:
            per_label[label] = [lv.UNCLEAR] * len(flat)
        except Exception as exc:  # a launch failure on one buyer must not lose the other's results
            log(f"[{label} checker] failed: {exc}")
            per_label[label] = [lv.ERROR] * len(flat)
        finally:
            remaining = len(flat) - counted.get(label, 0)
            if remaining > 0:
                counted[label] = len(flat)
                advance(remaining)

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
                                 elapsed=time.monotonic() - started_at, owner_scanned="_buyer_targets" not in post)
        if stop_event.is_set():
            values["Visibility"] = "STOPPED"
            values["PK vs US"] = "Not compared: run stopped"
            values["Visibility Details"] = "Stopped by user. Partial/unverified results follow.\n" + values["Visibility Details"]
        post["_visibility"] = values["Visibility"]
        post["_visibility_cols"] = values
        code = values["Visibility"].split(":")[0]
        summary[code] = summary.get(code, 0) + 1
        try:
            saved, message = update_column_values(LIVE_CHECKER_SHEET, post["rowNumber"], values)
            post["_visibility_saved"] = saved
            if not saved:
                log(f"Note: row {post['rowNumber']}: could not save visibility columns ({message}).")
        except Exception as exc:
            post["_visibility_saved"] = False
            log(f"Note: row {post['rowNumber']}: could not save visibility columns: {exc}")
    log(f"Buyer sample finished in {int(time.monotonic() - started_at)}s; {len(flat)} target(s) per enabled buyer.")
    if stop_event.is_set():
        raise AutomationStopped("Buyer checks stopped; partial results saved.")
    return summary
