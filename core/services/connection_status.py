"""Read-only, bounded health probes. Never expose deployment URLs or API keys."""
from dataclasses import dataclass, field
import time
from urllib.parse import urlparse

import requests

from core.config import load_config
from core.automation import browser_manager


STATUS_LABELS = {
    "checking": "Checking...",
    "connected": "Connected",
    "disconnected": "Disconnected",
    "not_configured": "Not set",
    "unverified": "Update script",
}


@dataclass(frozen=True)
class ConnectionStatus:
    state: str
    detail: str
    checked_at: float = 0

    @property
    def label(self):
        return STATUS_LABELS[self.state]


@dataclass(frozen=True)
class ConnectionSettings:
    provider: str
    script_url: str = field(repr=False)
    adspower_base: str = field(repr=False)
    adspower_key: str = field(repr=False)

    @property
    def provider_name(self):
        return "AdsPower" if self.provider == "adspower" else "Incogniton"


def read_connection_settings():
    config = load_config()
    provider = str(config.get("browser_provider") or "incogniton").strip().lower()
    return ConnectionSettings(
        provider="adspower" if provider == "adspower" else "incogniton",
        script_url=str(config.get("apps_script_url") or "").strip(),
        adspower_base=str(config.get("adspower_api_base") or
                          f"http://local.adspower.net:{config.get('adspower_api_port') or '50325'}"),
        adspower_key=str(config.get("adspower_api_key") or "").strip(),
    )


def probe_browser(settings):
    name = settings.provider_name
    try:
        connected = browser_manager.is_api_ready(settings.provider)
    except Exception:
        connected = False
    return ConnectionStatus(
        "connected" if connected else "disconnected",
        f"{name}'s Local API is responding." if connected else
        f"Open {name} and enable its Local API. ListPilot can launch it when a task starts.",
        time.time(),
    )


def probe_sheets(settings):
    url = settings.script_url
    if not url:
        return ConnectionStatus("not_configured", "Add your Apps Script Web app URL in Settings.")
    try:
        parsed = urlparse(url)
    except ValueError:
        return ConnectionStatus("disconnected", "Check the Apps Script Web app URL in Settings.", time.time())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ConnectionStatus("disconnected", "Check the Apps Script Web app URL in Settings.", time.time())
    try:
        response = requests.get(url, params={"action": "health"}, timeout=(3, 6))
        if response.status_code != 200:
            return ConnectionStatus(
                "disconnected", "Apps Script is unavailable. Check deployment permissions and internet access.",
                time.time(),
            )
        data = response.json()
        if isinstance(data, dict):
            if data.get("success") is True and data.get("service") == "ListPilot" and data.get("spreadsheetReady") is True:
                return ConnectionStatus("connected", "Apps Script can access its spreadsheet.", time.time())
            if str(data.get("error", "")).strip().lower() == "invalid action":
                return ConnectionStatus(
                    "unverified",
                    "Apps Script is reachable, but sheet access is unverified. Re-deploy the latest "
                    "script from Script Builder to enable the read-only health check.",
                    time.time(),
                )
    except requests.exceptions.RequestException:
        return ConnectionStatus("disconnected", "Cannot reach Google Sheets. Check your internet connection.", time.time())
    except (TypeError, ValueError):
        pass
    return ConnectionStatus(
        "disconnected", "Could not verify spreadsheet access. Check the Web app URL and deployment permissions.",
        time.time(),
    )
