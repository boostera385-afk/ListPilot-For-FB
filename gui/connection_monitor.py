"""Single-flight health checks; all state and callbacks stay on the UI thread."""
import threading
import time

from core.services.connection_status import (
    ConnectionStatus, probe_browser, probe_sheets, read_connection_settings,
)
from gui import theme
from gui.ui_helpers import run_on_ui


STATUS_COLORS = {
    "checking": theme.AMBER, "connected": theme.GREEN, "disconnected": theme.RED,
    "not_configured": theme.GRAY, "unverified": theme.AMBER,
}
INTERVALS = {"browser": 6, "sheets": 60}


class ConnectionMonitor:
    def __init__(self, on_update):
        self.on_update = on_update
        self.settings = None
        self.statuses = {}
        self._inflight = set()
        self._next_check = {"browser": 0, "sheets": 0}
        self._closed = False

    def poll(self, force=False):
        if self._closed:
            return
        settings = read_connection_settings()
        changed = settings != self.settings
        if changed:
            self.settings = settings
            self.statuses = {
                "browser": ConnectionStatus("checking", "Checking the browser's Local API..."),
                "sheets": ConnectionStatus("checking", "Verifying spreadsheet access..."),
            }
        if changed or force:
            self._next_check = {"browser": 0, "sheets": 0}
        if not settings.script_url:
            self.statuses["sheets"] = ConnectionStatus(
                "not_configured", "Add your Apps Script Web app URL in Settings.")
        for kind, probe in (("browser", probe_browser), ("sheets", probe_sheets)):
            if kind == "sheets" and not settings.script_url:
                continue
            if kind in self._inflight or time.monotonic() < self._next_check[kind]:
                continue
            self._inflight.add(kind)
            # Regular refreshes retain the last result rather than flashing every few seconds.
            if changed or force:
                self.statuses[kind] = ConnectionStatus("checking", "Checking connection...")
            threading.Thread(target=self._probe, args=(kind, probe, settings), daemon=True).start()
        self._notify()

    def _probe(self, kind, probe, settings):
        try:
            status = probe(settings)
        except Exception:
            status = ConnectionStatus(
                "disconnected", "Connection check failed. Check Settings and try again.", time.time())
        run_on_ui(self._finish, kind, settings, status)

    def _finish(self, kind, settings, status):
        self._inflight.discard(kind)
        if self._closed:
            return
        # Do not turn an old URL/provider green after the user changes Settings.
        if settings != read_connection_settings() or settings != self.settings:
            self.poll()
            return
        self.statuses[kind] = status
        self._next_check[kind] = time.monotonic() + INTERVALS[kind]
        self._notify()

    def _notify(self):
        self.on_update(self.settings, dict(self.statuses), bool(self._inflight))

    def close(self):
        self._closed = True
