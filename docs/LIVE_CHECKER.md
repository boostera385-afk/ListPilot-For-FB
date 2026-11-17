# Live Checker v2

Opens each Facebook profile (Incogniton or AdsPower), reads Marketplace listings across **all tabs**, classifies every listing, and writes counts, details and a reason code to Google Sheets.

Status: written and unit tested (pure logic and sheet helpers). **Not yet verified against live Facebook** - see "Verifying" below.

---

## 1. How a check runs

One sheet row = one browser profile. For each pending row the app:

1. Claims the row, starts the profile, connects over CDP.
2. Opens `facebook.com/marketplace/seller/listings/`.
3. Stops early on `LOGGED_OUT` or `CHECKPOINT` (checkpoints are never retried, to avoid triggering more checks).
4. Reads the Messenger unread badge -> `Responses`.
5. Reads listings, trying in order:
   1. **New table** (`Your listings` with status tabs)
   2. **Older cards** (`/marketplace/you/selling`)
   3. **Marketplace home** (`/marketplace/?ref=bookmark`) -> follows the Selling link, matched by **URL, not text** (works in any language), then retries both layouts.
6. Scans every tab (see 2), classifies each listing, writes one batched update to the sheet, appends a `Live History` row.
7. If nothing could be read: saves a screenshot, picks a reason code (see 4), writes it to `Check Result`, fails the row with that message.

Random delays (1.5-3.5 s) separate navigation steps.

---

## 2. Full scan

- Every tab is discovered from the page (`[role="tab"]`), not hard-coded. Tabs with a `(0)` badge are skipped.
- The table is virtualised, so the last row is scrolled into view until no new rows appear (cap 400 rows per tab).
- "Problem" tabs are read first and `Active` last, so a listing shown on two tabs is counted by its worst state.
- Rows are de-duplicated by Facebook **listing ID**. Without an ID, rows are kept apart by tab + position (new table) or by title/price/date/clicks (older cards) - so many listings with the same title in different cities are not merged.
- If fewer rows are read than a tab's badge claims, or a tab is skipped, `Check Result` becomes `PARTIAL: ...`.
- A **240 s** budget per profile (`SCAN_BUDGET_SECONDS`) stops a slow account from holding a worker.

## 3. Listing statuses

`LIVE`, `PENDING`, `REJECTED`, `REMOVED`, `EXPIRED`, `SOLD`, `DRAFT`, `UNKNOWN`.

Rules (`classify_status`): the row's own status/notice text wins; the tab label fills gaps. So an "Active" row inside *Needs attention* is `REJECTED`, "In review" inside *Needs attention* stays `PENDING`, and a rejection notice beats an "Active" status.

## 4. Check Result codes

| Code | Meaning |
|---|---|
| `OK` | Full scan, nothing missing |
| `PARTIAL: ...` | Counts saved but scan incomplete (reason follows) |
| `LOGGED_OUT` | Profile not logged in |
| `CHECKPOINT` | Facebook identity/lock screen; not retried |
| `NO_MARKETPLACE` | Marketplace unavailable for the account |
| `LANG_NOT_EN` | Page language is not English (selectors are English). The app **detects** this; it does not change the account's language |
| `NO_LISTINGS` | Table empty / "start selling" |
| `LAYOUT_UNKNOWN` | Page loaded but matches no known layout |
| `TIMEOUT` | Blank page / nothing loaded |

Failure text includes the final URL, page language and the screenshot filename.

**Diagnostics:** screenshots are saved to `debug/` next to `main.py` (newest 50 kept). They stay on this computer; they may contain account content, so do not share the folder carelessly.

---

## 5. Sheet columns

Written to the **Live Checker** sheet (created automatically if missing):

| Column | Content |
|---|---|
| `Status` | Queue marker (empty/Pending = to do; `CLAIMED` = running; `Posted`/`Failed: ...` = done) |
| `Responses` | Messenger unread badge |
| `All Listings` / `Needs Attention` | First listing of each tab (older v1 summary, kept) |
| `Total Listings`, `Live Count`, `Pending Count`, `Rejected Count` | Full-scan counts |
| `Listing Details` | One line per listing: `LIVE \| Title \| 3 clicks \| 1 msg \| ID 123` (first 40) |
| `Check Result` | Code from section 4 |
| `Last Checked` | `YYYY-MM-DD HH:MM` |

**Live History** sheet (auto-created): `Time, Profile, Total, Live, Pending, Rejected, Check Result, Change`. `Change` compares with that profile's previous scan, e.g. `Live 5->3, Rejected 0->2`, or `First check` / `Check failed` / `No change`. Capped at 50,000 rows (oldest 5,000 dropped).

## 6. Check modes (dropdown in the Live Checker tab)

| Mode | Queues |
|---|---|
| New rows (empty Status) | Default; rows not yet processed |
| Re-check profiles with pending listings | Finished rows with `Pending Count` > 0 |
| Re-check failed profiles | `Failed...` status or `Check Result` not `OK` |
| Re-check every profile | All finished rows |

Re-check modes only clear `Status` on finished rows, then the normal pending fetch picks them up. Rows that are `CLAIMED` are never touched. A row stuck as `CLAIMED` after a crash must be reset by hand (it cannot be told apart from a row another machine is running).

At the end of a run the log shows total listings by status and profiles with problems by code.

---

## 7. Apps Script

File: `core/assets/apps_script.js`. **Redeploy after any change** (Extensions -> Apps Script -> paste -> run `setupSheets` once -> Deploy -> New version).

Actions added: `updateColumnValues` (batched write), `appendLiveHistory`, `resetForRecheck`. New auto-created columns: see section 5.

Safety: every written string goes through `setCellSafely_` - values starting with `= + - @` are stored as text (listing titles are untrusted; `=IMPORTXML(...)` must not run), and cells are capped at 50,000 characters.

Compatibility: with an *old* deployed script the app falls back to one request per column; history and re-check modes report "out of date" and the new columns are not created.

## 7a. Code map

| File | Role |
|---|---|
| `core/automation/listing_status.py` | Pure logic: statuses, classification, de-duplication, counts, detail text, reason-code diagnosis |
| `core/automation/live_checker.py` | Browser flow: tab discovery, scrolling scan, layout fallbacks, diagnostics, history/columns writes |
| `core/services/sheets_service.py` | `update_column_values`, `append_live_history`, `reset_rows_for_recheck` |
| `gui/views/live_checker_view.py` | Mode dropdown, concurrency, end-of-run totals |
| `core/assets/apps_script.js` | Sheet-side actions and safety |
| `tests/` | 25 unit tests |

Run tests (from the project folder): `.venv\Scripts\python.exe -m unittest discover -s tests -t .`

---

## 8. Verifying on real profiles

1. Redeploy the Apps Script; run `setupSheets`.
2. Put one profile that works and one that previously showed nothing in the Live Checker sheet.
3. Run, then read `Check Result` and, for failures, the screenshot in `debug/`.
4. The Facebook page selectors (`table[aria-label="Your listings"]`, tab ids, `View listing:` labels) are the part most likely to need tuning, because Facebook changes its markup and the full-scan code has only been tested on its pure logic.

## 9. Not built / known limits

- No matching of results back to the separate **posting** sheet (title + city); `Listing Details` carries IDs/titles to allow it later.
- No automatic account language change.
- No automatic reset of rows stuck as `CLAIMED`.
- Listing dates are read only for the first listing per tab (opening each listing would be slow and more detectable).
- Per-tab row cap 400; scan time limit 240 s per profile.
