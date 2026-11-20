import unittest

from core.automation import listing_visibility as lv


class BuyerReadabilityTests(unittest.TestCase):
    def test_unread_search_is_not_displayed_as_a_zero_score(self):
        text = lv.summarize_checker([lv.UNCLEAR] * 3)
        self.assertIn("Could not check any of 3", text)
        self.assertNotIn("0/3", text)
        self.assertNotIn("Found in search: 0", text)

    def test_user_reported_run_prioritizes_failed_us_buyer(self):
        targets = [{"title": name, "listingId": str(index)} for index, name in enumerate(("Lights", "Furniture", "Spa"))]
        values = lv.row_columns(targets, {"PK": [lv.LINK_ONLY, lv.TEXT_MATCH, lv.TEXT_MATCH],
                                         "US": [lv.UNCLEAR] * 3}, skipped=61)
        self.assertTrue(values["Visibility"].startswith("PARTIAL:"))
        self.assertIn("US buyer could not finish", values["Buyer Summary"])
        self.assertIn("seller could not be confirmed", values["Buyer Summary"])
        self.assertIn("61 other", values["Buyer Summary"])
        self.assertIn("US buyer profile", values["Next Step"])
        self.assertIn("city/ZIP", values["Next Step"])
        self.assertNotIn("US: UNCLEAR", values["Visibility Details"])
        self.assertIn("US: Could not check search", values["Visibility Details"])

    def test_all_links_open_is_not_described_as_hidden(self):
        values = lv.row_columns([{"title": "Lights", "listingId": "123"}],
                                {"PK": [lv.LINK_ONLY], "US": [lv.LINK_ONLY]})
        self.assertTrue(values["Visibility"].startswith("NOT_IN_SEARCH:"))
        self.assertIn("link opens", values["Buyer Summary"])
        self.assertNotIn("hidden", values["Buyer Summary"].lower())
        self.assertIn("Marketplace city/ZIP", values["Next Step"])

    def test_success_and_disabled_states_have_clear_next_steps(self):
        summary, action = lv.buyer_guidance({"PK": [lv.SEARCH], "US": [lv.SEARCH]})
        self.assertIn("found the exact listing in search", summary)
        self.assertEqual(action, "No issue found in this sample.")
        values = lv.owner_buyer_columns(lv.BuyerOptions(enabled=False))
        self.assertEqual(values["Buyer Summary"], "Buyer checks are off.")

    def test_account_problems_have_specific_actions(self):
        for state, action in ((lv.LOGIN_REQUIRED, "sign in"), (lv.CHECKPOINT, "account check"),
                              (lv.NETWORK_ERROR, "internet/proxy")):
            with self.subTest(state=state):
                summary, next_step = lv.buyer_guidance({"US": [state]})
                self.assertIn("US", summary)
                self.assertIn(action, next_step)

    def test_missing_results_cannot_show_a_success_summary(self):
        values = lv.row_columns([{"title": "A"}, {"title": "B"}], {"PK": [lv.SEARCH], "US": []})
        self.assertTrue(values["Visibility"].startswith("PARTIAL:"))
        self.assertIn("PK, US buyer could not finish", values["Buyer Summary"])
        self.assertIn("Could not check any of 2", values["US Check"])

    def test_a_passed_planned_sample_is_not_labeled_as_a_failed_check(self):
        values = lv.row_columns([{"title": "A"}], {"PK": [lv.SEARCH]}, skipped=20)
        self.assertEqual(lv.visibility_label(values["Visibility"]), "Sample passed — other listings not checked")
        self.assertEqual(values["Next Step"], "Use a larger sample if needed.")

    def test_missing_seller_links_are_not_blame_assigned_to_the_buyers(self):
        values = lv.row_columns([{"title": "Lights"}], {"PK": [lv.ID_MISSING], "US": [lv.ID_MISSING]})
        self.assertIn("Exact seller links are missing", values["Buyer Summary"])
        self.assertNotIn("buyer could not finish", values["Buyer Summary"])
        self.assertIn("Re-run the seller scan", values["Next Step"])
        self.assertNotIn("buyer profile", values["Next Step"])
        self.assertNotIn("Found in search: 0", values["US Check"])


if __name__ == "__main__":
    unittest.main()
