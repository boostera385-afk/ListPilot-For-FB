"""Remember which Marketplace seller layout each profile showed last time.

Accounts keep the same layout between runs, so knowing it lets the scan go straight to the right
page instead of waiting about 15 s for a table the account does not have. A wrong or stale entry is
harmless: the caller falls back to the other layout when the remembered one does not load.
"""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "layout_memory.json")
_lock = threading.Lock()
VALID = ("old", "new")


def _key(profile):
    return " ".join(str(profile or "").lower().split())


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def recall(profile, path=None):
    """``"old"``, ``"new"`` or ``""`` when nothing is remembered for this profile."""
    with _lock:
        value = _load(path or _PATH).get(_key(profile), "")
    return value if value in VALID else ""


def remember(profile, layout, path=None):
    """Store the layout that just worked. Never raises: this is a speed-up, not a requirement."""
    key = _key(profile)
    if not key or layout not in VALID:
        return
    target = path or _PATH
    with _lock:
        data = _load(target)
        if data.get(key) == layout:
            return
        data[key] = layout
        try:
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
        except OSError:
            pass
