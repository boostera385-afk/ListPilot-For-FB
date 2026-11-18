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


class BuyerTimeTests(unittest.TestCase):
    def test_row_columns_reports_how_long_the_buyer_check_took(self):
        targets = [{"title": "A", "listingId": "1"}]
        values = lv.row_columns(targets, {"PK": [lv.SEARCH]}, elapsed=95)
        self.assertIn("Buyer check took 1m 35s", values["Visibility Details"])
        self.assertNotIn("took", lv.row_columns(targets, {"PK": [lv.SEARCH]})["Visibility Details"])


class SearchFirstTests(unittest.TestCase):
    def card(self, href, title, price, place="Houston, TX"):
        return {"href": href, "text": "\n".join([price, title, place])}

    def test_id_hit_wins_and_text_hit_needs_exact_title_and_price(self):
        results = [self.card("/marketplace/item/111/", "Roof repair", "$45"),
                   self.card("/marketplace/item/222/", "Other seller", "$10")]
        self.assertEqual(lv.match_in_search(results, {"title": "x", "listingId": "111"}), "ID")
        self.assertEqual(lv.match_in_search(results, {"title": "x", "listingId": "999"}), "")
        no_id = {"title": "roof REPAIR", "listingId": "", "price": "$45"}
        self.assertEqual(lv.match_in_search(results, no_id), "TEXT")
        self.assertEqual(lv.match_in_search(results, dict(no_id, price="$46")), "")
        self.assertEqual(lv.match_in_search(results, dict(no_id, price="")), "")
        self.assertEqual(lv.match_in_search(results, dict(no_id, title="Roof")), "")

    def test_a_listing_with_an_id_is_never_text_matched(self):
        results = [self.card("/marketplace/item/222/", "Roof repair", "$45")]
        self.assertEqual(lv.match_in_search(results, {"title": "Roof repair", "listingId": "111", "price": "$45"}), "")

    def test_targets_carry_the_price(self):
        targets, _ = lv.visibility_targets([{"state": "LIVE", "title": "A", "listingId": "", "price": "$45"}])
        self.assertEqual(targets[0]["price"], "$45")

    def test_summary_and_overall_count_text_matches_as_in_search_but_not_verified(self):
        states = [lv.SEARCH, lv.TEXT_MATCH, lv.TEXT_MATCH]
        self.assertEqual(lv.summarize_checker(states), "3/3 in search, 2 by title+price")
        self.assertTrue(lv.overall_visibility({"PK": states}).startswith("PARTIAL: 2 result(s) matched by title+price"))
        self.assertEqual(lv.overall_visibility({"PK": [lv.SEARCH, lv.SEARCH]}), "OK")
        self.assertEqual(lv.overall_visibility({"PK": [lv.NOT_FOUND, lv.SEARCH]}), "NOT_IN_SEARCH: 1 result(s)")
        self.assertIn("1 not found", lv.summarize_checker([lv.SEARCH, lv.NOT_FOUND]))

    def test_listing_verdict_puts_search_visibility_first(self):
        self.assertEqual(lv.listing_verdict([lv.SEARCH, lv.SEARCH]), "Visible in search")
        self.assertEqual(lv.listing_verdict([lv.TEXT_MATCH, lv.TEXT_MATCH]), "Visible (title + price)")
        self.assertEqual(lv.listing_verdict([lv.SEARCH, lv.LINK_ONLY]), "Visible to some buyers")
        self.assertEqual(lv.listing_verdict([lv.LINK_ONLY, lv.LINK_ONLY]), "Not in search")
        self.assertEqual(lv.listing_verdict([lv.NOT_FOUND]), "Not in search")
        self.assertEqual(lv.listing_verdict([lv.SEARCH, lv.HIDDEN]), "Hidden from a buyer")
        self.assertEqual(lv.listing_verdict([lv.UNCLEAR]), "Not verified")
        self.assertEqual(lv.listing_verdict([]), "Not checked")
        self.assertEqual(lv.state_label(lv.LINK_ONLY), "Link opens, not in search")


class IdenticalListingTests(unittest.TestCase):
    @staticmethod
    def card(item_id):
        return {"href": f"/marketplace/item/{item_id}/", "text": "\n".join(["$30", "Gutter clean", "Houston"])}

    def test_need_counts_distinct_result_cards(self):
        target = {"title": "Gutter clean", "listingId": "", "price": "$30"}
        one = [self.card("1")]
        self.assertEqual(lv.match_in_search(one, target, need=1), "TEXT")
        self.assertEqual(lv.match_in_search(one, target, need=2), "")
        # the same card seen again after scrolling is still one card
        self.assertEqual(lv.match_in_search(one + [self.card("1")], target, need=2), "")
        self.assertEqual(lv.match_in_search([self.card("1"), self.card("2")], target, need=2), "TEXT")

    def test_describe_results_is_short_and_deduplicated(self):
        results = [{"text": "$30\nA\nHouston"}, {"text": "$30\nA\nHouston"}, {"text": "$9\nB"}]
        self.assertEqual(lv.describe_results(results), "$30 A Houston | $9 B")
        self.assertEqual(lv.describe_results([]), "no result cards")


class SampleDiversityTests(unittest.TestCase):
    def live(self, title, price="$34", listing_id=""):
        return {"state": "LIVE", "title": title, "price": price, "listingId": listing_id}

    def test_a_small_sample_covers_different_services_before_repeats(self):
        listings = [self.live("Furniture"), self.live("Furniture"), self.live("Furniture"),
                    self.live("Carpet"), self.live("Lights")]
        targets, skipped = lv.visibility_targets(listings, limit=3)
        self.assertEqual([t["title"] for t in targets], ["Furniture", "Carpet", "Lights"])
        self.assertEqual(skipped, 2)

    def test_same_title_with_a_different_price_counts_as_different(self):
        listings = [self.live("Furniture", "$34"), self.live("Furniture", "$34"), self.live("Furniture", "$40")]
        targets, _ = lv.visibility_targets(listings, limit=2)
        self.assertEqual([t["price"] for t in targets], ["$34", "$40"])

    def test_ids_still_come_first(self):
        listings = [self.live("A"), self.live("B"), self.live("A", listing_id="9")]
        targets, _ = lv.visibility_targets(listings, limit=2)
        self.assertEqual(targets[0]["listingId"], "9")
        self.assertEqual(targets[1]["title"], "A")


class BuyerSummaryTests(unittest.TestCase):
    def test_summary_counts_listings_by_verdict(self):
        rows = [
            {"PK": lv.SEARCH, "US": lv.SEARCH},
            {"PK": lv.TEXT_MATCH, "US": lv.NOT_FOUND},
            {"PK": lv.LINK_ONLY, "US": lv.NOT_FOUND},
            {"PK": lv.NOT_FOUND, "US": lv.NOT_FOUND},
            {"PK": lv.UNCLEAR, "US": lv.UNCLEAR},
            {"PK": lv.SEARCH, "US": lv.HIDDEN},
        ]
        text = lv.summarize_buyer_rows(rows)
        self.assertTrue(text.startswith("Buyer sample: 6 listings checked"))
        for part in ("1 visible in search", "1 visible to some buyers", "2 not in search", "1 hidden", "1 not verified"):
            self.assertIn(part, text)

    def test_summary_is_empty_when_nothing_ran_and_singular_for_one(self):
        self.assertEqual(lv.summarize_buyer_rows([]), "")
        self.assertEqual(lv.summarize_buyer_rows(None), "")
        self.assertIn("1 listing checked", lv.summarize_buyer_rows([{"PK": lv.SEARCH}]))


class UncheckedBuyerVerdictTests(unittest.TestCase):
    def test_an_unchecked_buyer_is_not_hidden_behind_a_clean_verdict(self):
        self.assertEqual(lv.listing_verdict([lv.ERROR, lv.NOT_FOUND]), "Not in search (a buyer could not check)")
        self.assertEqual(lv.listing_verdict([lv.ERROR, lv.TEXT_MATCH]), "Visible (title + price) (a buyer could not check)")
        self.assertEqual(lv.listing_verdict([lv.ERROR, lv.SEARCH]), "Visible in search (a buyer could not check)")
        self.assertEqual(lv.listing_verdict([lv.SEARCH, lv.NOT_FOUND]), "Visible to some buyers")
        self.assertEqual(lv.listing_verdict([lv.ERROR, lv.UNCLEAR]), "Not verified")

    def test_summary_still_counts_them_by_what_was_seen(self):
        rows = [{"PK": lv.ERROR, "US": lv.NOT_FOUND}, {"PK": lv.ERROR, "US": lv.SEARCH}]
        text = lv.summarize_buyer_rows(rows)
        self.assertIn("1 visible in search", text)
        self.assertIn("1 not in search", text)
