import unittest
from unittest import mock

from core.services import sheets_service as svc
from core.automation import listing_status as ls


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class BatchWriteTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(svc, "get_script_url", return_value="https://script.example/exec")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_batched_request(self):
        with mock.patch.object(svc, "_requests_retry", return_value=FakeResponse({"success": True})) as post:
            ok, _ = svc.update_column_values("Live Checker", 5, {"Live Count": 3, "Check Result": "OK"})
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["action"], "updateColumnValues")

    def test_falls_back_to_per_column_on_old_script(self):
        def fake(method, url, **kwargs):
            if kwargs["json"]["action"] == "updateColumnValues":
                return FakeResponse({"success": False, "error": "Invalid action"})
            return FakeResponse({"success": True})

        with mock.patch.object(svc, "_requests_retry", side_effect=fake) as post:
            ok, _ = svc.update_column_values("Live Checker", 5, {"A": 1, "B": 2})
        self.assertTrue(ok)
        self.assertEqual(post.call_count, 3)  # 1 batch attempt + 2 single writes

    def test_fallback_reports_failed_columns(self):
        def fake(method, url, **kwargs):
            action = kwargs["json"]["action"]
            if action == "updateColumnValues":
                return FakeResponse({"success": False, "error": "Invalid action"})
            if kwargs["json"]["columnName"] == "B":
                return FakeResponse({"success": False, "error": "Column not found: B"})
            return FakeResponse({"success": True})

        with mock.patch.object(svc, "_requests_retry", side_effect=fake):
            ok, message = svc.update_column_values("Live Checker", 5, {"A": 1, "B": 2})
        self.assertFalse(ok)
        self.assertIn("B", message)

    def test_real_error_is_not_retried_per_column(self):
        with mock.patch.object(
            svc, "_requests_retry", return_value=FakeResponse({"success": False, "error": "Sheet not found: X"})
        ) as post:
            ok, message = svc.update_column_values("X", 5, {"A": 1})
        self.assertFalse(ok)
        self.assertEqual(post.call_count, 1)
        self.assertIn("Sheet not found", message)


class RecheckTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(svc, "get_script_url", return_value="https://script.example/exec")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_unknown_mode_without_a_request(self):
        with mock.patch.object(svc, "_requests_retry") as post:
            ok, _ = svc.reset_rows_for_recheck("Live Checker", "bogus")
        self.assertFalse(ok)
        post.assert_not_called()

    def test_returns_reset_count(self):
        with mock.patch.object(svc, "_requests_retry", return_value=FakeResponse({"success": True, "reset": 7})):
            self.assertEqual(svc.reset_rows_for_recheck("Live Checker", "failed"), (True, 7))

    def test_outdated_script_message(self):
        with mock.patch.object(
            svc, "_requests_retry", return_value=FakeResponse({"success": False, "error": "Invalid action"})
        ):
            ok, message = svc.reset_rows_for_recheck("Live Checker", "all")
        self.assertFalse(ok)
        self.assertIn("out of date", message)

    def test_history_is_best_effort(self):
        import requests

        with mock.patch.object(svc, "_requests_retry", side_effect=requests.exceptions.ConnectionError("x")):
            ok, message = svc.append_live_history("p1", 1, 1, 0, 0)
        self.assertFalse(ok)
        self.assertTrue(message)


class ScanNotesTests(unittest.TestCase):
    def test_notes_flow_through(self):
        scan = ls.build_scan_result([{"state": ls.LIVE, "title": "A", "listingId": "1"}], {"Active": 1}, ["x"])
        self.assertEqual(scan["notes"], ["x"])
        self.assertEqual(ls.build_scan_result([], {})["notes"], [])


if __name__ == "__main__":
    unittest.main()
