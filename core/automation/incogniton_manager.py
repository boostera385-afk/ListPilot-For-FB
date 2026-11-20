import threading
import subprocess
import psutil
import os
import time
import requests
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Serialize browser startup, not browsing: once CDP is ready the next profile
# may start while this profile scans. Concurrent cold launches can overload the
# provider's sync/proxy initialization and time out even after Chrome opens.
_MAX_CONCURRENT_LAUNCHES = 1
_launch_capacity = threading.BoundedSemaphore(_MAX_CONCURRENT_LAUNCHES)
_stop_capacity = threading.Lock()  # provider sync/stop requests can conflict across profiles
_STOP_CONFIRM_POLLS = 6
_profile_launch_locks = {}
_profile_launch_locks_guard = threading.Lock()
_uncertain_launches = set()  # launch transport failed; Incogniton may still open our browser
_launch_attempts = {}
_LAUNCH_WAIT_SECONDS = 60
_LAUNCH_POLL_SECONDS = 0.5
_LAUNCH_HTTP_TIMEOUT = (3, 120)
_PREFLIGHT_POLLS = 5
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
from core.engine.exceptions import RetryableError, PermanentError, AutomationStopped
from core.utils.smart_match import match_indices

_api_retry = RetryManager(max_attempts=3, backoff_seconds=2.0)


@dataclass
class _LaunchAttempt:
    done: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.monotonic)
    response: object = None
    error: object = None
    attached: bool = False
    stopped: bool = False
    expired: bool = False


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
def _profile_launch_slot(profile_id, logger_callback=None, stop_event=None):
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

    profile_acquired = False
    capacity_acquired = False
    try:
        while not profile_lock.acquire(timeout=0.2):
            _check_launch_stop(stop_event)
        profile_acquired = True
        _check_launch_stop(stop_event)
        if not _launch_capacity.acquire(blocking=False):
            _launch_log(
                logger_callback,
                "Another profile is starting; waiting for its browser to be ready. Existing scans continue in parallel...",
            )
            while not _launch_capacity.acquire(timeout=0.2):
                _check_launch_stop(stop_event)
        capacity_acquired = True
        _check_launch_stop(stop_event)

        queued_for = time.monotonic() - queue_started
        _launch_log(
            logger_callback,
            f"Launch slot acquired for profile {profile_id} after {queued_for:.2f}s.",
        )
        yield
    finally:
        if capacity_acquired:
            _launch_capacity.release()
        if profile_acquired:
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

def _browser_option(args, name):
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1].strip('"')
        if arg == name and index + 1 < len(args):
            return args[index + 1].strip('"')
    return ""


def _check_launch_stop(stop_event):
    if stop_event and stop_event.is_set():
        raise AutomationStopped("User stopped while waiting for the browser to launch.")


def _profile_cdp_candidates(profile_id):
    """Find ports belonging to this exact UUID, never another open browser."""
    ports = set()
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if str(process.info.get("name") or "").lower() not in (
                "chrome.exe", "chrome", "chromium.exe", "chromium",
            ):
                continue
            args = process.info.get("cmdline") or []
            if any(arg.startswith("--type=") for arg in args):
                continue  # renderer/utility processes repeat the browser's arguments
            directory = _browser_option(args, "--user-data-dir")
            basename = directory.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if basename.lower() != profile_id.lower():
                continue
            port = _browser_option(args, "--remote-debugging-port")
            if port.isdigit() and 0 < int(port) < 65536:
                ports.add(int(port))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return [f"http://127.0.0.1:{port}" for port in sorted(ports)]


def _probe_cdp(endpoint):
    """Verify a local DevTools endpoint without opening/closing any browser tabs."""
    try:
        parsed = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            return None
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or not parsed.port:
            return None
        host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
        origin = f"http://{host}:{parsed.port}"
        response = requests.get(f"{origin}/json/version", timeout=(0.5, 0.5), allow_redirects=False)
        if response.status_code != 200:
            return None
        websocket = response.json().get("webSocketDebuggerUrl") or ""
        target = urlsplit(websocket)
        if (target.scheme not in ("ws", "wss") or target.hostname not in ("127.0.0.1", "localhost", "::1")
                or target.port != parsed.port or not target.path.startswith("/devtools/browser/")):
            return None
        return {"status": "ok", "url": websocket, "puppeteerUrl": origin,
                "webSocketDebuggerUrl": websocket}
    except (requests.exceptions.RequestException, TypeError, ValueError, AttributeError):
        return None


def _ready_profile_cdp(profile_id, data=None):
    data = data or {}
    hint = data.get("url") or data.get("puppeteerUrl") or data.get("webSocketDebuggerUrl")
    if not hint and data.get("port"):
        hint = f"http://127.0.0.1:{data['port']}"
    if hint:
        ready = _probe_cdp(hint)
        if ready:
            return ready
    for endpoint in _profile_cdp_candidates(profile_id):
        ready = _probe_cdp(endpoint)
        if ready:
            return ready
    return None


def _begin_launch(profile_id):
    """One HTTP request per attempt; an early CDP connection need not wait for it."""
    attempt = _LaunchAttempt()
    _launch_attempts[profile_id] = attempt
    _uncertain_launches.add(profile_id)

    def request():
        try:
            attempt.response = _api_request(
                "get", f"/automation/launch/puppeteer/{profile_id}",
                timeout=_LAUNCH_HTTP_TIMEOUT, retry=False,
            )
        except Exception as exc:
            attempt.error = exc
        finally:
            attempt.done.set()

    try:
        threading.Thread(target=request, name=f"IncognitonLaunch-{profile_id[:8]}", daemon=True).start()
    except Exception:
        _launch_attempts.pop(profile_id, None)
        _uncertain_launches.discard(profile_id)
        raise
    return attempt


def _wait_for_launch(profile_id, attempt, logger_callback, stop_event=None):
    deadline = time.monotonic() + _LAUNCH_WAIT_SECONDS
    next_log = time.monotonic() + 5
    while True:
        _check_launch_stop(stop_event)
        if attempt.stopped and attempt.done.is_set():
            return False, "PROFILE_LAUNCH_PENDING: The previous launch was stopped. Re-check this profile to start a new session."
        data = {}
        if attempt.done.is_set() and attempt.response is not None:
            try:
                if 400 <= attempt.response.status_code < 500:
                    _launch_attempts.pop(profile_id, None)
                    _uncertain_launches.discard(profile_id)
                    return False, f"PROFILE_LAUNCH_REJECTED: Incogniton returned HTTP {attempt.response.status_code}. Check the profile and API permissions."
                if attempt.response.status_code == 200:
                    data = attempt.response.json()
                    if not isinstance(data, dict):
                        data = {}
                    if data.get("status") == "error":
                        # An explicit rejection is not permission to take over
                        # a browser opened elsewhere in the meantime.
                        _launch_attempts.pop(profile_id, None)
                        _uncertain_launches.discard(profile_id)
                        if "already open" in str(data.get("message") or "").lower():
                            return False, "PROFILE_ALREADY_OPEN: Close this profile in Incogniton before retrying."
                        return False, f"PROFILE_LAUNCH_REJECTED: {data.get('message') or 'Incogniton rejected the launch'}."
            except (ValueError, TypeError):
                pass
        if not attempt.stopped:
            ready = _ready_profile_cdp(profile_id, data)
            if ready:
                _check_launch_stop(stop_event)
                attempt.attached = True
                _uncertain_launches.discard(profile_id)
                elapsed = time.monotonic() - attempt.started
                recovered = not data.get("status") == "ok"
                note = " Recovered our running browser without another launch request." if recovered else ""
                _launch_log(logger_callback, f"Browser CDP ready for profile {profile_id} after {elapsed:.1f}s.{note}")
                return True, ready
        if attempt.done.is_set() and attempt.error:
            cause = getattr(attempt.error, "__cause__", None)
            if isinstance(cause, requests.exceptions.ConnectionError) and any(
                phrase in str(cause).lower() for phrase in ("connection refused", "actively refused")
            ):
                # Unlike a read timeout, refusal means the request never
                # reached the local provider and is safe for the worker to retry.
                _launch_attempts.pop(profile_id, None)
                _uncertain_launches.discard(profile_id)
                return False, "Incogniton refused the local API connection before the launch request was delivered."
        now = time.monotonic()
        if now >= deadline:
            attempt.expired = True
            detail = str(attempt.error) if attempt.error else "The provider has not supplied a ready browser connection."
            return False, (
                f"PROFILE_LAUNCH_PENDING: Browser was not ready within {_LAUNCH_WAIT_SECONDS}s. {detail} "
                "No duplicate launch sent and no browser was stopped. Check the profile's proxy/sync status in "
                "Incogniton; close the unfinished profile before re-checking."
            )
        if now >= next_log:
            _launch_log(logger_callback, f"Waiting for profile {profile_id}'s browser connection "
                        f"({now - attempt.started:.0f}s); no additional launch request sent...")
            next_log = now + 5
        pause = min(_LAUNCH_POLL_SECONDS, deadline - now)
        if stop_event:
            if stop_event.wait(pause):
                raise AutomationStopped("User stopped while waiting for the browser to launch.")
        elif not attempt.done.is_set():
            attempt.done.wait(pause)
        else:
            time.sleep(pause)


def start_profile(identifier, duplicate_resolver=None, master=None, max_retries=3, logger_callback=None, stop_event=None):
    """
    Starts an Incogniton profile for automation and returns the debugging WebSocket URL.
    Uses: /automation/launch/puppeteer/{profile_id}
    identifier can be a UUID or the profile name.
    """
    profile_id = resolve_profile_id(identifier, duplicate_resolver, master)
    if not profile_id:
        return False, f"Could not find profile with name/id: {identifier}"

    # Compatibility only: never replay an ambiguous launch transport failure.
    _ = max_retries

    with _profile_launch_slot(profile_id, logger_callback, stop_event):
        attempt = _launch_attempts.get(profile_id)
        if attempt:
            closed = attempt.stopped and attempt.done.is_set()
            if attempt.done.is_set() and (attempt.expired or attempt.attached) and not closed:
                # A later user re-check may start again after the unfinished
                # browser was closed. Never reset while the HTTP request runs.
                try:
                    state_response = _api_request("get", f"/profile/status/{profile_id}", timeout=(1, 3), retry=False)
                    closed = (state_response.status_code == 200 and
                              str(state_response.json().get("status") or "").lower() == "ready" and
                              not _profile_cdp_candidates(profile_id))
                except Exception:
                    pass
            if closed:
                _launch_attempts.pop(profile_id, None)
                _uncertain_launches.discard(profile_id)
            elif attempt.attached:
                return False, "PROFILE_ALREADY_OPEN: This profile is already in use by an automation worker."
            else:
                _launch_log(logger_callback, "Resuming our previous launch; no new launch request will be sent.")
                return _wait_for_launch(profile_id, attempt, logger_callback, stop_event)
        if profile_id in _uncertain_launches:
            return False, "PROFILE_LAUNCH_PENDING: Previous launch is unresolved. No duplicate launch sent."
        for check in range(_PREFLIGHT_POLLS):
            _check_launch_stop(stop_event)
            try:
                status_response = _api_request("get", f"/profile/status/{profile_id}", timeout=(1, 3), retry=False)
                status = str(status_response.json().get("status") or "").strip().lower()
                if status_response.status_code != 200:
                    return False, "Could not verify profile status before launch. No launch sent."
            except Exception as exc:
                return False, f"Could not verify profile status before launch: {exc}"
            if status in ("launched", "running", "open"):
                return False, "PROFILE_ALREADY_OPEN: Close this profile in Incogniton before retrying."
            if status == "ready":
                if _profile_cdp_candidates(profile_id):
                    return False, "PROFILE_ALREADY_OPEN: A browser for this profile is still running. Close it before retrying."
                break
            if status not in ("syncing", "synced", "stopping", "launching"):
                return False, f"PROFILE_LAUNCH_REJECTED: Profile status is {status or 'unknown'}; no launch sent."
            if check + 1 == _PREFLIGHT_POLLS:
                return False, f"PROFILE_LAUNCH_PENDING: Incogniton is still {status}; no launch sent. Re-check once it shows Ready."
            if check == 0:
                _launch_log(logger_callback, f"Incogniton is {status}; waiting briefly for Ready before launching...")
            if stop_event:
                if stop_event.wait(1):
                    _check_launch_stop(stop_event)
            else:
                time.sleep(1)
        _launch_log(logger_callback, "Sending one launch request; watching for the browser connection "
                    "so a slow API reply does not delay automation...")
        _check_launch_stop(stop_event)
        return _wait_for_launch(profile_id, _begin_launch(profile_id), logger_callback, stop_event)

def _confirm_profile_stopped(profile_id):
    """A stop error/timeout can still close Chrome. Verify status AND exact-profile processes."""
    for attempt in range(_STOP_CONFIRM_POLLS):
        try:
            response = _api_request("get", f"/profile/status/{profile_id}", timeout=(1, 3), retry=False)
            status = str(response.json().get("status") or "").strip().lower()
            if (response.status_code == 200 and status in ("ready", "stopped", "closed")
                    and not _profile_cdp_candidates(profile_id)):
                return True
        except Exception:
            pass
        if attempt + 1 < _STOP_CONFIRM_POLLS:
            time.sleep(0.5)
    return False


def stop_profile(identifier):
    """
    Stops an Incogniton profile.
    Uses: /profile/stop/{profile_id}
    """
    profile_id = resolve_profile_id(identifier)
    if not profile_id:
        return False
        
    with _stop_capacity:
        try:
            # One bounded request, not the generic HTTP retry loop. Even a
            # transport error may mean the provider already closed the browser.
            _api_request("get", f"/profile/stop/{profile_id}", timeout=(3, 15), retry=False)
        except Exception:
            pass
        if not _confirm_profile_stopped(profile_id):
            return False
        with _profile_launch_locks_guard:
            attempt = _launch_attempts.get(profile_id)
            if attempt:
                attempt.stopped = True
                attempt.attached = False
                if attempt.done.is_set():
                    _launch_attempts.pop(profile_id, None)
            _uncertain_launches.discard(profile_id)
        return True

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
