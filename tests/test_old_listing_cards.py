"""Regression cases from the sectioned and unsectioned Facebook card layouts."""
import html
import unittest

from core.automation import live_checker as lc
from core.automation import listing_status as ls
from core.automation import listing_visibility as lv
from core.automation import visibility_checker as vc
from core.automation.facebook_page import page_issue

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

    def test_persistent_clear_filters_control_does_not_block_the_owner_scan(self):
        self.render("""<h1>Your listings</h1>
          <input aria-label="Search your listings">
          <button aria-label="Clear filters" onclick="window.resets = (window.resets || 0) + 1">Clear</button>
          """ + card("Roof repair", "$50"))
        firsts = lc.read_first_listings_old_ui(self.page, self.sleep, lambda _: None, with_details=False)
        self.assertIsNotNone(firsts)
        self.assertEqual(firsts[0]["title"], "Roof repair")
        self.assertEqual(self.page.evaluate("window.resets"), 1)
        self.assertTrue(self.page.evaluate(lc._LISTINGS_VIEW_STATE_JS, "Search your listings")["hasClear"])

    def test_filter_reset_ignores_hidden_and_unrelated_clear_buttons(self):
        self.render("""<button aria-label="Clear filters" style="display:none" onclick="window.wrong = true">Clear</button>
          <button onclick="window.wrong = true">Clear</button>
          <button aria-label="Clear filters" onclick="window.reset = true">Clear</button>""")
        self.assertTrue(self.page.evaluate(lc._CLICK_CLEAR_FILTERS_JS))
        self.assertIsNone(self.page.evaluate("window.wrong"))
        self.assertTrue(self.page.evaluate("window.reset"))

    def test_failed_reset_and_remaining_filter_count_are_not_accepted(self):
        self.render("""<button aria-label="Clear filters" onclick="">Clear</button>
          <div role="button" aria-label="Filters">Filters (1)</div>""")
        # No navigation is needed to test the final state; keep the active filter after reset.
        from unittest import mock
        with mock.patch.object(self.page, "goto"), mock.patch.object(self.page, "wait_for_selector"):
            self.assertFalse(lc.ensure_default_listings_view(self.page, self.sleep, lambda _: None))
        self.render('<button disabled aria-label="Clear filters">Clear</button>')
        self.assertFalse(self.page.evaluate(lc._CLICK_CLEAR_FILTERS_JS))

    def test_twenty_plus_sidebar_is_a_lower_bound_not_an_exact_total(self):
        self.render("<h1>Your listings</h1>" + card("Only loaded one", "$50") + "<aside>20+ active listings</aside>")
        scan = ls.build_scan_result(*lc.read_all_listings_old_ui(self.page, self.sleep))
        self.assertIn("read 1 of 20", "; ".join(scan["notes"]))

    def test_search_cards_exclude_hidden_sidebar_dialog_and_recommendations(self):
        self.render("""<main>
          <h1>Search results</h1>
          <a href="/marketplace/item/111/">Requested listing</a>
          <a style="display:none" href="/marketplace/item/222/">Hidden listing</a>
          <nav><a href="/marketplace/item/333/">Sidebar</a></nav>
          <div role="dialog"><a href="/marketplace/item/444/">Dialog</a></div>
          <h2>Suggested listings</h2><a href="/marketplace/item/555/">Recommendation</a>
        </main>""")
        cards = self.page.evaluate(vc._SEARCH_RESULTS_JS)
        self.assertEqual([item["href"] for item in cards], ["/marketplace/item/111/"])
        self.assertFalse(lv.found_in_search(cards, {"listingId": "555"}))

    def test_search_reads_results_when_the_first_main_is_the_filters_sidebar(self):
        self.render("""<div role="main"><h1>Search results</h1><h2>Filters</h2><p>City, TX</p></div>
          <div role="main"><a href="/marketplace/item/123456/">Roof repair</a>
          <h2>Suggested listings</h2><a href="/marketplace/item/999999/">Other</a></div>""")
        self.assertEqual([row["href"] for row in self.page.evaluate(vc._SEARCH_RESULTS_JS)],
                         ["/marketplace/item/123456/"])

    def test_shown_search_area_is_read_without_guessing_from_the_account_label(self):
        self.render("<aside><div>McKinney, Texas</div><div>&nbsp; · Within 20 mi</div></aside>")
        self.assertEqual(self.page.evaluate(vc._SEARCH_AREA_JS), "McKinney, Texas — within 20 mi")

    def test_native_close_button_waits_for_the_dialog_animation(self):
        self.render("""<div role="dialog"><button aria-label="Close"
          onclick="setTimeout(() => this.parentElement.remove(), 1100)">Close</button></div>""")
        self.assertTrue(lc._close_listing_dialog(self.page, self.sleep))
        self.assertEqual(self.page.locator('[role="dialog"]:visible').count(), 0)

    def test_escape_closes_a_dialog_without_a_recognised_close_button(self):
        self.render('<div role="dialog">Your listing</div>')
        self.page.evaluate("""() => document.addEventListener('keydown', e => {
          if (e.key === 'Escape') document.querySelector('[role="dialog"]').remove();
        })""")
        self.assertTrue(lc._close_listing_dialog(self.page, self.sleep))

    def test_listing_identity_notice_is_not_an_account_checkpoint(self):
        self.render("<h1>Your listings</h1>" + card("Furniture", "$34", "Confirm your identity to publish"))
        self.assertEqual(page_issue(self.page), "")
        self.render('<h1>Confirm your identity</h1>')
        self.assertEqual(page_issue(self.page), "CHECKPOINT")
        self.render('<input name="email" style="display:none"><h1>Marketplace</h1>')
        self.assertEqual(page_issue(self.page), "")
        self.render('<input name="email">')
        self.assertEqual(page_issue(self.page), "LOGGED_OUT")

    def test_item_text_outside_the_item_heading_does_not_verify_link(self):
        self.render("<main><h1>Loading</h1><div>Roof repair</div></main>")
        headings = self.page.evaluate(vc._ITEM_HEADINGS_JS)
        url = "https://www.facebook.com/marketplace/item/111/"
        self.assertEqual(lv.classify_item_page(url, headings, "Roof repair", url), lv.UNCLEAR)

    def test_table_scrolls_and_resets_an_inner_panel(self):
        self.render("""<div id="panel" style="height:200px;overflow:auto">
          <table aria-label="Your listings"><tbody>
          <tr role="row" style="height:220px"><td>First</td></tr>
          <tr role="row" style="height:220px"><td>Last</td></tr>
          </tbody></table></div>""")
        self.page.evaluate(lc._SCROLL_LISTINGS_JS)
        self.assertGreater(self.page.locator("#panel").evaluate("el => el.scrollTop"), 0)
        lc._scroll_to_top(self.page)
        self.assertEqual(self.page.locator("#panel").evaluate("el => el.scrollTop"), 0)

    def test_new_table_reads_duplicate_notice_before_generic_active_status(self):
        self.render("""<table aria-label="Your listings"><tbody><tr role="row" data-index="0">
          <td role="gridcell" aria-colindex="2"><div>
          <a aria-label="View listing: Chair" href="/marketplace/item/123456/">Chair</a></div>
          <div>Category: Home</div><div>This looks like a duplicate so we didn't post it</div><div>Active</div></td>
          <td role="gridcell" aria-colindex="3">$34</td></tr></tbody></table>""")
        row = self.page.evaluate(lc._READ_LISTINGS_TABLE_JS)[0]
        listing = lc._listing_from_row(row, "All listings")
        self.assertEqual(listing["state"], ls.REJECTED)
        self.assertEqual(ls.build_scan_result([listing])["duplicates"], 1)

    def test_nested_scroll_panel_loads_all_cards(self):
        markup = [card(f"Nested {i}", "$50") for i in range(12)]
        self.render("<style>body{overflow:hidden}</style><h1>Your listings</h1>"
                    "<div id='panel' style='height:450px;overflow-y:auto'><div id='feed'></div></div>")
        self.page.evaluate("""(cards) => {
            let loaded = 0;
            const feed = document.querySelector('#feed'), panel = document.querySelector('#panel');
            function load() {
                for (let end = Math.min(cards.length, loaded + 5); loaded < end; loaded++)
                    feed.insertAdjacentHTML('beforeend', cards[loaded]);
            }
            load();
            panel.addEventListener('scroll', () => {
                if (panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 300) load();
            });
        }""", markup)
        cards = lc.read_old_section(self.page, lc.SECTION_ALL_LISTINGS, self.sleep, collect_all=True)
        self.assertEqual(len(cards), 12)

    def test_identical_cards_are_not_merged_into_one_listing(self):
        self.render("<h1>Your listings</h1>" + card("Furniture", "$34") * 3)
        scan = ls.build_scan_result(*lc.read_all_listings_old_ui(self.page, self.sleep))
        self.assertEqual(scan["total"], 3)
        self.assertEqual(scan["duplicates"], 0)  # repetition alone is not a Facebook duplicate rejection

    def test_buyer_ids_are_recovered_from_each_selected_cards_own_dialog(self):
        self.render("<h1>Your listings</h1>" + card("Roof repair service", "$50") +
                    card("Furniture assembly service", "$40") + card("Unselected", "$10"))
        cards = self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_ALL_LISTINGS)
        listings = [dict(lc.parse_old_card(c), state=ls.LIVE, listingId="", cardId=c["cardId"])
                    for c in cards]
        self.page.evaluate("""() => {
          document.querySelectorAll('[data-lp-card]').forEach((card, i) => {
            card.addEventListener('click', () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              const link = document.createElement('a');
              link.href = '/marketplace/item/' + (1234560 + i) + '/';
              link.textContent = card.querySelector('[data-lp-title]').innerText;
              dialog.appendChild(link);
              dialog.insertAdjacentHTML('beforeend',
                '<a href="/marketplace/item/9999999/">Unrelated recommendation</a>' +
                '<button role="button" aria-label="Close">Close</button>');
              dialog.querySelector('button').onclick = () => dialog.remove();
              document.body.appendChild(dialog);
            });
          });
        }""")
        lc.recover_old_sample_ids(self.page, listings, 2, self.sleep, lambda _: None)
        self.assertEqual([item["listingId"] for item in listings], ["1234560", "1234561", ""])
        self.assertEqual(self.page.locator('[role="dialog"]').count(), 0)

    def test_ambiguous_dialog_links_do_not_assign_a_listing_id(self):
        self.render("""<div role="dialog">
          <a href="/marketplace/item/1234560/">Roof repair</a>
          <a href="/marketplace/item/1234561/">Roof repair</a>
          <a href="https://example.com/marketplace/item/1234562/">Roof repair</a>
          </div>""")
        links = self.page.evaluate(lc._OLD_DIALOG_ITEM_LINK_JS, "Roof repair")
        ids = {lv.item_id_from_href(link) for link in links} - {""}
        self.assertEqual(ids, {"1234560", "1234561"})

    def test_existing_dialog_cannot_supply_another_cards_id(self):
        self.render("<h1>Your listings</h1>" + card("Roof repair", "$50"))
        selected = self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_ALL_LISTINGS)[0]
        listing = dict(lc.parse_old_card(selected), state=ls.LIVE, listingId="", cardId=selected["cardId"])
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
          '<div role="dialog"><a href="/marketplace/item/9999999/">Roof repair</a></div>')""")
        lc.recover_old_sample_ids(self.page, [listing], 1, self.sleep, lambda _: None)
        self.assertEqual(listing["listingId"], "")

    def install_sample_dialogs(self):
        self.page.evaluate("""() => {
          document.querySelectorAll('.card').forEach((card, index) => {
            card.addEventListener('click', () => {
              const dialog = document.createElement('div');
              dialog.setAttribute('role', 'dialog');
              const link = document.createElement('a');
              link.href = '/marketplace/item/' + (7000000 + index) + '/';
              link.textContent = card.querySelector('[data-lp-title]')?.innerText ||
                card.querySelector('.inner').children[1].innerText;
              dialog.appendChild(link);
              dialog.insertAdjacentHTML('beforeend', '<button aria-label="Close">Close</button>');
              dialog.querySelector('button').onclick = () => dialog.remove();
              document.body.appendChild(dialog);
            });
          });
        }""")

    def sample_listings(self):
        cards = self.page.evaluate(lc._READ_CARDS_JS, lc.SECTION_ALL_LISTINGS)
        return [dict(lc.parse_old_card(c), state=ls.LIVE, listingId="", cardId=c["cardId"],
                     clicksCount=lc.parse_count(lc.parse_old_card(c)["clicks"]), tab=lc.SECTION_ALL_LISTINGS)
                for c in cards]

    def discard_card_markers(self):
        self.page.evaluate("""() => {
          for (const el of document.querySelectorAll('.card,[data-lp-title]')) {
            for (const name of el.getAttributeNames()) if (name.startsWith('data-lp-')) el.removeAttribute(name);
          }
          // The nearest complete DOM root may be .inner instead of .card.
          for (const el of document.querySelectorAll('[data-lp-card-id],[data-lp-card],[data-lp-signature]')) {
            for (const name of el.getAttributeNames()) if (name.startsWith('data-lp-')) el.removeAttribute(name);
          }
        }""")

    def test_a_timed_out_click_reply_still_reads_and_closes_the_opened_dialog(self):
        from unittest import mock
        self.render("<h1>Your listings</h1>" + card("Lights", "$45") + card("Furniture", "$45"))
        listings = self.sample_listings()
        self.install_sample_dialogs()
        evaluate = self.page.evaluate
        interrupted = [False]

        def run(expression, arg=None):
            result = evaluate(expression, arg)
            if expression == lc._OPEN_SAMPLE_CARD_JS and not interrupted[0]:
                interrupted[0] = True
                raise RuntimeError("click reply timed out after the dialog opened")
            return result

        with mock.patch.object(self.page, "evaluate", side_effect=run):
            lc.recover_old_sample_ids(self.page, listings, 2, self.sleep, lambda _: None)
        self.assertEqual([item["listingId"] for item in listings], ["7000000", "7000001"])
        self.assertEqual(self.page.locator('[role="dialog"]:visible').count(), 0)

    def test_lost_dom_markers_are_rebound_to_a_unique_complete_card(self):
        self.render("<h1>Your listings</h1>" + card("Lights", "$45") + card("Furniture", "$45"))
        listings = self.sample_listings()
        self.discard_card_markers()
        self.install_sample_dialogs()
        lc.recover_old_sample_ids(self.page, listings, 2, self.sleep, lambda _: None)
        self.assertEqual([item["listingId"] for item in listings], ["7000000", "7000001"])

    def test_identical_unmarked_cards_are_not_rebound_by_title_and_price_alone(self):
        self.render("<h1>Your listings</h1>" + card("Lights", "$45") * 2)
        listings = self.sample_listings()
        self.discard_card_markers()
        self.install_sample_dialogs()
        lc.recover_old_sample_ids(self.page, listings, 2, self.sleep, lambda _: None)
        self.assertEqual([item["listingId"] for item in listings], ["", ""])
        self.assertEqual(self.page.locator('[role="dialog"]:visible').count(), 0)

    def test_a_lost_title_marker_does_not_need_a_slow_locator_text_read(self):
        self.render("<h1>Your listings</h1>" + card("Lights", "$45"))
        listings = self.sample_listings()
        self.page.evaluate("() => document.querySelectorAll('[data-lp-title]').forEach(el => el.removeAttribute('data-lp-title'))")
        self.install_sample_dialogs()
        lc.recover_old_sample_ids(self.page, listings, 1, self.sleep, lambda _: None)
        self.assertEqual(listings[0]["listingId"], "7000000")

    def test_a_recycled_card_with_changed_price_does_not_keep_the_old_identity(self):
        self.render("<h1>Your listings</h1>" + card("Lights", "$45"))
        listings = self.sample_listings()
        self.page.evaluate("""() => {
          const inner = document.querySelector('.inner');
          inner.children[2].textContent = '$40';
        }""")
        self.install_sample_dialogs()
        lc.recover_old_sample_ids(self.page, listings, 1, self.sleep, lambda _: None)
        self.assertEqual(listings[0]["listingId"], "")
        self.assertEqual(self.page.locator('[role="dialog"]:visible').count(), 0)


if __name__ == "__main__":
    unittest.main()
