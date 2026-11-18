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
        page.evaluate.return_value = [{"href": "/marketplace/item/999/", "text": "Other seller"}]
        cache = {}
        with mock.patch.object(vc, "_goto", return_value=True) as goto:
            self.assertFalse(vc._search(page, {"title": "Same title", "listingId": "1"}, lambda _: None, cache))
            self.assertFalse(vc._search(page, {"title": "Same title", "listingId": "2"}, lambda _: None, cache))
        goto.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
