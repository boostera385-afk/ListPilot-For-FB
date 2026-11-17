import threading
import subprocess
import psutil
import os
import time
import requests
from contextlib import contextmanager

# Launches for different profiles may run concurrently, while duplicate launch
# requests for the same profile must remain serialized.  The semaphore protects
# the local Incogniton service from an unbounded burst when this module is used
# outside the automation engine (the engine currently defaults to three workers).
_MAX_CONCURRENT_LAUNCHES = 3
_launch_capacity = threading.BoundedSemaphore(_MAX_CONCURRENT_LAUNCHES)
_profile_launch_locks = {}
_profile_launch_locks_guard = threading.Lock()
_PROFILE_CACHE_TTL_SECONDS = 15.0
_PROFILE_ERROR_CACHE_TTL_SECONDS = 2.0
_profile_cache_lock = threading.Lock()
_profile_cache_data = None
_profile_cache_at = 0.0
_profile_cache_error = None
_profile_cache_error_at = 0.0
from core.config import get_setting, set_setting
from core.utils.path_finder import find_incogniton_path

INCOGNITON_API = "http://127.0.0.1:35000"

from core.engine.retry_manager import RetryManager
from core.engine.exceptions import RetryableError, PermanentError
from core.utils.smart_match import match_indices

_api_retry = RetryManager(max_attempts=3, backoff_seconds=2.0)


def _friendly_incogniton_error(error):
    lowered = str(error or "").lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "Incogniton API timed out. Make sure Incogniton is fully loaded and its Local API is enabled."
    if any(token in lowered for token in ("connection refused", "actively refused", "failed to establish a new connection")):
        return "Incogniton API is not ready on port 35000. Keep Incogniton open and wait for it to finish loading."
    return "Incogniton API is temporarily unavailable. Make sure Incogniton is open and its Local API is enabled."

def _api_request(method, endpoint, timeout=15, retry=True, **kwargs):
    import requests
    url = f"{INCOGNITON_API}{endpoint}"
    
    def _do_req():
        try:
            if method.lower() == "get":
                resp = requests.get(url, timeout=timeout, **kwargs)
            else:
                resp = requests.post(url, timeout=timeout, **kwargs)
                
            if resp.status_code in [429, 500, 502, 503, 504]:
                raise RetryableError(f"Incogniton API {resp.status_code}")
                
            return resp
        except requests.exceptions.RequestException as e:
            raise RetryableError(_friendly_incogniton_error(e)) from e
            
    if retry:
        return _api_retry.execute_with_retry(_do_req)
    return _do_req()


def _launch_log(logger_callback, message):
    if logger_callback:
        try:
            logger_callback(message)
        except Exception:
            # Diagnostics must never strand a profile lock or fail a launch.
            pass


@contextmanager
def _profile_launch_slot(profile_id, logger_callback=None):
    """Serializes one profile ID while allowing bounded cross-profile launches."""
    with _profile_launch_locks_guard:
        entry = _profile_launch_locks.get(profile_id)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _profile_launch_locks[profile_id] = entry
        entry["users"] += 1

    profile_lock = entry["lock"]
    queue_started = time.monotonic()
    if profile_lock.locked():
        _launch_log(
            logger_callback,
            f"Profile {profile_id} already has a launch in progress; waiting for it to finish...",
        )

    profile_lock.acquire()
    capacity_acquired = False
    try:
        if not _launch_capacity.acquire(blocking=False):
            _launch_log(
                logger_callback,
                f"Launch capacity is full ({_MAX_CONCURRENT_LAUNCHES} profiles); waiting for a slot...",
            )
            _launch_capacity.acquire()
        capacity_acquired = True

        queued_for = time.monotonic() - queue_started
        _launch_log(
            logger_callback,
            f"Launch slot acquired for profile {profile_id} after {queued_for:.2f}s.",
        )
        yield
    finally:
        if capacity_acquired:
            _launch_capacity.release()
        profile_lock.release()
        with _profile_launch_locks_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _profile_launch_locks.pop(profile_id, None)


def is_incogniton_running():
    """Checks if Incogniton is already running."""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and 'incogniton' in proc.info['name'].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False

def _probe_incogniton_api():
    """Use Incogniton's lightweight health endpoint without nested retries."""
    try:
        resp = _api_request("get", "/alive", timeout=2, retry=False)
        body = str(getattr(resp, "text", "")).strip().upper()
        ready = resp.status_code == 200 and body == "OK"
        return ready, "Incogniton API did not return OK." if not ready else ""
    except RetryableError as exc:
        return False, str(exc)
    except Exception:
        return False, "Incogniton API health check failed."


def is_incogniton_api_ready():
    """Checks if the Incogniton local REST API is responding."""
    ready, _ = _probe_incogniton_api()
    return ready

def wait_for_incogniton_ready(timeout=60, logger_callback=print):
    """
    Waits for Incogniton process and its API to become responsive.
    Polls every 2-3 seconds up to timeout (default 60 seconds).
    Returns (True, message) when ready, (False, message) if timed out.
    """
    start_time = time.monotonic()
    deadline = start_time + timeout
    logger_callback(f"Waiting for Incogniton API to initialize (up to {timeout}s)...")

    last_log_time = 0
    last_error = ""
    while time.monotonic() < deadline:
        ready, probe_error = _probe_incogniton_api()
        elapsed = int(time.monotonic() - start_time)
        if ready:
            logger_callback(f"Incogniton is ready! (Initialized in {elapsed}s)")
            return True, "Incogniton is ready."
        last_error = probe_error or last_error
        if elapsed > 0 and elapsed - last_log_time >= 10:
            last_log_time = elapsed
            logger_callback(f"Still waiting for Incogniton to load... ({elapsed}s/{timeout}s)")

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))

    detail = f" Last check: {last_error}" if last_error else ""
    return False, f"Incogniton did not become ready within {timeout} seconds.{detail}"

def get_incogniton_exe():
    """Gets the path to the Incogniton executable, trying config first, then auto-discovery."""
    saved_path = get_setting("incogniton_path")
    if saved_path and os.path.exists(saved_path):
        return saved_path
    
    discovered_path = find_incogniton_path()
    if discovered_path:
        set_setting("incogniton_path", discovered_path)
        return discovered_path
        
    return None

def launch_incogniton():
    """Launches Incogniton if it's not already running."""
    if is_incogniton_running():
        return True, "Incogniton is already running."
        
    exe_path = get_incogniton_exe()
    if not exe_path:
        return False, "Incogniton executable not found. Please set it in Settings."
        
    try:
        # Use Popen to start it detached from our script
        subprocess.Popen([exe_path])
        return True, "Incogniton launched successfully."
    except Exception as e:
        return False, f"Failed to launch: {str(e)}"


def _get_all_profiles():
    """Fetch the large profile list once for workers starting in the same batch."""
    global _profile_cache_data, _profile_cache_at, _profile_cache_error, _profile_cache_error_at
    now = time.monotonic()
    with _profile_cache_lock:
        if (
            _profile_cache_data is not None
            and now - _profile_cache_at < _PROFILE_CACHE_TTL_SECONDS
        ):
            return _profile_cache_data

        if (
            _profile_cache_error is not None
            and now - _profile_cache_error_at < _PROFILE_ERROR_CACHE_TTL_SECONDS
        ):
            raise RetryableError(_profile_cache_error)

        try:
            resp = _api_request("get", "/profile/all", timeout=5, retry=False)
        except RetryableError as exc:
            _profile_cache_error = str(exc)
            _profile_cache_error_at = time.monotonic()
            raise
        if resp.status_code != 200:
            message = f"Incogniton profile list request failed (HTTP {resp.status_code})."
            _profile_cache_error = message
            _profile_cache_error_at = time.monotonic()
            raise RetryableError(message)
        profiles = resp.json().get("profileData", [])
        _profile_cache_data = profiles if isinstance(profiles, list) else []
        _profile_cache_at = time.monotonic()
        _profile_cache_error = None
        _profile_cache_error_at = 0.0
        return _profile_cache_data

def resolve_profile_id(identifier, duplicate_resolver=None, master=None):
    """Resolves a profile name to its browser_id (UUID). Returns the original identifier if already a UUID."""
    profile_id = str(identifier).strip()
    # If it looks like a UUID (has dashes and length 32-36), use it directly
    if len(profile_id) >= 32 and "-" in profile_id:
        return profile_id
        
    try:
        profiles = _get_all_profiles()
        if profiles:
            # Names match ignoring case, spacing, punctuation and small typos.
            infos = [p.get("general_profile_information", {}) or {} for p in profiles]
            names = [str(info.get("profile_name") or "") for info in infos]
            matches = [
                {"browser_id": infos[i].get("browser_id"), "group": profiles[i].get("group", "Unassigned")}
                for i in match_indices(profile_id, names)
            ]
            
            if len(matches) == 1:
                return matches[0]["browser_id"]
            elif len(matches) > 1:
                if duplicate_resolver and master:
                    print(f"[Warning] Multiple profiles found for '{profile_id}'. Prompting user...")
                    selected_id = duplicate_resolver(master, profile_id, matches)
                    if selected_id:
                        return selected_id
                    else:
                        print(f"User canceled duplicate resolution for '{profile_id}'.")
                        return None
                else:
                    print(f"[Warning] Multiple Incogniton profiles found with name '{profile_id}'. Please use the specific Profile ID in Google Sheets instead.")
                    return None
                
        return None
    except RetryableError:
        raise
    except Exception:
        return None

def start_profile(identifier, duplicate_resolver=None, master=None, max_retries=3, logger_callback=None):
    """
    Starts an Incogniton profile for automation and returns the debugging WebSocket URL.
    Uses: /automation/launch/puppeteer/{profile_id}
    identifier can be a UUID or the profile name.
    """
    profile_id = resolve_profile_id(identifier, duplicate_resolver, master)
    if not profile_id:
        return False, f"Could not find profile with name/id: {identifier}"

    # ``max_retries`` remains in the public signature for compatibility with
    # existing callers.  Launch retries are deliberately owned by ProfileWorker;
    # retrying here as well multiplies a 120-second transport timeout by both
    # retry layers and blocks queued profiles for many minutes.
    _ = max_retries

    with _profile_launch_slot(profile_id, logger_callback):
        request_started = time.monotonic()
        try:
            _launch_log(
                logger_callback,
                "Sending one launch request to Incogniton. Profile-level retries are managed by the automation worker...",
            )
            response = _api_request(
                "get",
                f"/automation/launch/puppeteer/{profile_id}",
                timeout=120,
                retry=False,
            )
            elapsed = time.monotonic() - request_started
            _launch_log(
                logger_callback,
                f"Incogniton launch request for profile {profile_id} finished in {elapsed:.2f}s (HTTP {response.status_code}).",
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok":
                    return True, data
                return False, f"Incogniton returned error: {data}"

            return False, f"Failed to start profile. Status: {response.status_code}"
        except Exception as e:
            elapsed = time.monotonic() - request_started
            _launch_log(
                logger_callback,
                f"Incogniton launch request for profile {profile_id} failed after {elapsed:.2f}s: {e}",
            )
            return False, str(e)

def stop_profile(identifier):
    """
    Stops an Incogniton profile.
    Uses: /profile/stop/{profile_id}
    """
    profile_id = resolve_profile_id(identifier)
    if not profile_id:
        return False
        
    try:
        url = f"{INCOGNITON_API}/profile/stop/{profile_id}"
        response = _api_request("get", f"/profile/stop/{profile_id}", timeout=30)
        return response.status_code == 200
    except:
        return False

def update_profile_proxy(profile_id, proxy_str):
    """
    Updates the proxy of an existing Incogniton profile before launching.
    """
    if not proxy_str or str(proxy_str).strip() == "" or str(proxy_str).lower() == "n/a":
        return False, "No valid proxy provided"
        
    # We must import parse_proxy here to avoid circular imports if any
    from core.automation.profile_builder import parse_proxy
    proxy_info = parse_proxy(proxy_str)
    if not proxy_info:
        return False, "Invalid proxy format (expected host:port:user:pass)"
        
    try:
        # Fetch current profile configurations
        resp = _api_request("get", "/profile/all")
        if resp.status_code != 200:
            return False, f"Failed to fetch profiles for update: HTTP {resp.status_code}"
            
        profiles = resp.json().get("profileData", [])
        profile_data = None
        actual_browser_id = profile_id
        
        for p in profiles:
            info = p.get("general_profile_information", {})
            if str(info.get("browser_id")) == str(profile_id) or str(info.get("profile_name")).strip().lower() == str(profile_id).strip().lower():
                profile_data = p
                actual_browser_id = info.get("browser_id", profile_id)
                break
                
        if not profile_data:
            return False, f"Profile not found for update: {profile_id}"
            
        # Check if existing proxy already matches
        existing_proxy = profile_data.get("Proxy", {})
        if isinstance(existing_proxy, dict):
            existing_url = str(existing_proxy.get("proxy_url", "")).strip()
            existing_user = str(existing_proxy.get("proxy_username", "")).strip()
            existing_pass = str(existing_proxy.get("proxy_password", "")).strip()
            
            target_url = f"{proxy_info['host']}:{proxy_info['port']}".strip()
            target_user = str(proxy_info.get("username") or "").strip()
            target_pass = str(proxy_info.get("password") or "").strip()
            
            if (existing_url == target_url and 
                existing_user == target_user and 
                existing_pass == target_pass and 
                str(existing_proxy.get("connection_type", "")).upper() in ["SOCKS5", "HTTP"]):
                return True, "Proxy is already up to date"

        # Build a 100% CLEAN proxy dictionary containing ONLY primitive strings
        # This completely prevents Java LinkedHashMap casting errors on Incogniton's backend
        clean_proxy = {
            "connection_type": str(proxy_info.get("type", "SOCKS5")),
            "proxy_url": f"{proxy_info['host']}:{proxy_info['port']}",
            "proxy_username": str(proxy_info.get("username") or ""),
            "proxy_password": str(proxy_info.get("password") or "")
        }
        
        profile_name = profile_data.get("general_profile_information", {}).get("profile_name", "")
        
        # Try payload formats in order
        payloads_to_try = [
            {
                "profileData": {
                    "profile_browser_id": str(actual_browser_id),
                    "Proxy": clean_proxy
                }
            },
            {
                "profile_id": str(actual_browser_id),
                "profileData": {
                    "profile_browser_id": str(actual_browser_id),
                    "general_profile_information": {
                        "profile_name": profile_name,
                        "browser_id": str(actual_browser_id)
                    },
                    "Proxy": clean_proxy
                }
            }
        ]
        
        last_err = "Unknown error"
        for payload in payloads_to_try:
            try:
                update_resp = _api_request("post", "/profile/update", json=payload, timeout=30)
                if update_resp.status_code == 200:
                    resp_json = update_resp.json()
                    if resp_json.get("status") == "ok" or resp_json.get("success") is True:
                        return True, "Proxy updated successfully"
                    else:
                        last_err = resp_json.get("message", "API returned non-ok status")
                else:
                    last_err = f"HTTP {update_resp.status_code}: {update_resp.text}"
            except Exception as ex:
                last_err = str(ex)
                
        return False, f"API Error: {last_err}"
            
    except Exception as e:
        return False, f"Error updating proxy: {str(e)}"


