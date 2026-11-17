"""Pure helpers for Live Checker v2: status vocabulary, counting and reason codes.

No browser or network access here, so everything is unit tested.
"""
import re
from datetime import datetime

# --- Listing statuses (one per Marketplace listing) -------------------------
LIVE = "LIVE"
PENDING = "PENDING"
REJECTED = "REJECTED"
REMOVED = "REMOVED"
EXPIRED = "EXPIRED"
SOLD = "SOLD"
DRAFT = "DRAFT"
UNKNOWN = "UNKNOWN"

# --- Check results (one per profile scan) ------------------------------------
OK = "OK"
NO_LISTINGS = "NO_LISTINGS"
LOGGED_OUT = "LOGGED_OUT"
CHECKPOINT = "CHECKPOINT"
NO_MARKETPLACE = "NO_MARKETPLACE"
LANG_NOT_EN = "LANG_NOT_EN"
LAYOUT_UNKNOWN = "LAYOUT_UNKNOWN"
TIMEOUT = "TIMEOUT"

# Order used when listing counts in summaries.
STATUS_ORDER = (LIVE, PENDING, REJECTED, REMOVED, EXPIRED, SOLD, DRAFT, UNKNOWN)

# Each rule is checked against "<tab label> <row status> <reason>", first hit wins.
# Rejection and review wording is checked before "active" so a row such as
# "Active - Pending review" is not counted as live.
_RULES = (
    (REJECTED, r"reject|against our|goes against|breaking our|needs? attention|violat|declined|not approved"),
    (PENDING, r"in review|being reviewed|pending|processing|under review|awaiting"),
    (REMOVED, r"removed|deleted|taken down|unavailable"),
    (EXPIRED, r"expired|renew"),
    (SOLD, r"\bsold\b"),
    (DRAFT, r"draft|unfinished|incomplete"),
    (LIVE, r"\bactive\b|\blive\b|\bavailable\b|listed on marketplace"),
)


def _clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def classify_status(tab_label="", row_status="", reason=""):
    """Map Facebook's tab / status / notice text to one fixed status value."""
    def match(value):
        text = _clean(value).lower()
        for status, pattern in _RULES:
            if text and re.search(pattern, text):
                return status
        return UNKNOWN

    row_state = match(f"{row_status} {reason}")
    tab_state = match(tab_label)
    # An "Active" row inside the Needs attention / Sold / Drafts tab is not live:
    # the tab says more than a generic row status.
    if row_state in (UNKNOWN, LIVE) and tab_state not in (UNKNOWN, LIVE):
        return tab_state
    return row_state if row_state != UNKNOWN else tab_state


def count_statuses(listings):
    """{status: count} for listings that each carry a ``state`` key (zeros omitted)."""
    counts = {}
    for item in listings or []:
        state = (item or {}).get("state") or UNKNOWN
        counts[state] = counts.get(state, 0) + 1
    return counts


def format_counts(counts):
    """'3 LIVE, 1 PENDING, 2 REJECTED' in a stable order; 'No listings' when empty."""
    parts = [f"{counts[s]} {s}" for s in STATUS_ORDER if counts.get(s)]
    return ", ".join(parts) if parts else "No listings"


def dedupe_listings(listings):
    """Drop repeats of one listing seen on several tabs or while scrolling.

    Keyed by Facebook's listing ID when present, otherwise by title + price +
    status, so two real listings with the same title are kept apart by ID.
    """
    seen = set()
    unique = []
    for item in listings or []:
        item = item or {}
        listing_id = _clean(item.get("listingId"))
        key = (
            ("id", listing_id)
            if listing_id
            else (
                "text",
                _clean(item.get("title")).lower(),
                _clean(item.get("price")),
                item.get("state"),
                _clean(item.get("status")),
                _clean(item.get("listed")),
                item.get("clicksCount"),
                # New-table rows are unique per tab + position; older cards have none.
                (item.get("tab"), item.get("index")) if item.get("index") else None,
            )
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def format_listing_details(listings, limit=40, max_chars=45000):
    """One line per listing for a sheet cell, e.g.
    'LIVE | Roof repair | 3 clicks | 1 msg | ID 123'.

    Google Sheets cells hold 50,000 characters, so output is capped.
    """
    lines = []
    for item in (listings or [])[:limit]:
        parts = [item.get("state") or UNKNOWN, _clean(item.get("title")) or "Untitled"]
        clicks = item.get("clicksCount")
        messages = item.get("messagesCount")
        if clicks is not None:
            parts.append(f"{clicks} click" + ("" if clicks == 1 else "s"))
        if messages is not None:
            parts.append(f"{messages} msg" + ("" if messages == 1 else "s"))
        if _clean(item.get("reason")):
            parts.append(_clean(item.get("reason")))
        if _clean(item.get("listingId")):
            parts.append(f"ID {_clean(item.get('listingId'))}")
        lines.append(" | ".join(parts))
    extra = len(listings or []) - limit
    if extra > 0:
        lines.append(f"... and {extra} more")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def build_scan_result(listings, tab_counts=None, notes=None):
    """Summary dict for one profile: counts, headline text and the details cell.

    ``notes`` lists anything that made the scan incomplete (tab skipped, fewer
    rows than the tab badge claims); the caller reports it as PARTIAL.
    """
    unique = dedupe_listings(listings)
    counts = count_statuses(unique)
    return {
        "notes": list(notes or []),
        "total": len(unique),
        "counts": counts,
        "headline": format_counts(counts),
        "details": format_listing_details(unique),
        "tabCounts": tab_counts or {},
        "listings": unique,
    }


def tab_total_mismatch(tab_counts, listings):
    """Warn when fewer rows were read than Facebook's tab badges claim.

    ``tab_counts`` is {tab label: count shown on the tab}. Returns a message or
    None. A mismatch means the virtual table was not scrolled fully.
    """
    claimed = sum(int(v) for v in (tab_counts or {}).values() if isinstance(v, int))
    read = len(listings or [])
    if claimed and read < claimed:
        return f"read {read} of {claimed} listings shown on the tabs"
    return None


# --- Page diagnosis -----------------------------------------------------------

def diagnose_empty_page(url="", lang="", body_text="", logged_out=False, checkpoint=False):
    """Pick the most likely reason code when no listings could be read."""
    if logged_out:
        return LOGGED_OUT
    if checkpoint:
        return CHECKPOINT
    lowered = _clean(body_text).lower()
    if lang and not str(lang).lower().startswith("en"):
        return LANG_NOT_EN
    if any(
        phrase in lowered
        for phrase in (
            "marketplace isn't available",
            "marketplace is not available",
            "isn't available to you",
            "you can't use marketplace",
            "can't access marketplace",
            "marketplace has been restricted",
        )
    ):
        return NO_MARKETPLACE
    if "/login" in (url or "") or "/checkpoint" in (url or ""):
        return CHECKPOINT if "/checkpoint" in url else LOGGED_OUT
    if not lowered:
        return TIMEOUT
    if "you haven't listed" in lowered or "no listings" in lowered or "start selling" in lowered:
        return NO_LISTINGS
    return LAYOUT_UNKNOWN


def format_check_result(code, detail=""):
    """'OK' or 'LAYOUT_UNKNOWN: <detail>' for the sheet's Check Result column."""
    detail = _clean(detail)
    return f"{code}: {detail}" if detail and code != OK else code


def timestamp(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
