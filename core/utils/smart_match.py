"""Forgiving text matching for values typed into Google Sheets.

Sheet users type profile names, column headers and dropdown values with
different casing, spacing, punctuation, plurals and small typos. These
helpers match them in tiers, from strict to loose, and only fall back to a
fuzzy match when there is a single clear winner.
"""
import difflib
import re
import unicodedata


def normalize_text(value):
    """Casefolded text with unified quotes/dashes and collapsed whitespace."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return " ".join(text.split()).casefold()


def loose_key(value):
    """Letters and digits only: 'Profile-Name ' -> 'profilename'."""
    return re.sub(r"[^0-9a-z]+", "", normalize_text(value))


def header_key(value):
    """Loose key that also ignores a trailing plural 's' ('Responses' == 'Response')."""
    key = loose_key(value)
    return key[:-1] if len(key) > 3 and key.endswith("s") else key


def _digits(value):
    return re.findall(r"\d+", str(value or ""))


def similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def best_fuzzy_index(target, options, cutoff=0.8, key=loose_key):
    """Index of the single clearly-best fuzzy match, or None.

    Options whose numbers differ from the target are never fuzzy matched, so
    'Profile 12' can't turn into 'Profile 13'.
    """
    target_key = key(target)
    if not target_key:
        return None
    target_digits = _digits(target)
    scored = []
    for index, option in enumerate(options):
        option_key = key(option)
        if not option_key or _digits(option) != target_digits:
            continue
        scored.append((similarity(target_key, option_key), index))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_score, best_index = scored[0]
    if best_score < cutoff:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < 0.05:
        return None
    return best_index


def match_indices(target, options, fuzzy_cutoff=0.85):
    """Return the indices of ``options`` that match ``target`` at the strictest tier.

    Tiers: exact -> case/space-insensitive -> letters-and-digits only ->
    single clear fuzzy winner. Several indices mean a genuine duplicate.
    """
    raw = str(target or "").strip()
    if not raw:
        return []
    options = [str(option or "") for option in options]
    for transform in (lambda v: v.strip(), normalize_text, loose_key):
        wanted = transform(raw)
        if not wanted:
            continue
        hits = [i for i, option in enumerate(options) if transform(option) == wanted]
        if hits:
            return hits
    fuzzy = best_fuzzy_index(raw, options, cutoff=fuzzy_cutoff)
    return [] if fuzzy is None else [fuzzy]
