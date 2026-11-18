from dataclasses import replace
import unittest
from unittest import mock

import requests

from core.automation import browser_manager
from core.services import connection_status as service
from gui import connection_monitor as monitoring


SETTINGS = service.ConnectionSettings(
    provider="incogniton", script_url="https://script.example/exec",
    adspower_base="http://localhost:50325", adspower_key="private-key",
)


class ConnectionProbeTests(unittest.TestCase):
    def test_settings_are_normalized_and_secrets_are_not_in_repr(self):
        with mock.patch.object(service, "load_config", return_value={
            "browser_provider": " AdsPower ", "apps_script_url": " https://private.example/exec ",
            "adspower_api_key": "secret", "adspower_api_port": "12345",
        }):
            settings = service.read_connection_settings()
        self.assertEqual(settings.provider_name, "AdsPower")
        self.assertEqual(settings.adspower_base, "http://local.adspower.net:12345")
        self.assertEqual(settings.script_url, "https://private.example/exec")
        self.assertNotIn("private.example", repr(settings))
        self.assertNotIn("secret", repr(settings))

    def test_browser_probe_uses_api_not_a_process_name(self):
        with mock.patch.object(service.browser_manager, "is_api_ready", return_value=True) as probe:
            status = service.probe_browser(SETTINGS)
        probe.assert_called_once_with("incogniton")
        self.assertEqual(status.state, "connected")
        self.assertGreater(status.checked_at, 0)

    def test_browser_probe_failure_is_safe_and_actionable(self):
        with mock.patch.object(service.browser_manager, "is_api_ready", side_effect=RuntimeError("secret-url")):
            status = service.probe_browser(SETTINGS)
        self.assertEqual(status.state, "disconnected")
        self.assertIn("Local API", status.detail)
        self.assertNotIn("secret-url", status.detail)

    def test_adspower_health_probe_is_short_and_has_no_retries(self):
        with mock.patch.object(browser_manager._adspower, "is_adspower_api_ready", return_value=True) as probe:
            self.assertTrue(browser_manager.is_api_ready("adspower"))
        probe.assert_called_once_with(timeout=2, retry=False)

    def test_incogniton_health_probe_uses_existing_alive_endpoint(self):
        with mock.patch.object(browser_manager._incogniton, "is_incogniton_api_ready", return_value=True) as probe:
            self.assertTrue(browser_manager.is_api_ready("incogniton"))
        probe.assert_called_once_with()

    def test_unconfigured_or_invalid_sheet_url_does_not_make_a_request(self):
        for url, state in (("", "not_configured"), ("not a URL", "disconnected"),
                           ("http://[invalid", "disconnected")):
            with self.subTest(url=url):
                with mock.patch.object(service.requests, "get") as get:
                    status = service.probe_sheets(replace(SETTINGS, script_url=url))
                get.assert_not_called()
                self.assertEqual(status.state, state)

    def test_sheet_connection_requires_verified_spreadsheet_access(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "service": "ListPilot", "spreadsheetReady": True}
        with mock.patch.object(service.requests, "get", return_value=response) as get:
            with mock.patch.object(service.requests, "post") as post:
                status = service.probe_sheets(SETTINGS)
        self.assertEqual(status.state, "connected")
        get.assert_called_once_with(SETTINGS.script_url, params={"action": "health"}, timeout=(3, 6))
        post.assert_not_called()

    def test_older_deployment_is_unverified_not_falsely_connected(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"error": "Invalid action"}
        with mock.patch.object(service.requests, "get", return_value=response):
            status = service.probe_sheets(SETTINGS)
        self.assertEqual(status.state, "unverified")
        self.assertEqual(status.label, "Update script")
        self.assertIn("Re-deploy", status.detail)

    def test_http_200_alone_is_not_a_sheet_connection(self):
        for payload in ({}, [], {"success": True},
                        {"success": True, "service": "ListPilot", "spreadsheetReady": False},
                        {"success": False, "error": "private spreadsheet identifier"}):
            with self.subTest(payload=payload):
                response = mock.Mock(status_code=200)
                response.json.return_value = payload
                with mock.patch.object(service.requests, "get", return_value=response):
                    status = service.probe_sheets(SETTINGS)
                self.assertEqual(status.state, "disconnected")
                self.assertNotIn("private spreadsheet identifier", status.detail)

    def test_http_error_html_and_network_failure_are_disconnected(self):
        forbidden = mock.Mock(status_code=403)
        html = mock.Mock(status_code=200)
        html.json.side_effect = ValueError("Sign-in page")
        for result in (forbidden, html, requests.exceptions.Timeout("secret-url")):
            with self.subTest(result=type(result).__name__):
                with mock.patch.object(service.requests, "get", side_effect=[result]):
                    status = service.probe_sheets(SETTINGS)
                self.assertEqual(status.state, "disconnected")
                self.assertNotIn("secret-url", status.detail)


class ConnectionMonitorTests(unittest.TestCase):
    def setUp(self):
        self.settings = SETTINGS
        self.update = mock.Mock()
        self.monitor = monitoring.ConnectionMonitor(self.update)
        for name, patcher in (
            ("read_settings", mock.patch.object(monitoring, "read_connection_settings", side_effect=lambda: self.settings)),
            ("thread", mock.patch.object(monitoring.threading, "Thread")),
            ("enqueue", mock.patch.object(monitoring, "run_on_ui")),
            ("clock", mock.patch.object(monitoring.time, "monotonic", return_value=100)),
        ):
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def finish(self, kind, state="connected"):
        self.monitor._finish(kind, self.settings, service.ConnectionStatus(state, "Test result", 123))

    def test_initial_checks_are_independent_and_single_flight(self):
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.thread.call_count, 2)
        self.assertEqual(self.monitor.statuses["browser"].state, "checking")
        self.finish("browser")
        self.assertEqual(self.monitor.statuses["browser"].state, "connected")
        self.assertEqual(self.monitor.statuses["sheets"].state, "checking")
        self.assertTrue(self.update.call_args.args[2])

    def test_automatic_refresh_retains_last_status_without_blinking(self):
        self.monitor.poll()
        self.finish("browser")
        self.finish("sheets")
        self.clock.return_value = 107
        self.monitor.poll()
        self.assertEqual(self.thread.call_count, 3)  # browser only; Sheets is cached for 60 seconds
        self.assertEqual(self.monitor.statuses["browser"].state, "connected")
        self.assertEqual(self.monitor.statuses["sheets"].state, "connected")

    def test_manual_refresh_does_not_overlap_existing_probes(self):
        self.monitor.poll()
        self.monitor.poll(force=True)
        self.monitor.poll(force=True)
        self.assertEqual(self.thread.call_count, 2)
        self.finish("browser")
        self.finish("sheets")
        self.monitor.poll(force=True)
        self.assertEqual(self.thread.call_count, 4)
        self.assertEqual(self.monitor.statuses["browser"].state, "checking")

    def test_stale_provider_or_url_response_is_discarded(self):
        self.monitor.poll()
        old_settings = self.settings
        self.settings = replace(SETTINGS, provider="adspower", script_url="https://new.example/exec")
        self.monitor._finish("browser", old_settings, service.ConnectionStatus("connected", "Old result", 123))
        self.assertEqual(self.monitor.settings.provider, "adspower")
        self.assertEqual(self.monitor.statuses["browser"].state, "checking")
        self.assertNotEqual(self.monitor.statuses["browser"].detail, "Old result")
        self.monitor._finish("sheets", old_settings, service.ConnectionStatus("connected", "Old URL", 123))
        self.assertEqual(self.monitor.statuses["sheets"].state, "checking")
        self.assertNotEqual(self.monitor.statuses["sheets"].detail, "Old URL")

    def test_removing_sheet_url_immediately_removes_connected_status(self):
        self.monitor.poll()
        self.finish("browser")
        self.finish("sheets")
        self.settings = replace(SETTINGS, script_url="")
        self.monitor.poll()
        self.assertEqual(self.monitor.statuses["sheets"].state, "not_configured")
        self.assertEqual(self.thread.call_count, 3)  # only browser rechecked

    def test_worker_does_not_update_ui_directly(self):
        self.monitor.poll()
        calls = self.update.call_count
        status = service.ConnectionStatus("connected", "Ready", 123)
        probe = mock.Mock(return_value=status)
        self.monitor._probe("browser", probe, SETTINGS)
        self.assertEqual(self.update.call_count, calls)
        self.enqueue.assert_called_once_with(self.monitor._finish, "browser", SETTINGS, status)

    def test_unexpected_worker_exception_still_delivers_safe_failure(self):
        probe = mock.Mock(side_effect=RuntimeError("secret-url-and-api-key"))
        self.monitor._probe("browser", probe, SETTINGS)
        status = self.enqueue.call_args.args[-1]
        self.assertEqual(status.state, "disconnected")
        self.assertNotIn("secret", status.detail)

    def test_close_ignores_late_results_and_does_not_launch_more_checks(self):
        self.monitor.poll()
        self.monitor.close()
        count = self.update.call_count
        self.finish("browser")
        self.monitor.poll(force=True)
        self.assertEqual(self.update.call_count, count)
        self.assertEqual(self.thread.call_count, 2)


if __name__ == "__main__":
    unittest.main()
