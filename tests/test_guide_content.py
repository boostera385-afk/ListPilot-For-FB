import os
import unittest

from gui import guide_content as gc

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "TESTING_GUIDE.md")
NAV_KEYS = {"home", "guide", "fb_listing", "dominancy", "response", "live", "script", "settings"}


class GuideContentTests(unittest.TestCase):
    def test_step_ids_are_unique_and_complete(self):
        steps = gc.all_steps()
        ids = [s["id"] for s in steps]
        self.assertEqual(len(ids), len(set(ids)))
        for step in steps:
            self.assertTrue(step["title"] and step["body"])

    def test_buttons_point_to_real_pages(self):
        for step in gc.all_steps():
            if step.get("action"):
                self.assertIn(step["action"][1], NAV_KEYS)

    def test_guide_page_is_registered_in_the_sidebar(self):
        app_source = open(os.path.join(os.path.dirname(DOC), "..", "gui", "app.py"), encoding="utf-8").read()
        self.assertIn('("guide",', app_source)

    def test_every_step_title_is_in_the_markdown_guide(self):
        text = open(DOC, encoding="utf-8").read()
        for section, steps in gc.SECTIONS:
            self.assertIn(section, text)
            for step in steps:
                self.assertIn(step["title"], text)

    def test_auto_detection_and_progress(self):
        settings = {"apps_script_url": "https://script.google.com/x"}
        get = lambda key, default=None: settings.get(key, default)
        self.assertTrue(gc.auto_done("script_url", get))
        self.assertFalse(gc.auto_done("checkers", get))
        done, total = gc.progress({"start", "browser"}, get)
        self.assertEqual(total, len(gc.all_steps()))
        self.assertEqual(done, 3)  # two ticks + the detected URL
        settings["apps_script_url"] = "  "
        self.assertFalse(gc.auto_done("script_url", get))


if __name__ == "__main__":
    unittest.main()
