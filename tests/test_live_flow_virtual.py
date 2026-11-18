"""Virtual end-to-end test of the Live Checker browser flow.

A local web server plays the Facebook seller "Your listings" page (status tabs,
a virtualised table, a listing dialog). The real ``run_live_checker_flow`` drives
a real headless Chromium against it, and the sheet writes are captured instead of
sent. No Facebook account, network or Google Sheet is involved.

Skipped automatically when Playwright's Chromium is not installed.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from core.automation import listing_status as ls
from core.engine.exceptions import PermanentError, RetryableError

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


def _row(i, title, status, tab_extra=None):
    data = {"id": str(1000000 + i), "title": title, "status": status, "price": "$50",
            "clicks": i % 7, "saves": 0, "messages": i % 3}
    data.update(tab_extra or {})
    return data


def _active(n, prefix="Roof repair"):
    # Same title in every row on purpose: IDs, not titles, keep listings apart.
    return [_row(i, f"{prefix}", "Active") for i in range(n)]


# scenario name -> tabs [(id, label, [rows])], or a special page kind
SCENARIOS = {
    "mixed": {
        "tabs": [
            ("active", "Active", _active(60)[:57] + [_row(57, "Fresh one", "In review"),
                                                       _row(58, "Fresh two", "In review"),
                                                       _row(59, "Fresh three", "In review")]),
            ("needs_attention", "Needs attention", [_row(100 + i, f"Rejected {i}", "Active") for i in range(3)]),
            ("sold", "Sold", [_row(200 + i, f"Sold item {i}", "Sold") for i in range(2)]),
        ],
    },
    "empty": {"tabs": [("active", "Active", [])]},
    "stubborn_tab": {
        "tabs": [
            ("active", "Active", _active(4)),
            ("needs_attention", "Needs attention", [_row(100, "Bad one", "Active")]),
        ],
        "broken_tabs": ["needs_attention"],  # clicking it never selects it
    },
    "spanish": {
        "tabs": [
            ("active", "Activos", [_row(i, f"Reparacion {i}", "Disponible ahora") for i in range(3)]),
            ("needs_attention", "Requiere atencion", [_row(100, "Techo", "Rechazado por reglas")]),
        ],
    },
    "logged_out": {"login": True},
}


PAGE = r"""<!doctype html><html lang="%(lang)s"><head><meta charset="utf-8"><title>fake fb</title>
<style>body{margin:0;font:14px sans-serif} tr{height:40px} td,th{padding:0 8px}</style></head><body>
%(body)s
<script>
const CFG = %(cfg)s;
const ROW_H = 40, BUFFER = 6, VISIBLE = 18;
let current = CFG.tabs.length ? CFG.tabs[0].id : '';
function tabRows(){ const t = CFG.tabs.find(t => t.id === current); return t ? t.rows : []; }
function render(){
  const tbody = document.querySelector('tbody'); if (!tbody) return;
  const rows = tabRows();
  const first = Math.max(0, Math.floor(window.scrollY / ROW_H) - BUFFER);
  const last = Math.min(rows.length, first + VISIBLE + 2 * BUFFER);
  let html = '<tr style="height:' + (first * ROW_H) + 'px"><td colspan="7"></td></tr>';
  for (let i = first; i < last; i++) {
    const r = rows[i];
    html += '<tr role="row" data-index="' + i + '" style="height:' + ROW_H + 'px">' +
      '<td role="gridcell" aria-colindex="1"></td>' +
      '<td role="gridcell" aria-colindex="2"><div><a href="/marketplace/item/?listing_id=' + r.id + '" aria-label="View listing: ' + r.title + '">' + r.title + '</a></div>' +
      '<div>Category: Home</div><div>' + r.status + '</div></td>' +
      '<td role="gridcell" aria-colindex="3">' + r.price + '</td><td role="gridcell" aria-colindex="4"></td>' +
      '<td role="gridcell" aria-colindex="5">' + r.clicks + '</td><td role="gridcell" aria-colindex="6">' + r.saves + '</td>' +
      '<td role="gridcell" aria-colindex="7">' + r.messages + '</td></tr>';
  }
  html += '<tr style="height:' + Math.max(0, (rows.length - last) * ROW_H) + 'px"><td colspan="7"></td></tr>';
  tbody.innerHTML = html;
  document.querySelectorAll('[role=tab]').forEach(t => t.setAttribute('aria-selected', String(t.id === current)));
}
window.addEventListener('scroll', render);
document.addEventListener('click', (e) => {
  const tab = e.target.closest('[role=tab]');
  if (tab) { if (!CFG.broken_tabs.includes(tab.id)) { current = tab.id; window.scrollTo(0, 0); render(); } return; }
  const view = e.target.closest('[aria-label^="View listing: "]');
  if (view) {
    e.preventDefault();
    const d = document.createElement('div'); d.setAttribute('role', 'dialog');
    d.innerHTML = '<div>Your Listing</div><div>This Listing Goes Against Our Rules</div><div>$50 · Atlanta, GA</div>' +
      '<span>Posted <abbr aria-label="4 weeks ago">4w</abbr> in Atlanta, GA</span>' +
      '<div aria-label="Close" role="button" style="width:40px;height:20px">x</div>';
    d.style.cssText = 'position:fixed;top:40px;left:40px;background:#fff;border:1px solid #000;padding:10px;z-index:9';
    document.body.appendChild(d); return;
  }
  const close = e.target.closest('[aria-label="Close"]');
  if (close) { const d = close.closest('[role=dialog]'); if (d) d.remove(); }
});
render();
</script></body></html>"""


def _build_page(name):
    cfg = SCENARIOS[name]
    if cfg.get("login"):
        body = '<form><input name="email" type="text"><input name="pass" type="password"></form>'
        payload = {"tabs": [], "broken_tabs": []}
    else:
        tabs_html = "".join(
            f'<div role="tab" id="{tid}" aria-selected="false">{label}{f" ({len(rows)})" if rows else " (0)"}</div>'
            for tid, label, rows in cfg["tabs"]
        )
        # A (0) badge on an empty Active tab would be skipped; show the real table anyway.
        body = (
            f'<div role="tablist">{tabs_html}</div>'
            '<table aria-label="Your listings"><thead><tr>'
            '<th role="columnheader" aria-colindex="1"></th><th role="columnheader" aria-colindex="2">Item</th>'
            '<th role="columnheader" aria-colindex="3">Price</th><th role="columnheader" aria-colindex="4">Date</th>'
            '<th role="columnheader" aria-colindex="5">Clicks</th><th role="columnheader" aria-colindex="6">Saves</th>'
            '<th role="columnheader" aria-colindex="7">Messages</th></tr></thead><tbody></tbody></table>'
        )
        payload = {"tabs": [{"id": t, "rows": r} for t, _, r in cfg["tabs"]],
                   "broken_tabs": cfg.get("broken_tabs", [])}
    lang = "es" if name == "spanish" else "en"
    return PAGE % {"lang": lang, "body": body, "cfg": json.dumps(payload)}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        name = parts[0] if parts and parts[0] in SCENARIOS else None
        if name is None:
            self.send_response(404)
            self.end_headers()
            return
        data = _build_page(name).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class VirtualLiveCheckerFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        try:
            with sync_playwright() as p:
                exe = p.chromium.executable_path
            if not os.path.exists(exe):
                raise FileNotFoundError(exe)
        except Exception as exc:
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium is not available: {exc}")

        cls.profile_dir = tempfile.mkdtemp(prefix="lp_virtual_")
        port = _free_port()
        cls.debug_url = f"http://127.0.0.1:{port}"
        cls.browser = subprocess.Popen(
            [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={cls.profile_dir}",
             "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{cls.debug_url}/json/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            cls.tearDownClass()
            raise unittest.SkipTest("Chromium did not start")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.terminate()
            cls.browser.wait(timeout=10)
        except Exception:
            pass
        cls.server.shutdown()
        shutil.rmtree(cls.profile_dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------
    def run_flow(self, scenario, post=None):
        from core.automation import live_checker as lc

        writes, history = [], []

        def fake_update(sheet, row, values):
            writes.append(dict(values))
            return True, "ok"

        def fake_history(profile, **kwargs):
            history.append({"profile": profile, **kwargs})
            return True, "ok"

        post = post if post is not None else {"rowNumber": 5, "profileName": f"virtual-{scenario}"}
        logs = []
        url = f"{self.base}/{scenario}/marketplace/seller/listings/"
        with mock.patch.object(lc, "FB_SELLER_LISTINGS", url), \
                mock.patch.object(lc, "update_column_values", fake_update), \
                mock.patch.object(lc, "append_live_history", fake_history), \
                mock.patch.object(lc.random, "uniform", lambda a, b: a * 0.4), \
                mock.patch("core.automation.fb_listing.read_messenger_unread_count", lambda page, sleep: 2), \
                mock.patch.object(lc, "DEBUG_DIR", os.path.join(self.profile_dir, "debug")):
            outcome, error = None, None
            try:
                outcome = lc.run_live_checker_flow(self.debug_url, post, logger=logs.append)
            except Exception as exc:  # PermanentError / RetryableError are asserted by tests
                error = exc
        merged = {}
        for values in writes:
            merged.update(values)
        return outcome, error, merged, history, logs, post

    # -- scenarios -------------------------------------------------------------
    def test_mixed_account_full_scan(self):
        outcome, error, sheet, history, logs, post = self.run_flow("mixed")
        self.assertIsNone(error, logs[-5:])
        self.assertTrue(outcome[0])
        # 57 active + 3 in review + 3 needs attention + 2 sold, all read across the virtualised table.
        self.assertEqual(sheet["Total Listings"], 65)
        self.assertEqual(sheet["Live Count"], 57)
        self.assertEqual(sheet["Pending Count"], 3)
        self.assertEqual(sheet["Rejected Count"], 3)
        self.assertEqual(sheet["Other Count"], 2)
        self.assertEqual(sheet["Check Result"], ls.OK)
        self.assertIn("57 LIVE", sheet["Status Summary"])
        self.assertEqual(sheet["Responses"], "2")
        self.assertTrue(sheet["Last Checked"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["live"], 57)

    def test_empty_table_zeroes_counts(self):
        outcome, error, sheet, history, logs, post = self.run_flow("empty")
        self.assertIsInstance(error, PermanentError)
        self.assertTrue(str(error).startswith(ls.NO_LISTINGS))
        self.assertEqual(sheet["Total Listings"], 0)
        self.assertEqual(sheet["Live Count"], 0)
        self.assertEqual(sheet["Other Count"], 0)
        self.assertEqual(sheet["Listing Details"], "")
        self.assertTrue(sheet["Check Result"].startswith(ls.NO_LISTINGS))

    def test_tab_that_cannot_open_gives_partial(self):
        outcome, error, sheet, history, logs, post = self.run_flow("stubborn_tab")
        self.assertIsNone(error, logs[-5:])
        self.assertTrue(sheet["Check Result"].startswith("PARTIAL"), sheet["Check Result"])
        self.assertIn("could not be opened", sheet["Check Result"])
        self.assertEqual(sheet["Live Count"], 4)

    def test_non_english_account_is_not_reported_ok(self):
        outcome, error, sheet, history, logs, post = self.run_flow("spanish")
        self.assertIsNone(error, logs[-5:])
        self.assertTrue(sheet["Check Result"].startswith("PARTIAL"), sheet["Check Result"])
        self.assertIn("unrecognised status", sheet["Check Result"])
        self.assertGreater(sheet["Other Count"], 0)

    def test_logged_out_is_permanent_and_recorded(self):
        outcome, error, sheet, history, logs, post = self.run_flow("logged_out")
        self.assertIsInstance(error, PermanentError)
        self.assertTrue(sheet["Check Result"].startswith(ls.LOGGED_OUT))
        self.assertEqual(post["_live_check"], ls.LOGGED_OUT)
        self.assertEqual(len(history), 1)


if __name__ == "__main__":
    unittest.main()
