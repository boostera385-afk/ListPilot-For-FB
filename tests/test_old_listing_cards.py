"""Regression cases from the sectioned and unsectioned Facebook card layouts."""
import html
import unittest

from core.automation import live_checker as lc
from core.automation import listing_status as ls

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def card(title, price, notice="Tip: Improve the description", status="Active", date="11/15"):
    title = html.escape(title)
    return f"""<div class="card" style="min-height:200px">
      <div class="inner">
        <div>{html.escape(notice)}</div><div>{title}</div><div>{price}</div>
        <div>{status}</div><div>&nbsp;</div><div> · Listed on {date}</div>
        <div>Listed on Marketplace</div><div>&nbsp;</div><div> · </div><div>5 clicks on listing</div>
        <div><button aria-label="More options for {title}"></button></div>
      </div>
      <div style="display:none"><button aria-label="More options for {title}"></button></div>
    </div>"""


class OldCardParsingTests(unittest.TestCase):
    def test_active_status_split_across_dom_lines(self):
        parsed = lc.parse_old_card({
            "title": "Spa repair",
            "text": "Tip: Improve the description\nSpa repair\n$50\nActive\n\u00a0\n · Listed on 11/15\n"
                    "Listed on Marketplace\n\u00a0\n · \n5 clicks on listing",
        })
        self.assertEqual(parsed["status"], "Active")
        self.assertEqual(parsed["clicks"], "5")
        self.assertEqual(ls.classify_status("All listings", parsed["status"], parsed["reason"]), ls.LIVE)

    def test_explicit_marketplace_publication_is_evidence_not_clicks_alone(self):
        listed = lc.parse_old_card({"title": "Spa", "text": "Spa\n$50\nListed on Marketplace\n5 clicks on listing"})
        self.assertEqual(listed["status"], "Listed on Marketplace")
        unclear = lc.parse_old_card({"title": "Spa", "text": "Tip: Improve the description\nSpa\n$50\n5 clicks on listing"})
        self.assertEqual(ls.classify_status("All listings", unclear["status"], unclear["reason"]), ls.UNKNOWN)

    def test_identity_notice_is_not_a_rejection_or_a_live_listing(self):
        notice = "Confirm your identity to publish this and any future listings"
        self.assertEqual(ls.classify_status("Needs attention", "Active", notice), ls.ACTION_REQUIRED)
        self.assertEqual(ls.classify_status("All listings", "Active", notice), ls.ACTION_REQUIRED)
        self.assertEqual(ls.classify_status("Needs attention", "Active",
                                          "We rejected this listing for breaking our rules"), ls.REJECTED)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class OldCardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium is unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.page.close)

    def render(self, content):
        self.page.set_content(f"<html lang='en'><head><meta charset='utf-8'></head><body>{content}</body></html>")

    def sleep(self, _seconds):
        # Flush browser scroll/render events without real network delays.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def test_same_title_different_prices_are_separate_cards(self):
        notice = "Confirm your identity to publish this and any future listings"
        self.render("<h2>Your listings</h2><h2>Needs attention</h2>" +
                    card("Furniture assembly", "$40", notice) +
                    card("Furniture assembly", "$35", notice) +
                    "<h2>All listings</h2>" + card("Spa repair", "$50"))
        needs = self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_NEEDS_ATTENTION)
        self.assertEqual(len(needs), 2)  # four responsive buttons, but two listings
        self.assertIn("$40", needs[0]["text"])
        self.assertNotIn("$35", needs[0]["text"])
        self.assertIn("$35", needs[1]["text"])
        scan = ls.build_scan_result(*lc.read_all_listings_old_ui(self.page, self.sleep))
        self.assertEqual(scan["total"], 3)
        self.assertEqual(scan["counts"], {ls.ACTION_REQUIRED: 2, ls.LIVE: 1})
        self.assertEqual(scan["other"], 2)
        self.assertEqual(scan["notes"], [])
        self.assertIn("Price $40", scan["details"])
        self.assertIn("Price $35", scan["details"])

    def test_unsectioned_feed_and_renew_tips_are_supported(self):
        self.render("<h1>Your listings</h1>" +
                    card("Lights", "$50", "Tip: Renew your listing?") +
                    card("Drywall", "$90", "Tip: Renew your listing?"))
        cards = self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_ALL_LISTINGS)
        self.assertEqual(len(cards), 2)
        self.assertEqual(self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_NEEDS_ATTENTION), [])
        parsed = [lc.parse_old_card(c) for c in cards]
        self.assertTrue(all(ls.classify_status("All listings", c["status"], c["reason"]) == ls.LIVE for c in parsed))

    def test_lazy_feed_is_scrolled_beyond_first_five_cards(self):
        markup = [card(f"Listing {i}", "$50", "Tip: Renew your listing?") for i in range(12)]
        self.render("<h1>Your listings</h1><div id='feed'></div><aside>12 active listings</aside>")
        self.page.evaluate("""(cards) => {
            let loaded = 0;
            const feed = document.querySelector('#feed');
            function load() {
                for (let end = Math.min(cards.length, loaded + 5); loaded < end; loaded++)
                    feed.insertAdjacentHTML('beforeend', cards[loaded]);
            }
            load();
            window.addEventListener('scroll', () => {
                if (window.scrollY + innerHeight >= document.body.scrollHeight - 300) load();
            });
        }""", markup)
        cards = lc.read_old_section(self.page, lc.SECTION_ALL_LISTINGS, self.sleep, collect_all=True)
        self.assertEqual(len(cards), 12)
        self.assertEqual(len({c["cardId"] for c in cards}), 12)
        self.assertEqual({c["title"] for c in cards}, {f"Listing {i}" for i in range(12)})

    def test_unread_sidebar_count_is_reported_as_partial(self):
        self.render("<h1>Your listings</h1>" + card("Only loaded one", "$50") + "<aside>12 active listings</aside>")
        scan = ls.build_scan_result(*lc.read_all_listings_old_ui(self.page, self.sleep))
        self.assertIn("read 1 of 12", "; ".join(scan["notes"]))


if __name__ == "__main__":
    unittest.main()
