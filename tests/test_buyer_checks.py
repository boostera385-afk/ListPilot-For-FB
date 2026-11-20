from dataclasses import FrozenInstanceError
import threading
import unittest
from unittest import mock

from core.automation import listing_visibility as lv
from core.automation import visibility_checker as vc
from core.engine.exceptions import AutomationStopped


class BuyerOptionsTests(unittest.TestCase):
    def test_quick_default_and_legacy_profiles(self):
        options = lv.BuyerOptions.from_settings({"live_check_pk_profile": "pk", "live_check_us_profile": "us"})
        self.assertEqual(options.limit, 3)
        self.assertFalse(options.open_details)
        self.assertEqual(options.checkers, (("PK", "pk"), ("US", "us")))
        with self.assertRaises(FrozenInstanceError):
            options.limit = 15

    def test_master_and_individual_switches_keep_saved_ids(self):
        options = lv.BuyerOptions.from_settings({
            "live_check_buyers_enabled": False, "live_check_pk_profile": "pk", "live_check_us_profile": "us"})
        self.assertEqual(options.checkers, ())
        self.assertEqual(options.pk_profile, "pk")
        options = lv.BuyerOptions.from_settings({
            "live_check_pk_enabled": False, "live_check_pk_profile": "pk", "live_check_us_profile": "us"})
        self.assertEqual(options.checkers, (("US", "us"),))

    def test_sample_limit_is_validated(self):
        for value, expected in ((0, 1), (99, 15), ("bad", 3), (None, 3)):
            self.assertEqual(lv.BuyerOptions.from_settings({"live_check_sample_size": value}).limit, expected)

    def test_known_ids_are_sampled_first(self):
        listings = [{"state": "LIVE", "title": "Missing ID", "listingId": ""},
                    {"state": "LIVE", "title": "Known", "listingId": "123"}]
        targets, skipped = lv.visibility_targets(listings, limit=1)
        self.assertEqual(targets[0]["listingId"], "123")
        self.assertEqual(skipped, 1)

    @mock.patch.object(vc, "resolve_profile_id", side_effect=lambda value: {"PK name": "buyer", "owner name": "buyer"}.get(value, value))
    def test_self_checks_and_duplicate_buyers_are_rejected(self, resolve):
        options = lv.BuyerOptions(pk_profile="PK name", us_profile="us")
        self.assertIn("also a buyer", vc.validate_checker_roles([{"id": "owner name", "rowNumber": 2}], options))
        self.assertIn("different", vc.validate_checker_roles([], lv.BuyerOptions(pk_profile="same", us_profile="same")))
        self.assertEqual(vc.validate_checker_roles([{"id": "owner name"}], lv.BuyerOptions(enabled=False)), "")


class BuyerRuntimeTests(unittest.TestCase):
    def test_disabled_buyers_never_launch_profiles(self):
        with mock.patch.object(vc, "start_profile") as start:
            self.assertEqual(vc.run_visibility_checks([], lambda _: None, threading.Event(),
                                                      options=lv.BuyerOptions(enabled=False)), {})
        start.assert_not_called()

    def test_no_ids_are_unverified_without_launching_a_profile(self):
        posts = [{"rowNumber": 4, "_live_listings": [{"state": "LIVE", "title": "Generic service", "listingId": ""}]}]
        options = lv.BuyerOptions(pk_profile="pk", us_enabled=False)
        with mock.patch.object(vc, "start_profile") as start, \
             mock.patch.object(vc, "update_column_values", return_value=(True, "ok")):
            vc.run_visibility_checks(posts, lambda _: None, threading.Event(), options=options)
        start.assert_not_called()
        values = posts[0]["_visibility_cols"]
        self.assertEqual(values["US Check"], "Off")
        self.assertIn("unverified", values["PK Check"])
        self.assertTrue(values["Visibility"].startswith("PARTIAL"))

    def test_complete_search_query_is_reused_for_repeated_titles(self):
        page = mock.Mock()
        page.url = lv.search_url("Same title")
        page.evaluate.return_value = [{"href": "/marketplace/item/999/", "text": "Other seller"}]
        cache = {}
        with mock.patch.object(vc, "_goto", return_value=True) as goto, \
             mock.patch.object(vc, "_blocked_reason", return_value=""):
            self.assertFalse(vc._search(page, {"title": "Same title", "listingId": "1"}, lambda _: None, cache))
            self.assertFalse(vc._search(page, {"title": "Same title", "listingId": "2"}, lambda _: None, cache))
        goto.assert_called_once()

    def test_keyword_fallback_can_find_an_exact_id_after_a_title_miss(self):
        target = {"title": "Best Roof Repair Service", "listingId": "123"}
        with mock.patch.object(vc, "_search", side_effect=["", "ID"]) as search:
            self.assertEqual(vc._search_with_fallback(mock.Mock(), target, lambda _: None,
                                                     lambda _: None, {}), "ID")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args.kwargs["query"], "roof repair")

    def test_keyword_failure_is_unverified_and_does_not_become_a_miss(self):
        target = {"title": "Roof Repair Service", "listingId": "123"}
        with mock.patch.object(vc, "_search", side_effect=["", None]):
            self.assertIsNone(vc._search_with_fallback(mock.Mock(), target, lambda _: None,
                                                       lambda _: None, {}))

    def test_keyword_query_does_not_relax_listing_identity(self):
        target = {"title": "Roof Repair Service", "listingId": "123", "price": "$50"}
        page = mock.Mock(url=lv.search_url("roof repair"))
        page.evaluate.return_value = [{"href": "/marketplace/item/999/", "text": "$50\nRoof Repair Service"}]
        with mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""):
            self.assertEqual(vc._search(page, target, lambda _: None, query="roof repair"), "")
            page.evaluate.return_value = [{"href": "/marketplace/item/123/", "text": "$50\nRoof Repair Service"}]
            self.assertEqual(vc._search(page, target, lambda _: None, query="roof repair"), "ID")

    def test_title_hit_or_unloaded_title_does_not_launch_keyword_search(self):
        for result in ("ID", "TEXT", None):
            with self.subTest(result=result), mock.patch.object(vc, "_search", return_value=result) as search:
                self.assertEqual(vc._search_with_fallback(mock.Mock(), {"title": "Roof Repair Service"},
                                                          lambda _: None, lambda _: None, {}), result)
                search.assert_called_once()

    def fake_browser(self):
        runtime = mock.MagicMock()
        browser = runtime.__enter__.return_value.chromium.connect_over_cdp.return_value
        page = mock.Mock()
        context = mock.Mock()
        context.new_page.return_value = page
        browser.contexts = [context]
        return runtime

    def test_repeated_listing_ids_reuse_results_and_no_ids_never_match_by_title(self):
        targets = [{"title": "A", "listingId": "123"}, {"title": "A", "listingId": "123"},
                   {"title": "Generic service", "listingId": ""}]
        with mock.patch.object(vc, "sync_playwright", return_value=self.fake_browser()), \
             mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""), \
             mock.patch.object(vc, "_search", return_value="ID") as search:
            results = vc.check_listings("local-debug", "PK", targets, lambda _: None, lambda _: None)
        self.assertEqual(results, [lv.SEARCH, lv.SEARCH, lv.UNCLEAR])
        search.assert_called_once()

    def test_unloaded_search_does_not_become_link_only(self):
        with mock.patch.object(vc, "sync_playwright", return_value=self.fake_browser()), \
             mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""), \
             mock.patch.object(vc, "_search", return_value=None), \
             mock.patch.object(vc, "_open_link", return_value=lv.LINK_ONLY):
            self.assertEqual(vc.check_listings("debug", "PK", [{"title": "A", "listingId": "123"}],
                                              lambda _: None, lambda _: None), [lv.UNCLEAR])

    def test_cancelled_pass_records_stopped_instead_of_leaving_waiting_results(self):
        posts = [{"rowNumber": 4, "_live_listings": [{"state": "LIVE", "title": "A", "listingId": "123"}]}]
        stop = threading.Event()
        stop.set()
        with mock.patch.object(vc, "start_profile") as start, \
             mock.patch.object(vc, "update_column_values", return_value=(True, "ok")):
            with self.assertRaises(AutomationStopped):
                vc.run_visibility_checks(posts, lambda _: None, stop, options=lv.BuyerOptions(pk_profile="pk"))
        start.assert_not_called()
        self.assertEqual(posts[0]["_visibility_cols"]["Visibility"], "STOPPED")

    def test_redirected_search_is_unverified_even_if_the_other_page_has_cards(self):
        page = mock.Mock(url=lv.search_url("Different title"))
        with mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""):
            self.assertIsNone(vc._search(page, {"title": "Roof", "listingId": "123"}, lambda _: None))
        page.evaluate.assert_not_called()

    def test_profile_cleanup_failure_preserves_verified_buyer_results(self):
        posts = [{"rowNumber": 4, "_live_listings": [{"state": "LIVE", "title": "Roof", "listingId": "123"}]}]
        with mock.patch.object(vc, "start_profile", return_value=(True, {"url": "http://127.0.0.1:1"})), \
             mock.patch.object(vc, "stop_profile", side_effect=RuntimeError("provider unavailable")), \
             mock.patch.object(vc, "check_listings", return_value=[lv.SEARCH]), \
             mock.patch.object(vc, "update_column_values", return_value=(True, "OK")):
            summary = vc.run_visibility_checks(posts, lambda _: None, threading.Event(),
                                                options=lv.BuyerOptions(pk_profile="pk", us_enabled=False))
        self.assertEqual(summary, {"OK": 1})
        self.assertEqual(posts[0]["_visibility_cols"]["PK Check"], "1/1 in search")

    def test_stop_after_last_result_does_not_erase_it(self):
        stop = threading.Event()

        def sleep(_seconds):
            if stop.is_set():
                raise AutomationStopped("Stopped")

        with mock.patch.object(vc, "sync_playwright", return_value=self.fake_browser()), \
             mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""), \
             mock.patch.object(vc, "_search", return_value="ID"):
            results = vc.check_listings("debug", "PK", [{"title": "Roof", "listingId": "123"}],
                                         lambda _: None, sleep, on_result=stop.set)
        self.assertEqual(results, [lv.SEARCH])


if __name__ == "__main__":
    unittest.main()


class ParallelBuyerTests(unittest.TestCase):
    def test_pk_and_us_run_side_by_side(self):
        barrier = threading.Barrier(2, timeout=5)  # both must be inside a check at the same time

        def fake_check(url, label, targets, log, sleep, on_result=None, on_detail=None):
            barrier.wait()
            for _ in targets:
                on_result()
            return [lv.SEARCH] * len(targets)

        posts = [{"rowNumber": 4, "_live_listings": [{"state": "LIVE", "title": "A", "listingId": "123", "price": "$5"}]}]
        options = lv.BuyerOptions(pk_profile="pk", us_profile="us")
        saved = []
        with mock.patch.object(vc, "start_profile", return_value=(True, {"url": "http://127.0.0.1:1"})),              mock.patch.object(vc, "stop_profile", return_value=True),              mock.patch.object(vc, "check_listings", side_effect=fake_check),              mock.patch.object(vc, "update_column_values", side_effect=lambda sheet, row, values: saved.append(values) or (True, "ok")):
            summary = vc.run_visibility_checks(posts, lambda _: None, threading.Event(), options=options)
        self.assertEqual(summary, {"OK": 1}, "a sequential run would time out on the barrier and report ERROR")
        self.assertEqual(saved[0]["PK Check"], "1/1 in search")
        self.assertEqual(saved[0]["US Check"], "1/1 in search")

    def test_one_buyer_failing_to_start_keeps_the_other_buyers_results(self):
        def fake_start(profile, **kwargs):
            return (True, {"url": "http://127.0.0.1:1"}) if profile == "us" else (False, "profile missing")

        posts = [{"rowNumber": 4, "_live_listings": [{"state": "LIVE", "title": "A", "listingId": "123", "price": "$5"}]}]
        saved = []
        with mock.patch.object(vc, "start_profile", side_effect=fake_start),              mock.patch.object(vc, "stop_profile", return_value=True),              mock.patch.object(vc, "check_listings", side_effect=lambda *a, on_result=None, **k: (on_result(), [lv.SEARCH])[1]),              mock.patch.object(vc, "update_column_values", side_effect=lambda sheet, row, values: saved.append(values) or (True, "ok")):
            vc.run_visibility_checks(posts, lambda _: None, threading.Event(),
                                     options=lv.BuyerOptions(pk_profile="pk", us_profile="us"))
        self.assertEqual(saved[0]["US Check"], "1/1 in search")
        self.assertIn("error", saved[0]["PK Check"])


class SlowSearchRetryTests(unittest.TestCase):
    def run_check(self, search_results):
        with mock.patch.object(vc, "sync_playwright", return_value=BuyerRuntimeTests.fake_browser(None)), \
             mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", return_value=""), \
             mock.patch.object(vc, "_search", side_effect=search_results) as search:
            results = vc.check_listings("debug", "US", [{"title": "A", "listingId": "123", "price": "$5"}],
                                        lambda _: None, lambda _: None)
        return results, search

    def test_a_slow_search_page_is_retried_once(self):
        results, search = self.run_check([None, "ID"])
        self.assertEqual(results, [lv.SEARCH])
        self.assertEqual(search.call_count, 2)

    def test_a_search_page_that_never_loads_stays_unverified(self):
        results, search = self.run_check([None, None])
        self.assertEqual(results, [lv.UNCLEAR])
        self.assertEqual(search.call_count, 2)


class MidRunBlockTests(unittest.TestCase):
    def run_check(self, blocked_answers):
        targets = [{"title": "A", "listingId": "1", "price": "$5"}, {"title": "B", "listingId": "2", "price": "$6"}]
        with mock.patch.object(vc, "sync_playwright", return_value=BuyerRuntimeTests.fake_browser(None)), \
             mock.patch.object(vc, "_goto", return_value=True), \
             mock.patch.object(vc, "_blocked_reason", side_effect=blocked_answers), \
             mock.patch.object(vc, "_search", return_value="ID"), \
             mock.patch.object(vc, "capture_diagnostics") as capture:
            logs = []
            results = vc.check_listings("debug", "PK", targets, logs.append, lambda _: None)
        return results, logs, capture

    def test_a_confirmed_block_stops_the_checker_with_a_reason_and_a_screenshot(self):
        # startup ok, then blocked twice (the re-check confirms it)
        results, logs, capture = self.run_check(["", "LOGGED_OUT", "LOGGED_OUT"])
        self.assertEqual(results, [lv.LOGIN_REQUIRED, lv.LOGIN_REQUIRED])
        capture.assert_called_once()
        self.assertTrue(any("interrupted (LOGGED_OUT)" in line for line in logs))

    def test_completed_results_survive_a_later_sign_in_prompt(self):
        results, logs, capture = self.run_check(["", "", "LOGGED_OUT", "LOGGED_OUT"])
        self.assertEqual(results, [lv.SEARCH, lv.LOGIN_REQUIRED])
        self.assertTrue(any("not evidence" in line for line in logs))

    def test_network_interruption_is_not_called_logged_out(self):
        results, logs, capture = self.run_check(["", lv.NETWORK_ERROR, lv.NETWORK_ERROR])
        self.assertEqual(results, [lv.NETWORK_ERROR, lv.NETWORK_ERROR])
        self.assertFalse(any("LOGGED_OUT" in line for line in logs))

    def test_a_passing_popup_does_not_stop_the_checker(self):
        # startup ok, blocked once, clear on the re-check, then clear for the next listing
        results, logs, capture = self.run_check(["", "CHECKPOINT", "", ""])
        self.assertEqual(results, [lv.SEARCH, lv.SEARCH])
        capture.assert_not_called()
