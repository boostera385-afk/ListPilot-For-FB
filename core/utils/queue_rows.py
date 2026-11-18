"""Queue hygiene for sheet rows (no browser, unit tested)."""


def _norm(value):
    return " ".join(str(value or "").lower().split())


def split_duplicate_rows(posts, resolve=None):
    """Split queued rows into ``(keep, duplicates)``.

    Two rows that name the same browser profile (same ID, same name ignoring case and
    extra spaces, or the same resolved profile) would scan one Facebook account twice at
    the same time and double every total. The first row is kept; each later one comes back
    as ``(post, first_row_number)``.

    ``resolve`` maps an ID or name to a canonical profile ID (or ``""``); lookups that fail
    are ignored, so a missing resolver never blocks a run.
    """
    seen = {}
    keep, duplicates = [], []
    for post in posts or []:
        keys = set()
        for field in ("id", "ID", "profileName", "Profile Name", "profile name"):
            text = _norm((post or {}).get(field))
            if text:
                keys.add(("text", text))
                if resolve:
                    try:
                        canonical = _norm(resolve(str((post or {}).get(field)).strip()))
                    except Exception:
                        canonical = ""
                    if canonical:
                        keys.add(("profile", canonical))
        first = next((seen[key] for key in keys if key in seen), None)
        if first is not None:
            duplicates.append((post, first))
            continue
        row = (post or {}).get("rowNumber", "?")
        for key in keys:
            seen[key] = row
        keep.append(post)
    return keep, duplicates
