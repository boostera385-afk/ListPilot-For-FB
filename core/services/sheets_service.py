import requests
import time
import random
from core.config import get_setting
from core.engine.exceptions import SheetsUnavailableError


_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def _friendly_connection_error(error):
    """Return a short user-safe network message without URLs or stack chains."""
    lowered = str(error or "").lower()
    if any(token in lowered for token in ("nameresolution", "name resolution", "getaddrinfo", "failed to resolve")):
        return "Google Sheets is temporarily unavailable because DNS could not resolve it. Check your internet or DNS connection."
    if "timed out" in lowered or "timeout" in lowered:
        return "Google Sheets timed out. Check your internet connection and try again."
    return "Google Sheets is temporarily unavailable. Check your internet connection and try again."


def _friendly_http_error(status_code):
    if status_code in (401, 403):
        return f"Google Sheets access was denied (HTTP {status_code}). Check the Apps Script deployment permissions."
    if status_code in _RETRYABLE_HTTP_STATUSES:
        return f"Google Sheets is temporarily busy or unavailable (HTTP {status_code})."
    return f"Google Sheets request failed (HTTP {status_code})."


def _requests_retry(method, url, retries=3, backoff=2, max_backoff=10, jitter=0.5, **kwargs):
    for i in range(retries):
        try:
            if method.lower() == "get":
                resp = requests.get(url, **kwargs)
            else:
                resp = requests.post(url, **kwargs)
                
            # Check for retryable HTTP statuses
            if resp.status_code in _RETRYABLE_HTTP_STATUSES:
                if i < retries - 1:
                    delay = min(max_backoff, backoff * (2 ** i)) + random.uniform(0, jitter)
                    time.sleep(delay)
                    continue
                else:
                    return resp # Return it anyway after all retries fail
                    
            return resp
        except requests.exceptions.RequestException as e:
            if i < retries - 1:
                delay = min(max_backoff, backoff * (2 ** i)) + random.uniform(0, jitter)
                time.sleep(delay)
            else:
                raise e

def get_script_url():
    return get_setting("apps_script_url", "")


def _status_update_result(response, success_message):
    """Return an Apps Script update result without hiding JSON-level failures."""
    if response.status_code != 200:
        return False, _friendly_http_error(response.status_code)

    try:
        data = response.json()
    except (TypeError, ValueError):
        # Keep compatibility with older deployments that returned an empty or
        # plain-text 200 response for successful writes.
        return True, success_message

    if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
        message = data.get("error") or data.get("message") or "Google Apps Script rejected the update."
        return False, str(message)

    return True, success_message

def fetch_pending_rows(sheet_name):
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
        
    try:
        response = _requests_retry("get", url, params={"action": "getPendingRows", "sheetName": sheet_name}, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                return True, data.get("data", [])
            else:
                return False, data.get("error", "Unknown error")
        else:
            return False, _friendly_http_error(response.status_code)
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."

def update_row_status(sheet_name, row_number, status, notes=""):
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
        
    if notes:
        status = f"{status} | {notes}"
        
    payload = {
        "action": "updateRowStatus",
        "sheetName": sheet_name,
        "rowNumber": row_number,
        "status": status
    }
    
    try:
        response = _requests_retry("post", url, json=payload, timeout=15)
        return _status_update_result(response, "Status updated")
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."

def update_column_value(sheet_name, row_number, column_name, value):
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
        
    payload = {
        "action": "updateColumnValue",
        "sheetName": sheet_name,
        "rowNumber": row_number,
        "columnName": column_name,
        "value": value
    }
    
    try:
        response = _requests_retry("post", url, json=payload, timeout=15)
        return _status_update_result(response, "Column updated")
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."

def update_column_values(sheet_name, row_number, values):
    """Write several columns of one row in one request.

    Falls back to one request per column when the deployed Apps Script is older
    and does not know ``updateColumnValues``. Returns ``(ok, message)``; a
    partial failure lists the columns that could not be saved.
    """
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
    if not values:
        return True, "Nothing to update"

    payload = {
        "action": "updateColumnValues",
        "sheetName": sheet_name,
        "rowNumber": row_number,
        "values": values,
    }
    try:
        response = _requests_retry("post", url, json=payload, timeout=20)
        ok, message = _status_update_result(response, "Columns updated")
        if ok or "invalid action" not in str(message).lower():
            return ok, message
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."

    failed = []
    for column, value in values.items():
        saved, message = update_column_value(sheet_name, row_number, column, value)
        if not saved:
            failed.append(f"{column} ({message})")
    if failed:
        return False, "Could not save: " + "; ".join(failed)
    return True, "Columns updated"


def append_live_history(profile, total=None, live=None, pending=None, rejected=None, check="OK"):
    """Append one scan to the 'Live History' sheet. The script adds a Change column.

    Best effort: returns ``(ok, message)`` and never raises, because a history
    problem must not fail a scan that already succeeded.
    """
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
    payload = {
        "action": "appendLiveHistory",
        "profile": str(profile or ""),
        "total": total,
        "live": live,
        "pending": pending,
        "rejected": rejected,
        "check": str(check or ""),
    }
    try:
        response = _requests_retry("post", url, json=payload, timeout=20)
        return _status_update_result(response, "History saved")
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."


RECHECK_MODES = ("pending", "failed", "all", "stuck")


def reset_rows_for_recheck(sheet_name, mode):
    """Clear Status on finished rows so the normal 'pending rows' fetch re-queues them.

    ``mode``: ``pending`` (rows with Pending Count > 0), ``failed`` (Failed
    status or a Check Result that is not OK) or ``all``. Rows being processed
    (CLAIMED) are never touched. Returns ``(ok, count_or_message)``.
    """
    if mode not in RECHECK_MODES:
        return False, f"Unknown recheck mode: {mode}"
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
    payload = {"action": "resetForRecheck", "sheetName": sheet_name, "mode": mode}
    try:
        response = _requests_retry("post", url, json=payload, timeout=30)
        if response.status_code != 200:
            return False, _friendly_http_error(response.status_code)
        data = response.json()
        if isinstance(data, dict) and data.get("success"):
            return True, int(data.get("reset", 0))
        message = (data or {}).get("error") if isinstance(data, dict) else None
        if message and "invalid action" in str(message).lower():
            message = "The deployed Apps Script is out of date. Re-deploy the latest script from Script Builder."
        return False, message or "Google Apps Script rejected the recheck request."
    except requests.exceptions.RequestException as e:
        return False, _friendly_connection_error(e)
    except Exception:
        return False, "Google Sheets returned an invalid response."


def claim_row(sheet_name, row_number):
    """
    Atomically claims a row for processing to prevent duplicates.
    Returns (True, "Claimed") if successful.
    Returns (False, "Already claimed") if someone else got it first.
    """
    url = get_script_url()
    if not url:
        return False, "Google Apps Script URL is not set."
        
    payload = {
        "action": "claimRow",
        "sheetName": sheet_name,
        "rowNumber": row_number
    }
    
    try:
        # Claim outages need a longer recovery window than ordinary UI reads.
        # Five staggered attempts cover brief DNS/Wi-Fi interruptions without
        # advancing to later rows and mislabelling them as conflicts.
        response = _requests_retry("post", url, retries=5, backoff=2, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                # Handle both {"success": true, "claimed": true} and just {"success": true}
                if data.get("claimed") is True or "claimed" not in data:
                    return True, "Claimed"
                else:
                    return False, f"Already claimed (Status: {data.get('currentStatus')})"
            else:
                return False, data.get("error", "Unknown error from Google Apps Script.")
        if response.status_code in _RETRYABLE_HTTP_STATUSES:
            raise SheetsUnavailableError(_friendly_http_error(response.status_code))
        return False, _friendly_http_error(response.status_code)
    except SheetsUnavailableError:
        raise
    except requests.exceptions.RequestException as e:
        raise SheetsUnavailableError(_friendly_connection_error(e)) from e
    except (TypeError, ValueError):
        return False, "Google Sheets returned an invalid claim response."
