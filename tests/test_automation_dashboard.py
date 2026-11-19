import threading
import unittest
from unittest import mock
from tkinter import TclError

import customtkinter as ctk

from gui import automation_dashboard as dashboard
from gui import theme
from gui.views import fb_listing_view as fb
from gui.views import dominancy_checker_view as dominancy
from gui.views import response_checker_view as response
from gui.views.live_checker_view import LiveCheckerView
from gui.views import settings_view
from core.automation import listing_visibility as lv


VIEW_CLASSES = (fb.FBListingView, dominancy.DominancyCheckerView, response.ResponseCheckerView)


def dispatch(func, *args):
    func(*args)


def sample_posts():
    # Two rows for the same profile must remain separate in the results table.
    return [{"rowNumber": 2, "Profile Name": "Test profile"},
            {"rowNumber": 3, "Profile Name": "Test profile"}]


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = ctk.CTk()
            cls.root.withdraw()
        except TclError as exc:
            raise unittest.SkipTest(f"Tk display is unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        for module in (dashboard, fb, dominancy, response):
            patcher = mock.patch.object(module, "run_on_ui", side_effect=dispatch)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(dashboard, "log_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, replacement in (
            ("count_up", lambda label, value: label.configure(text=str(value))),
            ("progress_to", lambda bar, value: bar.set(value)),
            ("fade_color", lambda widget, option, start, end, **kw: widget.configure(**{option: end})),
        ):
            patcher = mock.patch(f"gui.animations.{name}", side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_view(self, view_class=fb.FBListingView):
        view = view_class(self.root, fg_color=theme.BG)
        self.addCleanup(view.destroy)
        return view

    def test_initial_controls_and_existing_option_defaults(self):
        for view_class in VIEW_CLASSES:
            with self.subTest(view=view_class.__name__):
                view = self.make_view(view_class)
                self.assertIsInstance(view, dashboard.AutomationDashboard)
                self.assertEqual(view.start_btn.cget("state"),
                                 "disabled" if view.fetch_first else "normal")
                self.assertEqual(view.stop_btn.cget("state"), "disabled")
                self.assertEqual(view.continue_btn.cget("state"), "disabled")
                self.assertEqual(view.concurrency.get(), "3" if view_class is response.ResponseCheckerView else "1")
                self.assertEqual(view.tabs.get(), "Log")
                if view_class is fb.FBListingView:
                    self.assertFalse(view.live_check_var.get())
                    self.assertTrue(view.keep_open_var.get())
                if view_class is response.ResponseCheckerView:
                    self.assertTrue(view.keep_open_var.get())

    def test_fetch_shows_separate_rows_and_enables_start(self):
        view = self.make_view()
        view._finish_fetch(True, sample_posts())
        self.assertEqual(len(view.table.rows), 2)
        self.assertEqual(view.table.count_label.cget("text"), "2 rows")
        self.assertEqual(view.start_btn.cget("state"), "normal")
        self.assertEqual(view.stats["queued"].value.cget("text"), "2")
        self.assertEqual(view.tabs.get(), "Results")

    def test_failed_fetch_clears_stale_queue(self):
        view = self.make_view()
        view._finish_fetch(True, sample_posts())
        view._finish_fetch(False, "Sheet unavailable")
        self.assertEqual(view.pending_posts, [])
        self.assertEqual(view.table.rows, {})
        self.assertEqual(view.start_btn.cget("state"), "disabled")
        self.assertEqual(view.fetch_btn.cget("state"), "normal")
        self.assertEqual(view.tabs.get(), "Log")
        self.assertIn("Fetch failed", view.status_chip.cget("text"))

    def test_fetch_exception_restores_controls(self):
        view = self.make_view()
        view.is_fetching = True
        with mock.patch.object(dashboard, "fetch_pending_rows", side_effect=RuntimeError("Offline")):
            view._fetch_thread()
        self.assertFalse(view.is_fetching)
        self.assertEqual(view.fetch_btn.cget("state"), "normal")
        self.assertIn("Offline", "\n".join(text for text, _ in view.log_view.lines))

    def test_duplicate_fetch_and_start_during_fetch_are_blocked(self):
        view = self.make_view()
        view.pending_posts = sample_posts()
        with mock.patch.object(dashboard.threading, "Thread") as thread:
            view.fetch_posts()
            view.fetch_posts()
            view.start_posting()
        thread.assert_called_once()
        self.assertTrue(view.is_fetching)
        self.assertEqual(view.concurrency.plus.cget("state"), "disabled")

    def test_running_paused_and_idle_control_states(self):
        for view_class in VIEW_CLASSES:
            with self.subTest(view=view_class.__name__):
                view = self.make_view(view_class)
                view.pending_posts = sample_posts()
                for mode in ("preparing", "running", "paused"):
                    view._set_run_controls(mode)
                    self.assertEqual(view.start_btn.cget("state"), "disabled")
                    self.assertEqual(view.concurrency.minus.cget("state"), "disabled")
                    self.assertEqual(view.concurrency.plus.cget("state"), "disabled")
                    for option in view._option_widgets:
                        self.assertEqual(option.cget("state"), "disabled")
                    self.assertEqual(view.stop_btn.cget("state"), "normal" if mode == "running" else "disabled")
                    self.assertEqual(view.continue_btn.cget("state"), "normal" if mode == "paused" else "disabled")
                view._set_run_controls("idle")
                self.assertEqual(view.start_btn.cget("state"), "normal")
                self.assertEqual(view.concurrency.plus.cget("state"), "normal")

    def test_completion_counts_each_row_once_and_shows_details(self):
        view = self.make_view()
        posts = sample_posts()
        view._begin_run(posts)
        view.engine = mock.Mock(is_running=True, is_paused=False)
        result = (True, "Listing saved")
        self.assertIs(view.capture_result(posts[0], result), result)
        view._on_post_done(posts[0], True)
        view._on_post_done(posts[0], True)
        view._on_post_done(posts[1], False)
        self.assertEqual((view.done_count, view.success_count), (2, 1))
        self.assertEqual(view.stats["failed"].value.cget("text"), "1")
        self.assertEqual(view.table.rows[2]["values"]["Details"], "Listing saved")
        self.assertEqual(view.table.rows[3]["values"]["Result"], "Failed")
        self.assertEqual(view.progress.bar.get(), 1)
        view._finish_run()
        self.assertFalse(view.is_processing)
        self.assertIn("Completed with errors", view.status_chip.cget("text"))
        self.assertEqual(view.start_btn.cget("state"), "disabled")

    def test_preparation_failure_keeps_queue_retryable(self):
        view = self.make_view()
        view.pending_posts = sample_posts()
        view._begin_run(view.pending_posts)
        view._preparation_failed("Browser error")
        self.assertFalse(view.is_processing)
        self.assertEqual(view.start_btn.cget("state"), "normal")
        self.assertEqual(view.keep_open_checkbox.cget("state"), "normal")
        self.assertEqual(view.tabs.get(), "Log")

    def test_response_no_pending_rows_restores_start(self):
        view = self.make_view(response.ResponseCheckerView)
        view._begin_run([])
        with mock.patch.object(response, "fetch_pending_rows", return_value=(True, [])):
            view._prepare_and_run(True, 3, self.root)
        self.assertFalse(view.is_processing)
        self.assertEqual(view.start_btn.cget("state"), "normal")
        self.assertIn("No pending rows", view.status_chip.cget("text"))

    def test_worker_log_only_enqueues_ui_work(self):
        view = self.make_view()
        with mock.patch.object(dashboard, "run_on_ui") as enqueue:
            with mock.patch.object(view.log_view, "append") as append:
                worker = threading.Thread(target=view.log, args=("Worker message",))
                worker.start()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                append.assert_not_called()
                enqueue.assert_called_once_with(append, "Worker message")

    def test_live_checker_displays_other_counts_and_unknown_failed_totals(self):
        view = self.make_view(LiveCheckerView)
        view.run_options = lv.BuyerOptions(pk_profile="pk", us_profile="us")
        view.total_count = 2
        view._on_profile_done({
            "rowNumber": 3, "Profile Name": "Seller",
            "_live_check": "OK", "_live_counts": {"LIVE": 1, "ACTION_REQUIRED": 2},
        }, True)
        self.assertEqual(view.table.rows[3]["values"]["Total"], 3)
        self.assertEqual(view.table.rows[3]["values"]["Other"], 2)
        self.assertIn("needs action: 2", view.other_summary.cget("text"))
        view._on_profile_done({"rowNumber": 2, "Profile Name": "Unread", "_live_check": "LAYOUT_UNKNOWN"}, False)
        self.assertEqual(view.table.rows[2]["values"]["Total"], "—")
        self.assertEqual(view.table.rows[2]["values"]["Live"], "—")
        self.assertIn("seller scan failed", view.table.rows[2]["values"]["PK"])
        self.assertIn("seller scan failed", view.table.rows[2]["values"]["US"])
        self.assertEqual(view.table.rows[2]["values"]["Visibility"], "NOT_CHECKED")

    def test_progress_can_finish_with_a_warning_color(self):
        view = self.make_view()
        view.progress.finish("2 processed; 1 needs attention", theme.AMBER)
        self.assertEqual(view.progress.badge.cget("fg_color"), theme.AMBER)
        view.progress.reset()
        self.assertEqual(view.progress.badge.cget("text"), "1")

    def test_buyer_settings_switches_preserve_disabled_profile_ids(self):
        values = {"live_check_pk_profile": "pk-id", "live_check_us_profile": "us-id"}
        with mock.patch.object(settings_view, "get_setting", side_effect=lambda key, default=None: values.get(key, default)), \
             mock.patch.object(settings_view, "set_setting") as save:
            view = self.make_view(settings_view.SettingsView)
            view.buyers_enabled_var.set(False)
            view._update_buyer_controls()
            self.assertEqual(view.pk_checker_entry.cget("state"), "disabled")
            view.on_save()
            saved = dict(call.args for call in save.call_args_list)
        self.assertFalse(saved["live_check_buyers_enabled"])
        self.assertEqual(saved["live_check_pk_profile"], "pk-id")
        self.assertEqual(saved["live_check_us_profile"], "us-id")
        self.assertEqual(saved["live_check_sample_size"], 3)

    def test_all_three_engine_callbacks_update_dashboard(self):
        specs = (
            (fb, fb.FBListingView, "AutomationEngine", "run_fb_listing_flow"),
            (dominancy, dominancy.DominancyCheckerView, "GroupedAutomationEngine", "run_dominancy_checker_flow"),
            (response, response.ResponseCheckerView, "GroupedAutomationEngine", "run_response_checker_flow"),
        )
        for module, view_class, engine_name, flow_name in specs:
            with self.subTest(view=view_class.__name__):
                view = self.make_view(view_class)
                posts = sample_posts()
                engine = mock.Mock(is_running=True, is_paused=False)

                def start(**kwargs):
                    self.assertEqual(kwargs["task_type"], view.sheet_name)
                    for post in kwargs["posts"]:
                        result = kwargs["flow_wrapper_function"]("http://debug.test", post, lambda _: None)
                        self.assertEqual(result, (True, "Saved test result"))
                        kwargs["completion_callback"](post, True)
                    engine.is_running = False
                    kwargs["on_finished_callback"]()
                    return True

                engine.start.side_effect = start
                with mock.patch.object(module, engine_name, return_value=engine):
                    with mock.patch.object(module, "is_incogniton_running", return_value=True):
                        with mock.patch.object(module, "wait_for_incogniton_ready", return_value=(True, "Ready")):
                            with mock.patch.object(module, flow_name, return_value=(True, "Saved test result")) as flow:
                                if view_class is response.ResponseCheckerView:
                                    view._begin_run([])
                                    with mock.patch.object(response, "fetch_pending_rows", return_value=(True, posts)):
                                        view._prepare_and_run(True, 3, self.root)
                                else:
                                    view.pending_posts = posts
                                    view._begin_run(posts)
                                    if view_class is fb.FBListingView:
                                        view._posting_thread(1, True, True)
                                    else:
                                        view._posting_thread(1)
                                self.assertEqual(flow.call_count, 2)
                self.assertFalse(view.is_processing)
                self.assertEqual((view.total_count, view.done_count, view.success_count), (2, 2, 2))
                self.assertEqual(view.table.rows[2]["values"]["Details"], "Saved test result")
                self.assertIn("Completed", view.status_chip.cget("text"))


if __name__ == "__main__":
    unittest.main()
