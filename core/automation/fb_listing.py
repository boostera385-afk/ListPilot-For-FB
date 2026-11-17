import time
import os
import re
import html
import uuid
import tempfile
import requests
from core.engine.state_machine import AutomationState
from core.engine.exceptions import AutomationStopped, RetryableError, PermanentError
from playwright.sync_api import sync_playwright, Error as PlaywrightError
from core.utils.smart_match import best_fuzzy_index, loose_key

FB_HOME = "https://www.facebook.com/"
FB_MARKETPLACE_CREATE = "https://www.facebook.com/marketplace/create/item"
FB_MARKETPLACE_SELLING = "https://www.facebook.com/marketplace/you/selling"
PHOTO_RENDER_MIN_SECONDS = 1.0
MESSENGER_BADGE_MAX_WAIT_SECONDS = 5.0
MAX_CONNECTION_RESTARTS = 5
PUBLISH_CONFIRM_MAX_SECONDS = 90

# Any of these means Facebook accepted the listing: a redirect to the selling
# pages (old /marketplace/you, new /marketplace/seller) or the new item, the
# "Your listings" heading/table, or the "Listing published" popup.
_PUBLISH_SUCCESS_JS = r"""
() => {
  const href = String(location.href || "").toLowerCase();
  if (/facebook\.com\/marketplace\/(you|selling|seller|item\/)/.test(href)) return true;
  if (document.querySelector('table[aria-label="Your listings"]')) return true;
  if (Array.from(document.querySelectorAll("h1, h2")).some((h) => /your listings/i.test(h.innerText || ""))) return true;
  const toast = /(listing|item)\s+(?:was\s+|has been\s+|is\s+)?(?:published|posted|now live)|your listing is (?:now )?(?:live|pending|being reviewed|in review)/i;
  if (Array.from(document.querySelectorAll('[role="alert"], [role="status"], [aria-live]'))
    .some((node) => toast.test(node.innerText || ""))) return true;
  // The new UI's bottom-left popup is a plain span ("Listing published"),
  // not always inside an alert region.
  const popup = /^(?:your\s+)?listing\s+(?:was\s+|has been\s+)?(?:published|posted)\.?$/i;
  return Array.from(document.querySelectorAll("span")).some(
    (span) => span.childElementCount === 0 && popup.test((span.textContent || "").trim())
  );
}
"""
NEW_TAB_RECOVERY_ATTEMPTS = 4

_FB_OFFLINE_PATTERN = re.compile(
    r"(?:you(?:'|’)?re|you are)\s+(?:currently\s+)?offline|no internet connection",
    re.IGNORECASE,
)
_FB_RESTORED_PATTERN = re.compile(
    r"connection\s+(?:was|has been|is)\s+restored|(?:you(?:'|’)?re|you are)\s+back online",
    re.IGNORECASE,
)

# Reads the browser's own view of the connection plus Facebook's toast/alert
# text. Chrome error pages (proxy/DNS failures) have no Facebook UI at all.
_CONNECTION_STATE_JS = r"""
() => {
  const online = navigator.onLine !== false;
  const href = String(location.href || "");
  if (href.startsWith("chrome-error://") || document.getElementById("main-frame-error")) {
    const code = document.querySelector(".error-code");
    return {state: online ? "net_error" : "browser_offline", notices: code ? code.textContent : ""};
  }
  const parts = [];
  document.querySelectorAll('[role="alert"], [role="status"], [aria-live]').forEach((node) => {
    const text = (node.innerText || "").trim();
    if (text && text.length < 300) parts.push(text);
  });
  return {state: online ? "ok" : "browser_offline", notices: parts.join(" | ").slice(0, 4000)};
}
"""

# Collects every visible text/ARIA label that can carry the Messenger unread
# badge. The badge may sit inside the button or in a sibling wrapper, so the
# scope climbs a few levels but stops before it reaches other top-bar icons.
_MESSENGER_BADGE_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const ownLabel = /messenger|chats?\b|unread|message/i;
  const texts = [];
  const scopes = new Set();
  let found = false;
  const anchors = document.querySelectorAll(
    '[aria-label*="Messenger" i], [aria-label^="Chats" i], a[href*="/messages/"]'
  );
  for (const el of anchors) {
    if (!visible(el)) continue;
    found = true;
    texts.push(el.getAttribute("aria-label") || "");
    let scope = el;
    for (let level = 0; level < 3 && scope.parentElement; level++) {
      const parent = scope.parentElement;
      const foreign = Array.from(
        parent.querySelectorAll('[role="button"], [role="link"], a[href]')
      ).some((other) => {
        if (other === el || other.contains(el) || el.contains(other)) return false;
        return !ownLabel.test(other.getAttribute("aria-label") || other.textContent || "");
      });
      if (foreign) break;
      scope = parent;
    }
    if (scopes.has(scope)) continue;
    scopes.add(scope);
    scope.querySelectorAll("[aria-label], span, div").forEach((node) => {
      if (!visible(node)) return;
      const label = node.getAttribute("aria-label") || "";
      if (label && /\d/.test(label)) texts.push(label);
      if (node.childElementCount === 0) {
        const text = (node.textContent || "").trim();
        if (/^\d{1,3}\+?$/.test(text)) texts.push(text);
      }
    });
  }
  return {found, texts};
}
"""


def is_fresh_listing_card(card, expected_title, today=None):
    """True when a 'Your listings' card is this title and was listed today."""
    card = card or {}
    if not expected_title or loose_key(card.get("title")) != loose_key(expected_title):
        return False
    today = today or time.localtime()
    text = " ".join(str(card.get(key) or "") for key in ("date", "text"))
    listed = re.search(r"Listed on\s+(\d{1,2})/(\d{1,2})", text, re.IGNORECASE)
    if listed:
        return (int(listed.group(1)), int(listed.group(2))) == (today.tm_mon, today.tm_mday)
    return bool(re.search(
        r"just now|\b\d+\s*(?:m|min|minutes?|h|hrs?|hours?)\b(?:\s+ago)?|\btoday\b",
        text,
        re.IGNORECASE,
    ))


class ConnectionRecovered(Exception):
    """Facebook is reachable again; the listing flow restarts from the top."""


def is_connection_notice(text):
    """True for Facebook's offline/restored toasts (not form validation)."""
    value = str(text or "")
    return bool(_FB_OFFLINE_PATTERN.search(value) or _FB_RESTORED_PATTERN.search(value))


def read_messenger_unread_count(page, sleep, max_wait=MESSENGER_BADGE_MAX_WAIT_SECONDS):
    """Poll the Messenger badge until a count appears or ``max_wait`` passes.

    Facebook renders the badge a moment after the icon itself, so a single
    read right after the icon appears often reports zero. Returns ``None``
    when the Messenger control was never found, so callers never save a
    guessed zero.
    """
    deadline = time.monotonic() + max_wait
    found_control = False
    while True:
        try:
            result = page.evaluate(_MESSENGER_BADGE_JS) or {}
        except Exception:
            result = {}
        if result.get("found"):
            found_control = True
            count = max(
                (_extract_messenger_unread_count(text) for text in result.get("texts") or []),
                default=0,
            )
            if count > 0:
                return count
        if time.monotonic() >= deadline:
            return 0 if found_control else None
        sleep(0.5)

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_US_STATE_ABBREVIATIONS = set(_US_STATES.values())
_US_STATE_ABBREVIATION_PATTERN = "|".join(sorted(_US_STATE_ABBREVIATIONS))


def _normalize_option_label(value):
    return " ".join(str(value or "").split()).casefold()


def _extract_messenger_unread_count(*values):
    """Extract Facebook's unread Messenger count from badge/ARIA text."""
    for value in values:
        text = " ".join(str(value or "").split())
        if not text:
            continue

        for pattern in (
            r"(\d[\d,]*)\s+(?:unread|new)\b",
            r"\b(?:unread|new)\D{0,24}(\d[\d,]*)\b",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))

        compact = text.rstrip("+").replace(",", "")
        if compact.isdigit():
            return int(compact)
    return 0


def _remaining_photo_render_delay(upload_completed_at, now=None):
    """Return the remaining part of the one-second photo render window."""
    if upload_completed_at is None:
        return 0.0
    current = time.monotonic() if now is None else now
    return max(0.0, PHOTO_RENDER_MIN_SECONDS - (current - upload_completed_at))


def option_has_exact_label(value, container_text, nested_texts=None):
    """Return True when an option's primary line or nested label is exact.

    Facebook category buttons can contain a primary label plus secondary text,
    for example ``Garden`` and ``Shipping available`` in the same button.
    """
    target = _normalize_option_label(value)
    if not target:
        return False

    labels = [_normalize_option_label(text) for text in (nested_texts or [])]
    if target in labels:
        return True

    lines = [
        _normalize_option_label(line)
        for line in str(container_text or "").splitlines()
        if _normalize_option_label(line)
    ]
    return bool(lines and lines[0] == target)


def _extract_us_state(text):
    """Return a US state abbreviation from the location's first display line."""
    first_line = " ".join(str(text or "").splitlines()[0].split()) if text else ""
    abbreviation = re.search(
        rf",\s*({_US_STATE_ABBREVIATION_PATTERN})\b|"
        rf"(?:^|\s)({_US_STATE_ABBREVIATION_PATTERN})(?=\s+\d{{5}}(?:-\d{{4}})?\b|$)",
        first_line,
        re.IGNORECASE,
    )
    if abbreviation:
        return (abbreviation.group(1) or abbreviation.group(2)).upper()

    lowered = first_line.lower()
    for state_name, state_code in sorted(_US_STATES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"(?:,\s*|\b){re.escape(state_name)}\b", lowered):
            return state_code
    return None


def _location_terms(text):
    cleaned = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", str(text or "").lower())
    cleaned = re.sub(r"[^a-z]+", " ", cleaned)
    ignored = {"united", "states", "usa"}
    ignored.update(state.split()[0] for state in _US_STATES)
    ignored.update(code.lower() for code in _US_STATE_ABBREVIATIONS)
    return {token for token in cleaned.split() if len(token) > 2 and token not in ignored}


def select_us_location_option(requested_location, option_texts):
    """Choose the best fast US match from Facebook location suggestions."""
    requested = str(requested_location or "").strip()
    requested_zip_match = re.search(r"\b\d{5}(?:-\d{4})?\b", requested)
    requested_zip = requested_zip_match.group(0)[:5] if requested_zip_match else None
    requested_state = _extract_us_state(requested)
    requested_terms = _location_terms(requested)

    ranked = []
    for index, raw_text in enumerate(option_texts[:12]):
        display = " ".join(str(raw_text or "").split())
        first_line = " ".join(str(raw_text or "").splitlines()[0].split()) if raw_text else ""
        state = _extract_us_state(raw_text)
        us_marker = bool(re.search(r"\b(?:united states|u\.s\.a\.?|usa)\b", display, re.IGNORECASE))
        if not state and not us_marker:
            continue

        option_zip_match = re.search(r"\b\d{5}(?:-\d{4})?\b", first_line)
        option_zip = option_zip_match.group(0)[:5] if option_zip_match else None
        option_terms = _location_terms(first_line)
        term_matches = len(requested_terms.intersection(option_terms))

        # Facebook often returns "City, ST" without repeating the ZIP. Accept
        # that as a verified US fallback, but reject a visibly different ZIP.
        if (
            requested_zip
            and not requested_state
            and not requested_terms
            and option_zip
            and option_zip != requested_zip
        ):
            continue

        score = 40 if state else 0
        score += 15 if us_marker else 0
        if requested_zip:
            if option_zip == requested_zip:
                score += 220
            elif option_zip:
                score -= 120
            else:
                score += 10
        if requested_state:
            score += 100 if state == requested_state else -80
        score += term_matches * 35

        ranked.append((score, -index, index, first_line or display, state, option_zip))

    if not ranked:
        return None, "no suggestion contained a recognized US state"

    ranked.sort(reverse=True)
    score, _, index, display, state, option_zip = ranked[0]
    if score < 20:
        return None, "US suggestions did not match the requested city, state, or ZIP"

    matched = []
    if requested_zip and option_zip == requested_zip:
        matched.append(f"ZIP {requested_zip}")
    if state:
        matched.append(f"state {state}")
    return index, f"matched {', '.join(matched) if matched else 'US location'}: {display}"


def run_fb_listing_flow(
    debug_url,
    post=None,
    logger_callback=None,
    state_callback=None,
    stop_event=None,
):
    """
    Connects to a running browser profile (Incogniton/AdsPower) via Playwright and:
      1. Opens facebook.com (reuses an existing Facebook tab if one is open,
         otherwise opens it in a new tab).
      2. Verifies the profile is logged into Facebook.
      3. Briefly checks the Messenger response count.
      4. Opens the Marketplace "Create item" page directly.

    The actual listing form fill happens after this (added in a later step).
    """

    def log(msg):
        if logger_callback:
            logger_callback(msg)
        else:
            print(msg)

    def set_state(state, msg=""):
        if state_callback:
            state_callback(state, msg)

    def check_stop():
        if stop_event and stop_event.is_set():
            raise AutomationStopped("User manually stopped automation.")

    def interruptible_sleep(seconds):
        if stop_event is not None:
            if stop_event.wait(seconds):
                raise AutomationStopped("User manually stopped automation.")
        else:
            time.sleep(seconds)

    def wait_for_tab_load(p):
        """Waits for the page to fully load (DOM and Network) safely."""
        try:
            p.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            p.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        interruptible_sleep(0.3)

    def check_for_checkpoint(p):
        """Detects if the account is locked, restricted, or asking for identity confirmation."""
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
        """Detects the Facebook login screen."""
        try:
            login_selectors = [
                'input[name="email"]',
                '#email',
                '[data-testid="royal_email"]',
                'button[name="login"]',
                '[data-testid="royal_login_button"]',
            ]
            for sel in login_selectors:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            url = (page.url or "").lower()
            if "login" in url or "/checkpoint" in url or "recover" in url:
                return True
        except Exception:
            pass
        return False

    def is_logged_in(page):
        """Detects a logged-in Facebook session (feed/navigation present)."""
        selectors = [
            '[aria-label="Create a post"]',
            '[aria-label="Your profile"]',
            '[aria-label="Search Facebook"]',
            'div[role="feed"]',
            '[aria-label="Facebook"][role="navigation"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                pass
        return False

    page = None
    fallback_tabs = []

    def connection_state(pg):
        """Return ("ok" | "fb_offline" | "browser_offline" | "net_error", notices)."""
        try:
            info = pg.evaluate(_CONNECTION_STATE_JS) or {}
        except Exception:
            try:
                if (pg.url or "").lower().startswith("chrome-error://"):
                    return "net_error", ""
            except Exception:
                pass
            return "ok", ""
        state = info.get("state") or "ok"
        notices = str(info.get("notices") or "")
        if state != "ok":
            return state, notices
        if _FB_RESTORED_PATTERN.search(notices):
            return "ok", notices
        if _FB_OFFLINE_PATTERN.search(notices):
            return "fb_offline", notices
        try:
            if pg.get_by_text(_FB_OFFLINE_PATTERN).first.is_visible():
                return "fb_offline", notices
        except Exception:
            pass
        return "ok", notices

    def wait_until_back_online(pg, state):
        """Block until Facebook (or the browser) reports the connection is back.

        There is deliberately no time limit: the flow only continues once the
        page itself says it is online again. Stop still works at any moment.
        """
        if state == "fb_offline":
            log("Facebook says you are currently offline. Waiting until it says the connection is restored...")
        else:
            log("The browser is offline. Waiting until the connection comes back...")
        waiting_since = time.monotonic()
        next_reminder = 60
        while True:
            interruptible_sleep(1.0)
            current, notices = connection_state(pg)
            if _FB_RESTORED_PATTERN.search(notices):
                log("Facebook says your internet connection was restored.")
                return
            if current == "ok":
                log("Connection is back online.")
                return
            if current == "net_error":
                # The browser is online again but this page failed to load;
                # the refresh/new-tab recovery handles it from here.
                return
            if time.monotonic() - waiting_since >= next_reminder:
                log("Still offline. Waiting for the connection to be restored...")
                next_reminder += 60

    def settle_after_navigation(pg):
        try:
            pg.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        try:
            pg.wait_for_selector(
                '[aria-label*="Messenger" i]:visible, input[name="email"]:visible, '
                '#main-frame-error, input[type="file"][accept*="image"]',
                timeout=15000,
            )
        except Exception:
            pass

    def close_fallback_tabs():
        """Close tabs opened for recovery unless the profile stays open."""
        if post and post.get("_force_keep_open"):
            return
        for tab in fallback_tabs:
            try:
                if not tab.is_closed():
                    tab.close()
                    log("Closed the extra Facebook tab opened during connection recovery.")
            except Exception:
                pass

    def ensure_connection(where):
        """Wait out offline states, refresh once, and restart the flow.

        If the refresh does not bring Facebook back, Facebook is opened in a
        new tab and the flow restarts there. Raises ConnectionRecovered to
        restart, or RetryableError when Facebook cannot be reached at all.
        """
        nonlocal page
        state, _ = connection_state(page)
        if state == "ok":
            return
        log(f"Connection problem detected {where}.")
        if state in ("fb_offline", "browser_offline"):
            wait_until_back_online(page, state)

        log("Refreshing the page once...")
        try:
            page.reload(timeout=60000, wait_until="commit")
        except Exception:
            pass
        settle_after_navigation(page)
        state, _ = connection_state(page)
        if state == "ok":
            raise ConnectionRecovered("Connection restored and page refreshed.")

        for attempt in range(1, NEW_TAB_RECOVERY_ATTEMPTS + 1):
            check_stop()
            log(
                "Refresh did not fix the connection. Trying Facebook in a new tab "
                f"(attempt {attempt}/{NEW_TAB_RECOVERY_ATTEMPTS})..."
            )
            tab = next((t for t in fallback_tabs if not t.is_closed()), None)
            if tab is None:
                try:
                    tab = page.context.new_page()
                except Exception as exc:
                    log(f"Could not open a new tab: {exc}")
                    break
                fallback_tabs.append(tab)
            try:
                tab.bring_to_front()
                tab.goto(FB_HOME, timeout=60000, wait_until="commit")
            except Exception:
                pass
            settle_after_navigation(tab)
            tab_state, _ = connection_state(tab)
            if tab_state in ("fb_offline", "browser_offline"):
                wait_until_back_online(tab, tab_state)
                try:
                    tab.reload(timeout=60000, wait_until="commit")
                except Exception:
                    pass
                settle_after_navigation(tab)
                tab_state, _ = connection_state(tab)
            if tab_state == "ok":
                page = tab
                raise ConnectionRecovered("Facebook loaded in a new tab.")
            interruptible_sleep(min(5 * attempt, 20))

        raise RetryableError(
            "Facebook could not be reached after refreshing and opening a new tab "
            "(the proxy or internet connection may be down)."
        )

    def goto_checked(url, where):
        """Navigate, then route any offline/net-error result into recovery."""
        try:
            page.goto(url, timeout=60000, wait_until="commit")
        except Exception as exc:
            log(f"Navigation to {url} failed: {str(exc).splitlines()[0]}")
        ensure_connection(where)

    try:
        with sync_playwright() as p:
            log("Connecting to browser...")
            set_state(AutomationState.CDP_CONNECTING)

            max_retries = 5
            browser = None
            for attempt in range(max_retries):
                try:
                    browser = p.chromium.connect_over_cdp(debug_url)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        log(f"Browser not ready yet. Retrying in 3s ({attempt+1}/{max_retries})...")
                        interruptible_sleep(3)
                    else:
                        raise e

            context = browser.contexts[0] if browser.contexts else browser.new_context()

            prepared_photo_paths = None
            connection_restarts = 0
            publish_attempted = False
            try:
                while True:
                    try:
                        # 1. Reuse an existing Facebook tab if one is already open, else open a new tab.
                        # After a connection restart the recovered tab is kept.
                        check_stop()
                        set_state(AutomationState.PAGE_LOADING)
                        if page is not None and page.is_closed():
                            page = None
                        for pg in ([] if page is not None else context.pages):
                            try:
                                if "facebook.com" in (pg.url or "").lower():
                                    page = pg
                                    log("Found an existing Facebook tab. Reusing it.")
                                    break
                            except Exception:
                                pass

                        if page is None:
                            log("No Facebook tab open. Opening a new tab...")
                            page = context.new_page()

                        page.bring_to_front()

                        # Load the home navigation long enough to verify the session and
                        # let the Messenger badge render. There is intentionally no feed
                        # warm-up or scrolling before listing.
                        log("Opening Facebook to check the response count...")
                        try:
                            page.goto(FB_HOME, timeout=60000, wait_until="commit")
                        except Exception as exc:
                            log(f"Navigation to Facebook failed: {str(exc).splitlines()[0]}")

                        # Wait on Facebook's actual UI state instead of sleeping for a
                        # guessed amount of time. This returns the instant either the
                        # Messenger control, a login field, or a browser error page renders.
                        try:
                            page.wait_for_selector(
                                '[aria-label*="Messenger"]:visible, input[name="email"]:visible, '
                                '#email:visible, #main-frame-error',
                            )
                        except Exception:
                            pass
                        ensure_connection("while opening Facebook")

                        # 2. Verify the profile is logged in.
                        check_stop()
                        set_state(AutomationState.LOGIN_CHECK if hasattr(AutomationState, "LOGIN_CHECK") else AutomationState.PAGE_LOADING)
                        if is_logged_out(page) and not is_logged_in(page):
                            raise PermanentError("Facebook profile is not logged in. Please log in to this profile first.")
            
                        if check_for_checkpoint(page):
                            raise PermanentError("Confirm your identity")
                
                        log("Facebook session is logged in.")
                        set_state(AutomationState.READY)

                        # 3. Finish checking Messenger unread responses before navigating
                        # away. The badge renders a moment after the icon, so it is
                        # polled briefly and the read stops as soon as a count shows.
                        ensure_connection("before reading the Messenger count")
                        response_value = None
                        log("Checking Messenger response count...")
                        try:
                            page.locator('[aria-label*="Messenger" i]:visible').first.wait_for(
                                state="visible", timeout=15000
                            )
                        except Exception:
                            pass
                        unread_count = read_messenger_unread_count(page, interruptible_sleep)
                        if unread_count is None:
                            log("Could not find the Messenger icon, so the response count was not saved.")
                        else:
                            ensure_connection("while reading the Messenger count")
                            response_value = str(unread_count)
                            if unread_count > 0:
                                log(f"Found {unread_count} unread message(s) in Messenger!")
                                if post and post.get("_keep_profile_open", False):
                                    post["_force_keep_open"] = True
                                    log("Profile will be kept open so you can read the messages.")
                                elif post and not post.get("_keep_profile_open", False):
                                    log("Keep Profile Open is OFF. Will close profile after completion.")
                            else:
                                log("No unread messages found on the Messenger icon.")

                        # 4. Move directly to Marketplace and Create Item page.
                        check_stop()
                        log("Navigating directly to Marketplace Create Item page...")
                        try:
                            page.goto(FB_MARKETPLACE_CREATE, timeout=60000, wait_until="commit")
                        except Exception as exc:
                            log(f"Navigation to Create Item failed: {str(exc).splitlines()[0]}")

                        # Save only a count that was actually checked. A missing Messenger
                        # control must not silently overwrite the sheet with zero.
                        if response_value is not None and post and post.get("rowNumber"):
                            try:
                                from core.services.sheets_service import update_column_value
                                response_saved, response_message = update_column_value(
                                    "FB Listings",
                                    post.get("rowNumber"),
                                    "Response",
                                    response_value,
                                )
                                if not response_saved:
                                    log(f"Could not save Messenger response count: {response_message}")
                            except Exception as exc:
                                log(f"Could not save Messenger response count: {exc}")

                        def is_form_ready():
                            try:
                                loc = page.locator('input[type="file"], label:has-text("Title"), span:has-text("Photos")')
                                return loc.count() > 0 and loc.first.is_visible()
                            except Exception:
                                return False

                        try:
                            page.wait_for_selector(
                                'input[type="file"], span:has-text("Photos"), label:has-text("Title"), #main-frame-error',
                                timeout=5000,
                            )
                        except Exception:
                            pass
                        ensure_connection("while opening the Create Item page")
                        form_ready = is_form_ready()
                        if not form_ready:
                            log("Create Item form did not load yet. Retrying the direct URL...")
                            goto_checked(FB_MARKETPLACE_CREATE, "while reloading the Create Item page")
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=20000)
                                interruptible_sleep(0.1)
                            except Exception:
                                pass
                
                            try:
                                page.wait_for_selector('input[type="file"], label:has-text("Title")', timeout=15000)
                            except Exception:
                                pass
                            form_ready = is_form_ready()

                        if check_for_checkpoint(page):
                            raise PermanentError("Confirm your identity")

                        if form_ready:
                            log("Marketplace create item page is open and ready.")
                        else:
                            log("Form controls not confirmed yet. Proceeding anyway just in case...")

                        # ---------------------------------------------------------------
                        # Fill the "Item for sale" form.
                        # ---------------------------------------------------------------
                        from core.automation.profile_builder import get_val
                        # Reuse the exact same image handler as Auto Posting: it supports
                        # Google Drive links, direct image URLs, and local files (in the app
                        # folder or the "pictures"/"images" subfolders).
                        from core.automation.auto_poster import parse_and_download_images

                        def field_candidates(label):
                            """Return resilient locator candidates for a Marketplace field."""
                            esc = str(label).replace('"', '\\"')
                            exact_label = re.compile(rf"^\s*{re.escape(str(label))}\s*$", re.IGNORECASE)
                            return [
                                lambda: page.get_by_label(exact_label).first,
                                lambda: page.locator(
                                    f'input[aria-label="{esc}"], textarea[aria-label="{esc}"]'
                                ).first,
                                lambda: page.locator(
                                    f'label:has(span:text-is("{esc}")) input, '
                                    f'label:has(span:text-is("{esc}")) textarea'
                                ).first,
                                lambda: page.locator(
                                    f'label:has-text("{esc}") input, label:has-text("{esc}") textarea'
                                ).first,
                            ]

                        def get_field_value(label):
                            for getter in field_candidates(label):
                                try:
                                    loc = getter()
                                    if loc.count() > 0:
                                        return loc.input_value(timeout=1500).strip()
                                except Exception:
                                    continue
                            return None

                        def fill_field(label, value, fast=False):
                            """Fill and verify a text field using multiple Facebook layouts."""
                            if value is None or str(value).strip() == "":
                                return False
                            value = str(value).strip()
                            last_error = None
                            for getter in field_candidates(label):
                                try:
                                    loc = getter()
                                    loc.wait_for(state="visible", timeout=3500)
                                    loc.scroll_into_view_if_needed(timeout=2000)
                                    loc.click(timeout=3000)
                                    loc.fill(value, timeout=5000)
                                    actual = loc.input_value(timeout=1500).strip()
                                    if actual:
                                        return True
                                except Exception as exc:
                                    last_error = exc
                                    continue
                            log(f"Could not fill or verify '{label}': {last_error or 'field not found'}")
                            return False

                        def select_dropdown(label, value):
                            """Opens a combobox by its label and clicks the option matching value."""
                            if value is None or str(value).strip() == "":
                                return False
                            value = str(value)
                            combo = None
                            last_error = None
                            combo_getters = [
                                lambda: page.get_by_role(
                                    "combobox", name=re.compile(re.escape(label), re.IGNORECASE)
                                ).first,
                                lambda: page.locator(f'label[role="combobox"]:has-text("{label}")').first,
                                lambda: page.locator(f'[role="combobox"]:has-text("{label}")').first,
                            ]
                            for getter in combo_getters:
                                try:
                                    candidate = getter()
                                    candidate.wait_for(state="visible", timeout=3000)
                                    candidate.scroll_into_view_if_needed(timeout=2000)
                                    try:
                                        candidate.click(timeout=4000)
                                    except Exception:
                                        candidate.click(timeout=2500, force=True)
                                    combo = candidate
                                    interruptible_sleep(0.3)
                                    break
                                except Exception as exc:
                                    last_error = exc
                                    continue
                            if combo is None:
                                log(f"Could not open '{label}' dropdown: {last_error or 'control not found'}")
                                return False

                            # First inspect every visible clickable option. Facebook's
                            # category picker uses role=button rows whose accessible name
                            # can be "Garden Shipping available". Matching the primary
                            # line/nested span exactly selects Garden without confusing it
                            # with Garden Tools or a hidden duplicate.
                            role_candidates = page.locator(
                                'div[role="option"], div[role="menuitemradio"], '
                                'div[role="menuitem"], div[role="button"]'
                            )
                            try:
                                candidate_count = min(role_candidates.count(), 120)
                            except Exception:
                                candidate_count = 0

                            for index in range(candidate_count):
                                try:
                                    candidate = role_candidates.nth(index)
                                    if not candidate.is_visible():
                                        continue
                                    container_text = candidate.inner_text(timeout=1000)
                                    nested_texts = candidate.locator("span").all_inner_texts()
                                    if not option_has_exact_label(value, container_text, nested_texts):
                                        continue
                                    candidate.scroll_into_view_if_needed(timeout=2000)
                                    try:
                                        candidate.click(timeout=3000)
                                    except Exception:
                                        candidate.click(timeout=2000, force=True)
                                    interruptible_sleep(0.3)
                                    log(f"Selected exact {label} option: {str(value).strip()}")
                                    return True
                                except Exception:
                                    continue

                            # Forgive casing, punctuation and small typos in the sheet
                            # ("used like new" -> "Used - Like New", "Miscelaneous").
                            visible_options = []
                            for index in range(candidate_count):
                                try:
                                    candidate = role_candidates.nth(index)
                                    if not candidate.is_visible():
                                        continue
                                    lines = [
                                        line.strip()
                                        for line in candidate.inner_text(timeout=1000).splitlines()
                                        if line.strip()
                                    ]
                                    if lines:
                                        visible_options.append((candidate, lines[0]))
                                except Exception:
                                    continue
                            fuzzy_index = best_fuzzy_index(
                                value, [text for _, text in visible_options], cutoff=0.8
                            )
                            if fuzzy_index is not None:
                                candidate, option_text = visible_options[fuzzy_index]
                                try:
                                    candidate.scroll_into_view_if_needed(timeout=2000)
                                    try:
                                        candidate.click(timeout=3000)
                                    except Exception:
                                        candidate.click(timeout=2000, force=True)
                                    interruptible_sleep(0.3)
                                    log(f"Selected closest {label} option '{option_text}' for sheet value '{value.strip()}'.")
                                    return True
                                except Exception:
                                    pass

                            # Options may render as role=option/menuitem (Condition listbox)
                            # or as role=button rows (Category picker). The visible label is
                            # inside a nested <span>, so an exact span match is most reliable.
                            esc = value.replace('"', '\\"')
                            # Case-insensitive exact match (tolerates minor casing/spacing in the sheet).
                            ci_exact = re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
                            option_getters = [
                                lambda: page.locator(f'div[role="option"]:has(span:text-is("{esc}"))'),
                                lambda: page.locator(f'div[role="button"]:has(span:text-is("{esc}"))'),
                                lambda: page.locator(f'div[role="menuitemradio"]:has(span:text-is("{esc}"))'),
                                lambda: page.get_by_role("option", name=ci_exact),
                                lambda: page.get_by_role("menuitem", name=ci_exact),
                                lambda: page.get_by_role("button", name=ci_exact),
                                lambda: page.get_by_role("option", name=value, exact=True),
                                lambda: page.get_by_role("menuitem", name=value, exact=True),
                                lambda: page.get_by_role("button", name=value, exact=True),
                            ]
                            for getter in option_getters:
                                try:
                                    cand = getter().first
                                    cand.wait_for(state="visible", timeout=3000)
                                    cand.scroll_into_view_if_needed(timeout=2000)
                                    try:
                                        cand.click(timeout=3000)
                                    except Exception:
                                        cand.click(timeout=2000, force=True)
                                    interruptible_sleep(0.3)
                                    return True
                                except Exception:
                                    continue
                            log(f"Could not find option '{value}' for '{label}'.")
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            return False

                        photos_val = get_val(post or {}, "pictures", "picture", "photos", "photo", "images", "image", "picture link")
                        if prepared_photo_paths is None:
                            prepared_photo_paths = parse_and_download_images(photos_val, log)
                        photo_paths = prepared_photo_paths
                        title_val = get_val(post or {}, "title", "listing title", "item title", "item name")
                        price_val = get_val(post or {}, "price", "amount")
                        category_val = get_val(post or {}, "category")
                        condition_val = get_val(post or {}, "condition")
                        description_val = get_val(post or {}, "description", "desc", "details")
                        photo_upload_completed_at = None

                        def get_dropdown_text(label):
                            for getter in [
                                lambda: page.get_by_role(
                                    "combobox", name=re.compile(re.escape(label), re.IGNORECASE)
                                ).first,
                                lambda: page.locator(f'label[role="combobox"]:has-text("{label}")').first,
                                lambda: page.locator(f'[role="combobox"]:has-text("{label}")').first,
                            ]:
                                try:
                                    combo = getter()
                                    if combo.count() > 0:
                                        return combo.inner_text(timeout=1500).strip()
                                except Exception:
                                    continue
                            return None

                        def has_initial_form():
                            try:
                                if page.locator('input[type="file"][accept*="image"]').count() > 0:
                                    return True
                            except Exception:
                                pass
                            return get_field_value("Title") is not None or get_dropdown_text("Category") is not None

                        def uploaded_photo_count():
                            try:
                                file_input = page.locator('input[type="file"][accept*="image"]').first
                                if file_input.count() == 0:
                                    return None
                                return int(file_input.evaluate("el => el.files ? el.files.length : 0"))
                            except Exception:
                                return None

                        def upload_photos():
                            nonlocal photo_upload_completed_at
                            if not photo_paths:
                                return False
                            try:
                                file_input = page.locator('input[type="file"][accept*="image"]').first
                                file_input.wait_for(state="attached", timeout=10000)
                                file_input.set_input_files(photo_paths, timeout=15000)
                                photo_upload_completed_at = time.monotonic()
                                count = uploaded_photo_count()
                                if count and count > 0:
                                    log(f"Uploaded {count} photo(s).")
                                    return True
                                log("Photo upload completed but Facebook did not retain the files.")
                            except Exception as exc:
                                log(f"Could not upload photos: {exc}")
                            return False

                        def finish_photo_render_window():
                            """Gate Next while allowing form details to fill during the wait."""
                            nonlocal photo_upload_completed_at
                            remaining = _remaining_photo_render_delay(photo_upload_completed_at)
                            if remaining > 0:
                                log(f"Giving uploaded photo(s) {remaining:.1f}s more to render...")
                                interruptible_sleep(remaining)
                            photo_upload_completed_at = None

                        def visible_validation_messages():
                            messages = []
                            try:
                                alerts = page.locator('[role="alert"]:visible').all_inner_texts()
                                for text in alerts:
                                    cleaned = " ".join(str(text).split())
                                    if is_connection_notice(cleaned):
                                        continue
                                    if cleaned and cleaned not in messages:
                                        messages.append(cleaned[:160])
                            except Exception:
                                pass
                            try:
                                invalid = page.locator('[aria-invalid="true"]:visible')
                                for idx in range(min(invalid.count(), 6)):
                                    item = invalid.nth(idx)
                                    name = item.get_attribute("aria-label") or item.get_attribute("name") or "field"
                                    message = f"{name} is marked invalid by Facebook"
                                    if message not in messages:
                                        messages.append(message)
                            except Exception:
                                pass
                            return messages

                        def diagnose_initial_form():
                            issues = []
                            if not has_initial_form():
                                issues.append("Marketplace item form controls are not loaded")

                            if not photo_paths:
                                issues.append("Photos are missing or could not be resolved from the sheet")
                            else:
                                count = uploaded_photo_count()
                                if count is None:
                                    issues.append("Photo upload control is missing")
                                elif count < 1:
                                    issues.append("Photos were not accepted")

                            for label, expected in (("Title", title_val), ("Price", price_val)):
                                actual = get_field_value(label)
                                if not expected:
                                    issues.append(f"{label} is missing in the sheet")
                                elif actual is None:
                                    issues.append(f"{label} control is missing")
                                elif not actual.strip():
                                    issues.append(f"{label} was not accepted")

                            for label, expected in (("Category", category_val), ("Condition", condition_val)):
                                selected = get_dropdown_text(label)
                                if not expected:
                                    issues.append(f"{label} is missing in the sheet")
                                elif selected is None:
                                    issues.append(f"{label} control is missing")
                                elif not (
                                    loose_key(expected) in loose_key(selected)
                                    or best_fuzzy_index(expected, selected.splitlines(), cutoff=0.8) is not None
                                ):
                                    issues.append(f"{label} '{expected}' is not selected")

                            issues.extend(visible_validation_messages())
                            return list(dict.fromkeys(issues))

                        def apply_initial_form(force_reload=False):
                            """Load, fill, and verify the first Marketplace form step."""
                            check_stop()
                            ensure_connection("before filling the item form")
                            if force_reload or not has_initial_form():
                                log("Marketplace form controls are unavailable. Reloading the item form...")
                                goto_checked(FB_MARKETPLACE_CREATE, "while reloading the item form")
                                try:
                                    page.wait_for_selector(
                                        'input[type="file"][accept*="image"], '
                                        'input[aria-label="Title"], '
                                        'label:has(span:text-is("Title")) input',
                                        timeout=20000,
                                    )
                                except Exception:
                                    pass
                                wait_for_tab_load(page)

                            if photo_paths and (uploaded_photo_count() or 0) < 1:
                                upload_photos()
                            elif not photo_paths:
                                log("No photos were resolved from the sheet.")

                            if fill_field("Title", title_val):
                                log(f"Title set: {title_val}")
                            if fill_field("Price", price_val):
                                log(f"Price set: {price_val}")
                            if category_val and select_dropdown("Category", category_val):
                                log(f"Category set: {category_val}")
                            if condition_val and select_dropdown("Condition", condition_val):
                                log(f"Condition set: {condition_val}")
                            if description_val and fill_field("Description", description_val, fast=True):
                                log("Description set.")

                            # Upload starts first, so title, price, category, condition,
                            # and description are filled while Facebook renders photos.
                            # Only the remainder of the one-second window is waited here.
                            finish_photo_render_window()
                            return diagnose_initial_form()

                        initial_issues = apply_initial_form()
                        if initial_issues:
                            log("Initial form verification found: " + "; ".join(initial_issues))
                        else:
                            log("Initial Marketplace form verified successfully.")

                        def is_publish_visible():
                            """Check if the Publish button is currently visible on screen."""
                            for sel in [
                                'div[role="button"][aria-label="Publish"]',
                                'div[aria-label="Publish"][role="button"]',
                            ]:
                                try:
                                    btn = page.locator(sel).first
                                    if btn.is_visible():
                                        return True
                                except Exception:
                                    pass
                            return False

                        def click_next():
                            """Clicks an enabled 'Next' button; returns True on success."""
                            for sel in [
                                'div[role="button"][aria-label="Next"]',
                                'div[aria-label="Next"][role="button"]',
                            ]:
                                try:
                                    btn = page.locator(sel).first
                                    if btn.is_visible():
                                        btn.scroll_into_view_if_needed(timeout=2000)
                                        disabled = btn.get_attribute("aria-disabled") or ""
                                        if disabled.lower() == "true":
                                            log("Next button is disabled. A required field may be missing.")
                                            return False
                                        try:
                                            btn.click(timeout=5000)
                                        except Exception:
                                            btn.click(timeout=2500, force=True)
                                        return True
                                except Exception:
                                    continue
                            log("Could not find a visible Next button on this page.")
                            return False

                        def fill_location():
                            location_val = get_val(post or {}, "location", "city", "area", "place")
                            location_ok = True
                            try:
                                loc = page.locator('input[aria-label="Location"]').first
                                location_visible = loc.count() > 0 and loc.is_visible()
                            except Exception:
                                location_visible = False

                            if location_visible and not location_val:
                                log("Location is required on this step but is missing from the sheet.")
                                location_ok = False
                            elif location_visible and location_val:
                                try:
                                    loc.scroll_into_view_if_needed(timeout=2000)
                                    loc.click(timeout=5000)
                                    loc.fill("", timeout=3000)
                                    loc.press_sequentially(str(location_val), delay=60)
                                    interruptible_sleep(2.5)

                                    options = page.locator(
                                        'ul[role="listbox"] li[role="option"], '
                                        'div[role="listbox"] div[role="option"], '
                                        'div[role="option"]'
                                    )
                                    options.first.wait_for(state="visible", timeout=8000)

                                    option_texts = options.all_inner_texts()
                                    selected_index, match_reason = select_us_location_option(
                                        location_val,
                                        option_texts,
                                    )
                                    if selected_index is None:
                                        visible_options = [
                                            " ".join(str(text).split())[:100]
                                            for text in option_texts[:5]
                                        ]
                                        raise ValueError(
                                            f"No verified US location found ({match_reason}). "
                                            f"Suggestions: {visible_options}"
                                        )

                                    selected_option = options.nth(selected_index)
                                    selected_text = " ".join(selected_option.inner_text().split())
                                    try:
                                        selected_option.click(timeout=5000)
                                    except Exception:
                                        selected_option.click(timeout=2500, force=True)
                                    log(f"Location selected and US-verified: {selected_text} ({match_reason})")
                                    interruptible_sleep(1.0)
                                except Exception as exc:
                                    location_ok = False
                                    log(f"Could not set or verify Location '{location_val}': {exc}")
                
                            return location_ok

                        def diagnose_current_step():
                            issues = diagnose_initial_form() if has_initial_form() else []
                            location_val = get_val(post or {}, "location", "city", "area", "place")
                            try:
                                loc = page.locator('input[aria-label="Location"]').first
                                if loc.count() > 0 and loc.is_visible():
                                    current_location = loc.input_value(timeout=1500).strip()
                                    if not location_val:
                                        issues.append("Location is missing in the sheet")
                                    elif not current_location:
                                        issues.append(f"Location '{location_val}' was not accepted")
                            except Exception:
                                pass

                            issues.extend(visible_validation_messages())
                            if not issues and not is_publish_visible():
                                try:
                                    next_visible = page.locator(
                                        'div[role="button"][aria-label="Next"], '
                                        'div[aria-label="Next"][role="button"]'
                                    ).first.is_visible()
                                except Exception:
                                    next_visible = False
                                if not next_visible:
                                    issues.append("Facebook navigation controls are not loaded")
                            return list(dict.fromkeys(issues))

                        # Smart UI traversal: fill Location wherever it appears, leave all
                        # meetup preferences untouched, then click Next until Publish.
                        for step in range(4):
                            check_stop()
                
                            # Wait a moment for the current page to settle
                            interruptible_sleep(0.5)
                            ensure_connection(f"on form step {step + 1}")
                
                            # Fill location and meetup if they're on this page
                            fill_location()
                
                            # Check if we've reached the Publish button
                            if is_publish_visible():
                                log("Reached the final Publish step.")
                                break
                    
                            log(f"Clicking Next -> advancing to next step (step {step + 1})...")
                            if not click_next():
                                ensure_connection("after Next did not advance")
                                log("Next did not advance. Starting automatic form diagnosis and repair...")
                                repaired = False
                                remaining_issues = diagnose_current_step()
                                for repair_attempt in range(1, 3):
                                    check_stop()
                                    if remaining_issues:
                                        log(
                                            f"Repair attempt {repair_attempt}/2 detected: "
                                            + "; ".join(remaining_issues)
                                        )
                                    else:
                                        log(f"Repair attempt {repair_attempt}/2: refreshing the current form state.")

                                    on_initial_form = has_initial_form()
                                    if on_initial_form or step == 0:
                                        apply_initial_form(
                                            force_reload=(not on_initial_form or repair_attempt == 2)
                                        )

                                    fill_location()
                                    interruptible_sleep(1.0)
                                    remaining_issues = diagnose_current_step()

                                    if click_next():
                                        log(f"Automatic repair succeeded on attempt {repair_attempt}.")
                                        repaired = True
                                        break

                                if not repaired:
                                    remaining_issues = diagnose_current_step()
                                    detail = "; ".join(remaining_issues) if remaining_issues else "Facebook kept Next unavailable"
                                    return False, f"Could not advance after automatic form repair. Remaining issue(s): {detail}."
                
                            # Wait for the next page/step to load
                            interruptible_sleep(1.5)
                
                            # After clicking Next, wait for either the next form or the Publish button to appear
                            try:
                                page.wait_for_selector(
                                    'div[aria-label="Publish"][role="button"], '
                                    'div[aria-label="Next"][role="button"], '
                                    'input[aria-label="Location"], '
                                    'h1:has-text("Delivery method"), '
                                    'h1:has-text("List in more places")',
                                    timeout=10000,
                                )
                            except Exception:
                                pass

                        # ---------------------------------------------------------------
                        # Step 3 ("List in more places"): Publish.
                        # Leaves the default public Marketplace listing (no extra groups).
                        # ---------------------------------------------------------------
                        check_stop()

                        def click_publish():
                            for sel in [
                                'div[role="button"][aria-label="Publish"]',
                                'div[aria-label="Publish"][role="button"]',
                            ]:
                                try:
                                    btn = page.locator(sel).first
                                    if btn.is_visible():
                                        btn.click(timeout=5000)
                                        return True
                                except Exception:
                                    continue
                            return False

                        ensure_connection("before publishing")
                        publish_attempted = True
                        def confirm_listing_on_selling_page():
                            """Look for today's listing with this title on 'Your listings'."""
                            from core.automation.live_checker import (
                                FB_SELLER_LISTINGS,
                                SECTION_ALL_LISTINGS,
                                SECTION_NEEDS_ATTENTION,
                                _EXPAND_SECTION_JS,
                                _READ_CARDS_JS,
                                read_seller_listings,
                                table_row_is_fresh,
                            )
                            detail = "no listing cards were loaded"
                            for attempt in range(1, 4):
                                check_stop()
                                # New seller UI: the newest table rows.
                                try:
                                    page.goto(FB_SELLER_LISTINGS, wait_until="domcontentloaded", timeout=30000)
                                except Exception:
                                    pass
                                tables = read_seller_listings(page, interruptible_sleep)
                                if tables is not None:
                                    newest = tables[0][:2] + tables[1][:1]
                                    for row in newest:
                                        if table_row_is_fresh(row, title_val):
                                            return True, f"found the new listing '{row.get('title')}' ({row.get('status') or 'no status'})"
                                    if newest:
                                        detail = "newest listings were " + ", ".join(
                                            f"'{r.get('title')}' ({r.get('status') or 'no status'})" for r in newest
                                        )
                                    if attempt < 3:
                                        interruptible_sleep(3.0)
                                    continue
                                # Older UI: listing cards.
                                try:
                                    page.goto(FB_MARKETPLACE_SELLING, wait_until="domcontentloaded", timeout=30000)
                                except Exception:
                                    pass
                                try:
                                    page.wait_for_selector('[aria-label^="More options for"]', timeout=15000)
                                except Exception:
                                    pass
                                cards = []
                                for section_name in (SECTION_ALL_LISTINGS, SECTION_NEEDS_ATTENTION):
                                    try:
                                        if page.evaluate(_EXPAND_SECTION_JS, section_name):
                                            interruptible_sleep(0.5)
                                        section_cards = page.evaluate(_READ_CARDS_JS, section_name)
                                    except Exception:
                                        section_cards = []
                                    if isinstance(section_cards, list):
                                        cards.extend(section_cards[:3])
                                for card in cards:
                                    if is_fresh_listing_card(card, title_val):
                                        return True, f"found today's listing '{card.get('title')}'"
                                if cards:
                                    detail = "newest listings were " + ", ".join(
                                        f"'{c.get('title')}' ({c.get('date') or 'no date'})" for c in cards[:3]
                                    )
                                if attempt < 3:
                                    interruptible_sleep(3.0)
                            return False, detail

                        log("Publishing listing...")
                        if not click_publish():
                            log("Publish button is disabled or missing. Attempting to click 'Previous' to re-verify location...")
                            clicked_prev = False
                            for sel in ['div[role="button"][aria-label="Previous"]', 'div[aria-label="Previous"][role="button"]']:
                                try:
                                    prev_btn = page.locator(sel).first
                                    if prev_btn.is_visible():
                                        prev_btn.click(timeout=5000)
                                        clicked_prev = True
                                        break
                                except Exception:
                                    pass
                
                            if clicked_prev:
                                interruptible_sleep(2)
                                log("Re-filling location...")
                                fill_location()
                                interruptible_sleep(1)
                                log("Clicking Next to return to Publish step...")
                                click_next()
                                interruptible_sleep(2)
                    
                                log("Retrying Publish...")
                                if not click_publish():
                                    return False, "Failed: Publish button is still disabled after re-adding location. Please check manually."
                            else:
                                return False, "Failed: Publish button is disabled and could not find 'Previous' button to fix it."


                        log("Waiting for listing to publish and redirect to 'Your listings'...")
            
                        # Smart wait: return the moment Facebook shows any success
                        # signal. Busy servers can take well over 30s, so the wait runs
                        # up to PUBLISH_CONFIRM_MAX_SECONDS of online time; offline
                        # time does not count against it.
                        published = False
                        publish_wait_started = time.monotonic()
                        while time.monotonic() - publish_wait_started < PUBLISH_CONFIRM_MAX_SECONDS:
                            check_stop()
                            publish_conn_state, _ = connection_state(page)
                            if publish_conn_state in ("fb_offline", "browser_offline"):
                                wait_until_back_online(page, publish_conn_state)
                                publish_wait_started = time.monotonic()
                            try:
                                if page.evaluate(_PUBLISH_SUCCESS_JS):
                                    published = True
                                    break
                            except Exception:
                                pass
                            interruptible_sleep(0.5)

                        if not published:
                            # No redirect seen. Facebook sometimes publishes without
                            # leaving the form, so check the listings page itself.
                            log("Facebook did not redirect after Publish. Checking 'Your listings' for the new listing...")
                            published, confirm_detail = confirm_listing_on_selling_page()
                            log(f"Your listings check: {confirm_detail}.")

                        if not published:
                            log("Could not verify if listing published successfully (not found in 'Your listings').")
                            return False, "Failed to confirm publishing"

                        log("Listing verified as published successfully!")

                        if post and post.get("_run_live_checker", False):
                            log("Running Live Checker as requested...")
                            try:
                                from core.automation.live_checker import (
                                    FB_MARKETPLACE_SELLING,
                                    FB_SELLER_LISTINGS,
                                    SECTION_ALL_LISTINGS,
                                    SECTION_NEEDS_ATTENTION,
                                    read_first_listings,
                                    read_first_listings_old_ui,
                                    resolve_listing_from_table,
                                    summarize_first_listing,
                                )

                                live_result = None
                                responses = None
                                live_detail = "listing cards were not ready"
                                listing_summaries = None

                                # New seller UI: take the first Active and first Needs
                                # attention listing (clicks, messages, listed date).
                                try:
                                    page.goto(FB_SELLER_LISTINGS, wait_until="domcontentloaded", timeout=20000)
                                except Exception:
                                    pass
                                firsts = read_first_listings(page, interruptible_sleep, log)
                                use_old_layout = firsts is None
                                if use_old_layout:
                                    log("New listings table not found. Using the older 'Your listings' layout...")
                                    try:
                                        page.goto(FB_MARKETPLACE_SELLING, wait_until="domcontentloaded", timeout=20000)
                                    except Exception:
                                        pass
                                    interruptible_sleep(1.0)
                                    firsts = read_first_listings_old_ui(page, interruptible_sleep, log) or (None, None)

                                # Both layouts give the first listing of each section;
                                # retry while Facebook has not moved the new one to the top.
                                for attempt in range(1, 4):
                                    check_stop()
                                    active_first, needs_first = firsts or (None, None)
                                    if active_first or needs_first:
                                        listing_summaries = (
                                            summarize_first_listing(active_first, SECTION_ALL_LISTINGS)
                                            if active_first else "No active listings",
                                            summarize_first_listing(needs_first, SECTION_NEEDS_ATTENTION),
                                        )
                                    live_result, responses, live_detail = resolve_listing_from_table(
                                        title_val,
                                        [active_first] if active_first else [],
                                        [needs_first] if needs_first else [],
                                    )
                                    if live_result:
                                        break
                                    if attempt < 3:
                                        log(
                                            f"Live check attempt {attempt}/3: the first listings are not the new one yet; "
                                            "waiting briefly for Facebook to update them..."
                                        )
                                        interruptible_sleep(2.0)
                                        listings_url = FB_MARKETPLACE_SELLING if use_old_layout else FB_SELLER_LISTINGS
                                        reader = read_first_listings_old_ui if use_old_layout else read_first_listings
                                        try:
                                            page.goto(listings_url, wait_until="domcontentloaded", timeout=20000)
                                        except Exception:
                                            pass
                                        firsts = reader(page, interruptible_sleep, log) or (None, None)

                                if listing_summaries:
                                    log(f"All listings (first): {listing_summaries[0]}")
                                    log(f"Needs attention (first): {listing_summaries[1]}")

                                if not live_result:
                                    live_result = "Check Failed"
                                log(f"Live Check Result: {live_result} ({live_detail}).")
                                if responses is not None:
                                    log(f"Responses (messages on this listing): {responses}")

                                if post.get("rowNumber"):
                                    from core.services.sheets_service import update_column_value
                                    live_saved, live_message = update_column_value(
                                        "FB Listings",
                                        post.get("rowNumber"),
                                        "Live",
                                        live_result,
                                    )
                                    if not live_saved:
                                        log(f"Published successfully, but the Live result could not be saved: {live_message}")
                                    # Only save a count Facebook actually showed.
                                    if responses is not None:
                                        responses_saved, responses_message = update_column_value(
                                            "FB Listings",
                                            post.get("rowNumber"),
                                            "Responses",
                                            str(responses),
                                        )
                                        if not responses_saved:
                                            log(f"Could not save the Responses count: {responses_message}")
                                    if listing_summaries:
                                        for column, value in zip(("All Listings", "Needs Attention"), listing_summaries):
                                            saved, message = update_column_value(
                                                "FB Listings", post.get("rowNumber"), column, value
                                            )
                                            if not saved:
                                                log(f"Could not save the '{column}' column: {message}")
                            except AutomationStopped:
                                log("Listing was published, but the optional Live check was stopped. It will not be reposted.")
                            except Exception as e:
                                log(f"Listing was published, but the optional Live check could not finish: {e}")

                        set_state(AutomationState.SUCCESS)
                        return True, "Published Marketplace listing" 
                    except ConnectionRecovered as recovered:
                        connection_restarts += 1
                        if connection_restarts > MAX_CONNECTION_RESTARTS:
                            return False, (
                                "Facebook connection kept dropping. Gave up after "
                                f"{MAX_CONNECTION_RESTARTS} automatic restarts."
                            )
                        log(f"{recovered} Starting the listing again from the beginning...")
                    except PlaywrightError as exc:
                        # The Facebook tab was closed (crash or closed by hand) but the
                        # browser is still running: start over in a fresh tab. Never
                        # after Publish was clicked, so a listing can't be posted twice.
                        if (
                            "has been closed" not in str(exc)
                            or publish_attempted
                            or not browser.is_connected()
                        ):
                            raise
                        connection_restarts += 1
                        if connection_restarts > MAX_CONNECTION_RESTARTS:
                            raise
                        log("The Facebook tab was closed unexpectedly. Opening a new tab and starting again...")
                        page = None
            finally:
                close_fallback_tabs()

    except AutomationStopped as e:
        log(f"Stopped: {e}")
        raise
    except PermanentError as e:
        log(f"FB Listing failed: {e}")
        return False, str(e)
    except Exception as e:
        log(f"Error during FB Listing: {str(e)}")
        return False, str(e)
