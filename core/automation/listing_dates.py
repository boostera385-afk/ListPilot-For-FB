"""Posted-date window filter for the Live Checker ("Listed within").

Facebook shows a listing's date in different shapes:
  - older seller cards: ``Listed on 20/11`` (day/month) or ``Listed on 11/18``
    (month/day) depending on the account locale, sometimes with a year;
  - dialogs / newer pages: ``Posted a day ago``, ``Listed 3 hours ago``, ``5h``.

Each text becomes a time *range* (a day-only date covers the whole day), and a
listing is kept when that range overlaps the chosen window. Ranges that only
partly overlap are counted as approximate so the run can say so. No browser
access here, so everything is unit tested.
"""
import re
from datetime import datetime, timedelta

ANY = "all"
WINDOWS = {
    ANY: "Any time",
    "12h": "Last 12 hours",
    "24h": "Last 24 hours",
    "today": "Today",
    "yesterday": "Yesterday",
    "48h": "Last 2 days",
    "7d": "Last 7 days",
}

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def normalize_window(value):
    """Window key from a key or its label; unknown values mean no filter."""
    value = str(value or "").strip()
    if value in WINDOWS:
        return value
    return next((key for key, label in WINDOWS.items() if label.lower() == value.lower()), ANY)


def window_range(window, now):
    """``(start, end)`` datetimes for a window key, or None for no filter."""
    window = normalize_window(window)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "12h": (now - timedelta(hours=12), now),
        "24h": (now - timedelta(hours=24), now),
        "48h": (now - timedelta(hours=48), now),
        "7d": (now - timedelta(days=7), now),
        "today": (midnight, now),
        "yesterday": (midnight - timedelta(days=1), midnight),
    }.get(window)


def _numeric_parts(text):
    match = re.search(r"(?<![\d/])(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?(?![\d/])", str(text or ""))
    if not match:
        return None
    year = match.group(3)
    if year and len(year) == 2:
        year = "20" + year
    return int(match.group(1)), int(match.group(2)), int(year) if year else None


def infer_day_order(texts):
    """``"dm"`` / ``"md"`` from a profile's dates (any part above 12 decides), else None."""
    day_first = month_first = False
    for text in texts or []:
        parts = _numeric_parts(text)
        if not parts:
            continue
        first, second, _ = parts
        if first > 12 >= second:
            day_first = True
        elif second > 12 >= first:
            month_first = True
    if day_first != month_first:
        return "dm" if day_first else "md"
    return None


def _day_range(day, month, year, now):
    if year is None:
        year = now.year
        try:
            if datetime(year, month, day) > now:  # "Listed on 12/30" seen in January
                year -= 1
        except ValueError:
            return None
    try:
        start = datetime(year, month, day)
    except ValueError:
        return None
    if start > now:
        return None
    return start, min(start + timedelta(days=1), now)


def parse_listed(text, now, order=None):
    """Possible posting times for one date text as a list of ``(start, end)`` ranges.

    One range when the text is unambiguous; two when a numeric date could be
    day/month or month/day and ``order`` is unknown; empty when unreadable.
    """
    raw = " ".join(str(text or "").replace("\xa0", " ").split()).lower()
    if not raw:
        return []
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\b(just now|moments? ago|a few seconds ago)\b", raw):
        return [(now - timedelta(minutes=5), now)]
    if re.search(r"\byesterday\b", raw):
        return [(midnight - timedelta(days=1), midnight)]
    if re.search(r"\btoday\b", raw):
        return [(midnight, now)]
    match = re.search(r"\b(\d+|an?|one|two|three|four|five|six)\s*"
                      r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|[smhdw])\b(?:\s+ago)?", raw)
    if match and not re.search(r"\d[/.-]\d", raw):
        amount = match.group(1)
        number = _NUMBER_WORDS.get(amount) if not amount.isdigit() else int(amount)
        unit = _UNITS.get(match.group(2))
        if number is not None and unit:
            # Relative times are rounded ("a day ago" can mean ~22h), so allow half a unit
            # newer and one unit older than the number shown.
            newest = max(0.0, number - 0.5) * unit
            return [(now - timedelta(seconds=(number + 1) * unit), now - timedelta(seconds=newest))]
    parts = _numeric_parts(raw)
    if not parts:
        return []
    first, second, year = parts
    if order == "dm" or (order is None and first > 12):
        candidates = [(first, second)]
    elif order == "md" or (order is None and second > 12):
        candidates = [(second, first)]
    else:
        candidates = [(first, second), (second, first)] if first != second else [(first, second)]
    ranges = []
    for day, month in candidates:
        found = _day_range(day, month, year, now)
        if found and found not in ranges:
            ranges.append(found)
    return ranges


def classify(text, window, now, order=None):
    """``"in"`` / ``"approx"`` / ``"out"`` / ``"unknown"`` for one date text and window."""
    bounds = window_range(window, now)
    if bounds is None:
        return "in"
    ranges = parse_listed(text, now, order)
    if not ranges:
        return "unknown"
    start, end = bounds
    verdicts = set()
    for low, high in ranges:
        if high <= start or low >= end:
            verdicts.add("out")
        elif low >= start and high <= end:
            verdicts.add("in")
        else:
            verdicts.add("approx")
    if len(verdicts) == 1:
        return verdicts.pop()
    # day/month vs month/day disagree on the answer: not safe to keep or drop.
    return "unknown"


def filter_listings(listings, window, now=None):
    """``(kept, summary)``: listings posted within ``window``.

    ``summary`` is ``{"window", "label", "kept", "total", "approx", "unknown", "order"}``.
    Approximate (day-only date crossing the window edge) listings are kept and
    counted; listings without a readable/unambiguous date are left out and counted.
    """
    listings = list(listings or [])
    window = normalize_window(window)
    summary = {"window": window, "label": WINDOWS[window], "kept": len(listings), "total": len(listings),
               "approx": 0, "unknown": 0, "order": None}
    if window == ANY:
        return listings, summary
    now = now or datetime.now()
    order = infer_day_order(item.get("listed") for item in listings)
    summary["order"] = order
    kept = []
    for item in listings:
        verdict = classify(item.get("listed"), window, now, order)
        if verdict in ("in", "approx"):
            kept.append(item)
            summary["approx"] += verdict == "approx"
        elif verdict == "unknown":
            summary["unknown"] += 1
    summary["kept"] = len(kept)
    return kept, summary


def describe_filter(summary):
    """'Listed within Last 24 hours: 4 of 9 listings (2 by day-only date; 1 without a readable date left out)'."""
    if not summary or summary.get("window", ANY) == ANY:
        return ""
    text = f"Listed within {summary['label']}: {summary['kept']} of {summary['total']} listings"
    notes = []
    if summary.get("approx"):
        notes.append(f"{summary['approx']} matched by day-only date, so the window edge is approximate")
    if summary.get("unknown"):
        notes.append(f"{summary['unknown']} without a readable date left out")
    return text + (f" ({'; '.join(notes)})" if notes else "")
