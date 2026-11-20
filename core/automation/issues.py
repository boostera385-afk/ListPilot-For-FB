"""One vocabulary for every account / connection problem a profile can hit.

Seller scans, buyer checks, the sheet and the app all describe a problem with
the same short label and the same next step, so a proxy failure never looks
like a login problem (or like a hidden listing). Pure helpers, unit tested.
"""
import re

PROXY_ERROR = "PROXY_ERROR"            # the profile's proxy refused / dropped the connection
NETWORK_ERROR = "NETWORK_ERROR"        # internet, DNS or connection reset (not proxy specific)
PAGE_TIMEOUT = "PAGE_TIMEOUT"          # Facebook did not load in time (slow proxy/connection)
LOGGED_OUT = "LOGGED_OUT"              # login page / "Continue as" prompt
TWO_FACTOR = "TWO_FACTOR"              # login code / approve from another device
CHECKPOINT = "CHECKPOINT"              # identity / security check
TEMP_BLOCKED = "TEMP_BLOCKED"          # "You're temporarily blocked" / feature limit
ACCOUNT_DISABLED = "ACCOUNT_DISABLED"  # disabled / suspended / locked account
NO_MARKETPLACE = "NO_MARKETPLACE"      # Marketplace not available to this account
PROFILE_START_FAILED = "PROFILE_START_FAILED"  # browser profile did not open
PROFILE_PROXY_FAILED = "PROFILE_PROXY_FAILED"  # profile could not open because of its proxy
BROWSER_CLOSED = "BROWSER_CLOSED"      # browser/page closed during the check

# code: (short label for the app/sheet, next step for the user, retry automatically?)
ISSUES = {
    PROXY_ERROR: ("Proxy failed",
                  "Check this profile's proxy in Incogniton (online, correct login, not expired), then retry.", True),
    NETWORK_ERROR: ("No connection",
                    "Check this computer's internet connection, then retry.", True),
    PAGE_TIMEOUT: ("Page too slow",
                   "Facebook did not load in time. Check the proxy speed or try fewer profiles at a time.", True),
    LOGGED_OUT: ("Login needed",
                 "Open this profile in Incogniton and log in to Facebook, then retry.", False),
    TWO_FACTOR: ("Login code needed",
                 "Open this profile and enter Facebook's login code / approve the login, then retry.", False),
    CHECKPOINT: ("Account check",
                 "Open this profile and complete Facebook's security check. Not retried automatically.", False),
    TEMP_BLOCKED: ("Temporarily blocked",
                   "Facebook temporarily limited this account. Wait several hours before checking it again.", False),
    ACCOUNT_DISABLED: ("Account disabled",
                       "Facebook disabled or suspended this account. Open the profile to review or appeal.", False),
    NO_MARKETPLACE: ("No Marketplace",
                     "Marketplace is not available to this account. Open the profile to confirm.", False),
    PROFILE_START_FAILED: ("Launch failed",
                           "Open Incogniton, make sure the profile is closed and starts manually, then retry.", True),
    PROFILE_PROXY_FAILED: ("Proxy failed at launch",
                           "Incogniton could not start the profile with its proxy. Fix or replace the proxy, then retry.",
                           False),
    BROWSER_CLOSED: ("Browser closed",
                     "The browser closed during the check. Keep the profile open while it runs, then retry.", True),
}

CONNECTION_CODES = (PROXY_ERROR, NETWORK_ERROR, PAGE_TIMEOUT)
ACCOUNT_CODES = (LOGGED_OUT, TWO_FACTOR, CHECKPOINT, TEMP_BLOCKED, ACCOUNT_DISABLED, NO_MARKETPLACE)

_PROXY_WORDS = re.compile(r"socks|proxy|tunnel|407|err_proxy|err_socks", re.IGNORECASE)


def label(code):
    return ISSUES.get(str(code).split(":")[0], (str(code),))[0]


def next_step(code):
    entry = ISSUES.get(str(code).split(":")[0])
    return entry[1] if entry else ""


def is_issue(code):
    return str(code).split(":")[0] in ISSUES


def from_net_error(code):
    """Chrome 'ERR_SOCKS_CONNECTION_FAILED' (etc.) -> issue code."""
    code = str(code or "").upper()
    if not code:
        return NETWORK_ERROR
    if _PROXY_WORDS.search(code):
        return PROXY_ERROR
    if "TIMED_OUT" in code or "TIMEOUT" in code:
        return PAGE_TIMEOUT
    return NETWORK_ERROR


def from_launch_error(message):
    """Incogniton/browser launch failure text -> issue code."""
    text = str(message or "")
    if _PROXY_WORDS.search(text):
        return PROFILE_PROXY_FAILED
    return PROFILE_START_FAILED


def from_exception(exc):
    """Playwright navigation/connection exception -> issue code, or '' when unrelated."""
    text = str(exc or "")
    match = re.search(r"net::(ERR_[A-Z_]+)", text)
    if match:
        return from_net_error(match.group(1))
    if re.search(r"Timeout \d+ms exceeded", text):
        return PAGE_TIMEOUT
    if re.search(r"Target (?:page, context or browser )?(?:has been )?closed|Browser has been closed", text,
                 re.IGNORECASE):
        return BROWSER_CLOSED
    return ""


def describe(code, detail=""):
    """'Proxy failed (ERR_SOCKS_CONNECTION_FAILED)' for logs and the sheet."""
    words = label(code)
    return f"{words} ({detail})" if detail else words
