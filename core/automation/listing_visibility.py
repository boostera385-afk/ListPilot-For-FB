"""Pure helpers for the buyer-side visibility check (Live Checker v2).

The owner profile says a listing is LIVE; two other accounts (one Pakistani,
one with a confirmed USA location) then look for it as a buyer would. No
browser or network access here, so everything is unit tested.
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

from core.utils.smart_match import loose_key
from core.automation import listing_status as ls
from core.automation.listing_dates import normalize_window
from core.automation import issues as iss

# --- Result per listing per checker ------------------------------------------
SEARCH = "SEARCH"        # shown in Marketplace search results
LINK_ONLY = "LINK_ONLY"  # opens by its link but did not show in search
HIDDEN = "HIDDEN"        # link says the listing is not available
UNCLEAR = "UNCLEAR"      # page loaded but could not be read
ERROR = "ERROR"          # checker could not check it (logged out, timeout, ...)
LOGIN_REQUIRED = "LOGIN_REQUIRED"
CHECKPOINT = "CHECKPOINT"
NETWORK_ERROR = "NETWORK_ERROR"
ID_MISSING = "ID_MISSING"
# Account/connection problems share the codes in core.automation.issues.
PROXY_ERROR = iss.PROXY_ERROR
PAGE_TIMEOUT = iss.PAGE_TIMEOUT
TWO_FACTOR = iss.TWO_FACTOR
TEMP_BLOCKED = iss.TEMP_BLOCKED
ACCOUNT_DISABLED = iss.ACCOUNT_DISABLED
NO_MARKETPLACE = iss.NO_MARKETPLACE
PROFILE_START_FAILED = iss.PROFILE_START_FAILED
PROFILE_PROXY_FAILED = iss.PROFILE_PROXY_FAILED
BUYER_FAILURE_STATES = (UNCLEAR, ERROR, LOGIN_REQUIRED, CHECKPOINT, NETWORK_ERROR, PROXY_ERROR, PAGE_TIMEOUT,
                        TWO_FACTOR, TEMP_BLOCKED, ACCOUNT_DISABLED, NO_MARKETPLACE, PROFILE_START_FAILED,
                        PROFILE_PROXY_FAILED)
UNVERIFIED_STATES = (*BUYER_FAILURE_STATES, ID_MISSING)
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
    owner_enabled: bool = True
    listed_window: str = "all"  # listing_dates.WINDOWS key: only listings posted in this window

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
            owner_enabled=flag("live_check_owner_enabled"),
            listed_window=normalize_window(settings.get("live_check_listed_window")),
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
BUYER_TARGETS_COLUMN = "Buyer Listing Targets"

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


def owner_buyer_columns(options, has_live=False, owner_failed=False):
    """Keep disabled buyers distinct from enabled buyers with no seller sample."""
    active = dict(options.checkers)
    pending = bool(active and has_live)
    idle = "Seller scan failed" if owner_failed else "No live listings"
    return {
        **{checker_column(label): ("Waiting" if pending else f"Not checked: {idle.lower()}")
           if label in active else "Off" for label in ("PK", "US")},
        "PK vs US": ("Waiting" if pending else "Incomplete: seller scan failed" if owner_failed
                     else "Not compared: no live listings") if len(active) == 2
                    else "Not compared: both buyers must be enabled and configured",
        "Visibility": "PENDING" if pending else "NOT_CHECKED" if active else "SKIPPED",
        "Visibility Details": (
            f"Buyer sample: up to {options.limit} live listings per enabled buyer. Not a full visibility audit."
            if pending else "Buyer checks off." if not active else
            f"{idle}; enabled buyers were not run. No visibility conclusion."
        ),
        "Visibility Checked": "",
        "Buyer Summary": ("Seller scan finished; buyer checks are waiting." if pending else
                          "Buyer checks are off." if not active else f"{idle}; buyer checks did not run."),
        "Next Step": ("Wait for the buyer checks to finish." if pending else
                      "Fix the seller scan, then retry." if owner_failed and active else
                      "Enable buyer checks in Settings if needed." if not active else "No live listings to sample."),
        "Buyer Search Area": "",
    }


def visibility_targets(listings, limit=MAX_TARGETS_PER_PROFILE):
    """LIVE listings with a title, at most ``limit``, as ``(targets, skipped)``."""
    all_live = [item for item in listings or [] if (item or {}).get("state") == ls.LIVE]
    live = [{"title": _clean(item.get("title")), "listingId": _clean(item.get("listingId")),
             "price": _clean(item.get("price")),
             **({"cardId": item["cardId"]} if item.get("cardId") else {})}
            for item in all_live if _clean(item.get("title"))]
    # Prefer exact listing IDs (a shared service title cannot identify its owner), then
    # different services before repeats of one, so a small sample covers more than one title.
    seen, repeat = set(), {}
    for index, item in enumerate(live):
        key = (loose_key(item["title"]), item["price"].lower())
        repeat[index] = key in seen
        seen.add(key)
    order = sorted(range(len(live)), key=lambda index: (not live[index]["listingId"], repeat[index]))
    live = [live[index] for index in order]
    return live[:limit], len(all_live) - min(limit, len(live))


def parse_buyer_targets(value):
    """Explicit buyer-only input. Reject malformed lines rather than guessing an ID."""
    targets = {}
    for number, line in enumerate(str(value or "").splitlines(), 1):
        if not line.strip():
            continue
        title, separator, reference = line.rpartition("|")
        reference = reference.strip()
        listing_id = reference if re.fullmatch(r"[0-9]+", reference) else ""
        if not listing_id:
            try:
                url = urlsplit(reference)
                if (url.scheme == "https" and url.netloc.lower() in
                        {"facebook.com", "www.facebook.com", "m.facebook.com"}):
                    listing_id = item_id_from_href(reference)
            except ValueError:
                pass
        if not separator or not _clean(title) or not listing_id:
            raise ValueError(f"Line {number}: use title | Facebook Marketplace item URL or listing ID.")
        target = {"title": _clean(title), "listingId": listing_id}
        if listing_id in targets and targets[listing_id] != target:
            raise ValueError(f"Line {number}: the listing ID already has a different title.")
        targets[listing_id] = target
    return list(targets.values())


def search_url(title):
    return FB_SEARCH.format(query=quote(_clean(title)[:100]))


def search_queries(target):
    """Bounded title + service-keyword plan; keywords never establish identity or geography."""
    title = _clean(target.get("title"))[:100]
    if not title:
        return []
    noise = {"a", "an", "and", "the", "for", "of", "in", "to", "with", "near", "me",
             "service", "services", "professional", "affordable", "best", "quality",
             "call", "text", "us", "free", "estimate", "estimates"}
    words = []
    for word in re.findall(r"[^\W_]+", title.casefold()):
        if word in noise or (word.isdigit() and len(word) >= 7) or word in words:
            continue
        words.append(word)
    keywords = " ".join(words[:6])
    return [title, keywords] if keywords and keywords.casefold() != title.casefold() else [title]


def item_url(listing_id):
    return FB_ITEM.format(id=quote(_clean(listing_id), safe=""))


def item_id_from_href(href):
    try:
        url = urlsplit(str(href or ""))
    except ValueError:
        return ""
    if url.netloc and url.hostname not in ("facebook.com", "www.facebook.com", "m.facebook.com"):
        return ""
    match = re.fullmatch(r"/marketplace/item/([0-9]+)/?", url.path)
    return match.group(1) if match else ""


def matches_search_page(requested_url, current_url):
    """Require the requested query on a Marketplace search route, allowing a city redirect."""
    try:
        wanted, actual = urlsplit(requested_url), urlsplit(current_url)
        query = parse_qs(wanted.query).get("query")
        return bool(query and actual.scheme == wanted.scheme and actual.netloc == wanted.netloc
                    and re.search(r"/marketplace/(?:[^/]+/)?search/?$", actual.path)
                    and parse_qs(actual.query).get("query") == query)
    except ValueError:
        return False


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
        SEARCH: "Found in search", TEXT_MATCH: "Similar listing found; seller not confirmed",
        LINK_ONLY: "Link opens; not seen in sampled search", NOT_FOUND: "No match in sampled search (no ID)",
        HIDDEN: "Item page unavailable", UNCLEAR: "Could not check search; please retry", ERROR: "Check failed; please retry",
        LOGIN_REQUIRED: "Login needed", CHECKPOINT: "Account check needed",
        NETWORK_ERROR: "Connection interrupted", PROXY_ERROR: "Proxy failed", PAGE_TIMEOUT: "Page too slow",
        TWO_FACTOR: "Login code needed", TEMP_BLOCKED: "Temporarily blocked", ACCOUNT_DISABLED: "Account disabled",
        NO_MARKETPLACE: "No Marketplace", PROFILE_START_FAILED: "Buyer profile did not open",
        PROFILE_PROXY_FAILED: "Buyer proxy failed at launch",
        ID_MISSING: "Seller link missing; retry the seller scan",
    }.get(state, str(state))


def listing_verdict(states):
    """One verdict for a listing from its buyers' results (search visibility first)."""
    states = list(states)
    if not states:
        return "Not checked"
    found = SEARCH in states
    missed = LINK_ONLY in states
    unchecked = any(s in (*UNVERIFIED_STATES, TEXT_MATCH, NOT_FOUND) for s in states)
    if HIDDEN in states:
        return "Item unavailable to a buyer"
    note = " (a buyer could not check)" if unchecked else ""
    if found and not missed:
        return "Visible in search" + note
    if found:
        return "Visible to some buyers"
    if missed:
        return "Not seen in sampled search" + note
    if TEXT_MATCH in states:
        return "Possible title + price match"
    if ID_MISSING in states:
        return "Seller link missing — retry the seller scan"
    return "Not verified"


def summarize_buyer_rows(rows):
    """'Buyer sample: 9 listings checked - 1 visible in search - 8 not in search' (empty when nothing ran)."""
    rows = list(rows or [])
    if not rows:
        return ""
    counts = {"visible": 0, "some": 0, "not": 0, "hidden": 0, "unverified": 0}
    for row in rows:
        verdict = listing_verdict([row[label] for label in ("PK", "US") if label in row])
        if verdict.startswith("Visible in search"):
            counts["visible"] += 1
        elif verdict == "Visible to some buyers":
            counts["some"] += 1
        elif verdict.startswith("Item unavailable"):
            counts["hidden"] += 1
        elif verdict.startswith("Not seen"):
            counts["not"] += 1
        else:
            counts["unverified"] += 1
    words = (("visible", "found by at least one buyer"), ("some", "visible to some buyers"), ("not", "not seen in sampled search"),
             ("hidden", "item unavailable"), ("unverified", "not verified"))
    parts = [f"{len(rows)} listing{'s' if len(rows) != 1 else ''} checked"]
    parts += [f"{counts[key]} {word}" for key, word in words if counts[key]]
    incomplete = sum(any(row.get(label) in UNVERIFIED_STATES for label in ("PK", "US")) for row in rows)
    text_only = sum(any(row.get(label) == TEXT_MATCH for label in ("PK", "US")) and
                    not any(row.get(label) == SEARCH for label in ("PK", "US")) for row in rows)
    if incomplete:
        parts.append(f"{incomplete} with incomplete buyer checks")
    if text_only:
        parts.append(f"{text_only} title+price only (identity unverified)")
    return "Buyer sample: " + " · ".join(parts)


def classify_item_page(url, headings, title, requested_url):
    """Verify exact route/ID and item heading, never arbitrary body text or redirected errors."""
    try:
        actual, expected = urlsplit(url), urlsplit(requested_url)
    except ValueError:
        return UNCLEAR
    if (actual.scheme, actual.netloc, actual.path.rstrip("/")) != (
        expected.scheme, expected.netloc, expected.path.rstrip("/")
    ):
        return UNCLEAR
    clean_headings = [_clean(value).casefold().rstrip(".") for value in headings]
    if any(value in _UNAVAILABLE for value in clean_headings):
        return HIDDEN
    if _clean(title).casefold() in clean_headings:
        return LINK_ONLY
    return UNCLEAR


def summarize_checker(results):
    """Plain-language observations; an unread search is never presented as a zero score."""
    if not results:
        return "Not checked"
    total = len(results)
    counts = {}
    for state in results:
        counts[state] = counts.get(state, 0) + 1
    unverified = sum(counts.get(state, 0) for state in UNVERIFIED_STATES)
    if counts.get(ID_MISSING) == total:
        return f"Not checked: exact seller links are missing for {total} sampled listing(s). Retry the seller scan."
    if unverified == total:
        parts = [f"Could not check any of {total} listings"]
    else:
        parts = [f"Found in search: {counts.get(SEARCH, 0)} of {total} sampled"]
    if counts.get(TEXT_MATCH):
        parts.append(f"{counts[TEXT_MATCH]} similar listing(s); seller not confirmed")
    for state, word in ((LINK_ONLY, "link(s) open; not seen in search"), (NOT_FOUND, "no confirmed match; listing ID missing"),
                        (HIDDEN, "item page(s) unavailable"),
                        (UNCLEAR, "search check(s) could not finish"), (ERROR, "check(s) failed"), (LOGIN_REQUIRED, "login needed"),
                        (CHECKPOINT, "account check needed"), (NETWORK_ERROR, "connection interrupted"),
                        (PROXY_ERROR, "proxy failed"), (PAGE_TIMEOUT, "page too slow"),
                        (TWO_FACTOR, "login code needed"), (TEMP_BLOCKED, "temporarily blocked"),
                        (ACCOUNT_DISABLED, "account disabled"), (NO_MARKETPLACE, "Marketplace unavailable"),
                        (PROFILE_START_FAILED, "buyer profile did not open"),
                        (PROFILE_PROXY_FAILED, "buyer proxy failed at launch")):
        if counts.get(state):
            parts.append(f"{counts[state]} {word}")
    if counts.get(ID_MISSING):
        parts.append(f"{counts[ID_MISSING]} not checked; exact seller link missing")
    return ". ".join(parts) + "."


def compare_regions(per_checker, expected=None):
    """Compare the same listing positions, never just the two aggregate counts.

    Same means matching observed outcomes, not that a listing is visible.
    Title+price hits and misses without an ID cannot verify regional agreement.
    Missing/error results take precedence over observed differences.
    """
    if "PK" not in per_checker or "US" not in per_checker:
        return "Not compared: both buyers must be enabled and configured"
    pk, us = per_checker["PK"], per_checker["US"]
    total = max(len(pk), len(us)) if expected is None else expected
    if len(pk) > total or len(us) > total:
        return "Incomplete: buyer sample sizes differ"
    if not total:
        return "Not compared: no sampled listings"
    outcomes = {SEARCH: SEARCH, LINK_ONLY: LINK_ONLY, HIDDEN: HIDDEN}
    same = different = unverified = 0
    for index in range(total):
        left = outcomes.get(pk[index]) if index < len(pk) else None
        right = outcomes.get(us[index]) if index < len(us) else None
        if left is None or right is None:
            unverified += 1
        elif left == right:
            same += 1
        else:
            different += 1
    if unverified:
        result = f"Incomplete: {unverified}/{total} unverified; {same} same, {different} different"
    elif different:
        result = f"Different: {different}/{total} sampled listings; {same} same"
    else:
        result = f"Same: {same}/{total} sampled listings"
    if TEXT_MATCH in pk or TEXT_MATCH in us:
        result += " (includes title+price matches; identity unverified)"
    return result


def overall_visibility(per_checker):
    """One code for the row from ``{label: [state, ...]}``.

    An unavailable item page is a warning; otherwise incomplete buyer checks
    take precedence over observed search misses. OK means every sampled check found an exact ID.
    """
    states = [s for results in per_checker.values() for s in results]
    if not states:
        return "NOT_CHECKED"
    hidden = sum(1 for s in states if s == HIDDEN)
    if hidden:
        return f"HIDDEN: {hidden} result(s)"
    gaps = sum(1 for s in states if s not in (SEARCH, TEXT_MATCH, LINK_ONLY))
    if gaps:
        return f"PARTIAL: {gaps} buyer check(s) could not be completed"
    text_only = sum(1 for s in states if s == TEXT_MATCH)
    if text_only:
        return f"PARTIAL: {text_only} similar listing(s); seller not confirmed"
    link_only = states.count(LINK_ONLY)
    if link_only:
        return f"NOT_IN_SEARCH: {link_only} link(s) open; not seen in sampled search"
    return ls.OK


# Most specific failure first: a logged-out buyer explains its unreadable searches.
_FAILURE_PRIORITY = (ACCOUNT_DISABLED, TEMP_BLOCKED, LOGIN_REQUIRED, TWO_FACTOR, CHECKPOINT, NO_MARKETPLACE,
                     PROFILE_PROXY_FAILED, PROFILE_START_FAILED, PROXY_ERROR, NETWORK_ERROR, PAGE_TIMEOUT,
                     UNCLEAR, ERROR)
_FAILURE_WORDS = {
    LOGIN_REQUIRED: "logged out of Facebook",
    TWO_FACTOR: "Facebook asks for a login code",
    CHECKPOINT: "Facebook account check needed",
    TEMP_BLOCKED: "Facebook temporarily blocked the account",
    ACCOUNT_DISABLED: "Facebook account disabled",
    NO_MARKETPLACE: "Marketplace not available to the account",
    PROFILE_PROXY_FAILED: "profile could not start with its proxy",
    PROFILE_START_FAILED: "profile did not open",
    PROXY_ERROR: "proxy connection failed",
    NETWORK_ERROR: "internet/proxy connection failed",
    PAGE_TIMEOUT: "Facebook pages loaded too slowly",
    UNCLEAR: "search page could not be read",
    ERROR: "check failed",
}
_FAILURE_ACTIONS = {
    LOGIN_REQUIRED: "Open the {labels} buyer profile and sign in to Facebook",
    TWO_FACTOR: "Open the {labels} buyer profile and enter Facebook's login code",
    CHECKPOINT: "Open the {labels} buyer profile and complete Facebook's account check",
    TEMP_BLOCKED: "Rest the {labels} buyer account for several hours, or use another buyer account",
    ACCOUNT_DISABLED: "Replace the {labels} buyer account in Settings (Facebook disabled it)",
    NO_MARKETPLACE: "Use a {labels} buyer account that has Marketplace",
    PROFILE_PROXY_FAILED: "Fix or replace the {labels} buyer profile's proxy in Incogniton, then retry",
    PROFILE_START_FAILED: "Open Incogniton and make sure the {labels} buyer profile starts, then retry",
    PROXY_ERROR: "Check the {labels} buyer profile's proxy in Incogniton (internet/proxy online, login correct), then retry",
    NETWORK_ERROR: "Check the {labels} buyer's internet/proxy connection, then retry",
    PAGE_TIMEOUT: "The {labels} buyer's internet/proxy is too slow; check it or retry later",
    UNCLEAR: "Open the {labels} buyer profile, confirm Marketplace search loads, then retry",
    ERROR: "Retry the {labels} buyer check",
}


def buyer_failures(per_checker):
    """``[(failure state, [labels])]`` grouped by each buyer's most specific failure."""
    groups = {}
    for label, results in per_checker.items():
        state = next((s for s in _FAILURE_PRIORITY if s in results), None)
        if state:
            groups.setdefault(state, []).append(label)
    return [(state, groups[state]) for state in _FAILURE_PRIORITY if state in groups]


def buyer_guidance(per_checker, skipped=0):
    """A short outcome and an actionable next step, independent of technical result codes."""
    states = [state for results in per_checker.values() for state in results]
    if not states:
        return "Buyer checks have not run.", "Run buyer checks to see what another account can access."
    failures = buyer_failures(per_checker)
    found = states.count(SEARCH)
    links = states.count(LINK_ONLY)
    notes = []
    if failures:
        notes.append("Check incomplete: " + "; ".join(
            f"{', '.join(labels)} buyer could not finish ({_FAILURE_WORDS[state]})" for state, labels in failures))
    if found:
        notes.append(f"{found} buyer check(s) found the exact listing in search")
    if links:
        notes.append(f"{links} buyer check(s): link opens, not seen in search")
    if TEXT_MATCH in states:
        notes.append("Similar listings found, but the seller could not be confirmed")
    if HIDDEN in states:
        notes.append(f"{states.count(HIDDEN)} item page(s) unavailable to a buyer")
    if NOT_FOUND in states:
        notes.append("No confirmed match for listing(s) without an ID")
    if ID_MISSING in states:
        notes.append("Exact seller links are missing; buyer visibility could not be verified")
    if skipped:
        notes.append(f"Only a sample was checked; {skipped} other listing(s) were not checked")
    action = [_FAILURE_ACTIONS[state].format(labels=", ".join(labels)) for state, labels in failures]
    if HIDDEN in states:
        action.append("Open the affected item link manually; unavailable does not prove an account ban")
    if TEXT_MATCH in states or NOT_FOUND in states or ID_MISSING in states:
        action.append("Re-run the seller scan to collect exact listing links")
    if links:
        action.append("Check the buyer's Marketplace city/ZIP and radius near the listing, then retry search")
    if not action:
        action.append("No issue found in this sample" if not skipped else "Use a larger sample if needed")
    return ". ".join(notes) + ".", ". ".join(action) + "."


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
            parts.append(f"{label}: {state_label(results[index] if index < len(results) else ERROR)}")
        lines.append(" | ".join(parts))
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def visibility_label(code):
    if str(code).startswith("PARTIAL:") and "outside this sample" in str(code):
        return "Sample passed — other listings not checked"
    return {
        "OK": "Found in search", "NOT_IN_SEARCH": "Link opens; not seen in sample",
        "HIDDEN": "Item page unavailable", "PARTIAL": "Check incomplete — see next step",
        "NOT_CHECKED": "Not checked", "SKIPPED": "Buyer checks off", "STOPPED": "Stopped",
        "PENDING": "Waiting for buyers",
    }.get(str(code).split(":")[0], str(code))


def row_columns(targets, per_checker, skipped=0, disabled=(), elapsed=None, owner_scanned=True):
    """Sheet values for one owner row after both checkers ran."""
    complete = {label: [states[i] if i < len(states) else UNCLEAR for i in range(len(targets))]
                for label, states in per_checker.items()}
    values = {checker_column(label): summarize_checker(results) for label, results in complete.items()}
    for label in disabled:
        values[checker_column(label)] = "Off"
    values["PK vs US"] = compare_regions(per_checker, expected=len(targets))
    result = overall_visibility(complete)
    if skipped and result == ls.OK:
        result = f"PARTIAL: {skipped} listing(s) outside this sample"
    values["Visibility"] = result
    values["Buyer Summary"], values["Next Step"] = buyer_guidance(complete, skipped)
    source = "live listings" if owner_scanned else "provided targets (owner scan off; seller status not checked)"
    scope = f"Sample: {len(targets)} of {len(targets) + skipped} {source} per enabled buyer."
    if skipped:
        scope += f" {skipped} not checked."
    scope += " Search results depend on the buyer account and Marketplace location."
    scope += " Search uses the title, then a keyword fallback when different. Saved city/radius is not changed or independently verified."
    scope += " 'In search' requires the exact listing ID. 'Link only' opens by link but was not seen in sampled search results."
    scope += f" PK vs US: {values['PK vs US']}. Matching results do not necessarily mean visible listings."
    if elapsed is not None:
        scope += f" Buyer check took {ls.format_duration(elapsed)}."
    values["Visibility Details"] = (values["Buyer Summary"] + "\nNext step: " + values["Next Step"] +
                                    "\n" + scope + "\n" + format_visibility_details(targets, per_checker))
    values["Visibility Checked"] = ls.timestamp()
    return values
