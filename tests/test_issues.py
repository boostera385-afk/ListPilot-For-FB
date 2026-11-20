import unittest
from unittest import mock

from core.automation import issues as iss
from core.automation import listing_visibility as lv
from core.automation import visibility_checker as vc
from core.automation.facebook_page import page_issue, continue_remembered_account

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class IssueVocabularyTests(unittest.TestCase):
    def test_every_issue_has_a_label_and_next_step(self):
        for code, (label, step, _) in iss.ISSUES.items():
            with self.subTest(code=code):
                self.assertTrue(label and step)
                self.assertEqual(iss.label(f"{code}: detail"), label)

    def test_chrome_errors_are_split_into_proxy_timeout_and_network(self):
        self.assertEqual(iss.from_net_error("ERR_SOCKS_CONNECTION_FAILED"), iss.PROXY_ERROR)
        self.assertEqual(iss.from_net_error("ERR_PROXY_CONNECTION_FAILED"), iss.PROXY_ERROR)
        self.assertEqual(iss.from_net_error("ERR_TUNNEL_CONNECTION_FAILED"), iss.PROXY_ERROR)
        self.assertEqual(iss.from_net_error("ERR_TIMED_OUT"), iss.PAGE_TIMEOUT)
        self.assertEqual(iss.from_net_error("ERR_INTERNET_DISCONNECTED"), iss.NETWORK_ERROR)

    def test_playwright_exceptions(self):
        self.assertEqual(iss.from_exception("Page.goto: net::ERR_SOCKS_CONNECTION_FAILED at https://x"), iss.PROXY_ERROR)
        self.assertEqual(iss.from_exception("Page.goto: Timeout 30000ms exceeded."), iss.PAGE_TIMEOUT)
        self.assertEqual(iss.from_exception("Target page, context or browser has been closed"), iss.BROWSER_CLOSED)
        self.assertEqual(iss.from_exception("something else"), "")

    def test_launch_failures_mention_the_proxy_when_it_is_the_cause(self):
        self.assertEqual(iss.from_launch_error("Proxy check failed: SOCKS5 auth"), iss.PROFILE_PROXY_FAILED)
        self.assertEqual(iss.from_launch_error("Profile is already open"), iss.PROFILE_START_FAILED)

    def test_buyer_states_are_specific(self):
        self.assertEqual(vc._checker_failure_state("LOGGED_OUT"), lv.LOGIN_REQUIRED)
        for code in (iss.PROXY_ERROR, iss.TWO_FACTOR, iss.TEMP_BLOCKED, iss.ACCOUNT_DISABLED, iss.NO_MARKETPLACE):
            self.assertEqual(vc._checker_failure_state(code), code)
        summary, step = lv.buyer_guidance({"PK": [iss.TEMP_BLOCKED], "US": [iss.PROXY_ERROR]})
        self.assertIn("PK buyer could not finish (Facebook temporarily blocked the account)", summary)
        self.assertIn("US buyer could not finish (proxy connection failed)", summary)
        self.assertIn("US buyer profile's proxy", step)
        self.assertIn("Proxy failed", lv.state_label(iss.PROXY_ERROR))
        self.assertIn("1 proxy failed", lv.summarize_checker([iss.PROXY_ERROR, lv.SEARCH]))

    def test_buyer_launch_failure_names_the_proxy(self):
        log = []
        with mock.patch.object(vc, "start_profile", return_value=(False, "Proxy connection failed")), \
             mock.patch("gui.ui_helpers.resolve_duplicates_ui", create=True), \
             mock.patch.object(vc, "update_column_values", return_value=(True, "ok")):
            post = {"rowNumber": 2, "_live_listings": [{"state": "LIVE", "title": "A", "listingId": "1"}]}
            options = lv.BuyerOptions(pk_profile="pk", us_enabled=False, limit=1)
            vc.run_visibility_checks([post], log.append, mock.Mock(is_set=lambda: False, wait=lambda s: False),
                                     options=options)
        self.assertEqual(post["_buyer_rows"][0]["PK"], iss.PROFILE_PROXY_FAILED)
        self.assertTrue(any("Proxy failed at launch" in line for line in log))


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class PageIssueBrowserTests(unittest.TestCase):
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
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def render(self, content):
        self.page.set_content(f"<html lang='en'><head><meta charset='utf-8'></head><body>{content}</body></html>")

    def test_headings_map_to_specific_issues(self):
        for heading, code in (("You're Temporarily Blocked", iss.TEMP_BLOCKED),
                              ("Your account has been disabled", iss.ACCOUNT_DISABLED),
                              ("Enter login code", iss.TWO_FACTOR),
                              ("Marketplace isn't available to you", iss.NO_MARKETPLACE),
                              ("Confirm your identity", iss.CHECKPOINT),
                              ("Marketplace", "")):
            with self.subTest(heading=heading):
                self.render(f"<h1>{heading}</h1>")
                self.assertEqual(page_issue(self.page), code)

    def test_listing_titles_do_not_trigger_account_issues(self):
        self.render('<a href="/marketplace/item/1/"><h2>You\'re temporarily blocked</h2></a>')
        self.assertEqual(page_issue(self.page), "")

    def test_continue_as_prompt_clicks_only_the_named_button(self):
        self.render("""<input name="email"><div role="dialog"><img src="data:,">
          <div role="button" aria-label="Close"></div><span>Kevin Flair</span>
          <div role="button" onclick="document.body.innerHTML='<h1>Marketplace</h1>'">Kevin Flair کے طور پر جاری رکھیں</div>
          <a href="#">Use another profile</a></div>""")
        self.assertEqual(page_issue(self.page), "LOGGED_OUT")
        self.assertTrue(continue_remembered_account(self.page, lambda _: None))
        self.assertEqual(page_issue(self.page), "")

    def test_password_prompt_is_never_clicked(self):
        self.render("""<input name="email"><div role="dialog"><img src="data:,"><span>Kevin</span>
          <input type="password"><div role="button">Kevin log in</div></div>""")
        self.assertFalse(continue_remembered_account(self.page, lambda _: None))


if __name__ == "__main__":
    unittest.main()
