"""Read account/page health in one round trip, without changing the page."""
import re
from urllib.parse import urlsplit

from core.engine.exceptions import AutomationStopped
from core.automation.issues import is_issue


_PAGE_ISSUE_JS = r"""
() => {
  const visible = el => el && el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden';
  const path = location.pathname;
  // Chrome's own error page: proxy/SOCKS failure, timeout, DNS, reset. Never a Marketplace result.
  if (/^chrome-error:/.test(location.href)) {
    const code = ((document.body ? document.body.innerText : '').match(/\b(?:net::)?(ERR_[A-Z_]+)\b/) || [])[1] || '';
    if (/SOCKS|PROXY|TUNNEL/.test(code)) return 'PROXY_ERROR';
    if (/TIMED_OUT/.test(code)) return 'PAGE_TIMEOUT';
    return 'NETWORK_ERROR';
  }
  if (navigator.onLine === false) return 'NETWORK_ERROR';
  // URL routes work in every Facebook language.
  if (/^\/(?:two_step_verification|checkpoint\/\?.*two_factor|login\/(?:device-based\/)?approvals?)/i.test(path + location.search))
    return 'TWO_FACTOR';
  if (/^\/checkpoint\/(?:disabled|\d+\/?$)/i.test(path) || /^\/(?:disabled|help\/contact\/.*disabled)/i.test(path))
    return 'ACCOUNT_DISABLED';
  if (/^\/checkpoint(?:\/|$)/i.test(path)) return 'CHECKPOINT';
  if (/^\/(?:login|recover)(?:\/|\.php|$)/i.test(path)) return 'LOGGED_OUT';
  const headings = Array.from(document.querySelectorAll(
      'h1,h2,[role="heading"][aria-level="1"],[role="heading"][aria-level="2"],[role="dialog"] [role="heading"]'))
    .filter(el => visible(el) && !el.closest('a[href*="/marketplace/item/"]'))
    .map(el => el.innerText.trim().replace(/[.!?]+$/, ''));
  const said = re => headings.some(text => re.test(text));
  if (said(/^(enter (?:the |your )?(?:login |security )?code|two-factor authentication required|check your notifications on another device|approve (?:your|this) login|go to your authentication app)/i))
    return 'TWO_FACTOR';
  if (said(/^(your account (?:has been|was) (?:disabled|suspended)|we suspended your account|account disabled|your account is disabled|your account has been locked|account restricted|your account is restricted)/i))
    return 'ACCOUNT_DISABLED';
  if (said(/^(you(?:'re| are) temporarily blocked|you can't use this feature (?:right )?now|you(?:'ve| have) been (?:temporarily )?blocked|you're going too fast)/i))
    return 'TEMP_BLOCKED';
  if (said(/^(confirm your identity|confirm you're a real person|help us confirm it's you|security check|we noticed unusual activity|confirm it's you)/i))
    return 'CHECKPOINT';
  if (said(/^(marketplace isn't available to you|you can't use marketplace|marketplace (?:is|isn't) not available|marketplace access (?:has been )?(?:restricted|removed))/i))
    return 'NO_MARKETPLACE';
  if (Array.from(document.querySelectorAll('input[name="email"],input[name="pass"]')).some(visible))
    return 'LOGGED_OUT';
  return '';
}
"""


_NET_ERROR_CODE_JS = r"""
() => {
  if (!/^chrome-error:/.test(location.href)) return '';
  const match = (document.body ? document.body.innerText : '').match(/\b(?:net::)?(ERR_[A-Z_]+)\b/);
  return match ? match[1] : 'ERR_UNKNOWN';
}
"""

_CONTINUE_AS_JS = r"""
() => {
  // Facebook's remembered-account prompt: a dialog with the account photo and
  // "Continue as <name>" (localized). Click only that primary button, never a
  // password form or "Use another profile".
  const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  for (const dialog of document.querySelectorAll('[role="dialog"]')) {
    if (!visible(dialog) || dialog.querySelector('input[type="password"],input[name="pass"]')) continue;
    if (!dialog.querySelector('img,svg image')) continue;
    const buttons = Array.from(dialog.querySelectorAll('[role="button"],button'))
      .filter(el => visible(el) && (el.innerText || '').trim());
    // The dialog's first line is the account name; the localized "Continue as <name>"
    // button repeats it ("Kevin Flair کے طور پر جاری رکھیں").
    const name = ((dialog.innerText || '').split('\n').map(s => s.trim()).find(Boolean) || '');
    const english = buttons.find(el => /^continue as\b/i.test(el.innerText.trim()));
    const named = name.length > 1 ? buttons.filter(el => el.innerText.trim() !== name && el.innerText.includes(name)) : [];
    const target = english || (named.length === 1 ? named[0] : null);
    if (target) { target.click(); return (target.innerText || '').trim().slice(0, 80); }
  }
  return '';
}
"""


def network_error_code(page):
    """'ERR_SOCKS_CONNECTION_FAILED' etc. when the tab shows Chrome's error page, else ''."""
    try:
        return page.evaluate(_NET_ERROR_CODE_JS) or ""
    except AutomationStopped:
        raise
    except Exception:
        return ""


def describe_network_error(code):
    """Plain words for a Chrome network error code."""
    code = str(code or "")
    if "SOCKS" in code or "PROXY" in code or "TUNNEL" in code:
        return f"proxy connection failed ({code})"
    if "TIMED_OUT" in code or "TIMEOUT" in code:
        return f"page timed out ({code})"
    return f"connection failed ({code})" if code else "connection failed"


def continue_remembered_account(page, wait):
    """Click Facebook's own "Continue as <name>" prompt once; True if the login wall cleared.

    ``wait(seconds)`` is the caller's interruptible sleep. No credentials are typed.
    """
    clicked = ""
    for _ in range(10):  # the prompt renders a few seconds after the login page
        try:
            clicked = page.evaluate(_CONTINUE_AS_JS)
        except AutomationStopped:
            raise
        except Exception:
            clicked = ""
        if clicked:
            break
        wait(1.0)
    if not clicked:
        return False
    for _ in range(15):
        wait(1.0)
        if page_issue(page) != "LOGGED_OUT":
            return True
    return False


def page_issue(page):
    try:
        result = page.evaluate(_PAGE_ISSUE_JS)
        if result and is_issue(result):
            return result
    except AutomationStopped:
        raise
    except Exception:
        pass
    try:
        if str(page.url).startswith("chrome-error:"):
            return "NETWORK_ERROR"
        path = urlsplit(page.url).path
        if re.match(r"^/checkpoint(?:/|$)", path, re.IGNORECASE):
            return "CHECKPOINT"
        if re.match(r"^/login(?:/|$)", path, re.IGNORECASE):
            return "LOGGED_OUT"
    except (TypeError, ValueError):
        pass
    return ""
