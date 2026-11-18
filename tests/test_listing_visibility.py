import unittest

from core.automation import listing_status as ls
from core.automation import listing_visibility as lv


class TargetTests(unittest.TestCase):
    def test_only_live_listings_with_titles_and_cap(self):
        listings = [
            {"state": ls.LIVE, "title": "Roof repair", "listingId": "1"},
            {"state": ls.PENDING, "title": "Gutter", "listingId": "2"},
            {"state": ls.LIVE, "title": "", "listingId": "3"},
            {"state": ls.LIVE, "title": "Fence", "listingId": ""},
        ]
        targets, skipped = lv.visibility_targets(listings)
        self.assertEqual([t["title"] for t in targets], ["Roof repair", "Fence"])
        self.assertEqual(skipped, 0)
        targets, skipped = lv.visibility_targets(listings, limit=1)
        self.assertEqual(len(targets), 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(lv.visibility_targets(None), ([], 0))


class SearchMatchTests(unittest.TestCase):
    def test_match_by_id_ignores_same_title_other_listing(self):
        results = [{"href": "/marketplace/item/999/?ref=search", "text": "Roof repair $50"}]
        self.assertFalse(lv.found_in_search(results, {"title": "Roof repair", "listingId": "123"}))
        results.append({"href": "https://www.facebook.com/marketplace/item/123/", "text": ""})
        self.assertTrue(lv.found_in_search(results, {"title": "Roof repair", "listingId": "123"}))

    def test_title_only_match_is_not_verified(self):
        results = [{"href": "/marketplace/item/5/", "text": "$40\nRoof Repair!\nDallas, TX"}]
        self.assertFalse(lv.found_in_search(results, {"title": "roof repair", "listingId": ""}))
        self.assertFalse(lv.found_in_search(results, {"title": "Fence", "listingId": ""}))

    def test_urls_are_encoded(self):
        self.assertIn("query=Roof%20%26%20gutter", lv.search_url("Roof & gutter"))
        self.assertEqual(lv.item_url("123"), "https://www.facebook.com/marketplace/item/123/")


class ItemPageTests(unittest.TestCase):
    def test_classify(self):
        url = "https://www.facebook.com/marketplace/item/1/"
        self.assertEqual(lv.classify_item_page(url, "Roof repair\n$50", "Roof repair"), lv.LINK_ONLY)
        self.assertEqual(lv.classify_item_page(url, "This listing isn't available", "Roof repair"), lv.HIDDEN)
        self.assertEqual(lv.classify_item_page("https://www.facebook.com/marketplace/", "x", "Roof repair"), lv.UNCLEAR)
        self.assertEqual(lv.classify_item_page(url, "Loading", "Roof repair"), lv.UNCLEAR)


class RowTests(unittest.TestCase):
    targets = [{"title": "A", "listingId": "1"}, {"title": "B", "listingId": "2"}]

    def test_all_found(self):
        values = lv.row_columns(self.targets, {"PK": [lv.SEARCH, lv.SEARCH], "US": [lv.SEARCH, lv.SEARCH]})
        self.assertEqual(values["Visibility"], ls.OK)
        self.assertEqual(values["PK Check"], "2/2 in search")
        self.assertIn("Sample: 2 of 2", values["Visibility Details"])
        self.assertTrue(values["Visibility Details"].endswith("A | ID 1 | PK: SEARCH | US: SEARCH\nB | ID 2 | PK: SEARCH | US: SEARCH"))

    def test_hidden_beats_link_only(self):
        values = lv.row_columns(self.targets, {"PK": [lv.LINK_ONLY, lv.SEARCH], "US": [lv.HIDDEN, lv.SEARCH]})
        self.assertEqual(values["Visibility"], "HIDDEN: 1 result(s)")
        self.assertEqual(values["US Check"], "1/2 in search, 1 unavailable")

    def test_link_only_and_gaps(self):
        self.assertEqual(lv.overall_visibility({"PK": [lv.LINK_ONLY, lv.ERROR]}), "NOT_IN_SEARCH: 1 result(s)")
        self.assertEqual(lv.overall_visibility({"PK": [lv.SEARCH, lv.ERROR]}), "PARTIAL: 1 result(s) not checked")
        self.assertEqual(lv.overall_visibility({}), "NOT_CHECKED")

    def test_skipped_listings_make_ok_partial(self):
        values = lv.row_columns(self.targets, {"PK": [lv.SEARCH, lv.SEARCH]}, skipped=3)
        self.assertTrue(values["Visibility"].startswith("PARTIAL: 3 listing(s)"))


if __name__ == "__main__":
    unittest.main()
