import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from core.automation import incogniton_manager as im
from core.automation import listing_visibility as lv
from core.automation import visibility_checker as vc
from core.engine import grouped_automation_engine as grouped
from core.engine import profile_worker as single
from core.engine.exceptions import AutomationStopped, RetryableError


def response(payload):
    result = mock.Mock(status_code=200)
    result.json.return_value = payload
    return result


class LaunchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.profile = "6645144d-c5da-4451-8d16-2fa50488ea42"
        im._uncertain_launches.discard(self.profile)
        im._launch_attempts.pop(self.profile, None)
        self.addCleanup(im._uncertain_launches.discard, self.profile)
        self.addCleanup(im._launch_attempts.pop, self.profile, None)
        for patcher in (mock.patch.object(im, "_profile_cdp_candidates", return_value=[]),
                        mock.patch.object(im, "_probe_cdp", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def inline_thread(**kwargs):
        thread = mock.Mock()
        thread.start.side_effect = kwargs["target"]
        return thread

    def test_timeout_recovers_our_browser_without_stopping_or_relaunching(self):
        attempt = im._LaunchAttempt(error=RetryableError("API timed out"))
        attempt.done.set()
        im._launch_attempts[self.profile] = attempt
        im._uncertain_launches.add(self.profile)
        ready = {"status": "ok", "url": "ws://127.0.0.1:9222/devtools/browser/abc"}
        with mock.patch.object(im, "_api_request") as request, \
             mock.patch.object(im, "_ready_profile_cdp", return_value=ready):
            self.assertTrue(im.start_profile(self.profile)[0])
        request.assert_not_called()
        self.assertTrue(attempt.attached)

    def test_ready_browser_does_not_wait_for_the_launch_http_response(self):
        pending = im._LaunchAttempt()

        def begin(profile):
            im._launch_attempts[profile] = pending
            return pending

        ready = {"status": "ok", "url": "ws://127.0.0.1:9222/devtools/browser/abc"}
        with mock.patch.object(im, "_api_request", return_value=response({"status": "Ready"})) as request, \
             mock.patch.object(im, "_begin_launch", side_effect=begin) as launch, \
             mock.patch.object(im, "_ready_profile_cdp", return_value=ready):
            self.assertEqual(im.start_profile(self.profile), (True, ready))
            self.assertFalse(pending.done.is_set(), "the HTTP request is still pending when we return")
            self.assertTrue(im.start_profile(self.profile)[1].startswith("PROFILE_ALREADY_OPEN:"))
        launch.assert_called_once_with(self.profile)
        self.assertEqual(request.call_count, 1)

    def test_launch_http_request_is_sent_once_without_internal_retries(self):
        with mock.patch.object(im.threading, "Thread", side_effect=self.inline_thread), \
             mock.patch.object(im, "_api_request", return_value=response({"status": "ok"})) as request:
            attempt = im._begin_launch(self.profile)
        self.assertTrue(attempt.done.is_set())
        request.assert_called_once_with("get", f"/automation/launch/puppeteer/{self.profile}",
                                        timeout=im._LAUNCH_HTTP_TIMEOUT, retry=False)

    def test_existing_user_session_is_not_stopped(self):
        with mock.patch.object(im, "_api_request", return_value=response({"status": "Launched"})) as request:
            ok, message = im.start_profile(self.profile)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("PROFILE_ALREADY_OPEN:"))
        self.assertEqual(request.call_count, 1)

    def test_already_open_response_is_a_permanent_actionable_error(self):
        with mock.patch.object(im, "_api_request", side_effect=[
            response({"status": "Ready"}),
            response({"status": "error", "message": "Profile is already open"}),
        ]), mock.patch.object(im.threading, "Thread", side_effect=self.inline_thread):
            ok, message = im.start_profile(self.profile)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("PROFILE_ALREADY_OPEN:"))
        self.assertNotIn(self.profile, im._uncertain_launches)

    def test_unknown_launch_state_does_not_trigger_duplicate_request(self):
        im._uncertain_launches.add(self.profile)
        with mock.patch.object(im, "_api_request", return_value=response({"status": "unknown"})) as request:
            ok, message = im.start_profile(self.profile)
        self.assertFalse(ok)
        self.assertIn("No duplicate launch", message)
        request.assert_not_called()

    def test_unresolved_launch_is_bounded_and_not_replayed_on_another_call(self):
        attempt = im._LaunchAttempt()
        im._launch_attempts[self.profile] = attempt
        with mock.patch.object(im, "_LAUNCH_WAIT_SECONDS", 0), \
             mock.patch.object(im, "_api_request") as request, \
             mock.patch.object(im, "_begin_launch") as begin:
            for _ in range(2):
                ok, message = im.start_profile(self.profile)
                self.assertFalse(ok)
                self.assertTrue(message.startswith("PROFILE_LAUNCH_PENDING:"))
        request.assert_not_called()
        begin.assert_not_called()
        self.assertTrue(attempt.expired)

    def test_http_success_still_requires_a_ready_browser(self):
        attempt = im._LaunchAttempt(response=response({"status": "ok", "port": 9222}))
        attempt.done.set()
        with mock.patch.object(im, "_LAUNCH_WAIT_SECONDS", 0):
            ok, message = im._wait_for_launch(self.profile, attempt, None)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("PROFILE_LAUNCH_PENDING:"))

    def test_monitoring_can_be_stopped_without_relaunching_or_closing_any_browser(self):
        stop = threading.Event()
        stop.set()
        with mock.patch.object(im, "_api_request") as request, mock.patch.object(im, "_begin_launch") as begin:
            with self.assertRaises(AutomationStopped):
                im.start_profile(self.profile, stop_event=stop)
        request.assert_not_called()
        begin.assert_not_called()
        self.assertNotIn(self.profile, im._profile_launch_locks)

    def test_capacity_wait_can_be_cancelled_without_leaking_the_profile_lock(self):
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        capacity = threading.BoundedSemaphore(1)
        capacity.acquire()
        with mock.patch.object(im, "_launch_capacity", capacity):
            with self.assertRaises(AutomationStopped):
                with im._profile_launch_slot(self.profile, stop_event=stop):
                    self.fail("a full startup slot must not be entered")
        self.assertNotIn(self.profile, im._profile_launch_locks)
        capacity.release()
        self.assertTrue(capacity.acquire(blocking=False))
        capacity.release()

    def test_failed_launch_can_be_rechecked_after_it_finishes_and_browser_is_closed(self):
        previous = im._LaunchAttempt(expired=True, error=RetryableError("slow"))
        previous.done.set()
        im._launch_attempts[self.profile] = previous
        ready = {"status": "ok", "url": "ws://127.0.0.1:9222/devtools/browser/abc"}
        with mock.patch.object(im, "_api_request", side_effect=[
            response({"status": "Ready"}), response({"status": "Ready"}),
            response({"status": "ok", "port": 9222}),
        ]), mock.patch.object(im.threading, "Thread", side_effect=self.inline_thread), \
             mock.patch.object(im, "_ready_profile_cdp", return_value=ready):
            self.assertEqual(im.start_profile(self.profile), (True, ready))
        self.assertIsNot(im._launch_attempts[self.profile], previous)

    def test_failed_preflight_does_not_launch_or_claim_ownership(self):
        with mock.patch.object(im, "_api_request", side_effect=RetryableError("API unavailable")) as request:
            ok, message = im.start_profile(self.profile)
        self.assertFalse(ok)
        self.assertIn("verify profile status", message)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn(self.profile, im._uncertain_launches)

    def test_temporary_sync_is_polled_before_a_single_launch(self):
        ready = {"status": "ok", "url": "ws://127.0.0.1:9222/devtools/browser/a"}
        logs = []
        with mock.patch.object(im, "_api_request", side_effect=[
            response({"status": "Syncing"}), response({"status": "Ready"}),
            response({"status": "ok", "port": 9222}),
        ]) as request, mock.patch.object(im.time, "sleep"), \
             mock.patch.object(im.threading, "Thread", side_effect=self.inline_thread), \
             mock.patch.object(im, "_ready_profile_cdp", return_value=ready):
            self.assertTrue(im.start_profile(self.profile, logger_callback=logs.append)[0])
        self.assertEqual(sum("launch/puppeteer" in call.args[1] for call in request.call_args_list), 1)
        self.assertTrue(any("waiting briefly" in line for line in logs))

    def test_process_still_open_despite_ready_status_is_not_relaunched(self):
        with mock.patch.object(im, "_api_request", return_value=response({"status": "Ready"})) as request, \
             mock.patch.object(im, "_profile_cdp_candidates", return_value=["http://127.0.0.1:9222"]), \
             mock.patch.object(im, "_begin_launch") as launch:
            ok, message = im.start_profile(self.profile)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("PROFILE_ALREADY_OPEN:"))
        launch.assert_not_called()
        request.assert_called_once()

    def test_connection_refusal_fails_fast_but_does_not_replay_an_ambiguous_timeout(self):
        error = RetryableError("unreachable")
        error.__cause__ = im.requests.exceptions.ConnectionError("connection refused")
        attempt = im._LaunchAttempt(error=error)
        attempt.done.set()
        im._launch_attempts[self.profile] = attempt
        with mock.patch.object(im, "_ready_profile_cdp", return_value=None), \
             mock.patch.object(im.time, "sleep") as sleep:
            ok, message = im._wait_for_launch(self.profile, attempt, None)
        self.assertFalse(ok)
        self.assertIn("before the launch request was delivered", message)
        self.assertNotIn(self.profile, im._launch_attempts)
        sleep.assert_not_called()

    def test_launch_failure_is_recorded_and_completed_once(self):
        post = {"rowNumber": 8, "_buyer_options": lv.BuyerOptions(pk_profile="pk", us_profile="us")}
        complete = mock.Mock()
        worker = grouped.GroupedProfileWorker("Live Checker", self.profile, [post], mock.Mock(),
                                              lambda _: None, complete)
        with mock.patch.object(grouped, "start_profile", return_value=(False, "PROFILE_ALREADY_OPEN: close it")) as start, \
             mock.patch.object(grouped, "claim_row", return_value=(True, "Claimed")), \
             mock.patch.object(grouped, "update_row_status") as status, \
             mock.patch("core.services.sheets_service.update_column_values", return_value=(True, "Saved")) as save:
            worker.run()
        start.assert_called_once()
        complete.assert_called_once_with(post, False)
        self.assertEqual(post["_live_check"], "PROFILE_START_FAILED")
        values = save.call_args.args[2]
        self.assertEqual(values["Total Listings"], "")
        self.assertIn("seller scan failed", values["PK Check"])
        status.assert_called_once()

    def test_group_does_not_repeat_an_ambiguous_launch(self):
        complete = mock.Mock()
        worker = grouped.GroupedProfileWorker("Other task", self.profile, [{}], mock.Mock(),
                                              lambda _: None, complete)
        with mock.patch.object(grouped, "start_profile", return_value=(False, "PROFILE_LAUNCH_PENDING: slow")) as start:
            worker.run()
        start.assert_called_once()
        complete.assert_called_once_with({}, False)

    def test_single_worker_does_not_close_a_manually_opened_profile(self):
        post = {"rowNumber": 8, "id": self.profile, "profile name": "Seller"}
        complete = mock.Mock()
        worker = single.ProfileWorker("FB Listings", post, mock.Mock(), lambda _: None, complete)
        with mock.patch.object(single, "claim_row", return_value=(True, "Claimed")), \
             mock.patch.object(single, "start_profile", return_value=(False, "PROFILE_ALREADY_OPEN: close it")) as start, \
             mock.patch.object(single, "stop_profile") as stop, \
             mock.patch.object(single, "update_row_status"):
            worker.run()
        start.assert_called_once()
        stop.assert_not_called()
        complete.assert_called_once_with(post, False)

    def test_stopping_before_launch_leaves_unclaimed_rows_pending(self):
        post = {"rowNumber": 8}
        complete = mock.Mock()
        worker = grouped.GroupedProfileWorker("Live Checker", self.profile, [post], mock.Mock(),
                                              lambda _: None, complete)
        worker.stop()
        with mock.patch.object(grouped, "claim_row") as claim:
            worker.run()
        claim.assert_not_called()
        complete.assert_called_once_with(post, False)
        self.assertEqual(post["_live_check"], "STOPPED")


class StopConfirmationTests(unittest.TestCase):
    profile = "6645144d-c5da-4451-8d16-2fa50488ea42"

    def setUp(self):
        self.resolve = mock.patch.object(im, "resolve_profile_id", return_value=self.profile)
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.sleep = mock.patch.object(im.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(im._launch_attempts.pop, self.profile, None)
        self.addCleanup(im._uncertain_launches.discard, self.profile)

    def test_stop_error_is_success_only_after_exact_profile_closure_is_verified(self):
        with mock.patch.object(im, "_api_request", side_effect=[
            response({"status": "error", "message": "not running"}),
            response({"status": "Stopping"}), response({"status": "Ready"}),
        ]) as request, mock.patch.object(im, "_profile_cdp_candidates", return_value=[]):
            self.assertTrue(im.stop_profile(self.profile))
        self.assertEqual(sum(call.args[1].startswith("/profile/stop/") for call in request.call_args_list), 1)
        self.assertFalse(request.call_args_list[0].kwargs["retry"])

    def test_transport_failure_does_not_lose_a_verified_shutdown(self):
        with mock.patch.object(im, "_api_request", side_effect=[
            RetryableError("stop reply timed out"), response({"status": "Ready"}),
        ]), mock.patch.object(im, "_profile_cdp_candidates", return_value=[]):
            self.assertTrue(im.stop_profile(self.profile))

    def test_success_reply_with_a_still_running_browser_is_not_confirmation(self):
        with mock.patch.object(im, "_api_request", return_value=response({"status": "Ready"})), \
             mock.patch.object(im, "_profile_cdp_candidates", return_value=["http://127.0.0.1:9222"]):
            self.assertFalse(im.stop_profile(self.profile))

    def test_unknown_provider_status_does_not_prove_shutdown(self):
        with mock.patch.object(im, "_api_request", return_value=response({"status": "unknown"})), \
             mock.patch.object(im, "_profile_cdp_candidates", return_value=[]):
            self.assertFalse(im.stop_profile(self.profile))

    def test_ownership_state_is_cleared_only_after_confirmed_shutdown(self):
        attempt = im._LaunchAttempt(attached=True)
        attempt.done.set()
        im._launch_attempts[self.profile] = attempt
        im._uncertain_launches.add(self.profile)
        with mock.patch.object(im, "_api_request", return_value=response({"status": "ok"})), \
             mock.patch.object(im, "_confirm_profile_stopped", return_value=False):
            self.assertFalse(im.stop_profile(self.profile))
        self.assertTrue(attempt.attached)
        with mock.patch.object(im, "_api_request", return_value=response({"status": "ok"})), \
             mock.patch.object(im, "_confirm_profile_stopped", return_value=True):
            self.assertTrue(im.stop_profile(self.profile))
        self.assertTrue(attempt.stopped)
        self.assertFalse(attempt.attached)
        self.assertNotIn(self.profile, im._launch_attempts)


class CdpDiscoveryTests(unittest.TestCase):
    profile = "6645144d-c5da-4451-8d16-2fa50488ea42"

    def process(self, args, name="chrome.exe"):
        return mock.Mock(info={"name": name, "cmdline": args})

    def test_ports_are_bound_to_the_exact_profile_directory(self):
        directory = rf"C:\Incogniton\config\{self.profile}"
        processes = [
            self.process([f"--user-data-dir={directory}", "--remote-debugging-port=9222"]),
            self.process([f"--user-data-dir={directory}", "--remote-debugging-port=9222", "--type=renderer"]),
            self.process([f"--user-data-dir={directory}-other", "--remote-debugging-port=9333"]),
            self.process(["--user-data-dir", f'"{directory}"', "--remote-debugging-port", "9444"]),
            self.process([f"--user-data-dir={directory}", "--remote-debugging-port=99999"]),
            self.process([f"--user-data-dir={directory}", "--remote-debugging-port=9555"], "pwsh.exe"),
        ]
        with mock.patch.object(im.psutil, "process_iter", return_value=processes):
            self.assertEqual(im._profile_cdp_candidates(self.profile),
                             ["http://127.0.0.1:9222", "http://127.0.0.1:9444"])

    def test_probe_returns_a_verified_local_browser_endpoint(self):
        websocket = "ws://127.0.0.1:9222/devtools/browser/abc"
        with mock.patch.object(im.requests, "get", return_value=response({"webSocketDebuggerUrl": websocket})) as get:
            data = im._probe_cdp("http://127.0.0.1:9222")
        self.assertEqual(data["url"], websocket)
        get.assert_called_once_with("http://127.0.0.1:9222/json/version",
                                    timeout=(0.5, 0.5), allow_redirects=False)

    def test_remote_or_malformed_hints_are_never_requested(self):
        with mock.patch.object(im.requests, "get") as get:
            for url in ("http://example.com:9222", "file://127.0.0.1:9222", "http://127.0.0.1:bad", ""):
                self.assertIsNone(im._probe_cdp(url))
        get.assert_not_called()

    def test_probe_rejects_remote_redirected_or_wrong_port_websockets(self):
        for websocket in ("ws://example.com:9222/devtools/browser/a",
                          "ws://127.0.0.1:9333/devtools/browser/a",
                          "ws://127.0.0.1:9222/devtools/page/a"):
            with self.subTest(websocket=websocket):
                with mock.patch.object(im.requests, "get", return_value=response({"webSocketDebuggerUrl": websocket})):
                    self.assertIsNone(im._probe_cdp("http://127.0.0.1:9222"))

    def test_api_endpoint_is_tried_before_process_discovery(self):
        ready = {"status": "ok", "url": "ws://127.0.0.1:9222/devtools/browser/a"}
        with mock.patch.object(im, "_probe_cdp", return_value=ready) as probe, \
             mock.patch.object(im, "_profile_cdp_candidates") as discover:
            self.assertEqual(im._ready_profile_cdp(self.profile, {"port": 9222}), ready)
        probe.assert_called_once_with("http://127.0.0.1:9222")
        discover.assert_not_called()


class LaunchHttpIntegrationTests(unittest.TestCase):
    def test_connects_via_real_local_http_while_launch_reply_is_pending(self):
        profile = "00000000-1111-2222-3333-444444444444"
        release = threading.Event()
        launch_sent = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.server.server_port}/devtools/browser/test",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        launches = []

        def api(_method, endpoint, **_kwargs):
            if endpoint == f"/profile/status/{profile}":
                return response({"status": "Ready"})
            self.assertEqual(endpoint, f"/automation/launch/puppeteer/{profile}")
            launches.append(endpoint)
            launch_sent.set()
            release.wait(10)  # the fake provider deliberately withholds its reply
            return response({"status": "ok", "puppeteerUrl": origin})

        attempt = None
        try:
            with mock.patch.object(im, "_api_request", side_effect=api), \
                 mock.patch.object(im, "_profile_cdp_candidates",
                                   side_effect=lambda _profile: [origin] if launch_sent.is_set() else []):
                try:
                    ok, data = im.start_profile(profile)
                    attempt = im._launch_attempts[profile]
                    self.assertTrue(ok)
                    self.assertEqual(data["puppeteerUrl"], origin)
                    self.assertFalse(attempt.done.is_set(), "must return before the launch API replies")
                    self.assertEqual(len(launches), 1)
                finally:
                    release.set()
                    if attempt:
                        self.assertTrue(attempt.done.wait(5))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            serving.join(timeout=5)
            im._launch_attempts.pop(profile, None)
            im._uncertain_launches.discard(profile)


class NavigationRecoveryTests(unittest.TestCase):
    def test_wifi_change_is_retried_once(self):
        page = mock.Mock()
        page.goto.side_effect = [RuntimeError("net::ERR_NETWORK_CHANGED"), None]
        sleep = mock.Mock()
        self.assertTrue(vc._goto(page, "https://www.facebook.com/marketplace/", sleep))
        self.assertEqual(page.goto.call_count, 2)
        sleep.assert_any_call(2.0)

    def test_stop_is_not_swallowed_during_navigation_recovery(self):
        page = mock.Mock()
        page.goto.side_effect = RuntimeError("offline")
        with self.assertRaises(AutomationStopped):
            vc._goto(page, "https://www.facebook.com/", mock.Mock(side_effect=AutomationStopped("Stop")))
        self.assertEqual(page.goto.call_count, 1)

    def test_login_wall_cards_are_never_cached_as_a_search_miss(self):
        cache = {}
        with mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value="LOGGED_OUT"):
            self.assertIsNone(vc._search(mock.Mock(url=lv.search_url("Roof")), {"title": "Roof", "listingId": "1"},
                                         lambda _: None, cache))
        self.assertEqual(cache, {})
