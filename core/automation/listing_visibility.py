"""Pure helpers for the buyer-side visibility check (Live Checker v2).

The owner profile says a listing is LIVE; two other accounts (one Pakistani,
one with a confirmed USA location) then look for it as a buyer would. No
browser or network access here, so everything is unit tested.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote

from core.utils.smart_match import loose_key
from core.automation import listing_status as ls

# --- Result per listing per checker ------------------------------------------
SEARCH = "SEARCH"        # shown in Marketplace search results
LINK_ONLY = "LINK_ONLY"  # opens by its link but did not show in search
HIDDEN = "HIDDEN"        # link says the listing is not available
UNCLEAR = "UNCLEAR"      # page loaded but could not be read
ERROR = "ERROR"          # checker could not check it (logged out, timeout, ...)
TEXT_MATCH = "TEXT_MATCH"  # in search by exact title + price; no listing ID to confirm it
NOT_FOUND = "NOT_FOUND"    # searched, not in the results, and no listing ID to try the link

MAX_TARGETS_PER_PROFILE = 15  # listings verified per owner profile, per checker
DEFAULT_SAMPLE_SIZE = 3

# Checker accounts: (label used in logs and column names, config key).
CHECKERS = (
    ("PK", "live_check_pk_profile"),
    ("US", "live_check_us_profile"),
)


@dataclass(frozen=True)
class BuyerOptions:
    """A settings snapshot: changing switches affects the next run, not this one."""
    enabled: bool = True
    pk_enabled: bool = True
    us_enabled: bool = True
    pk_profile: str = ""
    us_profile: str = ""
    limit: int = DEFAULT_SAMPLE_SIZE
    open_details: bool = False

    @classmethod
    def from_settings(cls, settings):
        def flag(key, default=True):
            value = settings.get(key, default)
            return value if isinstance(value, bool) else str(value).lower() not in ("false", "0", "off", "")
        try:
            limit = max(1, min(MAX_TARGETS_PER_PROFILE, int(settings.get("live_check_sample_size", DEFAULT_SAMPLE_SIZE))))
        except (TypeError, ValueError):
            limit = DEFAULT_SAMPLE_SIZE
        return cls(
            enabled=flag("live_check_buyers_enabled"),
            pk_enabled=flag("live_check_pk_enabled"), us_enabled=flag("live_check_us_enabled"),
            pk_profile=str(settings.get("live_check_pk_profile") or "").strip(),
            us_profile=str(settings.get("live_check_us_profile") or "").strip(),
            limit=limit, open_details=flag("live_check_open_details", False),
        )

    @property
    def checkers(self):
        if not self.enabled:
            return ()
        return tuple((label, profile) for label, enabled, profile in (
            ("PK", self.pk_enabled, self.pk_profile), ("US", self.us_enabled, self.us_profile),
        ) if enabled and profile)

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
        {"title": _clean(item.get("title")), "listingId": _clean(item.get("listingId")),
         "price": _clean(item.get("price"))}
        for item in listings or []
        if (item or {}).get("state") == ls.LIVE and _clean(item.get("title"))
    ]
    # Prefer exact listing IDs. A shared service title cannot identify its owner.
    live.sort(key=lambda item: not bool(item["listingId"]))
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
    """True only when a search result has the target's exact listing ID.

    ``results`` are ``{"href", "text"}`` dicts read from the results page.
    """
    listing_id = _clean(target.get("listingId"))
    for result in results or []:
        result_id = item_id_from_href(result.get("href"))
        if listing_id:
            if result_id == listing_id:
                return True
    return False


def match_in_search(results, target, need=1):
    """How a target shows in search results: ``"ID"``, ``"TEXT"`` or ``""`` (not found).

    ``need`` is how many identical listings (same title and price, no ID) the seller has
    in the sample so far: a text match then needs that many distinct result cards, so two
    identical listings are not both "found" by a single card.

    ``"ID"`` is an exact Facebook listing-ID hit. ``"TEXT"`` is used only for a
    listing that has no ID (older seller pages): a result card whose lines hold
    the exact title and the exact price. It is reported separately because the
    same seller can have several identical cards.
    """
    if found_in_search(results, target):
        return "ID"
    if _clean(target.get("listingId")):
        return ""
    title = loose_key(target.get("title"))
    price = _clean(target.get("price")).lower()
    if not title or not price:
        return ""
    cards = set()
    for result in results or []:
        lines = [_clean(line).lower() for line in str(result.get("text") or "").splitlines() if _clean(line)]
        if price in lines and any(loose_key(line) == title for line in lines):
            cards.add(item_id_from_href(result.get("href")) or _clean(result.get("href")) or " ".join(lines))
    return "TEXT" if len(cards) >= max(1, int(need or 1)) else ""


def describe_results(results, limit=3):
    """A short 'price Title place | ...' sample of what a search returned, for the log."""
    seen, parts = set(), []
    for result in results or []:
        text = " ".join(str(result.get("text") or "").split())[:70]
        if text and text not in seen:
            seen.add(text)
            parts.append(text)
        if len(parts) >= limit:
            break
    return " | ".join(parts) if parts else "no result cards"


def state_label(state):
    """Plain words for one buyer result (used in the app's Buyer checks tab)."""
    return {
        SEARCH: "In search", TEXT_MATCH: "In search (title + price)",
        LINK_ONLY: "Link opens, not in search", NOT_FOUND: "Not in search",
        HIDDEN: "Unavailable", UNCLEAR: "Not verified", ERROR: "Error",
    }.get(state, str(state))


def listing_verdict(states):
    """One verdict for a listing from its buyers' results (search visibility first)."""
    states = list(states)
    if not states:
        return "Not checked"
    found = [s for s in states if s in (SEARCH, TEXT_MATCH)]
    if HIDDEN in states:
        return "Hidden from a buyer"
    if len(found) == len(states):
        return "Visible in search" if SEARCH in found else "Visible (title + price)"
    if found:
        return "Visible to some buyers"
    if any(s in (LINK_ONLY, NOT_FOUND) for s in states):
        return "Not in search"
    return "Not verified"


def classify_item_page(url, body_text, title):
    """SEARCH-fallback result for the listing's own page: LINK_ONLY, HIDDEN or UNCLEAR."""
    lowered = _clean(body_text).lower()
    if any(phrase in lowered for phrase in _UNAVAILABLE):
        return HIDDEN
    if "/marketplace/item/" not in str(url or ""):
        return UNCLEAR  # A redirect/loading failure is not proof the listing is hidden.
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
    parts = [f"{counts.get(SEARCH, 0) + counts.get(TEXT_MATCH, 0)}/{total} in search"]
    if counts.get(TEXT_MATCH):
        parts.append(f"{counts[TEXT_MATCH]} by title+price")
    for state, word in ((LINK_ONLY, "link only"), (NOT_FOUND, "not found"), (HIDDEN, "unavailable"),
                        (UNCLEAR, "unverified"), (ERROR, "error")):
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
    link_only = sum(1 for s in states if s in (LINK_ONLY, NOT_FOUND))
    if link_only:
        return f"NOT_IN_SEARCH: {link_only} result(s)"
    gaps = sum(1 for s in states if s in (UNCLEAR, ERROR))
    if gaps:
        return f"PARTIAL: {gaps} result(s) not checked"
    text_only = sum(1 for s in states if s == TEXT_MATCH)
    if text_only:
        return f"PARTIAL: {text_only} result(s) matched by title+price only (no listing ID)"
    return ls.OK


def format_visibility_details(targets, per_checker, max_chars=45000):
    """'Roof repair | ID 123 | PK: SEARCH | US: LINK_ONLY', one line per listing."""
    lines = []
    for index, target in enumerate(targets or []):
        parts = [target.get("title") or "Untitled"]
        if target.get("price"):
            parts.append(target["price"])
        if target.get("listingId"):
            parts.append(f"ID {target['listingId']}")
        for label, results in per_checker.items():
            parts.append(f"{label}: {results[index] if index < len(results) else ERROR}")
        lines.append(" | ".join(parts))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def visibility_label(code):
    return {
        "OK": "Found in search", "NOT_IN_SEARCH": "Link opens; not in search",
        "HIDDEN": "Unavailable to buyer", "PARTIAL": "Sample / incomplete",
        "NOT_CHECKED": "Not checked", "SKIPPED": "Buyer checks off", "STOPPED": "Stopped",
        "PENDING": "Waiting for buyers",
    }.get(str(code).split(":")[0], str(code))


def row_columns(targets, per_checker, skipped=0, disabled=(), elapsed=None):
    """Sheet values for one owner row after both checkers ran."""
    values = {checker_column(label): summarize_checker(results) for label, results in per_checker.items()}
    for label in disabled:
        values[checker_column(label)] = "Off"
    result = overall_visibility(per_checker)
    if skipped and result == ls.OK:
        result = f"PARTIAL: {skipped} listing(s) outside this sample"
    values["Visibility"] = result
    scope = f"Sample: {len(targets)} of {len(targets) + skipped} live listings per enabled buyer."
    if skipped:
        scope += f" {skipped} not checked."
    scope += " Search results depend on the buyer account and Marketplace location."
    scope += " 'In search' = found by searching Marketplace as a buyer; 'link only' = opens by link but is not in search."
    if elapsed is not None:
        scope += f" Buyer check took {ls.format_duration(elapsed)}."
    values["Visibility Details"] = scope + "\n" + format_visibility_details(targets, per_checker)
    values["Visibility Checked"] = ls.timestamp()
    return values
