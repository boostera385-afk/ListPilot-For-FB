import unittest

from gui import theme
from gui.animations import mix
from gui.widgets import classify_log_line, LOG_ERROR, LOG_WARN, LOG_OK, LOG_VIS, LOG_STEP, LOG_INFO


class LogColorTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_log_line("[Ali] ERROR: LOGGED_OUT"), LOG_ERROR)
        self.assertEqual(classify_log_line("Failed to launch Incogniton"), LOG_ERROR)
        self.assertEqual(classify_log_line("Note: could not save some sheet columns"), LOG_WARN)
        self.assertEqual(classify_log_line("[PK checker] 1/7 Roof repair: SEARCH"), LOG_VIS)
        self.assertEqual(classify_log_line("--- All Live Checks Completed ---"), LOG_OK)
        self.assertEqual(classify_log_line("--- Starting Grouped Automation Engine ---"), LOG_STEP)
        self.assertEqual(classify_log_line("Connecting to browser..."), LOG_INFO)


class ThemeTests(unittest.TestCase):
    def test_status_color_uses_code_prefix(self):
        self.assertEqual(theme.status_color("PARTIAL: tab skipped"), theme.AMBER)
        self.assertEqual(theme.status_color("hidden: 2 result(s)"), theme.PURPLE)
        self.assertEqual(theme.status_color("whatever"), theme.GRAY)

    def test_mix(self):
        self.assertEqual(mix("#000000", "#FFFFFF", 0), "#000000")
        self.assertEqual(mix("#000000", "#FFFFFF", 1), "#FFFFFF")
        self.assertEqual(mix("#000000", "#FF0000", 0.5), "#800000")


if __name__ == "__main__":
    unittest.main()
