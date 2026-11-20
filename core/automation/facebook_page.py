"""Read account/page health in one round trip, without changing the page."""
import re
from urllib.parse import urlsplit

from core.engine.exceptions import AutomationStopped


_PAGE_ISSUE_JS = r"""
() => {
  const visible = el => el && el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden';
  if (navigator.onLine === false) return 'NETWORK_ERROR';
  if (/^\/checkpoint(?:\/|$)/i.test(location.pathname)) return 'CHECKPOINT';
  if (/^\/login(?:\/|$)/i.test(location.pathname)) return 'LOGGED_OUT';
  if (Array.from(document.querySelectorAll('input[name="email"],input[name="pass"]')).some(visible))
    return 'LOGGED_OUT';
  const heading = Array.from(document.querySelectorAll('h1,h2,[role="heading"][aria-level="1"],[role="heading"][aria-level="2"]'))
    .filter(el => visible(el) && !el.closest('a[href*="/marketplace/item/"]'))
    .some(el => /^(confirm your identity|confirm you're a real person|account restricted|we suspended your account|your account has been locked)[.!?]?$/i.test(el.innerText.trim()));
  if (heading) return 'CHECKPOINT';
  if (/^chrome-error:/.test(location.href) &&
      /ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_NAME_NOT_RESOLVED|ERR_PROXY_CONNECTION_FAILED/.test(
        document.body ? document.body.innerText : '')) return 'NETWORK_ERROR';
  return '';
}
"""


def page_issue(page):
    try:
        result = page.evaluate(_PAGE_ISSUE_JS)
        if result in ("NETWORK_ERROR", "LOGGED_OUT", "CHECKPOINT"):
            return result
    except AutomationStopped:
        raise
    except Exception:
        pass
    try:
        path = urlsplit(page.url).path
        if re.match(r"^/checkpoint(?:/|$)", path, re.IGNORECASE):
            return "CHECKPOINT"
        if re.match(r"^/login(?:/|$)", path, re.IGNORECASE):
            return "LOGGED_OUT"
    except (TypeError, ValueError):
        pass
    return ""
