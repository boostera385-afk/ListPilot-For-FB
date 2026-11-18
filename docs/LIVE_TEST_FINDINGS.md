# Live testing findings, fixes and what is left

Written after the first real-account runs. "Fixed" means a code change plus a passing test. Nothing here claims more than was actually run.

## 1. Time counts

| What | Time | Source |
|---|---|---|
| Owner scan of the test run | about 2 min (about 14:34 to 14:36) | `logs/listpilot.log` |
| Buyer pass, before the speed-up: 15 listings x 2 buyer accounts = 30 checks | about 9 min (14:36 to 14:45), about 18 s per check | `logs/listpilot.log` |
| Buyer pass, after the speed-up (Quick sample = 3 listings x 2 buyers = 6 checks, fixed waits cut to 0.2 s, page waits capped at 5 s, repeated searches reused) | **not measured on a live account yet**; expect roughly 1-2 min | estimate |

Time is now counted by the app itself, so the next run gives real numbers:
- **Live Checker page:** `Phase - mm:ss - current listing` ticks every second.
- **Log:** `Full scan: ... in 1m 42s`, then `Owner scan time`, `Buyer sample finished in Ns` and `Total run time (owner scan, buyer sample)`.
- **Sheet:** the `Scan Time` column per profile, and `Buyer check took ...` in `Visibility Details`.

## 2. Problems found in real use, and the fix

| # | Problem found | Status |
|---|---|---|
| 1 | Windows showed the Python icon | Fixed: ListPilot airplane icon, taskbar identity and shortcut |
| 2 | Three tools had different, older dashboards | Fixed: FB Listings, Dominancy and Response now share the Live Checker dashboard |
| 3 | No way to tell if the browser or Sheets were connected | Fixed: verified connection indicators, background checks, manual refresh |
| 4 | Older Facebook cards misread: Active/date text joined, same title with a different price merged, pages with no sections, lazy-loaded cards missed, renewal tips counted as expired | Fixed (`tests/test_old_listing_cards.py`) |
| 5 | Identity notices counted as ordinary rejections; Other not visible; stale counts stayed after a failed read | Fixed: `ACTION_REQUIRED`, visible Other count, amber warnings, counts cleared on failure |
| 6 | Buyer pass slow (about 9 min for 30 checks) | Fixed: Quick / Standard / Extended sample (3 / 5 / 15), bounded waits, cached searches |
| 7 | **Misleading result:** 10 sampled listings without an ID were reported "in search" because a common title matched | Fixed: a search match now needs the exact Facebook listing ID. No ID means `UNCLEAR` / unverified |
| 8 | No way to turn buyer checks off | Fixed: master switch plus separate PK and US switches; saved profile IDs are kept |
| 9 | Buyer step turned green even for `NOT_IN_SEARCH`; summaries cut at the first comma; checker names showed as long IDs | Fixed: amber when anything needs a look, clearer text |
| 10 | A buyer account could also be in the seller queue (the "testing" row) | Fixed: blocked before any profile starts |
| 11 | Sheet looked plain and hard to scan | Fixed: see section 4 |
| 12 | Earlier test runs: a tab that never opened was read under the wrong label; the table scroll skipped rows | Fixed (see git history) |

## 3. Reading buyer results correctly
- A buyer result is a **sample**, not a full audit. The `Visibility Details` first line says how many of the live listings were checked.
- `LINK_ONLY` for the Pakistani account on a US-city listing is expected: search is location based. `HIDDEN` / "unavailable" is the real warning.
- The PK buyer's saved Marketplace location is Houston, Texas and the US buyer's is San Diego, California. A listing in another city may not appear in their search.

## 4. Sheet formatting (Apps Script)
Run `formatListPilotSheets` once (or use the **ListPilot** menu in the sheet). It never clears, moves or rewrites values or formulas, and never touches other tabs such as Sheet1.

- Dark-blue header row, frozen header and first two columns, filter, tab colour, wrapped text, alternating row shading.
- Column widths by purpose (ID wide, counts narrow, details very wide).
- Colour rules on Status, Check Result, Visibility, PK Check and US Check: **green** = success / OK, **amber** = partial, pending, claimed, unverified, link only, **red** = failed, logged out, checkpoint, hidden, error, **grey** = off, skipped, not checked.
- Count columns coloured when above 0 (Rejected red, Pending / Other amber, Live green).
- Hover notes on key headers explaining what each column means.
- Only the ID column is forced to plain text, so long IDs never turn into `1.2E+15`; counts and dates keep their own format.
- New columns the app adds copy the header style automatically.
- Re-running replaces only the helper's own colour rules and keeps yours.

## 5. Still open
1. **Redeploy the Apps Script** (new version), run `setupSheets` once, then `formatListPilotSheets`. Formatting could not be tested inside a real Sheet from here, only syntax-checked.
2. **Account `19 june id 6` has a Facebook checkpoint.** It needs the account owner to confirm identity by hand. ListPilot does not bypass it or retry it.
3. **US buyer location** is set in Marketplace, but its IP/country was not independently verified.
4. **The ten planned department rows are not ten verified published listings.** Buyer checks need real listing IDs from the owner scan.
5. **Buyer timing** after the speed-up is not measured live yet (section 1).
6. Row `testing` is an old failed seller row and is now the PK buyer. Do not use "Re-check every profile" on it.
