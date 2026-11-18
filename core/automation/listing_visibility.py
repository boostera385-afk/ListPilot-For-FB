"""Pure helpers for the buyer-side visibility check (Live Checker v2).

The owner profile says a listing is LIVE; two other accounts (one Pakistani,
one with a confirmed USA location) then look for it as a buyer would. No
browser or network access here, so everything is unit tested.
"""
import re
from urllib.parse import quote

from core.utils.smart_match import loose_key
from core.automation import listing_status as ls

# --- Result per listing per checker ------------------------------------------
SEARCH = "SEARCH"        # shown in Marketplace search results
LINK_ONLY = "LINK_ONLY"  # opens by its link but did not show in search
HIDDEN = "HIDDEN"        # link says the listing is not available
UNCLEAR = "UNCLEAR"      # page loaded but could not be read
ERROR = "ERROR"          # checker could not check it (logged out, timeout, ...)

MAX_TARGETS_PER_PROFILE = 15  # listings verified per owner profile, per checker

# Checker accounts: (label used in logs and column names, config key).
CHECKERS = (
    ("PK", "live_check_pk_profile"),
    ("US", "live_check_us_profile"),
)

FB_SEARCH = "https://www.facebook.com/marketplace/search/?query={query}&exact=false"
FB_ITEM = "https://www.facebook.com/marketplace/item/{id}/"

_UNAVAILABLE = (
    "this listing isn't available",
    "this listing is no longer available",
    "listing isn't available anymore",
    "this content isn't available",
    "content isn't available right now",
    "this page isn't available",
    "listing not found",
)


def _clean(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def checker_column(label):
    return f"{label} Check"


def visibility_targets(listings, limit=MAX_TARGETS_PER_PROFILE):
    """LIVE listings with a title, at most ``limit``, as ``(targets, skipped)``."""
    live = [
        {"title": _clean(item.get("title")), "listingId": _clean(item.get("listingId"))}
        for item in listings or []
        if (item or {}).get("state") == ls.LIVE and _clean(item.get("title"))
    ]
    return live[:limit], max(0, len(live) - limit)


def search_url(title):
    return FB_SEARCH.format(query=quote(_clean(title)[:100]))


def item_url(listing_id):
    return FB_ITEM.format(id=quote(_clean(listing_id), safe=""))


def item_id_from_href(href):
    match = re.search(r"/marketplace/item/(\d+)", str(href or ""))
    return match.group(1) if match else ""


def _title_in(title, text):
    key = loose_key(title)
    return bool(key) and key in loose_key(text)


def found_in_search(results, target):
    """True when a search result is the target: same listing ID, else same title.

    ``results`` are ``{"href", "text"}`` dicts read from the results page.
    """
    listing_id = _clean(target.get("listingId"))
    for result in results or []:
        result_id = item_id_from_href(result.get("href"))
        if listing_id:
            if result_id == listing_id:
                return True
        elif _title_in(target.get("title"), result.get("text")):
            return True
    return False


def classify_item_page(url, body_text, title):
    """SEARCH-fallback result for the listing's own page: LINK_ONLY, HIDDEN or UNCLEAR."""
    lowered = _clean(body_text).lower()
    if any(phrase in lowered for phrase in _UNAVAILABLE):
        return HIDDEN
    if "/marketplace/item/" not in str(url or ""):
        return HIDDEN  # Facebook redirected away from the listing
    if _title_in(title, body_text):
        return LINK_ONLY
    return UNCLEAR


def summarize_checker(results):
    """'3/4 in search, 1 link only' style text for one checker's column."""
    if not results:
        return "Not checked"
    total = len(results)
    counts = {}
    for state in results:
        counts[state] = counts.get(state, 0) + 1
    parts = [f"{counts.get(SEARCH, 0)}/{total} in search"]
    for state, word in ((LINK_ONLY, "link only"), (HIDDEN, "hidden"), (UNCLEAR, "unclear"), (ERROR, "error")):
        if counts.get(state):
            parts.append(f"{counts[state]} {word}")
    return ", ".join(parts)


def overall_visibility(per_checker):
    """One code for the row from ``{label: [state, ...]}``.

    HIDDEN beats everything (a buyer cannot open it), then not-in-search, then
    gaps in the check. OK means every checker found every listing in search.
    """
    states = [s for results in per_checker.values() for s in results]
    if not states:
        return "NOT_CHECKED"
    hidden = sum(1 for s in states if s == HIDDEN)
    if hidden:
        return f"HIDDEN: {hidden} result(s)"
    link_only = sum(1 for s in states if s == LINK_ONLY)
    if link_only:
        return f"NOT_IN_SEARCH: {link_only} result(s)"
    gaps = sum(1 for s in states if s in (UNCLEAR, ERROR))
    if gaps:
        return f"PARTIAL: {gaps} result(s) not checked"
    return ls.OK


def format_visibility_details(targets, per_checker, max_chars=45000):
    """'Roof repair | ID 123 | PK: SEARCH | US: LINK_ONLY', one line per listing."""
    lines = []
    for index, target in enumerate(targets or []):
        parts = [target.get("title") or "Untitled"]
        if target.get("listingId"):
            parts.append(f"ID {target['listingId']}")
        for label, results in per_checker.items():
            parts.append(f"{label}: {results[index] if index < len(results) else ERROR}")
        lines.append(" | ".join(parts))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def row_columns(targets, per_checker, skipped=0):
    """Sheet values for one owner row after both checkers ran."""
    values = {checker_column(label): summarize_checker(results) for label, results in per_checker.items()}
    result = overall_visibility(per_checker)
    if skipped and result == ls.OK:
        result = f"PARTIAL: {skipped} listing(s) over the {MAX_TARGETS_PER_PROFILE} limit not checked"
    values["Visibility"] = result
    values["Visibility Details"] = format_visibility_details(targets, per_checker)
    values["Visibility Checked"] = ls.timestamp()
    return values
