import unittest

from core.automation import listing_status as ls
from core.automation.live_checker import _tab_badge, _tab_label


class ClassifyStatusTests(unittest.TestCase):
    def test_basic_states(self):
        self.assertEqual(ls.classify_status("Active", "Active"), ls.LIVE)
        self.assertEqual(ls.classify_status("Active", "In review"), ls.PENDING)
        self.assertEqual(ls.classify_status("Active", "Pending"), ls.PENDING)
        self.assertEqual(ls.classify_status("Sold", ""), ls.SOLD)
        self.assertEqual(ls.classify_status("Drafts", ""), ls.DRAFT)
        self.assertEqual(ls.classify_status("", "Expired"), ls.EXPIRED)
        self.assertEqual(ls.classify_status("", ""), ls.UNKNOWN)

    def test_rejection_notice_beats_active_status(self):
        self.assertEqual(
            ls.classify_status("All listings", "Active", "We rejected this listing for breaking our rules"),
            ls.REJECTED,
        )

    def test_needs_attention_tab_is_never_live(self):
        self.assertEqual(ls.classify_status("Needs attention", "Active"), ls.REJECTED)
        self.assertEqual(ls.classify_status("Needs attention", ""), ls.REJECTED)

    def test_in_review_inside_needs_attention_stays_pending(self):
        self.assertEqual(ls.classify_status("Needs attention", "In review"), ls.PENDING)

    def test_active_tab_defaults_to_live(self):
        self.assertEqual(ls.classify_status("Active", ""), ls.LIVE)


class CountingTests(unittest.TestCase):
    def test_counts_and_format(self):
        listings = [{"state": ls.LIVE}, {"state": ls.LIVE}, {"state": ls.PENDING}, {"state": ls.REJECTED}]
        counts = ls.count_statuses(listings)
        self.assertEqual(counts, {ls.LIVE: 2, ls.PENDING: 1, ls.REJECTED: 1})
        self.assertEqual(ls.format_counts(counts), "2 LIVE, 1 PENDING, 1 REJECTED")
        self.assertEqual(ls.format_counts({}), "No listings")

    def test_dedupe_by_listing_id(self):
        rows = [
            {"listingId": "1", "title": "A", "state": ls.LIVE},
            {"listingId": "1", "title": "A", "state": ls.REJECTED},
            {"listingId": "2", "title": "A", "state": ls.LIVE},
        ]
        self.assertEqual(len(ls.dedupe_listings(rows)), 2)

    def test_same_title_different_rows_are_kept_without_ids(self):
        rows = [
            {"title": "Roof", "price": "$50", "state": ls.LIVE, "tab": "Active", "index": "0"},
            {"title": "Roof", "price": "$50", "state": ls.LIVE, "tab": "Active", "index": "1"},
        ]
        self.assertEqual(len(ls.dedupe_listings(rows)), 2)

    def test_old_cards_repeated_across_sections_collapse(self):
        card = {"title": "Roof", "price": "$50", "state": ls.REJECTED, "status": "Active", "listed": "Listed on 9/26", "clicksCount": 0}
        self.assertEqual(len(ls.dedupe_listings([dict(card, tab="Needs attention"), dict(card, tab="All listings")])), 1)

    def test_details_are_capped(self):
        listings = [{"state": ls.LIVE, "title": f"T{i}", "listingId": str(i)} for i in range(50)]
        text = ls.format_listing_details(listings, limit=40)
        self.assertEqual(len(text.splitlines()), 41)
        self.assertTrue(text.endswith("... and 10 more"))

    def test_details_line_format(self):
        line = ls.format_listing_details(
            [{"state": ls.LIVE, "title": "Roof", "clicksCount": 1, "messagesCount": 2, "listingId": "9"}]
        )
        self.assertEqual(line, "LIVE | Roof | 1 click | 2 msgs | ID 9")

    def test_build_scan_result(self):
        scan = ls.build_scan_result([{"state": ls.LIVE, "title": "A", "listingId": "1"}], {"Active": 1})
        self.assertEqual(scan["total"], 1)
        self.assertEqual(scan["headline"], "1 LIVE")

    def test_tab_mismatch(self):
        self.assertIn("read 2 of 5", ls.tab_total_mismatch({"Active": 5}, [{}, {}]))
        self.assertIsNone(ls.tab_total_mismatch({"Active": 2}, [{}, {}]))


class DiagnoseTests(unittest.TestCase):
    def test_priority_and_codes(self):
        self.assertEqual(ls.diagnose_empty_page(logged_out=True), ls.LOGGED_OUT)
        self.assertEqual(ls.diagnose_empty_page(checkpoint=True), ls.CHECKPOINT)
        self.assertEqual(ls.diagnose_empty_page(lang="es", body_text="hola"), ls.LANG_NOT_EN)
        self.assertEqual(ls.diagnose_empty_page(lang="en", body_text="Marketplace isn't available to you"), ls.NO_MARKETPLACE)
        self.assertEqual(ls.diagnose_empty_page(url="https://x/checkpoint/1", lang="en", body_text="hi"), ls.CHECKPOINT)
        self.assertEqual(ls.diagnose_empty_page(lang="en", body_text=""), ls.TIMEOUT)
        self.assertEqual(ls.diagnose_empty_page(lang="en", body_text="Start selling"), ls.NO_LISTINGS)
        self.assertEqual(ls.diagnose_empty_page(lang="en", body_text="Some other page"), ls.LAYOUT_UNKNOWN)

    def test_format_check_result(self):
        self.assertEqual(ls.format_check_result(ls.OK, "ignored"), "OK")
        self.assertEqual(ls.format_check_result(ls.TIMEOUT, "slow"), "TIMEOUT: slow")


class TabTextTests(unittest.TestCase):
    def test_label_and_badge(self):
        self.assertEqual(_tab_label("Needs attention (9)"), "Needs attention")
        self.assertEqual(_tab_badge("Needs attention (9)"), 9)
        self.assertEqual(_tab_badge("Active (1,204)"), 1204)
        self.assertIsNone(_tab_badge("Active"))


if __name__ == "__main__":
    unittest.main()


class ScanQualityTests(unittest.TestCase):
    def test_unknown_statuses_are_reported_as_a_note(self):
        listings = [
            {"title": "A", "listingId": "1", "state": ls.LIVE},
            {"title": "B", "listingId": "2", "state": ls.UNKNOWN},
        ]
        scan = ls.build_scan_result(listings)
        self.assertEqual(len(scan["notes"]), 1)
        self.assertIn("1 of 2", scan["notes"][0])

    def test_clean_scan_has_no_notes(self):
        scan = ls.build_scan_result([{"title": "A", "listingId": "1", "state": ls.LIVE}])
        self.assertEqual(scan["notes"], [])

    def test_other_count_covers_everything_but_the_three_columns(self):
        counts = {ls.LIVE: 3, ls.PENDING: 1, ls.REJECTED: 2, ls.SOLD: 4, ls.UNKNOWN: 1}
        self.assertEqual(ls.other_count(counts), 5)
        self.assertEqual(ls.other_count({}), 0)
        scan = ls.build_scan_result([
            {"title": "A", "listingId": "1", "state": ls.SOLD},
            {"title": "B", "listingId": "2", "state": ls.LIVE},
        ])
        self.assertEqual(scan["other"], 1)

    def test_only_slow_page_reasons_are_retried(self):
        self.assertIn(ls.TIMEOUT, ls.RETRYABLE_CODES)
        self.assertIn(ls.LAYOUT_UNKNOWN, ls.RETRYABLE_CODES)
        for code in (ls.LOGGED_OUT, ls.CHECKPOINT, ls.NO_LISTINGS, ls.LANG_NOT_EN, ls.NO_MARKETPLACE):
            self.assertNotIn(code, ls.RETRYABLE_CODES)
