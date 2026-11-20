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

# --- Result per listing per checker ------------------------------------------
SEARCH = "SEARCH"        # shown in Marketplace search results
LINK_ONLY = "LINK_ONLY"  # opens by its link but did not show in search
HIDDEN = "HIDDEN"        # link says the listing is not available
UNCLEAR = "UNCLEAR"      # page loaded but could not be read
ERROR = "ERROR"          # checker could not check it (logged out, timeout, ...)
LOGIN_REQUIRED = "LOGIN_REQUIRED"
CHECKPOINT = "CHECKPOINT"
NETWORK_ERROR = "NETWORK_ERROR"
UNVERIFIED_STATES = (UNCLEAR, ERROR, LOGIN_REQUIRED, CHECKPOINT, NETWORK_ERROR)
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
        SEARCH: "In search (exact ID)", TEXT_MATCH: "Possible match (title + price)",
        LINK_ONLY: "Link opens; not seen in sampled search", NOT_FOUND: "No match in sampled search (no ID)",
        HIDDEN: "Item page unavailable", UNCLEAR: "Not verified", ERROR: "Error",
        LOGIN_REQUIRED: "Login needed", CHECKPOINT: "Account check needed",
        NETWORK_ERROR: "Connection interrupted",
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
    """'3/4 in search, 1 link only' style text for one checker's column."""
    if not results:
        return "Not checked"
    total = len(results)
    counts = {}
    for state in results:
        counts[state] = counts.get(state, 0) + 1
    parts = [f"{counts.get(SEARCH, 0)}/{total} in search"]
    if counts.get(TEXT_MATCH):
        parts.append(f"{counts[TEXT_MATCH]} possible by title+price")
    for state, word in ((LINK_ONLY, "link only"), (NOT_FOUND, "no match (no ID)"), (HIDDEN, "item unavailable"),
                        (UNCLEAR, "unverified"), (ERROR, "error"), (LOGIN_REQUIRED, "login needed"),
                        (CHECKPOINT, "account check needed"), (NETWORK_ERROR, "connection interrupted")):
        if counts.get(state):
            parts.append(f"{counts[state]} {word}")
    return ", ".join(parts)


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

    HIDDEN beats everything (a buyer cannot open it), then not-in-search, then
    gaps in the check. OK means every checker found every listing in search.
    """
    states = [s for results in per_checker.values() for s in results]
    if not states:
        return "NOT_CHECKED"
    hidden = sum(1 for s in states if s == HIDDEN)
    if hidden:
        return f"HIDDEN: {hidden} result(s)"
    link_only = states.count(LINK_ONLY)
    if link_only:
        return f"NOT_IN_SEARCH: {link_only} result(s)"
    gaps = sum(1 for s in states if s != SEARCH and s != TEXT_MATCH)
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
        "OK": "Found in search", "NOT_IN_SEARCH": "Link opens; not seen in sample",
        "HIDDEN": "Item page unavailable", "PARTIAL": "Sample / incomplete",
        "NOT_CHECKED": "Not checked", "SKIPPED": "Buyer checks off", "STOPPED": "Stopped",
        "PENDING": "Waiting for buyers",
    }.get(str(code).split(":")[0], str(code))


def row_columns(targets, per_checker, skipped=0, disabled=(), elapsed=None, owner_scanned=True):
    """Sheet values for one owner row after both checkers ran."""
    values = {checker_column(label): summarize_checker(results) for label, results in per_checker.items()}
    for label in disabled:
        values[checker_column(label)] = "Off"
    values["PK vs US"] = compare_regions(per_checker, expected=len(targets))
    result = overall_visibility({label: [states[i] if i < len(states) else UNCLEAR
                                          for i in range(len(targets))]
                                 for label, states in per_checker.items()})
    if skipped and result == ls.OK:
        result = f"PARTIAL: {skipped} listing(s) outside this sample"
    values["Visibility"] = result
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
    values["Visibility Details"] = scope + "\n" + format_visibility_details(targets, per_checker)
    values["Visibility Checked"] = ls.timestamp()
    return values
