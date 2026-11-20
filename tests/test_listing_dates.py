import unittest
from datetime import datetime

from core.automation import listing_dates as ld
from core.automation import listing_visibility as lv

NOW = datetime(2026, 11, 20, 14, 30)


class ListingDateTests(unittest.TestCase):
    def test_day_order_is_inferred_per_profile(self):
        self.assertEqual(ld.infer_day_order(["Listed on 20/11", "Listed on 14/11"]), "dm")
        self.assertEqual(ld.infer_day_order(["Listed on 11/18", "Listed on 11/9"]), "md")
        self.assertIsNone(ld.infer_day_order(["Listed on 11/10"]))

    def test_old_card_dates_in_both_locales(self):
        self.assertEqual(ld.classify("Listed on 20/11", "today", NOW), "in")
        self.assertEqual(ld.classify("Listed on 11/19", "yesterday", NOW), "in")
        self.assertEqual(ld.classify("Listed on 11/18", "yesterday", NOW, order="md"), "out")
        # Yesterday's whole day only partly overlaps the last 24 hours.
        self.assertEqual(ld.classify("Listed on 19/11", "24h", NOW), "approx")

    def test_ambiguous_numeric_date_is_not_guessed(self):
        # 11/10 could be Nov 10 or Oct 11: both outside 24h, so the answer is still safe.
        self.assertEqual(ld.classify("Listed on 11/10", "24h", NOW), "out")
        # 11/20 vs 20/11 cannot be swapped; 10/11 vs 11/10 disagree for "7d" only if one is recent.
        self.assertEqual(ld.classify("Listed on 11/20", "today", NOW), "in")

    def test_future_dates_roll_back_a_year(self):
        jan = datetime(2027, 1, 2, 9, 0)
        self.assertEqual(ld.classify("Listed on 12/30", "7d", jan, order="md"), "in")

    def test_relative_texts(self):
        self.assertEqual(ld.classify("Posted 3 hours ago", "12h", NOW), "in")
        self.assertEqual(ld.classify("Listed a day ago", "24h", NOW), "approx")
        self.assertEqual(ld.classify("Posted a week ago", "24h", NOW), "out")
        self.assertEqual(ld.classify("Listed yesterday", "yesterday", NOW), "in")
        self.assertEqual(ld.classify("just now", "12h", NOW), "in")
        self.assertEqual(ld.classify("", "12h", NOW), "unknown")
        self.assertEqual(ld.classify("Listed on Marketplace", "12h", NOW), "unknown")

    def test_filter_reports_kept_approx_and_unknown(self):
        listings = [{"listed": "Listed on 11/20"}, {"listed": "Listed on 11/19"}, {"listed": "Listed on 11/2"},
                    {"listed": ""}]
        kept, summary = ld.filter_listings(listings, "24h", NOW)
        self.assertEqual(len(kept), 2)
        self.assertEqual((summary["approx"], summary["unknown"], summary["total"]), (1, 1, 4))
        text = ld.describe_filter(summary)
        self.assertIn("Listed within Last 24 hours: 2 of 4", text)
        self.assertIn("without a readable date", text)

    def test_any_time_keeps_everything(self):
        listings = [{"listed": ""}, {"listed": "x"}]
        kept, summary = ld.filter_listings(listings, "all", NOW)
        self.assertEqual(kept, listings)
        self.assertEqual(ld.describe_filter(summary), "")

    def test_window_setting_is_part_of_the_run_snapshot(self):
        self.assertEqual(lv.BuyerOptions.from_settings({"live_check_listed_window": "24h"}).listed_window, "24h")
        self.assertEqual(lv.BuyerOptions.from_settings({"live_check_listed_window": "Yesterday"}).listed_window,
                         "yesterday")
        self.assertEqual(lv.BuyerOptions.from_settings({"live_check_listed_window": "bogus"}).listed_window, "all")


class BuyerFailureGuidanceTests(unittest.TestCase):
    def test_each_buyer_gets_its_own_reason_and_step(self):
        summary, step = lv.buyer_guidance({"PK": [lv.LOGIN_REQUIRED] * 3, "US": [lv.NETWORK_ERROR] * 3})
        self.assertIn("PK buyer could not finish (logged out of Facebook)", summary)
        self.assertIn("US buyer could not finish (internet/proxy connection failed)", summary)
        self.assertIn("Open the PK buyer profile and sign in", step)
        self.assertIn("Check the US buyer's internet/proxy", step)
        self.assertNotIn("PK, US", step)

    def test_same_failure_is_grouped(self):
        summary, step = lv.buyer_guidance({"PK": [lv.UNCLEAR], "US": [lv.UNCLEAR]})
        self.assertIn("PK, US buyer could not finish (search page could not be read)", summary)
        self.assertEqual(step.count("buyer profile"), 1)


if __name__ == "__main__":
    unittest.main()
