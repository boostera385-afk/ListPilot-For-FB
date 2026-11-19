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
- If fewer rows are read than a tab's badge claims, a tab is skipped or cannot be opened, or some listings have an unrecognised status (usually a non-English account), `Check Result` becomes `PARTIAL: ...`.
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

`TIMEOUT` and `LAYOUT_UNKNOWN` are retried once with a fresh page before anything is written; the other codes are never retried. `NO_LISTINGS` zeroes the count columns so old numbers do not look current.

Failure text includes the final URL, page language and the screenshot filename.

**Diagnostics:** screenshots are saved to `debug/` next to `main.py` (newest 50 kept). They stay on this computer; they may contain account content, so do not share the folder carelessly.

---

## 5. Sheet columns

Written to the **Live Checker** sheet (created automatically if missing):

| Column | Content |
|---|---|
| `Status` | Queue marker (empty/Pending = to do; `CLAIMED` = running; `Success`/`Failed: ...` = done) |
| `Responses` | Messenger unread badge |
| `All Listings` / `Needs Attention` | First listing of each tab (older v1 summary, kept) |
| `Total Listings`, `Live Count`, `Pending Count`, `Rejected Count`, `Other Count` | Full-scan counts. `Other Count` = removed, expired, sold, draft and unknown, so Total = Live + Pending + Rejected + Other |
| `Status Summary` | e.g. `3 LIVE, 1 PENDING, 2 SOLD` |
| `Duplicate Count` / `Duplicate Details` | Facebook-marked duplicate rejections only. A subset of Rejected, never added to Total. Same title/price alone is not proof of duplication; distinct listings are retained. |
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

Re-check modes only clear `Status` on finished rows, then the normal pending fetch picks them up. Rows that are `CLAIMED` are never touched. A row left as `CLAIMED` for over an hour (a crashed run) is freed automatically at the start of the next run. Each claim stores a timestamp note on the Status cell; rows claimed by an older script have none and are freed too.

At the end of a run the log shows total listings by status and profiles with problems by code.

---

## 6a. Visibility check (PK + US checker accounts)

The owner profile only tells us what Facebook shows *the seller*. To confirm a buyer can see a listing, two **other** accounts check it after all owner scans finish:

- Set them in **Settings -> Live Checker - Checker Profiles**: one Pakistani account, one USA account with a confirmed location. Empty = step skipped.
- Each checker profile opens **once** per run (PK first, then US) and, for every `LIVE` listing (max 15 per owner row):
  1. searches Marketplace by the title (scrolls 3 times) and looks in the results for the exact listing ID. A listing with no ID (older seller pages) is matched only on the exact title **and** exact price shown on a result card;
  2. if an ID listing is not in the results, opens `marketplace/item/<ID>` to tell "opens by link" from "unavailable". A listing with no ID has no link to try.
- **Search is the verdict.** A listing counts as visible only if it shows in search; opening by link never counts as visible.
- Identical listings with no ID (same title and price) each need their own result card, so one card cannot prove two listings. When a listing is not found, the log shows what the search returned.
- The sample takes listings with an ID first, then different title + price combinations before repeats. The PK and US buyers run side by side, so the pass takes about as long as one buyer.
- A profile named twice in the queue is checked once; the later row gets `Skipped: same profile as row N`.
- Results per listing: `SEARCH` (found in search by listing ID), `TEXT_MATCH` (found in search by title + price, no ID to confirm it), `NOT_FOUND` (searched, not in results, no ID to try the link), `LINK_ONLY` (opens by link but not in search near that account), `HIDDEN` (link says not available), `UNCLEAR`, `ERROR` (checker logged out / checkpoint / failed).
- Columns written: `PK Check`, `US Check` (e.g. `3/4 in search, 1 link only`), `Visibility` (`OK`, `HIDDEN: n`, `NOT_IN_SEARCH: n`, `PARTIAL: ...`), `Visibility Details` (one line per listing), `Visibility Checked`.
- `PK vs US` is inserted immediately after `US Check` by `setupSheets` without clearing existing rows. It compares the same sampled listings: **Same** (matching outcomes, even if both missed), **Different** (some outcomes differ), **Incomplete** (missing results, login/network errors), or **Not compared** (one buyer disabled/no sample). Counts say how many listings agree/differ. Title+price hits retain an identity-unverified warning. Existing rows populate on the next check, not during setup.
- Note: search is location based, so a Pakistani account will usually not find US-city listings in search; for it, `LINK_ONLY` is the expected "visible" result and `HIDDEN` is the real warning.
- **"Buyer: to review" card** = the number of *profiles* whose buyer result is not fully OK (3 profiles with a not-in-search listing shows 3, however many listings were sampled). The line under the cards says what was seen: `Buyer sample: 9 listings checked - 1 visible in search - 8 not in search`.
- **Buyer checks tab** in the app: one row per sampled listing with what the PK and US buyers saw (In search / Link opens, not in search / Not in search / Unavailable / Not verified) and a verdict (Visible in search, Visible to some buyers, Not in search, Hidden from a buyer).
- Stop works during this step. Checker logged out / checkpoint saves a screenshot to `debug/`.

### Interrupted runs and duplicate best practices

- `Off` means the buyer is disabled/unconfigured. An enabled buyer with a failed seller scan shows `Not checked: seller scan failed`, not Off.
- Buyer results distinguish `LOGIN_REQUIRED`, `CHECKPOINT` and `NETWORK_ERROR`. A login prompt does not prove an account ban or a hidden listing. Completed results survive a later interruption.
- Brief navigation failures get one bounded retry. Keep Wi-Fi/proxy stable during a run; after a persistent sign-in prompt, open that checker manually, sign in, then re-check. The app never submits credentials or dismisses an identity checkpoint.
- Incogniton startup is serialized, but scans remain concurrent. A single launch request runs in the background while ListPilot watches for that exact profile's local DevTools port (verified by `/json/version`). A browser that opens before the API replies is reused immediately, without stopping or relaunching it. Monitoring is bounded to 60 seconds and responds to Stop. An unresolved request is reported as `PROFILE_LAUNCH_PENDING`, not automatically replayed; check proxy/sync status and close the unfinished profile before re-checking. Manually opened profiles are never taken over.
- Keep one seller row per exact profile ID and keep PK/US checker profiles out of the seller queue. Do not delete/repost listings automatically just because titles repeat. Review Facebook's duplicate notice and listing IDs first.
- Update `core/assets/apps_script.js` in Apps Script, run `setupSheets`, then deploy a new version for the new duplicate columns. Existing rows are not retroactively rewritten; re-check to refresh them.

## 6b. Time counts and findings

`Scan Time` (sheet), `Owner scan time` / `Total run time` (log) and the live `phase - mm:ss` line show where time goes. Real-account findings, fixes and open items are in `docs/LIVE_TEST_FINDINGS.md`.

## 7. Apps Script

File: `core/assets/apps_script.js`. **Redeploy after any change** (Extensions -> Apps Script -> paste -> run `setupSheets` once -> Deploy -> New version).

Actions added: `updateColumnValues` (batched write), `appendLiveHistory`, `resetForRecheck` (modes `pending`, `failed`, `all`, `stuck`). New auto-created columns: see section 5.

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
| `tests/` | Unit tests (pure logic, sheet helpers) |

**Virtual browser test** (`tests/test_live_flow_virtual.py`, ~70 s): a local fake of the seller page (status tabs, virtualised table, listing dialog) is driven by the real flow in headless Chromium, with sheet writes captured. Covers a 65-listing mixed account, empty table, a tab that will not open, a non-English account and logged out. It proves the logic and scrolling, not Facebook's real markup; that still needs section 8. Skipped when Chromium is not installed (`.venv\Scripts\python.exe -m playwright install chromium`).

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
- Listing dates are read only for the first listing per tab (opening each listing would be slow and more detectable).
- Per-tab row cap 400; scan time limit 240 s per profile.
