import os
import re
import time
import html
import random
from playwright.sync_api import sync_playwright
from core.engine.state_machine import AutomationState
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped
from core.services.sheets_service import update_column_value, update_column_values, append_live_history
from core.utils.smart_match import loose_key
from core.utils import layout_memory
from core.automation import listing_status as ls
from core.automation import listing_visibility as lv
from core.automation.facebook_page import page_issue
from core.config import load_config

FB_MARKETPLACE_HOME = "https://www.facebook.com/marketplace/?ref=bookmark"
FB_MARKETPLACE_SELLING = "https://www.facebook.com/marketplace/you/selling"
DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "debug")
MAX_DEBUG_FILES = 50
MAX_SCAN_ROWS = 400  # per tab; a safety cap for very large accounts
SCAN_BUDGET_SECONDS = 240  # full scan time limit per profile, so one profile cannot hang a worker
# Facebook's newer seller UI: a "Your listings" table with status tabs.
FB_SELLER_LISTINGS = "https://www.facebook.com/marketplace/seller/listings/"
LIVE_CHECKER_SHEET = "Live Checker"

SECTION_NEEDS_ATTENTION = "Needs attention"
SECTION_ALL_LISTINGS = "All listings"


# ---------------------------------------------------------------------------
# Pure parsing helpers (no browser needed, unit tested)
# ---------------------------------------------------------------------------

def _clean(value):
    return " ".join(html.unescape(str(value or "")).replace("\xa0", " ").split())


def extract_listing_clicks(text):
    """Return Facebook's click count in a consistent sheet-friendly format."""
    match = re.search(r"\b(\d[\d,]*)\s+clicks?\s+on\s+listing\b", str(text or ""), re.IGNORECASE)
    if not match:
        return None
    count = int(match.group(1).replace(",", ""))
    return f"{count} click" if count == 1 else f"{count} clicks"


def extract_listing_date(text):
    """Return the date shown on a card or dialog.

    All-listings cards say ``Listed on 10/5``; the "Your Listing" dialog opened
    from a Needs-attention card says ``Posted a week ago``.
    """
    text = str(text or "")
    listed = re.search(r"Listed on\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text, re.IGNORECASE)
    if listed:
        return f"Listed on {listed.group(1)}"
    posted = re.search(r"\bPosted\s+([^\n\r]+)", text)
    if posted:
        value = _clean(posted.group(1))
        if value:
            return f"Posted {value}"
    return None


def parse_listing_dialog_text(text):
    """Parse the text of Facebook's "Your Listing" dialog.

    Returns a dict with ``reason`` (the dialog's headline, e.g. "This Listing
    Goes Against Our Rules"), ``date`` and ``clicks`` - any of which may be None.
    """
    lines = [_clean(line) for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    reason = None
    for line in lines:
        if line.lower() in ("your listing", "close"):
            continue
        reason = line
        break
    return {
        "reason": reason,
        "date": extract_listing_date(text),
        "clicks": extract_listing_clicks(text),
    }


def parse_old_card(card):
    """Fields of an older-UI listing card (/marketplace/you/selling).

    Card text looks like::

        We rejected this listing for breaking our rules. See options.
        rOOF rePAIR & iNSTALLATiON
        $50
        Active · Listed on 9/26
        Listed on Marketplace · 0 clicks on listing

    The line(s) above the title are Facebook's notice for the listing.
    """
    card = card or {}
    title = _clean(card.get("title"))
    text = str(card.get("text") or "")
    lines = [line for line in (_clean(item) for item in text.splitlines()) if line]
    title_at = next((i for i, line in enumerate(lines) if _same_title(line, title)), None)
    notice = " ".join(lines[:title_at]) if title_at else ""
    # Keep the first sentence: "We rejected this listing for breaking our rules".
    reason = re.split(r"(?<=[.!?])\s+", notice)[0].rstrip(".") if notice else ""

    status = ""
    for index, line in enumerate(lines):
        match = re.match(r"^(.+?)\s*·\s*Listed on\s+\d", line, re.IGNORECASE)
        if match:
            status = match.group(1)
            break
        # Facebook often places Active, NBSP and "· Listed on ..." in separate nodes.
        if index + 1 < len(lines) and re.match(r"^[·•]?\s*Listed on\s+\d", lines[index + 1], re.IGNORECASE):
            if ls.classify_status("", line) != ls.UNKNOWN:
                status = line
                break
    if not status and any(re.match(r"^Listed on Marketplace\b", line, re.IGNORECASE) for line in lines):
        status = "Listed on Marketplace"
    if _is_in_review(reason):
        status = "In review"

    clicks = re.search(r"(\d[\d,]*)\s+clicks?\s+on\s+listing", text, re.IGNORECASE)
    price = next((line for line in lines if re.match(r"^([$€£₹]\s?[\d,.]+|free)$", line, re.IGNORECASE)), "")
    return {
        "title": title,
        "reason": reason,
        "status": status,
        "price": price,
        "clicks": clicks.group(1) if clicks else "",
        # The card's own date; replaced by "Posted ..." once the listing is opened.
        "listed": extract_listing_date(text) or "",
    }


# --- New seller UI ("Your listings" table) ---------------------------------

def parse_count(value):
    """Return the integer in a table cell like '6' or '1,204', else None."""
    match = re.search(r"\d[\d,]*", str(value or ""))
    return int(match.group(0).replace(",", "")) if match else None


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _same_title(a, b):
    return bool(a) and loose_key(a) == loose_key(b)


def _is_in_review(status):
    return bool(re.search(r"in review|being reviewed|pending|processing", str(status or ""), re.IGNORECASE))


def table_row_is_fresh(row, expected_title):
    """True when a table row is plausibly the listing that was just published.

    The table has no dates, so a fresh row is this title and either still in
    review or with no clicks, saves or messages yet.
    """
    row = row or {}
    if not _same_title(row.get("title"), expected_title):
        return False
    if _is_in_review(row.get("status")):
        return True
    return all(parse_count(row.get(key)) in (0, None) for key in ("clicks", "saves", "messages"))


def resolve_listing_from_table(expected_title, active_rows, needs_rows):
    """Classify the just-published listing from the new table.

    Returns ``(live_result, responses, detail)``; ``live_result`` is None when
    the newest rows do not match the title yet. ``responses`` is the
    listing's Messages count (or None when it was not shown).
    """
    if not _clean(expected_title):
        return None, None, "the published title is empty"
    active_latest = (active_rows or [None])[0]
    needs_latest = (needs_rows or [None])[0]

    def responses_of(row):
        return parse_count(row.get("messages"))

    # A newest Active row still in review is certainly the fresh listing, even
    # if an older listing with the same title sits in Needs attention.
    if active_latest and _same_title(active_latest.get("title"), expected_title) and _is_in_review(active_latest.get("status")):
        return "In review", responses_of(active_latest), "the newest listing is still in review"

    if needs_latest and _same_title(needs_latest.get("title"), expected_title):
        return "Not Live", responses_of(needs_latest), "the newest listing is in Needs attention"

    if active_latest and _same_title(active_latest.get("title"), expected_title):
        clicks = parse_count(active_latest.get("clicks"))
        status = _clean(active_latest.get("status"))
        if status and status.lower() != "active":
            result = status
        elif clicks is not None:
            result = _plural(clicks, "click")
        else:
            result = "Live (click count unavailable)"
        return result, responses_of(active_latest), "the newest listing is active"

    latest = []
    if active_latest:
        latest.append(f"Active: {active_latest.get('title') or 'unknown'}")
    if needs_latest:
        latest.append(f"Needs attention: {needs_latest.get('title') or 'unknown'}")
    detail = "; ".join(latest) if latest else "no listing rows were loaded"
    return None, None, f"the newest row did not match '{expected_title}' ({detail})"


def summarize_first_listing(row, section):
    """One line for the first listing of a tab, e.g.
    'Title - Active - 0 clicks - 1 message - Listed a day ago (Hilliard, OH)'.
    """
    if not row:
        return "None"
    parts = [_clean(row.get("title")) or "Untitled"]
    reason = _clean(row.get("reason"))
    status = _clean(row.get("status"))
    if reason and status and status.lower() not in reason.lower() and not (
        _is_in_review(status) and _is_in_review(reason)
    ):
        status = f"{status} ({reason})"
    else:
        status = reason or status
    if not status and section == SECTION_NEEDS_ATTENTION:
        status = "Needs attention"
    if status:
        parts.append(status)
    clicks = parse_count(row.get("clicks"))
    messages = parse_count(row.get("messages"))
    if clicks is not None:
        parts.append(_plural(clicks, "click"))
    if messages is not None:
        parts.append(_plural(messages, "message"))
    listed = _clean(row.get("listed"))
    if listed:
        listed = listed if re.match(r"(listed|posted)\b", listed, re.IGNORECASE) else f"Listed {listed}"
        location = _clean(row.get("location"))
        parts.append(f"{listed} ({location})" if location else listed)
    else:
        parts.append("date not shown")
    return " - ".join(parts)


def summarize_table_row(row):
    """'Title - 3 clicks - 1 message - Active' for a table row."""
    row = row or {}
    parts = [_clean(row.get("title")) or "Untitled"]
    clicks = parse_count(row.get("clicks"))
    messages = parse_count(row.get("messages"))
    parts.append(_plural(clicks, "click") if clicks is not None else "clicks not shown")
    parts.append(_plural(messages, "message") if messages is not None else "messages not shown")
    if _clean(row.get("status")):
        parts.append(_clean(row.get("status")))
    return " - ".join(parts)


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------

_READ_CARDS_JS = """
(sectionName) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  document.querySelectorAll('[data-lp-card]').forEach((n) => {
    if ((n.getAttribute('data-lp-card') || '').startsWith(sectionName + '-')) {
      n.querySelectorAll('[data-lp-title]').forEach((t) => t.removeAttribute('data-lp-title'));
      n.removeAttribute('data-lp-card');
    }
  });

  const headingSelector = 'h1,h2,h3,[role="heading"]';
  const sectionNames = new Set(['your listings', 'needs attention', 'all listings']);
  const headings = Array.from(document.querySelectorAll(headingSelector)).filter(h =>
    sectionNames.has(normalize(h.innerText)) &&
    !Array.from(h.querySelectorAll(headingSelector)).some(child =>
      normalize(child.innerText) === normalize(h.innerText))
  );
  let headingIndex = headings.findIndex((h) => normalize(h.innerText) === normalize(sectionName));
  // Another card layout has one "Your listings" feed, without All/Needs sections.
  if (headingIndex < 0 && sectionName === 'All listings' &&
      !headings.some(h => normalize(h.innerText) === 'needs attention')) {
    headingIndex = headings.findIndex(h => normalize(h.innerText) === 'your listings');
  }
  if (headingIndex < 0) return [];
  const heading = headings[headingIndex];
  const nextHeading = headings[headingIndex + 1] || null;

  const allMarkers = Array.from(document.querySelectorAll('[aria-label^="More options for "]'));
  const markers = allMarkers.filter((marker) => {
    const followsStart = Boolean(heading.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING);
    const precedesEnd = !nextHeading || Boolean(marker.compareDocumentPosition(nextHeading) & Node.DOCUMENT_POSITION_FOLLOWING);
    return followsStart && precedesEnd;
  });

  const cards = [];
  const seen = [];
  markers.forEach((marker) => {
    const title = (marker.getAttribute('aria-label') || '').replace(/^More options for\\s+/i, '').trim();
    const wanted = normalize(title);
    // Stop at the nearest complete card. Climbing to a parent with the same
    // title can merge distinct listings (e.g. two furniture cards at $35/$40).
    let card = marker;
    while (card.parentElement && card.parentElement !== document.body) {
      const parent = card.parentElement;
      if (Array.from(parent.querySelectorAll(headingSelector)).some(h =>
          sectionNames.has(normalize(h.innerText)))) break;
      card = parent;
      const content = card.innerText || '';
      if (normalize(content).includes(wanted) &&
          /(?:[$€£₹]\\s?[\\d,.]+|\\bfree\\b)/i.test(content) &&
          /listed on|clicks? on listing|\\bactive\\b|in review|rejected|sold|draft/i.test(content)) break;
    }
    const text = (card.innerText || '').trim();
    if (!text || !normalize(text).includes(wanted)) return;
    // Responsive duplicate buttons can live in nested wrappers of the same
    // card. Deduplicate overlapping DOM roots, never by title/click count alone.
    if (seen.some(n => n === card || n.contains(card) || card.contains(n))) return;
    seen.push(card);
    const signature = normalize(text);
    if (card.getAttribute('data-lp-signature') !== signature) {
      window.__lpCardSequence = (window.__lpCardSequence || 0) + 1;
      card.setAttribute('data-lp-card-id', String(window.__lpCardSequence));
      card.setAttribute('data-lp-signature', signature);
    }

    const key = `${sectionName}-${cards.length}`;
    card.setAttribute('data-lp-card', key);
    const candidates = Array.from(card.querySelectorAll('span, div')).filter((el) => {
      if (normalize(el.innerText) !== wanted) return false;
      return !Array.from(el.children).some((child) => normalize(child.innerText) === wanted);
    });
    if (candidates.length) candidates[0].setAttribute('data-lp-title', '1');
    let listingId = '';
    for (const a of card.querySelectorAll('a[href]')) {
      const m = (a.getAttribute('href') || '').match(/(?:\\/item\\/|listing_id=)(\\d{6,})/);
      if (m) { listingId = m[1]; break; }
    }
    cards.push({ key, title, text, listingId, cardId: card.getAttribute('data-lp-card-id') });
  });
  return cards;
}
"""

_EXPAND_SECTION_JS = """
(sectionName) => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const headingSelector = 'h1,h2,h3,[role="heading"]';
  const heading = Array.from(document.querySelectorAll(headingSelector)).find(
    (item) => normalize(item.innerText) === normalize(sectionName)
  );
  if (!heading) return false;
  let node = heading.parentElement;
  for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
    if (Array.from(node.querySelectorAll(headingSelector)).filter(h =>
      normalize(h.innerText) === normalize(sectionName)).length !== 1) continue;
    const button = Array.from(node.querySelectorAll('[role="button"], button')).find(
      (item) => normalize(item.innerText) === 'show'
    );
    if (button) {
      button.click();
      return true;
    }
  }
  return false;
}
"""

# Reads the new seller UI table. Columns are found by their header text
# (Price, Clicks, Saves, Messages) so a reordered table still parses.
_READ_LISTINGS_TABLE_JS = r"""
() => {
  const table = document.querySelector('table[aria-label="Your listings"]');
  if (!table) return null;
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const columns = {};
  table.querySelectorAll('thead [role="columnheader"][aria-colindex]').forEach((th) => {
    const name = clean(th.innerText).toLowerCase();
    if (name) columns[name] = th.getAttribute('aria-colindex');
  });
  const findCol = (prefix, fallback) => {
    const key = Object.keys(columns).find((name) => name.startsWith(prefix));
    return key ? columns[key] : fallback;
  };
  const col = {
    item: findCol('item', '2'),
    price: findCol('price', '3'),
    clicks: findCol('clicks', '5'),
    saves: findCol('saves', '6'),
    messages: findCol('messages', '7'),
  };
  const cellText = (row, index) => {
    const cell = row.querySelector(`[role="gridcell"][aria-colindex="${index}"]`);
    return cell ? clean(cell.innerText) : '';
  };
  const rows = Array.from(table.querySelectorAll('tbody tr[role="row"]'));
  rows.sort((a, b) => Number(a.getAttribute('data-index') || 0) - Number(b.getAttribute('data-index') || 0));
  return rows.map((row) => {
    const view = row.querySelector('[aria-label^="View listing: "]');
    const itemCell = row.querySelector(`[role="gridcell"][aria-colindex="${col.item}"]`);
    const lines = itemCell ? itemCell.innerText.split('\n').map(clean).filter(Boolean) : [];
    const title = view ? clean(view.getAttribute('aria-label').replace(/^View listing:\s*/i, '')) : (lines[0] || '');
    const categoryLine = lines.find((line) => /^category:/i.test(line)) || '';
    const last = lines.length > 1 ? lines[lines.length - 1] : '';
    const status = last && last !== title && !/^category:/i.test(last) ? last : '';
    const reason = lines.find(line => line !== title &&
      /^(we(?:'ve| have)? rejected|this listing (?:goes against|was rejected)|this looks like a duplicate|confirm your identity|verify your identity)/i.test(line)) || '';
    // Any link in the row may carry the listing ID (listing_id=, item_id= or /marketplace/item/<id>).
    let listingId = '';
    for (const a of row.querySelectorAll('a[href]')) {
      const m = (a.getAttribute('href') || '').match(/(?:listing_id=|item_id=|\/marketplace\/item\/)(\d{6,})/);
      if (m) { listingId = m[1]; break; }
    }
    return {
      index: row.getAttribute('data-index') || '',
      title: title,
      category: categoryLine.replace(/^category:\s*/i, ''),
      status: status,
      reason: reason,
      price: cellText(row, col.price),
      clicks: cellText(row, col.clicks),
      saves: cellText(row, col.saves),
      messages: cellText(row, col.messages),
      listingId: listingId,
    };
  });
}
"""

_TABLE_ROWS_SELECTOR = 'table[aria-label="Your listings"] tbody tr[role="row"]'


def _tab_count(page, tab_id):
    """Count shown on a status tab, e.g. 9 for 'Needs attention (9)'."""
    try:
        text = page.locator(f'[role="tab"]#{tab_id}').first.inner_text(timeout=2000)
    except Exception:
        return None
    return parse_count(text)


def _open_tab(page, tab_id, sleep):
    """Click a status tab and wait until Facebook marks it selected."""
    tab = page.locator(f'[role="tab"][id="{tab_id}"]').first
    try:
        if tab.count() == 0:
            return False
        if (tab.get_attribute("aria-selected", timeout=2000) or "") == "true":
            return True
        tab.click(timeout=5000)
    except Exception:
        return False
    selected = False
    for _ in range(20):
        try:
            if (tab.get_attribute("aria-selected", timeout=1000) or "") == "true":
                selected = True
                break
        except Exception:
            pass
        sleep(0.3)
    sleep(1.2)
    # A tab that never became selected still shows the previous tab's rows;
    # reading them would label them with the wrong tab.
    return selected


def read_seller_listings(page, sleep, wait_ms=15000):
    """Read the Active and Needs attention tabs of the new seller UI.

    Returns ``(active_rows, needs_rows)`` newest first, or None when the page
    does not show the new "Your listings" table (older UI).
    """
    try:
        page.wait_for_selector('table[aria-label="Your listings"]', timeout=wait_ms)
    except Exception:
        return None
    try:
        page.wait_for_selector(_TABLE_ROWS_SELECTOR, timeout=8000)
    except Exception:
        pass

    _open_tab(page, "active", sleep)
    try:
        active = page.evaluate(_READ_LISTINGS_TABLE_JS) or []
    except Exception:
        active = []

    needs = []
    if _tab_count(page, "needs_attention") != 0 and _open_tab(page, "needs_attention", sleep):
        for _ in range(3):
            try:
                needs = page.evaluate(_READ_LISTINGS_TABLE_JS) or []
            except Exception:
                needs = []
            # Right after switching, the table can still show Active rows.
            if needs != active or not active:
                break
            sleep(1.0)
        if needs == active:
            needs = []
        _open_tab(page, "active", sleep)
    return active, needs


# Reads an opened listing. Two shapes are known:
#  - the new UI's detail panel: "Listed <abbr aria-label='a day ago'> in Hilliard, OH"
#  - the "Your Listing" dialog: "$50 · Atlanta, GA" then "Posted <abbr aria-label='4 weeks ago'>"
# The visible dialog text is returned too, for its headline and click count.
_READ_LISTING_DETAILS_JS = r"""
(expectedTitle) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const norm = (v) => clean(v).toLowerCase().replace(/[^0-9a-z]+/g, '');
  const titles = Array.from(document.querySelectorAll('h1'))
    .filter(visible).map((h) => clean(h.innerText)).filter(Boolean);
  const title = titles.find((t) => norm(t) === norm(expectedTitle)) || '';
  const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible).pop();
  const scope = dialog || document;
  const spans = Array.from(scope.querySelectorAll('span')).filter(
    (s) => visible(s) && s.querySelector('abbr[aria-label]') && /^\s*(listed|posted)\b/i.test(s.textContent || '')
  );
  let listed = '', listedWord = '', location = '', text = '';
  if (spans.length) {
    const span = spans.reduce((a, b) => (a.contains(b) ? b : a));
    const abbr = span.querySelector('abbr[aria-label]');
    text = clean(span.innerText || span.textContent);
    listedWord = (text.match(/^(listed|posted)/i) || ['', ''])[1].toLowerCase();
    listed = clean(abbr.getAttribute('aria-label') || abbr.textContent);
    location = clean((text.match(/\bin\s+(.+)$/i) || [])[1] || '');
  }
  if (!location && dialog) {
    // "$50 · Atlanta, GA" (or "Free · Atlanta, GA") under the dialog's title.
    const line = (dialog.innerText || '').split('\n').map(clean)
      .find((l) => /^([$€£₹]\s?[\d,.]+|free)\s*·\s*\S/i.test(l));
    if (line) location = clean(line.split('·').slice(1).join('·'));
  }
  return {title, listed, listedWord, location, text, dialogText: dialog ? dialog.innerText : ''};
}
"""


def _details_from_info(info, with_reason):
    """Turn _READ_LISTING_DETAILS_JS output into {listed, location, reason, clicks}."""
    info = info or {}
    details = {}
    listed = _clean(info.get("listed"))
    if listed:
        details["listed"] = f"Posted {listed}" if info.get("listedWord") == "posted" else listed
    if _clean(info.get("location")):
        details["location"] = _clean(info.get("location"))
    if info.get("dialogText"):
        dialog = parse_listing_dialog_text(info["dialogText"])
        if not details.get("listed") and dialog.get("date"):
            details["listed"] = dialog["date"]
        if dialog.get("clicks"):
            details["clicks"] = str(parse_count(dialog["clicks"]))
        if with_reason and dialog.get("reason") and not _same_title(dialog["reason"], info.get("expected")):
            details["reason"] = dialog["reason"]
    return details


def _wait_for_listing_details(page, title, sleep):
    """Poll the opened listing for up to 10s; returns the raw details."""
    found = {}
    for _ in range(20):
        sleep(0.5)
        try:
            info = page.evaluate(_READ_LISTING_DETAILS_JS, title or "") or {}
        except Exception:
            info = {}
        if info.get("listed") or info.get("dialogText"):
            found = info
            if info.get("listed"):
                break
    found["expected"] = title or ""
    return found


def _close_listing_dialog(page, sleep):
    """Close an open "Your Listing" dialog with its Close button (or Escape)."""
    try:
        close_btn = page.locator('[role="dialog"] [aria-label="Close"][role="button"]').last
        if close_btn.count():
            try:
                close_btn.click(timeout=3000)
            except Exception:
                close_btn.evaluate("el => el.click()")
        else:
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    sleep(0.8)

# A filter, search or non-default sort can push an older listing to the top.
_LISTINGS_VIEW_STATE_JS = r"""
(searchLabel) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const search = document.querySelector(`input[aria-label="${searchLabel}"]`);
  const filtersButton = document.querySelector('[aria-label="Filters"][role="button"]');
  const sorted = Array.from(document.querySelectorAll('table[aria-label="Your listings"] th[aria-sort]'))
    .map((th) => th.getAttribute('aria-sort')).filter((v) => v && v !== 'none');
  const clearButton = Array.from(document.querySelectorAll('[role="button"], button')).find((b) =>
    b.getClientRects().length && !b.disabled && b.getAttribute('aria-disabled') !== 'true' &&
    /^(?:clear(?: all)? filters|reset filters)$/i.test(clean(b.getAttribute('aria-label') || b.innerText))
  );
  return {
    search: search ? search.value : '',
    filtersText: filtersButton ? clean(filtersButton.innerText) : '',
    sorted: sorted,
    hasClear: Boolean(clearButton),
  };
}
"""

_CLICK_CLEAR_FILTERS_JS = r"""
() => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const button = Array.from(document.querySelectorAll('[role="button"], button')).find((b) =>
    b.getClientRects().length && !b.disabled && b.getAttribute('aria-disabled') !== 'true' &&
    /^(?:clear(?: all)? filters|reset filters)$/i.test(clean(b.getAttribute('aria-label') || b.innerText))
  );
  if (!button) return false;
  button.click();
  return true;
}
"""


_OLD_CARD_SELECTOR = '[aria-label^="More options for "]'


def _old_heading_selector(text):
    return f':is(h1, h2, h3, [role="heading"]):has-text("{text}")'


def ensure_default_listings_view(
    page, sleep, log,
    url=FB_SELLER_LISTINGS,
    search_label="Search all listings",
    ready_selector='table[aria-label="Your listings"]',
    items_selector=_TABLE_ROWS_SELECTOR,
):
    """Make sure no search, filter or sort reorders the listings (newest first).

    Defaults are for the new table; the older card page passes its own URL,
    search box label and selectors.
    """
    try:
        if "?" in (page.url or "") or "#" in (page.url or ""):
            log("Listings page had extra URL options. Reloading the plain listings page...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(ready_selector, timeout=15000)
    except Exception:
        pass

    try:
        state = page.evaluate(_LISTINGS_VIEW_STATE_JS, search_label) or {}
    except Exception:
        return False
    changed = False
    filters_reset = False

    if _clean(state.get("search")):
        log(f"Clearing the listings search box ('{_clean(state.get('search'))}')...")
        try:
            box = page.locator(f'input[aria-label="{search_label}"]').first
            box.fill("", timeout=3000)
            box.press("Enter", timeout=3000)
            changed = True
        except Exception:
            pass

    if state.get("hasClear"):
        # The older sidebar always renders an enabled "Clear filters" control,
        # including after a reset. Its presence alone is not an active filter.
        log("Resetting listing filters (Clear may also appear in the default view)...")
        try:
            filters_reset = bool(page.evaluate(_CLICK_CLEAR_FILTERS_JS))
            changed = filters_reset or changed
        except Exception:
            pass

    filter_count = re.search(r"\d+", state.get("filtersText") or "")
    if (filter_count and int(filter_count.group(0)) > 0) or state.get("sorted"):
        log("A filter or sort is still active. Reloading the plain listings page...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(ready_selector, timeout=15000)
        except Exception:
            pass
        changed = True

    if changed:
        sleep(1.5)
        try:
            page.wait_for_selector(items_selector, timeout=8000)
        except Exception:
            pass
    try:
        state = page.evaluate(_LISTINGS_VIEW_STATE_JS, search_label) or {}
        filter_count = re.search(r"\d+", state.get("filtersText") or "")
        return not (_clean(state.get("search")) or (state.get("hasClear") and not filters_reset) or
                    (filter_count and int(filter_count.group(0)) > 0))
    except Exception:
        return False


def _back_to_listings_table(page, sleep):
    """Close an opened listing and get back to the table."""
    for attempt in range(3):
        try:
            if page.locator(_TABLE_ROWS_SELECTOR).first.is_visible():
                return True
        except Exception:
            pass
        try:
            if attempt == 0:
                _close_listing_dialog(page, sleep)
            elif attempt == 1:
                page.go_back(wait_until="domcontentloaded", timeout=15000)
            else:
                page.goto(FB_SELLER_LISTINGS, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        sleep(1.5)
    try:
        page.wait_for_selector(_TABLE_ROWS_SELECTOR, timeout=10000)
        return True
    except Exception:
        return False


def open_listing_details(page, row, sleep, log, with_reason=False):
    """Open the first row's listing and read its listed date and location."""
    details = {}
    target = page.locator(
        'table[aria-label="Your listings"] tbody tr[role="row"][data-index="0"] [aria-label^="View listing: "]'
    ).first
    try:
        if target.count() == 0:
            target = page.locator(f'{_TABLE_ROWS_SELECTOR} [aria-label^="View listing: "]').first
        try:
            target.click(timeout=3000)
        except Exception:
            # Thumbnail hidden or covered: trigger the same click directly.
            target.evaluate("el => el.click()")
    except Exception as exc:
        log(f"Could not open '{row.get('title')}': {str(exc).splitlines()[0]}")
        return details

    details = _details_from_info(_wait_for_listing_details(page, row.get("title"), sleep), with_reason)
    _back_to_listings_table(page, sleep)
    return details


def read_first_listings(page, sleep, log, with_details=True):
    """Details of the first Active and first Needs attention listing.

    Returns ``(active_first, needs_first)`` (either may be None when the tab
    is empty), or None when the page is not the new "Your listings" table.
    """
    try:
        page.wait_for_selector('table[aria-label="Your listings"], ' + _OLD_CARD_SELECTOR, timeout=8000)
        if not page.locator('table[aria-label="Your listings"]').count():
            return None
    except Exception:
        return None
    ensure_default_listings_view(page, sleep, log)

    results = {}
    for tab_id, section in (("active", SECTION_ALL_LISTINGS), ("needs_attention", SECTION_NEEDS_ATTENTION)):
        results[tab_id] = None
        if tab_id != "active" and _tab_count(page, tab_id) == 0:
            continue
        _open_tab(page, tab_id, sleep)
        rows = []
        for _ in range(4):
            try:
                page.wait_for_selector(_TABLE_ROWS_SELECTOR, timeout=8000)
                rows = page.evaluate(_READ_LISTINGS_TABLE_JS) or []
            except Exception:
                rows = []
            active_first = results.get("active")
            # Right after switching tabs the table can still show Active rows.
            stale = (
                tab_id != "active" and rows and active_first
                and rows[0].get("title") == active_first.get("title")
                and rows[0].get("listingId") == active_first.get("listingId")
                and rows[0].get("clicks") == active_first.get("clicks")
            )
            if not stale:
                break
            sleep(1.0)
        else:
            rows = []
        if not rows:
            continue
        first = dict(rows[0])
        details = {}
        if with_details:
            log(f"Opening the first {section} listing '{first.get('title')}' to read its date...")
            details = open_listing_details(page, first, sleep, log, with_reason=tab_id != "active")
        for key in ("listed", "location", "reason"):
            if details.get(key):
                first[key] = details[key]
        results[tab_id] = first
    _open_tab(page, "active", sleep)
    return results["active"], results["needs_attention"]


# --- Older UI (/marketplace/you/selling cards) -----------------------------

# How long to wait for a lazy-loading feed to add more cards after a scroll. Three browsers on one
# connection can take several seconds per batch, and a scan that gives up early reports a shorter
# account as if it were complete (the same profile read 38, 18 and 28 listings in one afternoon).
FEED_GROWTH_WINDOW = 3.5
FEED_FIRST_CARD_WAIT_MS = 8000

_OLD_MARKER_COUNT_JS = r"""
() => document.querySelectorAll('[aria-label^="More options for "]').length
"""

_FEED_SPINNER_JS = r"""
() => Array.from(document.querySelectorAll('[role="progressbar"], [aria-busy="true"]')).some((el) => {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
})
"""

_SCROLL_OLD_FEED_JS = r"""
([section, toBottom]) => {
  const cards = Array.from(document.querySelectorAll('[data-lp-card]'))
      .filter(n => (n.getAttribute('data-lp-card') || '').startsWith(section + '-'));
  const last = cards[cards.length - 1];
  // Facebook often scrolls an inner panel, not the browser window.
  let panel = last ? last.parentElement : null;
  while (panel && panel !== document.body) {
    if (panel.scrollHeight > panel.clientHeight + 20 &&
        /auto|scroll/.test(getComputedStyle(panel).overflowY)) break;
    panel = panel.parentElement;
  }
  const scroller = panel && panel !== document.body ? panel : document.scrollingElement;
  const before = scroller.scrollTop;
  if (toBottom) { scroller.scrollTop = scroller.scrollHeight; return; }
  if (last) last.scrollIntoView({block: 'end'});
  // Bring the "load more" area below the last card into view as well.
  scroller.scrollTop += 300;
  if (scroller.scrollTop === before) scroller.scrollTop += Math.max(200, scroller.clientHeight * 0.8);
}
"""


def _wait_for_feed_growth(page, known_markers, sleep, window, deadline=None):
    """Wait up to ``window`` s for the feed to add cards; extends while a loading spinner shows."""
    waited, extensions = 0.0, 0
    while waited < window:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        sleep(min(0.5, max(0, deadline - time.monotonic())) if deadline is not None else 0.5)
        waited += 0.5
        try:
            if page.evaluate(_OLD_MARKER_COUNT_JS) > known_markers:
                return True
            if waited >= window and extensions < 3 and page.evaluate(_FEED_SPINNER_JS):
                extensions += 1
                waited = 0.0
        except Exception:
            return False
    return False


def read_old_section(page, section_name, sleep, collect_all=False, deadline=None):
    """Cards of one section ("Needs attention" / "All listings"), newest first."""
    try:
        selector = _old_heading_selector(section_name)
        if section_name == SECTION_ALL_LISTINGS:
            selector += ", " + _old_heading_selector("Your listings")
        page.wait_for_selector(selector, state="visible", timeout=6000)
    except Exception:
        return []
    try:
        # A hidden section shows a "Show" button instead of its cards.
        if page.evaluate(_EXPAND_SECTION_JS, section_name):
            sleep(0.8)
    except Exception:
        pass
    try:
        # Cards can render a few seconds after their heading; an empty section just times out here.
        page.wait_for_selector(_OLD_CARD_SELECTOR, state="attached",
                               timeout=FEED_FIRST_CARD_WAIT_MS if collect_all else 3000)
    except Exception:
        pass
    # Only the last section is an open-ended feed that keeps loading as you scroll.
    patient = collect_all and section_name == SECTION_ALL_LISTINGS
    window = FEED_GROWTH_WINDOW if patient else 1.5
    need_idle = 2 if patient else 1
    collected, idle = {}, 0
    for _ in range(300 if collect_all else 1):
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            cards = page.evaluate(_READ_CARDS_JS, section_name) or []
        except Exception:
            break
        if not isinstance(cards, list):
            break
        if not collect_all:
            return cards
        grew = False
        for card in cards:
            key = card.get("listingId") or card.get("cardId") or card.get("key")
            if key not in collected:
                grew = True
            collected[key] = card
        if len(collected) >= MAX_SCAN_ROWS:
            break
        if grew:
            idle = 0
        else:
            idle += 1
        if idle >= need_idle:
            break
        # After one quiet window, jump to the very bottom: that is where a "load more" area
        # triggers. If that window is quiet too, the feed has really ended.
        to_bottom = patient and idle == 1
        try:
            known = page.evaluate(_OLD_MARKER_COUNT_JS)
            page.evaluate(_SCROLL_OLD_FEED_JS, [section_name, to_bottom])
        except Exception:
            break
        _wait_for_feed_growth(page, known, sleep, window, deadline)
    return list(collected.values())


def _back_to_selling_page(page, sleep, start_url):
    """Close the opened listing and make sure the selling page is showing."""
    _close_listing_dialog(page, sleep)
    try:
        if (page.url or "").split("?")[0].rstrip("/") != (start_url or "").split("?")[0].rstrip("/"):
            try:
                page.go_back(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            if "/marketplace/you/selling" not in (page.url or ""):
                page.goto(FB_MARKETPLACE_SELLING, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(_OLD_CARD_SELECTOR, timeout=10000)
    except Exception:
        pass


def open_old_card_details(page, card, sleep, log, with_reason=False):
    """Click an older-UI card and read the "Your Listing" dialog it opens."""
    key = card.get("key")
    start_url = page.url
    try:
        target = page.locator(f'[data-lp-card="{key}"] [data-lp-title]').first
        if target.count() == 0:
            target = page.locator(f'[data-lp-card="{key}"] [role="button"][aria-label]').first
        try:
            target.click(timeout=5000)
        except Exception:
            target.evaluate("el => el.click()")
    except Exception as exc:
        log(f"Could not open '{card.get('title')}': {str(exc).splitlines()[0]}")
        return {}
    details = _details_from_info(_wait_for_listing_details(page, card.get("title"), sleep), with_reason)
    _back_to_selling_page(page, sleep, start_url)
    return details


def read_first_listings_old_ui(page, sleep, log, with_details=True):
    """Details of the first All listings and first Needs attention card.

    Returns ``(all_first, needs_first)`` (either may be None), or None when
    the page shows neither section.
    """
    try:
        page.wait_for_selector(
            ", ".join(_old_heading_selector(title) for title in
                      (SECTION_ALL_LISTINGS, SECTION_NEEDS_ATTENTION, "Your listings")),
            timeout=15000,
        )
    except Exception:
        return None
    if ensure_default_listings_view(
        page, sleep, log,
        url=FB_MARKETPLACE_SELLING,
        search_label="Search your listings",
        ready_selector=", ".join(_old_heading_selector(title) for title in (SECTION_ALL_LISTINGS, "Your listings")),
        items_selector=_OLD_CARD_SELECTOR,
    ) is False:
        log("Cannot confirm the older listings view is unfiltered.")
        return None
    try:
        page.wait_for_selector(_OLD_CARD_SELECTOR, timeout=8000)
    except Exception:
        pass

    results = {}
    for section in (SECTION_ALL_LISTINGS, SECTION_NEEDS_ATTENTION):
        results[section] = None
        # Read again right before opening: an earlier open may have reloaded
        # the page and dropped the card markers.
        cards = read_old_section(page, section, sleep)
        if not cards:
            continue
        first = parse_old_card(cards[0])
        details = {}
        if with_details:
            log(f"Opening the first {section} listing '{first.get('title')}' to read its date...")
            details = open_old_card_details(page, cards[0], sleep, log, with_reason=section == SECTION_NEEDS_ATTENTION)
        for key in ("listed", "location", "clicks"):
            if details.get(key) and not (key == "clicks" and first.get("clicks")):
                first[key] = details[key]
        if details.get("reason") and not first.get("reason"):
            first["reason"] = details["reason"]
        results[section] = first
    if not results[SECTION_ALL_LISTINGS] and not results[SECTION_NEEDS_ATTENTION]:
        return None
    return results[SECTION_ALL_LISTINGS], results[SECTION_NEEDS_ATTENTION]


# --- Live Checker v2: full scan of every tab ---------------------------------

_LIST_TABS_JS = r"""
() => Array.from(document.querySelectorAll('[role="tab"]')).map((t) => ({
  id: t.id || '',
  text: (t.innerText || '').replace(/\s+/g, ' ').trim(),
})).filter((t) => t.id && t.text)
"""

# The listings table is virtualised: only rows near the viewport exist. Scroll
# one step at a time (never straight to the bottom, which would skip the rows
# in between): bring the last rendered row to the bottom edge, or move down by
# most of a screen when that does not move the page.
_SCROLL_LISTINGS_JS = r"""
() => {
  const rows = document.querySelectorAll('table[aria-label="Your listings"] tbody tr[role="row"]');
  const last = rows[rows.length - 1];
  let panel = last ? last.parentElement : null;
  while (panel && panel !== document.body) {
    if (panel.scrollHeight > panel.clientHeight + 20 &&
        /auto|scroll/.test(getComputedStyle(panel).overflowY)) break;
    panel = panel.parentElement;
  }
  const scroller = panel && panel !== document.body ? panel : document.scrollingElement;
  const before = scroller.scrollTop;
  if (last) last.scrollIntoView({block: 'end'});
  if (scroller.scrollTop === before) scroller.scrollTop += Math.max(200, scroller.clientHeight * 0.8);
}
"""

_PAGE_FACTS_JS = r"""
() => ({
  url: location.href,
  lang: document.documentElement.lang || '',
  text: ((document.body && document.body.innerText) || '').slice(0, 4000),
})
"""

_FIND_SELLING_LINK_JS = r"""
() => {
  const links = Array.from(document.querySelectorAll('a[href]'));
  // Matched by URL, not link text, so it works in any Facebook language.
  const patterns = [/\/marketplace\/you\/selling/, /\/marketplace\/seller/, /\/marketplace\/you(\/|\?|$)/];
  for (const pattern of patterns) {
    const link = links.find((a) => pattern.test(a.getAttribute('href') || ''));
    if (link) {
      const href = link.getAttribute('href');
      link.click();
      return href;
    }
  }
  return '';
}
"""


def _tab_label(text):
    """'Needs attention (9)' -> 'Needs attention'."""
    return _clean(re.sub(r"\(\s*\d[\d,]*\s*\)\s*$", "", str(text or "")))


def _tab_badge(text):
    match = re.search(r"\(\s*(\d[\d,]*)\s*\)\s*$", str(text or ""))
    return int(match.group(1).replace(",", "")) if match else None


def _scroll_to_top(page):
    try:
        page.evaluate(r"""() => {
          for (const root of document.querySelectorAll('table[aria-label="Your listings"],[data-lp-card]')) {
            for (let panel = root.parentElement; panel && panel !== document.body; panel = panel.parentElement) {
              if (panel.scrollTop && /auto|scroll/.test(getComputedStyle(panel).overflowY)) panel.scrollTop = 0;
            }
          }
          window.scrollTo(0, 0);
        }""")
    except Exception:
        pass


def _collect_table_rows(page, sleep, deadline=None, expected=None):
    """Read the open tab's rows, scrolling until no new rows appear or time is up.

    ``expected`` is the count on the tab badge: once that many distinct rows are read
    there is nothing left to scroll for, so it stops at once instead of waiting.
    """
    rows_by_key = {}
    stable = 0
    for _ in range(120):
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            rows = page.evaluate(_READ_LISTINGS_TABLE_JS) or []
        except Exception:
            rows = []
        before = len(rows_by_key)
        for row in rows:
            key = row.get("listingId") or f"{row.get('index')}|{row.get('title')}"
            rows_by_key.setdefault(key, row)
        if len(rows_by_key) >= MAX_SCAN_ROWS:
            break
        if expected and len(rows_by_key) >= expected:
            break
        stable = stable + 1 if len(rows_by_key) == before else 0
        if stable >= 3:
            break
        try:
            page.evaluate(_SCROLL_LISTINGS_JS)
        except Exception:
            pass
        sleep(random.uniform(0.6, 1.1))
    _scroll_to_top(page)
    return list(rows_by_key.values())


def _listing_from_row(row, label):
    return {
        "title": _clean(row.get("title")),
        "status": _clean(row.get("status")),
        "reason": _clean(row.get("reason")),
        "price": _clean(row.get("price")),
        "index": row.get("index") if row.get("index") is not None else "",
        "listingId": _clean(row.get("listingId")),
        "clicksCount": parse_count(row.get("clicks")),
        "messagesCount": parse_count(row.get("messages")),
        "tab": label,
        "state": ls.classify_status(label, row.get("status"), row.get("reason")),
    }


def read_all_listings(page, sleep, log):
    """Every listing on every tab of the new "Your listings" table.

    Returns ``(listings, tab_counts)`` or None when the table is not shown.
    """
    try:
        page.wait_for_selector('table[aria-label="Your listings"]', timeout=15000)
        tabs = page.evaluate(_LIST_TABS_JS) or []
    except Exception:
        return None

    # Problem tabs first, so a listing shown on both is counted by its worst state.
    # The "active" tab id is language independent; the label is only a fallback.
    def _is_active_tab(tab):
        return tab.get("id") == "active" or _tab_label(tab.get("text")).lower().startswith("active")

    tabs.sort(key=_is_active_tab)
    active_id = next((tab["id"] for tab in tabs if _is_active_tab(tab)), "active")
    listings, tab_counts, notes, previous = [], {}, [], None
    if not tabs:
        notes.append("no readable status tabs were found")
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    for tab in tabs:
        label, badge = _tab_label(tab["text"]), _tab_badge(tab["text"])
        if badge == 0:
            tab_counts[label] = 0
            continue
        if time.monotonic() > deadline:
            notes.append(f"'{label}' tab skipped (scan time limit)")
            continue
        if not _open_tab(page, tab["id"], sleep):
            log(f"Could not open the '{label}' tab; skipping it.")
            notes.append(f"'{label}' tab could not be opened")
            continue
        rows = []
        for _ in range(3):
            rows = _collect_table_rows(page, sleep, deadline, expected=badge)
            signature = tuple((r.get("listingId"), r.get("title")) for r in rows)
            if signature != previous:
                break
            sleep(1.0)  # still showing the previous tab's rows
        else:
            rows, signature = [], None
        previous = signature or previous
        tab_counts[label] = badge if badge is not None else len(rows)
        listings.extend(_listing_from_row(row, label) for row in rows)
        if not rows and badge is None:
            notes.append(f"'{label}' tab returned no rows and did not show a zero count")
        warning = ls.tab_total_mismatch({label: badge}, rows) if badge else None
        if warning:
            log(f"Note: '{label}' tab: {warning}.")
            notes.append(f"'{label}' tab: {warning}")
    _open_tab(page, active_id, sleep)
    return listings, tab_counts, notes


def read_all_listings_old_ui(page, sleep):
    """Every card of the older layout's two sections, as ``(listings, tab_counts)``."""
    listings, tab_counts, notes = [], {}, []
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    for section in (SECTION_NEEDS_ATTENTION, SECTION_ALL_LISTINGS):
        if time.monotonic() >= deadline:
            notes.append(f"'{section}' section skipped (scan time limit)")
            continue
        cards = read_old_section(page, section, sleep, collect_all=True, deadline=deadline)
        tab_counts[section] = len(cards)
        if len(cards) >= MAX_SCAN_ROWS or time.monotonic() >= deadline:
            notes.append(f"'{section}' section reached the scan limit")
        for card in cards:
            parsed = parse_old_card(card)
            listings.append({
                "title": parsed["title"],
                "status": parsed["status"],
                "reason": parsed["reason"],
                "price": parsed["price"],
                "listed": parsed["listed"],
                "clicksCount": parse_count(parsed["clicks"]),
                "messagesCount": None,
                "listingId": _clean(card.get("listingId")),
                "cardId": card.get("cardId"),
                "tab": section,
                "state": ls.classify_status(section, parsed["status"], parsed["reason"]),
            })
    # The sidebar count is a lower bound, not a substitute for reading cards.
    try:
        claimed = page.evaluate(r"""() => {
            const match = document.body.innerText.match(/\b(\d[\d,]*)\+?\s+active listings\b/i);
            return match ? Number(match[1].replace(/,/g, '')) : 0;
        }""")
        active = [item for item in ls.dedupe_listings(listings) if item["state"] in (ls.LIVE, ls.PENDING)]
        warning = ls.tab_total_mismatch({"Marketplace profile": claimed}, active)
        if warning:
            notes.append(warning)
    except Exception:
        pass
    _scroll_to_top(page)
    return listings, tab_counts, notes


_OLD_DIALOG_ITEM_LINK_JS = r"""
(expectedTitle) => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const links = [];
  for (const dialog of document.querySelectorAll('[role="dialog"]')) {
    if (!dialog.getClientRects().length) continue;
    for (const a of dialog.querySelectorAll('a[href*="/marketplace/item/"]')) {
      if (a.getClientRects().length &&
          (a.innerText || '').split('\n').some(line => clean(line) === clean(expectedTitle)))
        links.push(a.getAttribute('href'));
    }
  }
  return links;
}
"""


def recover_old_sample_ids(page, listings, limit, sleep, log):
    """Read item links from only the sampled cards' own dialogs, never guess IDs by title."""
    sample, _ = lv.visibility_targets(listings, limit)
    for target in sample:
        card_id = str(target.get("cardId") or "")
        if target.get("listingId") or not re.fullmatch(r"\d+", card_id):
            continue
        start_url = page.url
        opened = False
        try:
            if page.locator('[role="dialog"]:visible').count():
                log("An existing dialog is still open; skipping ID recovery rather than reusing its item link.")
                continue
            card = page.locator(f'[data-lp-card-id="{card_id}"]')
            title = card.locator('[data-lp-title]').first
            if not title.count() or not _same_title(title.inner_text(timeout=2000), target["title"]):
                continue
            log(f"Reading the exact item ID for buyer sample '{target['title']}'...")
            title.click(timeout=5000)
            opened = True
            for _ in range(10):
                sleep(0.5)
                links = page.evaluate(_OLD_DIALOG_ITEM_LINK_JS, target["title"]) or []
                ids = {lv.item_id_from_href(link) for link in links} - {""}
                if len(ids) == 1:
                    listing_id = ids.pop()
                    for item in listings:
                        if str(item.get("cardId") or "") == card_id:
                            item["listingId"] = listing_id
                    log(f"Buyer target ID confirmed from its seller card: {listing_id}.")
                    break
            else:
                log("No unambiguous item link in this card's dialog; buyer identity remains unverified.")
        except AutomationStopped:
            raise
        except Exception as exc:
            log(f"Could not read this sample's item ID; left unverified: {str(exc).splitlines()[0]}")
        finally:
            if opened:
                _back_to_selling_page(page, sleep, start_url)


# --- Diagnostics and navigation fallback --------------------------------------

def _prune_debug_dir():
    try:
        files = sorted(
            (os.path.join(DEBUG_DIR, name) for name in os.listdir(DEBUG_DIR)),
            key=os.path.getmtime,
        )
        for path in files[:-MAX_DEBUG_FILES]:
            os.remove(path)
    except OSError:
        pass


def capture_diagnostics(page, profile_label, log):
    """Save a screenshot and return the page facts used to explain an empty read.

    The screenshot stays on this computer in ``debug/`` (newest 50 are kept).
    """
    facts = {"url": "", "lang": "", "text": "", "screenshot": ""}
    try:
        facts.update(page.evaluate(_PAGE_FACTS_JS) or {})
    except Exception:
        pass
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(profile_label or "profile"))[:40]
        path = os.path.join(DEBUG_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.png")
        page.screenshot(path=path, full_page=False, timeout=8000)
        facts["screenshot"] = path
        _prune_debug_dir()
    except Exception:
        pass
    log(f"Diagnostics: url={facts['url'] or '?'} lang={facts['lang'] or '?'} screenshot={facts['screenshot'] or 'not saved'}")
    return facts


def open_selling_from_marketplace_home(page, sleep, log):
    """Open Marketplace home and follow its Selling link (matched by URL)."""
    log("Trying Marketplace home to find the Selling link...")
    try:
        page.goto(FB_MARKETPLACE_HOME, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    sleep(random.uniform(2.0, 3.5))
    try:
        href = page.evaluate(_FIND_SELLING_LINK_JS)
    except Exception:
        href = ""
    if not href:
        log("No Selling link was found on Marketplace home.")
        return False
    log(f"Followed Marketplace link: {href.split('?')[0]}")
    sleep(random.uniform(2.0, 3.5))
    return True


def run_live_checker_flow(debug_url, post, logger=print, state_callback=None, stop_event=None):
    post = post or {}
    options = post.get("_buyer_options") or lv.BuyerOptions.from_settings(load_config())
    scan_started = time.monotonic()

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

    def is_logged_out(page):
        return page_issue(page) == ls.LOGGED_OUT

    def has_checkpoint(page):
        return page_issue(page) == ls.CHECKPOINT

    row_number = post.get("rowNumber")
    target_sheet = post.get("_target_sheet", LIVE_CHECKER_SHEET)
    profile_label = post.get("profile name") or post.get("Profile Name") or post.get("profileName") or post.get("id")

    def buyer_columns(has_live=False, owner_failed=False):
        return lv.owner_buyer_columns(options, has_live, owner_failed) if target_sheet == LIVE_CHECKER_SHEET else {}

    def write_columns(values):
        """Report whether the current scan was saved, separately from what was read."""
        if not row_number:
            return True
        try:
            saved, message = update_column_values(target_sheet, row_number, values)
            if not saved:
                log(f"Note: could not save some sheet columns ({message}).")
            return saved
        except Exception as exc:
            log(f"Note: could not save the sheet columns: {exc}")
            return False

    def record_history(check, scan=None):
        if not row_number:
            return
        counts = (scan or {}).get("counts") or {}
        try:
            saved, message = append_live_history(
                profile_label,
                total=scan["total"] if scan else None,
                live=counts.get(ls.LIVE, 0) if scan else None,
                pending=counts.get(ls.PENDING, 0) if scan else None,
                rejected=counts.get(ls.REJECTED, 0) if scan else None,
                check=check,
            )
            if not saved:
                log(f"Note: could not save the Live History row ({message}).")
        except Exception as exc:
            log(f"Note: could not save the Live History row: {exc}")

    def fail(code, detail, extra=None):
        """Record why the scan failed in the sheet, then stop this profile.

        A slow or half-loaded page (TIMEOUT / LAYOUT_UNKNOWN) gets one quiet
        retry first: nothing is written to the sheet until the final attempt.
        """
        result = ls.format_check_result(code, detail)
        if code in ls.RETRYABLE_CODES and not post.get("_live_retried"):
            post["_live_retried"] = True
            log(f"{result}. Trying once more with a fresh page...")
            raise RetryableError(result)
        # A failed read must not make a previous run's counts look freshly verified.
        values = {
            "Check Result": result, "Last Checked": ls.timestamp(),
            "Scan Time": ls.format_duration(time.monotonic() - scan_started),
            "Total Listings": "", "Live Count": "", "Pending Count": "", "Rejected Count": "",
            "Other Count": "", "Status Summary": "", "Listing Details": "",
            "Duplicate Count": "", "Duplicate Details": "",
            "All Listings": "", "Needs Attention": "",
        }
        values.update(extra or {})
        if target_sheet == LIVE_CHECKER_SHEET:
            values.update(lv.owner_buyer_columns(options, owner_failed=code != ls.NO_LISTINGS))
        post.pop("_live_counts", None)
        post.pop("_live_listings", None)
        post.pop("_live_duplicates", None)
        write_columns(values)
        record_history(code)
        post["_live_check"] = code
        raise PermanentError(result)

    responses = None

    def save_results(all_summary, needs_summary, scan, confirmed_empty=False):
        log(f"All listings: {all_summary}")
        log(f"Needs attention: {needs_summary}")
        values = {"All Listings": all_summary, "Needs Attention": needs_summary, "Last Checked": ls.timestamp(),
                   "Scan Time": ls.format_duration(time.monotonic() - scan_started)}
        if responses is not None:
            values["Responses"] = str(responses)
        values.update(buyer_columns(bool(scan and scan["counts"].get(ls.LIVE)),
                                     owner_failed=not (scan and (scan["total"] or confirmed_empty))))
        if (target_sheet == LIVE_CHECKER_SHEET and options.checkers and scan and scan["counts"].get(ls.LIVE)
                and not lv.visibility_targets(scan["listings"], options.limit)[0]):
            values.update({lv.checker_column(label): "Not checked: no readable listing titles"
                           for label, _ in options.checkers})
            values.update({"Visibility": "NOT_CHECKED", "PK vs US": "Not compared: no searchable targets",
                           "Visibility Details": "Live listings were counted, but no readable titles were available for buyer search."})
            post["_visibility"] = values["Visibility"]
            post["_visibility_cols"] = {column: values[column] for column in (
                "PK Check", "US Check", "PK vs US", "Visibility", "Visibility Details", "Visibility Checked")}
        headline = ""
        if scan and (scan["total"] or confirmed_empty):
            counts = scan["counts"]
            headline = scan["headline"]
            log(f"Full scan: {headline} ({scan['total']} listings; tabs: {scan['tabCounts']}) in "
                f"{ls.format_duration(time.monotonic() - scan_started)}")
            log(f"Facebook-marked duplicates: {scan['duplicates']} (included in Rejected, not added to Total).")
            values.update({
                "Total Listings": scan["total"],
                "Live Count": counts.get(ls.LIVE, 0),
                "Pending Count": counts.get(ls.PENDING, 0),
                "Rejected Count": counts.get(ls.REJECTED, 0),
                "Other Count": scan["other"],
                "Duplicate Count": scan["duplicates"],
                "Duplicate Details": scan["duplicateDetails"],
                "Status Summary": headline,
                "Listing Details": scan["details"],
            })
            post["_live_counts"] = counts
            post["_live_duplicates"] = scan["duplicates"]
            post["_live_listings"] = scan["listings"]  # for the PK/US checker visibility pass
            if scan["notes"]:
                values["Check Result"] = ls.format_check_result("PARTIAL", "; ".join(scan["notes"]))
                post["_live_check"] = "PARTIAL"
            else:
                values["Check Result"] = ls.NO_LISTINGS if confirmed_empty else ls.OK
                post["_live_check"] = values["Check Result"]
        else:
            values["Check Result"] = ls.format_check_result("PARTIAL", "full scan read no rows; only the first listings were saved")
            post["_live_check"] = "PARTIAL"
            values.update({column: "" for column in (
                "Total Listings", "Live Count", "Pending Count", "Rejected Count", "Other Count",
                "Duplicate Count", "Duplicate Details", "Status Summary", "Listing Details")})
            for key in ("_live_counts", "_live_listings", "_live_duplicates"):
                post.pop(key, None)
        if not write_columns(values):
            post["_live_check"] = "SHEET_WRITE_FAILED"
            raise PermanentError("Seller scan read, but Google Sheets did not confirm the result was saved.")
        record_history(post["_live_check"], scan if scan and (scan["total"] or confirmed_empty) else None)
        set_state(AutomationState.SUCCESS)
        prefix = f"{headline}; " if headline else ""
        if post["_live_check"] == "PARTIAL":
            prefix = f"{values['Check Result']}; {prefix}"
        return True, f"{prefix}All listings: {all_summary}; Needs attention: {needs_summary}"

    def read_new_layout():
        """(firsts, scan) from the new table, "empty" for a table with no rows, or None."""
        try:
            page.wait_for_selector('table[aria-label="Your listings"], ' + _OLD_CARD_SELECTOR, timeout=8000)
            if not page.locator('table[aria-label="Your listings"]').count():
                return None
        except Exception:
            return None
        if not ensure_default_listings_view(page, interruptible_sleep, log):
            log("Cannot confirm the seller table is unfiltered.")
            return None
        scan = read_all_listings(page, interruptible_sleep, log)
        if not scan:
            return None
        items, tab_counts, notes = scan
        if not items:
            # A rendered but unloaded/filtered table is not proof of zero listings.
            if tab_counts and not notes and all(count == 0 for count in tab_counts.values()):
                return "empty"
            return None
        if options.open_details:
            firsts = read_first_listings(page, interruptible_sleep, log, with_details=True)
        else:
            firsts = None
        if not firsts or not any(firsts):
            firsts = (
                next((row for row in items if row["tab"].lower().startswith(("active", "all listings"))), None),
                next((row for row in items if row["tab"].lower().startswith("needs attention")), None),
            )
        return firsts, scan

    def read_old_layout(attempts):
        for attempt in range(1, attempts + 1):
            check_stop()
            firsts = read_first_listings_old_ui(page, interruptible_sleep, log, with_details=options.open_details)
            if firsts and (firsts[0] or firsts[1]):
                scan = read_all_listings_old_ui(page, interruptible_sleep)
                if target_sheet == LIVE_CHECKER_SHEET and options.checkers:
                    recover_old_sample_ids(page, scan[0], options.limit, interruptible_sleep, log)
                return firsts, scan
            if attempt < attempts:
                log(f"Listings not loaded yet (attempt {attempt}/{attempts}). Reloading...")
                interruptible_sleep(random.uniform(1.5, 3))
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                interruptible_sleep(1)
        return None

    with sync_playwright() as p:
        check_stop()
        log("Connecting to browser...")
        try:
            browser = p.chromium.connect_over_cdp(debug_url, timeout=30000)
        except Exception as exc:
            raise RetryableError(f"CDP Connection failed: {exc}")

        if not browser.contexts:
            raise RetryableError("The profile's persistent browser context is not ready; no fresh context was created.")
        context = browser.contexts[0]
        page = context.new_page()

        try:
            remembered_old = layout_memory.recall(profile_label) == "old"
            log("Opening Marketplace 'Your listings'...")
            try:
                page.goto(FB_MARKETPLACE_SELLING if remembered_old else FB_SELLER_LISTINGS,
                          wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass

            set_state(AutomationState.PAGE_LOADING)
            if is_logged_out(page):
                fail(ls.LOGGED_OUT, "Facebook profile is not logged in. Please log in to this profile first.")
            if has_checkpoint(page):
                fail(ls.CHECKPOINT, "Confirm your identity. Not retried, to avoid triggering more checks.")

            # Responses first: the Messenger badge in Facebook's top bar.
            from core.automation.fb_listing import read_messenger_unread_count
            log("Checking for new responses (Messenger badge)...")
            responses = read_messenger_unread_count(page, interruptible_sleep, max_wait=2)
            if responses is None:
                log("Could not find the Messenger icon, so Responses was not saved.")
            else:
                log(f"Responses: {responses}")

            # 1) The layout this profile showed last time (skips a 15 s wait for a table it does not have).
            # 2) New table. 3) Older cards by URL. 4) Marketplace home -> Selling link.
            result, layout_used = None, ""
            if remembered_old:
                log("Using the older 'Your listings' layout (remembered for this profile)...")
                result = read_old_layout(2)
                if result is not None:
                    layout_used = "old"
                else:
                    log("The remembered layout did not load; checking the newer table...")
                    try:
                        page.goto(FB_SELLER_LISTINGS, wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    interruptible_sleep(random.uniform(1.0, 2.0))
            if result is None:
                result = read_new_layout()
                if result is not None:
                    layout_used = "new"
            if result is None:
                log("New listings table not found. Using the older 'Your listings' layout...")
                if page.url.split("?")[0].rstrip("/") != FB_MARKETPLACE_SELLING.rstrip("/"):
                    try:
                        page.goto(FB_MARKETPLACE_SELLING, wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    interruptible_sleep(0.5)
                result = read_old_layout(2)
                if result is not None:
                    layout_used = "old"
            if result is None and open_selling_from_marketplace_home(page, interruptible_sleep, log):
                result = read_new_layout()
                if result is not None:
                    layout_used = "new"
                else:
                    result = read_old_layout(1)
                    if result is not None:
                        layout_used = "old"
            if layout_used and result not in (None, "empty"):
                layout_memory.remember(profile_label, layout_used)

            if result == "empty":
                return save_results("No listings", "None", ls.build_scan_result([]), confirmed_empty=True)
            if result is None:
                if page.is_closed():
                    raise RetryableError("The browser page closed before listings could be read.")
                facts = capture_diagnostics(page, profile_label, log)
                code = ls.diagnose_empty_page(
                    url=facts.get("url"),
                    lang=facts.get("lang"),
                    body_text=facts.get("text"),
                    logged_out=is_logged_out(page),
                    checkpoint=has_checkpoint(page),
                )
                if code == ls.NO_LISTINGS:
                    code = ls.LAYOUT_UNKNOWN  # generic page text did not prove the owner's listing count
                detail = f"could not read any listings (url {str(facts.get('url') or '?').split('?')[0]}, lang {facts.get('lang') or '?'})"
                if facts.get("screenshot"):
                    detail += f", screenshot {os.path.basename(facts['screenshot'])}"
                fail(code, detail)

            (all_first, needs_first), scan = result
            all_summary = (
                summarize_first_listing(all_first, SECTION_ALL_LISTINGS) if all_first else "No active listings"
            )
            needs_summary = summarize_first_listing(needs_first, SECTION_NEEDS_ATTENTION)
            built = ls.build_scan_result(*scan) if scan else None
            return save_results(all_summary, needs_summary, built)
        finally:
            try:
                page.close()
            except Exception:
                pass
